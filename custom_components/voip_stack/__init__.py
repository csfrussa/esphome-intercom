"""VoIP Stack integration for Home Assistant.

HA is a SIP softphone and SIP B2BUA/router for ESPHome SIP phones. Public call
control is expressed in SIP/SDP/RTP terms only; logical targets are resolved by
the central phonebook and routed through HA as SIP dialogs when needed.
"""

import asyncio
from dataclasses import replace
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, EVENT_SERVICE_REGISTERED, Platform
from homeassistant.core import HomeAssistant, CoreState, Event, ServiceCall, callback
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import config_validation as cv

from .config import (
    entry_assist_config as _entry_assist_config,
    entry_transport_config as _entry_transport_config,
    entry_trunk_config as _entry_trunk_config,
    transport_config as _get_transport_config,
    trunk_config as _get_trunk_config,
    trunk_enabled as _trunk_enabled,
)
from .call_scope import (
    call_belongs_to_endpoint as _call_belongs_to_endpoint,
    endpoint_call_ids as _endpoint_call_ids,
    pending_routes as _pending_routes,
    single_pending_route_call_id as _single_pending_route_call_id,
)
from .const import (
    CONF_ASSIST_ENDPOINT_ENABLED,
    CONF_ASSIST_PIPELINE,
    CONF_ASSIST_INTENTS,
    CONF_DEBUG_MODE,
    CONF_EXPERIMENTAL_VIDEO,
    CONF_PHONEBOOK_CONTACTS,
    CONF_SIP_ACCOUNTS,
    CONF_VIDEO_CAMERA_SEND,
    CONF_AUTOMATION_ROUTING_ENABLED,
    CONF_TRUNK_DTMF_ENABLED,
    CONF_TRUNK_DTMF_TIMEOUT_MS,
    CONF_TRUNK_INBOUND_MODE,
    CONF_TRUNK_AUTH_USERNAME,
    CONF_TRUNK_OUTBOUND_PROXY,
    CONF_TRUNK_PASSWORD,
    CONF_TRUNK_PORT,
    CONF_TRUNK_SERVER,
    CONF_TRUNK_TRANSPORT,
    CONF_TRUNK_USERNAME,
    DOMAIN,
    HA_PEER_FALLBACK_NAME,
    HA_SOFTPHONE_DEVICE_ID,
    VOIP_STACK_RTP_PORT,
    VOIP_STACK_SIP_PORT,
    TRUNK_INBOUND_MODE_DIRECT,
    TRUNK_INBOUND_MODE_DTMF,
)
from .endpoint_lifecycle import call_registry as _call_registry, create_runtime_task
from .endpoint_registry import EndpointBusyError
from .endpoint_routing import (
    device_formats as _device_formats,
    roster_entry_formats as _roster_entry_formats,
    sip_target_audio_profile as _sip_target_audio_profile,
)
from .esphome_state_bridge import (
    register_state_event_bridge as _register_esp_state_event_bridge,
)
from .esphome_actions import (
    async_call_action as _call_esphome_action,
    async_press_device_button as _press_device_button,
    async_resolve_command_phone as _resolve_command_phone,
    async_resolve_source_device as _resolve_source_device_from_call,
    async_resolve_target_device as _resolve_target_device,
    has_action as _has_esphome_action,
)
from .fsm import (
    CallState,
    TerminalReason,
    sip_public_state as _sip_public_state,
    sip_terminal_reason as _sip_terminal_reason,
)
from .inbound_answer import AnswerTransaction
from .media_ports import (
    allocate_sip_rtp_port as _allocate_sip_rtp_port,
    release_media_reservation as _release_media_reservation,
    reserve_sip_video_media,
)
from .outbound_lifecycle import (
    HA_SOFTPHONE_ACTIVE_STATES,
    async_prepare_ha_outbound_call as _async_prepare_ha_outbound_call,
    async_track_outbound_sip_client as _track_outbound_sip_client,
)
from .session_cleanup import async_cleanup_sip_runtime
from .sip_runtime import (
    enable_reused_tcp_connection as _enable_reused_sip_tcp_connection,
    send_bye as _sip_send_bye,
    send_final_response as _sip_send_final_response,
    sip_servers as _sip_servers,
    uri_transport as _sip_uri_transport,
)
from .audio_format import HA_TRUNK_AUDIO_FORMATS
from .authorization import (
    async_require_service_admin,
)
from .peer_snapshot import (
    async_advertise_host as _ha_advertise_host,
)
from .phone_endpoint import (
    DEFAULT_ENDPOINT_ID,
    EndpointAvailability,
    EndpointKind,
    OfflinePolicy,
)
from .service_endpoints import (
    async_require_phone_service_control as _require_phone_service_control,
    browser_endpoint_name as _browser_endpoint_name,
    service_browser_endpoint as _service_browser_endpoint,
    service_configured_endpoint as _service_configured_endpoint,
)
from .phone_config import (
    async_ensure_phone_subentries,
    async_load_legacy_default_phone_overrides,
    async_setup_endpoint_registry,
    phone_subentries,
    restore_default_phone_subentry,
    sync_registry_from_entry,
)
from .phonebook_runtime import push_roster_json_to_esps as _push_roster_json_to_esps
from .router import (
    RouteAction,
    RouteReason,
    ha_uri_for,
    resolve_ha_router,
)
from .route_decisions import set_pending_route_decision as _set_pending_route_decision
from .store import (
    manual_roster_entries as _manual_roster_entries,
)
from .video_rtp import RtpSenderState
from .websocket_api import (
    async_register_websocket_api,
    _async_load_ha_softphone_store,
    _get_voip_devices,
    _fire_call_event,
    async_set_ha_softphone_settings,
    _ha_softphone_store,
    _publish_ha_softphone_state,
    _set_ha_softphone_call_state,
    _set_sip_bridge_call_state,
)

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.EVENT,
    Platform.SENSOR,
    Platform.SWITCH,
]
CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


async def async_migrate_entry(hass: HomeAssistant, config_entry: ConfigEntry) -> bool:
    """Migrate inbound routing options without changing existing behavior."""
    if config_entry.version < 2:
        data = dict(config_entry.data)
        raw_timeout = data.get(CONF_TRUNK_DTMF_TIMEOUT_MS, 3000)
        timeout_ms = int(raw_timeout or 0)
        if 0 <= timeout_ms <= 10:
            timeout_ms *= 1000
        legacy_dtmf = bool(data.get(CONF_TRUNK_DTMF_ENABLED, False)) and timeout_ms > 0
        mode = TRUNK_INBOUND_MODE_DTMF if legacy_dtmf else TRUNK_INBOUND_MODE_DIRECT
        data[CONF_TRUNK_INBOUND_MODE] = mode
        data[CONF_AUTOMATION_ROUTING_ENABLED] = False
        data[CONF_TRUNK_DTMF_ENABLED] = legacy_dtmf
        data[CONF_TRUNK_DTMF_TIMEOUT_MS] = timeout_ms
        hass.config_entries.async_update_entry(config_entry, data=data, version=2)
        _LOGGER.info("Migrated VoIP Stack inbound routing mode to %s", mode)
    if config_entry.version < 3:
        legacy_phone_data = await async_load_legacy_default_phone_overrides(
            hass,
            config_entry,
        )
        async_ensure_phone_subentries(
            hass,
            config_entry,
            default_overrides=legacy_phone_data,
        )
        hass.config_entries.async_update_entry(config_entry, version=3)
        _LOGGER.info("Migrated VoIP Stack phones to config subentries")
    return True


_LOGGER = logging.getLogger(__name__)
SIP_ROUTE_DECISION_TIMEOUT = 1.5
async def _mark_sip_account_unreachable(hass: HomeAssistant, username: str) -> None:
    """Drop a stale registrar Contact while keeping the account in the roster."""

    wanted = (username or "").strip().lower()
    if not wanted:
        return
    registrar = hass.data.get(DOMAIN, {}).get("sip_registrar")
    if registrar is None:
        return
    removed = False
    for key, registration in list(getattr(registrar, "registrations", {}).items()):
        reg_user = str(getattr(registration, "username", key) or key).strip().lower()
        if reg_user == wanted or str(key).strip().lower() == wanted:
            removed = True
    if removed:
        # Go through the registrar API so its observer also marks the logical
        # endpoint offline. Directly popping Contact state left HA's connected
        # binary sensor stale even though routing already returned 480.
        registrar.remove_registration(username)
        _LOGGER.info("SIP registrar contact marked unreachable user=%s", username)


def _ha_softphone_has_active_call(
    hass: HomeAssistant,
    *,
    endpoint_id: str = DEFAULT_ENDPOINT_ID,
    ignore_call_id: str = "",
) -> bool:
    store = _ha_softphone_store(hass, endpoint_id)
    if ignore_call_id and str(store.get("call_id") or "") == ignore_call_id:
        return False
    state = str(store.get("state") or CallState.IDLE.value)
    return bool(store.get("session_device_id") or state in HA_SOFTPHONE_ACTIVE_STATES)


def _ha_peer_name(hass: HomeAssistant) -> str:
    """Return the HA phonebook peer name.

    HA normally always has a configured location_name. The default is only for
    malformed/empty local config and avoids a hardcoded "Home Assistant" peer
    identity.
    """
    return (hass.config.location_name or "").strip() or HA_PEER_FALLBACK_NAME


async def _terminate_sip_bridge(
    hass: HomeAssistant,
    call_id: str,
    *,
    endpoint_id: str = DEFAULT_ENDPOINT_ID,
    session_device_id: str = HA_SOFTPHONE_DEVICE_ID,
    terminal_reason: str = TerminalReason.LOCAL_HANGUP.value,
) -> tuple[bool, str, str, bool, bool]:
    from .bridge_manager import async_terminate_sip_bridge

    softphone = _ha_softphone_store(hass, endpoint_id)
    softphone_call_id = str(softphone.get("call_id") or "")
    result = await async_terminate_sip_bridge(
        hass,
        call_id,
        terminal_reason=terminal_reason,
        send_bye=lambda source_call_id: _sip_send_bye(hass, source_call_id),
    )
    handled, source_call_id, _dest_call_id, _client_closed, _source_bye = result
    if handled and source_call_id == softphone_call_id:
        reason = terminal_reason or TerminalReason.LOCAL_HANGUP.value
        _set_ha_softphone_call_state(
            hass,
            CallState.IDLE.value,
            endpoint_id=endpoint_id,
            session_device_id=session_device_id,
            caller=str(softphone.get("caller") or ""),
            callee=str(softphone.get("callee") or ""),
            peer_name=str(softphone.get("peer_name") or ""),
            direction=str(softphone.get("direction") or ""),
            call_id=source_call_id,
            reason=reason,
            terminal_reason=reason,
            origin="self" if reason == TerminalReason.LOCAL_HANGUP.value else "remote",
            last_sip_event="SIP_BYE",
        )
    return result


def _bind_service_call_controller(
    registry,
    call_id: str,
    call: ServiceCall,
    *,
    endpoint_id: str = "",
) -> None:
    """Persist the initiating HA Context before publishing call events."""

    from homeassistant.exceptions import ServiceValidationError

    try:
        registry.bind_controller(
            call_id,
            context=getattr(call, "context", None),
            endpoint_id=endpoint_id,
        )
    except ValueError as err:
        raise ServiceValidationError(str(err)) from err


async def _handle_purge_devices_service(call: ServiceCall) -> None:
    """Remove stale VoIP devices."""
    from datetime import datetime, timedelta, timezone
    from homeassistant.helpers import device_registry as dr

    hass: HomeAssistant = call.hass
    device_registry = dr.async_get(hass)
    min_hours = float(call.data.get("min_unavailable_hours", 0) or 0)
    cutoff = datetime.now(timezone.utc) - timedelta(hours=min_hours) if min_hours > 0 else None

    targeted = await _resolve_target_device(hass, call)
    purged: list[str] = []

    if targeted:
        device_registry.async_remove_device(targeted["device_id"])
        purged.append(targeted["name"])
    else:
        devices = await _get_voip_devices(hass)
        for device in devices:
            entity_id = (device.get("entities") or {}).get("voip_state")
            if not entity_id:
                continue
            state = hass.states.get(entity_id)
            if state is None or state.state not in ("unavailable", "unknown"):
                continue
            if cutoff is not None and state.last_changed and state.last_changed > cutoff:
                continue
            device_registry.async_remove_device(device["device_id"])
            purged.append(device["name"])

    if purged:
        _LOGGER.info("Purged %d VoIP device(s): %s", len(purged), ", ".join(purged))
    else:
        _LOGGER.info("Purge: no stale VoIP devices to remove")


async def _handle_sip_answer_service(call: ServiceCall) -> None:
    hass: HomeAssistant = call.hass
    device = await _resolve_command_phone(hass, call)
    if device is not None:
        call_button = str((device.get("entities") or {}).get("call") or "").strip()
        await _require_phone_service_control(
            hass,
            call,
            device=device,
            action_entity_ids=(call_button,) if call_button else (),
        )
        # On ESP phones the Call button is the local answer control while ringing.
        if not await _press_device_button(
            hass,
            device,
            "call",
            "SIP answer",
            context=call.context,
        ):
            from homeassistant.exceptions import ServiceValidationError

            raise ServiceValidationError(f"{device.get('name') or 'ESP phone'} has no answer/call button")
        return
    endpoint_id, browser_endpoint = _service_browser_endpoint(hass, call, strict=True)
    await _require_phone_service_control(
        hass,
        call,
        endpoint=browser_endpoint,
    )
    local_name = _browser_endpoint_name(hass, endpoint_id, browser_endpoint)
    endpoint_device_id = str(
        getattr(browser_endpoint, "device_id", "") or HA_SOFTPHONE_DEVICE_ID
    )
    call_id = str(call.data.get("call_id") or "").strip()
    if not call_id:
        call_id = _single_pending_route_call_id(hass, endpoint_id) or str(
            _ha_softphone_store(hass, endpoint_id).get("call_id") or ""
        ).strip()
    registry = _call_registry(hass)
    if call_id and not _call_belongs_to_endpoint(registry, call_id, endpoint_id):
        from homeassistant.exceptions import ServiceValidationError

        raise ServiceValidationError(
            f"call_id {call_id} belongs to another phone endpoint"
        )
    if call_id and registry.resolve_session_id(call_id) in registry.sessions:
        _bind_service_call_controller(
            registry,
            call_id,
            call,
            endpoint_id=endpoint_id,
        )
    camera_send_requested = bool(
        _get_transport_config(hass).get(CONF_VIDEO_CAMERA_SEND, False)
    ) and bool(call.data.get("send_video", False))
    from .local_softphone_runtime import local_softphone_bridge

    local_bridge = local_softphone_bridge(hass)
    if local_bridge is not None and local_bridge.get_call(call_id) is not None:
        from homeassistant.exceptions import ServiceValidationError

        from .local_softphone_bridge import LocalBridgeError

        try:
            local_bridge.answer(
                call_id,
                endpoint_id,
                str(call.data.get("media_client_id") or ""),
                enable_video_send=camera_send_requested,
            )
        except LocalBridgeError as err:
            raise ServiceValidationError(str(err)) from err
        return
    bucket = hass.data.setdefault(DOMAIN, {})
    # A browser member answering a ring group resolves the pending route that
    # is owned by the forward task itself.  Handle that decision before the
    # generic forward guard; otherwise every legitimate group answer is
    # rejected merely because the group coordinator is still waiting for its
    # winner.
    if call_id and call_id in _pending_routes(hass):
        _set_pending_route_decision(
            hass,
            {
                "call_id": call_id,
                "action": "answer_ha",
                "endpoint_id": endpoint_id,
                "media_client_id": str(
                    call.data.get("media_client_id") or ""
                ),
                "send_video": camera_send_requested,
            },
        )
        return
    forward_task = bucket.get("forward_tasks", {}).get(call_id)
    forward_claimed = call_id in bucket.get("forward_claims", set())
    group_answer_commit = call_id in bucket.get("ring_group_answer_commits", set())
    if not group_answer_commit and (
        forward_claimed or (forward_task is not None and not forward_task.done())
    ):
        from homeassistant.exceptions import ServiceValidationError

        raise ServiceValidationError(f"call_id {call_id} is being forwarded")
    if call_id.startswith("conference:"):
        manager = hass.data.setdefault(DOMAIN, {}).get("conference_manager")
        resolved = manager.resolve_ha_call(call_id) if manager is not None else None
        if resolved is None or resolved[1] != endpoint_id:
            from homeassistant.exceptions import ServiceValidationError

            raise ServiceValidationError(
                f"conference call {call_id} does not belong to phone {endpoint_id}"
            )
        room_name = resolved[0]
        joined = manager.join_ha_softphone(
            room_name,
            endpoint_id=endpoint_id,
            call_id=call_id,
        )
        if joined is None:
            _LOGGER.warning("sip_answer: conference room not found or full for %s", call_id)
            return
        _joined_call_id, queue = joined
        conference_media = {
            "conference_room": room_name,
            "conference_queue": queue,
            "endpoint_id": endpoint_id,
            "media_client_id": str(call.data.get("media_client_id") or ""),
        }
        registry.upsert(
            call_id,
            state=CallState.IN_CALL.value,
            owner="ha_softphone",
            caller=room_name,
            callee=local_name,
            route_kind="conference",
            endpoint_id=endpoint_id,
        )
        registry.attach_media(call_id, conference_media)
        _bind_service_call_controller(registry, call_id, call)
        registry.add_leg(call_id, call_id, role="ha_softphone", state=CallState.IN_CALL.value)
        _set_ha_softphone_call_state(
            hass,
            CallState.IN_CALL.value,
            endpoint_id=endpoint_id,
            session_device_id=endpoint_device_id,
            caller=room_name,
            callee=local_name,
            peer_name=room_name,
            direction="incoming",
            call_id=call_id,
            sip_status_code=200,
            last_sip_event="SIP_RESPONSE",
            selected_tx_format="16000:s16le:1:20",
            selected_rx_format="16000:s16le:1:20",
            selected_tx_rtp_format="pt=96:L16/16000/1/20ms",
            selected_rx_rtp_format="pt=96:L16/16000/1/20ms",
        )
        return
    pending = registry.pending_invites
    endpoint_pending = _endpoint_call_ids(registry, pending, endpoint_id)
    if not call_id and len(endpoint_pending) == 1:
        call_id = endpoint_pending[0]
        _bind_service_call_controller(registry, call_id, call)
    invite = pending.get(call_id) if call_id else None
    if invite is None:
        from homeassistant.exceptions import ServiceValidationError

        raise ServiceValidationError(
            f"SIP call {call_id or '(current)'} was already answered or is no longer ringing"
        )

    session = registry.sessions.get(registry.resolve_session_id(call_id))
    pbx_runtime = bucket.get("pbx_runtime")
    authoritative_session = (
        pbx_runtime.get_session(
            registry.resolve_session_id(call_id),
            generation=session.generation if session is not None else None,
        )
        if pbx_runtime is not None
        else None
    )
    if authoritative_session is None:
        from homeassistant.exceptions import ServiceValidationError

        raise ServiceValidationError(
            f"PBX session for call_id {call_id} is no longer available"
        )

    preanswered = registry.take_media(call_id, provisional=True)
    from .sdp import (
        browser_video_send_supported,
        build_answer_directional,
        constrained_video_direction,
        offered_dtmf_formats,
    )

    local_rtp_port = int((preanswered or {}).get("local_rtp_port") or 0)
    local_video_rtp_port = int(
        (preanswered or {}).get("local_video_rtp_port") or 0
    )
    video_rtp_socket = (preanswered or {}).get("video_rtp_socket")
    video_rtcp_socket = (preanswered or {}).get("video_rtcp_socket")
    video_rtp_source = (preanswered or {}).get("video_rtp_source")
    media_reservation = (preanswered or {}).get("rtp_reservation")
    video_media_reservation = (preanswered or {}).get("video_rtp_reservation")
    video_failure_reason = str(
        (preanswered or {}).get("video_failure_reason") or ""
    )
    endpoint_video_enabled = (
        browser_endpoint is None or browser_endpoint.supports("video")
    )
    # Camera transmission is opt-in for each answer.  The integration option
    # is the administrator-level capability gate; ``send_video`` is the
    # browser/user choice for this dialog.  A receive-only answer can still
    # display the remote stream without advertising media that the browser did
    # not authorize HA to send.
    camera_send_enabled = (
        endpoint_video_enabled
        and bool(_get_transport_config(hass).get(CONF_VIDEO_CAMERA_SEND, False))
        and bool(call.data.get("send_video", False))
    )
    video_direction = (
        constrained_video_direction(
            invite.video_format.direction,
            allow_send=camera_send_enabled
            and browser_video_send_supported(invite.video_format)
            and not invite.remote_video_connection_held,
        )
        if invite.video_format is not None and endpoint_video_enabled
        else "inactive"
    )
    if preanswered is not None and preanswered.get("final_response_sent", True):
        # Compatibility with calls created by older runtimes which already
        # sent a final response before routing completed.
        negotiated_video_direction = str(
            preanswered.get("video_direction") or "inactive"
        )
        video_direction = constrained_video_direction(
            negotiated_video_direction,
            allow_send=camera_send_enabled
            and browser_video_send_supported(invite.video_format)
            and not invite.remote_video_connection_held,
        )
    answer_sdp = ""
    response_already_sent = bool(
        preanswered is not None and preanswered.get("final_response_sent", True)
    )
    if local_rtp_port:
        if not bool((preanswered or {}).get("final_response_sent", True)):
            local_ip = await _ha_advertise_host(hass)
            dtmf_formats = offered_dtmf_formats(invite.remote_sdp)
            answer = build_answer_directional(
                local_ip,
                local_ip,
                local_rtp_port,
                invite.send_format,
                invite.recv_format,
                dtmf=dtmf_formats[0] if dtmf_formats else None,
                remote_sdp=invite.remote_sdp,
                video_port=local_video_rtp_port,
                video_format=(
                    invite.answer_video_format
                    if local_video_rtp_port and endpoint_video_enabled
                    else None
                ),
                video_direction=video_direction,
            )
            answer_sdp = answer
        _LOGGER.info("SIP answered early-media trunk call_id=%s", call_id)
    else:
        local_ip = await _ha_advertise_host(hass)
        if invite.video_format is not None and endpoint_video_enabled:
            try:
                (
                    media_reservation,
                    video_rtp_socket,
                    video_rtcp_socket,
                ) = reserve_sip_video_media(hass)
                local_rtp_port, local_video_rtp_port = media_reservation.ports
            except (OSError, RuntimeError) as err:
                _LOGGER.warning(
                    "SIP video socket unavailable, answering audio-only: %s", err
                )
                media_reservation = None
                video_failure_reason = "local_video_resources_unavailable"
                local_rtp_port = _allocate_sip_rtp_port(hass)
                local_video_rtp_port = 0
        else:
            local_rtp_port = _allocate_sip_rtp_port(hass)
        if local_video_rtp_port and invite.video_format is not None:
            video_rtp_source = RtpSenderState.create(
                clock_rate=int(invite.video_format.clock_rate),
                now=asyncio.get_running_loop().time(),
            )
        answer = build_answer_directional(
            local_ip,
            local_ip,
            local_rtp_port,
            invite.send_format,
            invite.recv_format,
            remote_sdp=invite.remote_sdp,
            video_port=local_video_rtp_port,
            video_format=(
                invite.answer_video_format if endpoint_video_enabled else None
            ),
            video_direction=video_direction,
        )
        answer_sdp = answer

    resolved_callee = str(
        (session.callee if session is not None else "") or local_name
    )
    softphone_media = {
        "invite": invite,
        "local_rtp_port": local_rtp_port,
        "local_video_rtp_port": local_video_rtp_port,
        "video_direction": video_direction,
        # Preserve the explicit per-call camera consent even when the initial
        # offer is audio-only.  A later re-INVITE may add video (the normal
        # Wildix flow); codec support is evaluated against that new offer by
        # the media-update path, while consent remains immutable for the
        # lifetime of the dialog.
        "camera_send_authorized": bool(camera_send_enabled),
        "video_rtp_socket": video_rtp_socket,
        "video_rtcp_socket": video_rtcp_socket,
        "video_rtp_source": video_rtp_source,
        "rtp_reservation": media_reservation,
        "video_rtp_reservation": video_media_reservation,
        "endpoint_id": endpoint_id,
        "media_client_id": str(call.data.get("media_client_id") or ""),
        "video_failure_reason": video_failure_reason,
    }
    def _release_answer_media(_reason: str) -> None:
        if registry.softphone_media.get(call_id) is softphone_media:
            registry.softphone_media.pop(call_id, None)
        _release_media_reservation(softphone_media)

    transaction = AnswerTransaction(
        authoritative_session,
        lambda status, reason, sdp: (
            True
            if response_already_sent
            else _sip_send_final_response(
                hass,
                call_id,
                status,
                reason,
                answer_sdp=sdp,
            )
        ),
    )
    transaction.add_resource(
        f"softphone_media:{call_id}",
        softphone_media,
        _release_answer_media,
    )

    def _claim_answer() -> bool:
        if pending.get(call_id) is not invite:
            return False
        claimed = registry.transition(
            call_id,
            state=CallState.IN_CALL.value,
            owner="ha_softphone",
            caller=invite.caller,
            callee=resolved_callee,
            route_kind="ha_softphone",
            endpoint_id=endpoint_id,
            media_client_id=str(call.data.get("media_client_id") or ""),
            expected_generation=authoritative_session.generation,
        )
        if claimed is None:
            return False
        pending.pop(call_id, None)
        return True

    answer_result = await transaction.commit(
        answer_sdp,
        claim=_claim_answer,
    )
    if not answer_result.committed:
        if response_already_sent:
            _sip_send_bye(hass, call_id)
        registry.finish_and_pop(
            call_id,
            reason=answer_result.reason or TerminalReason.PROTOCOL_ERROR.value,
            state=CallState.CANCELLED.value,
        )
        from homeassistant.exceptions import ServiceValidationError

        raise ServiceValidationError(
            f"SIP answer transaction failed for call_id {call_id}: "
            f"{answer_result.reason or 'unknown error'}"
        )
    registry.attach_media(call_id, softphone_media)
    registry.add_leg(call_id, call_id, role="ha_softphone", state=CallState.IN_CALL.value)
    _LOGGER.info("SIP answered call_id=%s", call_id)
    video_active = bool(
        invite.video_format is not None
        and local_video_rtp_port
        and video_direction != "inactive"
    )
    _set_ha_softphone_call_state(
        hass,
        CallState.IN_CALL.value,
        endpoint_id=endpoint_id,
        session_device_id=endpoint_device_id,
        caller=invite.caller,
        callee=resolved_callee,
        peer_name=invite.caller,
        direction="incoming",
        call_id=call_id,
        dialed_target=invite.target,
        sip_status_code=200,
        last_sip_event="SIP_RESPONSE",
        selected_tx_format=invite.send_format.audio_format.wire_token(),
        selected_rx_format=invite.recv_format.audio_format.wire_token(),
        selected_tx_rtp_format=invite.send_format.wire_token(),
        selected_rx_rtp_format=invite.recv_format.wire_token(),
        audio_direction=invite.local_audio_direction,
        audio_connection_held=invite.remote_audio_connection_held,
        video_active=video_active,
        video_requested=bool(invite.video_format is not None),
        video_negotiated=bool(invite.video_format is not None and local_video_rtp_port),
        video_status=(
            "degraded"
            if video_failure_reason
            else "active"
            if video_active
            else "rejected"
            if invite.video_format is not None
            else "inactive"
        ),
        video_failure_reason=video_failure_reason,
        video_format=(invite.video_format.wire_token() if invite.video_format else ""),
        video_send_format=(
            invite.send_video_format.wire_token()
            if invite.send_video_format is not None
            else ""
        ),
        video_receive_format=(
            invite.recv_video_format.wire_token()
            if invite.recv_video_format is not None
            else ""
        ),
        video_direction=(
            video_direction
            if invite.video_format is not None and local_video_rtp_port
            else "inactive"
        ),
    )


async def _handle_sip_decline_service(call: ServiceCall) -> None:
    hass: HomeAssistant = call.hass
    device = await _resolve_command_phone(hass, call)
    if device is not None:
        decline_button = str(
            (device.get("entities") or {}).get("decline") or ""
        ).strip()
        await _require_phone_service_control(
            hass,
            call,
            device=device,
            action_entity_ids=(decline_button,) if decline_button else (),
        )
        reason = str(call.data.get("reason") or call.data.get("decline_reason") or "").strip()
        if _has_esphome_action(hass, device, "decline_call"):
            await _call_esphome_action(
                hass,
                device,
                "decline_call",
                {"reason": reason},
                context=call.context,
            )
        elif not await _press_device_button(
            hass,
            device,
            "decline",
            "SIP decline",
            context=call.context,
        ):
            from homeassistant.exceptions import ServiceValidationError

            raise ServiceValidationError(f"{device.get('name') or 'ESP phone'} has no decline control")
        return
    endpoint_id, browser_endpoint = _service_browser_endpoint(hass, call, strict=True)
    await _require_phone_service_control(
        hass,
        call,
        endpoint=browser_endpoint,
    )
    endpoint_device_id = str(
        getattr(browser_endpoint, "device_id", "") or HA_SOFTPHONE_DEVICE_ID
    )
    call_id = str(call.data.get("call_id") or "").strip()
    status = int(call.data.get("status") or 486)
    reason = str(call.data.get("reason") or "Busy Here").strip() or "Busy Here"
    app_reason = str(call.data.get("decline_reason") or "").strip()
    if not app_reason:
        if status == 486:
            app_reason = TerminalReason.BUSY.value
        elif status == 487:
            app_reason = TerminalReason.CANCELLED.value
        elif status == 603:
            app_reason = TerminalReason.DECLINED.value
        else:
            app_reason = reason or TerminalReason.DECLINED.value
    if not call_id:
        call_id = _single_pending_route_call_id(hass, endpoint_id) or str(
            _ha_softphone_store(hass, endpoint_id).get("call_id") or ""
        ).strip()
    registry = _call_registry(hass)
    if call_id and not _call_belongs_to_endpoint(registry, call_id, endpoint_id):
        from homeassistant.exceptions import ServiceValidationError

        raise ServiceValidationError(
            f"call_id {call_id} belongs to another phone endpoint"
        )
    from .local_softphone_runtime import local_softphone_bridge

    local_bridge = local_softphone_bridge(hass)
    if local_bridge is not None and local_bridge.get_call(call_id) is not None:
        from homeassistant.exceptions import ServiceValidationError

        from .local_softphone_bridge import LocalBridgeError

        try:
            local_bridge.decline(call_id, endpoint_id)
        except LocalBridgeError as err:
            raise ServiceValidationError(str(err)) from err
        return
    if call_id.startswith("conference:"):
        manager = hass.data.setdefault(DOMAIN, {}).get("conference_manager")
        if manager is not None and await manager.decline_ha_softphone(
            call_id,
            endpoint_id,
            reason=app_reason,
        ):
            return
        from homeassistant.exceptions import ServiceValidationError

        raise ServiceValidationError(
            f"conference call {call_id} is no longer ringing on phone {endpoint_id}"
        )
    # A decline from one browser fork is a leg decision, not a request to
    # cancel the ring-group coordinator which owns every other candidate.
    # Let the pending-route primitive record that endpoint as declined and
    # resolve the aggregate decision only when all candidates have declined.
    # Generic forwarding is cancelled below only when there is no active
    # per-leg route decision to consume this command.
    if call_id and call_id in _pending_routes(hass):
        _set_pending_route_decision(
            hass,
            {
                "call_id": call_id,
                "action": "busy" if status == 486 else "cancel" if status == 487 else "decline",
                "status": status,
                "reason": reason,
                "decline_reason": app_reason,
                "endpoint_id": endpoint_id,
            },
        )
        return
    forward_task = hass.data.setdefault(DOMAIN, {}).get("forward_tasks", {}).get(call_id)
    if forward_task is not None and not forward_task.done():
        forward_task.cancel()
        await asyncio.gather(forward_task, return_exceptions=True)
    pending = registry.pending_invites
    endpoint_pending = _endpoint_call_ids(registry, pending, endpoint_id)
    if not call_id and len(endpoint_pending) == 1:
        call_id = endpoint_pending[0]
    pending.pop(call_id, None)
    preanswered_item = (
        registry.take_media(call_id, provisional=True) if call_id else None
    )
    if preanswered_item is not None:
        _release_media_reservation(preanswered_item)
        final_response_sent = bool(
            preanswered_item.get("final_response_sent", True)
        )
        if final_response_sent:
            _sip_send_bye(hass, call_id)
        elif not _sip_send_final_response(
            hass,
            call_id,
            status,
            reason,
            decline_reason=app_reason,
        ):
            _LOGGER.warning(
                "sip_decline: early SIP transaction no longer exists for %s",
                call_id,
            )
        _LOGGER.info(
            "SIP declined %s trunk call_id=%s reason=%s",
            "answered" if final_response_sent else "early-media",
            call_id,
            app_reason,
        )
        _set_ha_softphone_call_state(
            hass,
            "declined",
            endpoint_id=endpoint_id,
            session_device_id=endpoint_device_id,
            reason=app_reason,
            call_id=call_id,
            sip_status_code=status,
            last_sip_event="BYE" if final_response_sent else "SIP_RESPONSE",
        )
        registry.finish_and_pop(call_id, reason=app_reason, state="declined")
        return
    if not call_id or not _sip_send_final_response(
        hass,
        call_id,
        status,
        reason,
        decline_reason=app_reason,
    ):
        _LOGGER.warning("sip_decline: no pending SIP call %s", call_id or "(current)")
        return

    _LOGGER.info("SIP declined call_id=%s status=%s reason=%s app_reason=%s", call_id, status, reason, app_reason)
    _set_ha_softphone_call_state(
        hass,
        "declined",
        endpoint_id=endpoint_id,
        session_device_id=endpoint_device_id,
        reason=app_reason,
        call_id=call_id,
        sip_status_code=status,
        last_sip_event="SIP_RESPONSE",
    )
    registry.finish_and_pop(call_id, reason=app_reason, state="declined")


async def _handle_sip_hangup_service(call: ServiceCall) -> None:
    hass: HomeAssistant = call.hass
    device = await _resolve_command_phone(hass, call)
    if device is not None:
        decline_button = str(
            (device.get("entities") or {}).get("decline") or ""
        ).strip()
        await _require_phone_service_control(
            hass,
            call,
            device=device,
            action_entity_ids=(decline_button,) if decline_button else (),
        )
        reason = str(call.data.get("reason") or "local_hangup").strip()
        if _has_esphome_action(hass, device, "decline_call"):
            await _call_esphome_action(
                hass,
                device,
                "decline_call",
                {"reason": reason},
                context=call.context,
            )
        elif not await _press_device_button(
            hass,
            device,
            "decline",
            "SIP hangup",
            context=call.context,
        ):
            from homeassistant.exceptions import ServiceValidationError

            raise ServiceValidationError(f"{device.get('name') or 'ESP phone'} has no hangup/decline control")
        return
    endpoint_id, browser_endpoint = _service_browser_endpoint(hass, call, strict=True)
    await _require_phone_service_control(
        hass,
        call,
        endpoint=browser_endpoint,
    )
    endpoint_device_id = str(
        getattr(browser_endpoint, "device_id", "") or HA_SOFTPHONE_DEVICE_ID
    )
    call_id = str(call.data.get("call_id") or "").strip()
    if not call_id:
        call_id = _single_pending_route_call_id(hass, endpoint_id) or str(
            _ha_softphone_store(hass, endpoint_id).get("call_id") or ""
        ).strip()
    registry = _call_registry(hass)
    if call_id and not _call_belongs_to_endpoint(registry, call_id, endpoint_id):
        from homeassistant.exceptions import ServiceValidationError

        raise ServiceValidationError(
            f"call_id {call_id} belongs to another phone endpoint"
        )
    from .local_softphone_runtime import local_softphone_bridge

    local_bridge = local_softphone_bridge(hass)
    if local_bridge is not None and local_bridge.get_call(call_id) is not None:
        from homeassistant.exceptions import ServiceValidationError

        from .local_softphone_bridge import LocalBridgeError

        try:
            local_bridge.hangup(call_id, endpoint_id)
        except LocalBridgeError as err:
            raise ServiceValidationError(str(err)) from err
        return
    forward_task = hass.data.setdefault(DOMAIN, {}).get("forward_tasks", {}).get(call_id)
    if forward_task is not None and not forward_task.done():
        forward_task.cancel()
        await asyncio.gather(forward_task, return_exceptions=True)
    if call_id and call_id in _pending_routes(hass):
        future = _pending_routes(hass)[call_id].get("future")
        if future is not None and future.done():
            _pending_routes(hass).pop(call_id, None)
        else:
            _set_pending_route_decision(
                hass,
                {
                    "call_id": call_id,
                    "action": "cancel",
                    "reason": "Request Terminated",
                    "decline_reason": TerminalReason.LOCAL_HANGUP.value,
                    "endpoint_id": endpoint_id,
                },
            )
            return
    clients = registry.sip_clients
    relays = registry.relays
    pending = registry.pending_invites
    media_sessions = registry.softphone_media
    preanswered = registry.preanswered
    softphone_store = _ha_softphone_store(hass, endpoint_id)
    endpoint_bridge_calls = _endpoint_call_ids(
        registry, registry.bridge_clients, endpoint_id
    )
    endpoint_clients = _endpoint_call_ids(registry, clients, endpoint_id)
    endpoint_pending = _endpoint_call_ids(registry, pending, endpoint_id)
    endpoint_media = _endpoint_call_ids(registry, media_sessions, endpoint_id)
    if not call_id and len(endpoint_bridge_calls) == 1:
        call_id = endpoint_bridge_calls[0]
    if not call_id and len(endpoint_clients) == 1:
        call_id = endpoint_clients[0]
    if not call_id and len(endpoint_pending) == 1:
        call_id = endpoint_pending[0]
    if not call_id and len(endpoint_media) == 1:
        call_id = endpoint_media[0]
    if not call_id:
        call_id = str(softphone_store.get("call_id") or "").strip()
    active_session = (
        registry.sessions.get(registry.resolve_session_id(call_id)) if call_id else None
    )
    caller = str(
        (active_session.caller if active_session is not None else "")
        or softphone_store.get("caller")
        or softphone_store.get("last_terminal_caller")
        or ""
    )
    callee = str(
        (active_session.callee if active_session is not None else "")
        or softphone_store.get("callee")
        or softphone_store.get("last_terminal_callee")
        or ""
    )
    peer_name = str(
        callee
        or softphone_store.get("peer_name")
        or softphone_store.get("last_terminal_peer_name")
        or ""
    )
    direction = str(
        softphone_store.get("direction")
        or softphone_store.get("last_terminal_direction")
        or ("incoming" if active_session is not None else "")
        or ""
    )
    bridge_handled, bridge_source_call_id, bridge_dest_call_id, bridge_client, bridge_server_bye = await _terminate_sip_bridge(
        hass,
        call_id,
        endpoint_id=endpoint_id,
        session_device_id=endpoint_device_id,
    )
    if bridge_handled:
        call_id = bridge_source_call_id
        _set_sip_bridge_call_state(
            hass,
            CallState.IDLE.value,
            caller=caller,
            callee=callee,
            peer_name=peer_name,
            call_id=call_id,
            dest_call_id=bridge_dest_call_id,
            reason=TerminalReason.LOCAL_HANGUP.value,
            origin="self",
            last_sip_event="SIP_BYE",
        )
        _LOGGER.info(
            "SIP bridge hangup call_id=%s dest_call_id=%s client=%s server_bye=%s",
            bridge_source_call_id,
            bridge_dest_call_id,
            bridge_client,
            bridge_server_bye,
        )
        return
    client, watcher = registry.detach_client(call_id) if call_id else (None, None)
    if client is not None and watcher is None and client.dialog is None:
        # The initial INVITE coroutine owns response processing. Ask it to
        # defer CANCEL until RFC 3261 permits it, rather than racing a second
        # reader against the same SIP transaction.
        client.request_cancel()
        client = None
    relay = relays.pop(call_id, None) if call_id else None
    media_session = media_sessions.pop(call_id, None) if call_id else None
    _release_media_reservation(media_session)
    conference_room = str((media_session or {}).get("conference_room") or "")
    if conference_room:
        manager = hass.data.setdefault(DOMAIN, {}).get("conference_manager")
        if manager is not None:
            await manager.leave_ha_softphone(
                conference_room,
                call_id=call_id,
                reason=TerminalReason.LOCAL_HANGUP.value,
            )
    pending_ids = (
        [call_id]
        if call_id and call_id in pending
        else ([] if call_id else endpoint_pending)
    )
    server_bye = False
    pending_closed = 0
    await async_cleanup_sip_runtime(
        relay=relay,
        client=client,
        watcher=watcher,
        terminate_client=True,
        relay_first=False,
    )
    for pending_call_id in pending_ids:
        invite = pending.pop(pending_call_id, None)
        if invite is None:
            continue
        preanswered_item = preanswered.pop(pending_call_id, None)
        if preanswered_item is not None:
            _release_media_reservation(preanswered_item)
            if _sip_send_bye(hass, pending_call_id):
                pending_closed += 1
        elif _sip_send_final_response(
            hass,
            pending_call_id,
            487,
            "Request Terminated",
            decline_reason=TerminalReason.LOCAL_HANGUP.value,
        ):
            pending_closed += 1
        _set_ha_softphone_call_state(
            hass,
            CallState.IDLE.value,
            endpoint_id=endpoint_id,
            session_device_id=endpoint_device_id,
            caller=invite.caller,
            callee=invite.target,
            peer_name=invite.caller,
            direction="incoming",
            call_id=pending_call_id,
            reason=TerminalReason.LOCAL_HANGUP.value,
            origin="self",
            sip_status_code=487,
            last_sip_event="SIP_RESPONSE",
        )
        registry.finish_and_pop(pending_call_id, reason=TerminalReason.LOCAL_HANGUP.value)
    if client is None and relay is None:
        for server in _sip_servers(hass):
            send_bye = getattr(server, "send_bye", None)
            if callable(send_bye) and send_bye(call_id):
                server_bye = True
                if not call_id:
                    call_id = "(active)"
                break
    _set_ha_softphone_call_state(
        hass,
        CallState.IDLE.value,
        endpoint_id=endpoint_id,
        session_device_id=endpoint_device_id,
        caller=caller,
        callee=callee,
        peer_name=peer_name,
        direction=direction,
        call_id=call_id,
        reason=TerminalReason.LOCAL_HANGUP.value,
        origin="self",
        last_sip_event="SIP_BYE" if (client is not None or relay is not None or server_bye) else "SIP_HANGUP",
        pending_closed=pending_closed,
    )
    if call_id:
        registry.finish_and_pop(call_id, reason=TerminalReason.LOCAL_HANGUP.value)
    _LOGGER.info(
        "SIP hangup call_id=%s client=%s relay=%s pending_closed=%d server_bye=%s",
        call_id,
        client is not None,
        relay is not None,
        pending_closed,
        server_bye,
    )


async def _refresh_phonebook_sensor(hass: HomeAssistant) -> None:
    sensor = hass.data.get(DOMAIN, {}).get("phonebook_sensor")
    if sensor is not None:
        await sensor.async_update()


async def _current_roster_json(hass: HomeAssistant) -> str:
    sensor = hass.data.get(DOMAIN, {}).get("phonebook_sensor")
    if sensor is not None:
        return str(sensor.extra_state_attributes.get("roster_json", "") or "")
    state = hass.states.get("sensor.voip_phonebook")
    if state is None:
        return ""
    return str(state.attributes.get("roster_json") or "")


async def _refresh_and_push_phonebook(hass: HomeAssistant) -> None:
    await _refresh_phonebook_sensor(hass)
    roster_json = await _current_roster_json(hass)
    await _push_roster_json_to_esps(hass, roster_json)


async def _deferred_phonebook_sync(hass: HomeAssistant) -> None:
    """Push the canonical phonebook after entry setup/reload settles."""
    for delay in (0.0, 2.0, 10.0):
        if delay:
            await asyncio.sleep(delay)
        await _refresh_and_push_phonebook(hass)


def _entry_runtime_signature(entry: ConfigEntry) -> dict:
    """Return parent-entry fields whose mutation requires a transport reload."""
    return {
        key: value
        for key, value in entry.data.items()
        if key not in {CONF_PHONEBOOK_CONTACTS, CONF_SIP_ACCOUNTS}
    }


def _entry_phone_signature(entry: ConfigEntry) -> tuple:
    """Return an equality-stable snapshot of native logical-phone subentries."""
    return tuple(
        (
            subentry.subentry_id,
            subentry.title,
            dict(subentry.data),
        )
        for subentry in sorted(phone_subentries(entry), key=lambda item: item.subentry_id)
    )


async def _async_config_entry_updated(
    hass: HomeAssistant, entry: ConfigEntry
) -> None:
    """Apply native config/subentry changes as soon as HA persists them."""
    bucket = hass.data.setdefault(DOMAIN, {})
    if not any(
        str(subentry.data.get("endpoint_id") or "").strip()
        == DEFAULT_ENDPOINT_ID
        for subentry in phone_subentries(entry)
    ):
        previous_records = bucket.get("entry_phone_records", {})
        restore_default_phone_subentry(
            hass,
            entry,
            previous_records.get(DEFAULT_ENDPOINT_ID),
        )
        bucket["entry_phone_signature"] = _entry_phone_signature(entry)
        bucket["entry_phone_records"] = {
            str(subentry.data.get("endpoint_id") or "").strip(): dict(
                subentry.data
            )
            for subentry in phone_subentries(entry)
        }
        await hass.config_entries.async_reload(entry.entry_id)
        return
    runtime_signature = _entry_runtime_signature(entry)
    phone_signature = _entry_phone_signature(entry)
    contacts_signature = tuple(
        dict(item)
        for item in entry.data.get(CONF_PHONEBOOK_CONTACTS, [])
        if isinstance(item, dict)
    )
    previous_runtime = bucket.get("entry_runtime_signature")
    previous_phones = bucket.get("entry_phone_signature")
    previous_contacts = bucket.get("entry_contacts_signature")
    bucket["entry_runtime_signature"] = runtime_signature
    bucket["entry_phone_signature"] = phone_signature
    bucket["entry_phone_records"] = {
        str(subentry.data.get("endpoint_id") or "").strip(): dict(subentry.data)
        for subentry in phone_subentries(entry)
    }
    bucket["entry_contacts_signature"] = contacts_signature

    if previous_runtime is not None and previous_runtime != runtime_signature:
        # Listener-owned reload keeps ConfigFlow and ConfigSubentryFlow on the
        # non-reloading HA API and avoids duplicate/racing reload requests.
        await hass.config_entries.async_reload(entry.entry_id)
        return

    phones_changed = previous_phones is not None and previous_phones != phone_signature
    contacts_changed = (
        previous_contacts is not None and previous_contacts != contacts_signature
    )
    if phones_changed:
        previous_browser_ids = {
            endpoint.endpoint_id
            for endpoint in tuple(
                getattr(bucket.get("endpoint_registry"), "endpoints", ())
            )
            if endpoint.kind is EndpointKind.BROWSER
        }
        sync_registry_from_entry(hass, entry)
        for subentry in phone_subentries(entry):
            endpoint_id = str(subentry.data.get("endpoint_id") or "").strip()
            endpoint_registry = bucket.get("endpoint_registry")
            endpoint = (
                endpoint_registry.get(endpoint_id)
                if endpoint_registry is not None and endpoint_id
                else None
            )
            if endpoint is None or endpoint.kind is not EndpointKind.BROWSER:
                continue
            await _async_load_ha_softphone_store(
                hass,
                entry,
                endpoint_id=endpoint.endpoint_id,
                endpoint_data=dict(subentry.data),
            )
        endpoint_registry = bucket.get("endpoint_registry")
        current_browser_ids = {
            endpoint.endpoint_id
            for endpoint in tuple(getattr(endpoint_registry, "endpoints", ()))
            if endpoint.kind is EndpointKind.BROWSER
        }
        removed_browser_ids = previous_browser_ids - current_browser_ids
        presence = bucket.setdefault("ha_softphone_presence", {})
        waiters = bucket.setdefault("ha_softphone_presence_events", {})
        for endpoint_id in removed_browser_ids:
            presence.pop(endpoint_id, None)
            waiter = waiters.get(endpoint_id)
            if waiter is not None:
                waiter.clear()
        # Existing websocket subscriptions survive subentry updates. Push the
        # new name/groups/capabilities/availability immediately instead of
        # waiting for a card reconnect or an unrelated call-state transition.
        for endpoint_id in sorted(previous_browser_ids | current_browser_ids):
            _publish_ha_softphone_state(hass, endpoint_id=endpoint_id)
        endpoint_sensor = bucket.get("ha_softphone_endpoint_sensor")
        if endpoint_sensor is not None:
            # This legacy compatibility entity is non-polling. Keep the
            # default phone's extension/groups current after native subentry
            # reconfiguration just as the old settings service did.
            await endpoint_sensor.async_update()
        from .store import sip_accounts

        registrar = bucket.get("sip_registrar")
        if registrar is not None:
            registrar.update_accounts(sip_accounts(hass))

    if contacts_changed:
        bucket["manual_roster_entries"] = _manual_roster_entries(hass)
    if phones_changed or contacts_changed:
        await _refresh_and_push_phonebook(hass)


def _register_phonebook_service_event_sync(hass: HomeAssistant) -> None:
    """Refresh the phonebook when an ESPHome roster service appears."""
    bucket = hass.data.setdefault(DOMAIN, {})
    if bucket.get("phonebook_service_event_unsub") is not None:
        return

    @callback
    def _on_service_registered(event: Event) -> None:
        if event.data.get("domain") != "esphome":
            return
        service = str(event.data.get("service") or "")
        if not service.endswith("_set_roster_json"):
            return
        create_runtime_task(hass, _refresh_and_push_phonebook(hass))

    bucket["phonebook_service_event_unsub"] = hass.bus.async_listen(
        EVENT_SERVICE_REGISTERED,
        _on_service_registered,
    )


async def _handle_set_dnd_service(call: ServiceCall) -> None:
    hass: HomeAssistant = call.hass
    endpoint_id, _endpoint = _service_configured_endpoint(hass, call)
    dnd_entities = tuple(
        entity_id
        for entity_id in getattr(_endpoint, "entity_ids", ())
        if str(entity_id).startswith("switch.")
    )
    await _require_phone_service_control(
        hass,
        call,
        endpoint=_endpoint,
        action_entity_ids=dnd_entities,
    )
    enabled = bool(call.data.get("dnd"))
    from .switch import async_set_endpoint_dnd

    await async_set_endpoint_dnd(hass, endpoint_id, enabled)
    _LOGGER.info(
        "HA softphone endpoint=%s DND set to %s via service",
        endpoint_id,
        enabled,
    )


async def _handle_set_ha_softphone_settings_service(call: ServiceCall) -> None:
    hass = call.hass
    endpoint_id, _endpoint = _service_browser_endpoint(hass, call, strict=True)
    await async_set_ha_softphone_settings(
        hass,
        endpoint_id=endpoint_id,
        extension=call.data.get("extension"),
        ring_group=call.data.get("ring_group"),
        conference_group=call.data.get("conference_group"),
        conference_ring=call.data.get("conference_ring"),
    )
    await _refresh_and_push_phonebook(hass)


async def _async_resolve_browser_destination(
    hass: HomeAssistant,
    *,
    route,
    target: str,
    contacts: list,
    trunk_ready: bool,
    source_endpoint_id: str,
):
    """Apply browser availability policy and return the effective route."""
    from homeassistant.exceptions import ServiceValidationError

    endpoint_registry = hass.data.get(DOMAIN, {}).get("endpoint_registry")
    if endpoint_registry is None:
        return route, target, None

    visited: set[str] = set()
    effective_target = target
    while route.action is RouteAction.ANSWER_HA and route.entry is not None:
        endpoint_id = str((route.entry.metadata or {}).get("endpoint_id") or "").strip()
        endpoint = endpoint_registry.get(endpoint_id) if endpoint_id else None
        if endpoint is None or endpoint.kind is not EndpointKind.BROWSER:
            return route, effective_target, None
        if endpoint.endpoint_id == source_endpoint_id:
            raise ServiceValidationError("a Home Assistant phone cannot call itself")
        if endpoint.endpoint_id in visited:
            raise ServiceValidationError("browser phone offline-forward loop detected")
        if endpoint.dnd or endpoint.active_call_id:
            raise ServiceValidationError(f"{endpoint.name} is busy")
        if endpoint.availability is EndpointAvailability.AVAILABLE:
            return route, effective_target, endpoint

        if (
            endpoint.availability is EndpointAvailability.OFFLINE
            and endpoint.offline_policy is OfflinePolicy.WAIT
        ):
            waiters = hass.data.setdefault(DOMAIN, {}).setdefault(
                "ha_softphone_presence_events", {}
            )
            event = waiters.setdefault(endpoint.endpoint_id, asyncio.Event())
            try:
                await asyncio.wait_for(
                    event.wait(), timeout=float(endpoint.offline_wait_seconds)
                )
            except TimeoutError as err:
                raise ServiceValidationError(
                    f"{endpoint.name} did not come online within "
                    f"{endpoint.offline_wait_seconds} seconds"
                ) from err
            continue

        if endpoint.offline_policy is OfflinePolicy.FORWARD:
            visited.add(endpoint.endpoint_id)
            effective_target = endpoint.offline_forward_target
            route = resolve_ha_router(
                effective_target,
                contacts,
                trunk_ready=trunk_ready,
            )
            continue

        raise ServiceValidationError(f"{endpoint.name} is unavailable")

    return route, effective_target, None


def _logical_endpoint_for_route(hass: HomeAssistant, route):
    """Return the configured logical endpoint carried by a roster route."""
    entry = getattr(route, "entry", None)
    endpoint_id = str(
        ((getattr(entry, "metadata", None) or {}).get("endpoint_id")) or ""
    ).strip()
    registry = hass.data.get(DOMAIN, {}).get("endpoint_registry")
    return registry.get(endpoint_id) if registry is not None and endpoint_id else None


async def _handle_sip_call_target_service(call: ServiceCall, *, force_ha_bridge: bool = False) -> None:
    """Originate a standards SIP call from HA to a roster target or URI-shaped target."""
    from homeassistant.exceptions import ServiceValidationError

    from .roster import parse_roster_json
    from .sip import parse_sip_uri
    from .sip_client import SIP_TIMER_B, SipCallClient

    hass: HomeAssistant = call.hass
    source = await _resolve_source_device_from_call(hass, call)
    dest_device = None if source is not None else await _resolve_target_device(hass, call)
    target = str(
        call.data.get("destination") or call.data.get("target") or call.data.get("call") or ""
    ).strip()
    if not target and dest_device is not None:
        target = str(dest_device.get("name") or "").strip()
    if not target:
        raise ServiceValidationError("target is required")
    if source is not None:
        call_button = str((source.get("entities") or {}).get("call") or "").strip()
        await _require_phone_service_control(
            hass,
            call,
            device=source,
            action_entity_ids=(call_button,) if call_button else (),
        )
        await _call_esphome_action(
            hass,
            source,
            "start_call",
            {"dest": target},
            context=call.context,
        )
        _LOGGER.info("ESP SIP phone %s originating call to %s", source.get("name"), target)
        return
    endpoint_id, browser_endpoint = _service_browser_endpoint(hass, call)
    await _require_phone_service_control(
        hass,
        call,
        endpoint=browser_endpoint,
    )
    if (
        browser_endpoint is not None
        and browser_endpoint.availability is EndpointAvailability.UNAVAILABLE
    ):
        raise ServiceValidationError(f"{browser_endpoint.name} is disabled")
    local_name = _browser_endpoint_name(hass, endpoint_id, browser_endpoint)
    source_device_id = str(
        getattr(browser_endpoint, "device_id", "") or HA_SOFTPHONE_DEVICE_ID
    )
    _ha_softphone_store(hass, endpoint_id)["device_id"] = source_device_id
    cfg = _get_transport_config(hass)
    trunk = hass.data.get(DOMAIN, {}).get("sip_trunk")
    trunk_cfg = _get_trunk_config(hass)
    trunk_ready = _trunk_enabled(trunk_cfg) and bool(getattr(trunk, "registered", False))
    sensor = hass.states.get("sensor.voip_phonebook")
    roster_json = str(sensor.attributes.get("roster_json") or "") if sensor is not None else ""
    contacts = parse_roster_json(roster_json) if roster_json else []
    route = resolve_ha_router(target, contacts, trunk_ready=trunk_ready)
    route, target, browser_destination = await _async_resolve_browser_destination(
        hass,
        route=route,
        target=target,
        contacts=contacts,
        trunk_ready=trunk_ready,
        source_endpoint_id=endpoint_id,
    )
    if browser_destination is not None:
        from .local_softphone_bridge import LocalBridgeError
        from .local_softphone_runtime import start_local_softphone_call

        await _async_prepare_ha_outbound_call(hass, endpoint_id)
        try:
            snapshot = start_local_softphone_call(
                hass,
                endpoint_id,
                browser_destination.endpoint_id,
                request_video=bool(
                    browser_endpoint is not None
                    and browser_endpoint.supports("video")
                ),
                enable_caller_video_send=bool(
                    cfg.get(CONF_VIDEO_CAMERA_SEND, False)
                    and call.data.get("send_video", False)
                ),
                caller_owner_id=str(
                    call.data.get("media_client_id") or ""
                ),
                context=getattr(call, "context", None),
            )
        except LocalBridgeError as err:
            raise ServiceValidationError(str(err)) from err
        _LOGGER.info(
            "HA local phone call started call_id=%s source=%s destination=%s video=%s",
            snapshot.call_id,
            endpoint_id,
            browser_destination.endpoint_id,
            snapshot.video_enabled,
        )
        return
    target_endpoint = _logical_endpoint_for_route(hass, route)
    if (
        target_endpoint is not None
        and target_endpoint.kind is not EndpointKind.BROWSER
    ):
        if target_endpoint.dnd or target_endpoint.active_call_id:
            raise ServiceValidationError(f"{target_endpoint.name} is busy")
        if target_endpoint.availability is not EndpointAvailability.AVAILABLE:
            raise ServiceValidationError(f"{target_endpoint.name} is unavailable")
    # Browser-to-browser calls use the in-memory logical bridge and must not
    # depend on SIP network discovery.  Resolve the advertised address only
    # after that path has been exhausted, for conference or external SIP/RTP.
    local_ip = await _ha_advertise_host(hass)
    if not local_ip:
        raise ServiceValidationError("HA advertise IP is unknown")
    if route.reason is RouteReason.DIRECT_URI:
        # Entity CONTROL permits ordinary phone operation, but an ad-hoc SIP
        # URI also chooses an arbitrary network host/port.  Keep that SSRF and
        # port-probing capability behind HA administrator authentication;
        # roster contacts, registered endpoints and trunk numbers are still
        # available to normal controllers.
        await async_require_service_admin(hass, call)
    display_target = route.entry.display_name if route.entry is not None else target
    if (force_ha_bridge or bool(call.data.get("ha_bridge", False))) and route.action not in {
        RouteAction.ANSWER_HA,
        RouteAction.TRUNK,
        RouteAction.REJECT,
    }:
        if route.entry is not None and route.entry.metadata.get("registered"):
            bridge_uri = route.sip_uri
        else:
            bridge_uri = ha_uri_for(route.target or target, contacts)
        route = replace(route, action=RouteAction.BRIDGE, sip_uri=bridge_uri)
    use_trunk = route.action is RouteAction.TRUNK and trunk_ready
    use_registered_contact_codecs = bool(
        route.entry is not None and route.entry.sip_uri and route.entry.metadata.get("registered")
    )
    if route.action is RouteAction.TRUNK and not use_trunk:
        raise ServiceValidationError(f"{target} requires a registered SIP trunk")
    if route.action is RouteAction.GROUP and route.entry is not None:
        group_type = str((route.entry.metadata or {}).get("group_type") or "")
        if group_type == "ring":
            start_ring_group = hass.data.setdefault(DOMAIN, {}).get("async_start_ring_group_from_ha")
            if start_ring_group is None:
                raise ServiceValidationError(f"{target} is not available yet")
            await _async_prepare_ha_outbound_call(hass, endpoint_id)
            await start_ring_group(
                route.entry,
                context=getattr(call, "context", None),
                endpoint_id=endpoint_id,
                media_client_id=str(call.data.get("media_client_id") or ""),
                request_video=bool(
                    browser_endpoint is not None
                    and browser_endpoint.supports("video")
                ),
                enable_caller_video_send=bool(
                    cfg.get(CONF_VIDEO_CAMERA_SEND, False)
                    and call.data.get("send_video", False)
                ),
            )
            _LOGGER.info("HA softphone started ring group=%s", route.entry.display_name)
            return
        if group_type == "conference":
            room_name = route.entry.name or route.entry.id or target
            from .conference import conference_manager

            await _async_prepare_ha_outbound_call(hass, endpoint_id)
            manager = conference_manager(hass, local_ip=local_ip)
            joined = manager.start_ha_softphone(
                room_name,
                endpoint_id=endpoint_id,
            )
            if joined is None:
                raise ServiceValidationError(f"Conference {room_name} is full")
            call_id, queue = joined
            registry = _call_registry(hass)
            conference_media = {
                "conference_room": room_name,
                "conference_queue": queue,
                "endpoint_id": endpoint_id,
                "media_client_id": str(call.data.get("media_client_id") or ""),
            }
            registry.upsert(
                call_id,
                state=CallState.IN_CALL.value,
                owner="ha_softphone",
                caller=local_name,
                callee=room_name,
                route_kind="conference",
                endpoint_id=endpoint_id,
                source_endpoint_id=endpoint_id,
                media_client_id=str(call.data.get("media_client_id") or ""),
            )
            registry.attach_media(call_id, conference_media)
            _bind_service_call_controller(registry, call_id, call)
            registry.add_leg(call_id, call_id, role="ha_softphone", state=CallState.IN_CALL.value)
            _set_ha_softphone_call_state(
                hass,
                CallState.IN_CALL.value,
                endpoint_id=endpoint_id,
                session_device_id=source_device_id,
                caller=local_name,
                callee=room_name,
                peer_name=room_name,
                direction="outgoing",
                call_id=call_id,
                route_kind="conference",
                sip_status_code=200,
                last_sip_event="LOCAL_CONFERENCE_JOIN",
                selected_tx_format="16000:s16le:1:20",
                selected_rx_format="16000:s16le:1:20",
                selected_tx_rtp_format="pt=96:L16/16000/1/20ms",
                selected_rx_rtp_format="pt=96:L16/16000/1/20ms",
                scope="conference",
                room=room_name,
                target=target,
            )
            ring_members = hass.data.setdefault(DOMAIN, {}).get("async_ring_conference_members")
            if ring_members is not None:
                create_runtime_task(
                    hass,
                    ring_members(route.entry, owner_call_id=call_id),
                )
            _LOGGER.info("HA softphone joined conference room=%s", room_name)
            return
    route_uri = route.sip_uri
    if route.action is RouteAction.GROUP:
        route_uri = ha_uri_for(route.target or target, contacts)
    elif route.action is RouteAction.ASSIST:
        # Assist is a PBX-local application.  Originate the browser media leg
        # to this integration's listener so the canonical inbound dispatcher
        # owns the Assist session and RTP pipeline; never fall through to the
        # external-trunk resolver merely because the roster entry has no
        # network address of its own.
        route_uri = ha_uri_for(route.target or target, contacts)
        route = replace(route, action=RouteAction.BRIDGE, sip_uri=route_uri)
    if use_trunk:
        trunk_target = route.target or target
        route_uri = (
            f"sip:{trunk_target}@{trunk_cfg[CONF_TRUNK_SERVER]}:{int(trunk_cfg[CONF_TRUNK_PORT])};"
            f"transport={str(trunk_cfg[CONF_TRUNK_TRANSPORT]).lower()}"
        )
    if not use_trunk and (route.action not in {RouteAction.DIRECT, RouteAction.FORWARD, RouteAction.BRIDGE, RouteAction.GROUP} or not route_uri):
        raise ServiceValidationError(f"cannot resolve SIP target: {target}")
    await _async_prepare_ha_outbound_call(hass, endpoint_id)
    uri = parse_sip_uri(route_uri)
    remote_tx_formats = _roster_entry_formats(route.entry, "tx_formats") or _device_formats(dest_device, "tx_formats")
    remote_rx_formats = _roster_entry_formats(route.entry, "rx_formats") or _device_formats(dest_device, "rx_formats")
    sip_send_formats, sip_recv_formats = _sip_target_audio_profile(
        remote_tx_formats=remote_tx_formats,
        remote_rx_formats=remote_rx_formats,
        target=target,
    )
    if use_trunk or use_registered_contact_codecs:
        sip_send_formats = list(HA_TRUNK_AUDIO_FORMATS)
        sip_recv_formats = list(HA_TRUNK_AUDIO_FORMATS)
    entry_metadata = dict(route.entry.metadata or {}) if route.entry is not None else {}
    native_audio_endpoint = bool(
        entry_metadata.get("local_ha")
        or entry_metadata.get("virtual_endpoint")
        or entry_metadata.get("group_type")
        or str(entry_metadata.get("endpoint_kind") or "").strip().lower()
        == EndpointKind.ESPHOME.value
    )
    # SIP video is HA/browser-owned.  Native ESP audio endpoints deliberately
    # stay outside this path, while direct SIP URIs, trunk calls, registered
    # clients and manually configured standard SIP contacts may negotiate it.
    # A phonebook entry may carry ESP-style audio capability metadata while
    # its resolved route still exits through the SIP trunk.  The final route,
    # not those contact hints, decides whether browser-owned video is valid.
    source_video_enabled = (
        browser_endpoint is None or browser_endpoint.supports("video")
    )
    target_video_enabled = (
        target_endpoint is None or target_endpoint.supports("video")
    )
    video_enabled = (
        bool(cfg.get(CONF_EXPERIMENTAL_VIDEO, False))
        and source_video_enabled
        and target_video_enabled
        and (use_trunk or not native_audio_endpoint)
    )
    video_reservation = None
    video_rtp_socket = None
    video_rtcp_socket = None
    video_failure_reason = ""
    if video_enabled:
        try:
            (
                video_reservation,
                video_rtp_socket,
                video_rtcp_socket,
            ) = reserve_sip_video_media(hass)
            local_rtp_port, local_video_rtp_port = video_reservation.ports
        except (OSError, RuntimeError) as err:
            _LOGGER.warning(
                "SIP video socket unavailable, originating audio-only: %s", err
            )
            video_reservation = None
            video_failure_reason = "local_video_resources_unavailable"
            local_rtp_port = _allocate_sip_rtp_port(hass)
            local_video_rtp_port = 0
    else:
        local_rtp_port = _allocate_sip_rtp_port(hass)
        local_video_rtp_port = 0
    from .sdp import (
        DEFAULT_VIDEO_FORMATS,
        browser_video_send_supported,
        video_formats_renegotiation_compatible,
    )

    # Camera transmission is opt-in for each call.  Keep offering a receive
    # path when it is off, but never advertise send capability solely because
    # the administrator enabled the experimental camera feature globally.
    camera_send_enabled = (
        video_enabled
        and bool(cfg.get(CONF_VIDEO_CAMERA_SEND, False))
        and bool(call.data.get("send_video", False))
    )
    offered_video_formats = (
        tuple(item for item in DEFAULT_VIDEO_FORMATS if browser_video_send_supported(item))
        if camera_send_enabled
        else DEFAULT_VIDEO_FORMATS
    )

    client = SipCallClient(
        local_ip=local_ip,
        local_name=(
            str(trunk_cfg.get(CONF_TRUNK_USERNAME) or local_name)
            if use_trunk
            else local_name
        ),
        local_sip_port=int(cfg["sip_port"]),
        local_rtp_port=local_rtp_port,
        supported_send_formats=sip_send_formats,
        supported_recv_formats=sip_recv_formats,
        signaling_transport=_sip_uri_transport(uri),
        auth_username=str(trunk_cfg.get(CONF_TRUNK_AUTH_USERNAME) or ""),
        username=str(trunk_cfg.get(CONF_TRUNK_USERNAME) or ""),
        password=str(trunk_cfg.get(CONF_TRUNK_PASSWORD) or "") if use_trunk else "",
        outbound_proxy=str(trunk_cfg.get(CONF_TRUNK_OUTBOUND_PROXY) or "") if use_trunk else "",
        include_common_codecs=use_trunk or use_registered_contact_codecs or video_enabled,
        local_video_rtp_port=local_video_rtp_port,
        video_formats=offered_video_formats if video_enabled else (),
        video_direction=("sendrecv" if camera_send_enabled else "recvonly"),
        media_reservation=video_reservation,
        video_rtp_socket=video_rtp_socket,
        video_rtcp_socket=video_rtcp_socket,
    )

    async def _prepare_softphone_media_update(previous, updated, method):
        """Stage one peer re-offer for the live HA browser media owner."""

        call_id = client.dialog_ids.call_id
        session = registry.sessions.get(registry.resolve_session_id(call_id))
        if session is None:
            return None
        call_generation = session.generation
        bucket = hass.data.setdefault(DOMAIN, {})
        audio_session = bucket.setdefault("active_audio_sessions", {}).get(call_id)
        previous_video = previous.video_format
        updated_video = updated.video_format
        if (previous_video is None) != (updated_video is None):
            return None
        video_session = bucket.setdefault("active_video_sessions", {}).get(call_id)
        if previous_video is not None and updated_video is not None:
            if not (
                video_formats_renegotiation_compatible(
                    previous_video,
                    updated_video,
                )
                and video_formats_renegotiation_compatible(
                    previous.recv_video_format,
                    updated.recv_video_format,
                )
            ):
                return None

        async def _commit() -> None:
            if not registry.is_generation_current(call_id, call_generation):
                raise RuntimeError(
                    "SIP softphone media update belongs to a terminated call"
                )
            if audio_session is not None:
                audio_session.send_format = updated.send_format
                audio_session.recv_format = updated.recv_format
                audio_session.remote_rtp_host = updated.remote_rtp_host
                audio_session.remote_rtp_port = int(updated.remote_rtp_port)
                audio_session.local_audio_direction = updated.local_audio_direction
                audio_session.remote_audio_connection_held = bool(
                    updated.remote_audio_connection_held
                )
                audio_session.media_generation += 1
                audio_session.update_event.set()
            if video_session is not None and updated_video is not None:
                registry.video_parameter_sets.pop(call_id, None)
                video_session.remote_rtp_host = updated.remote_video_rtp_host
                video_session.remote_rtp_port = int(updated.remote_video_rtp_port)
                video_session.remote_rtcp_host = (
                    updated.remote_video_rtcp_host
                    or updated.remote_video_rtp_host
                )
                video_session.remote_rtcp_port = int(
                    updated.remote_video_rtcp_port
                    or int(updated.remote_video_rtp_port) + 1
                )
                video_session.remote_rtcp_mux = bool(
                    updated.remote_video_rtcp_mux
                )
                video_session.remote_video_payload_types = tuple(
                    updated.remote_video_payload_types
                )
                video_session.video_format = updated_video
                video_session.local_video_format = updated.recv_video_format
                video_session.local_direction = updated.local_video_direction
                video_session.remote_connection_held = bool(
                    updated.remote_video_connection_held
                )
                video_session.media_generation += 1
                video_session.update_event.set()
            store = _ha_softphone_store(hass, endpoint_id)
            if str(store.get("call_id") or "") == call_id:
                store.update(
                    {
                        "audio_direction": updated.local_audio_direction,
                        "audio_connection_held": updated.remote_audio_connection_held,
                        "video_active": bool(
                            updated_video is not None
                            and updated.local_video_direction != "inactive"
                        ),
                        "video_requested": bool(updated_video is not None),
                        "video_negotiated": bool(updated_video is not None),
                        "video_status": (
                            "active"
                            if updated_video is not None
                            and updated.local_video_direction != "inactive"
                            else "inactive"
                        ),
                        "video_failure_reason": "",
                        "video_format": (
                            updated_video.wire_token() if updated_video else ""
                        ),
                        "video_send_format": (
                            updated.send_video_format.wire_token()
                            if updated.send_video_format is not None
                            else ""
                        ),
                        "video_receive_format": (
                            updated.recv_video_format.wire_token()
                            if updated.recv_video_format is not None
                            else ""
                        ),
                        "video_direction": updated.local_video_direction,
                        "video_connection_held": updated.remote_video_connection_held,
                        "last_sip_event": method,
                        "media_renegotiations": int(
                            store.get("media_renegotiations") or 0
                        )
                        + 1,
                    }
                )
                _fire_call_event(
                    hass,
                    dict(
                        store,
                        endpoint_id=endpoint_id,
                        device_id=source_device_id,
                    ),
                    "session",
                )

        return _commit

    client.on_media_update = _prepare_softphone_media_update
    if not use_trunk:
        _enable_reused_sip_tcp_connection(
            hass,
            client,
            uri,
            target=target,
            default_sip_port=int(cfg["sip_port"]),
        )
    registry = _call_registry(hass)
    registry.upsert(
        client.dialog_ids.call_id,
        state=CallState.CALLING.value,
        owner="ha_softphone",
        caller=local_name,
        callee=display_target,
        route_kind="direct",
        direction="outgoing",
        endpoint_id=endpoint_id,
        media_client_id=str(call.data.get("media_client_id") or ""),
        target_endpoint_id=(
            target_endpoint.endpoint_id if target_endpoint is not None else ""
        ),
    )
    try:
        registry.claim_endpoint(
            client.dialog_ids.call_id,
            endpoint_id,
            role="source",
        )
        if (
            target_endpoint is not None
            and target_endpoint.kind is not EndpointKind.BROWSER
        ):
            registry.claim_endpoint(
                client.dialog_ids.call_id,
                target_endpoint.endpoint_id,
                role="destination",
            )
    except EndpointBusyError as err:
        registry.finish_and_pop(
            client.dialog_ids.call_id,
            reason=TerminalReason.BUSY.value,
            state=CallState.BUSY.value,
        )
        await client.close()
        raise ServiceValidationError(str(err)) from err
    _bind_service_call_controller(registry, client.dialog_ids.call_id, call)
    _set_ha_softphone_call_state(
        hass,
        CallState.CALLING.value,
        endpoint_id=endpoint_id,
        session_device_id=source_device_id,
        caller=local_name,
        callee=display_target,
        peer_name=display_target,
        direction="outgoing",
        call_id=client.dialog_ids.call_id,
        sip_transport=_sip_uri_transport(uri).lower(),
        last_sip_event="INVITE",
        sip_uri=route_uri,
        video_requested=video_enabled,
        video_negotiated=False,
        video_status=(
            "degraded"
            if video_failure_reason
            else "requested"
            if video_enabled
            else "inactive"
        ),
        video_failure_reason=video_failure_reason,
    )
    registry.attach_sip_client(
        client.dialog_ids.call_id,
        client.dialog_ids.call_id,
        client,
        role="ha_softphone",
        state=CallState.CALLING.value,
    )
    try:
        result = await client.invite(
            target=uri.user,
            remote_host=uri.host,
            remote_sip_port=uri.port or int(cfg["sip_port"]),
            request_uri=str(uri),
            timeout=SIP_TIMER_B if use_trunk else 8.0,
        )
    except Exception as err:  # noqa: BLE001 - isolate one outbound SIP leg.
        registry.detach_client(client.dialog_ids.call_id)
        await client.close()
        _set_ha_softphone_call_state(
            hass,
            CallState.TRANSPORT_UNREACHABLE.value,
            endpoint_id=endpoint_id,
            session_device_id=source_device_id,
            caller=local_name,
            callee=display_target,
            peer_name=display_target,
            direction="outgoing",
            call_id=client.dialog_ids.call_id,
            reason=TerminalReason.TRANSPORT_UNREACHABLE.value,
            terminal_reason=TerminalReason.TRANSPORT_UNREACHABLE.value,
            last_sip_event="INVITE_ERROR",
            sip_uri=route_uri,
        )
        registry.finish_and_pop(
            client.dialog_ids.call_id,
            reason=TerminalReason.TRANSPORT_UNREACHABLE.value,
            state=CallState.TRANSPORT_UNREACHABLE.value,
        )
        raise ServiceValidationError(
            f"could not start SIP call to {display_target}"
        ) from err
    if registry.sip_clients.get(client.dialog_ids.call_id) is not client:
        await client.close()
        return
    if result == TerminalReason.TRANSPORT_UNREACHABLE.value and route.entry is not None and route.entry.metadata.get("registered"):
        await _mark_sip_account_unreachable(hass, route.entry.id)
    public_result = _sip_public_state(result)
    # Publish the first result before starting the detached final-response
    # watcher. A fast peer can place 180 and 200 on the socket back-to-back:
    # if the watcher runs first it publishes IN_CALL, then this coroutine used
    # to regress the same call to REMOTE_RINGING from the earlier 180 result.
    # Keeping the signaling order here makes the backend snapshot monotonic;
    # the card remains a plain mirror of that authoritative state.
    if public_result == CallState.REMOTE_RINGING.value or result == "ringing":
        _set_ha_softphone_call_state(
            hass,
            CallState.REMOTE_RINGING.value,
            endpoint_id=endpoint_id,
            session_device_id=source_device_id,
            caller=local_name,
            callee=display_target,
            peer_name=display_target,
            direction="outgoing",
            call_id=client.dialog_ids.call_id,
            sip_status_code=180,
            last_sip_event="SIP_RESPONSE",
            sip_uri=route_uri,
        )
    elif public_result == CallState.IN_CALL.value and client.dialog is not None:
        video_active = bool(
            client.dialog.video_format is not None
            and client.dialog.local_video_direction != "inactive"
        )
        video_status = (
            "degraded"
            if video_failure_reason
            else "active"
            if video_active
            else "rejected"
            if video_enabled
            else "inactive"
        )
        final_video_failure_reason = video_failure_reason or (
            "remote_video_rejected" if video_enabled and not video_active else ""
        )
        _set_ha_softphone_call_state(
            hass,
            CallState.IN_CALL.value,
            endpoint_id=endpoint_id,
            session_device_id=source_device_id,
            caller=local_name,
            callee=display_target,
            peer_name=display_target,
            direction="outgoing",
            call_id=client.dialog_ids.call_id,
            selected_tx_format=client.dialog.send_format.audio_format.wire_token(),
            selected_rx_format=client.dialog.recv_format.audio_format.wire_token(),
            selected_tx_rtp_format=client.dialog.send_format.wire_token(),
            selected_rx_rtp_format=client.dialog.recv_format.wire_token(),
            audio_direction=client.dialog.local_audio_direction,
            audio_connection_held=client.dialog.remote_audio_connection_held,
            video_active=video_active,
            video_requested=video_enabled,
            video_negotiated=video_active,
            video_status=video_status,
            video_failure_reason=final_video_failure_reason,
            video_format=(client.dialog.video_format.wire_token() if client.dialog.video_format else ""),
            video_send_format=(
                client.dialog.send_video_format.wire_token()
                if client.dialog.send_video_format is not None
                else ""
            ),
            video_receive_format=(
                client.dialog.recv_video_format.wire_token()
                if client.dialog.recv_video_format is not None
                else ""
            ),
            video_direction=client.dialog.local_video_direction,
            sip_status_code=200,
            last_sip_event="SIP_RESPONSE",
            sip_uri=route_uri,
        )
    elif public_result not in {CallState.REMOTE_RINGING.value, CallState.IN_CALL.value}:
        terminal_reason = _sip_terminal_reason(result, public_result)
        _set_ha_softphone_call_state(
            hass,
            public_result,
            endpoint_id=endpoint_id,
            session_device_id=source_device_id,
            caller=local_name,
            callee=display_target,
            peer_name=display_target,
            direction="outgoing",
            call_id=client.dialog_ids.call_id,
            reason=terminal_reason,
            terminal_reason=terminal_reason,
            sip_status_code=client.last_sip_status_code,
            last_sip_event=client.last_sip_event or "SIP_RESPONSE",
            sip_uri=route_uri,
        )
    await _track_outbound_sip_client(
        hass,
        client=client,
        result=result,
        target=target,
        sip_uri=route_uri,
        endpoint_id=endpoint_id,
        local_name=local_name,
        session_device_id=source_device_id,
        video_requested=video_enabled,
        video_failure_reason=video_failure_reason,
    )
    _LOGGER.info("SIP call target=%s uri=%s result=%s", target, route_uri, result)


async def _handle_sip_route_service(call: ServiceCall) -> None:
    _set_pending_route_decision(call.hass, dict(call.data))


async def _handle_select_inbound_destination_service(call: ServiceCall) -> None:
    """Select the initial destination of one pending inbound route request."""
    from homeassistant.exceptions import ServiceValidationError

    from .automation_routing import resolve_pending_route_call_id

    data = dict(call.data)
    registry = _call_registry(call.hass)
    try:
        call_id = resolve_pending_route_call_id(
            str(data.get("call_id") or ""), registry.pending_routes
        )
    except ValueError as err:
        raise ServiceValidationError(str(err)) from err
    data["call_id"] = call_id
    data["action"] = "forward"
    _set_pending_route_decision(call.hass, data)


async def _handle_sip_forward_service(call: ServiceCall) -> None:
    """Forward a SIP call through HA's dial plan/B2BUA path."""
    from homeassistant.exceptions import ServiceValidationError

    from .automation_routing import resolve_forward_call_id

    data = dict(call.data)
    registry = _call_registry(call.hass)
    endpoint_id, _endpoint = _service_browser_endpoint(
        call.hass, call, strict=True
    )
    await _require_phone_service_control(
        call.hass,
        call,
        endpoint=_endpoint,
    )
    pending_routes = {
        call_id: route
        for call_id, route in registry.pending_routes.items()
        if _call_belongs_to_endpoint(registry, call_id, endpoint_id)
    }
    pending_invites = {
        call_id: invite
        for call_id, invite in registry.pending_invites.items()
        if _call_belongs_to_endpoint(registry, call_id, endpoint_id)
    }
    try:
        call_id = resolve_forward_call_id(
            str(data.get("call_id") or ""),
            pending_routes,
            pending_invites,
        )
    except ValueError as err:
        raise ServiceValidationError(str(err)) from err
    if not _call_belongs_to_endpoint(registry, call_id, endpoint_id):
        raise ServiceValidationError(
            f"call_id {call_id} belongs to another phone endpoint"
        )
    if not data.get("call_id"):
        context = registry.event_context(call_id)
        data["call_id"] = call_id
        if context is not None:
            data.setdefault("expected_state", context.state)
            data.setdefault("expected_sequence", context.sequence)
    if call_id and call_id in _pending_routes(call.hass):
        route = _pending_routes(call.hass)[call_id]
        if route.get("ring_group_endpoint_ids"):
            # A ring-group coordinator currently owns the candidate legs.
            # Request an explicit handoff and wait until it has cancelled and
            # released those legs before the normal forwarding primitive
            # claims the same source dialog.
            handoff = asyncio.get_running_loop().create_future()
            route["forward_handoff"] = handoff
            data["action"] = "forward"
            _set_pending_route_decision(call.hass, data)
            try:
                await asyncio.wait_for(handoff, timeout=5.0)
            except TimeoutError as err:
                raise ServiceValidationError(
                    f"ring-group route for call_id {call_id} did not release ownership"
                ) from err
            previous_coordinator = call.hass.data.get(DOMAIN, {}).get(
                "forward_tasks", {}
            ).get(call_id)
            if previous_coordinator is not None and not previous_coordinator.done():
                await asyncio.gather(previous_coordinator, return_exceptions=True)
        else:
            data["action"] = "forward"
            _set_pending_route_decision(call.hass, data)
            return
    if call_id:
        callback = call.hass.data.get(DOMAIN, {}).get("async_forward_call")
        if callback is None:
            raise ServiceValidationError("SIP endpoint is not running")
        destination = str(
            data.get("destination")
            or data.get("target")
            or data.get("call")
            or ""
        ).strip()
        await callback(
            call_id=call_id,
            destination=destination,
            on_failure=str(data.get("on_failure") or "resume"),
            expected_state=str(data.get("expected_state") or ""),
            expected_sequence=int(data.get("expected_sequence") or 0),
        )
        return
    raise ServiceValidationError(f"call_id {call_id} is not forwardable")


async def _handle_sip_set_deadline_service(call: ServiceCall) -> None:
    from .call_deadlines import async_set_call_deadline

    await async_set_call_deadline(call.hass, dict(call.data))


async def _handle_sip_cancel_deadline_service(call: ServiceCall) -> None:
    from .call_deadlines import cancel_call_deadline

    cancel_call_deadline(call.hass, str(call.data.get("call_id") or ""))


async def _async_register_services(hass: HomeAssistant) -> None:
    """Register HA services for SIP phone control."""
    from .account_services import build_account_service_handlers
    from .phonebook_services import build_phonebook_service_handlers
    from .services import async_register_services

    account_handlers = build_account_service_handlers(_refresh_and_push_phonebook)
    phonebook_handlers = build_phonebook_service_handlers(_refresh_and_push_phonebook)

    await async_register_services(
        hass,
        {
            "purge_devices": _handle_purge_devices_service,
            "answer": _handle_sip_answer_service,
            "decline": _handle_sip_decline_service,
            "hangup": _handle_sip_hangup_service,
            **phonebook_handlers,
            "set_dnd": _handle_set_dnd_service,
            "set_ha_softphone_settings": _handle_set_ha_softphone_settings_service,
            "call": _handle_sip_call_target_service,
            "forward": _handle_sip_forward_service,
            "route": _handle_sip_route_service,
            "select_inbound_destination": _handle_select_inbound_destination_service,
            "set_deadline": _handle_sip_set_deadline_service,
            "cancel_deadline": _handle_sip_cancel_deadline_service,
            **account_handlers,
        },
    )


async def _async_apply_assist_intents(hass: HomeAssistant, enabled: bool) -> None:
    """Register optional Assist intent handlers only when explicitly enabled."""
    if enabled:
        from .assist_intents import async_register_assist_intents

        async_register_assist_intents(hass)
        return

    if hass.data.get(DOMAIN, {}).get("assist_intents_registered"):
        from .assist_intents import async_unregister_assist_intents

        async_unregister_assist_intents(hass)


async def _async_setup_shared(hass: HomeAssistant, config: dict | None = None) -> None:
    """Shared setup logic for both YAML and config entry."""
    bucket = hass.data.setdefault(DOMAIN, {})
    # Views survive config-entry reloads; reopen media ownership only after a
    # new setup begins.
    bucket.setdefault("media_shutdown", asyncio.Event()).clear()
    if bucket.get("initialized"):
        # Services, websocket commands and HTTP views stay registered across a
        # config-entry reload. The event listeners are explicitly removed by
        # unload, so restore just those idempotent subscriptions here.
        _register_esp_state_event_bridge(hass)
        _register_phonebook_service_event_sync(hass)
        if _get_transport_config(hass).get(CONF_EXPERIMENTAL_VIDEO, False):
            from .video_ws_view import async_register_video_ws_view

            async_register_video_ws_view(hass)
        return

    bucket["initialized"] = True

    await _async_load_ha_softphone_store(hass)
    async_register_websocket_api(hass)
    from .audio_ws_view import async_register_audio_ws_view
    async_register_audio_ws_view(hass)
    if _get_transport_config(hass).get(CONF_EXPERIMENTAL_VIDEO, False):
        from .video_ws_view import async_register_video_ws_view

        async_register_video_ws_view(hass)
    await _async_register_services(hass)
    _register_esp_state_event_bridge(hass)
    _register_phonebook_service_event_sync(hass)

    # Sensor platform is forwarded per config entry; YAML setup gets only
    # services + websocket API.

    async def _register_frontend(_event: Event | None = None) -> None:
        from .frontend import JSModuleRegistration
        registration = JSModuleRegistration(hass)
        await registration.async_register()

    if hass.state == CoreState.running:
        await _register_frontend(None)
    else:
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STARTED, _register_frontend)

    _LOGGER.info("VoIP Stack loaded (SIP softphone + SIP B2BUA/router)")


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up VoIP Stack defaults from configuration.yaml."""
    hass.data.setdefault(DOMAIN, {})["transport_config"] = {
        "sip_port": VOIP_STACK_SIP_PORT,
        "rtp_port": VOIP_STACK_RTP_PORT,
    }
    hass.data[DOMAIN][CONF_DEBUG_MODE] = False
    hass.data[DOMAIN]["assist_config"] = _entry_assist_config(None)
    hass.data[DOMAIN]["trunk_config"] = _entry_trunk_config(None)
    hass.data[DOMAIN]["sip_port"] = VOIP_STACK_SIP_PORT
    await _async_setup_shared(hass, config)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up VoIP Stack from a config entry (UI setup)."""
    async_ensure_phone_subentries(hass, entry)
    endpoint_registry = async_setup_endpoint_registry(hass, entry)
    from .local_softphone_runtime import async_setup_local_softphone_bridge

    async_setup_local_softphone_bridge(hass)
    cfg = _entry_transport_config(entry)
    assist_cfg = _entry_assist_config(entry)
    trunk_cfg = _entry_trunk_config(entry)
    hass.data.setdefault(DOMAIN, {})["transport_config"] = cfg
    hass.data[DOMAIN]["assist_config"] = assist_cfg
    hass.data[DOMAIN]["trunk_config"] = trunk_cfg
    hass.data[DOMAIN]["sip_port"] = cfg["sip_port"]
    hass.data[DOMAIN][CONF_DEBUG_MODE] = bool(entry.data.get(CONF_DEBUG_MODE, False))
    hass.data[DOMAIN]["manual_roster_entries"] = _manual_roster_entries(hass)
    await _async_setup_shared(hass)
    for subentry in phone_subentries(entry):
        endpoint = endpoint_registry.get(
            str(subentry.data.get("endpoint_id") or "")
        )
        if endpoint is None or endpoint.kind is not EndpointKind.BROWSER:
            continue
        await _async_load_ha_softphone_store(
            hass,
            entry,
            endpoint_id=endpoint.endpoint_id,
            endpoint_data=dict(subentry.data),
        )
    await _async_apply_assist_intents(
        hass,
        bool(entry.data.get(CONF_ASSIST_INTENTS, False)),
    )
    if assist_cfg[CONF_ASSIST_ENDPOINT_ENABLED]:
        from homeassistant.components.assist_pipeline.pipeline import async_get_pipeline

        pipeline_id = assist_cfg[CONF_ASSIST_PIPELINE]
        pipeline = async_get_pipeline(
            hass,
            pipeline_id=None if pipeline_id in {"", "preferred"} else pipeline_id,
        )
        assist_cfg["name"] = pipeline.name
    if not await _async_start_sip_endpoint(hass):
        raise ConfigEntryError(
            f"Failed to bind SIP port {cfg['sip_port']}. Another SIP "
            "endpoint may already be listening on that port."
        )
    await _async_start_sip_trunk(hass)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    bucket = hass.data.setdefault(DOMAIN, {})
    bucket["entry_runtime_signature"] = _entry_runtime_signature(entry)
    bucket["entry_phone_signature"] = _entry_phone_signature(entry)
    bucket["entry_phone_records"] = {
        str(subentry.data.get("endpoint_id") or "").strip(): dict(subentry.data)
        for subentry in phone_subentries(entry)
    }
    bucket["entry_contacts_signature"] = tuple(
        dict(item)
        for item in entry.data.get(CONF_PHONEBOOK_CONTACTS, [])
        if isinstance(item, dict)
    )
    entry.async_on_unload(entry.add_update_listener(_async_config_entry_updated))
    create_runtime_task(hass, _deferred_phonebook_sync(hass))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if not unload_ok:
        return False
    await _async_apply_assist_intents(hass, False)

    # Stop sessions / bridges before tearing down listeners; otherwise
    # orphaned transports leak sockets across config-entry reload.
    from .websocket_api import _async_shutdown_all
    await _async_shutdown_all(hass)

    # The authoritative PBX runtime stops calls, trunk and listeners in one
    # ordered cleanup barrier.  Do not tear the trunk out from under live call
    # sessions before that owner begins shutdown.
    await _async_stop_sip_endpoint(hass)
    unsub = hass.data.get(DOMAIN, {}).pop("esp_state_event_bridge_unsub", None)
    if unsub is not None:
        unsub()
    unsub = hass.data.get(DOMAIN, {}).pop("phonebook_service_event_unsub", None)
    if unsub is not None:
        unsub()
    unsub = hass.data.get(DOMAIN, {}).pop("pending_endpoint_removal_unsub", None)
    if unsub is not None:
        unsub()
    hass.data.get(DOMAIN, {}).pop("pending_endpoint_removals", None)
    hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unload_ok


_REMOVED_ENTRY_RUNTIME_KEYS = (
    # Configured endpoint graph and its dynamic HA entity adapters.
    "endpoint_registry",
    "endpoint_subentry_ids",
    "pending_endpoint_removals",
    "endpoint_connectivity_entity_manager",
    "endpoint_call_event_entity_manager",
    "endpoint_call_state_entity_manager",
    "endpoint_dnd_entity_manager",
    # Per-entry browser-phone state. A later entry must start from its own
    # subentries instead of inheriting a removed kiosk or its page presence.
    "ha_softphone",
    "ha_softphones",
    "ha_softphone_presence",
    "ha_softphone_presence_events",
    "ha_softphone_start_locks",
    "ha_softphone_endpoint_sensor",
    "ha_softphone_call_state_sensor",
    "phonebook_sensor",
    # SIP/B2BUA and local logical-phone runtime.
    "call_registry",
    "pbx_runtime",
    "sip_bridge_state",
    "sip_registrar",
    "sip_endpoint",
    "sip_server",
    "sip_tcp_server",
    "sip_trunk",
    "sip_rtp_port_pool",
    "sip_rtp_next_port",
    "trunk_info_queues",
    "trunk_closed_calls",
    "active_audio_sessions",
    "active_video_sessions",
    "audio_ws_owners",
    "audio_ws_owner_lock",
    "video_ws_owners",
    "video_ws_owner_lock",
    "media_identity_locks",
    "media_controller_lock",
    "local_softphone_bridge",
    "local_softphone_bridge_unsub",
    "conference_manager",
    "async_forward_call",
    "async_ring_conference_members",
    "async_start_ring_group_from_ha",
    "forward_tasks",
    "forward_claims",
    "call_deadlines",
    "runtime_tasks",
    "video_transcoder_active",
    "video_transcoder_lock",
    # Entry-derived configuration and resolver caches.
    "transport_config",
    "assist_config",
    "trunk_config",
    "sip_port",
    CONF_DEBUG_MODE,
    "manual_roster_entries",
    "device_resolver",
    "esp_state_event_generations",
    "entry_runtime_signature",
    "entry_phone_signature",
    "entry_phone_records",
    "entry_contacts_signature",
)


async def async_remove_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Forget all runtime state owned by a permanently removed entry.

    Services, websocket commands and HTTP views are process-wide Home
    Assistant registrations and intentionally survive. ``initialized`` and
    the view-registration sentinels therefore remain in the domain bucket so
    adding the integration again cannot attempt duplicate registrations.
    """
    bucket = hass.data.get(DOMAIN)
    if not isinstance(bucket, dict):
        return

    # Wake any bounded offline wait before discarding its Event object. The
    # normal unload has already cancelled runtime tasks, but this keeps the
    # final-removal hook safe when called by a minimal HA test harness too.
    waiters = bucket.get("ha_softphone_presence_events")
    if isinstance(waiters, dict):
        for waiter in tuple(waiters.values()):
            set_waiter = getattr(waiter, "set", None)
            if callable(set_waiter):
                set_waiter()

    registry = bucket.get("call_registry")
    clear_runtime = getattr(registry, "clear_runtime", None)
    if callable(clear_runtime):
        clear_runtime()

    unsubscribe = bucket.pop("local_softphone_bridge_unsub", None)
    if callable(unsubscribe):
        unsubscribe()

    pending_unsubscribe = bucket.pop("pending_endpoint_removal_unsub", None)
    if callable(pending_unsubscribe):
        pending_unsubscribe()

    for key in _REMOVED_ENTRY_RUNTIME_KEYS:
        bucket.pop(key, None)
    bucket.pop(entry.entry_id, None)


async def _async_start_sip_trunk(hass: HomeAssistant) -> bool:
    from .trunk_runtime import async_start_sip_trunk

    return await async_start_sip_trunk(hass, local_ip=await _ha_advertise_host(hass))


async def _async_stop_sip_trunk(hass: HomeAssistant) -> None:
    from .trunk_runtime import async_stop_sip_trunk

    await async_stop_sip_trunk(hass)


async def _async_start_sip_endpoint(hass: HomeAssistant) -> bool:
    from .endpoint_runtime import async_start_sip_endpoint

    return await async_start_sip_endpoint(hass)


async def _async_stop_sip_endpoint(hass: HomeAssistant) -> None:
    from .endpoint_lifecycle import async_stop_sip_endpoint

    await async_stop_sip_endpoint(hass)
