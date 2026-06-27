"""Intercom Native integration for Home Assistant.

HA is a SIP softphone and SIP B2BUA/router for ESPHome SIP phones. Public call
control is expressed in SIP/SDP/RTP terms only; logical targets are resolved by
the central phonebook and routed through HA as SIP dialogs when needed.
"""

import asyncio
import logging

import voluptuous as vol

from homeassistant.core import HomeAssistant, CoreState, Event, ServiceCall, callback
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform, EVENT_HOMEASSISTANT_STARTED, EVENT_STATE_CHANGED
from homeassistant.exceptions import ConfigEntryError

PLATFORMS: list[Platform] = [Platform.SENSOR]
from homeassistant.helpers import config_validation as cv
from homeassistant.components import network

from .const import (
    CONF_ASSIST_INTENTS,
    DOMAIN,
    HA_PEER_FALLBACK_NAME,
    HA_SOFTPHONE_DEVICE_ID,
    INTERCOM_RTP_PORT,
    INTERCOM_SIP_PORT,
)
from .device_resolver import get_resolver
from .fsm import CallState, TerminalReason, sip_phone_state
from .audio_format import (
    AudioFormat,
    HA_SIP_PCM_FORMATS,
    HA_SIP_PCM_RX_FORMATS,
    HA_SIP_PCM_TX_FORMATS,
    DEFAULT_AUDIO_FORMAT,
    parse_audio_format_list,
)
from .peer import Peer
from .websocket_api import (
    async_register_websocket_api,
    _async_load_ha_softphone_store,
    _get_intercom_devices,
    _fire_call_event,
    _async_save_ha_softphone_store,
    _ha_softphone_dnd,
    _ha_softphone_state,
    _ha_softphone_store,
    _set_ha_softphone_call_state,
    _session_pop,
    _sessions,
)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


_LOGGER = logging.getLogger(__name__)


def _device_formats(device: dict | None, key: str):
    if not device:
        return [DEFAULT_AUDIO_FORMAT]
    value = device.get(key)
    if isinstance(value, str):
        raw = value
    else:
        raw = ";".join(value or [])
    try:
        return parse_audio_format_list(raw)
    except ValueError as err:
        _LOGGER.warning(
            "Ignoring invalid %s on %s: %s",
            key,
            (device or {}).get("name") or (device or {}).get("device_id"),
            err,
        )
        return [DEFAULT_AUDIO_FORMAT]


def _roster_entry_formats(entry, key: str) -> list[AudioFormat]:
    """Return audio formats from a canonical roster entry metadata field."""
    if entry is None:
        return [DEFAULT_AUDIO_FORMAT]
    metadata = getattr(entry, "metadata", {}) or {}
    value = metadata.get(key)
    if isinstance(value, list):
        raw = ";".join(str(item) for item in value)
    else:
        raw = str(value or "")
    try:
        return parse_audio_format_list(raw)
    except ValueError as err:
        _LOGGER.warning(
            "Ignoring invalid roster %s on %s: %s",
            key,
            getattr(entry, "display_name", None) or getattr(entry, "id", ""),
            err,
        )
        return [DEFAULT_AUDIO_FORMAT]


def _sip_public_state(state: str) -> str:
    """Normalize internal SIP client outcomes to the public SipPhoneState."""
    value = (state or "").strip().lower()
    if value == "in_call":
        return CallState.IN_CALL.value
    if value == "answered":
        return CallState.IN_CALL.value
    if value == "sip_486":
        return CallState.BUSY.value
    if value == "sip_603":
        return CallState.DECLINED.value
    if value == "sip_487":
        return CallState.CANCELLED.value
    if value == "sip_488":
        return CallState.MEDIA_INCOMPATIBLE.value
    if value in {"sip_401", "sip_407"}:
        return CallState.AUTH_REQUIRED_UNSUPPORTED.value
    if value == "timeout":
        return TerminalReason.TIMEOUT.value
    if value == "error":
        return TerminalReason.PROTOCOL_ERROR.value
    return value or CallState.IDLE.value


def _sip_target_audio_profile(
    *,
    remote_tx_formats: list[AudioFormat] | None,
    remote_rx_formats: list[AudioFormat] | None,
    target: str,
) -> tuple[list[AudioFormat], list[AudioFormat]]:
    """Constrain HA SIP offers to formats that can actually work with target."""
    remote_tx = list(remote_tx_formats or [])
    remote_rx = list(remote_rx_formats or [])
    send_candidates = (
        [fmt for fmt in HA_SIP_PCM_TX_FORMATS if fmt in set(remote_rx)]
        if remote_rx else list(HA_SIP_PCM_TX_FORMATS)
    )
    recv_candidates = (
        [fmt for fmt in HA_SIP_PCM_RX_FORMATS if fmt in set(remote_tx)]
        if remote_tx else list(HA_SIP_PCM_RX_FORMATS)
    )
    common = [fmt for fmt in send_candidates if fmt in set(recv_candidates)]
    if not common:
        _LOGGER.warning(
            "No common direct SIP PCM wire format for %s "
            "(ha_send=%s ha_recv=%s remote_tx=%s remote_rx=%s)",
            target,
            [fmt.wire_token() for fmt in HA_SIP_PCM_TX_FORMATS],
            [fmt.wire_token() for fmt in HA_SIP_PCM_RX_FORMATS],
            [fmt.wire_token() for fmt in remote_tx],
            [fmt.wire_token() for fmt in remote_rx],
        )
    else:
        _LOGGER.info(
            "Direct SIP PCM profile for %s: wire candidates=%s",
            target,
            [fmt.wire_token() for fmt in common],
        )
    return send_candidates, recv_candidates


def _ha_peer_name(hass: HomeAssistant) -> str:
    """Return the HA phonebook peer name.

    HA normally always has a configured location_name. The fallback is only for
    malformed/empty local config and avoids a hardcoded "Home Assistant" peer
    identity.
    """
    return (hass.config.location_name or "").strip() or HA_PEER_FALLBACK_NAME


def _entry_transport_config(entry: ConfigEntry | None = None) -> dict:
    """Normalised SIP/RTP config."""
    data = entry.data if entry is not None else {}
    return {
        "use_sip": data.get("use_sip", True),
        "sip_port": int(data.get("sip_port", INTERCOM_SIP_PORT)),
        "rtp_port": int(data.get("rtp_port", INTERCOM_RTP_PORT)),
        "advertise_host": (data.get("advertise_host") or "").strip(),
    }


def _get_transport_config(hass: HomeAssistant) -> dict:
    """Return current HA-side network config (transport flags + ports)."""
    return hass.data.get(DOMAIN, {}).get(
        "transport_config",
        {
            "use_sip": True,
            "sip_port": INTERCOM_SIP_PORT,
            "rtp_port": INTERCOM_RTP_PORT,
            "advertise_host": "",
        },
    )


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


def _select_transport_type(hass: HomeAssistant, host: str | None = None) -> str:
    """SIP signaling transport inferred from a host."""
    return "udp"


def _resolve_esphome_route_id(hass: HomeAssistant, host: str) -> str:
    """ESPHome node_name slug for `host`, or '' if not configured."""
    return get_resolver(hass).route_id_for_host(host)


def _available_esphome_services(hass: HomeAssistant) -> set[str]:
    """Return currently registered ESPHome service names."""
    try:
        services = hass.services.async_services().get("esphome", {})
    except Exception:
        return set()
    if isinstance(services, dict):
        return set(services)
    return set(services or [])


def _resolve_esphome_service_slug(
    hass: HomeAssistant,
    route_slug: str,
    action: str,
) -> str:
    """Map a route_id slug to the actual ESPHome action service prefix.

    ESPHome service prefixes can lag behind a flashed node-name change until
    HA's ESPHome config entry is cleaned up. Keep the phonebook/control path
    resilient by falling back to a compatible registered service prefix.
    """
    if not route_slug:
        return ""
    services = _available_esphome_services(hass)
    direct = f"{route_slug}_{action}"
    if direct in services:
        return route_slug

    suffix = f"_{action}"
    candidates: list[str] = []
    for service in services:
        if not service.endswith(suffix):
            continue
        candidate = service[:-len(suffix)]
        if candidate == route_slug or candidate.startswith(f"{route_slug}_"):
            candidates.append(candidate)

    if not candidates:
        return route_slug

    candidates.sort(key=lambda item: (abs(len(item) - len(route_slug)), len(item), item))
    resolved = candidates[0]
    _LOGGER.warning(
        "ESPHome service esphome.%s not registered; using esphome.%s instead",
        direct,
        f"{resolved}_{action}",
    )
    return resolved


def _bridge_for_device(device_id: str):
    """Return any live/setup bridge involving `device_id`.

    During bridge setup `_active` is still false, but the device is already
    reserved. Treating setup as busy prevents a second START from stealing or
    tearing down the in-flight call.
    """
    return next(
        (
            bridge
            for bridge in _bridges.values()
            if bridge.source_device_id == device_id or bridge.dest_device_id == device_id
        ),
        None,
    )


def _state_entity_is_busy(hass: HomeAssistant, device: dict) -> bool:
    """True when the ESP-published FSM state says this device is not idle."""
    state_entity = (device.get("entities") or {}).get("intercom_state")
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
    endpoint = bucket.get("sip_endpoint")
    if endpoint is not None:
        return [endpoint]
    return [server for server in (bucket.get("sip_server"), bucket.get("sip_tcp_server")) if server is not None]


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


async def _async_emit_esp_state_event(
    hass: HomeAssistant,
    entity_id: str,
    state: str,
    old_state: str,
    delay: float = 0.0,
) -> None:
    """Mirror ESP-published intercom_state changes onto the public call bus."""
    if delay > 0:
        import asyncio

        await asyncio.sleep(delay)
    devices = await _get_intercom_devices(hass)
    device = next(
        (
            item
            for item in devices
            if (item.get("entities") or {}).get("intercom_state") == entity_id
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
                "endpoint": _device_entity_state(hass, device, "intercom_endpoint"),
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
    """Forward ESP intercom_state entity changes to intercom_native.call_event."""
    bucket = hass.data.setdefault(DOMAIN, {})
    if bucket.get("esp_state_event_bridge_unsub") is not None:
        return

    @callback
    def _on_state_changed(event: Event) -> None:
        entity_id = str(event.data.get("entity_id") or "")
        if "intercom_state" not in entity_id:
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
    """True when the ESP should be advertised to other intercom peers."""
    entities = device.get("entities") or {}
    endpoint_entity = entities.get("intercom_endpoint")
    if not endpoint_entity:
        return False
    endpoint_state = hass.states.get(endpoint_entity)
    if endpoint_state is None or str(endpoint_state.state).strip().lower() in ("", "unknown", "unavailable"):
        return False

    state_entity = entities.get("intercom_state")
    if state_entity:
        state = hass.states.get(state_entity)
        if state is None or str(state.state).strip().lower() in ("unknown", "unavailable"):
            return False
    return True


def _device_can_answer_locally(hass: HomeAssistant, device: dict) -> bool:
    state = _device_entity_state(hass, device, "intercom_state").lower()
    return state in ("ringing", "incoming")


async def _press_device_button(hass: HomeAssistant, device: dict, key: str, label: str) -> bool:
    button_eid = (device.get("entities") or {}).get(key)
    if not button_eid:
        _LOGGER.warning("Cannot press %s for %s: entity not found", label, device.get("name"))
        return False
    try:
        await hass.services.async_call("button", "press", {"entity_id": button_eid}, blocking=True)
        _LOGGER.info("Pressed %s for %s via intercom_native service", button_eid, device.get("name"))
        return True
    except Exception:
        _LOGGER.exception("Failed pressing %s for %s", button_eid, device.get("name"))
        return False


def _device_has_ha_call(device_id: str) -> bool:
    """True when HA already owns a session/bridge leg for this device."""
    return device_id in _sessions or _bridge_for_device(device_id) is not None


def _device_transport(hass: HomeAssistant, d: dict, udp_manager=None) -> str:
    """Read the endpoint-declared SIP signaling transport."""
    value = str(d.get("sip_transport") or d.get("transport") or "").lower()
    return value if value in ("udp", "tcp") else _select_transport_type(hass, d.get("host"))


async def _async_build_peer_snapshot(hass: HomeAssistant) -> list[Peer]:
    """Snapshot of every online peer (ESPs + HA itself).

    HA is appended last as kind="ha". Consumers format this into the
    HA phonebook sensor used by ESP SIP dial plans.
    """
    devices = await _get_intercom_devices(hass)
    cfg = _get_transport_config(hass)
    out: list[Peer] = []
    for d in devices:
        name = d.get("name") or ""
        host = d.get("host") or ""
        if not name or not host:
            continue
        if not _device_is_phonebook_available(hass, d):
            _LOGGER.debug("Skipping offline intercom peer from phonebook: %s", name or host)
            continue
        sip_transport = _device_transport(hass, d)
        out.append(Peer(
            kind="esp",
            device=d,
            name=name,
            host=host,
            transport="sip",
            tcp_port=0,
            udp_audio_port=0,
            udp_control_port=0,
            sip_port=int(d.get("sip_port") or cfg["sip_port"]),
            rtp_port=int(d.get("rtp_port") or cfg["rtp_port"]),
            audio_mode=d.get("audio_mode", "full_duplex"),
            tx_formats=list(d.get("tx_formats") or []),
            rx_formats=list(d.get("rx_formats") or []),
        ))
        d["sip_transport"] = sip_transport
    ha_host = await _ha_advertise_host(hass)
    if ha_host:
        out.append(Peer(
            kind="ha",
            device=None,
            name=_ha_peer_name(hass),
            host=ha_host,
            transport="sip",
            tcp_port=0,
            udp_audio_port=0,
            udp_control_port=0,
            sip_port=cfg["sip_port"],
            rtp_port=cfg["rtp_port"],
            audio_mode="full_duplex",
        ))
    else:
        _LOGGER.warning(
            "Cannot determine HA announce IP (network.async_get_announce_addresses "
            "returned empty); HA will not appear in the SIP phonebook until this is fixed."
        )
    return out


def _format_entry_unified(peer: Peer) -> str:
    """Authoritative SIP phonebook entry."""
    name = peer.name
    peer_ip = peer.host or ""
    if not peer_ip:
        return name
    tx = ";".join(peer.tx_formats or [])
    rx = ";".join(peer.rx_formats or [])
    sip_transport = str((peer.device or {}).get("sip_transport") or "tcp").lower()
    if sip_transport not in {"tcp", "udp"}:
        sip_transport = "tcp"
    return (
        f"{name}|sip|{peer_ip}|{peer.sip_port or 5060}|"
        f"{peer.rtp_port or 40000}|{peer.audio_mode}|{tx}|{rx}|{sip_transport}"
    )


def _sip_uri_transport(uri) -> str:
    for key, value in getattr(uri, "params", ()) or ():
        if str(key).lower() == "transport" and str(value or "").lower() in {"tcp", "udp"}:
            return str(value).upper()
    return "UDP"


async def _resolve_target_device(hass: HomeAssistant, call: ServiceCall) -> dict | None:
    """Thin wrapper over IntercomDeviceResolver.resolve_target."""
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
    devices = await _get_intercom_devices(hass)
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
                    f"intercom_native.{op_label}: no intercom device matches the target"
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
    bucket = hass.data.setdefault(DOMAIN, {})
    active = bucket.setdefault("sip_clients", {})
    if result not in {"ringing", "in_call"}:
        hass.async_create_task(client.close())
        return

    active[client.dialog_ids.call_id] = client
    if result != "ringing":
        return

    async def _watch_sip_final() -> None:
        try:
            final = await client.wait_for_final()
        except asyncio.CancelledError:
            raise
        except TimeoutError:
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
                sip_status_code=200,
                last_sip_event="SIP_RESPONSE",
                sip_uri=sip_uri,
            )
        elif public_final not in {CallState.RINGING.value, CallState.IN_CALL.value}:
            _set_ha_softphone_call_state(
                hass,
                public_final,
                session_device_id=HA_SOFTPHONE_DEVICE_ID,
                caller=_ha_peer_name(hass),
                callee=target,
                peer_name=target,
                direction="outgoing",
                call_id=client.dialog_ids.call_id,
                reason=public_final,
                terminal_reason=public_final,
                sip_status_code=client.last_sip_status_code,
                last_sip_event=client.last_sip_event or "SIP_RESPONSE",
                sip_uri=sip_uri,
            )
        payload = {
            "state": public_final,
            "scope": "sip",
            "call_id": client.dialog_ids.call_id,
            "target": target,
            "terminal_reason": "" if public_final in {CallState.RINGING.value, CallState.IN_CALL.value} else public_final,
        }
        if sip_uri:
            payload["sip_uri"] = sip_uri
        _fire_call_event(hass, payload, "sip")
        if final not in {"ringing", "in_call"}:
            active.pop(client.dialog_ids.call_id, None)
            await client.close()

    hass.async_create_task(_watch_sip_final())


async def _handle_purge_devices_service(call: ServiceCall) -> None:
    """Remove stale intercom devices."""
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
        devices = await _get_intercom_devices(hass)
        for device in devices:
            entity_id = (device.get("entities") or {}).get("intercom_state")
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
        _LOGGER.info("Purged %d intercom device(s): %s", len(purged), ", ".join(purged))
    else:
        _LOGGER.info("Purge: no stale intercom devices to remove")


async def _handle_sip_answer_service(call: ServiceCall) -> None:
    hass: HomeAssistant = call.hass
    call_id = str(call.data.get("call_id") or "").strip()
    pending = hass.data.get(DOMAIN, {}).setdefault("sip_pending", {})
    if not call_id and len(pending) == 1:
        call_id = next(iter(pending))
    invite = pending.pop(call_id, None) if call_id else None
    if invite is None:
        _LOGGER.warning("sip_answer: no pending SIP call %s", call_id or "(current)")
        return

    cfg = _get_transport_config(hass)
    local_ip = await _ha_advertise_host(hass)
    from .sdp import build_answer_directional
    answer = build_answer_directional(
        local_ip,
        local_ip,
        int(cfg["rtp_port"]),
        invite.send_format,
        invite.recv_format,
    )
    if not _sip_send_final_response(hass, call_id, 200, "OK", answer_sdp=answer):
        _LOGGER.warning("sip_answer: SIP transaction not found for %s", call_id)
        return

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
    )


async def _handle_sip_decline_service(call: ServiceCall) -> None:
    hass: HomeAssistant = call.hass
    call_id = str(call.data.get("call_id") or "").strip()
    status = int(call.data.get("status") or 486)
    reason = str(call.data.get("reason") or "Busy Here").strip() or "Busy Here"
    app_reason = str(call.data.get("decline_reason") or "").strip() or (
        TerminalReason.DECLINED.value if reason == "Busy Here" else reason
    )
    pending = hass.data.get(DOMAIN, {}).setdefault("sip_pending", {})
    if not call_id and len(pending) == 1:
        call_id = next(iter(pending))
    pending.pop(call_id, None)
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


async def _handle_sip_hangup_service(call: ServiceCall) -> None:
    hass: HomeAssistant = call.hass
    call_id = str(call.data.get("call_id") or "").strip()
    bucket = hass.data.get(DOMAIN, {})
    clients = bucket.setdefault("sip_clients", {})
    relays = bucket.setdefault("sip_relays", {})
    pending = bucket.setdefault("sip_pending", {})
    if not call_id and len(clients) == 1:
        call_id = next(iter(clients))
    if not call_id and len(pending) == 1:
        call_id = next(iter(pending))
    client = clients.pop(call_id, None) if call_id else None
    relay = relays.pop(call_id, None) if call_id else None
    pending_ids = [call_id] if call_id and call_id in pending else ([] if call_id else list(pending))
    server_bye = False
    pending_closed = 0
    if client is not None:
        client.bye_or_cancel()
        await client.close()
    if relay is not None:
        await relay.stop()
    for pending_call_id in pending_ids:
        invite = pending.pop(pending_call_id, None)
        if invite is None:
            continue
        if _sip_send_final_response(
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
    if client is None and relay is None:
        for server in _sip_servers(hass):
            send_bye = getattr(server, "send_bye", None)
            if callable(send_bye) and send_bye(call_id):
                server_bye = True
                if not call_id:
                    call_id = "(active)"
                break
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
    state = hass.states.get("sensor.intercom_phonebook")
    if state is None:
        return ""
    return str(state.attributes.get("roster_json") or "")


async def _push_roster_json_to_esps(hass: HomeAssistant, roster_json: str) -> None:
    """Push the canonical JSON roster to every online ESP endpoint."""
    if not roster_json:
        return
    devices = await _get_intercom_devices(hass)
    services = _available_esphome_services(hass)
    for device in devices:
        if not device.get("host"):
            continue
        slug = _resolve_esphome_route_id(hass, device["host"])
        if not slug:
            _LOGGER.debug("Phonebook push skipped for %s: no ESPHome route id", device.get("name"))
            continue
        service_slug = _resolve_esphome_service_slug(hass, slug, "set_roster_json")
        service_name = f"{service_slug}_set_roster_json"
        if service_name not in services:
            _LOGGER.debug("Phonebook push skipped for %s: missing esphome.%s", device.get("name"), service_name)
            continue
        try:
            await hass.services.async_call(
                "esphome",
                service_name,
                {"roster_json": roster_json},
                blocking=True,
            )
            _LOGGER.info("Phonebook JSON pushed to %s via esphome.%s", device.get("name"), service_name)
        except Exception as err:
            _LOGGER.error("Phonebook JSON push to %s failed: %s", device.get("name"), err)


async def _refresh_and_push_phonebook(hass: HomeAssistant) -> None:
    await _refresh_phonebook_sensor(hass)
    roster_json = await _current_roster_json(hass)
    await _push_roster_json_to_esps(hass, roster_json)


async def _handle_phonebook_add_contact_service(call: ServiceCall) -> None:
    from .roster import RosterEntry

    hass: HomeAssistant = call.hass
    def _metadata_value(key: str):
        value = call.data.get(key)
        if value in (None, ""):
            return None
        return value

    metadata = {
        key: _metadata_value(key)
        for key in (
            "transport",
            "sip_transport",
            "signaling_transport",
            "sip_port",
            "rtp_port",
            "tx_rate",
            "rx_rate",
            "tx_formats",
            "rx_formats",
            "max_payload_bytes",
            "audio_mode",
        )
        if key in call.data and _metadata_value(key) is not None
    }
    metadata["transport"] = "sip"
    entry = RosterEntry(
        id=str(call.data["id"]).strip(),
        name=str(call.data.get("name") or call.data["id"]).strip(),
        kind=str(call.data.get("kind") or "esp").strip().lower(),
        address=str(call.data.get("address") or "").strip(),
        sip_uri=str(call.data.get("sip_uri") or "").strip(),
        number=str(call.data.get("number") or "").strip(),
        ha_bridge=bool(call.data.get("ha_bridge", False)),
        metadata=metadata,
    )
    bucket = hass.data.setdefault(DOMAIN, {}).setdefault("manual_roster_entries", [])
    bucket[:] = [item for item in bucket if getattr(item, "id", "").lower() != entry.id.lower()]
    bucket.append(entry)
    await _refresh_and_push_phonebook(hass)
    _LOGGER.info("Phonebook contact added: %s (%s)", entry.id, entry.kind)


async def _handle_phonebook_set_contacts_service(call: ServiceCall) -> None:
    from .roster import parse_roster_json

    hass: HomeAssistant = call.hass
    entries = parse_roster_json(str(call.data.get("roster_json") or "[]"))
    hass.data.setdefault(DOMAIN, {})["manual_roster_entries"] = entries
    await _refresh_and_push_phonebook(hass)
    _LOGGER.info("Phonebook manual contacts replaced: %d entries", len(entries))


async def _handle_phonebook_clear_service(call: ServiceCall) -> None:
    hass: HomeAssistant = call.hass
    hass.data.setdefault(DOMAIN, {})["manual_roster_entries"] = []
    await _refresh_and_push_phonebook(hass)
    _LOGGER.info("Phonebook manual contacts cleared")


async def _handle_phonebook_export_service(call: ServiceCall) -> None:
    hass: HomeAssistant = call.hass
    sensor = hass.data.get(DOMAIN, {}).get("phonebook_sensor")
    if sensor is not None:
        await sensor.async_update()
        roster_json = sensor.extra_state_attributes.get("roster_json", "")
    else:
        roster_json = ""
    _fire_call_event(
        hass,
        {
            "state": "phonebook_export",
            "roster_json": roster_json,
            "call_id": "",
        },
        "phonebook",
    )
    _LOGGER.info("Phonebook exported (%d bytes)", len(roster_json))


async def _handle_phonebook_push_service(call: ServiceCall) -> None:
    hass: HomeAssistant = call.hass
    sensor = hass.data.get(DOMAIN, {}).get("phonebook_sensor")
    if sensor is not None:
        await sensor.async_update()
        roster_json = sensor.extra_state_attributes.get("roster_json", "")
    else:
        state = hass.states.get("sensor.intercom_phonebook")
        roster_json = str(state.attributes.get("roster_json") or "") if state is not None else ""
    await _push_roster_json_to_esps(hass, roster_json)
    _LOGGER.info("Phonebook push requested (%d bytes)", len(roster_json))


async def _handle_set_dnd_service(call: ServiceCall) -> None:
    hass: HomeAssistant = call.hass
    enabled = bool(call.data.get("dnd"))
    store = _ha_softphone_store(hass)
    store["dnd"] = enabled
    await _async_save_ha_softphone_store(hass)
    state = _ha_softphone_state(hass)
    _fire_call_event(hass, state, "session")
    _LOGGER.info("HA softphone DND set to %s via service", enabled)


async def _handle_sip_call_target_service(call: ServiceCall, *, force_ha_bridge: bool = False) -> None:
    """Originate a standards SIP call from HA to a roster target or URI-shaped target."""
    from homeassistant.exceptions import ServiceValidationError

    from .roster import parse_roster_json, resolve_target
    from .sip import parse_sip_uri
    from .sip_client import SipCallClient

    hass: HomeAssistant = call.hass
    target = str(
        call.data.get("destination") or call.data.get("target") or call.data.get("call") or ""
    ).strip()
    if not target:
        raise ServiceValidationError("target is required")
    cfg = _get_transport_config(hass)
    local_ip = await _ha_advertise_host(hass)
    if not local_ip:
        raise ServiceValidationError("HA advertise IP is unknown")
    sensor = hass.states.get("sensor.intercom_phonebook")
    roster_json = str(sensor.attributes.get("roster_json") or "") if sensor is not None else ""
    contacts = parse_roster_json(roster_json) if roster_json else []
    route = resolve_target(
        target,
        contacts,
        ha_host=local_ip,
        ha_sip_port=int(cfg["sip_port"]),
        force_ha=force_ha_bridge or bool(call.data.get("ha_bridge", False)),
    )
    if route.kind == "requires_bridge":
        raise ServiceValidationError(f"{target} requires an HA SIP bridge route")
    if route.kind not in {"direct", "bridge"} or not route.sip_uri:
        raise ServiceValidationError(f"cannot resolve SIP target: {target}")
    uri = parse_sip_uri(route.sip_uri)
    sip_send_formats, sip_recv_formats = _sip_target_audio_profile(
        remote_tx_formats=_roster_entry_formats(route.entry, "tx_formats"),
        remote_rx_formats=_roster_entry_formats(route.entry, "rx_formats"),
        target=target,
    )
    client = SipCallClient(
        local_ip=local_ip,
        local_name=_ha_peer_name(hass),
        local_sip_port=int(cfg["sip_port"]),
        local_rtp_port=int(cfg["rtp_port"]),
        supported_send_formats=sip_send_formats,
        supported_recv_formats=sip_recv_formats,
        signaling_transport=_sip_uri_transport(uri),
    )
    _set_ha_softphone_call_state(
        hass,
        CallState.CALLING.value,
        session_device_id=HA_SOFTPHONE_DEVICE_ID,
        caller=_ha_peer_name(hass),
        callee=target,
        peer_name=target,
        direction="outgoing",
        call_id=client.dialog_ids.call_id,
        sip_transport=_sip_uri_transport(uri).lower(),
        last_sip_event="INVITE",
        sip_uri=route.sip_uri,
    )
    result = await client.invite(
        target=uri.user,
        remote_host=uri.host,
        remote_sip_port=uri.port or int(cfg["sip_port"]),
    )
    public_result = _sip_public_state(result)
    _track_outbound_sip_client(
        hass,
        client=client,
        result=result,
        target=target,
        sip_uri=route.sip_uri,
    )
    if public_result == CallState.REMOTE_RINGING.value or result == "ringing":
        _set_ha_softphone_call_state(
            hass,
            CallState.REMOTE_RINGING.value,
            session_device_id=HA_SOFTPHONE_DEVICE_ID,
            caller=_ha_peer_name(hass),
            callee=target,
            peer_name=target,
            direction="outgoing",
            call_id=client.dialog_ids.call_id,
            sip_status_code=180,
            last_sip_event="SIP_RESPONSE",
            sip_uri=route.sip_uri,
        )
    elif public_result == CallState.IN_CALL.value and client.dialog is not None:
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
            sip_status_code=200,
            last_sip_event="SIP_RESPONSE",
            sip_uri=route.sip_uri,
        )
    elif public_result not in {CallState.REMOTE_RINGING.value, CallState.IN_CALL.value}:
        _set_ha_softphone_call_state(
            hass,
            public_result,
            session_device_id=HA_SOFTPHONE_DEVICE_ID,
            caller=_ha_peer_name(hass),
            callee=target,
            peer_name=target,
            direction="outgoing",
            call_id=client.dialog_ids.call_id,
            reason=public_result,
            terminal_reason=public_result,
            sip_status_code=client.last_sip_status_code,
            last_sip_event=client.last_sip_event or "SIP_RESPONSE",
            sip_uri=route.sip_uri,
        )
    _fire_call_event(
        hass,
        {
            "state": public_result,
            "scope": "sip",
            "call_id": client.dialog_ids.call_id,
            "target": target,
            "sip_uri": route.sip_uri,
            "terminal_reason": "" if public_result in {CallState.RINGING.value, CallState.IN_CALL.value} else public_result,
        },
        "sip",
    )
    _LOGGER.info("SIP call target=%s uri=%s result=%s", target, route.sip_uri, result)


async def _handle_sip_forward_service(call: ServiceCall) -> None:
    """Forward a SIP call through HA's dial plan/B2BUA path."""
    await _handle_sip_call_target_service(call, force_ha_bridge=True)


async def _async_register_services(hass: HomeAssistant) -> None:
    """Register HA services for SIP phone control."""

    async def handle_purge_devices(call: ServiceCall) -> None:
        await _handle_purge_devices_service(call)

    async def handle_sip_answer(call: ServiceCall) -> None:
        await _handle_sip_answer_service(call)

    async def handle_sip_decline(call: ServiceCall) -> None:
        await _handle_sip_decline_service(call)

    async def handle_sip_hangup(call: ServiceCall) -> None:
        await _handle_sip_hangup_service(call)

    async def handle_phonebook_add_contact(call: ServiceCall) -> None:
        await _handle_phonebook_add_contact_service(call)

    async def handle_phonebook_set_contacts(call: ServiceCall) -> None:
        await _handle_phonebook_set_contacts_service(call)

    async def handle_phonebook_clear(call: ServiceCall) -> None:
        await _handle_phonebook_clear_service(call)

    async def handle_phonebook_export(call: ServiceCall) -> None:
        await _handle_phonebook_export_service(call)

    async def handle_phonebook_push(call: ServiceCall) -> None:
        await _handle_phonebook_push_service(call)

    async def handle_sip_set_dnd(call: ServiceCall) -> None:
        await _handle_set_dnd_service(call)

    async def handle_sip_call(call: ServiceCall) -> None:
        await _handle_sip_call_target_service(call)

    async def handle_sip_forward(call: ServiceCall) -> None:
        await _handle_sip_forward_service(call)

    target_fields = {
        vol.Optional("device_id"): vol.Any(cv.string, [cv.string]),
        vol.Optional("entity_id"): vol.Any(cv.entity_id, [cv.entity_id]),
        vol.Optional("name"): cv.string,
        vol.Optional("friendly_name"): cv.string,
    }
    purge_schema = vol.Schema(
        {**target_fields, vol.Optional("min_unavailable_hours", default=0): vol.Coerce(float)},
        extra=vol.PREVENT_EXTRA,
    )
    sip_answer_schema = vol.Schema(
        {vol.Optional("call_id", default=""): cv.string},
        extra=vol.PREVENT_EXTRA,
    )
    sip_decline_schema = vol.Schema(
        {
            vol.Optional("call_id", default=""): cv.string,
            vol.Optional("status", default=486): vol.Coerce(int),
            vol.Optional("reason", default="Busy Here"): cv.string,
            vol.Optional("decline_reason", default=""): cv.string,
        },
        extra=vol.PREVENT_EXTRA,
    )
    sip_hangup_schema = vol.Schema(
        {vol.Optional("call_id", default=""): cv.string},
        extra=vol.PREVENT_EXTRA,
    )
    sip_call_schema = vol.Schema(
        {
            vol.Optional("destination"): cv.string,
            vol.Optional("target"): cv.string,
            vol.Optional("call"): cv.string,
            vol.Optional("ha_bridge", default=False): cv.boolean,
        },
        extra=vol.PREVENT_EXTRA,
    )
    phonebook_add_schema = vol.Schema(
        {
            vol.Required("id"): cv.string,
            vol.Optional("name", default=""): cv.string,
            vol.Optional("kind", default="esp"): vol.In(["ha", "esp", "phone", "sip", "group"]),
            vol.Optional("address", default=""): cv.string,
            vol.Optional("sip_uri", default=""): cv.string,
            vol.Optional("number", default=""): cv.string,
            vol.Optional("ha_bridge", default=False): cv.boolean,
            vol.Optional("transport", default="sip"): vol.Any("", vol.In(["sip"])),
            vol.Optional("sip_transport", default=""): vol.Any("", vol.In(["tcp", "udp"])),
            vol.Optional("signaling_transport", default=""): vol.Any("", vol.In(["tcp", "udp"])),
            vol.Optional("sip_port"): vol.Coerce(int),
            vol.Optional("rtp_port"): vol.Coerce(int),
            vol.Optional("tx_rate"): vol.Any("auto", vol.Coerce(int)),
            vol.Optional("rx_rate"): vol.Any("auto", vol.Coerce(int)),
            vol.Optional("tx_formats"): vol.Any(cv.string, [cv.string]),
            vol.Optional("rx_formats"): vol.Any(cv.string, [cv.string]),
            vol.Optional("max_payload_bytes"): vol.Coerce(int),
            vol.Optional("audio_mode", default=""): cv.string,
        },
        extra=vol.PREVENT_EXTRA,
    )
    phonebook_set_schema = vol.Schema(
        {vol.Required("roster_json"): cv.string},
        extra=vol.PREVENT_EXTRA,
    )
    set_dnd_schema = vol.Schema(
        {vol.Required("dnd"): cv.boolean},
        extra=vol.PREVENT_EXTRA,
    )
    hass.services.async_register(DOMAIN, "purge_devices", handle_purge_devices, schema=purge_schema)
    hass.services.async_register(DOMAIN, "sip_answer", handle_sip_answer, schema=sip_answer_schema)
    hass.services.async_register(DOMAIN, "sip_decline", handle_sip_decline, schema=sip_decline_schema)
    hass.services.async_register(DOMAIN, "sip_hangup", handle_sip_hangup, schema=sip_hangup_schema)
    hass.services.async_register(DOMAIN, "sip_call", handle_sip_call, schema=sip_call_schema)
    hass.services.async_register(DOMAIN, "sip_forward", handle_sip_forward, schema=sip_call_schema)
    hass.services.async_register(
        DOMAIN, "phonebook_add_contact", handle_phonebook_add_contact, schema=phonebook_add_schema
    )
    hass.services.async_register(
        DOMAIN, "phonebook_set_contacts", handle_phonebook_set_contacts, schema=phonebook_set_schema
    )
    hass.services.async_register(DOMAIN, "phonebook_clear", handle_phonebook_clear)
    hass.services.async_register(DOMAIN, "phonebook_export", handle_phonebook_export)
    hass.services.async_register(DOMAIN, "phonebook_push", handle_phonebook_push)
    hass.services.async_register(DOMAIN, "sip_set_dnd", handle_sip_set_dnd, schema=set_dnd_schema)


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

    _LOGGER.info("Intercom Native loaded (SIP softphone + SIP B2BUA/router)")


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up Intercom Native defaults from configuration.yaml."""
    hass.data.setdefault(DOMAIN, {})["transport_config"] = {
        "use_sip": True,
        "sip_port": INTERCOM_SIP_PORT,
        "rtp_port": INTERCOM_RTP_PORT,
    }
    hass.data[DOMAIN]["sip_port"] = INTERCOM_SIP_PORT
    await _async_setup_shared(hass, config)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Intercom Native from a config entry (UI setup)."""
    cfg = _entry_transport_config(entry)
    hass.data.setdefault(DOMAIN, {})["transport_config"] = cfg
    hass.data[DOMAIN]["sip_port"] = cfg["sip_port"]
    await _async_setup_shared(hass)
    await _async_apply_assist_intents(
        hass,
        bool(entry.data.get(CONF_ASSIST_INTENTS, False)),
    )
    if cfg["use_sip"]:
        if not await _async_start_sip_udp_server(hass):
            raise ConfigEntryError(
                f"Failed to bind SIP port {cfg['sip_port']}. Another SIP "
                "endpoint may already be listening on that port."
            )
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry."""
    await _async_apply_assist_intents(hass, False)
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)

    # Stop sessions / bridges before tearing down listeners; otherwise
    # orphaned transports leak sockets across config-entry reload.
    from .websocket_api import _async_shutdown_all
    await _async_shutdown_all()

    await _async_stop_sip_udp_server(hass)
    unsub = hass.data.get(DOMAIN, {}).pop("esp_state_event_bridge_unsub", None)
    if unsub is not None:
        unsub()
    hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unload_ok


async def _async_start_sip_udp_server(hass: HomeAssistant) -> bool:
    """Bind the SIP/UDP endpoint for standards-compatible phase-1 calls."""
    from .roster import RosterEntry, resolve_target
    from .sdp import build_answer_directional
    from .sip import parse_sip_uri
    from .sip_client import SipCallClient
    from .sip_endpoint import SipEndpointManager
    from .sip_listener import SipInvite, SipInviteResult
    from .sip_rtp_bridge import RtpPeer, SipRtpRelay

    if hass.data.get(DOMAIN, {}).get("sip_endpoint") is not None:
        return True

    cfg = _get_transport_config(hass)
    local_ip = await _ha_advertise_host(hass)
    if not local_ip:
        _LOGGER.error("Cannot start SIP endpoint: HA announce IP is unknown")
        return False

    def _roster_from_peers(peers: list[Peer]) -> list[RosterEntry]:
        entries: list[RosterEntry] = []
        for peer in peers:
            entries.append(
                RosterEntry(
                    id=peer.name,
                    name=peer.name,
                    kind="ha" if peer.is_ha else "esp",
                    address=peer.host,
                    metadata={
                        "transport": "sip",
                        "sip_transport": (
                            str((peer.device or {}).get("sip_transport") or "tcp").lower()
                            if peer.is_ha or peer.transport == "sip"
                            else ""
                        ),
                        "sip_port": peer.sip_port,
                        "rtp_port": peer.rtp_port,
                        "audio_mode": peer.audio_mode,
                    },
                )
            )
        return entries

    def _same_route_name(left: str, right: str) -> bool:
        def norm(value: str) -> str:
            return "".join(ch for ch in value.lower() if ch.isalnum())

        return bool(left and right and norm(left) == norm(right))

    def _peer_for_target(target: str, peers: list[Peer]) -> Peer | None:
        for peer in peers:
            if peer.is_ha:
                continue
            if _same_route_name(target, peer.name):
                return peer
        return None

    async def _on_invite(invite: SipInvite) -> SipInviteResult:
        peers = await _async_build_peer_snapshot(hass)
        decision = resolve_target(invite.target, _roster_from_peers(peers), ha_bridge=True)
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
                "route_kind": decision.kind,
                "sip_uri": decision.sip_uri,
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
            decision.kind,
            decision.sip_uri or "-",
            invite.selected_format.encoding,
            invite.selected_format.sample_rate,
        )
        if decision.kind == "requires_bridge":
            return SipInviteResult(503, "Service Unavailable", to_tag="")
        if decision.reason == "ha_required":
            return SipInviteResult(404, "Not Found", to_tag="")
        if decision.kind in {"direct", "bridge", "group"} and decision.entry is not None:
            peer_target = _peer_for_target(invite.target, peers)
            bridge_uri = None
            if peer_target is not None and peer_target.host:
                sip_transport = str((peer_target.device or {}).get("sip_transport") or "tcp").lower()
                if sip_transport not in {"tcp", "udp"}:
                    sip_transport = "tcp"
                bridge_uri = parse_sip_uri(
                    f"sip:{invite.target}@{peer_target.host}:{peer_target.sip_port or cfg['sip_port']};transport={sip_transport}"
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
                client = SipCallClient(
                    local_ip=local_ip,
                    local_name=invite.caller or _ha_peer_name(hass),
                    local_sip_port=int(cfg["sip_port"]),
                    local_rtp_port=dest_relay_port,
                    supported_send_formats=[invite.recv_format.audio_format],
                    supported_recv_formats=[invite.send_format.audio_format],
                    signaling_transport=_sip_uri_transport(decision_uri),
                )
                result = await client.invite(
                    target=decision_uri.user,
                    remote_host=decision_uri.host,
                    remote_sip_port=decision_uri.port or int(cfg["sip_port"]),
                )
                if result not in {"ringing", "in_call"}:
                    decline_reason = result if result and result != "sip_486" else "busy"
                    await client.close()
                    return SipInviteResult(
                        486,
                        "Busy Here",
                        to_tag="",
                        decline_reason=decline_reason,
                    )
                active = bucket.setdefault("sip_clients", {})
                active[client.dialog_ids.call_id] = client
                bucket.setdefault("sip_bridge_clients", {})[invite.call_id] = client.dialog_ids.call_id

                async def _finish_bridge(initial_result: str) -> None:
                    final = initial_result
                    if final == "ringing":
                        final = await client.wait_for_final()
                    if final != "in_call" or client.dialog is None:
                        decline_reason = final if final and final != "sip_486" else "busy"
                        status_code = 486
                        public_state = _sip_public_state(final)
                        if public_state == CallState.MEDIA_INCOMPATIBLE.value:
                            status_code = 488
                        elif public_state == CallState.CANCELLED.value:
                            status_code = 487
                        _sip_send_final_response(
                            hass,
                            invite.call_id,
                            status_code,
                            "Not Acceptable Here" if status_code == 488 else "Request Terminated" if status_code == 487 else "Busy Here",
                            decline_reason=decline_reason,
                        )
                        bucket.setdefault("sip_bridge_clients", {}).pop(invite.call_id, None)
                        active.pop(client.dialog_ids.call_id, None)
                        await client.close()
                        return
                    relay = SipRtpRelay(
                        left=RtpPeer(
                            host=invite.remote_rtp_host,
                            port=invite.remote_rtp_port,
                            payload_type=invite.recv_format.payload_type,
                            audio_format=invite.recv_format.audio_format,
                            send_payload_type=invite.send_format.payload_type,
                            send_audio_format=invite.send_format.audio_format,
                        ),
                        right=RtpPeer(
                            host=client.dialog.remote_rtp_host,
                            port=client.dialog.remote_rtp_port,
                            payload_type=client.dialog.recv_format.payload_type,
                            audio_format=client.dialog.recv_format.audio_format,
                            send_payload_type=client.dialog.send_format.payload_type,
                            send_audio_format=client.dialog.send_format.audio_format,
                        ),
                        left_port=source_relay_port,
                        right_port=dest_relay_port,
                    )
                    await relay.start()
                    bucket.setdefault("sip_relays", {})[invite.call_id] = relay
                    answer = build_answer_directional(
                        local_ip,
                        local_ip,
                        source_relay_port,
                        invite.send_format,
                        invite.recv_format,
                    )
                    _sip_send_final_response(hass, invite.call_id, 200, "OK", answer_sdp=answer)
                    _set_ha_softphone_call_state(
                        hass,
                        CallState.IN_CALL.value,
                        session_device_id=HA_SOFTPHONE_DEVICE_ID,
                        caller=invite.caller,
                        callee=invite.target,
                        peer_name=invite.target,
                        direction="incoming",
                        call_id=invite.call_id,
                        selected_tx_format=invite.send_format.audio_format.wire_token(),
                        selected_rx_format=invite.recv_format.audio_format.wire_token(),
                        sip_status_code=200,
                        last_sip_event="SIP_RESPONSE",
                        route_kind=decision.kind,
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

                hass.async_create_task(_finish_bridge(result))
                return SipInviteResult(180, "Ringing", to_tag="", defer_final=True)
            pending = hass.data.setdefault(DOMAIN, {}).setdefault("sip_pending", {})
            active_dialogs = sum(_sip_active_dialog_count(item) for item in _sip_servers(hass))
            if _sessions or pending or active_dialogs:
                _LOGGER.info(
                    "SIP INVITE from %s rejected: HA softphone is busy (sessions=%d pending=%d active_dialogs=%d)",
                    invite.caller or invite.source_host,
                    len(_sessions),
                    len(pending),
                    active_dialogs,
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
                audio_mode="full_duplex",
                route_kind=decision.kind,
                sip_uri=decision.sip_uri,
                sip_status_code=180,
                last_sip_event="INVITE",
            )
            return SipInviteResult(180, "Ringing", to_tag="", defer_final=True)
        answer = build_answer_directional(
            local_ip,
            local_ip,
            int(cfg["rtp_port"]),
            invite.send_format,
            invite.recv_format,
        )
        return SipInviteResult(200, "OK", answer_sdp=answer, to_tag="")

    async def _on_terminated(call_id: str, reason: str = "remote_hangup") -> None:
        bucket = hass.data.setdefault(DOMAIN, {})
        pending = bucket.setdefault("sip_pending", {})
        invite = pending.pop(call_id, None)
        session = _session_pop(call_id) or _session_pop(HA_SOFTPHONE_DEVICE_ID)
        softphone_store = bucket.get("ha_softphone", {})
        softphone_call_id = str(softphone_store.get("call_id") or "")
        terminal_reason = reason or "remote_hangup"
        terminal_state = (
            CallState.CANCELLED.value
            if terminal_reason == TerminalReason.CANCELLED.value
            else CallState.IDLE.value
        )
        if session is not None:
            await session.stop(send_signaling=False)
        if (
            invite is not None
            or session is not None
            or (call_id and softphone_call_id == call_id)
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
        relay = bucket.setdefault("sip_relays", {}).pop(call_id, None)
        if relay is not None:
            await relay.stop()
        dest_call_id = bucket.setdefault("sip_bridge_clients", {}).pop(call_id, "")
        client = bucket.setdefault("sip_clients", {}).pop(dest_call_id, None) if dest_call_id else None
        if client is not None:
            client.bye()
            await client.close()
        if relay is not None or client is not None:
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
    )
    if not await endpoint.start():
        return False
    hass.data[DOMAIN]["sip_endpoint"] = endpoint
    hass.data[DOMAIN]["sip_server"] = endpoint.udp_server
    hass.data[DOMAIN]["sip_tcp_server"] = endpoint.tcp_server
    _LOGGER.info("SIP endpoint enabled on UDP+TCP/%s (RTP base %s)", cfg["sip_port"], cfg["rtp_port"])
    return True


async def _async_stop_sip_udp_server(hass: HomeAssistant) -> None:
    hass.data.get(DOMAIN, {}).pop("sip_bridge_clients", None)
    relays = hass.data.get(DOMAIN, {}).pop("sip_relays", {})
    for relay in list(relays.values()):
        try:
            await relay.stop()
        except Exception:
            _LOGGER.debug("Ignoring SIP RTP relay stop error", exc_info=True)
    clients = hass.data.get(DOMAIN, {}).pop("sip_clients", {})
    for client in list(clients.values()):
        try:
            client.bye()
            await client.close()
        except Exception:
            _LOGGER.debug("Ignoring SIP client stop error", exc_info=True)
    endpoint = hass.data.get(DOMAIN, {}).pop("sip_endpoint", None)
    hass.data.get(DOMAIN, {}).pop("sip_server", None)
    hass.data.get(DOMAIN, {}).pop("sip_tcp_server", None)
    if endpoint is not None:
        await endpoint.stop()
