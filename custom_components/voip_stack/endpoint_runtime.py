"""Runtime SIP endpoint/B2BUA orchestration for VoIP Stack."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import replace
import logging
import time

from homeassistant.core import HomeAssistant

from .audio_format import HA_SIP_PCM_FORMATS, HA_SIP_PCM_RX_FORMATS, HA_SIP_PCM_TX_FORMATS, HA_TRUNK_AUDIO_FORMATS
from .const import (
    CONF_REGISTRAR_ENABLED,
    CONF_TRUNK_AUTH_USERNAME,
    CONF_TRUNK_DTMF_ENABLED,
    CONF_TRUNK_DTMF_ROUTES,
    CONF_TRUNK_DTMF_TERMINATOR,
    CONF_TRUNK_DTMF_TIMEOUT_MS,
    CONF_TRUNK_INBOUND_DEFAULT_TARGET,
    CONF_TRUNK_OUTBOUND_PROXY,
    CONF_TRUNK_PASSWORD,
    CONF_TRUNK_PORT,
    CONF_TRUNK_SERVER,
    CONF_TRUNK_TRANSPORT,
    CONF_TRUNK_USERNAME,
    DOMAIN,
    HA_SOFTPHONE_DEVICE_ID,
)
from .endpoint_lifecycle import call_registry as _call_registry, async_stop_sip_endpoint
from .endpoint_routing import (
    peer_audio_formats as _peer_audio_formats,
    peer_for_target as _peer_for_target,
    roster_entry_formats as _roster_entry_formats,
    roster_from_peers as _roster_from_peers,
    same_route_name as _same_route_name,
    sip_target_audio_profile as _sip_target_audio_profile,
)
from .dtmf import parse_dtmf_route_map
from .fsm import (
    CallState,
    TerminalReason,
    sip_failure_response as _sip_failure_response,
    sip_public_state as _sip_public_state,
    sip_terminal_reason as _sip_terminal_reason,
)
from .media_ports import allocate_sip_rtp_port as _allocate_sip_rtp_port
from .phonebook_runtime import registered_roster_entries as _registered_roster_entries
from .router import CallContext, RouteAction, RouteHintSource, route_inbound_trunk, resolve_ha_router
from .sip_bridge import build_invite_client_relay
from .websocket_api import _fire_call_event, _ha_softphone_dnd, _set_ha_softphone_call_state, _set_sip_bridge_call_state

_LOGGER = logging.getLogger(__name__)
SIP_ROUTE_DECISION_TIMEOUT = 1.5


async def async_start_sip_endpoint(hass: HomeAssistant) -> bool:
    """Bind the enabled SIP signaling listeners for HA softphone and bridge calls."""
    from . import (
        _debug_mode,
        _get_transport_config,
        _get_trunk_config,
        _trunk_enabled,
        _ha_advertise_host,
        _ha_peer_name,
        _ha_softphone_has_active_call,
        _sip_send_bye,
        _sip_send_final_response,
        _sip_uri_transport,
        _sip_accounts,
        _enable_reused_sip_tcp_connection,
        _async_build_peer_snapshot,
        _pending_routes,
        _refresh_and_push_phonebook,
        _terminate_sip_bridge,
    )
    from .dtmf import DtmfCollector
    from .sdp import build_answer_directional
    from . import sdp as sip_sdp
    from .sip import parse_sip_uri
    from .sip_client import SIP_TIMER_B, SipCallClient
    from .sip_endpoint import SipEndpointManager
    from .sip_listener import SipInvite, SipInviteResult
    from .sip_registrar import SipRegistrar

    if hass.data.get(DOMAIN, {}).get("sip_endpoint") is not None:
        _LOGGER.debug("Stopping existing SIP endpoint before rebinding listeners")
        await async_stop_sip_endpoint(hass)

    cfg = _get_transport_config(hass)
    local_ip = await _ha_advertise_host(hass)
    if not local_ip:
        _LOGGER.error("Cannot start SIP endpoint: HA announce IP is unknown")
        return False
    registrar = SipRegistrar(
        enabled=bool(cfg.get(CONF_REGISTRAR_ENABLED, False)),
        accounts=_sip_accounts(hass),
        local_ip=local_ip,
        local_sip_port=int(cfg["sip_port"]),
    )
    hass.data.setdefault(DOMAIN, {})["sip_registrar"] = registrar

    async def _on_register(request, addr, transport):
        result = await registrar.handle_register(request, addr, transport)
        if 200 <= int(result.status) < 300:
            await _refresh_and_push_phonebook(hass)
        return result

    def _is_trunk_invite(invite: SipInvite) -> bool:
        trunk_cfg = _get_trunk_config(hass)
        if not _trunk_enabled(trunk_cfg):
            return False
        target_user = str(invite.request_uri.user or "").strip().lower()
        trunk_users = {
            str(trunk_cfg.get(CONF_TRUNK_USERNAME) or "").strip().lower(),
            str(trunk_cfg.get(CONF_TRUNK_AUTH_USERNAME) or "").strip().lower(),
        }
        trunk_users.discard("")
        trunk_hosts = {
            str(trunk_cfg.get(CONF_TRUNK_SERVER) or "").strip().lower(),
            str(trunk_cfg.get(CONF_TRUNK_OUTBOUND_PROXY) or "").strip().lower(),
        }
        trunk_hosts.discard("")
        return bool(
            (target_user and target_user in trunk_users)
            or str(invite.source_host or "").strip().lower() in trunk_hosts
        )

    def _is_ha_target(value: str) -> bool:
        return _same_route_name(value, _ha_peer_name(hass)) or _same_route_name(value, "ha")

    def _ha_router_decision(target: str, entries: list[RosterEntry]):
        trunk = hass.data.get(DOMAIN, {}).get("sip_trunk")
        trunk_cfg = _get_trunk_config(hass)
        trunk_ready = _trunk_enabled(trunk_cfg) and bool(getattr(trunk, "registered", False))
        return resolve_ha_router(target, entries, trunk_ready=trunk_ready)

    def _inbound_route_decision(invite: SipInvite, peers: list[Peer], entries: list[RosterEntry]):
        # Once an INVITE reached HA, HA is the router. ESP-origin direct-vs-HA
        # decisions are made before dialing by the ESP phonebook mirror.
        return _ha_router_decision(invite.target, entries)

    async def _run_trunk_inbound_route(
        invite: SipInvite,
        *,
        source_relay_port: int,
        dest_relay_port: int,
    ) -> None:
        bucket = hass.data.setdefault(DOMAIN, {})
        trunk_cfg = _get_trunk_config(hass)
        routes = parse_dtmf_route_map(trunk_cfg.get(CONF_TRUNK_DTMF_ROUTES))
        dtmf_formats = sip_sdp.offered_dtmf_formats(invite.remote_sdp)
        dtmf_format = dtmf_formats[0] if dtmf_formats else None
        destination = ""
        digits = ""
        if trunk_cfg.get(CONF_TRUNK_DTMF_ENABLED) and routes and dtmf_format is not None:
            try:
                collector = DtmfCollector(
                    host="0.0.0.0",
                    port=source_relay_port,
                    payload_type=dtmf_format.payload_type,
                    routes=routes,
                    timeout=float(trunk_cfg.get(CONF_TRUNK_DTMF_TIMEOUT_MS) or 1000) / 1000.0,
                    terminator=str(trunk_cfg.get(CONF_TRUNK_DTMF_TERMINATOR) or ""),
                )
                digits, destination = await collector.collect()
            except Exception as err:
                _LOGGER.info("SIP trunk DTMF collection unavailable: %s", err)
        elif trunk_cfg.get(CONF_TRUNK_DTMF_ENABLED) and routes:
            _LOGGER.info("SIP trunk inbound call has no telephone-event SDP offer; using default destination")

        default_target = str(trunk_cfg.get(CONF_TRUNK_INBOUND_DEFAULT_TARGET) or "HA").strip() or "HA"
        route_hint = destination or digits
        peers = await _async_build_peer_snapshot(hass)
        decision = route_inbound_trunk(
            CallContext(
                call_id=invite.call_id,
                direction="inbound",
                origin="trunk",
                caller=invite.caller,
                called_did=str(invite.request_uri.user or ""),
                requested_target=default_target,
                route_hint=route_hint,
                route_hint_source=RouteHintSource.DTMF if route_hint else RouteHintSource.NONE,
                source_host=invite.source_host,
            ),
            _roster_from_peers(hass, peers, _registered_roster_entries(hass)),
            trunk_ready=False,
        )
        if decision.action is RouteAction.ANSWER_HA:
            destination = default_target
        elif decision.action is RouteAction.REJECT:
            _LOGGER.info("SIP trunk route not found call_id=%s digits=%s hint=%s", invite.call_id, digits or "-", route_hint or "-")
            _sip_send_bye(hass, invite.call_id)
            _call_registry(hass).upsert(
                invite.call_id,
                state=CallState.TRANSPORT_UNREACHABLE.value,
                caller=invite.caller,
                callee=route_hint or default_target,
                route_kind="trunk",
                terminal_reason="route_not_found",
            )
            _set_sip_bridge_call_state(
                hass,
                CallState.TRANSPORT_UNREACHABLE.value,
                caller=invite.caller,
                callee=route_hint or default_target,
                peer_name=invite.caller,
                call_id=invite.call_id,
                reason="route_not_found",
                terminal_reason="route_not_found",
                origin="self",
                sip_status_code=404,
                last_sip_event="BYE",
            )
            return
        else:
            destination = decision.target or route_hint or default_target
        _LOGGER.info(
            "SIP trunk inbound route call_id=%s caller=%s digits=%s destination=%s tx=%s rx=%s",
            invite.call_id,
            invite.caller or invite.source_host,
            digits or "-",
            destination,
            invite.send_format.wire_token(),
            invite.recv_format.wire_token(),
        )

        if _is_ha_target(destination):
            registry = _call_registry(hass)
            registry.pending_invites[invite.call_id] = invite
            registry.preanswered[invite.call_id] = {
                "local_rtp_port": source_relay_port,
            }
            registry.upsert(
                invite.call_id,
                state=CallState.RINGING.value,
                caller=invite.caller,
                callee=_ha_peer_name(hass),
                route_kind="trunk",
            )
            registry.add_leg(invite.call_id, invite.call_id, role="ha_softphone", state=CallState.RINGING.value)
            _set_ha_softphone_call_state(
                hass,
                CallState.RINGING.value,
                session_device_id=HA_SOFTPHONE_DEVICE_ID,
                caller=invite.caller,
                callee=_ha_peer_name(hass),
                peer_name=invite.caller,
                direction="incoming",
                call_id=invite.call_id,
                selected_tx_format=invite.send_format.audio_format.wire_token(),
                selected_rx_format=invite.recv_format.audio_format.wire_token(),
                selected_tx_rtp_format=invite.send_format.wire_token(),
                selected_rx_rtp_format=invite.recv_format.wire_token(),
                audio_mode="full_duplex",
                route_kind="trunk",
                sip_status_code=200,
                last_sip_event="INVITE",
            )
            _fire_call_event(
                hass,
                {
                    "state": CallState.RINGING.value,
                    "scope": "sip_trunk",
                    "call_id": invite.call_id,
                    "caller": invite.caller,
                    "callee": _ha_peer_name(hass),
                    "dtmf_digits": digits,
                    "target": destination,
                },
                "sip",
            )
            return

        decision = _ha_router_decision(destination, _roster_from_peers(hass, peers, _registered_roster_entries(hass)))
        peer_target = _peer_for_target(decision.target or destination, peers)
        bridge_uri = None
        try:
            if peer_target is not None and peer_target.host:
                sip_transport = str((peer_target.device or {}).get("sip_transport") or "tcp").lower()
                if sip_transport not in {"tcp", "udp"}:
                    sip_transport = "tcp"
                bridge_uri = parse_sip_uri(
                    f"sip:{decision.target or destination}@{peer_target.host}:{peer_target.sip_port or cfg['sip_port']};transport={sip_transport}"
                )
            elif decision.entry is not None and decision.entry.sip_uri:
                bridge_uri = parse_sip_uri(decision.entry.sip_uri)
            elif decision.entry is not None and decision.entry.kind != "ha" and decision.entry.address:
                bridge_port = int((decision.entry.metadata or {}).get("sip_port") or cfg["sip_port"])
                bridge_uri = parse_sip_uri(f"sip:{decision.entry.id}@{decision.entry.address}:{bridge_port}")
            elif decision.sip_uri:
                bridge_uri = parse_sip_uri(decision.sip_uri)
        except Exception as err:
            _LOGGER.info("SIP trunk route parse failed destination=%s: %s", destination, err)

        if bridge_uri is None or bridge_uri.host == local_ip:
            _LOGGER.info("SIP trunk destination unresolved destination=%s route=%s", destination, decision.action.value)
            _sip_send_bye(hass, invite.call_id)
            _set_sip_bridge_call_state(
                hass,
                CallState.TRANSPORT_UNREACHABLE.value,
                caller=invite.caller,
                callee=destination,
                peer_name=invite.caller,
                call_id=invite.call_id,
                reason=TerminalReason.TRANSPORT_UNREACHABLE.value,
                terminal_reason=TerminalReason.TRANSPORT_UNREACHABLE.value,
                origin="self",
                sip_status_code=404,
                last_sip_event="BYE",
            )
            return

        peer_target = _peer_for_target(destination, peers)
        remote_tx_formats = _peer_audio_formats(peer_target, "tx_formats") or _roster_entry_formats(decision.entry, "tx_formats")
        remote_rx_formats = _peer_audio_formats(peer_target, "rx_formats") or _roster_entry_formats(decision.entry, "rx_formats")
        sip_send_formats, sip_recv_formats = _sip_target_audio_profile(
            remote_tx_formats=remote_tx_formats,
            remote_rx_formats=remote_rx_formats,
            target=destination,
        )
        client = SipCallClient(
            local_ip=local_ip,
            local_name=invite.caller or _ha_peer_name(hass),
            local_sip_port=int(cfg["sip_port"]),
            local_rtp_port=dest_relay_port,
            supported_send_formats=sip_send_formats,
            supported_recv_formats=sip_recv_formats,
            signaling_transport=_sip_uri_transport(bridge_uri),
        )
        _enable_reused_sip_tcp_connection(
            hass,
            client,
            bridge_uri,
            target=destination,
            default_sip_port=int(cfg["sip_port"]),
        )
        result = await client.invite(
            target=bridge_uri.user,
            remote_host=bridge_uri.host,
            remote_sip_port=bridge_uri.port or int(cfg["sip_port"]),
        )
        if result == "ringing":
            result = await client.wait_for_final()
        if result != "in_call" or client.dialog is None:
            _LOGGER.info("SIP trunk destination failed destination=%s result=%s", destination, result)
            await client.close()
            _sip_send_bye(hass, invite.call_id)
            public_result = _sip_public_state(result)
            terminal_reason = _sip_terminal_reason(result, public_result)
            _set_sip_bridge_call_state(
                hass,
                public_result,
                caller=invite.caller,
                callee=destination,
                peer_name=invite.caller,
                call_id=invite.call_id,
                dest_call_id=client.dialog_ids.call_id,
                reason=terminal_reason,
                terminal_reason=terminal_reason,
                origin="remote",
                sip_status_code=client.last_sip_status_code,
                last_sip_event=client.last_sip_event or "BYE",
            )
            return
        _LOGGER.info(
            "SIP trunk bridge media call_id=%s trunk_tx=%s trunk_rx=%s destination_tx=%s destination_rx=%s",
            invite.call_id,
            invite.send_format.wire_token(),
            invite.recv_format.wire_token(),
            client.dialog.send_format.wire_token(),
            client.dialog.recv_format.wire_token(),
        )

        try:
            relay = build_invite_client_relay(
                invite=invite,
                client=client,
                source_relay_port=source_relay_port,
                dest_relay_port=dest_relay_port,
                debug_capture=_debug_mode(hass),
            )
            await relay.start()
        except Exception as err:
            _LOGGER.warning("SIP trunk RTP bridge unavailable: %s", err)
            client.bye()
            await client.close()
            _sip_send_bye(hass, invite.call_id)
            _set_sip_bridge_call_state(
                hass,
                CallState.MEDIA_INCOMPATIBLE.value,
                caller=invite.caller,
                callee=destination,
                peer_name=invite.caller,
                call_id=invite.call_id,
                dest_call_id=client.dialog_ids.call_id,
                reason=TerminalReason.MEDIA_INCOMPATIBLE.value,
                terminal_reason=TerminalReason.MEDIA_INCOMPATIBLE.value,
                origin="self",
                sip_status_code=488,
                last_sip_event="BYE",
            )
            return

        registry = _call_registry(hass)
        registry.register_bridge(
            source_call_id=invite.call_id,
            dest_call_id=client.dialog_ids.call_id,
            client=client,
            state=CallState.IN_CALL.value,
            caller=invite.caller,
            callee=destination,
            route_kind="trunk",
            source_role="trunk",
        )
        _LOGGER.info(
            "SIP bridge registered call_id=%s dest_call_id=%s target=%s",
            invite.call_id,
            client.dialog_ids.call_id,
            bridge_uri.user,
        )
        registry.relays[invite.call_id] = relay
        _set_sip_bridge_call_state(
            hass,
            CallState.IN_CALL.value,
            caller=invite.caller,
            callee=destination,
            peer_name=destination,
            call_id=invite.call_id,
            dest_call_id=client.dialog_ids.call_id,
            selected_tx_format=invite.send_format.audio_format.wire_token(),
            selected_rx_format=invite.recv_format.audio_format.wire_token(),
            selected_tx_rtp_format=invite.send_format.wire_token(),
            selected_rx_rtp_format=invite.recv_format.wire_token(),
            audio_mode="full_duplex",
            route_kind="trunk",
            sip_status_code=200,
            last_sip_event="SIP_RESPONSE",
            sip_uri=str(bridge_uri),
        )
        _fire_call_event(
            hass,
            {
                "state": CallState.IN_CALL.value,
                "scope": "sip_trunk",
                "call_id": invite.call_id,
                "target": destination,
                "dtmf_digits": digits,
                "dest_call_id": client.dialog_ids.call_id,
            },
            "sip",
        )

    async def _on_invite(invite: SipInvite) -> SipInviteResult:
        peers = await _async_build_peer_snapshot(hass)
        caller_peer = _peer_for_target(invite.caller, peers)
        if caller_peer is not None:
            send_candidates, recv_candidates = _sip_target_audio_profile(
                remote_tx_formats=_peer_audio_formats(caller_peer, "tx_formats"),
                remote_rx_formats=_peer_audio_formats(caller_peer, "rx_formats"),
                target=caller_peer.name,
            )
            selected = sip_sdp.negotiate_directional(
                invite.remote_sdp,
                send_candidates,
                recv_candidates,
            )
            if selected is None:
                _LOGGER.info(
                    "SIP INVITE from %s rejected: roster directional PCM profile is incompatible",
                    invite.caller or invite.source_host,
                )
                return SipInviteResult(488, "Not Acceptable Here", to_tag="", decline_reason=TerminalReason.MEDIA_INCOMPATIBLE.value)
            invite = replace(invite, send_format=selected.send, recv_format=selected.recv)
        roster_entries = _roster_from_peers(hass, peers, _registered_roster_entries(hass))
        decision = _inbound_route_decision(invite, peers, roster_entries)
        bucket = hass.data.setdefault(DOMAIN, {})
        registry = _call_registry(hass)
        route_bucket = _pending_routes(hass)
        pending = registry.pending_invites
        active_media = len(registry.softphone_media)
        ha_softphone_active = _ha_softphone_has_active_call(hass)
        if route_bucket or pending or active_media or ha_softphone_active:
            _LOGGER.info(
                "SIP INVITE from %s rejected: HA SIP endpoint is busy "
                "(routes=%d pending=%d media=%d ha_softphone=%s)",
                invite.caller or invite.source_host,
                len(route_bucket),
                len(pending),
                active_media,
                ha_softphone_active,
            )
            _fire_call_event(
                hass,
                {
                    "state": CallState.BUSY.value,
                    "call_id": invite.call_id,
                    "caller": invite.caller,
                    "callee": invite.target,
                    "peer_name": invite.caller,
                    "direction": "incoming",
                    "terminal_reason": TerminalReason.BUSY.value,
                    "sip_status_code": 486,
                    "last_sip_event": "SIP_RESPONSE",
                },
                "sip",
            )
            return SipInviteResult(486, "Busy Here", to_tag="", decline_reason=TerminalReason.BUSY.value)
        if _is_trunk_invite(invite):
            next_port = int(bucket.get("sip_rtp_next_port", int(cfg["rtp_port"]) + 2))
            source_relay_port = next_port
            dest_relay_port = next_port + 2
            bucket["sip_rtp_next_port"] = next_port + 4
            trunk_cfg = _get_trunk_config(hass)
            dtmf_format = None
            if trunk_cfg.get(CONF_TRUNK_DTMF_ENABLED):
                dtmf_formats = sip_sdp.offered_dtmf_formats(invite.remote_sdp)
                dtmf_format = dtmf_formats[0] if dtmf_formats else None
            answer = build_answer_directional(
                local_ip,
                local_ip,
                source_relay_port,
                invite.send_format,
                invite.recv_format,
                dtmf=dtmf_format,
            )
            _set_sip_bridge_call_state(
                hass,
                CallState.CONNECTING.value,
                caller=invite.caller,
                callee=str(trunk_cfg.get(CONF_TRUNK_INBOUND_DEFAULT_TARGET) or "HA"),
                peer_name=invite.caller,
                call_id=invite.call_id,
                selected_tx_format=invite.send_format.audio_format.wire_token(),
                selected_rx_format=invite.recv_format.audio_format.wire_token(),
                selected_tx_rtp_format=invite.send_format.wire_token(),
                selected_rx_rtp_format=invite.recv_format.wire_token(),
                audio_mode="full_duplex",
                route_kind="trunk",
                sip_status_code=200,
                last_sip_event="INVITE",
            )
            hass.async_create_task(
                _run_trunk_inbound_route(
                    invite,
                    source_relay_port=source_relay_port,
                    dest_relay_port=dest_relay_port,
                )
            )
            return SipInviteResult(200, "OK", answer_sdp=answer, to_tag="")
        loop = asyncio.get_running_loop()
        future: asyncio.Future = loop.create_future()
        expires_at = time.time() + SIP_ROUTE_DECISION_TIMEOUT
        route_bucket[invite.call_id] = {
            "future": future,
            "invite": invite,
            "decision": decision,
            "created_at": time.time(),
            "expires_at": expires_at,
        }
        _set_sip_bridge_call_state(
            hass,
            "route_requested",
            caller=invite.caller,
            callee=invite.target,
            peer_name=invite.caller,
            call_id=invite.call_id,
            selected_tx_format=invite.send_format.audio_format.wire_token(),
            selected_rx_format=invite.recv_format.audio_format.wire_token(),
            selected_tx_rtp_format=invite.send_format.wire_token(),
            selected_rx_rtp_format=invite.recv_format.wire_token(),
            audio_mode="full_duplex",
            route_kind=decision.action.value,
            sip_uri=decision.sip_uri,
            sip_status_code=100,
            last_sip_event="INVITE",
        )
        _fire_call_event(
            hass,
            {
                "state": "route_requested",
                "caller": invite.caller,
                "callee": invite.target,
                "peer_name": invite.caller,
                "local_name": _ha_peer_name(hass),
                "direction": "incoming",
                "call_id": invite.call_id,
                "source_host": invite.source_host,
                "target": decision.target,
                "route_kind": decision.action.value,
                "default_destination": decision.target,
                "sip_uri": decision.sip_uri,
                "expires_at": expires_at,
                "decision_timeout_ms": int(SIP_ROUTE_DECISION_TIMEOUT * 1000),
                "selected_tx_format": invite.send_format.audio_format.wire_token(),
                "selected_rx_format": invite.recv_format.audio_format.wire_token(),
                "rtp_format": (
                    f"{invite.selected_format.encoding}/"
                    f"{invite.selected_format.sample_rate}/"
                    f"{invite.selected_format.channels}"
                ),
            },
            "sip",
        )
        _LOGGER.info(
            "SIP route requested: caller=%s target=%s route=%s uri=%s media=%s/%s",
            invite.caller or invite.source_host,
            invite.target,
            decision.action.value,
            decision.sip_uri or "-",
            invite.selected_format.encoding,
            invite.selected_format.sample_rate,
        )
        route_action = "default"
        route_destination = ""
        route_status = 0
        route_reason = ""
        route_decline_reason = ""
        try:
            route_decision = await asyncio.wait_for(future, timeout=SIP_ROUTE_DECISION_TIMEOUT)
        except asyncio.TimeoutError:
            route_decision = {}
        finally:
            route_bucket.pop(invite.call_id, None)
        if isinstance(route_decision, dict):
            route_action = str(route_decision.get("action") or "default").strip().lower()
            route_destination = str(route_decision.get("destination") or "").strip()
            route_status = int(route_decision.get("status") or 0)
            route_reason = str(route_decision.get("reason") or "").strip()
            route_decline_reason = str(route_decision.get("decline_reason") or "").strip()

        if route_action in {"decline", "busy", "cancel"}:
            if route_action == "busy":
                status = route_status or 486
                reason = route_reason or "Busy Here"
                app_reason = TerminalReason.BUSY.value
            elif route_action == "cancel":
                status = route_status or 487
                reason = route_reason or "Request Terminated"
                app_reason = TerminalReason.CANCELLED.value
            else:
                status = route_status or 603
                reason = route_reason or "Decline"
                app_reason = route_decline_reason or TerminalReason.DECLINED.value
            _set_sip_bridge_call_state(
                hass,
                CallState.BUSY.value if app_reason == TerminalReason.BUSY.value
                else CallState.CANCELLED.value if status == 487
                else "declined",
                caller=invite.caller,
                callee=invite.target,
                peer_name=invite.caller,
                call_id=invite.call_id,
                reason=app_reason,
                origin="self",
                sip_status_code=status,
                last_sip_event="SIP_RESPONSE",
            )
            return SipInviteResult(status, reason, to_tag="", decline_reason=app_reason)

        if route_action in {"forward", "bridge"} and route_destination:
            decision = _ha_router_decision(route_destination, roster_entries)
            _LOGGER.info(
                "SIP route override call_id=%s action=%s destination=%s route=%s uri=%s",
                invite.call_id,
                route_action,
                route_destination,
                decision.action.value,
                decision.sip_uri or "-",
            )

        force_ha_softphone = route_action == "answer_ha"
        trunk_cfg = _get_trunk_config(hass)
        trunk = hass.data.get(DOMAIN, {}).get("sip_trunk")
        trunk_ready = _trunk_enabled(trunk_cfg) and bool(getattr(trunk, "registered", False))
        bridge_to_trunk = bool(
            not force_ha_softphone
            and decision.action is RouteAction.TRUNK
            and trunk_ready
        )
        if not force_ha_softphone and decision.action is RouteAction.REJECT:
            if decision.reason is RouteReason.TARGET_DISABLED:
                status = 403
                sip_reason = "Forbidden"
            elif decision.reason in {
                RouteReason.TRUNK_UNAVAILABLE,
                RouteReason.NO_DIRECT_TRANSPORT,
                RouteReason.TARGET_UNREACHABLE,
            }:
                status = 480
                sip_reason = "Temporarily Unavailable"
            else:
                status = 404
                sip_reason = "Not Found"
            _set_sip_bridge_call_state(
                hass,
                CallState.TRANSPORT_UNREACHABLE.value if status == 480 else "declined",
                caller=invite.caller,
                callee=invite.target,
                peer_name=invite.caller,
                call_id=invite.call_id,
                reason=decision.reason.value if decision.reason else TerminalReason.DECLINED.value,
                origin="self",
                sip_status_code=status,
                last_sip_event="SIP_RESPONSE",
            )
            return SipInviteResult(status, sip_reason, to_tag="", decline_reason=decision.reason.value if decision.reason else TerminalReason.DECLINED.value)
        if not force_ha_softphone and decision.action is RouteAction.TRUNK and not bridge_to_trunk:
            return SipInviteResult(503, "Service Unavailable", to_tag="")
        routeable_sip_target = decision.action in {
            RouteAction.DIRECT,
            RouteAction.FORWARD,
            RouteAction.BRIDGE,
            RouteAction.GROUP,
        } and (decision.entry is not None or bool(decision.sip_uri))
        if not force_ha_softphone and (bridge_to_trunk or routeable_sip_target):
            peer_target = _peer_for_target(decision.target or invite.target, peers)
            bridge_uri = None
            if bridge_to_trunk:
                bridge_uri = parse_sip_uri(
                    f"sip:{decision.target or invite.target}@{trunk_cfg[CONF_TRUNK_SERVER]}:"
                    f"{int(trunk_cfg[CONF_TRUNK_PORT])};"
                    f"transport={str(trunk_cfg[CONF_TRUNK_TRANSPORT]).lower()}"
                )
            elif peer_target is not None and peer_target.host:
                sip_transport = str((peer_target.device or {}).get("sip_transport") or "tcp").lower()
                if sip_transport not in {"tcp", "udp"}:
                    sip_transport = "tcp"
                bridge_uri = parse_sip_uri(
                    f"sip:{decision.target or invite.target}@{peer_target.host}:{peer_target.sip_port or cfg['sip_port']};transport={sip_transport}"
                )
            elif decision.entry.sip_uri:
                bridge_uri = parse_sip_uri(decision.entry.sip_uri)
            elif decision.entry.kind != "ha" and decision.entry.address:
                bridge_port = int((decision.entry.metadata or {}).get("sip_port") or cfg["sip_port"])
                bridge_uri = parse_sip_uri(f"sip:{decision.entry.id}@{decision.entry.address}:{bridge_port}")
            decision_uri = bridge_uri or (parse_sip_uri(decision.sip_uri) if decision.sip_uri else None)
            if decision_uri is not None and decision_uri.host != local_ip:
                bucket = hass.data.setdefault(DOMAIN, {})
                next_port = int(bucket.get("sip_rtp_next_port", int(cfg["rtp_port"]) + 2))
                source_relay_port = next_port
                dest_relay_port = next_port + 2
                bucket["sip_rtp_next_port"] = next_port + 4
                peer_target = _peer_for_target(decision.target or invite.target, peers)
                remote_tx_formats = _peer_audio_formats(peer_target, "tx_formats") or _roster_entry_formats(decision.entry, "tx_formats")
                remote_rx_formats = _peer_audio_formats(peer_target, "rx_formats") or _roster_entry_formats(decision.entry, "rx_formats")
                sip_send_formats, sip_recv_formats = _sip_target_audio_profile(
                    remote_tx_formats=remote_tx_formats,
                    remote_rx_formats=remote_rx_formats,
                    target=decision.target or invite.target,
                )
                bridge_to_softphone = bool(decision.entry is not None and decision.entry.kind == "softphone")
                if bridge_to_trunk or bridge_to_softphone:
                    sip_send_formats = list(HA_TRUNK_AUDIO_FORMATS)
                    sip_recv_formats = list(HA_TRUNK_AUDIO_FORMATS)
                client = SipCallClient(
                    local_ip=local_ip,
                    local_name=(
                        str(trunk_cfg.get(CONF_TRUNK_USERNAME) or _ha_peer_name(hass))
                        if bridge_to_trunk
                        else invite.caller or _ha_peer_name(hass)
                    ),
                    local_sip_port=int(cfg["sip_port"]),
                    local_rtp_port=dest_relay_port,
                    supported_send_formats=sip_send_formats,
                    supported_recv_formats=sip_recv_formats,
                    signaling_transport=_sip_uri_transport(decision_uri),
                    auth_username=str(trunk_cfg.get(CONF_TRUNK_AUTH_USERNAME) or "") if bridge_to_trunk else "",
                    username=str(trunk_cfg.get(CONF_TRUNK_USERNAME) or "") if bridge_to_trunk else "",
                    password=str(trunk_cfg.get(CONF_TRUNK_PASSWORD) or "") if bridge_to_trunk else "",
                    outbound_proxy=str(trunk_cfg.get(CONF_TRUNK_OUTBOUND_PROXY) or "") if bridge_to_trunk else "",
                    include_common_codecs=bridge_to_trunk or bridge_to_softphone,
                )
                if not bridge_to_trunk:
                    _enable_reused_sip_tcp_connection(
                        hass,
                        client,
                        decision_uri,
                        target=decision.target or invite.target,
                        default_sip_port=int(cfg["sip_port"]),
                    )
                result = await client.invite(
                    target=decision_uri.user,
                    remote_host=decision_uri.host,
                    remote_sip_port=decision_uri.port or int(cfg["sip_port"]),
                    timeout=SIP_TIMER_B if bridge_to_trunk else 8.0,
                )
                if result not in {"ringing", "in_call"}:
                    status_code, sip_reason, terminal_reason, public_state = _sip_failure_response(result)
                    await client.close()
                    _set_sip_bridge_call_state(
                        hass,
                        public_state,
                        caller=invite.caller,
                        callee=invite.target,
                        peer_name=invite.target,
                        call_id=invite.call_id,
                        dest_call_id=client.dialog_ids.call_id,
                        reason=terminal_reason,
                        terminal_reason=terminal_reason,
                        origin="remote",
                        sip_status_code=status_code,
                        last_sip_event=client.last_sip_event or "SIP_RESPONSE",
                        route_kind=decision.action.value,
                        sip_uri=str(decision_uri),
                    )
                    return SipInviteResult(
                        status_code,
                        sip_reason,
                        to_tag="",
                        decline_reason=terminal_reason,
                    )
                registry.register_bridge(
                    source_call_id=invite.call_id,
                    dest_call_id=client.dialog_ids.call_id,
                    client=client,
                    state=CallState.CONNECTING.value,
                    caller=invite.caller,
                    callee=invite.target,
                    route_kind=decision.action.value,
                    source_state=CallState.CONNECTING.value,
                    dest_state=result,
                )
                _LOGGER.info(
                    "SIP bridge registered call_id=%s dest_call_id=%s target=%s",
                    invite.call_id,
                    client.dialog_ids.call_id,
                    decision_uri.user,
                )

                async def _finish_bridge(initial_result: str) -> None:
                    final = initial_result
                    if final == "ringing":
                        final = await client.wait_for_final()
                    if final != "in_call" or client.dialog is None:
                        status_code, sip_reason, terminal_reason, public_state = _sip_failure_response(final)
                        _sip_send_final_response(
                            hass,
                            invite.call_id,
                            status_code,
                            sip_reason,
                            decline_reason=terminal_reason,
                        )
                        registry.discard_bridge_session(
                            invite.call_id,
                            client.dialog_ids.call_id,
                            reason=terminal_reason,
                            state=public_state,
                        )
                        registry.client_watchers.pop(client.dialog_ids.call_id, None)
                        await client.close()
                        _set_sip_bridge_call_state(
                            hass,
                            public_state,
                            caller=invite.caller,
                            callee=invite.target,
                            peer_name=invite.target,
                            call_id=invite.call_id,
                            dest_call_id=client.dialog_ids.call_id,
                            reason=terminal_reason,
                            terminal_reason=terminal_reason,
                            origin="remote",
                            sip_status_code=status_code,
                            last_sip_event="SIP_RESPONSE",
                            route_kind=decision.action.value,
                            sip_uri=str(decision_uri),
                        )
                        return
                    try:
                        relay = build_invite_client_relay(
                            invite=invite,
                            client=client,
                            source_relay_port=source_relay_port,
                            dest_relay_port=dest_relay_port,
                            debug_capture=_debug_mode(hass),
                        )
                        await relay.start()
                    except Exception as err:
                        _LOGGER.warning("SIP RTP bridge media conversion unavailable: %s", err)
                        _sip_send_final_response(
                            hass,
                            invite.call_id,
                            488,
                            "Not Acceptable Here",
                            decline_reason=TerminalReason.MEDIA_INCOMPATIBLE.value,
                        )
                        registry.discard_bridge_session(
                            invite.call_id,
                            client.dialog_ids.call_id,
                            reason=TerminalReason.MEDIA_INCOMPATIBLE.value,
                            state=CallState.MEDIA_INCOMPATIBLE.value,
                        )
                        registry.client_watchers.pop(client.dialog_ids.call_id, None)
                        await client.close()
                        return
                    registry.relays[invite.call_id] = relay
                    registry.upsert(
                        invite.call_id,
                        state=CallState.IN_CALL.value,
                        caller=invite.caller,
                        callee=invite.target,
                        route_kind=decision.action.value,
                    )
                    answer = build_answer_directional(
                        local_ip,
                        local_ip,
                        source_relay_port,
                        invite.send_format,
                        invite.recv_format,
                    )
                    _sip_send_final_response(hass, invite.call_id, 200, "OK", answer_sdp=answer)
                    _set_sip_bridge_call_state(
                        hass,
                        CallState.IN_CALL.value,
                        caller=invite.caller,
                        callee=invite.target,
                        peer_name=invite.target,
                        call_id=invite.call_id,
                        dest_call_id=client.dialog_ids.call_id,
                        selected_tx_format=invite.send_format.audio_format.wire_token(),
                        selected_rx_format=invite.recv_format.audio_format.wire_token(),
                        selected_tx_rtp_format=invite.send_format.wire_token(),
                        selected_rx_rtp_format=invite.recv_format.wire_token(),
                        sip_status_code=200,
                        last_sip_event="SIP_RESPONSE",
                        route_kind=decision.action.value,
                        sip_uri=str(decision_uri),
                    )
                    _fire_call_event(
                        hass,
                        {
                            "state": CallState.IN_CALL.value,
                            "scope": "sip_bridge",
                            "call_id": invite.call_id,
                            "target": invite.target,
                            "dest_call_id": client.dialog_ids.call_id,
                        },
                        "sip",
                    )
                    try:
                        terminal = await client.wait_for_dialog_termination()
                    except asyncio.CancelledError:
                        raise
                    except Exception as err:  # noqa: BLE001 - detached bridge watcher.
                        _LOGGER.warning(
                            "SIP bridge destination watcher failed call_id=%s dest_call_id=%s: %s",
                            invite.call_id,
                            client.dialog_ids.call_id,
                            err,
                        )
                        terminal = "error"
                    terminal_reason = (
                        TerminalReason.REMOTE_HANGUP.value
                        if terminal == "remote_hangup"
                        else _sip_terminal_reason(terminal, _sip_public_state(terminal))
                    )
                    bridge_handled, source_call_id, dest_call_id, _client_closed, source_bye = await _terminate_sip_bridge(
                        hass,
                        client.dialog_ids.call_id,
                        terminal_reason=terminal_reason,
                    )
                    if bridge_handled:
                        _set_sip_bridge_call_state(
                            hass,
                            _sip_public_state(terminal),
                            caller=invite.caller,
                            callee=invite.target,
                            peer_name=invite.target,
                            call_id=source_call_id or invite.call_id,
                            dest_call_id=dest_call_id,
                            reason=terminal_reason,
                            terminal_reason=terminal_reason,
                            origin="remote",
                            sip_status_code=client.last_sip_status_code,
                            last_sip_event=client.last_sip_event or "BYE",
                            route_kind=decision.action.value,
                            sip_uri=str(decision_uri),
                        )
                        _LOGGER.info(
                            "SIP bridge destination ended call_id=%s dest_call_id=%s reason=%s source_bye=%s",
                            source_call_id,
                            dest_call_id,
                            terminal_reason,
                            source_bye,
                        )

                finish_task = hass.async_create_task(_finish_bridge(result))
                registry.client_watchers[client.dialog_ids.call_id] = finish_task
                return SipInviteResult(180, "Ringing", to_tag="", defer_final=True)
        if not force_ha_softphone and decision.action is RouteAction.ANSWER_HA:
            ha_softphone_active = _ha_softphone_has_active_call(hass, ignore_call_id=invite.call_id)
            active_media = len(registry.softphone_media)
            if pending or active_media or ha_softphone_active:
                _LOGGER.info(
                    "SIP INVITE from %s rejected: HA softphone is busy "
                    "(pending=%d media=%d ha_softphone=%s)",
                    invite.caller or invite.source_host,
                    len(pending),
                    active_media,
                    ha_softphone_active,
                )
                _fire_call_event(
                    hass,
                    {
                        "state": CallState.BUSY.value,
                        "call_id": invite.call_id,
                        "caller": invite.caller,
                        "callee": invite.target,
                        "peer_name": invite.caller,
                        "direction": "incoming",
                        "terminal_reason": TerminalReason.BUSY.value,
                        "sip_status_code": 486,
                        "last_sip_event": "SIP_RESPONSE",
                    },
                    "sip",
                )
                return SipInviteResult(486, "Busy Here", to_tag="", decline_reason="busy")
            if _ha_softphone_dnd(hass):
                _LOGGER.info(
                    "SIP INVITE from %s rejected: HA softphone DND is enabled",
                    invite.caller or invite.source_host,
                )
                _set_ha_softphone_call_state(
                    hass,
                    "declined",
                    session_device_id=HA_SOFTPHONE_DEVICE_ID,
                    caller=invite.caller,
                    callee=invite.target,
                    peer_name=invite.caller,
                    direction="incoming",
                    call_id=invite.call_id,
                    reason="busy",
                    origin="self",
                    sip_status_code=486,
                    last_sip_event="SIP_RESPONSE",
                )
                return SipInviteResult(486, "Busy Here", to_tag="", decline_reason="busy")
            pending[invite.call_id] = invite
            registry.upsert(
                invite.call_id,
                state=CallState.RINGING.value,
                caller=invite.caller,
                callee=invite.target,
                route_kind=decision.action.value,
            )
            registry.add_leg(invite.call_id, invite.call_id, role="ha_softphone", state=CallState.RINGING.value)
            _set_ha_softphone_call_state(
                hass,
                "ringing",
                session_device_id=HA_SOFTPHONE_DEVICE_ID,
                caller=invite.caller,
                callee=invite.target,
                peer_name=invite.caller,
                direction="incoming",
                call_id=invite.call_id,
                selected_tx_format=invite.send_format.audio_format.wire_token(),
                selected_rx_format=invite.recv_format.audio_format.wire_token(),
                selected_tx_rtp_format=invite.send_format.wire_token(),
                selected_rx_rtp_format=invite.recv_format.wire_token(),
                audio_mode="full_duplex",
                route_kind=decision.action.value,
                sip_uri=decision.sip_uri,
                sip_status_code=180,
                last_sip_event="INVITE",
            )
            return SipInviteResult(180, "Ringing", to_tag="", defer_final=True)
        local_rtp_port = _allocate_sip_rtp_port(hass)
        answer = build_answer_directional(
            local_ip,
            local_ip,
            local_rtp_port,
            invite.send_format,
            invite.recv_format,
        )
        registry.softphone_media[invite.call_id] = {
            "invite": invite,
            "local_rtp_port": local_rtp_port,
        }
        registry.upsert(
            invite.call_id,
            state=CallState.IN_CALL.value,
            caller=invite.caller,
            callee=invite.target,
            route_kind=decision.action.value,
        )
        registry.add_leg(invite.call_id, invite.call_id, role="ha_softphone", state=CallState.IN_CALL.value)
        _set_ha_softphone_call_state(
            hass,
            CallState.IN_CALL.value,
            session_device_id=HA_SOFTPHONE_DEVICE_ID,
            caller=invite.caller,
            callee=invite.target,
            peer_name=invite.caller,
            direction="incoming",
            call_id=invite.call_id,
            selected_tx_format=invite.send_format.audio_format.wire_token(),
            selected_rx_format=invite.recv_format.audio_format.wire_token(),
            selected_tx_rtp_format=invite.send_format.wire_token(),
            selected_rx_rtp_format=invite.recv_format.wire_token(),
            audio_mode="full_duplex",
            route_kind=decision.action.value,
            sip_uri=decision.sip_uri,
            sip_status_code=200,
            last_sip_event="SIP_RESPONSE",
        )
        return SipInviteResult(200, "OK", answer_sdp=answer, to_tag="")

    async def _on_terminated(call_id: str, reason: str = "remote_hangup") -> None:
        bucket = hass.data.setdefault(DOMAIN, {})
        registry = _call_registry(hass)
        route = _pending_routes(hass).pop(call_id, None)
        if route is not None:
            future = route.get("future")
            if future is not None and not future.done():
                future.set_result(
                    {
                        "action": "cancel",
                        "reason": "Request Terminated",
                        "decline_reason": reason or TerminalReason.CANCELLED.value,
                    }
                )
        pending = registry.pending_invites
        invite = pending.pop(call_id, None)
        registry.preanswered.pop(call_id, None)
        active_media_invite = registry.softphone_media.pop(call_id, {}).get("invite")
        if invite is None:
            invite = active_media_invite
        source_call_id, dest_call_id, relay, client, watcher, _called_by_dest = registry.detach_bridge(call_id)
        if source_call_id:
            call_id = source_call_id
        softphone_store = bucket.get("ha_softphone", {})
        softphone_call_id = str(softphone_store.get("call_id") or "")
        terminal_reason = reason or "remote_hangup"
        terminal_state = (
            CallState.CANCELLED.value
            if terminal_reason == TerminalReason.CANCELLED.value
            else CallState.IDLE.value
        )
        if (
            relay is None
            and client is None
            and (invite is not None
            or (call_id and softphone_call_id == call_id)
            )
        ):
            _set_ha_softphone_call_state(
                hass,
                terminal_state,
                session_device_id=HA_SOFTPHONE_DEVICE_ID,
                caller=(invite.caller if invite is not None else ""),
                callee=(invite.target if invite is not None else _ha_peer_name(hass)),
                peer_name=(invite.caller if invite is not None else ""),
                direction="incoming",
                call_id=call_id,
                reason=terminal_reason,
                origin="remote",
            )
            registry.finish_and_pop(call_id, reason=terminal_reason, state=terminal_state)
        if relay is not None:
            await relay.stop()
        if watcher is not None:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher
        if client is not None:
            await client.terminate()
            await client.close()
        if relay is not None or client is not None:
            _set_sip_bridge_call_state(
                hass,
                CallState.IDLE.value,
                call_id=call_id,
                dest_call_id=dest_call_id,
                caller=(invite.caller if invite is not None else ""),
                callee=(invite.target if invite is not None else ""),
                peer_name=(invite.caller if invite is not None else ""),
                reason=terminal_reason,
                terminal_reason=terminal_reason,
                origin="remote",
                last_sip_event="BYE",
            )
            _fire_call_event(
                hass,
                {
                    "state": CallState.IDLE.value,
                    "scope": "sip_bridge",
                    "call_id": call_id,
                    "dest_call_id": dest_call_id,
                    "reason": terminal_reason,
                    "terminal_reason": terminal_reason,
                },
                "sip",
            )
            _LOGGER.info(
                "SIP bridge terminated call_id=%s reason=%s relay=%s dest_client=%s",
                call_id,
                terminal_reason,
                relay is not None,
                bool(dest_call_id),
            )
            registry.finish_and_pop(call_id, reason=terminal_reason)

    supported_formats = list(HA_SIP_PCM_FORMATS)
    endpoint = SipEndpointManager(
        host="0.0.0.0",
        port=int(cfg["sip_port"]),
        local_ip=local_ip,
        local_rtp_port=int(cfg["rtp_port"]),
        supported_formats=supported_formats,
        supported_send_formats=list(HA_SIP_PCM_TX_FORMATS),
        supported_recv_formats=list(HA_SIP_PCM_RX_FORMATS),
        on_invite=_on_invite,
        on_terminated=_on_terminated,
        on_register=_on_register,
        udp_enabled=True,
        tcp_enabled=True,
    )
    if not await endpoint.start():
        return False
    hass.data[DOMAIN]["sip_endpoint"] = endpoint
    hass.data[DOMAIN]["sip_server"] = endpoint.udp_server
    hass.data[DOMAIN]["sip_tcp_server"] = endpoint.tcp_server
    _LOGGER.info("SIP endpoint enabled on UDP+TCP/%s (RTP base %s)", cfg["sip_port"], cfg["rtp_port"])
    return True
