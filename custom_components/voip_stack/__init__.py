"""VoIP Stack integration for Home Assistant.

HA is a SIP softphone and SIP B2BUA/router for ESPHome SIP phones. Public call
control is expressed in SIP/SDP/RTP terms only; logical targets are resolved by
the central phonebook and routed through HA as SIP dialogs when needed.
"""

import asyncio
import contextlib
from dataclasses import replace
import logging
import time

import voluptuous as vol

from homeassistant.core import HomeAssistant, CoreState, Event, ServiceCall, callback
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform, EVENT_HOMEASSISTANT_STARTED, EVENT_STATE_CHANGED
from homeassistant.exceptions import ConfigEntryError

PLATFORMS: list[Platform] = [Platform.SENSOR]
from homeassistant.helpers import config_validation as cv
from homeassistant.components import network

from .config import (
    debug_mode as _debug_mode,
    entry_transport_config as _entry_transport_config,
    entry_trunk_config as _entry_trunk_config,
    transport_config as _get_transport_config,
    trunk_config as _get_trunk_config,
    trunk_enabled as _trunk_enabled,
)
from .const import (
    CONF_ASSIST_INTENTS,
    CONF_DEBUG_MODE,
    CONF_REGISTRAR_ENABLED,
    CONF_TRUNK_AUTH_USERNAME,
    CONF_TRUNK_DOMAIN,
    CONF_TRUNK_DTMF_ENABLED,
    CONF_TRUNK_DTMF_ROUTES,
    CONF_TRUNK_DTMF_TERMINATOR,
    CONF_TRUNK_DTMF_TIMEOUT_MS,
    CONF_TRUNK_ENABLED,
    CONF_TRUNK_EXPIRES,
    CONF_TRUNK_INBOUND_DEFAULT_TARGET,
    CONF_TRUNK_OUTBOUND_PROXY,
    CONF_TRUNK_PASSWORD,
    CONF_TRUNK_PORT,
    CONF_TRUNK_SERVER,
    CONF_TRUNK_TRANSPORT,
    CONF_TRUNK_USERNAME,
    DOMAIN,
    HA_PEER_FALLBACK_NAME,
    HA_SOFTPHONE_DEVICE_ID,
    HA_SOFTPHONE_ENDPOINT_ENTITY_ID,
    VOIP_STACK_RTP_PORT,
    VOIP_STACK_SIP_PORT,
)
from .dtmf import parse_dtmf_route_map
from .device_resolver import get_resolver, parse_voip_endpoint
from .endpoint_lifecycle import call_registry as _call_registry
from .endpoint_routing import (
    device_formats as _device_formats,
    peer_audio_formats as _peer_audio_formats,
    peer_for_target as _peer_for_target,
    roster_from_peers as _roster_from_peers,
    roster_entry_formats as _roster_entry_formats,
    same_route_name as _same_route_name,
    sip_target_audio_profile as _sip_target_audio_profile,
)
from .fsm import (
    CallState,
    TerminalReason,
    sip_failure_response as _sip_failure_response,
    sip_phone_state,
    sip_public_state as _sip_public_state,
    sip_terminal_reason as _sip_terminal_reason,
)
from .media_ports import allocate_sip_rtp_port as _allocate_sip_rtp_port
from .audio_format import (
    HA_SIP_PCM_FORMATS,
    HA_SIP_PCM_RX_FORMATS,
    HA_SIP_PCM_TX_FORMATS,
    HA_TRUNK_AUDIO_FORMATS,
)
from .peer import Peer
from .phonebook_runtime import (
    available_esphome_services as _available_esphome_services,
    format_entry_unified as _format_entry_unified,
    push_roster_json_to_esps as _push_roster_json_to_esps,
)
from .router import (
    CallContext,
    RouteAction,
    RouteHintSource,
    RouteReason,
    ha_uri_for,
    resolve_ha_router,
    route_inbound_trunk,
)
from .route_decisions import set_pending_route_decision as _set_pending_route_decision
from .sip_bridge import build_invite_client_relay
from .store import (
    config_entry as _config_entry,
    manual_roster_entries as _manual_roster_entries,
    sip_accounts as _sip_accounts,
)
from .websocket_api import (
    async_register_websocket_api,
    _async_load_ha_softphone_store,
    _get_voip_devices,
    _fire_call_event,
    _async_save_ha_softphone_store,
    async_set_ha_softphone_groups,
    _ha_softphone_dnd,
    _ha_softphone_state,
    _ha_softphone_store,
    _set_ha_softphone_call_state,
    _set_sip_bridge_call_state,
)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


_LOGGER = logging.getLogger(__name__)
SIP_ROUTE_DECISION_TIMEOUT = 1.5
HA_SOFTPHONE_ACTIVE_STATES = {
    CallState.CALLING.value,
    CallState.REMOTE_RINGING.value,
    CallState.RINGING.value,
    CallState.CONNECTING.value,
    CallState.IN_CALL.value,
    CallState.TERMINATING.value,
}


def _pending_routes(hass: HomeAssistant) -> dict:
    return _call_registry(hass).pending_routes


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
            registrar.registrations.pop(key, None)
            removed = True
    if removed:
        _LOGGER.info("SIP registrar contact marked unreachable user=%s", username)
        await _refresh_and_push_phonebook(hass)


def _ha_softphone_has_active_call(hass: HomeAssistant, *, ignore_call_id: str = "") -> bool:
    store = _ha_softphone_store(hass)
    if ignore_call_id and str(store.get("call_id") or "") == ignore_call_id:
        return False
    state = str(store.get("state") or CallState.IDLE.value)
    return bool(store.get("session_device_id") or state in HA_SOFTPHONE_ACTIVE_STATES)


def _single_pending_route_call_id(hass: HomeAssistant) -> str:
    routes = _pending_routes(hass)
    return next(iter(routes)) if len(routes) == 1 else ""


def _ha_peer_name(hass: HomeAssistant) -> str:
    """Return the HA phonebook peer name.

    HA normally always has a configured location_name. The default is only for
    malformed/empty local config and avoids a hardcoded "Home Assistant" peer
    identity.
    """
    return (hass.config.location_name or "").strip() or HA_PEER_FALLBACK_NAME


async def _ha_advertise_host(hass: HomeAssistant) -> str:
    """Return the IP/host HA should publish to ESP phonebooks.

    `network.async_get_announce_addresses()` is fine on a flat LAN, but it can
    pick the wrong interface in routed/LXC/NAT installs. The config-flow
    override is authoritative when set.
    """
    cfg = _get_transport_config(hass)
    configured = (cfg.get("advertise_host") or "").strip()
    if configured:
        return configured
    addresses = await network.async_get_announce_addresses(hass)
    return addresses[0] if addresses else ""


def _resolve_esphome_route_id(hass: HomeAssistant, host: str) -> str:
    """ESPHome node_name slug for `host`, or '' if not configured."""
    return get_resolver(hass).route_id_for_host(host)


def _state_entity_is_busy(hass: HomeAssistant, device: dict) -> bool:
    """True when the ESP-published FSM state says this device is not idle."""
    state_entity = (device.get("entities") or {}).get("voip_state")
    if not state_entity:
        return False
    state = hass.states.get(state_entity)
    if state is None:
        return False
    return str(state.state or "").strip().lower() in {
        "calling",
        "remote_ringing",
        "ringing",
        "connecting",
        "in_call",
        "terminating",
    }


def _device_entity_state(hass: HomeAssistant, device: dict, key: str) -> str:
    entity_id = (device.get("entities") or {}).get(key)
    if not entity_id:
        return ""
    state = hass.states.get(entity_id)
    value = (state.state if state is not None else "").strip()
    return "" if value.lower() in ("unknown", "unavailable") else value


def _sip_active_dialog_count(server: object | None) -> int:
    count = getattr(server, "active_dialog_count", None)
    if callable(count):
        try:
            return int(count())
        except Exception:
            return 0
    dialogs = getattr(server, "active_dialogs", None)
    if isinstance(dialogs, dict):
        return len(dialogs)
    endpoint = getattr(server, "endpoint", None)
    dialogs = getattr(endpoint, "active_dialogs", None)
    if isinstance(dialogs, dict):
        return len(dialogs)
    endpoints = getattr(server, "endpoints", None)
    if isinstance(endpoints, set):
        total = 0
        for endpoint in endpoints:
            endpoint_dialogs = getattr(endpoint, "active_dialogs", None)
            if isinstance(endpoint_dialogs, dict):
                total += len(endpoint_dialogs)
        return total
    return 0


def _sip_servers(hass: HomeAssistant) -> list[object]:
    bucket = hass.data.get(DOMAIN, {})
    servers: list[object] = []
    endpoint = bucket.get("sip_endpoint")
    if endpoint is not None:
        servers.append(endpoint)
    else:
        servers.extend(server for server in (bucket.get("sip_server"), bucket.get("sip_tcp_server")) if server is not None)
    trunk_endpoint = getattr(bucket.get("sip_trunk"), "inbound_endpoint", None)
    if trunk_endpoint is not None:
        servers.append(trunk_endpoint)
    return servers


def _sip_send_final_response(
    hass: HomeAssistant,
    call_id: str,
    status: int,
    reason: str,
    *,
    answer_sdp: str = "",
    decline_reason: str = "",
) -> bool:
    for server in _sip_servers(hass):
        send = getattr(server, "send_final_response", None)
        if callable(send) and send(
            call_id,
            status,
            reason,
            answer_sdp=answer_sdp,
            decline_reason=decline_reason,
        ):
            return True
    return False


def _sip_send_bye(hass: HomeAssistant, call_id: str = "") -> bool:
    for server in _sip_servers(hass):
        send_bye = getattr(server, "send_bye", None)
        if callable(send_bye) and send_bye(call_id):
            return True
    return False


async def _terminate_sip_bridge(
    hass: HomeAssistant,
    call_id: str,
    *,
    terminal_reason: str = TerminalReason.LOCAL_HANGUP.value,
) -> tuple[bool, str, str, bool, bool]:
    from .bridge_manager import async_terminate_sip_bridge

    return await async_terminate_sip_bridge(
        hass,
        call_id,
        terminal_reason=terminal_reason,
        send_bye=lambda source_call_id: _sip_send_bye(hass, source_call_id),
    )


async def _async_emit_esp_state_event(
    hass: HomeAssistant,
    entity_id: str,
    state: str,
    old_state: str,
    delay: float = 0.0,
) -> None:
    """Mirror ESP-published voip_state changes onto the public call bus."""
    if delay > 0:
        import asyncio

        await asyncio.sleep(delay)
    devices = await _get_voip_devices(hass)
    device = next(
        (
            item
            for item in devices
            if (item.get("entities") or {}).get("voip_state") == entity_id
        ),
        None,
    )
    payload = {
        "state": state,
        "old_state": old_state,
        "entity_id": entity_id,
        "direction": "",
        "call_id": "",
    }
    if device is not None:
        entities = device.get("entities") or {}
        caller = _device_entity_state(hass, device, "incoming_caller")
        destination = _device_entity_state(hass, device, "destination")
        reason = _device_entity_state(hass, device, "last_reason")
        payload.update(
            {
                "device_id": device.get("device_id", ""),
                "peer_name": device.get("name", ""),
                "local_name": device.get("name", ""),
                "caller": caller,
                "callee": destination,
                "destination": destination,
                "reason": reason,
                "endpoint": _device_entity_state(hass, device, "voip_endpoint"),
                "caller_entity_id": entities.get("incoming_caller", ""),
                "destination_entity_id": entities.get("destination", ""),
                "last_reason_entity_id": entities.get("last_reason", ""),
            }
        )
        if state.strip().lower() in ("ringing", "incoming"):
            payload["direction"] = "incoming"
        elif state.strip().lower() in ("calling", "remote_ringing"):
            payload["direction"] = "outgoing"
    _fire_call_event(hass, payload, "esp")


def _register_esp_state_event_bridge(hass: HomeAssistant) -> None:
    """Forward ESP voip_state entity changes to voip_stack.call_event."""
    bucket = hass.data.setdefault(DOMAIN, {})
    if bucket.get("esp_state_event_bridge_unsub") is not None:
        return

    @callback
    def _on_state_changed(event: Event) -> None:
        entity_id = str(event.data.get("entity_id") or "")
        if "voip_state" not in entity_id:
            return
        old = event.data.get("old_state")
        new = event.data.get("new_state")
        if new is None:
            return
        old_value = "" if old is None else str(old.state or "")
        new_value = str(new.state or "")
        if old_value == new_value:
            return
        if new_value.lower() in ("unknown", "unavailable"):
            return
        terminal_delay = 0.2 if new_value.strip().lower() in ("idle", "ended", "declined") else 0.0
        hass.async_create_task(
            _async_emit_esp_state_event(hass, entity_id, new_value, old_value, terminal_delay)
        )

    bucket["esp_state_event_bridge_unsub"] = hass.bus.async_listen(
        EVENT_STATE_CHANGED,
        _on_state_changed,
    )


def _device_is_phonebook_available(hass: HomeAssistant, device: dict) -> bool:
    """True when the ESP should be advertised to other VoIP peers."""
    entities = device.get("entities") or {}
    endpoint_entity = entities.get("voip_endpoint")
    if not endpoint_entity:
        return False
    endpoint_state = hass.states.get(endpoint_entity)
    if endpoint_state is None or str(endpoint_state.state).strip().lower() in ("", "unknown", "unavailable"):
        return False

    state_entity = entities.get("voip_state")
    if state_entity:
        state = hass.states.get(state_entity)
        if state is None or str(state.state).strip().lower() in ("unknown", "unavailable"):
            return False
    return True


def _device_can_answer_locally(hass: HomeAssistant, device: dict) -> bool:
    state = _device_entity_state(hass, device, "voip_state").lower()
    return state in ("ringing", "incoming")


async def _press_device_button(hass: HomeAssistant, device: dict, key: str, label: str) -> bool:
    button_eid = (device.get("entities") or {}).get(key)
    if not button_eid:
        _LOGGER.warning("Cannot press %s for %s: entity not found", label, device.get("name"))
        return False
    try:
        await hass.services.async_call("button", "press", {"entity_id": button_eid}, blocking=True)
        _LOGGER.info("Pressed %s for %s via voip_stack service", button_eid, device.get("name"))
        return True
    except Exception:
        _LOGGER.exception("Failed pressing %s for %s", button_eid, device.get("name"))
        return False


async def _call_esphome_action(hass: HomeAssistant, device: dict, action: str, data: dict | None = None) -> None:
    """Invoke a native ESPHome action exposed by the selected SIP phone."""
    from homeassistant.exceptions import ServiceValidationError

    route_id = str(device.get("route_id") or "").strip()
    if not route_id:
        raise ServiceValidationError(f"{device.get('name') or 'ESP phone'} has no ESPHome service route")
    service = f"{route_id}_{action}"
    if not hass.services.has_service("esphome", service):
        raise ServiceValidationError(f"ESPHome service esphome.{service} is not available")
    await hass.services.async_call("esphome", service, data or {}, blocking=True)
    _LOGGER.info("ESP SIP phone %s action=%s data=%s", device.get("name"), action, data or {})


def _has_esphome_action(hass: HomeAssistant, device: dict, action: str) -> bool:
    route_id = str(device.get("route_id") or "").strip()
    return bool(route_id and hass.services.has_service("esphome", f"{route_id}_{action}"))


async def _resolve_command_phone(hass: HomeAssistant, call: ServiceCall) -> dict | None:
    """Resolve an optional ESP phone selector for sip_* services.

    No selector means the command targets the HA softphone. A selector in
    source/source_device_id/source_name or the usual HA target fields means the
    command is a mirror action on that ESP SIP phone.
    """
    source = await _resolve_source_device_from_call(hass, call)
    if source is not None:
        return source
    return await _resolve_target_device(hass, call)


def _device_transport(hass: HomeAssistant, d: dict, udp_manager=None) -> str:
    """Read the endpoint-declared SIP signaling transport."""
    value = str(d.get("sip_transport") or "").lower()
    return value if value in ("udp", "tcp") else ""


async def _async_build_peer_snapshot(hass: HomeAssistant) -> list[Peer]:
    """Snapshot of every online peer published through endpoint sensors."""
    devices = await _get_voip_devices(hass)
    cfg = _get_transport_config(hass)
    out: list[Peer] = []
    for d in devices:
        name = d.get("name") or ""
        host = d.get("host") or ""
        if not name or not host:
            continue
        if not _device_is_phonebook_available(hass, d):
            _LOGGER.debug("Skipping offline VoIP peer from phonebook: %s", name or host)
            continue
        sip_transport = _device_transport(hass, d)
        if not sip_transport:
            _LOGGER.warning("Skipping SIP peer %s from phonebook: endpoint did not publish sip_transport", name or host)
            continue
        out.append(Peer(
            device=d,
            name=name,
            host=host,
            sip_port=int(d.get("sip_port") or cfg["sip_port"]),
            rtp_port=int(d.get("rtp_port") or cfg["rtp_port"]),
            extension=str(d.get("extension") or ""),
            conference_group=str(d.get("conference_group") or ""),
            conference_ring=bool(d.get("conference_ring", False)),
            ring_group=str(d.get("ring_group") or ""),
            audio_mode=d.get("audio_mode", "full_duplex"),
            tx_formats=list(d.get("tx_formats") or []),
            rx_formats=list(d.get("rx_formats") or []),
        ))
        d["sip_transport"] = sip_transport

    ha_endpoint_state = hass.states.get(HA_SOFTPHONE_ENDPOINT_ENTITY_ID)
    ha_endpoint = parse_voip_endpoint(ha_endpoint_state.state if ha_endpoint_state else None)
    if ha_endpoint is not None:
        out.append(Peer(
            device=None,
            name=ha_endpoint["name"],
            host=ha_endpoint["host"],
            local_ha=True,
            sip_port=int(ha_endpoint.get("sip_port") or cfg["sip_port"]),
            rtp_port=int(ha_endpoint.get("rtp_port") or cfg["rtp_port"]),
            extension=str(ha_endpoint.get("extension") or ""),
            conference_group=str(ha_endpoint.get("conference_group") or ""),
            conference_ring=bool(ha_endpoint.get("conference_ring", False)),
            ring_group=str(ha_endpoint.get("ring_group") or ""),
            audio_mode=ha_endpoint.get("audio_mode", "full_duplex"),
            tx_formats=[fmt.wire_token() for fmt in ha_endpoint.get("tx_formats") or []],
            rx_formats=[fmt.wire_token() for fmt in ha_endpoint.get("rx_formats") or []],
        ))
    else:
        _LOGGER.warning(
            "HA softphone endpoint sensor is unavailable; HA will not appear in the SIP phonebook"
        )
    return out


def _sip_uri_transport(uri) -> str:
    for key, value in getattr(uri, "params", ()) or ():
        if str(key).lower() == "transport" and str(value or "").lower() in {"tcp", "udp"}:
            return str(value).upper()
    return "UDP"


def _enable_reused_sip_tcp_connection(
    hass: HomeAssistant,
    client,
    uri,
    *,
    target: str,
    default_sip_port: int,
) -> bool:
    """Use the REGISTER TCP connection when a registered client contact points at it."""
    if _sip_uri_transport(uri).upper() != "TCP":
        return False
    endpoint = hass.data.get(DOMAIN, {}).get("sip_endpoint")
    tcp_server = getattr(endpoint, "tcp_server", None)
    if tcp_server is None:
        return False
    remote_addr = (uri.host, int(uri.port or default_sip_port))
    reuse = tcp_server.open_reused_dialog(remote_addr, client.dialog_ids.call_id)
    if reuse is None:
        return False
    send, responses = reuse
    client.use_reused_tcp_connection(
        send=send,
        responses=responses,
        close=lambda addr=remote_addr, call_id=client.dialog_ids.call_id: tcp_server.close_reused_dialog(addr, call_id),
    )
    _LOGGER.info("SIP TCP connection reuse enabled for %s via %s:%s", target, remote_addr[0], remote_addr[1])
    return True


async def _resolve_target_device(hass: HomeAssistant, call: ServiceCall) -> dict | None:
    """Thin wrapper over VoipDeviceResolver.resolve_target."""
    return await get_resolver(hass).resolve_target(call)


async def _resolve_source_device_from_call(hass: HomeAssistant, call: ServiceCall) -> dict | None:
    source = str(
        call.data.get("source")
        or call.data.get("source_device_id")
        or call.data.get("source_name")
        or ""
    ).strip()
    if not source:
        return None
    devices = await _get_voip_devices(hass)
    wanted = source.lower()
    for device in devices:
        if (
            str(device.get("device_id") or "").lower() == wanted
            or str(device.get("name") or "").lower() == wanted
            or str(device.get("route_id") or "").lower() == wanted
            or str(device.get("host") or "").lower() == wanted
        ):
            return device
    return None


def _call_destination(call: ServiceCall, dest_device: dict | None = None) -> str:
    value = str(
        call.data.get("destination")
        or call.data.get("target")
        or call.data.get("call")
        or ""
    ).strip()
    if value:
        return value
    if dest_device is not None:
        return str(dest_device.get("name") or "").strip()
    return ""


def _with_target_device(op_label: str):
    """Decorator: resolve the call target or raise ServiceValidationError."""
    from homeassistant.exceptions import ServiceValidationError

    def decorator(fn):
        async def wrapper(call: ServiceCall) -> None:
            hass: HomeAssistant = call.hass
            device = await _resolve_target_device(hass, call)
            if not device:
                raise ServiceValidationError(
                    f"voip_stack.{op_label}: no VoIP device matches the target"
                )
            await fn(call, device)
        return wrapper
    return decorator


def _require_service_target(value: dict) -> dict:
    """Service calls need one explicit device selector in data."""
    if any(value.get(key) for key in ("device_id", "entity_id", "name", "friendly_name")):
        return value
    raise vol.Invalid("provide one target: device_id, entity_id, name, or friendly_name")


def _require_call_destination(value: dict) -> dict:
    if any(value.get(key) for key in ("device_id", "entity_id", "name", "friendly_name")):
        return value
    if any(str(value.get(key) or "").strip() for key in ("destination", "target", "call")):
        return value
    raise vol.Invalid("provide a destination device or destination/target/call")


def _track_outbound_sip_client(hass: HomeAssistant, *, client, result: str, target: str, sip_uri: str = "") -> None:
    """Keep an outbound SIP client alive and complete early-dialog INVITEs."""
    registry = _call_registry(hass)
    if result not in {"ringing", "in_call"}:
        hass.async_create_task(client.close())
        return

    registry.sip_clients[client.dialog_ids.call_id] = client
    registry.upsert(
        client.dialog_ids.call_id,
        state=CallState.REMOTE_RINGING.value if result == "ringing" else CallState.IN_CALL.value,
        caller=_ha_peer_name(hass),
        callee=target,
        route_kind="direct",
    )
    registry.add_leg(client.dialog_ids.call_id, client.dialog_ids.call_id, role="ha_softphone", state=result)

    async def _watch_sip_lifecycle() -> None:
        try:
            final = "in_call" if result == "in_call" else await client.wait_for_final()
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            final = "timeout"
        except Exception as err:  # noqa: BLE001 - keep detached watcher failures contained.
            _LOGGER.warning(
                "SIP final watcher failed for call_id=%s target=%s: %s",
                client.dialog_ids.call_id,
                target,
                err,
            )
            final = "error"
        public_final = _sip_public_state(final)
        if public_final == CallState.IN_CALL.value and client.dialog is not None:
            _set_ha_softphone_call_state(
                hass,
                CallState.IN_CALL.value,
                session_device_id=HA_SOFTPHONE_DEVICE_ID,
                caller=_ha_peer_name(hass),
                callee=target,
                peer_name=target,
                direction="outgoing",
                call_id=client.dialog_ids.call_id,
                selected_tx_format=client.dialog.send_format.audio_format.wire_token(),
                selected_rx_format=client.dialog.recv_format.audio_format.wire_token(),
                selected_tx_rtp_format=client.dialog.send_format.wire_token(),
                selected_rx_rtp_format=client.dialog.recv_format.wire_token(),
                sip_status_code=200,
                last_sip_event="SIP_RESPONSE",
                sip_uri=sip_uri,
            )
        elif public_final not in {CallState.RINGING.value, CallState.IN_CALL.value}:
            terminal_reason = _sip_terminal_reason(final, public_final)
            _set_ha_softphone_call_state(
                hass,
                public_final,
                session_device_id=HA_SOFTPHONE_DEVICE_ID,
                caller=_ha_peer_name(hass),
                callee=target,
                peer_name=target,
                direction="outgoing",
                call_id=client.dialog_ids.call_id,
                reason=terminal_reason,
                terminal_reason=terminal_reason,
                sip_status_code=client.last_sip_status_code,
                last_sip_event=client.last_sip_event or "SIP_RESPONSE",
                sip_uri=sip_uri,
            )
        terminal_reason = "" if public_final in {CallState.RINGING.value, CallState.IN_CALL.value} else _sip_terminal_reason(final, public_final)
        payload = {
            "state": public_final,
            "scope": "sip",
            "call_id": client.dialog_ids.call_id,
            "target": target,
            "terminal_reason": terminal_reason,
        }
        if sip_uri:
            payload["sip_uri"] = sip_uri
        _fire_call_event(hass, payload, "sip")
        if final not in {"ringing", "in_call"}:
            registry.detach_client(client.dialog_ids.call_id)
            registry.finish_and_pop(client.dialog_ids.call_id, reason=terminal_reason, state=public_final)
            await client.close()
            return
        if final == "in_call":
            try:
                terminal = await client.wait_for_dialog_termination()
            except asyncio.CancelledError:
                raise
            except Exception as err:  # noqa: BLE001 - keep detached watcher failures contained.
                _LOGGER.warning(
                    "SIP dialog watcher failed for call_id=%s target=%s: %s",
                    client.dialog_ids.call_id,
                    target,
                    err,
                )
                terminal = "error"
            registry.detach_client(client.dialog_ids.call_id)
            await client.close()
            terminal_reason = TerminalReason.REMOTE_HANGUP.value if terminal == "remote_hangup" else _sip_terminal_reason(terminal, _sip_public_state(terminal))
            _set_ha_softphone_call_state(
                hass,
                CallState.IDLE.value,
                session_device_id=HA_SOFTPHONE_DEVICE_ID,
                caller=_ha_peer_name(hass),
                callee=target,
                peer_name=target,
                direction="outgoing",
                call_id=client.dialog_ids.call_id,
                reason=terminal_reason,
                terminal_reason=terminal_reason,
                origin="remote" if terminal == "remote_hangup" else "self",
                sip_status_code=client.last_sip_status_code,
                last_sip_event=client.last_sip_event or "BYE",
                sip_uri=sip_uri,
            )
            registry.finish_and_pop(client.dialog_ids.call_id, reason=terminal_reason)

    task = hass.async_create_task(_watch_sip_lifecycle())
    registry.client_watchers[client.dialog_ids.call_id] = task


async def _async_prepare_ha_outbound_call(hass: HomeAssistant) -> None:
    """Close stale HA softphone SIP clients before creating a new dialog."""
    from homeassistant.exceptions import ServiceValidationError

    registry = _call_registry(hass)
    store = _ha_softphone_store(hass)
    if str(store.get("state") or "").strip().lower() in HA_SOFTPHONE_ACTIVE_STATES:
        raise ServiceValidationError("HA softphone already has an active SIP call")

    for call_id, client in list(registry.sip_clients.items()):
        _client, watcher = registry.detach_client(call_id)
        if watcher is not None:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher
        try:
            await client.terminate()
            await client.close()
        except Exception:
            _LOGGER.debug("Ignoring stale HA SIP client cleanup error", exc_info=True)
        registry.finish_and_pop(call_id, reason=TerminalReason.LOCAL_HANGUP.value)


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
        # On ESP phones the Call button is the local answer control while ringing.
        if not await _press_device_button(hass, device, "call", "SIP answer"):
            from homeassistant.exceptions import ServiceValidationError

            raise ServiceValidationError(f"{device.get('name') or 'ESP phone'} has no answer/call button")
        return
    call_id = str(call.data.get("call_id") or "").strip()
    if not call_id:
        call_id = _single_pending_route_call_id(hass)
    if call_id and call_id in _pending_routes(hass):
        _set_pending_route_decision(hass, {"call_id": call_id, "action": "answer_ha"})
        return
    registry = _call_registry(hass)
    if call_id.startswith("conference:"):
        room_name = call_id.split(":", 1)[1]
        manager = hass.data.setdefault(DOMAIN, {}).get("conference_manager")
        queue = manager.join_ha_softphone(room_name) if manager is not None else None
        if queue is None:
            _LOGGER.warning("sip_answer: conference room not found for %s", call_id)
            return
        registry.softphone_media[call_id] = {
            "conference_room": room_name,
            "conference_queue": queue,
        }
        registry.upsert(
            call_id,
            state=CallState.IN_CALL.value,
            caller=room_name,
            callee=_ha_peer_name(hass),
            route_kind="conference",
        )
        registry.add_leg(call_id, call_id, role="ha_softphone", state=CallState.IN_CALL.value)
        _set_ha_softphone_call_state(
            hass,
            CallState.IN_CALL.value,
            session_device_id=HA_SOFTPHONE_DEVICE_ID,
            caller=room_name,
            callee=_ha_peer_name(hass),
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
    if not call_id and len(pending) == 1:
        call_id = next(iter(pending))
    invite = pending.pop(call_id, None) if call_id else None
    if invite is None:
        _LOGGER.warning("sip_answer: no pending SIP call %s", call_id or "(current)")
        return

    preanswered = registry.preanswered.pop(call_id, None)
    local_rtp_port = int((preanswered or {}).get("local_rtp_port") or 0)
    if local_rtp_port:
        _LOGGER.info("SIP answered pre-answered trunk call_id=%s", call_id)
    else:
        local_ip = await _ha_advertise_host(hass)
        from .sdp import build_answer_directional
        local_rtp_port = _allocate_sip_rtp_port(hass)
        answer = build_answer_directional(
            local_ip,
            local_ip,
            local_rtp_port,
            invite.send_format,
            invite.recv_format,
        )
        if not _sip_send_final_response(hass, call_id, 200, "OK", answer_sdp=answer):
            _LOGGER.warning("sip_answer: SIP transaction not found for %s", call_id)
            return

    registry.softphone_media[call_id] = {
        "invite": invite,
        "local_rtp_port": local_rtp_port,
    }
    registry.upsert(
        call_id,
        state=CallState.IN_CALL.value,
        caller=invite.caller,
        callee=_ha_peer_name(hass),
        route_kind="ha_softphone",
    )
    registry.add_leg(call_id, call_id, role="ha_softphone", state=CallState.IN_CALL.value)
    _LOGGER.info("SIP answered call_id=%s", call_id)
    _set_ha_softphone_call_state(
        hass,
        CallState.IN_CALL.value,
        session_device_id=HA_SOFTPHONE_DEVICE_ID,
        caller=invite.caller,
        callee=_ha_peer_name(hass),
        peer_name=invite.caller,
        direction="incoming",
        call_id=call_id,
        sip_status_code=200,
        last_sip_event="SIP_RESPONSE",
        selected_tx_format=invite.send_format.audio_format.wire_token(),
        selected_rx_format=invite.recv_format.audio_format.wire_token(),
        selected_tx_rtp_format=invite.send_format.wire_token(),
        selected_rx_rtp_format=invite.recv_format.wire_token(),
    )


async def _handle_sip_decline_service(call: ServiceCall) -> None:
    hass: HomeAssistant = call.hass
    device = await _resolve_command_phone(hass, call)
    if device is not None:
        reason = str(call.data.get("reason") or call.data.get("decline_reason") or "").strip()
        if _has_esphome_action(hass, device, "decline_call"):
            await _call_esphome_action(hass, device, "decline_call", {"reason": reason})
        elif not await _press_device_button(hass, device, "decline", "SIP decline"):
            from homeassistant.exceptions import ServiceValidationError

            raise ServiceValidationError(f"{device.get('name') or 'ESP phone'} has no decline control")
        return
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
        call_id = _single_pending_route_call_id(hass)
    if call_id and call_id in _pending_routes(hass):
        _set_pending_route_decision(
            hass,
            {
                "call_id": call_id,
                "action": "busy" if status == 486 else "cancel" if status == 487 else "decline",
                "status": status,
                "reason": reason,
                "decline_reason": app_reason,
            },
        )
        return
    registry = _call_registry(hass)
    pending = registry.pending_invites
    if not call_id and len(pending) == 1:
        call_id = next(iter(pending))
    pending.pop(call_id, None)
    was_preanswered = bool(call_id and registry.preanswered.pop(call_id, None) is not None)
    if was_preanswered:
        _sip_send_bye(hass, call_id)
        _LOGGER.info("SIP declined pre-answered trunk call_id=%s reason=%s", call_id, app_reason)
        _set_ha_softphone_call_state(
            hass,
            "declined",
            session_device_id=HA_SOFTPHONE_DEVICE_ID,
            reason=app_reason,
            call_id=call_id,
            sip_status_code=status,
            last_sip_event="BYE",
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
        session_device_id=HA_SOFTPHONE_DEVICE_ID,
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
        reason = str(call.data.get("reason") or "local_hangup").strip()
        if _has_esphome_action(hass, device, "decline_call"):
            await _call_esphome_action(hass, device, "decline_call", {"reason": reason})
        elif not await _press_device_button(hass, device, "decline", "SIP hangup"):
            from homeassistant.exceptions import ServiceValidationError

            raise ServiceValidationError(f"{device.get('name') or 'ESP phone'} has no hangup/decline control")
        return
    call_id = str(call.data.get("call_id") or "").strip()
    if not call_id:
        call_id = _single_pending_route_call_id(hass)
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
                },
            )
            return
    registry = _call_registry(hass)
    clients = registry.sip_clients
    relays = registry.relays
    pending = registry.pending_invites
    media_sessions = registry.softphone_media
    preanswered = registry.preanswered
    softphone_store = _ha_softphone_store(hass)
    if not call_id and len(clients) == 1:
        call_id = next(iter(clients))
    if not call_id and len(pending) == 1:
        call_id = next(iter(pending))
    if not call_id and len(media_sessions) == 1:
        call_id = next(iter(media_sessions))
    if not call_id:
        call_id = str(softphone_store.get("call_id") or "").strip()
    caller = str(softphone_store.get("caller") or softphone_store.get("last_terminal_caller") or "")
    callee = str(softphone_store.get("callee") or softphone_store.get("last_terminal_callee") or "")
    peer_name = str(softphone_store.get("peer_name") or softphone_store.get("last_terminal_peer_name") or "")
    direction = str(softphone_store.get("direction") or softphone_store.get("last_terminal_direction") or "")
    bridge_handled, bridge_source_call_id, bridge_dest_call_id, bridge_client, bridge_server_bye = await _terminate_sip_bridge(hass, call_id)
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
        _fire_call_event(
            hass,
            {
                "state": CallState.IDLE.value,
                "scope": "sip_bridge",
                "call_id": bridge_source_call_id,
                "dest_call_id": bridge_dest_call_id,
                "terminal_reason": TerminalReason.LOCAL_HANGUP.value,
            },
            "sip",
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
    relay = relays.pop(call_id, None) if call_id else None
    media_session = media_sessions.pop(call_id, None) if call_id else None
    conference_room = str((media_session or {}).get("conference_room") or "")
    if conference_room:
        manager = hass.data.setdefault(DOMAIN, {}).get("conference_manager")
        if manager is not None:
            await manager.leave_ha_softphone(conference_room)
    pending_ids = [call_id] if call_id and call_id in pending else ([] if call_id else list(pending))
    server_bye = False
    pending_closed = 0
    if client is not None:
        if watcher is not None:
            watcher.cancel()
            try:
                await watcher
            except asyncio.CancelledError:
                pass
        await client.terminate()
        await client.close()
    if relay is not None:
        await relay.stop()
    for pending_call_id in pending_ids:
        invite = pending.pop(pending_call_id, None)
        if invite is None:
            continue
        if preanswered.pop(pending_call_id, None) is not None:
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
            session_device_id=HA_SOFTPHONE_DEVICE_ID,
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
        session_device_id=HA_SOFTPHONE_DEVICE_ID,
        caller=caller,
        callee=callee,
        peer_name=peer_name,
        direction=direction,
        call_id=call_id,
        reason=TerminalReason.LOCAL_HANGUP.value,
        origin="self",
        last_sip_event="SIP_BYE" if (client is not None or relay is not None or server_bye) else "SIP_HANGUP",
    )
    if call_id:
        registry.finish_and_pop(call_id, reason=TerminalReason.LOCAL_HANGUP.value)
    _fire_call_event(
        hass,
        {
            "state": CallState.IDLE.value,
            "scope": "sip",
            "call_id": call_id,
            "pending_closed": pending_closed,
            "terminal_reason": TerminalReason.LOCAL_HANGUP.value,
        },
        "sip",
    )
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


async def _handle_set_dnd_service(call: ServiceCall) -> None:
    hass: HomeAssistant = call.hass
    enabled = bool(call.data.get("dnd"))
    store = _ha_softphone_store(hass)
    store["dnd"] = enabled
    await _async_save_ha_softphone_store(hass)
    state = _ha_softphone_state(hass)
    _fire_call_event(hass, state, "session")
    _LOGGER.info("HA softphone DND set to %s via service", enabled)


async def _handle_set_ha_softphone_groups_service(call: ServiceCall) -> None:
    hass = call.hass
    await async_set_ha_softphone_groups(
        hass,
        ring_group=call.data.get("ring_group"),
        conference_group=call.data.get("conference_group"),
        conference_ring=call.data.get("conference_ring"),
    )


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
        await _call_esphome_action(hass, source, "start_call", {"dest": target})
        _LOGGER.info("ESP SIP phone %s originating call to %s", source.get("name"), target)
        return
    cfg = _get_transport_config(hass)
    local_ip = await _ha_advertise_host(hass)
    if not local_ip:
        raise ServiceValidationError("HA advertise IP is unknown")
    trunk = hass.data.get(DOMAIN, {}).get("sip_trunk")
    trunk_cfg = _get_trunk_config(hass)
    trunk_ready = _trunk_enabled(trunk_cfg) and bool(getattr(trunk, "registered", False))
    sensor = hass.states.get("sensor.voip_phonebook")
    roster_json = str(sensor.attributes.get("roster_json") or "") if sensor is not None else ""
    contacts = parse_roster_json(roster_json) if roster_json else []
    route = resolve_ha_router(target, contacts, trunk_ready=trunk_ready)
    display_target = route.entry.display_name if route.entry is not None else target
    if (force_ha_bridge or bool(call.data.get("ha_bridge", False))) and route.action not in {
        RouteAction.ANSWER_HA,
        RouteAction.TRUNK,
        RouteAction.REJECT,
    }:
        route = replace(route, action=RouteAction.BRIDGE, sip_uri=ha_uri_for(route.target or target, contacts))
    use_trunk = route.action is RouteAction.TRUNK and trunk_ready
    use_registered_contact_codecs = bool(
        route.entry is not None and route.entry.sip_uri and route.entry.metadata.get("registered")
    )
    if route.action is RouteAction.TRUNK and not use_trunk:
        raise ServiceValidationError(f"{target} requires a registered SIP trunk")
    if route.action is RouteAction.GROUP and route.entry is not None:
        group_type = str((route.entry.metadata or {}).get("group_type") or "")
        if group_type == "conference":
            room_name = route.entry.name or route.entry.id or target
            from .conference import conference_manager

            manager = conference_manager(hass, local_ip=local_ip)
            queue = manager.start_ha_softphone(room_name)
            call_id = f"conference:{room_name}"
            registry = _call_registry(hass)
            registry.softphone_media[call_id] = {
                "conference_room": room_name,
                "conference_queue": queue,
            }
            registry.upsert(
                call_id,
                state=CallState.IN_CALL.value,
                caller=_ha_peer_name(hass),
                callee=room_name,
                route_kind="conference",
            )
            registry.add_leg(call_id, call_id, role="ha_softphone", state=CallState.IN_CALL.value)
            _set_ha_softphone_call_state(
                hass,
                CallState.IN_CALL.value,
                session_device_id=HA_SOFTPHONE_DEVICE_ID,
                caller=_ha_peer_name(hass),
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
            )
            _fire_call_event(
                hass,
                {
                    "state": CallState.IN_CALL.value,
                    "scope": "conference",
                    "call_id": call_id,
                    "room": room_name,
                    "target": target,
                },
                "sip",
            )
            _LOGGER.info("HA softphone joined conference room=%s", room_name)
            return
    route_uri = route.sip_uri
    if route.action is RouteAction.GROUP:
        route_uri = ha_uri_for(route.target or target, contacts)
    if use_trunk:
        trunk_target = route.target or target
        route_uri = (
            f"sip:{trunk_target}@{trunk_cfg[CONF_TRUNK_SERVER]}:{int(trunk_cfg[CONF_TRUNK_PORT])};"
            f"transport={str(trunk_cfg[CONF_TRUNK_TRANSPORT]).lower()}"
        )
    if not use_trunk and (route.action not in {RouteAction.DIRECT, RouteAction.FORWARD, RouteAction.BRIDGE, RouteAction.GROUP} or not route_uri):
        raise ServiceValidationError(f"cannot resolve SIP target: {target}")
    await _async_prepare_ha_outbound_call(hass)
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
    local_rtp_port = _allocate_sip_rtp_port(hass)
    client = SipCallClient(
        local_ip=local_ip,
        local_name=_ha_peer_name(hass),
        local_sip_port=int(cfg["sip_port"]),
        local_rtp_port=local_rtp_port,
        supported_send_formats=sip_send_formats,
        supported_recv_formats=sip_recv_formats,
        signaling_transport=_sip_uri_transport(uri),
        auth_username=str(trunk_cfg.get(CONF_TRUNK_AUTH_USERNAME) or ""),
        username=str(trunk_cfg.get(CONF_TRUNK_USERNAME) or ""),
        password=str(trunk_cfg.get(CONF_TRUNK_PASSWORD) or "") if use_trunk else "",
        outbound_proxy=str(trunk_cfg.get(CONF_TRUNK_OUTBOUND_PROXY) or "") if use_trunk else "",
        include_common_codecs=use_trunk or use_registered_contact_codecs,
    )
    if not use_trunk:
        _enable_reused_sip_tcp_connection(
            hass,
            client,
            uri,
            target=target,
            default_sip_port=int(cfg["sip_port"]),
        )
    _set_ha_softphone_call_state(
        hass,
        CallState.CALLING.value,
        session_device_id=HA_SOFTPHONE_DEVICE_ID,
        caller=_ha_peer_name(hass),
        callee=display_target,
        peer_name=display_target,
        direction="outgoing",
        call_id=client.dialog_ids.call_id,
        sip_transport=_sip_uri_transport(uri).lower(),
        last_sip_event="INVITE",
        sip_uri=route_uri,
    )
    result = await client.invite(
        target=uri.user,
        remote_host=uri.host,
        remote_sip_port=uri.port or int(cfg["sip_port"]),
        timeout=SIP_TIMER_B if use_trunk else 8.0,
    )
    if result == TerminalReason.TRANSPORT_UNREACHABLE.value and route.entry is not None and route.entry.metadata.get("registered"):
        await _mark_sip_account_unreachable(hass, route.entry.id)
    public_result = _sip_public_state(result)
    _track_outbound_sip_client(
        hass,
        client=client,
        result=result,
        target=target,
        sip_uri=route_uri,
    )
    if public_result == CallState.REMOTE_RINGING.value or result == "ringing":
        _set_ha_softphone_call_state(
            hass,
            CallState.REMOTE_RINGING.value,
            session_device_id=HA_SOFTPHONE_DEVICE_ID,
            caller=_ha_peer_name(hass),
            callee=display_target,
            peer_name=display_target,
            direction="outgoing",
            call_id=client.dialog_ids.call_id,
            sip_status_code=180,
            last_sip_event="SIP_RESPONSE",
            sip_uri=route_uri,
        )
    elif public_result == CallState.IN_CALL.value and client.dialog is not None:
        _set_ha_softphone_call_state(
            hass,
            CallState.IN_CALL.value,
            session_device_id=HA_SOFTPHONE_DEVICE_ID,
            caller=_ha_peer_name(hass),
            callee=display_target,
            peer_name=display_target,
            direction="outgoing",
            call_id=client.dialog_ids.call_id,
            selected_tx_format=client.dialog.send_format.audio_format.wire_token(),
            selected_rx_format=client.dialog.recv_format.audio_format.wire_token(),
            selected_tx_rtp_format=client.dialog.send_format.wire_token(),
            selected_rx_rtp_format=client.dialog.recv_format.wire_token(),
            sip_status_code=200,
            last_sip_event="SIP_RESPONSE",
            sip_uri=route_uri,
        )
    elif public_result not in {CallState.REMOTE_RINGING.value, CallState.IN_CALL.value}:
        terminal_reason = _sip_terminal_reason(result, public_result)
        _set_ha_softphone_call_state(
            hass,
            public_result,
            session_device_id=HA_SOFTPHONE_DEVICE_ID,
            caller=_ha_peer_name(hass),
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
    terminal_reason = "" if public_result in {CallState.RINGING.value, CallState.IN_CALL.value} else _sip_terminal_reason(result, public_result)
    _fire_call_event(
        hass,
        {
            "state": public_result,
            "scope": "sip",
            "call_id": client.dialog_ids.call_id,
            "target": target,
            "sip_uri": route_uri,
            "terminal_reason": terminal_reason,
        },
        "sip",
    )
    _LOGGER.info("SIP call target=%s uri=%s result=%s", target, route_uri, result)


async def _handle_sip_route_service(call: ServiceCall) -> None:
    _set_pending_route_decision(call.hass, dict(call.data))


async def _handle_sip_forward_service(call: ServiceCall) -> None:
    """Forward a SIP call through HA's dial plan/B2BUA path."""
    call_id = str(call.data.get("call_id") or "").strip()
    if call_id and call_id in _pending_routes(call.hass):
        data = dict(call.data)
        data["action"] = "forward"
        _set_pending_route_decision(call.hass, data)
        return
    await _handle_sip_call_target_service(call, force_ha_bridge=True)


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
            "set_ha_softphone_groups": _handle_set_ha_softphone_groups_service,
            "call": _handle_sip_call_target_service,
            "forward": _handle_sip_forward_service,
            "route": _handle_sip_route_service,
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
    if hass.data.get(DOMAIN, {}).get("initialized"):
        return

    hass.data.setdefault(DOMAIN, {})
    hass.data[DOMAIN]["initialized"] = True

    await _async_load_ha_softphone_store(hass)
    async_register_websocket_api(hass)
    from .audio_ws_view import async_register_audio_ws_view
    async_register_audio_ws_view(hass)
    await _async_register_services(hass)
    _register_esp_state_event_bridge(hass)

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
    hass.data[DOMAIN]["trunk_config"] = _entry_trunk_config(None)
    hass.data[DOMAIN]["sip_port"] = VOIP_STACK_SIP_PORT
    await _async_setup_shared(hass, config)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up VoIP Stack from a config entry (UI setup)."""
    cfg = _entry_transport_config(entry)
    trunk_cfg = _entry_trunk_config(entry)
    hass.data.setdefault(DOMAIN, {})["transport_config"] = cfg
    hass.data[DOMAIN]["trunk_config"] = trunk_cfg
    hass.data[DOMAIN]["sip_port"] = cfg["sip_port"]
    hass.data[DOMAIN][CONF_DEBUG_MODE] = bool(entry.data.get(CONF_DEBUG_MODE, False))
    hass.data[DOMAIN]["manual_roster_entries"] = _manual_roster_entries(hass)
    await _async_setup_shared(hass)
    await _async_apply_assist_intents(
        hass,
        bool(entry.data.get(CONF_ASSIST_INTENTS, False)),
    )
    if not await _async_start_sip_endpoint(hass):
        raise ConfigEntryError(
            f"Failed to bind SIP port {cfg['sip_port']}. Another SIP "
            "endpoint may already be listening on that port."
        )
    await _async_start_sip_trunk(hass)
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    hass.async_create_task(_deferred_phonebook_sync(hass))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    await _async_apply_assist_intents(hass, False)
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    # Stop sessions / bridges before tearing down listeners; otherwise
    # orphaned transports leak sockets across config-entry reload.
    from .websocket_api import _async_shutdown_all
    await _async_shutdown_all(hass)

    await _async_stop_sip_trunk(hass)
    hass.data.get(DOMAIN, {}).pop("sip_registrar", None)
    await _async_stop_sip_endpoint(hass)
    unsub = hass.data.get(DOMAIN, {}).pop("esp_state_event_bridge_unsub", None)
    if unsub is not None:
        unsub()
    hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unload_ok


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
