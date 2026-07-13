"""Runtime SIP endpoint/B2BUA orchestration for VoIP Stack."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, replace
import logging
import secrets
import time
from typing import TYPE_CHECKING

from homeassistant.core import HomeAssistant

from . import sdp as sip_sdp
from .audio_format import HA_SIP_PCM_FORMATS, HA_SIP_PCM_RX_FORMATS, HA_SIP_PCM_TX_FORMATS, HA_TRUNK_AUDIO_FORMATS
from .call_registry import TERMINAL_STATES
from .config import debug_mode as _debug_mode
from .const import (
    CONF_ASSIST_PIPELINE,
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
from .endpoint_lifecycle import (
    async_stop_sip_endpoint,
    call_registry as _call_registry,
    create_runtime_task,
)
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
from .router import CallContext, RouteAction, RouteReason, route_inbound_trunk, resolve_ha_router
from .session_cleanup import async_cleanup_sip_runtime
from .sip_bridge import build_invite_client_relay
from .store import sip_accounts as _sip_accounts
from .websocket_api import (
    SIP_DTMF_EVENT,
    _fire_call_event,
    _ha_softphone_dnd,
    _release_ha_softphone_claim,
    _set_ha_softphone_call_state,
    _set_sip_bridge_call_state,
)

if TYPE_CHECKING:
    from .peer import Peer
    from .roster import RosterEntry

_LOGGER = logging.getLogger(__name__)
SIP_ROUTE_DECISION_TIMEOUT = 1.5
RING_GROUP_TIMEOUT_S = 30.0
MAX_RING_GROUP_ATTEMPTS = 16
MAX_TRUNK_INFO_DIGITS = 16


def _publish_dtmf_event(
    hass: HomeAssistant,
    *,
    call_id: str,
    dest_call_id: str,
    caller: str,
    callee: str,
    side: str,
    digit: str,
    transport: str,
) -> None:
    """Publish one canonical in-dialog DTMF occurrence."""
    source_is_caller = side == "left"
    registry = _call_registry(hass)
    context = registry.event_context(call_id)
    state = context.state if context is not None else CallState.IN_CALL.value
    session = registry.sessions.get(registry.resolve_session_id(call_id))
    payload = {
        "schema_version": 1,
        "event_type": "dtmf",
        "state": state,
        "sip_state": state,
        "call_id": call_id,
        "dest_call_id": dest_call_id,
        "caller": caller,
        "callee": callee,
        "source": caller if source_is_caller else callee,
        "source_leg": "caller" if source_is_caller else "callee",
        "side": side,
        "digit": digit,
        "transport": transport,
        "direction": str(
            (session.metadata if session is not None else {}).get("direction")
            or "incoming"
        ),
        "scope": "sip_bridge",
        "origin": "sip_bridge",
        "route_kind": session.route_kind if session is not None else "",
        "automation_control": "ha_anchored",
    }
    payload.update(registry.event_fields(call_id, state))
    hass.bus.async_fire(SIP_DTMF_EVENT, payload)


def _attach_dtmf_event_bridge(
    hass: HomeAssistant,
    relay,
    *,
    call_id: str,
    dest_call_id: str,
    caller: str,
    callee: str,
    client=None,
) -> None:
    """Publish one HA event for each negotiated in-dialog DTMF press."""

    def _emit(side: str, digit: str, transport: str) -> None:
        _publish_dtmf_event(
            hass,
            call_id=call_id,
            dest_call_id=dest_call_id,
            caller=caller,
            callee=callee,
            side=side,
            digit=digit,
            transport=transport,
        )

    relay.on_dtmf = _emit
    if client is not None:
        client.on_info_dtmf = lambda digit: _emit("right", digit, "sip_info")


def _invite_dtmf_format(invite):
    formats = sip_sdp.offered_dtmf_formats(invite.remote_sdp)
    return formats[0] if formats else None


def _unique_group_members(value) -> list[str]:
    members: list[str] = []
    seen: set[str] = set()
    raw_members = value.split(",") if isinstance(value, str) else (value or [])
    for raw in raw_members:
        member = str(raw).strip()
        key = member.casefold()
        if member and key not in seen:
            seen.add(key)
            members.append(member)
    return members


def _dtmf_extension_routes(entries) -> dict[str, str]:
    return {
        str(entry.extension).strip(): str(entry.extension).strip()
        for entry in entries
        if str(getattr(entry, "extension", "") or "").strip()
    }


async def async_start_sip_endpoint(hass: HomeAssistant) -> bool:
    """Bind the enabled SIP signaling listeners for HA softphone and bridge calls."""
    from . import (
        _get_transport_config,
        _get_trunk_config,
        _trunk_enabled,
        _ha_advertise_host,
        _ha_peer_name,
        _ha_softphone_has_active_call,
        _sip_send_bye,
        _sip_send_final_response,
        _sip_uri_transport,
        _enable_reused_sip_tcp_connection,
        _async_build_peer_snapshot,
        _pending_routes,
        _refresh_and_push_phonebook,
        _terminate_sip_bridge,
    )
    from .dtmf import DtmfCollector, collect_info_digits, parse_sip_info_digit
    from .sdp import build_answer_directional
    from .sip import parse_sip_uri
    from .sip_client import SIP_TIMER_B, SipCallClient
    from .sip_endpoint import SipEndpointManager
    from .sip_listener import SipInvite, SipInviteResult
    from .sip_registrar import SipRegistrar
    from .conference import MAX_CONFERENCE_LEGS, conference_manager
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

    async def _on_info(request, addr, transport) -> None:
        digit = parse_sip_info_digit(request.header("Content-Type"), request.body)
        if not digit:
            _LOGGER.info(
                "SIP INFO ignored call_id=%s content_type=%s",
                request.header("Call-ID"),
                request.header("Content-Type") or "-",
            )
            return
        call_id = request.header("Call-ID")
        queue = hass.data.setdefault(DOMAIN, {}).setdefault("trunk_info_queues", {}).get(call_id)
        if queue is None:
            registry = _call_registry(hass)
            relay = registry.relays.get(call_id)
            callback = getattr(relay, "on_dtmf", None)
            if callback is not None:
                callback("left", digit, "sip_info")
                _LOGGER.info("SIP in-call INFO DTMF RX call_id=%s digit=%s transport=%s", call_id, digit, transport)
                return
            if relay is not None or call_id in registry.softphone_media:
                session = registry.sessions.get(registry.resolve_session_id(call_id))
                _publish_dtmf_event(
                    hass,
                    call_id=call_id,
                    dest_call_id=registry.bridge_clients.get(call_id, ""),
                    caller=session.caller if session is not None else "",
                    callee=session.callee if session is not None else "",
                    side="left",
                    digit=digit,
                    transport="sip_info",
                )
                _LOGGER.info(
                    "SIP local in-call INFO DTMF RX call_id=%s digit=%s transport=%s",
                    call_id,
                    digit,
                    transport,
                )
                return
            _LOGGER.info("SIP INFO DTMF arrived outside active call call_id=%s digit=%s", call_id, digit)
            return
        if queue.full():
            _LOGGER.warning("SIP INFO DTMF queue full call_id=%s; digit ignored", call_id)
            return
        queue.put_nowait(digit)
        _LOGGER.info("SIP trunk INFO DTMF RX call_id=%s digit=%s transport=%s", call_id, digit, transport)

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
        trunk_hosts: set[str] = set()
        for raw_host in (
            trunk_cfg.get(CONF_TRUNK_SERVER),
            trunk_cfg.get(CONF_TRUNK_OUTBOUND_PROXY),
        ):
            value = str(raw_host or "").strip()
            if not value:
                continue
            try:
                parsed = parse_sip_uri(value if value.lower().startswith("sip:") else f"sip:{value}")
                trunk_hosts.add(parsed.host.lower())
            except (TypeError, ValueError):
                trunk_hosts.add(value.lower())
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

    async def _start_local_assist_bridge(
        invite: SipInvite,
        *,
        reservation: RtpPortReservation,
        local_rtp_port: int,
        roster_entries: list[RosterEntry],
        source: str,
        called_extension: str,
        release_reservation_on_failure: bool = True,
    ):
        from .assist_runtime import AssistMediaSession, build_call_context_prompt

        assist_cfg = hass.data.setdefault(DOMAIN, {}).get("assist_config", {})
        caller_entry = _roster_entry_for_target(invite.caller, roster_entries)
        if caller_entry is None and invite.caller_uri is not None:
            caller_entry = _roster_entry_for_target(invite.caller_uri.user, roster_entries)
        if caller_entry is None:
            caller_token = str(invite.caller or "").strip()
            caller_entry = next(
                (
                    entry
                    for entry in roster_entries
                    if caller_token and str(entry.number or "").strip() == caller_token
                ),
                None,
            )
        caller_id = str(
            (invite.caller_uri.user if invite.caller_uri is not None else "")
            or invite.caller
            or invite.source_host
            or "Unknown"
        ).strip()
        caller_name = (
            str(caller_entry.name or caller_entry.id).strip()
            if caller_entry is not None
            else str(invite.caller or caller_id or "Unknown").strip()
        )
        caller_uri = str(invite.caller_uri) if invite.caller_uri is not None else ""
        destination_name = str(assist_cfg.get("name") or "Assist").strip() or "Assist"
        assist_leg_id = f"assist:{invite.call_id}"

        async def _complete(reason: str) -> None:
            await _terminate_sip_bridge(
                hass,
                invite.call_id,
                terminal_reason=reason or TerminalReason.PROTOCOL_ERROR.value,
            )

        media = AssistMediaSession(
            hass,
            invite=invite,
            local_rtp_port=local_rtp_port,
            reservation=reservation,
            pipeline_id=str(assist_cfg.get(CONF_ASSIST_PIPELINE) or "preferred"),
            caller_label=caller_name,
            extra_system_prompt=build_call_context_prompt(
                caller=caller_name,
                caller_id=caller_id,
                caller_uri=caller_uri,
                caller_in_phonebook=caller_entry is not None,
                source=source,
                called_extension=called_extension,
            ),
            on_complete=_complete,
        )
        try:
            await media.start()
        except Exception:
            if release_reservation_on_failure:
                reservation.release()
            raise

        registry = _call_registry(hass)
        registry.bridge_clients[invite.call_id] = assist_leg_id
        registry.relays[invite.call_id] = media
        registry.upsert(
            invite.call_id,
            state=CallState.IN_CALL.value,
            owner="assist",
            caller=caller_name,
            callee=destination_name,
            route_kind=RouteAction.ASSIST.value,
        )
        registry.add_leg(
            invite.call_id,
            invite.call_id,
            role="trunk" if source == "trunk" else "caller",
            state=CallState.IN_CALL.value,
        )
        registry.add_leg(
            invite.call_id,
            assist_leg_id,
            role="assist",
            state=CallState.IN_CALL.value,
        )
        _set_sip_bridge_call_state(
            hass,
            CallState.IN_CALL.value,
            caller=caller_name,
            callee=destination_name,
            peer_name=destination_name,
            call_id=invite.call_id,
            dest_call_id=assist_leg_id,
            route_kind=RouteAction.ASSIST.value,
            selected_tx_format=invite.send_format.audio_format.wire_token(),
            selected_rx_format=invite.recv_format.audio_format.wire_token(),
            selected_tx_rtp_format=invite.send_format.wire_token(),
            selected_rx_rtp_format=invite.recv_format.wire_token(),
            sip_status_code=200,
            last_sip_event="ASSIST_PIPELINE",
            caller_uri=caller_uri,
            source=source,
        )
        return media

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
                with contextlib.suppress(Exception):
                    await client.terminate()
            await client.close()
        finally:
            ports.release()

    async def _close_outbound_leg(attempt: OutboundLeg, *, cancel: bool = False, bye_or_cancel: bool = False) -> None:
        if cancel:
            async def _finish_cancel() -> None:
                try:
                    with contextlib.suppress(Exception):
                        await attempt.client.terminate(timeout=RING_GROUP_TIMEOUT_S)
                    await attempt.client.close()
                finally:
                    attempt.ports.release()

            create_runtime_task(hass, _finish_cancel())
            return
        try:
            if bye_or_cancel:
                with contextlib.suppress(Exception):
                    await attempt.client.terminate()
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
        session = registry.upsert(
            invite.call_id,
            state=CallState.RINGING.value,
            caller=invite.caller,
            callee=callee or invite.target,
            route_kind=route_kind,
            owner="ha_softphone",
        )
        registry.add_leg(invite.call_id, invite.call_id, role="ha_softphone", state=CallState.RINGING.value)
        expected_revision = session.revision

        def _publish_ringing_if_current() -> None:
            if not registry.is_current(
                invite.call_id,
                revision=expected_revision,
                owner="ha_softphone",
            ):
                _LOGGER.debug(
                    "Ignoring stale HA ringing callback for call %s revision %s",
                    invite.call_id,
                    expected_revision,
                )
                return
            _set_ha_softphone_call_state(
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

        hass.loop.call_soon(_publish_ringing_if_current)

    def _inbound_route_decision(invite: SipInvite, peers: list[Peer], entries: list[RosterEntry]):
        # Once an INVITE reached HA, HA is the router. ESP-origin direct-vs-HA
        # decisions are made before dialing by the ESP phonebook mirror.
        return _ha_router_decision(invite.target, entries)

    async def _run_trunk_inbound_route(
        invite: SipInvite,
        *,
        bridge_ports: RtpPortReservation,
        route_future: asyncio.Future | None = None,
    ) -> None:
        source_relay_port, dest_relay_port = bridge_ports.ports
        bucket = hass.data.setdefault(DOMAIN, {})
        trunk_cfg = _get_trunk_config(hass)
        dtmf_timeout_ms = max(0, int(trunk_cfg.get(CONF_TRUNK_DTMF_TIMEOUT_MS) or 0))
        dtmf_formats = sip_sdp.offered_dtmf_formats(invite.remote_sdp)
        dtmf_format = dtmf_formats[0] if dtmf_formats else None
        destination = ""
        digits = ""
        automation_decision: dict = {}
        peers = await _async_build_peer_snapshot(hass)
        roster_entries = _roster_from_peers(hass, peers, _registered_roster_entries(hass))
        routes = _dtmf_extension_routes(roster_entries)
        if trunk_cfg.get(CONF_TRUNK_DTMF_ENABLED) and dtmf_timeout_ms > 0:
            timeout = float(dtmf_timeout_ms) / 1000.0
            terminator = str(trunk_cfg.get(CONF_TRUNK_DTMF_TERMINATOR) or "")
            info_queue = bucket.setdefault("trunk_info_queues", {}).setdefault(
                invite.call_id,
                asyncio.Queue(maxsize=MAX_TRUNK_INFO_DIGITS),
            )
            collector_tasks = {
                asyncio.create_task(
                    collect_info_digits(
                        info_queue,
                        routes=routes,
                        timeout=timeout,
                        terminator=terminator,
                    )
                )
            }
            if dtmf_format is not None:
                collector_tasks.add(
                    asyncio.create_task(
                        DtmfCollector(
                            host="0.0.0.0",
                            port=source_relay_port,
                            payload_type=dtmf_format.payload_type,
                            routes=routes,
                            timeout=timeout,
                            terminator=terminator,
                            remote_host=invite.remote_rtp_host,
                        ).collect()
                    )
                )
            else:
                _LOGGER.info(
                    "SIP trunk inbound call has no telephone-event SDP offer; collecting SIP INFO for %.1fs",
                    timeout,
                )
            pending = set(collector_tasks)
            if route_future is not None:
                pending.add(route_future)
            try:
                while pending and not digits and not automation_decision:
                    done, pending = await asyncio.wait(pending, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        if task is route_future:
                            decision_data = task.result() if not task.cancelled() else {}
                            action = str((decision_data or {}).get("action") or "default").strip().lower()
                            if action != "default":
                                automation_decision = dict(decision_data or {})
                                break
                            continue
                        try:
                            candidate_digits, candidate_destination = task.result()
                        except Exception as err:
                            _LOGGER.info("SIP trunk DTMF collector unavailable: %s", err)
                            continue
                        if candidate_digits:
                            digits, destination = candidate_digits, candidate_destination
                            break
                    # Once every DTMF collector has completed, only an
                    # optional automation decision may remain pending. Do not
                    # let that future extend the configured DTMF window: the
                    # normal trunk route must resume immediately.
                    if not any(task in pending for task in collector_tasks):
                        break
            finally:
                remaining_collectors = [
                    task for task in pending if task is not route_future
                ]
                for task in remaining_collectors:
                    task.cancel()
                await asyncio.gather(
                    *remaining_collectors, return_exceptions=True
                )
        route = _pending_routes(hass).pop(invite.call_id, None)
        if route is not None:
            future = route.get("future")
            if future is not None and not future.done():
                future.cancel()
        bucket.setdefault("trunk_info_queues", {}).pop(invite.call_id, None)

        if invite.call_id in bucket.get("trunk_closed_calls", set()):
            bucket["trunk_closed_calls"].discard(invite.call_id)
            _LOGGER.info("SIP trunk inbound call_id=%s closed before routing", invite.call_id)
            bridge_ports.release()
            return

        automation_action = str(automation_decision.get("action") or "").strip().lower()
        if automation_action in {"forward", "bridge"}:
            await _async_forward_existing_call(
                call_id=invite.call_id,
                destination=str(automation_decision.get("destination") or ""),
                on_failure="resume",
            )
            return
        if automation_action in {"decline", "busy", "cancel"}:
            registry = _call_registry(hass)
            registry.pending_invites.pop(invite.call_id, None)
            registry.preanswered.pop(invite.call_id, None)
            status = 486 if automation_action == "busy" else 603
            reason = (
                TerminalReason.BUSY.value
                if automation_action == "busy"
                else TerminalReason.CANCELLED.value
                if automation_action == "cancel"
                else TerminalReason.DECLINED.value
            )
            _sip_send_bye(hass, invite.call_id)
            bridge_ports.release()
            _set_sip_bridge_call_state(
                hass,
                CallState.BUSY.value if automation_action == "busy" else CallState.DECLINED.value,
                caller=invite.caller,
                callee=invite.target,
                peer_name=invite.caller,
                call_id=invite.call_id,
                direction="incoming",
                reason=reason,
                terminal_reason=reason,
                origin="automation",
                sip_status_code=status,
                last_sip_event="BYE",
            )
            registry.finish_and_pop(invite.call_id, reason=reason)
            return

        default_target = str(trunk_cfg.get(CONF_TRUNK_INBOUND_DEFAULT_TARGET) or "HA").strip() or "HA"
        route_hint = destination or digits
        decision = route_inbound_trunk(
            CallContext(
                call_id=invite.call_id,
                direction="inbound",
                origin="trunk",
                caller=invite.caller,
                route_hint=route_hint,
                source_host=invite.source_host,
            ),
            roster_entries,
            trunk_ready=False,
        )
        if decision.action is RouteAction.ANSWER_HA:
            destination = default_target
        elif decision.action is RouteAction.REJECT:
            registry = _call_registry(hass)
            registry.pending_invites.pop(invite.call_id, None)
            registry.preanswered.pop(invite.call_id, None)
            _LOGGER.info("SIP trunk route not found call_id=%s digits=%s hint=%s", invite.call_id, digits or "-", route_hint or "-")
            _sip_send_bye(hass, invite.call_id)
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

        if decision.action is RouteAction.ASSIST:
            registry = _call_registry(hass)
            registry.pending_invites.pop(invite.call_id, None)
            registry.preanswered.pop(invite.call_id, None)
            try:
                await _start_local_assist_bridge(
                    invite,
                    reservation=bridge_ports,
                    local_rtp_port=source_relay_port,
                    roster_entries=roster_entries,
                    source="trunk",
                    called_extension=digits or route_hint,
                )
            except Exception as err:
                _LOGGER.exception("SIP trunk Assist bridge failed call_id=%s", invite.call_id)
                _sip_send_bye(hass, invite.call_id)
                _set_sip_bridge_call_state(
                    hass,
                    CallState.MEDIA_INCOMPATIBLE.value,
                    caller=invite.caller,
                    callee=destination,
                    call_id=invite.call_id,
                    reason=str(err),
                    terminal_reason=TerminalReason.MEDIA_INCOMPATIBLE.value,
                    origin="self",
                    sip_status_code=488,
                    last_sip_event="BYE",
                )
            return

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
                owner="ha_softphone",
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
                scope="sip_trunk",
                dtmf_digits=digits,
                target=destination,
            )
            return

        decision = _ha_router_decision(destination, _roster_from_peers(hass, peers, _registered_roster_entries(hass)))
        registry = _call_registry(hass)
        registry.pending_invites.pop(invite.call_id, None)
        registry.preanswered.pop(invite.call_id, None)
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
            _attach_dtmf_event_bridge(
                hass,
                relay,
                call_id=invite.call_id,
                dest_call_id=client.dialog_ids.call_id,
                caller=invite.caller,
                callee=destination,
                client=client,
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
            scope="sip_trunk",
            dtmf_digits=digits,
        )

    async def _async_forward_existing_call(
        *,
        call_id: str,
        destination: str,
        on_failure: str = "resume",
        expected_state: str = "",
        expected_sequence: int = 0,
    ) -> None:
        """Move one HA-owned pending/ringing call to another dial-plan target."""
        from homeassistant.exceptions import ServiceValidationError

        call_id = str(call_id or "").strip()
        destination = str(destination or "").strip()
        on_failure = str(on_failure or "resume").strip().lower()
        if not call_id or not destination:
            raise ServiceValidationError("call_id and destination are required")
        if on_failure not in {"resume", "terminate", "busy"}:
            raise ServiceValidationError("on_failure must be resume, terminate, or busy")

        registry = _call_registry(hass)
        context = registry.event_context(call_id)
        expected_state = str(expected_state or "").strip().lower()
        if expected_state and (context is None or context.state != expected_state):
            actual = context.state if context is not None else "ended"
            raise ServiceValidationError(
                f"call_id {call_id} is {actual}, expected {expected_state}"
            )
        if expected_sequence and (
            context is None or context.sequence != int(expected_sequence)
        ):
            actual = context.sequence if context is not None else 0
            raise ServiceValidationError(
                f"call_id {call_id} sequence is {actual}, expected {expected_sequence}"
            )
        if context is not None and len(context.route_history) >= 8:
            raise ServiceValidationError(f"call_id {call_id} exceeded 8 routing hops")

        invite = registry.pending_invites.get(call_id)
        if invite is None:
            raise ServiceValidationError(
                f"call_id {call_id} is not a forwardable pending or ringing HA-owned call"
            )
        forward_tasks = hass.data.setdefault(DOMAIN, {}).setdefault("forward_tasks", {})
        forward_claims = hass.data.setdefault(DOMAIN, {}).setdefault(
            "forward_claims", set()
        )
        current_forward = forward_tasks.get(call_id)
        if current_forward is not None and not current_forward.done():
            current_context = registry.event_context(call_id)
            if (
                current_context is None
                or current_context.state != CallState.REMOTE_RINGING.value
            ):
                raise ServiceValidationError(
                    f"call_id {call_id} is already being forwarded"
                )
            current_forward.cancel()
            await asyncio.gather(current_forward, return_exceptions=True)
        if call_id in forward_claims:
            raise ServiceValidationError(f"call_id {call_id} is already being forwarded")
        forward_claims.add(call_id)
        try:
            peers = await _async_build_peer_snapshot(hass)
            if registry.pending_invites.get(call_id) is not invite:
                raise ServiceValidationError(
                    f"call_id {call_id} changed while the route was being resolved"
                )
            context = registry.event_context(call_id)
            if expected_state and (
                context is None or context.state != expected_state
            ):
                actual = context.state if context is not None else "ended"
                raise ServiceValidationError(
                    f"call_id {call_id} is {actual}, expected {expected_state}"
                )
            if expected_sequence and (
                context is None or context.sequence != int(expected_sequence)
            ):
                actual = context.sequence if context is not None else 0
                raise ServiceValidationError(
                    f"call_id {call_id} sequence is {actual}, expected {expected_sequence}"
                )
            roster_entries = _roster_from_peers(
                hass,
                peers,
                _registered_roster_entries(hass),
            )
            decision = _ha_router_decision(destination, roster_entries)
            if decision.action in {RouteAction.REJECT, RouteAction.ANSWER_HA}:
                raise ServiceValidationError(
                    f"destination {destination} is not a forwardable SIP dial-plan target"
                )
            if decision.action is RouteAction.GROUP and str(
                (
                    (decision.entry.metadata if decision.entry is not None else {})
                    or {}
                ).get("group_type")
                or ""
            ) != GROUP_TYPE_RING:
                raise ServiceValidationError(
                    "forwarding an already-ringing call is currently limited to ring groups"
                )

            last_route = context.route_history[-1] if context and context.route_history else {}
            if not (
                last_route.get("action") in {"forward", "bridge"}
                and last_route.get("destination") == destination
            ):
                registry.record_route(
                    call_id,
                    action="forward",
                    destination=destination,
                    source="automation",
                )
            session = registry.sessions.get(registry.resolve_session_id(call_id))
            original_callee = session.callee if session is not None else invite.target
            original_route_kind = session.route_kind if session is not None else ""
            # A trunk call is already SIP-answered while its DTMF/automation
            # route is being selected.  It has not populated the HA softphone
            # store yet, but HA is still its default owner.  Preserve that
            # ownership so ``on_failure: resume`` can enter normal ringing
            # instead of leaving the answered caller on silent RTP.
            ha_claimed = (
                bool(session is not None and session.owner == "ha_softphone")
                or call_id in registry.preanswered
                or bool(
                    session is not None
                    and session.metadata.get("automation_resume_ha")
                )
            )
            if session is not None:
                session.metadata["automation_resume_ha"] = ha_claimed
                claimed = registry.transition(
                    call_id,
                    state=CallState.CONNECTING.value,
                    owner="router",
                    callee=destination,
                    route_kind=decision.action.value,
                    expected_revision=session.revision,
                    expected_owner=session.owner,
                    automation_resume_ha=ha_claimed,
                )
                if claimed is None:
                    raise ServiceValidationError(
                        f"call_id {call_id} changed while forwarding ownership was claimed"
                    )
            _release_ha_softphone_claim(
                hass,
                call_id,
                destination=destination,
            )
            _set_sip_bridge_call_state(
                hass,
                CallState.CONNECTING.value,
                caller=invite.caller,
                callee=destination,
                peer_name=destination,
                call_id=call_id,
                direction="incoming",
                route_source="automation",
                route_kind=decision.action.value,
                event_type="forwarding",
                last_sip_event="ROUTE_FORWARD",
            )
        except Exception:
            forward_claims.discard(call_id)
            raise

        async def _restore_or_terminate(reason: str) -> None:
            preanswered = registry.preanswered.get(call_id)
            if on_failure == "resume" and call_id not in hass.data.setdefault(DOMAIN, {}).get(
                "trunk_closed_calls", set()
            ):
                if session is not None:
                    current = registry.sessions.get(registry.resolve_session_id(call_id))
                    if current is None or current.owner not in {"router", "bridge", "assist"}:
                        return
                    resumed = registry.transition(
                        call_id,
                        state=CallState.RINGING.value,
                        owner="ha_softphone",
                        callee=original_callee,
                        route_kind=original_route_kind,
                        expected_revision=current.revision,
                        expected_owner=current.owner,
                    )
                    if resumed is None:
                        return
                if ha_claimed:
                    _set_ha_softphone_call_state(
                        hass,
                        CallState.RINGING.value,
                        session_device_id=HA_SOFTPHONE_DEVICE_ID,
                        caller=invite.caller,
                        callee=original_callee,
                        peer_name=invite.caller,
                        direction="incoming",
                        call_id=call_id,
                        selected_tx_format=invite.send_format.audio_format.wire_token(),
                        selected_rx_format=invite.recv_format.audio_format.wire_token(),
                        selected_tx_rtp_format=invite.send_format.wire_token(),
                        selected_rx_rtp_format=invite.recv_format.wire_token(),
                        audio_mode="full_duplex",
                        route_kind=original_route_kind,
                        sip_status_code=200 if preanswered is not None else 180,
                        last_sip_event="ROUTE_RESUME",
                    )
                return

            registry.pending_invites.pop(call_id, None)
            preanswered = registry.preanswered.pop(call_id, None)
            if preanswered is not None:
                _release_media_reservation(preanswered)
                _sip_send_bye(hass, call_id)
            else:
                status = 486 if on_failure == "busy" else 480
                _sip_send_final_response(
                    hass,
                    call_id,
                    status,
                    "Busy Here" if status == 486 else "Temporarily Unavailable",
                    decline_reason=reason,
                )
            terminal_state = (
                CallState.BUSY.value
                if on_failure == "busy"
                else CallState.TRANSPORT_UNREACHABLE.value
            )
            current = registry.sessions.get(registry.resolve_session_id(call_id))
            if current is not None:
                registry.transition(
                    call_id,
                    state=terminal_state,
                    owner="terminal",
                    outcome=reason,
                    expected_revision=current.revision,
                    expected_owner=current.owner,
                )
            _set_sip_bridge_call_state(
                hass,
                terminal_state,
                caller=invite.caller,
                callee=destination,
                call_id=call_id,
                reason=reason,
                terminal_reason=reason,
                origin="self",
                last_sip_event="BYE" if preanswered is not None else "SIP_RESPONSE",
            )
            registry.finish_and_pop(call_id, reason=reason, state=terminal_state)

        async def _run_forward() -> None:
            client = None
            reservation = None
            reservation_from_preanswer = False
            dest_call_id = ""
            try:
                preanswered = registry.preanswered.get(call_id)
                reservation = (preanswered or {}).get("rtp_reservation")
                reservation_from_preanswer = reservation is not None
                if reservation is None:
                    reservation = RtpPortReservation.allocate(hass)
                source_relay_port, dest_relay_port = reservation.ports

                if decision.action is RouteAction.GROUP:
                    entry = decision.entry
                    if entry is None:
                        raise RuntimeError("ring group has no roster entry")
                    members = _unique_group_members(entry.metadata.get("members"))
                    attempts: list[OutboundLeg] = []
                    for member in members:
                        # Forwarding away from HA must not ring HA again, and a
                        # caller must never be invited back into its own call.
                        if _is_ha_target(member) or _caller_matches_member(
                            invite.caller, invite.source_host, member, peers
                        ):
                            continue
                        if len(attempts) >= MAX_RING_GROUP_ATTEMPTS:
                            break
                        attempt = _prepare_outbound_leg(
                            member=member,
                            peers=peers,
                            roster_entries=roster_entries,
                            local_name=invite.caller or _ha_peer_name(hass),
                            local_rtp_port_index=1,
                        )
                        if attempt is not None:
                            attempts.append(attempt)
                    if not attempts:
                        raise RuntimeError("ring group has no reachable non-HA members")
                    group_ringing_published = False

                    async def _dial_group_member(
                        attempt: OutboundLeg,
                    ) -> tuple[str, OutboundLeg]:
                        nonlocal group_ringing_published
                        result = await attempt.client.invite(
                            target=attempt.uri.user or attempt.member,
                            remote_host=attempt.uri.host,
                            remote_sip_port=attempt.uri.port or int(cfg["sip_port"]),
                            request_uri=str(attempt.uri),
                            timeout=8.0,
                        )
                        if result == "ringing":
                            if not group_ringing_published:
                                group_ringing_published = True
                                _set_sip_bridge_call_state(
                                    hass,
                                    CallState.REMOTE_RINGING.value,
                                    caller=invite.caller,
                                    callee=entry.display_name,
                                    peer_name=entry.display_name,
                                    call_id=call_id,
                                    direction="incoming",
                                    route_source="automation",
                                    route_kind=GROUP_TYPE_RING,
                                    last_sip_event="SIP_RESPONSE",
                                )
                            result = await attempt.client.wait_for_final(
                                timeout=RING_GROUP_TIMEOUT_S
                            )
                        return result, attempt

                    tasks = [
                        asyncio.create_task(_dial_group_member(attempt))
                        for attempt in attempts
                    ]
                    winner: OutboundLeg | None = None
                    failure = "timeout"
                    try:
                        deadline = (
                            asyncio.get_running_loop().time() + RING_GROUP_TIMEOUT_S
                        )
                        pending_tasks = set(tasks)
                        while pending_tasks and winner is None:
                            timeout = max(
                                0.0, deadline - asyncio.get_running_loop().time()
                            )
                            if timeout <= 0:
                                break
                            done, pending_tasks = await asyncio.wait(
                                pending_tasks,
                                timeout=timeout,
                                return_when=asyncio.FIRST_COMPLETED,
                            )
                            if not done:
                                break
                            for task in tasks:
                                if task not in done:
                                    continue
                                try:
                                    result, attempt = task.result()
                                except Exception as err:  # noqa: BLE001
                                    failure = str(err or failure)
                                    continue
                                if (
                                    result == "in_call"
                                    and attempt.client.dialog is not None
                                ):
                                    winner = attempt
                                    break
                                failure = result or failure
                    except asyncio.CancelledError:
                        for task in tasks:
                            if not task.done():
                                task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)
                        await asyncio.gather(
                            *(
                                _close_outbound_leg(attempt, bye_or_cancel=True)
                                for attempt in attempts
                            ),
                            return_exceptions=True,
                        )
                        raise
                    finally:
                        for task in tasks:
                            if not task.done():
                                task.cancel()
                        await asyncio.gather(*tasks, return_exceptions=True)

                    losers = [attempt for attempt in attempts if attempt is not winner]
                    await asyncio.gather(
                        *(
                            _close_outbound_leg(attempt, cancel=True)
                            for attempt in losers
                        ),
                        return_exceptions=True,
                    )
                    if winner is None:
                        raise RuntimeError(failure)

                    client = winner.client
                    dest_call_id = client.dialog_ids.call_id
                    dest_relay_port = winner.ports.ports[1]
                    registry.register_bridge(
                        source_call_id=call_id,
                        dest_call_id=dest_call_id,
                        client=client,
                        state=CallState.CONNECTING.value,
                        caller=invite.caller,
                        callee=entry.display_name,
                        route_kind=GROUP_TYPE_RING,
                        source_role="trunk" if preanswered is not None else "caller",
                        source_state=(
                            CallState.IN_CALL.value
                            if preanswered is not None
                            else CallState.CONNECTING.value
                        ),
                        dest_state=CallState.IN_CALL.value,
                    )

                    source_ports = reservation.ports
                    winner_ports = winner.ports.ports

                    def _release_group_ports(_ports) -> None:
                        _release_sip_rtp_port_pair(hass, source_ports)
                        _release_sip_rtp_port_pair(hass, winner_ports)

                    relay = build_invite_client_relay(
                        invite=invite,
                        client=client,
                        source_relay_port=source_relay_port,
                        dest_relay_port=dest_relay_port,
                        debug_capture=_debug_mode(hass),
                        on_release=_release_group_ports,
                    )
                    _attach_dtmf_event_bridge(
                        hass,
                        relay,
                        call_id=call_id,
                        dest_call_id=dest_call_id,
                        caller=invite.caller,
                        callee=winner.member,
                        client=client,
                    )
                    try:
                        await relay.start()
                    except Exception:
                        registry.bridge_clients.pop(call_id, None)
                        registry.sip_clients.pop(dest_call_id, None)
                        registry.client_watchers.pop(dest_call_id, None)
                        registry.remove_leg(call_id, dest_call_id)
                        await _close_outbound_leg(winner, bye_or_cancel=True)
                        raise
                    reservation.detach()
                    winner.ports.detach()
                    registry.relays[call_id] = relay
                    registry.pending_invites.pop(call_id, None)
                    registry.preanswered.pop(call_id, None)
                    if preanswered is None:
                        answer = build_answer_directional(
                            local_ip,
                            local_ip,
                            source_relay_port,
                            invite.send_format,
                            invite.recv_format,
                            dtmf=_invite_dtmf_format(invite),
                        )
                        _sip_send_final_response(
                            hass, call_id, 200, "OK", answer_sdp=answer
                        )
                    connected_party = str(winner.member or "").strip()
                    _set_sip_bridge_call_state(
                        hass,
                        CallState.IN_CALL.value,
                        caller=invite.caller,
                        callee=entry.display_name,
                        peer_name=connected_party,
                        call_id=call_id,
                        dest_call_id=dest_call_id,
                        dialed_target=entry.display_name,
                        connected_party=connected_party,
                        answered_by=connected_party,
                        direction="incoming",
                        route_source="automation",
                        route_kind=GROUP_TYPE_RING,
                        sip_status_code=200,
                        last_sip_event="SIP_RESPONSE",
                        sip_uri=str(winner.uri),
                    )
                    current_task = asyncio.current_task()
                    if current_task is not None:
                        registry.client_watchers[dest_call_id] = current_task
                    terminal = await client.wait_for_dialog_termination()
                    terminal_reason = (
                        TerminalReason.REMOTE_HANGUP.value
                        if terminal == "remote_hangup"
                        else _sip_terminal_reason(terminal, _sip_public_state(terminal))
                    )
                    await _terminate_sip_bridge(
                        hass, dest_call_id, terminal_reason=terminal_reason
                    )
                    return

                if decision.action is RouteAction.ASSIST:
                    current = registry.sessions.get(registry.resolve_session_id(call_id))
                    if current is not None:
                        claimed_assist = registry.transition(
                            call_id,
                            state=CallState.CONNECTING.value,
                            owner="assist",
                            callee=destination,
                            expected_revision=current.revision,
                            expected_owner=current.owner,
                        )
                        if claimed_assist is None:
                            raise RuntimeError("Assist route ownership changed")
                    await _start_local_assist_bridge(
                        invite,
                        reservation=reservation,
                        local_rtp_port=source_relay_port,
                        roster_entries=roster_entries,
                        source="trunk" if preanswered is not None else "sip",
                        called_extension=str(
                            (decision.entry.extension if decision.entry is not None else "")
                            or destination
                        ),
                        release_reservation_on_failure=preanswered is None,
                    )
                    if preanswered is None:
                        answer = build_answer_directional(
                            local_ip,
                            local_ip,
                            source_relay_port,
                            invite.send_format,
                            invite.recv_format,
                        )
                        _sip_send_final_response(
                            hass,
                            call_id,
                            200,
                            "OK",
                            answer_sdp=answer,
                        )
                    registry.pending_invites.pop(call_id, None)
                    registry.preanswered.pop(call_id, None)
                    current = registry.sessions.get(registry.resolve_session_id(call_id))
                    if current is not None:
                        registry.transition(
                            call_id,
                            state=CallState.IN_CALL.value,
                            owner="assist",
                            callee=destination,
                            expected_revision=current.revision,
                            expected_owner=current.owner,
                        )
                    return

                bridge_to_trunk = decision.action is RouteAction.TRUNK
                bridge_uri = None
                peer_target = _peer_for_target(decision.target or destination, peers)
                if bridge_to_trunk:
                    trunk_cfg = _get_trunk_config(hass)
                    bridge_uri = parse_sip_uri(
                        f"sip:{decision.target or destination}@{trunk_cfg[CONF_TRUNK_SERVER]}:"
                        f"{int(trunk_cfg[CONF_TRUNK_PORT])};"
                        f"transport={str(trunk_cfg[CONF_TRUNK_TRANSPORT]).lower()}"
                    )
                else:
                    bridge_uri, _peer, member_entry = _sip_uri_for_member(
                        decision.target or destination,
                        peers,
                        roster_entries,
                    )
                    if bridge_uri is None and decision.sip_uri:
                        bridge_uri = parse_sip_uri(decision.sip_uri)
                    if bridge_uri is None:
                        raise RuntimeError(f"destination {destination} has no reachable SIP URI")

                remote_tx_formats = _peer_audio_formats(peer_target, "tx_formats") or _roster_entry_formats(
                    decision.entry,
                    "tx_formats",
                )
                remote_rx_formats = _peer_audio_formats(peer_target, "rx_formats") or _roster_entry_formats(
                    decision.entry,
                    "rx_formats",
                )
                sip_send_formats, sip_recv_formats = _sip_target_audio_profile(
                    remote_tx_formats=remote_tx_formats,
                    remote_rx_formats=remote_rx_formats,
                    target=decision.target or destination,
                )
                bridge_to_registered = bool(
                    decision.entry is not None
                    and decision.entry.sip_uri
                    and decision.entry.metadata.get("registered")
                )
                if bridge_to_trunk or bridge_to_registered:
                    sip_send_formats = list(HA_TRUNK_AUDIO_FORMATS)
                    sip_recv_formats = list(HA_TRUNK_AUDIO_FORMATS)
                trunk_cfg = _get_trunk_config(hass)
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
                    signaling_transport=_sip_uri_transport(bridge_uri),
                    auth_username=str(trunk_cfg.get(CONF_TRUNK_AUTH_USERNAME) or "") if bridge_to_trunk else "",
                    username=str(trunk_cfg.get(CONF_TRUNK_USERNAME) or "") if bridge_to_trunk else "",
                    password=str(trunk_cfg.get(CONF_TRUNK_PASSWORD) or "") if bridge_to_trunk else "",
                    outbound_proxy=str(trunk_cfg.get(CONF_TRUNK_OUTBOUND_PROXY) or "") if bridge_to_trunk else "",
                    include_common_codecs=bridge_to_trunk or bridge_to_registered,
                )
                if not bridge_to_trunk:
                    _enable_reused_sip_tcp_connection(
                        hass,
                        client,
                        bridge_uri,
                        target=decision.target or destination,
                        default_sip_port=int(cfg["sip_port"]),
                    )
                result = await client.invite(
                    target=bridge_uri.user,
                    remote_host=bridge_uri.host,
                    remote_sip_port=bridge_uri.port or int(cfg["sip_port"]),
                    request_uri=str(bridge_uri),
                    timeout=SIP_TIMER_B if bridge_to_trunk else 8.0,
                )
                bucket = hass.data.setdefault(DOMAIN, {})
                if call_id in bucket.get("trunk_closed_calls", set()):
                    bucket["trunk_closed_calls"].discard(call_id)
                    raise RuntimeError(TerminalReason.CANCELLED.value)
                if result not in {"ringing", "in_call"}:
                    raise RuntimeError(result)

                dest_call_id = client.dialog_ids.call_id
                registry.register_bridge(
                    source_call_id=call_id,
                    dest_call_id=dest_call_id,
                    client=client,
                    state=CallState.REMOTE_RINGING.value if result == "ringing" else CallState.CONNECTING.value,
                    caller=invite.caller,
                    callee=destination,
                    route_kind=decision.action.value,
                    source_role="trunk" if preanswered is not None else "caller",
                    source_state=CallState.IN_CALL.value if preanswered is not None else CallState.CONNECTING.value,
                    dest_state=result,
                )
                current_task = asyncio.current_task()
                if current_task is not None:
                    registry.client_watchers[dest_call_id] = current_task
                if result == "ringing":
                    _set_sip_bridge_call_state(
                        hass,
                        CallState.REMOTE_RINGING.value,
                        caller=invite.caller,
                        callee=destination,
                        peer_name=destination,
                        call_id=call_id,
                        dest_call_id=dest_call_id,
                        direction="incoming",
                        route_source="automation",
                        last_sip_event="SIP_RESPONSE",
                    )
                    result = await client.wait_for_final()
                if result != "in_call" or client.dialog is None:
                    raise RuntimeError(result)

                relay = build_invite_client_relay(
                    invite=invite,
                    client=client,
                    source_relay_port=source_relay_port,
                    dest_relay_port=dest_relay_port,
                    debug_capture=_debug_mode(hass),
                    on_release=lambda ports: _release_sip_rtp_port_pair(hass, ports),
                )
                _attach_dtmf_event_bridge(
                    hass,
                    relay,
                    call_id=call_id,
                    dest_call_id=dest_call_id,
                    caller=invite.caller,
                    callee=destination,
                    client=client,
                )
                await relay.start()
                reservation.detach()
                registry.relays[call_id] = relay
                registry.pending_invites.pop(call_id, None)
                registry.preanswered.pop(call_id, None)
                if preanswered is None:
                    answer = build_answer_directional(
                        local_ip,
                        local_ip,
                        source_relay_port,
                        invite.send_format,
                        invite.recv_format,
                        dtmf=_invite_dtmf_format(invite),
                    )
                    _sip_send_final_response(hass, call_id, 200, "OK", answer_sdp=answer)
                registry.upsert(
                    call_id,
                    state=CallState.IN_CALL.value,
                    owner="bridge",
                    caller=invite.caller,
                    callee=destination,
                    route_kind=decision.action.value,
                )
                _set_sip_bridge_call_state(
                    hass,
                    CallState.IN_CALL.value,
                    caller=invite.caller,
                    callee=destination,
                    peer_name=destination,
                    call_id=call_id,
                    dest_call_id=dest_call_id,
                    direction="incoming",
                    route_source="automation",
                    answered_by=destination,
                    selected_tx_format=invite.send_format.audio_format.wire_token(),
                    selected_rx_format=invite.recv_format.audio_format.wire_token(),
                    selected_tx_rtp_format=invite.send_format.wire_token(),
                    selected_rx_rtp_format=invite.recv_format.wire_token(),
                    sip_status_code=200,
                    last_sip_event="SIP_RESPONSE",
                    route_kind=decision.action.value,
                    sip_uri=str(bridge_uri),
                )
                terminal = await client.wait_for_dialog_termination()
                terminal_reason = (
                    TerminalReason.REMOTE_HANGUP.value
                    if terminal == "remote_hangup"
                    else _sip_terminal_reason(terminal, _sip_public_state(terminal))
                )
                await _terminate_sip_bridge(
                    hass,
                    dest_call_id,
                    terminal_reason=terminal_reason,
                )
            except asyncio.CancelledError:
                if dest_call_id:
                    registry.bridge_clients.pop(call_id, None)
                    registry.sip_clients.pop(dest_call_id, None)
                    registry.client_watchers.pop(dest_call_id, None)
                    registry.remove_leg(call_id, dest_call_id)
                if client is not None:
                    with contextlib.suppress(Exception):
                        await client.terminate()
                    with contextlib.suppress(Exception):
                        await client.close()
                if reservation is not None and not reservation_from_preanswer:
                    reservation.release()
                raise
            except Exception as err:  # noqa: BLE001 - convert route failures to policy.
                reason = str(err or TerminalReason.TRANSPORT_UNREACHABLE.value)
                _LOGGER.info(
                    "SIP automation forward failed call_id=%s destination=%s reason=%s",
                    call_id,
                    destination,
                    reason,
                )
                if dest_call_id:
                    registry.bridge_clients.pop(call_id, None)
                    registry.sip_clients.pop(dest_call_id, None)
                    registry.client_watchers.pop(dest_call_id, None)
                    registry.remove_leg(call_id, dest_call_id)
                if client is not None:
                    with contextlib.suppress(Exception):
                        await client.terminate()
                    with contextlib.suppress(Exception):
                        await client.close()
                if reservation is not None and not reservation_from_preanswer:
                    reservation.release()
                await _restore_or_terminate(reason)
            finally:
                forward_tasks.pop(call_id, None)
                forward_claims.discard(call_id)

        task = create_runtime_task(hass, _run_forward())
        forward_tasks[call_id] = task

    async def _run_ring_group_call(
        invite: SipInvite,
        entry: RosterEntry,
        peers: list[Peer],
        roster_entries: list[RosterEntry],
    ) -> None:
        registry = _call_registry(hass)
        members = _unique_group_members(entry.metadata.get("members"))
        attempts: list[OutboundLeg] = []
        ha_member = False
        for member in members:
            if _caller_matches_member(invite.caller, invite.source_host, member, peers):
                continue
            if _is_ha_target(member):
                ha_member = True
                continue
            if len(attempts) >= MAX_RING_GROUP_ATTEMPTS:
                _LOGGER.warning(
                    "SIP ring group %s has more than %d dialable members; excess members were skipped",
                    entry.display_name,
                    MAX_RING_GROUP_ATTEMPTS,
                )
                break
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
            registry.finish_and_pop(
                invite.call_id,
                reason=TerminalReason.TRANSPORT_UNREACHABLE.value,
                state=CallState.TRANSPORT_UNREACHABLE.value,
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

        tasks = [asyncio.create_task(_dial(attempt)) for attempt in attempts]
        if ha_member:
            tasks.append(asyncio.create_task(_wait_ha()))
        else:
            tasks.append(asyncio.create_task(_wait_caller_cancel()))
        winner: OutboundLeg | dict | None = None
        ha_winner = False
        final_result = "timeout"
        failure_priority = {
            "ignored": -1,
            "timeout": 0,
            "transport_unreachable": 1,
            "auth_required_unsupported": 2,
            "proxy_auth_required_unsupported": 2,
            "media_incompatible": 3,
            "busy": 4,
            "declined": 5,
            "dnd": 5,
            "cancelled": 6,
        }
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
                completed = []
                # asyncio.wait() returns a set. Preserve configured member order
                # so simultaneous answers never depend on hash iteration order.
                for task in tasks:
                    if task not in done:
                        continue
                    try:
                        completed.append(task.result())
                    except Exception as err:
                        _LOGGER.debug("SIP ring group member task failed: %s", err)

                # A local/caller cancellation wins even if an outbound answer
                # became ready in the same event-loop turn.
                control_cancel = next(
                    (
                        (result, attempt)
                        for result, attempt in completed
                        if result == "cancelled"
                        and isinstance(attempt, dict)
                        and (attempt.get("caller_control") or attempt.get("ha"))
                    ),
                    None,
                )
                if control_cancel is not None:
                    final_result = control_cancel[0]
                    pending_tasks.clear()
                    break

                for result, attempt in completed:
                    if isinstance(attempt, OutboundLeg) and result == "in_call" and attempt.client.dialog is not None:
                        winner = attempt
                        break
                    if result == "in_call_ha":
                        winner = attempt
                        ha_winner = True
                        break
                    if failure_priority.get(result, 1) > failure_priority.get(final_result, 0):
                        final_result = result
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            losers = [attempt for attempt in attempts if attempt is not winner]
            await asyncio.gather(
                *(_close_outbound_leg(attempt, cancel=True) for attempt in losers),
                return_exceptions=True,
            )
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
                registry.finish_and_pop(
                    invite.call_id,
                    reason=terminal_reason,
                    state=public_state,
                )
                return
            if ha_winner:
                _pending_routes(hass).pop(invite.call_id, None)
                connected_party = _ha_peer_name(hass)
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
                    owner="ha_softphone",
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
                # Mirror the same established-call contract used when a SIP
                # endpoint wins: retain the group as dialed target and expose
                # the HA softphone as the party that actually answered.
                _set_sip_bridge_call_state(
                    hass,
                    CallState.IN_CALL.value,
                    caller=invite.caller,
                    callee=entry.display_name,
                    peer_name=connected_party,
                    call_id=invite.call_id,
                    dialed_target=entry.display_name,
                    connected_party=connected_party,
                    answered_by=connected_party,
                    selected_tx_format=invite.send_format.audio_format.wire_token(),
                    selected_rx_format=invite.recv_format.audio_format.wire_token(),
                    selected_tx_rtp_format=invite.send_format.wire_token(),
                    selected_rx_rtp_format=invite.recv_format.wire_token(),
                    sip_status_code=200,
                    last_sip_event="SIP_RESPONSE",
                    route_kind=GROUP_TYPE_RING,
                )
                return
            _pending_routes(hass).pop(invite.call_id, None)
            if not isinstance(winner, OutboundLeg):
                _LOGGER.error("SIP ring group selected an invalid winner for call_id=%s", invite.call_id)
                _sip_send_final_response(
                    hass,
                    invite.call_id,
                    500,
                    "Server Internal Error",
                    decline_reason=TerminalReason.PROTOCOL_ERROR.value,
                )
                registry.finish_and_pop(
                    invite.call_id,
                    reason=TerminalReason.PROTOCOL_ERROR.value,
                    state=CallState.TRANSPORT_UNREACHABLE.value,
                )
                return
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
                _attach_dtmf_event_bridge(
                    hass,
                    relay,
                    call_id=invite.call_id,
                    dest_call_id=client.dialog_ids.call_id,
                    caller=invite.caller,
                    callee=str(winner.member or invite.target),
                    client=client,
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
            ha_origin = _is_ha_target(invite.caller)
            if ha_origin:
                # The synthetic HA caller has no SIP/RTP socket of its own.
                # Feed the already-running source side of the relay from the
                # authenticated browser websocket via a local UDP endpoint.
                registry.softphone_media[invite.call_id] = {
                    "rtp_loopback": True,
                    "remote_rtp_host": local_ip,
                    "remote_rtp_port": source_relay_port,
                    "send_format": invite.recv_format,
                    "recv_format": invite.send_format,
                    "local_ssrc": secrets.randbelow(0xFFFFFFFF) + 1,
                }
            registry.upsert(
                invite.call_id,
                state=CallState.IN_CALL.value,
                owner="ha_softphone",
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
                dtmf=_invite_dtmf_format(invite),
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
            if ha_origin:
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
                    selected_tx_format=invite.recv_format.audio_format.wire_token(),
                    selected_rx_format=invite.send_format.audio_format.wire_token(),
                    selected_tx_rtp_format=invite.recv_format.wire_token(),
                    selected_rx_rtp_format=invite.send_format.wire_token(),
                    audio_mode="full_duplex",
                    route_kind=GROUP_TYPE_RING,
                    sip_status_code=200,
                    last_sip_event="SIP_RESPONSE",
                    sip_uri=str(winner.uri),
                )
            current_task = asyncio.current_task()
            if current_task is not None:
                registry.client_watchers[client.dialog_ids.call_id] = current_task
            terminal = await client.wait_for_dialog_termination()
            terminal_reason = (
                TerminalReason.REMOTE_HANGUP.value
                if terminal == "remote_hangup"
                else _sip_terminal_reason(terminal, _sip_public_state(terminal))
            )
            await _terminate_sip_bridge(hass, client.dialog_ids.call_id, terminal_reason=terminal_reason)
        except asyncio.CancelledError:
            _pending_routes(hass).pop(invite.call_id, None)
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            await asyncio.gather(
                *(_close_outbound_leg(attempt, bye_or_cancel=True) for attempt in attempts),
                return_exceptions=True,
            )
            registry.finish_and_pop(
                invite.call_id,
                reason=TerminalReason.CANCELLED.value,
                state=CallState.CANCELLED.value,
            )
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
        room = manager.rooms.get(str(room_name or "").strip())
        available_legs = max(0, MAX_CONFERENCE_LEGS - (len(room.legs) if room is not None and not room._closed else 0))
        members = _unique_group_members(entry.metadata.get("ring_members"))
        attempts: list[OutboundLeg] = []
        for member in members:
            if _caller_matches_member(caller, source_host, member, peers) or _is_ha_target(member):
                continue
            if len(attempts) >= available_legs:
                _LOGGER.warning(
                    "SIP conference %s has no capacity for additional ring members; excess members were skipped",
                    room_name,
                )
                break
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
                raise
            except Exception as err:
                _LOGGER.debug("SIP conference member invite failed member=%s: %s", attempt.member, err)
            finally:
                if not owned_by_room:
                    with contextlib.suppress(Exception):
                        await _close_outbound_leg(attempt, bye_or_cancel=True)

        await asyncio.gather(*(_dial(attempt) for attempt in attempts), return_exceptions=True)

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

    async def _start_ring_group_from_ha(entry: RosterEntry) -> str:
        group_name = str(entry.name or entry.id or "")
        call_id = f"ha-{int(time.time() * 1000):x}"
        send_format = next(
            fmt
            for fmt in HA_SIP_PCM_TX_FORMATS
            if fmt.channels == 1 and fmt.nominal_frame_bytes <= 1200
        )
        recv_format = next(
            fmt
            for fmt in HA_SIP_PCM_RX_FORMATS
            if fmt.channels == 1 and fmt.nominal_frame_bytes <= 1200
        )
        invite = SipInvite(
            source_host=local_ip,
            source_port=int(cfg["sip_port"]),
            request_uri=parse_sip_uri(f"sip:{group_name.replace(' ', '_')}@{local_ip};transport=tcp"),
            caller_uri=parse_sip_uri(f"sip:{_ha_peer_name(hass).replace(' ', '_')}@{local_ip};transport=tcp"),
            target=group_name,
            caller=_ha_peer_name(hass),
            call_id=call_id,
            cseq="1 INVITE",
            remote_sdp=b"",
            send_format=sip_sdp.audio_format_to_rtp(send_format, 96),
            recv_format=sip_sdp.audio_format_to_rtp(recv_format, 96),
            remote_rtp_host=local_ip,
            remote_rtp_port=0,
        )
        registry = _call_registry(hass)
        registry.upsert(
            call_id,
            state=CallState.RINGING.value,
            owner="ha_softphone",
            caller=_ha_peer_name(hass),
            callee=group_name,
            route_kind=GROUP_TYPE_RING,
        )
        registry.add_leg(call_id, call_id, role="ha_softphone", state=CallState.REMOTE_RINGING.value)
        _set_ha_softphone_call_state(
            hass,
            CallState.REMOTE_RINGING.value,
            session_device_id=HA_SOFTPHONE_DEVICE_ID,
            caller=_ha_peer_name(hass),
            callee=group_name,
            peer_name=group_name,
            direction="outgoing",
            call_id=call_id,
            route_kind=GROUP_TYPE_RING,
            sip_status_code=180,
            last_sip_event="LOCAL_RING_GROUP",
        )
        try:
            peers = await _async_build_peer_snapshot(hass)
            roster_entries = _roster_from_peers(hass, peers, _registered_roster_entries(hass))
        except Exception:
            registry.finish_and_pop(
                call_id,
                reason=TerminalReason.TRANSPORT_UNREACHABLE.value,
                state=CallState.TRANSPORT_UNREACHABLE.value,
            )
            _set_ha_softphone_call_state(
                hass,
                CallState.TRANSPORT_UNREACHABLE.value,
                call_id=call_id,
                caller=_ha_peer_name(hass),
                callee=group_name,
                peer_name=group_name,
                direction="outgoing",
                reason=TerminalReason.TRANSPORT_UNREACHABLE.value,
                route_kind=GROUP_TYPE_RING,
                last_sip_event="PEER_SNAPSHOT_FAILED",
            )
            raise
        create_runtime_task(hass, _run_ring_group_call(invite, entry, peers, roster_entries))
        return call_id

    hass.data.setdefault(DOMAIN, {})["async_ring_conference_members"] = _ring_conference_members_from_ha
    hass.data.setdefault(DOMAIN, {})["async_start_ring_group_from_ha"] = _start_ring_group_from_ha

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
        if decision.action is RouteAction.ASSIST:
            # The configured Assist extension is a public local dial-plan
            # destination, so callers do not need a registrar account.
            caller_is_registered_endpoint = True
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
        if decision.action is RouteAction.ASSIST and any(
            session.route_kind == RouteAction.ASSIST.value and session.state not in TERMINAL_STATES
            for session in registry.sessions.values()
        ):
            return SipInviteResult(486, "Busy Here", to_tag="", decline_reason=TerminalReason.BUSY.value)
        if decision.action is RouteAction.ASSIST:
            try:
                assist_ports = RtpPortReservation.allocate(hass)
            except RuntimeError as err:
                _LOGGER.warning("Assist RTP port allocation failed: %s", err)
                return SipInviteResult(503, "Service Unavailable", to_tag="")
            assist_rtp_port = assist_ports.ports[0]
            try:
                await _start_local_assist_bridge(
                    invite,
                    reservation=assist_ports,
                    local_rtp_port=assist_rtp_port,
                    roster_entries=roster_entries,
                    source="sip",
                    called_extension=str(decision.entry.extension or invite.target) if decision.entry is not None else invite.target,
                )
            except Exception:
                _LOGGER.exception("Assist bridge failed call_id=%s", invite.call_id)
                assist_ports.release()
                return SipInviteResult(
                    500,
                    "Server Internal Error",
                    to_tag="",
                    decline_reason=TerminalReason.PROTOCOL_ERROR.value,
                )
            answer = build_answer_directional(
                local_ip,
                local_ip,
                assist_rtp_port,
                invite.send_format,
                invite.recv_format,
            )
            return SipInviteResult(200, "OK", answer_sdp=answer, to_tag="")
        if decision.action is RouteAction.GROUP:
            group_type = str((decision.entry.metadata or {}).get("group_type") or "") if decision.entry is not None else ""
            if group_type == GROUP_TYPE_CONFERENCE:
                ring_members = [str(member).strip() for member in ((decision.entry.metadata or {}).get("ring_members") or [])]
                ring_ha = any(_is_ha_target(member) for member in ring_members)
                result = await conference_manager(hass, local_ip=local_ip).join(invite, decision.entry, ring_ha=ring_ha)
                if result.status == 200:
                    registry.upsert(
                        invite.call_id,
                        state=CallState.IN_CALL.value,
                        owner="bridge",
                        caller=invite.caller,
                        callee=invite.target,
                        route_kind=GROUP_TYPE_CONFERENCE,
                    )
                    registry.add_leg(
                        invite.call_id,
                        invite.call_id,
                        role="caller",
                        state=CallState.IN_CALL.value,
                    )
                    create_runtime_task(
                        hass,
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
                    owner="router",
                    caller=invite.caller,
                    callee=invite.target,
                    route_kind=GROUP_TYPE_RING,
                )
                registry.add_leg(invite.call_id, invite.call_id, role="caller", state=CallState.RINGING.value)
                create_runtime_task(
                    hass,
                    _run_ring_group_call(invite, decision.entry, peers, roster_entries),
                )
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
                        source_host=invite.source_host,
                    ),
                    roster_entries,
                    trunk_ready=False,
                )
                # Continue into the normal route-decision path so HA
                # automations can still forward/bridge/decline the call
                # before the default HA softphone ringing response.
                source_relay_port = 0
                dest_relay_port = 0
            else:
                # Clear a theoretically reused Call-ID before handing control
                # back to the event loop. A BYE received after this point must
                # remain visible to the background DTMF/router task.
                bucket.setdefault("trunk_closed_calls", set()).discard(invite.call_id)
                bucket.setdefault("trunk_info_queues", {})[invite.call_id] = asyncio.Queue(
                    maxsize=MAX_TRUNK_INFO_DIGITS
                )
                try:
                    bridge_ports = RtpPortReservation.allocate(hass)
                except RuntimeError as err:
                    _LOGGER.warning("SIP trunk RTP bridge port allocation failed: %s", err)
                    return SipInviteResult(503, "Service Unavailable", to_tag="")
                source_relay_port, _dest_relay_port = bridge_ports.ports
                registry.pending_invites[invite.call_id] = invite
                registry.preanswered[invite.call_id] = {
                    "local_rtp_port": source_relay_port,
                    "rtp_reservation": bridge_ports,
                }
                registry.upsert(
                    invite.call_id,
                    state=CallState.CONNECTING.value,
                    owner="router",
                    caller=invite.caller,
                    callee=str(
                        trunk_cfg.get(CONF_TRUNK_INBOUND_DEFAULT_TARGET) or "HA"
                    ),
                    route_kind="trunk",
                )
                route_future = asyncio.get_running_loop().create_future()
                expires_at = time.time() + (float(dtmf_timeout_ms) / 1000.0)
                route_bucket[invite.call_id] = {
                    "future": route_future,
                    "invite": invite,
                    "decision": decision,
                    "created_at": time.time(),
                    "expires_at": expires_at,
                }
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
                    direction="incoming",
                    scope="sip_trunk",
                    phase="dtmf_route",
                    source_host=invite.source_host,
                    expires_at=expires_at,
                    decision_timeout_ms=dtmf_timeout_ms,
                )
                create_runtime_task(
                    hass,
                    _run_trunk_inbound_route(
                        invite,
                        bridge_ports=bridge_ports,
                        route_future=route_future,
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
                CallState.CONNECTING.value,
                caller=invite.caller,
                callee=invite.target,
                peer_name=invite.caller,
                local_name=_ha_peer_name(hass),
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
                direction="incoming",
                route_request=True,
                phase="route_decision",
                source_host=invite.source_host,
                target=decision.target,
                default_destination=decision.target,
                expires_at=expires_at,
                decision_timeout_ms=int(SIP_ROUTE_DECISION_TIMEOUT * 1000),
                rtp_format=(
                    f"{invite.selected_format.encoding}/"
                    f"{invite.selected_format.sample_rate}/"
                    f"{invite.selected_format.channels}"
                ),
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
            RouteAction.ASSIST,
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
            if (
                peer_target is not None
                and peer_target.host
                and invite.source_host
                and peer_target.host == invite.source_host
            ):
                _set_sip_bridge_call_state(
                    hass,
                    CallState.BUSY.value,
                    caller=invite.caller,
                    callee=invite.target,
                    peer_name=invite.caller,
                    call_id=invite.call_id,
                    reason=TerminalReason.BUSY.value,
                    origin="self",
                    sip_status_code=486,
                    last_sip_event="SIP_RESPONSE",
                )
                return SipInviteResult(486, "Busy Here", to_tag="", decline_reason=TerminalReason.BUSY.value)
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
                    await _close_client_and_release(client, bridge_ports, bye=True)
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
                        _attach_dtmf_event_bridge(
                            hass,
                            relay,
                            call_id=invite.call_id,
                            dest_call_id=client.dialog_ids.call_id,
                            caller=invite.caller,
                            callee=(
                                decision.entry.display_name
                                if decision.entry is not None
                                else decision.target or invite.target
                            ),
                            client=client,
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
                        owner="bridge",
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
                        dtmf=_invite_dtmf_format(invite),
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
            owner="ha_softphone",
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
        forward_task = bucket.setdefault("forward_tasks", {}).get(call_id)
        if forward_task is not None and forward_task is not asyncio.current_task():
            forward_task.cancel()
            await asyncio.gather(forward_task, return_exceptions=True)
        bucket.setdefault("trunk_info_queues", {}).pop(call_id, None)
        registry = _call_registry(hass)
        route = _pending_routes(hass).pop(call_id, None)
        closed_calls = bucket.setdefault("trunk_closed_calls", set())
        if len(closed_calls) >= 256:
            closed_calls.pop()
        closed_calls.add(call_id)
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
                target=event_callee,
                reason=terminal_reason,
                terminal_reason=terminal_reason,
                origin="remote",
                last_sip_event="BYE",
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
                client is not None,
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
        on_info=_on_info,
        udp_enabled=True,
        tcp_enabled=True,
    )
    if not await endpoint.start():
        return False
    hass.data[DOMAIN]["async_forward_call"] = _async_forward_existing_call
    hass.data[DOMAIN]["sip_endpoint"] = endpoint
    hass.data[DOMAIN]["sip_server"] = endpoint.udp_server
    hass.data[DOMAIN]["sip_tcp_server"] = endpoint.tcp_server
    _LOGGER.info("SIP endpoint enabled on UDP+TCP/%s (RTP base %s)", cfg["sip_port"], cfg["rtp_port"])
    return True
