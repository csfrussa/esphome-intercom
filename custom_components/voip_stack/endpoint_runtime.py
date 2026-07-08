"""Runtime SIP endpoint/B2BUA orchestration for VoIP Stack."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, replace
import logging
import time

from homeassistant.core import HomeAssistant

from .audio_format import HA_SIP_PCM_FORMATS, HA_SIP_PCM_RX_FORMATS, HA_SIP_PCM_TX_FORMATS, HA_TRUNK_AUDIO_FORMATS
from .const import (
    CONF_REGISTRAR_ENABLED,
    CONF_TRUNK_AUTH_USERNAME,
    CONF_TRUNK_DTMF_ENABLED,
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
from .fsm import (
    CallState,
    TerminalReason,
    sip_failure_response as _sip_failure_response,
    sip_public_state as _sip_public_state,
    sip_terminal_reason as _sip_terminal_reason,
)
from .media_ports import (
    RtpPortReservation,
    allocate_sip_rtp_port as _allocate_sip_rtp_port,
    release_media_reservation as _release_media_reservation,
    release_sip_rtp_port_pair as _release_sip_rtp_port_pair,
)
from .phonebook_runtime import registered_roster_entries as _registered_roster_entries
from .router import CallContext, RouteAction, RouteHintSource, RouteReason, route_inbound_trunk, resolve_ha_router
from .session_cleanup import async_cleanup_sip_runtime
from .sip_bridge import build_invite_client_relay
from .websocket_api import _fire_call_event, _ha_softphone_dnd, _set_ha_softphone_call_state, _set_sip_bridge_call_state

_LOGGER = logging.getLogger(__name__)
SIP_ROUTE_DECISION_TIMEOUT = 1.5
RING_GROUP_TIMEOUT_S = 30.0


def _dtmf_extension_routes(entries) -> dict[str, str]:
    return {
        str(entry.extension).strip(): str(entry.extension).strip()
        for entry in entries
        if str(getattr(entry, "extension", "") or "").strip()
    }


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
    from .conference import conference_manager
    from .groups import GROUP_TYPE_CONFERENCE, GROUP_TYPE_RING

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

    def _roster_entry_for_target(target: str, entries: list[RosterEntry]):
        for entry in entries:
            if _same_route_name(entry.id, target) or _same_route_name(entry.name, target):
                return entry
            if entry.extension and str(entry.extension).strip() == str(target).strip():
                return entry
        return None

    def _sip_uri_for_member(member: str, peers: list[Peer], entries: list[RosterEntry]):
        peer = _peer_for_target(member, peers)
        if peer is not None and peer.host:
            sip_transport = str((peer.device or {}).get("sip_transport") or "tcp").lower()
            if sip_transport not in {"tcp", "udp"}:
                sip_transport = "tcp"
            return (
                parse_sip_uri(f"sip:{member}@{peer.host}:{peer.sip_port or cfg['sip_port']};transport={sip_transport}"),
                peer,
                None,
            )
        entry = _roster_entry_for_target(member, entries)
        if entry is None:
            return None, None, None
        if entry.sip_uri:
            return parse_sip_uri(entry.sip_uri), None, entry
        if not entry.metadata.get("local_ha") and entry.address:
            bridge_port = int(entry.port or (entry.metadata or {}).get("port") or (entry.metadata or {}).get("sip_port") or cfg["sip_port"])
            return parse_sip_uri(f"sip:{entry.id}@{entry.address}:{bridge_port}"), None, entry
        return None, None, entry

    @dataclass(slots=True)
    class OutboundLeg:
        member: str
        uri: object
        client: SipCallClient
        ports: RtpPortReservation
        bridge_to_softphone: bool = False

    def _prepare_outbound_leg(
        *,
        member: str,
        peers: list[Peer],
        roster_entries: list[RosterEntry],
        local_name: str,
        local_rtp_port_index: int,
    ) -> OutboundLeg | None:
        uri, peer_target, member_entry = _sip_uri_for_member(member, peers, roster_entries)
        if uri is None or uri.host == local_ip:
            return None
        ports = RtpPortReservation.allocate(hass)
        try:
            remote_tx_formats = _peer_audio_formats(peer_target, "tx_formats") or _roster_entry_formats(member_entry, "tx_formats")
            remote_rx_formats = _peer_audio_formats(peer_target, "rx_formats") or _roster_entry_formats(member_entry, "rx_formats")
            sip_send_formats, sip_recv_formats = _sip_target_audio_profile(
                remote_tx_formats=remote_tx_formats,
                remote_rx_formats=remote_rx_formats,
                target=member,
            )
            bridge_to_softphone = bool(member_entry is not None and member_entry.sip_uri and member_entry.metadata.get("registered"))
            if bridge_to_softphone:
                sip_send_formats = list(HA_TRUNK_AUDIO_FORMATS)
                sip_recv_formats = list(HA_TRUNK_AUDIO_FORMATS)
            client = SipCallClient(
                local_ip=local_ip,
                local_name=local_name,
                local_sip_port=int(cfg["sip_port"]),
                local_rtp_port=ports.ports[local_rtp_port_index],
                supported_send_formats=sip_send_formats,
                supported_recv_formats=sip_recv_formats,
                signaling_transport=_sip_uri_transport(uri),
                include_common_codecs=bridge_to_softphone,
            )
            _enable_reused_sip_tcp_connection(
                hass,
                client,
                uri,
                target=member,
                default_sip_port=int(cfg["sip_port"]),
            )
            return OutboundLeg(member=member, uri=uri, client=client, ports=ports, bridge_to_softphone=bridge_to_softphone)
        except Exception:
            ports.release()
            raise

    async def _close_client_and_release(client: SipCallClient, ports: RtpPortReservation, *, bye: bool = False) -> None:
        try:
            if bye:
                client.bye()
            await client.close()
        finally:
            ports.release()

    async def _close_outbound_leg(attempt: OutboundLeg, *, cancel: bool = False, bye_or_cancel: bool = False) -> None:
        try:
            if cancel:
                with contextlib.suppress(Exception):
                    attempt.client.cancel()
            elif bye_or_cancel:
                with contextlib.suppress(Exception):
                    attempt.client.bye_or_cancel()
            await attempt.client.close()
        finally:
            attempt.ports.release()

    def _caller_matches_member(caller: str, source_host: str, member: str, peers: list[Peer]) -> bool:
        if _same_route_name(member, caller):
            return True
        peer = _peer_for_target(member, peers)
        return bool(peer is not None and peer.host and peer.host == source_host)

    def _defer_invite_to_ha_softphone(
        invite: SipInvite,
        *,
        route_kind: str,
        callee: str | None = None,
        sip_uri: str | None = None,
    ) -> None:
        registry = _call_registry(hass)
        registry.pending_invites[invite.call_id] = invite
        registry.upsert(
            invite.call_id,
            state=CallState.RINGING.value,
            caller=invite.caller,
            callee=callee or invite.target,
            route_kind=route_kind,
        )
        registry.add_leg(invite.call_id, invite.call_id, role="ha_softphone", state=CallState.RINGING.value)
        hass.loop.call_soon(
            lambda: _set_ha_softphone_call_state(
                hass,
                CallState.RINGING.value,
                session_device_id=HA_SOFTPHONE_DEVICE_ID,
                caller=invite.caller,
                callee=callee or invite.target,
                peer_name=invite.caller,
                direction="incoming",
                call_id=invite.call_id,
                selected_tx_format=invite.send_format.audio_format.wire_token(),
                selected_rx_format=invite.recv_format.audio_format.wire_token(),
                selected_tx_rtp_format=invite.send_format.wire_token(),
                selected_rx_rtp_format=invite.recv_format.wire_token(),
                audio_mode="full_duplex",
                route_kind=route_kind,
                sip_uri=sip_uri,
                sip_status_code=180,
                last_sip_event="INVITE",
            )
        )

    def _inbound_route_decision(invite: SipInvite, peers: list[Peer], entries: list[RosterEntry]):
        # Once an INVITE reached HA, HA is the router. ESP-origin direct-vs-HA
        # decisions are made before dialing by the ESP phonebook mirror.
        return _ha_router_decision(invite.target, entries)

    async def _run_trunk_inbound_route(
        invite: SipInvite,
        *,
        bridge_ports: RtpPortReservation,
    ) -> None:
        source_relay_port, dest_relay_port = bridge_ports.ports
        bucket = hass.data.setdefault(DOMAIN, {})
        bucket.setdefault("trunk_closed_calls", set()).discard(invite.call_id)
        trunk_cfg = _get_trunk_config(hass)
        dtmf_timeout_ms = max(0, int(trunk_cfg.get(CONF_TRUNK_DTMF_TIMEOUT_MS) or 0))
        dtmf_formats = sip_sdp.offered_dtmf_formats(invite.remote_sdp)
        dtmf_format = dtmf_formats[0] if dtmf_formats else None
        destination = ""
        digits = ""
        peers = await _async_build_peer_snapshot(hass)
        roster_entries = _roster_from_peers(hass, peers, _registered_roster_entries(hass))
        routes = _dtmf_extension_routes(roster_entries)
        if trunk_cfg.get(CONF_TRUNK_DTMF_ENABLED) and dtmf_timeout_ms > 0 and dtmf_format is not None:
            try:
                collector = DtmfCollector(
                    host="0.0.0.0",
                    port=source_relay_port,
                    payload_type=dtmf_format.payload_type,
                    routes=routes,
                    timeout=float(dtmf_timeout_ms) / 1000.0,
                    terminator=str(trunk_cfg.get(CONF_TRUNK_DTMF_TERMINATOR) or ""),
                )
                digits, destination = await collector.collect()
            except Exception as err:
                _LOGGER.info("SIP trunk DTMF collection unavailable: %s", err)
        elif trunk_cfg.get(CONF_TRUNK_DTMF_ENABLED) and dtmf_timeout_ms > 0:
            timeout = float(dtmf_timeout_ms) / 1000.0
            _LOGGER.info(
                "SIP trunk inbound call has no telephone-event SDP offer; ringing default destination after %.1fs",
                timeout,
            )
            await asyncio.sleep(timeout)
            if invite.call_id in bucket.get("trunk_closed_calls", set()):
                bucket["trunk_closed_calls"].discard(invite.call_id)
                _LOGGER.info("SIP trunk inbound call_id=%s closed before default routing", invite.call_id)
                bridge_ports.release()
                return

        default_target = str(trunk_cfg.get(CONF_TRUNK_INBOUND_DEFAULT_TARGET) or "HA").strip() or "HA"
        route_hint = destination or digits
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
            roster_entries,
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
            bridge_ports.release()
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
                "rtp_reservation": bridge_ports,
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
            elif decision.entry is not None and not decision.entry.metadata.get("local_ha") and decision.entry.address:
                bridge_port = int(decision.entry.port or (decision.entry.metadata or {}).get("port") or (decision.entry.metadata or {}).get("sip_port") or cfg["sip_port"])
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
            bridge_ports.release()
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
            request_uri=str(bridge_uri),
        )
        if result == "ringing":
            result = await client.wait_for_final()
        if result != "in_call" or client.dialog is None:
            _LOGGER.info("SIP trunk destination failed destination=%s result=%s", destination, result)
            await _close_client_and_release(client, bridge_ports)
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
                on_release=lambda ports: _release_sip_rtp_port_pair(hass, ports),
            )
            await relay.start()
        except Exception as err:
            _LOGGER.warning("SIP trunk RTP bridge unavailable: %s", err)
            await _close_client_and_release(client, bridge_ports, bye=True)
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
        bridge_ports.detach()
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

    async def _run_ring_group_call(
        invite: SipInvite,
        entry: RosterEntry,
        peers: list[Peer],
        roster_entries: list[RosterEntry],
    ) -> None:
        registry = _call_registry(hass)
        members = [str(member).strip() for member in (entry.metadata.get("members") or []) if str(member).strip()]
        attempts: list[OutboundLeg] = []
        ha_member = False
        for member in members:
            if _caller_matches_member(invite.caller, invite.source_host, member, peers):
                continue
            if _is_ha_target(member):
                ha_member = True
                continue
            try:
                leg = _prepare_outbound_leg(
                    member=member,
                    peers=peers,
                    roster_entries=roster_entries,
                    local_name=invite.caller or _ha_peer_name(hass),
                    local_rtp_port_index=1,
                )
            except RuntimeError as err:
                _LOGGER.warning("SIP ring group RTP port allocation failed member=%s: %s", member, err)
                break
            if leg is not None:
                attempts.append(leg)
        route_future: asyncio.Future = asyncio.get_running_loop().create_future()
        _pending_routes(hass)[invite.call_id] = {"invite": invite, "future": route_future}
        if ha_member:
            registry.add_leg(invite.call_id, invite.call_id, role="ha_softphone", state=CallState.RINGING.value)
            _set_ha_softphone_call_state(
                hass,
                CallState.RINGING.value,
                session_device_id=HA_SOFTPHONE_DEVICE_ID,
                caller=invite.caller,
                callee=entry.display_name,
                peer_name=invite.caller,
                direction="incoming",
                call_id=invite.call_id,
                selected_tx_format=invite.send_format.audio_format.wire_token(),
                selected_rx_format=invite.recv_format.audio_format.wire_token(),
                selected_tx_rtp_format=invite.send_format.wire_token(),
                selected_rx_rtp_format=invite.recv_format.wire_token(),
                audio_mode="full_duplex",
                route_kind=GROUP_TYPE_RING,
                sip_status_code=180,
                last_sip_event="INVITE",
            )
        if not attempts and not ha_member:
            _pending_routes(hass).pop(invite.call_id, None)
            _sip_send_final_response(
                hass,
                invite.call_id,
                480,
                "Temporarily Unavailable",
                decline_reason=TerminalReason.TRANSPORT_UNREACHABLE.value,
            )
            return

        async def _dial(attempt: OutboundLeg) -> tuple[str, OutboundLeg]:
            client = attempt.client
            uri = attempt.uri
            result = await client.invite(
                target=uri.user or attempt.member,
                remote_host=uri.host,
                remote_sip_port=uri.port or int(cfg["sip_port"]),
                request_uri=str(uri),
                timeout=8.0,
            )
            if result == "ringing":
                result = await client.wait_for_final(timeout=RING_GROUP_TIMEOUT_S)
            return result, attempt

        async def _wait_ha() -> tuple[str, dict]:
            try:
                decision = await asyncio.wait_for(route_future, timeout=RING_GROUP_TIMEOUT_S)
            except asyncio.TimeoutError:
                return "timeout", {"member": _ha_peer_name(hass), "ha": True}
            action = str((decision or {}).get("action") or "").strip().lower()
            if action in {"answer_ha", "default"}:
                return "in_call_ha", {"member": _ha_peer_name(hass), "ha": True}
            if action == "busy":
                return "busy", {"member": _ha_peer_name(hass), "ha": True}
            if action == "cancel":
                return "cancelled", {"member": _ha_peer_name(hass), "ha": True}
            return "declined", {"member": _ha_peer_name(hass), "ha": True}

        async def _wait_caller_cancel() -> tuple[str, dict]:
            try:
                decision = await asyncio.wait_for(route_future, timeout=RING_GROUP_TIMEOUT_S)
            except asyncio.TimeoutError:
                return "timeout", {"member": "__caller__", "caller_control": True}
            action = str((decision or {}).get("action") or "").strip().lower()
            return ("cancelled" if action == "cancel" else "ignored", {"member": "__caller__", "caller_control": True})

        tasks = [hass.async_create_task(_dial(attempt)) for attempt in attempts]
        if ha_member:
            tasks.append(hass.async_create_task(_wait_ha()))
        else:
            tasks.append(hass.async_create_task(_wait_caller_cancel()))
        winner: OutboundLeg | dict | None = None
        ha_winner = False
        final_result = "timeout"
        try:
            deadline = asyncio.get_running_loop().time() + RING_GROUP_TIMEOUT_S
            pending_tasks = set(tasks)
            while pending_tasks and winner is None:
                timeout = max(0.0, deadline - asyncio.get_running_loop().time())
                if timeout <= 0:
                    break
                done, pending_tasks = await asyncio.wait(
                    pending_tasks,
                    timeout=timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    break
                for task in done:
                    try:
                        result, attempt = task.result()
                    except Exception as err:
                        _LOGGER.debug("SIP ring group member task failed: %s", err)
                        continue
                    final_result = result
                    if isinstance(attempt, OutboundLeg) and result == "in_call" and attempt.client.dialog is not None:
                        winner = attempt
                        break
                    if result == "in_call_ha":
                        winner = attempt
                        ha_winner = True
                        break
                    if result == "cancelled" and (attempt.get("caller_control") or attempt.get("ha")):
                        pending_tasks.clear()
                        break
            for task in tasks:
                if not task.done():
                    task.cancel()
            for attempt in attempts:
                if attempt is winner:
                    continue
                await _close_outbound_leg(attempt, cancel=True)
            if ha_member and not ha_winner:
                _pending_routes(hass).pop(invite.call_id, None)
                _set_ha_softphone_call_state(
                    hass,
                    CallState.CANCELLED.value if winner is not None else CallState.IDLE.value,
                    session_device_id=HA_SOFTPHONE_DEVICE_ID,
                    caller=invite.caller,
                    callee=entry.display_name,
                    peer_name=invite.caller,
                    direction="incoming",
                    call_id=invite.call_id,
                    reason=TerminalReason.CANCELLED.value if winner is not None else TerminalReason.TIMEOUT.value,
                    terminal_reason=TerminalReason.CANCELLED.value if winner is not None else TerminalReason.TIMEOUT.value,
                    route_kind=GROUP_TYPE_RING,
                    last_sip_event="SIP_RESPONSE",
                )
            if winner is None:
                _pending_routes(hass).pop(invite.call_id, None)
                status_code, sip_reason, terminal_reason, public_state = _sip_failure_response(final_result)
                _sip_send_final_response(hass, invite.call_id, status_code, sip_reason, decline_reason=terminal_reason)
                _set_sip_bridge_call_state(
                    hass,
                    public_state,
                    caller=invite.caller,
                    callee=invite.target,
                    peer_name=invite.target,
                    call_id=invite.call_id,
                    reason=terminal_reason,
                    terminal_reason=terminal_reason,
                    origin="remote",
                    sip_status_code=status_code,
                    last_sip_event="SIP_RESPONSE",
                    route_kind=GROUP_TYPE_RING,
                )
                return
            if ha_winner:
                _pending_routes(hass).pop(invite.call_id, None)
                local_rtp_port = _allocate_sip_rtp_port(hass)
                answer = build_answer_directional(
                    local_ip,
                    local_ip,
                    local_rtp_port,
                    invite.send_format,
                    invite.recv_format,
                )
                registry.pending_invites.pop(invite.call_id, None)
                registry.softphone_media[invite.call_id] = {
                    "invite": invite,
                    "local_rtp_port": local_rtp_port,
                }
                registry.upsert(
                    invite.call_id,
                    state=CallState.IN_CALL.value,
                    caller=invite.caller,
                    callee=entry.display_name,
                    route_kind=GROUP_TYPE_RING,
                )
                registry.add_leg(invite.call_id, invite.call_id, role="ha_softphone", state=CallState.IN_CALL.value)
                _sip_send_final_response(hass, invite.call_id, 200, "OK", answer_sdp=answer)
                _set_ha_softphone_call_state(
                    hass,
                    CallState.IN_CALL.value,
                    session_device_id=HA_SOFTPHONE_DEVICE_ID,
                    caller=invite.caller,
                    callee=entry.display_name,
                    peer_name=invite.caller,
                    direction="incoming",
                    call_id=invite.call_id,
                    selected_tx_format=invite.send_format.audio_format.wire_token(),
                    selected_rx_format=invite.recv_format.audio_format.wire_token(),
                    selected_tx_rtp_format=invite.send_format.wire_token(),
                    selected_rx_rtp_format=invite.recv_format.wire_token(),
                    audio_mode="full_duplex",
                    route_kind=GROUP_TYPE_RING,
                    sip_status_code=200,
                    last_sip_event="SIP_RESPONSE",
                )
                return
            _pending_routes(hass).pop(invite.call_id, None)
            assert isinstance(winner, OutboundLeg)
            client = winner.client
            source_relay_port, dest_relay_port = winner.ports.ports
            registry.register_bridge(
                source_call_id=invite.call_id,
                dest_call_id=client.dialog_ids.call_id,
                client=client,
                state=CallState.CONNECTING.value,
                caller=invite.caller,
                callee=invite.target,
                route_kind=GROUP_TYPE_RING,
                source_state=CallState.CONNECTING.value,
                dest_state=CallState.IN_CALL.value,
            )
            try:
                relay = build_invite_client_relay(
                    invite=invite,
                    client=client,
                    source_relay_port=source_relay_port,
                    dest_relay_port=dest_relay_port,
                    debug_capture=_debug_mode(hass),
                    on_release=lambda ports: _release_sip_rtp_port_pair(hass, ports),
                )
                await relay.start()
            except Exception as err:
                _LOGGER.warning("SIP ring group media bridge unavailable: %s", err)
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
                await _close_outbound_leg(winner)
                return
            winner.ports.detach()
            registry.relays[invite.call_id] = relay
            dialed_target = entry.display_name or invite.target
            connected_party = str(winner.member or "").strip() or invite.target
            registry.upsert(
                invite.call_id,
                state=CallState.IN_CALL.value,
                caller=invite.caller,
                callee=dialed_target,
                route_kind=GROUP_TYPE_RING,
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
                callee=dialed_target,
                peer_name=connected_party,
                call_id=invite.call_id,
                dest_call_id=client.dialog_ids.call_id,
                dialed_target=dialed_target,
                connected_party=connected_party,
                answered_by=connected_party,
                selected_tx_format=invite.send_format.audio_format.wire_token(),
                selected_rx_format=invite.recv_format.audio_format.wire_token(),
                selected_tx_rtp_format=invite.send_format.wire_token(),
                selected_rx_rtp_format=invite.recv_format.wire_token(),
                sip_status_code=200,
                last_sip_event="SIP_RESPONSE",
                route_kind=GROUP_TYPE_RING,
                sip_uri=str(winner.uri),
            )
            if _is_ha_target(invite.caller):
                _set_ha_softphone_call_state(
                    hass,
                    CallState.IN_CALL.value,
                    session_device_id=HA_SOFTPHONE_DEVICE_ID,
                    caller=invite.caller,
                    callee=dialed_target,
                    peer_name=connected_party,
                    direction="outgoing",
                    call_id=invite.call_id,
                    dest_call_id=client.dialog_ids.call_id,
                    dialed_target=dialed_target,
                    connected_party=connected_party,
                    answered_by=connected_party,
                    selected_tx_format=invite.send_format.audio_format.wire_token(),
                    selected_rx_format=invite.recv_format.audio_format.wire_token(),
                    selected_tx_rtp_format=invite.send_format.wire_token(),
                    selected_rx_rtp_format=invite.recv_format.wire_token(),
                    audio_mode="full_duplex",
                    route_kind=GROUP_TYPE_RING,
                    sip_status_code=200,
                    last_sip_event="SIP_RESPONSE",
                    sip_uri=str(winner.uri),
                )
            terminal = await client.wait_for_dialog_termination()
            terminal_reason = (
                TerminalReason.REMOTE_HANGUP.value
                if terminal == "remote_hangup"
                else _sip_terminal_reason(terminal, _sip_public_state(terminal))
            )
            await _terminate_sip_bridge(hass, client.dialog_ids.call_id, terminal_reason=terminal_reason)
        except asyncio.CancelledError:
            _pending_routes(hass).pop(invite.call_id, None)
            for attempt in attempts:
                with contextlib.suppress(Exception):
                    await _close_outbound_leg(attempt, bye_or_cancel=True)
            raise

    async def _ring_conference_members(
        *,
        room_name: str,
        caller: str,
        source_host: str,
        entry: RosterEntry,
        peers: list[Peer],
        roster_entries: list[RosterEntry],
    ) -> None:
        manager = conference_manager(hass, local_ip=local_ip)
        members = [str(member).strip() for member in (entry.metadata.get("ring_members") or []) if str(member).strip()]
        attempts: list[OutboundLeg] = []
        for member in members:
            if _caller_matches_member(caller, source_host, member, peers) or _is_ha_target(member):
                continue
            try:
                leg = _prepare_outbound_leg(
                    member=member,
                    peers=peers,
                    roster_entries=roster_entries,
                    local_name=room_name,
                    local_rtp_port_index=0,
                )
            except RuntimeError as err:
                _LOGGER.warning("SIP conference member RTP port allocation failed member=%s: %s", member, err)
                break
            if leg is not None:
                attempts.append(leg)

        async def _dial(attempt: OutboundLeg) -> None:
            client = attempt.client
            uri = attempt.uri
            owned_by_room = False
            try:
                result = await client.invite(
                    target=uri.user or attempt.member,
                    remote_host=uri.host,
                    remote_sip_port=uri.port or int(cfg["sip_port"]),
                    request_uri=str(uri),
                    timeout=8.0,
                )
                if result == "ringing":
                    result = await client.wait_for_final(timeout=RING_GROUP_TIMEOUT_S)
                if result != "in_call" or client.dialog is None:
                    return
                owned_by_room = await manager.add_client_leg(
                    room_name,
                    call_id=client.dialog_ids.call_id,
                    caller=attempt.member,
                    client=client,
                    port_reservation=attempt.ports,
                    role="auto_invited",
                )
                if not owned_by_room:
                    return
                terminal = await client.wait_for_dialog_termination()
                terminal_reason = (
                    TerminalReason.REMOTE_HANGUP.value
                    if terminal == "remote_hangup"
                    else _sip_terminal_reason(terminal, _sip_public_state(terminal))
                )
                await manager.leave_call(client.dialog_ids.call_id, reason=terminal_reason)
            except asyncio.CancelledError:
                with contextlib.suppress(Exception):
                    client.bye_or_cancel()
                raise
            except Exception as err:
                _LOGGER.debug("SIP conference member invite failed member=%s: %s", attempt.member, err)
            finally:
                if not owned_by_room:
                    with contextlib.suppress(Exception):
                        await _close_outbound_leg(attempt, bye_or_cancel=True)

        for attempt in attempts:
            hass.async_create_task(_dial(attempt))

    async def _ring_conference_members_from_ha(entry: RosterEntry) -> None:
        peers = await _async_build_peer_snapshot(hass)
        roster_entries = _roster_from_peers(hass, peers, _registered_roster_entries(hass))
        room_name = str(entry.name or entry.id or "")
        await _ring_conference_members(
            room_name=room_name,
            caller=_ha_peer_name(hass),
            source_host=local_ip,
            entry=entry,
            peers=peers,
            roster_entries=roster_entries,
        )

    hass.data.setdefault(DOMAIN, {})["async_ring_conference_members"] = _ring_conference_members_from_ha

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
        registered_entries = _registered_roster_entries(hass)
        roster_entries = _roster_from_peers(hass, peers, registered_entries)
        caller_entry = _roster_entry_for_target(invite.caller, registered_entries)
        if caller_entry is None and invite.caller_uri is not None:
            caller_entry = _roster_entry_for_target(invite.caller_uri.user, registered_entries)
        caller_is_registered_endpoint = bool(caller_entry is not None and caller_entry.metadata.get("registered"))
        decision = _inbound_route_decision(invite, peers, roster_entries)
        bucket = hass.data.setdefault(DOMAIN, {})
        registry = _call_registry(hass)
        route_bucket = _pending_routes(hass)
        pending = registry.pending_invites
        if invite.call_id in route_bucket:
            _LOGGER.debug("SIP INVITE retransmit while route is pending call_id=%s", invite.call_id)
            return SipInviteResult(100, "Trying", to_tag="")
        if invite.call_id in pending:
            _LOGGER.debug("SIP INVITE retransmit while HA softphone is ringing call_id=%s", invite.call_id)
            return SipInviteResult(180, "Ringing", to_tag="", defer_final=True)
        if decision.action is RouteAction.GROUP:
            group_type = str((decision.entry.metadata or {}).get("group_type") or "") if decision.entry is not None else ""
            if group_type == GROUP_TYPE_CONFERENCE:
                ring_members = [str(member).strip() for member in ((decision.entry.metadata or {}).get("ring_members") or [])]
                ring_ha = any(_is_ha_target(member) for member in ring_members)
                registry.upsert(
                    invite.call_id,
                    state=CallState.IN_CALL.value,
                    caller=invite.caller,
                    callee=invite.target,
                    route_kind=GROUP_TYPE_CONFERENCE,
                )
                registry.add_leg(invite.call_id, invite.call_id, role="caller", state=CallState.IN_CALL.value)
                result = await conference_manager(hass, local_ip=local_ip).join(invite, decision.entry, ring_ha=ring_ha)
                if result.status == 200:
                    hass.async_create_task(
                        _ring_conference_members(
                            room_name=str(decision.entry.name or decision.entry.id or invite.target),
                            caller=invite.caller,
                            source_host=invite.source_host,
                            entry=decision.entry,
                            peers=peers,
                            roster_entries=roster_entries,
                        )
                    )
                return result
            if group_type == GROUP_TYPE_RING and decision.entry is not None:
                registry.upsert(
                    invite.call_id,
                    state=CallState.RINGING.value,
                    caller=invite.caller,
                    callee=invite.target,
                    route_kind=GROUP_TYPE_RING,
                )
                registry.add_leg(invite.call_id, invite.call_id, role="caller", state=CallState.RINGING.value)
                hass.async_create_task(_run_ring_group_call(invite, decision.entry, peers, roster_entries))
                return SipInviteResult(180, "Ringing", to_tag="", defer_final=True)
            return SipInviteResult(480, "Temporarily Unavailable", to_tag="")
        if _is_trunk_invite(invite):
            trunk_cfg = _get_trunk_config(hass)
            dtmf_timeout_ms = max(0, int(trunk_cfg.get(CONF_TRUNK_DTMF_TIMEOUT_MS) or 0))
            dtmf_preanswer = bool(trunk_cfg.get(CONF_TRUNK_DTMF_ENABLED) and dtmf_timeout_ms > 0)
            if not dtmf_preanswer:
                _LOGGER.info(
                    "SIP trunk inbound skips DTMF pre-answer call_id=%s caller=%s",
                    invite.call_id,
                    invite.caller or invite.source_host,
                )
                invite = replace(invite, target=_ha_peer_name(hass))
                decision = route_inbound_trunk(
                    CallContext(
                        call_id=invite.call_id,
                        direction="inbound",
                        origin="trunk",
                        caller=invite.caller,
                        called_did=str(invite.request_uri.user or ""),
                        requested_target=_ha_peer_name(hass),
                        source_host=invite.source_host,
                    ),
                    roster_entries,
                    trunk_ready=False,
                )
                # Continue into the normal route_requested path so HA
                # automations can still forward/bridge/decline the call
                # before the default HA softphone ringing response.
                source_relay_port = 0
                dest_relay_port = 0
            else:
                try:
                    bridge_ports = RtpPortReservation.allocate(hass)
                except RuntimeError as err:
                    _LOGGER.warning("SIP trunk RTP bridge port allocation failed: %s", err)
                    return SipInviteResult(503, "Service Unavailable", to_tag="")
                source_relay_port, _dest_relay_port = bridge_ports.ports
                dtmf_format = None
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
                        bridge_ports=bridge_ports,
                    )
                )
                return SipInviteResult(200, "OK", answer_sdp=answer, to_tag="")
        route_action = "default"
        route_destination = ""
        route_status = 0
        route_reason = ""
        route_decline_reason = ""
        if caller_is_registered_endpoint:
            _LOGGER.debug(
                "SIP registered endpoint uses central dialplan caller=%s target=%s route=%s uri=%s",
                invite.caller or invite.source_host,
                invite.target,
                decision.action.value,
                decision.sip_uri or "-",
            )
        else:
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
            elif decision.entry is not None and decision.entry.sip_uri:
                bridge_uri = parse_sip_uri(decision.entry.sip_uri)
            elif decision.entry is not None and not decision.entry.metadata.get("local_ha") and decision.entry.address:
                bridge_port = int(decision.entry.port or (decision.entry.metadata or {}).get("port") or (decision.entry.metadata or {}).get("sip_port") or cfg["sip_port"])
                bridge_uri = parse_sip_uri(f"sip:{decision.entry.id}@{decision.entry.address}:{bridge_port}")
            decision_uri = bridge_uri or (parse_sip_uri(decision.sip_uri) if decision.sip_uri else None)
            if decision_uri is not None and decision_uri.host != local_ip:
                try:
                    bridge_ports = RtpPortReservation.allocate(hass)
                except RuntimeError as err:
                    _LOGGER.warning("SIP RTP bridge port allocation failed: %s", err)
                    return SipInviteResult(503, "Service Unavailable", to_tag="")
                source_relay_port, dest_relay_port = bridge_ports.ports
                peer_target = _peer_for_target(decision.target or invite.target, peers)
                remote_tx_formats = _peer_audio_formats(peer_target, "tx_formats") or _roster_entry_formats(decision.entry, "tx_formats")
                remote_rx_formats = _peer_audio_formats(peer_target, "rx_formats") or _roster_entry_formats(decision.entry, "rx_formats")
                sip_send_formats, sip_recv_formats = _sip_target_audio_profile(
                    remote_tx_formats=remote_tx_formats,
                    remote_rx_formats=remote_rx_formats,
                    target=decision.target or invite.target,
                )
                bridge_to_softphone = bool(
                    decision.entry is not None and decision.entry.sip_uri and decision.entry.metadata.get("registered")
                )
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
                    request_uri=str(decision_uri),
                    timeout=SIP_TIMER_B if bridge_to_trunk else 8.0,
                )
                if invite.call_id in bucket.get("trunk_closed_calls", set()):
                    bucket["trunk_closed_calls"].discard(invite.call_id)
                    _LOGGER.info(
                        "SIP bridge invite completed after caller cancelled call_id=%s; closing outbound leg",
                        invite.call_id,
                    )
                    with contextlib.suppress(Exception):
                        client.bye_or_cancel()
                    await _close_client_and_release(client, bridge_ports)
                    return SipInviteResult(
                        487,
                        "Request Terminated",
                        to_tag="",
                        decline_reason=TerminalReason.CANCELLED.value,
                    )
                if result not in {"ringing", "in_call"}:
                    status_code, sip_reason, terminal_reason, public_state = _sip_failure_response(result)
                    await _close_client_and_release(client, bridge_ports)
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
                        await _close_client_and_release(client, bridge_ports)
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
                            on_release=lambda ports: _release_sip_rtp_port_pair(hass, ports),
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
                        await _close_client_and_release(client, bridge_ports)
                        return
                    bridge_ports.detach()
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
                    reason="dnd",
                    terminal_reason="dnd",
                    origin="self",
                    sip_status_code=486,
                    last_sip_event="SIP_RESPONSE",
                )
                return SipInviteResult(486, "Busy Here", to_tag="", decline_reason="dnd")
            _defer_invite_to_ha_softphone(invite, route_kind=decision.action.value, sip_uri=decision.sip_uri)
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
        bucket.setdefault("trunk_closed_calls", set()).add(call_id)
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
        preanswered_item = registry.preanswered.pop(call_id, None)
        _release_media_reservation(preanswered_item)
        active_media = registry.softphone_media.pop(call_id, {})
        _release_media_reservation(active_media)
        active_media_invite = active_media.get("invite")
        if invite is None:
            invite = active_media_invite
        session = registry.sessions.get(registry.resolve_session_id(call_id))
        source_call_id, dest_call_id, relay, client, watcher, _called_by_dest = registry.detach_bridge(call_id)
        if source_call_id:
            call_id = source_call_id
        event_caller = invite.caller if invite is not None else (session.caller if session is not None else "")
        event_callee = invite.target if invite is not None else (session.callee if session is not None else "")
        softphone_store = bucket.get("ha_softphone", {})
        softphone_call_id = str(softphone_store.get("call_id") or "")
        terminal_reason = reason or "remote_hangup"
        terminal_state = (
            CallState.CANCELLED.value
            if terminal_reason == TerminalReason.CANCELLED.value
            else CallState.IDLE.value
        )
        manager = bucket.get("conference_manager")
        if manager is not None and await manager.leave_call(call_id, reason=terminal_reason):
            registry.finish_and_pop(call_id, reason=terminal_reason, state=terminal_state)
            return
        if relay is not None or client is not None:
            await async_cleanup_sip_runtime(
                relay=relay,
                client=client,
                watcher=watcher,
                terminate_client=True,
                relay_first=False,
            )
            _set_sip_bridge_call_state(
                hass,
                terminal_state,
                call_id=call_id,
                dest_call_id=dest_call_id,
                caller=event_caller,
                callee=event_callee,
                peer_name=event_callee,
                reason=terminal_reason,
                terminal_reason=terminal_reason,
                origin="remote",
                last_sip_event="BYE",
            )
            _fire_call_event(
                hass,
                {
                    "state": terminal_state,
                    "scope": "sip_bridge",
                    "call_id": call_id,
                    "dest_call_id": dest_call_id,
                    "caller": event_caller,
                    "callee": event_callee,
                    "peer_name": event_callee,
                    "target": event_callee,
                    "reason": terminal_reason,
                    "terminal_reason": terminal_reason,
                },
                "sip",
            )
            registry.finish_and_pop(call_id, reason=terminal_reason, state=terminal_state)
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
        if relay is not None or client is not None:
            _LOGGER.info(
                "SIP bridge terminated call_id=%s reason=%s relay=%s dest_client=%s",
                call_id,
                terminal_reason,
                relay is not None,
                bool(dest_call_id),
            )

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
