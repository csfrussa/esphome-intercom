"""VoIP Stack integration for Home Assistant.

HA is a SIP softphone and SIP B2BUA/router for ESPHome SIP phones. Public call
control is expressed in SIP/SDP/RTP terms only; logical targets are resolved by
the central phonebook and routed through HA as SIP dialogs when needed.
"""

import asyncio
from dataclasses import replace
import logging

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STARTED, Platform
from homeassistant.core import HomeAssistant, CoreState, Event, ServiceCall
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
    pending_routes as _pending_routes,
)
from .config_entry_runtime import (
    async_config_entry_updated as _async_config_entry_updated,
    async_deferred_phonebook_sync as _deferred_phonebook_sync,
    async_refresh_and_push_phonebook as _refresh_and_push_phonebook,
    entry_phone_signature as _entry_phone_signature,
    entry_runtime_signature as _entry_runtime_signature,
    register_phonebook_service_event_sync as _register_phonebook_service_event_sync,
)
from .const import (
    CONF_ASSIST_ENDPOINT_ENABLED,
    CONF_ASSIST_PIPELINE,
    CONF_ASSIST_INTENTS,
    CONF_DEBUG_MODE,
    CONF_EXPERIMENTAL_VIDEO,
    CONF_PHONEBOOK_CONTACTS,
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
    async_resolve_source_device as _resolve_source_device_from_call,
    async_resolve_target_device as _resolve_target_device,
)
from .fsm import (
    CallState,
    TerminalReason,
    sip_public_state as _sip_public_state,
    sip_terminal_reason as _sip_terminal_reason,
)
from .media_ports import (
    allocate_sip_rtp_port as _allocate_sip_rtp_port,
    reserve_sip_video_media,
)
from .outbound_lifecycle import (
    HA_SOFTPHONE_ACTIVE_STATES,
    async_prepare_ha_outbound_call as _async_prepare_ha_outbound_call,
    async_track_outbound_sip_client as _track_outbound_sip_client,
)
from .sip_runtime import (
    enable_reused_tcp_connection as _enable_reused_sip_tcp_connection,
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
from .softphone_commands import (
    async_decline_browser_call as _decline_browser_call,
    async_resolve_browser_call_command as _resolve_browser_call_command,
    async_try_esp_answer as _try_esp_answer,
    async_try_esp_end_call as _try_esp_end_call,
    bind_service_call_controller as _bind_service_call_controller,
)
from .softphone_answer import async_answer_browser_call as _answer_browser_call
from .softphone_termination import (
    async_hangup_browser_call as _hangup_browser_call,
)
from .phone_config import (
    async_ensure_phone_subentries,
    async_load_legacy_default_phone_overrides,
    async_setup_endpoint_registry,
    phone_subentries,
)
from .router import (
    RouteAction,
    RouteReason,
    ha_uri_for,
    resolve_ha_router,
)
from .route_decisions import set_pending_route_decision as _set_pending_route_decision
from .store import manual_roster_entries as _manual_roster_entries
from .websocket_api import (
    async_register_websocket_api,
    _async_load_ha_softphone_store,
    _get_voip_devices,
    _fire_call_event,
    async_set_ha_softphone_settings,
    _ha_softphone_store,
    _set_ha_softphone_call_state,
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
    if await _try_esp_answer(call):
        return
    command = await _resolve_browser_call_command(hass, call)
    await _answer_browser_call(hass, call, command)


async def _handle_sip_decline_service(call: ServiceCall) -> None:
    hass: HomeAssistant = call.hass
    if await _try_esp_end_call(call, operation="decline"):
        return
    command = await _resolve_browser_call_command(hass, call)
    await _decline_browser_call(hass, call, command)


async def _handle_sip_hangup_service(call: ServiceCall) -> None:
    hass: HomeAssistant = call.hass
    if await _try_esp_end_call(call, operation="hangup"):
        return
    command = await _resolve_browser_call_command(hass, call)
    await _hangup_browser_call(hass, command)


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
                audio_session.dtmf_payload_type = updated.dtmf_payload_type
                audio_session.dtmf_events = updated.dtmf_events
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
    from .dtmf_events import attach_direct_client_dtmf_events

    attach_direct_client_dtmf_events(
        hass,
        client,
        call_id=client.dialog_ids.call_id,
        caller=local_name,
        callee=display_target,
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
