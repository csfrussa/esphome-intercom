"""VoIP Stack integration for Home Assistant.

HA is a SIP softphone and SIP B2BUA/router for ESPHome SIP phones. Public call
control is expressed in SIP/SDP/RTP terms only; logical targets are resolved by
the central phonebook and routed through HA as SIP dialogs when needed.
"""

import asyncio
import contextlib
from dataclasses import replace
import logging
import socket
import time

import voluptuous as vol

from homeassistant.core import HomeAssistant, CoreState, Event, ServiceCall, callback
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform, EVENT_HOMEASSISTANT_STARTED, EVENT_STATE_CHANGED
from homeassistant.exceptions import ConfigEntryError

PLATFORMS: list[Platform] = [Platform.SENSOR]
from homeassistant.helpers import config_validation as cv
from homeassistant.components import network, persistent_notification

from .call_registry import CallRegistry
from .const import (
    CONF_ASSIST_INTENTS,
    CONF_DEBUG_MODE,
    CONF_PHONEBOOK_CONTACTS,
    CONF_REGISTRAR_ENABLED,
    CONF_SIP_ACCOUNTS,
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
    VOIP_STACK_RTP_PORT,
    VOIP_STACK_SIP_PORT,
)
from .dtmf import parse_dtmf_route_map
from .device_resolver import get_resolver
from .fsm import CallState, TerminalReason, sip_phone_state
from .audio_format import (
    AudioFormat,
    HA_SIP_PCM_FORMATS,
    HA_SIP_PCM_RX_FORMATS,
    HA_SIP_PCM_TX_FORMATS,
    HA_TRUNK_AUDIO_FORMATS,
    choose_common_frame_ms,
    parse_audio_format_list,
)
from .peer import Peer
from .router import (
    CallContext,
    RouteAction,
    RouteHintSource,
    RouteReason,
    route_inbound_trunk,
)
from .sip_bridge import build_invite_client_relay
from .websocket_api import (
    async_register_websocket_api,
    _async_load_ha_softphone_store,
    _get_voip_devices,
    _fire_call_event,
    _async_save_ha_softphone_store,
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


def _call_registry(hass: HomeAssistant) -> CallRegistry:
    bucket = hass.data.setdefault(DOMAIN, {})
    registry = bucket.get("call_registry")
    if not isinstance(registry, CallRegistry):
        registry = CallRegistry()
        bucket["call_registry"] = registry
    return registry


def _config_entry(hass: HomeAssistant) -> ConfigEntry | None:
    return next(iter(hass.config_entries.async_entries(DOMAIN)), None)


def _sip_account_dicts(hass: HomeAssistant) -> list[dict]:
    entry = _config_entry(hass)
    if entry is None:
        return []
    return [dict(item) for item in entry.data.get(CONF_SIP_ACCOUNTS, []) if isinstance(item, dict)]


def _sip_accounts(hass: HomeAssistant):
    from .sip_registrar import account_from_mapping

    accounts = []
    for raw in _sip_account_dicts(hass):
        try:
            accounts.append(account_from_mapping(raw))
        except ValueError as err:
            _LOGGER.warning("Ignoring invalid SIP account in config entry: %s", err)
    return accounts


def _phonebook_contact_dicts(hass: HomeAssistant) -> list[dict]:
    entry = _config_entry(hass)
    if entry is None:
        return []
    return [dict(item) for item in entry.data.get(CONF_PHONEBOOK_CONTACTS, []) if isinstance(item, dict)]


def _manual_roster_entries(hass: HomeAssistant):
    from .roster import RosterError, parse_roster_json

    try:
        return parse_roster_json(_phonebook_contact_dicts(hass))
    except (RosterError, ValueError, TypeError) as err:
        _LOGGER.warning("Ignoring invalid manual phonebook contacts in config entry: %s", err)
        return []


def _store_manual_roster_entries(hass: HomeAssistant, entries) -> None:
    from .roster import dump_roster_json, parse_roster_json

    entry = _config_entry(hass)
    if entry is None:
        raise ConfigEntryError("VoIP Stack config entry is required for phonebook contacts")
    # Round-trip through JSON so storage is plain dict/list data.
    contacts = parse_roster_json(dump_roster_json(list(entries)))
    payload = [
        {
            "id": item.id,
            "name": item.name,
            "kind": item.kind,
            "address": item.address,
            "sip_uri": item.sip_uri,
            "number": item.number,
            "ha_bridge": item.ha_bridge,
            "enabled": item.enabled,
            "metadata": item.metadata,
        }
        for item in contacts
    ]
    data = dict(entry.data)
    data[CONF_PHONEBOOK_CONTACTS] = payload
    hass.config_entries.async_update_entry(entry, data=data)
    hass.data.setdefault(DOMAIN, {})["manual_roster_entries"] = contacts


def _update_sip_accounts(hass: HomeAssistant, accounts: list[dict]) -> None:
    entry = _config_entry(hass)
    if entry is None:
        raise ConfigEntryError("VoIP Stack config entry is required for SIP accounts")
    data = dict(entry.data)
    data[CONF_SIP_ACCOUNTS] = accounts
    hass.config_entries.async_update_entry(entry, data=data)
    registrar = hass.data.get(DOMAIN, {}).get("sip_registrar")
    if registrar is not None:
        registrar.update_accounts(_sip_accounts(hass))


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


def _device_formats(device: dict | None, key: str):
    if not device:
        return []
    value = device.get(key)
    if value in (None, ""):
        return []
    if isinstance(value, str):
        raw = value
    else:
        raw = ";".join(value or [])
    if not raw.strip():
        return []
    try:
        return parse_audio_format_list(raw)
    except ValueError as err:
        _LOGGER.warning(
            "Ignoring invalid %s on %s: %s",
            key,
            (device or {}).get("name") or (device or {}).get("device_id"),
            err,
        )
        return []


def _roster_entry_formats(entry, key: str) -> list[AudioFormat]:
    """Return audio formats from a canonical roster entry metadata field."""
    if entry is None:
        return []
    metadata = getattr(entry, "metadata", {}) or {}
    value = metadata.get(key)
    if value in (None, ""):
        return []
    if isinstance(value, list):
        raw = ";".join(str(item) for item in value)
    else:
        raw = str(value or "")
    if not raw.strip():
        return []
    try:
        return parse_audio_format_list(raw)
    except ValueError as err:
        _LOGGER.warning(
            "Ignoring invalid roster %s on %s: %s",
            key,
            getattr(entry, "display_name", None) or getattr(entry, "id", ""),
            err,
        )
        return []


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
        return CallState.TRANSPORT_UNREACHABLE.value
    if value in {"error", "protocol_error"}:
        return CallState.TRANSPORT_UNREACHABLE.value
    return value or CallState.IDLE.value


def _sip_terminal_reason(result: str, public_state: str) -> str:
    value = (result or "").strip().lower()
    if value == "timeout":
        return TerminalReason.TIMEOUT.value
    if value in {"error", "protocol_error"}:
        return TerminalReason.PROTOCOL_ERROR.value
    return public_state


def _sip_failure_response(result: str) -> tuple[int, str, str, str]:
    public_state = _sip_public_state(result)
    terminal_reason = _sip_terminal_reason(result, public_state)
    if public_state == CallState.BUSY.value:
        return 486, "Busy Here", terminal_reason, public_state
    if public_state == CallState.DECLINED.value:
        return 603, "Decline", terminal_reason, public_state
    if public_state == CallState.CANCELLED.value:
        return 487, "Request Terminated", terminal_reason, public_state
    if public_state == CallState.MEDIA_INCOMPATIBLE.value:
        return 488, "Not Acceptable Here", terminal_reason, public_state
    if terminal_reason == TerminalReason.TIMEOUT.value:
        return 408, "Request Timeout", terminal_reason, public_state
    return 480, "Temporarily Unavailable", terminal_reason, public_state


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
    if not send_candidates or not recv_candidates:
        _LOGGER.warning(
            "No compatible directional SIP PCM profile for %s "
            "(ha_send=%s ha_recv=%s remote_tx=%s remote_rx=%s)",
            target,
            [fmt.wire_token() for fmt in HA_SIP_PCM_TX_FORMATS],
            [fmt.wire_token() for fmt in HA_SIP_PCM_RX_FORMATS],
            [fmt.wire_token() for fmt in remote_tx],
            [fmt.wire_token() for fmt in remote_rx],
        )
        return [], []

    common_frame_ms = choose_common_frame_ms(send_candidates, recv_candidates)
    if common_frame_ms is None:
        _LOGGER.warning(
            "No common SIP ptime for %s (send=%s recv=%s)",
            target,
            [fmt.wire_token() for fmt in send_candidates],
            [fmt.wire_token() for fmt in recv_candidates],
        )
        return [], []

    send_candidates = [fmt for fmt in send_candidates if fmt.frame_ms == common_frame_ms]
    recv_candidates = [fmt for fmt in recv_candidates if fmt.frame_ms == common_frame_ms]
    _LOGGER.debug(
        "Directional SIP PCM profile for %s: ptime=%sms send=%s recv=%s",
        target,
        common_frame_ms,
        [fmt.wire_token() for fmt in send_candidates],
        [fmt.wire_token() for fmt in recv_candidates],
    )
    return send_candidates, recv_candidates


def _ha_peer_name(hass: HomeAssistant) -> str:
    """Return the HA phonebook peer name.

    HA normally always has a configured location_name. The default is only for
    malformed/empty local config and avoids a hardcoded "Home Assistant" peer
    identity.
    """
    return (hass.config.location_name or "").strip() or HA_PEER_FALLBACK_NAME


def _entry_transport_config(entry: ConfigEntry | None = None) -> dict:
    """Normalised SIP/RTP config."""
    data = entry.data if entry is not None else {}
    return {
        CONF_REGISTRAR_ENABLED: bool(data.get(CONF_REGISTRAR_ENABLED, False)),
        "sip_port": int(data.get("sip_port", VOIP_STACK_SIP_PORT)),
        "rtp_port": int(data.get("rtp_port", VOIP_STACK_RTP_PORT)),
        "advertise_host": (data.get("advertise_host") or "").strip(),
    }


def _entry_trunk_config(entry: ConfigEntry | None = None) -> dict:
    data = entry.data if entry is not None else {}
    return {
        CONF_TRUNK_ENABLED: bool(data.get(CONF_TRUNK_ENABLED, False)),
        CONF_TRUNK_TRANSPORT: str(data.get(CONF_TRUNK_TRANSPORT) or "udp").strip().lower(),
        CONF_TRUNK_SERVER: str(data.get(CONF_TRUNK_SERVER) or "").strip(),
        CONF_TRUNK_PORT: int(data.get(CONF_TRUNK_PORT) or VOIP_STACK_SIP_PORT),
        CONF_TRUNK_DOMAIN: str(data.get(CONF_TRUNK_DOMAIN) or "").strip(),
        CONF_TRUNK_USERNAME: str(data.get(CONF_TRUNK_USERNAME) or "").strip(),
        CONF_TRUNK_AUTH_USERNAME: str(data.get(CONF_TRUNK_AUTH_USERNAME) or "").strip(),
        CONF_TRUNK_PASSWORD: str(data.get(CONF_TRUNK_PASSWORD) or ""),
        CONF_TRUNK_EXPIRES: int(data.get(CONF_TRUNK_EXPIRES) or 300),
        CONF_TRUNK_OUTBOUND_PROXY: str(data.get(CONF_TRUNK_OUTBOUND_PROXY) or "").strip(),
        CONF_TRUNK_INBOUND_DEFAULT_TARGET: str(data.get(CONF_TRUNK_INBOUND_DEFAULT_TARGET) or "HA").strip() or "HA",
        CONF_TRUNK_DTMF_ENABLED: bool(data.get(CONF_TRUNK_DTMF_ENABLED, False)),
        CONF_TRUNK_DTMF_TIMEOUT_MS: max(100, min(2000, int(data.get(CONF_TRUNK_DTMF_TIMEOUT_MS) or 1000))),
        CONF_TRUNK_DTMF_TERMINATOR: str(data.get(CONF_TRUNK_DTMF_TERMINATOR) or "").strip(),
        CONF_TRUNK_DTMF_ROUTES: str(data.get(CONF_TRUNK_DTMF_ROUTES) or "").strip(),
    }


def _get_transport_config(hass: HomeAssistant) -> dict:
    """Return current HA-side network config.

    HA always listens for SIP signaling on both UDP and TCP.
    """
    return hass.data.get(DOMAIN, {}).get(
        "transport_config",
        {
            "sip_port": VOIP_STACK_SIP_PORT,
            "rtp_port": VOIP_STACK_RTP_PORT,
            "advertise_host": "",
        },
    )


def _get_trunk_config(hass: HomeAssistant) -> dict:
    return hass.data.get(DOMAIN, {}).get("trunk_config", _entry_trunk_config(None))


def _debug_mode(hass: HomeAssistant) -> bool:
    return bool(hass.data.get(DOMAIN, {}).get(CONF_DEBUG_MODE, False))


def _trunk_enabled(cfg: dict) -> bool:
    return bool(
        cfg.get(CONF_TRUNK_ENABLED)
        and cfg.get(CONF_TRUNK_SERVER)
        and cfg.get(CONF_TRUNK_USERNAME)
        and cfg.get(CONF_TRUNK_PASSWORD)
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
    """Legacy inference is intentionally disabled for SIP routing."""
    return ""


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


async def _terminate_sip_bridge(hass: HomeAssistant, call_id: str) -> tuple[bool, str, str, bool, bool]:
    """Terminate a B2BUA bridge by either source or destination leg call-id."""
    if not call_id:
        return False, "", "", False, False
    registry = _call_registry(hass)
    source_call_id, dest_call_id, relay, client, watcher, called_by_dest = registry.detach_bridge(call_id)
    if not source_call_id:
        return False, "", "", False, False
    if relay is not None:
        await relay.stop()

    client_closed = False
    if dest_call_id:
        if watcher is not None:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await watcher
        if client is not None and not called_by_dest:
            await client.terminate()
            await client.close()
            client_closed = True
        elif client is not None:
            await client.close()
            client_closed = True

    source_bye = _sip_send_bye(hass, source_call_id)
    registry.finish_and_pop(source_call_id, reason=TerminalReason.LOCAL_HANGUP.value)
    return True, source_call_id, dest_call_id, client_closed, source_bye


def _rtp_port_available(port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind(("0.0.0.0", int(port)))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _allocate_sip_rtp_port(hass: HomeAssistant, *, step: int = 2) -> int:
    cfg = _get_transport_config(hass)
    bucket = hass.data.setdefault(DOMAIN, {})
    base_port = int(cfg["rtp_port"])
    if _rtp_port_available(base_port):
        bucket["sip_rtp_next_port"] = base_port + int(step)
        return base_port
    candidate = int(bucket.get("sip_rtp_next_port", base_port + int(step)))
    for _ in range(64):
        if candidate == base_port:
            candidate += int(step)
            continue
        if _rtp_port_available(candidate):
            bucket["sip_rtp_next_port"] = candidate + int(step)
            return candidate
        candidate += int(step)
    return base_port


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
    """Snapshot of every online peer (ESPs + HA itself).

    HA is appended last as kind="ha". Consumers format this into the
    HA phonebook sensor used by ESP SIP dial plans.
    """
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
            kind="esp",
            device=d,
            name=name,
            host=host,
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
    sip_transport = str((peer.device or {}).get("sip_transport") or ("tcp" if peer.is_ha else "")).lower()
    if sip_transport not in {"tcp", "udp"}:
        return name
    sip_transport_token = "sip_tcp" if sip_transport == "tcp" else "sip_udp"
    return (
        f"{name}|{peer_ip}|{peer.sip_port or 5060}|"
        f"{peer.rtp_port or 40000}|{peer.audio_mode}|{tx}|{rx}|{sip_transport_token}"
    )


def _registered_roster_entries(hass: HomeAssistant):
    registrar = hass.data.get(DOMAIN, {}).get("sip_registrar")
    entries = getattr(registrar, "registered_roster_entries", None)
    return list(entries()) if callable(entries) else []


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
    if call_id:
        media_sessions.pop(call_id, None)
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


async def _push_roster_json_to_esps(hass: HomeAssistant, roster_json: str) -> None:
    """Push the canonical JSON roster to every online ESP endpoint."""
    if not roster_json:
        return
    devices = await _get_voip_devices(hass)
    services = _available_esphome_services(hass)
    for device in devices:
        if not device.get("host"):
            continue
        slug = _resolve_esphome_route_id(hass, device["host"])
        if not slug:
            _LOGGER.debug("Phonebook push skipped for %s: no ESPHome route id", device.get("name"))
            continue
        service_name = f"{slug}_set_roster_json"
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


async def _deferred_phonebook_sync(hass: HomeAssistant) -> None:
    """Push the canonical phonebook after entry setup/reload settles."""
    await asyncio.sleep(0)
    await _refresh_and_push_phonebook(hass)


async def _handle_phonebook_add_contact_service(call: ServiceCall) -> None:
    from .roster import RosterEntry

    hass: HomeAssistant = call.hass
    name = str(call.data["name"]).strip()
    entry_id = str(call.data.get("id") or name).strip()

    def _metadata_value(key: str):
        value = call.data.get(key)
        if value in (None, ""):
            return None
        return value

    metadata = {
        key: _metadata_value(key)
        for key in (
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
    entry = RosterEntry(
        id=entry_id,
        name=name,
        kind=str(call.data.get("kind") or "esp").strip().lower(),
        address=str(call.data.get("address") or "").strip(),
        sip_uri=str(call.data.get("sip_uri") or "").strip(),
        number=str(call.data.get("number") or "").strip(),
        ha_bridge=bool(call.data.get("ha_bridge", False)),
        metadata=metadata,
    )
    entries = [
        item for item in _manual_roster_entries(hass)
        if getattr(item, "id", "").lower() != entry.id.lower()
        and getattr(item, "name", "").lower() != entry.name.lower()
    ]
    entries.append(entry)
    _store_manual_roster_entries(hass, entries)
    await _refresh_and_push_phonebook(hass)
    _LOGGER.info("Phonebook contact added: %s (%s)", entry.id, entry.kind)


async def _handle_phonebook_remove_contact_service(call: ServiceCall) -> None:
    hass: HomeAssistant = call.hass
    name = str(call.data["name"]).strip()
    wanted = name.lower()
    entries = _manual_roster_entries(hass)
    before = len(entries)
    entries = [
        item
        for item in entries
        if getattr(item, "id", "").lower() != wanted
        and getattr(item, "name", "").lower() != wanted
        and getattr(item, "number", "").lower() != wanted
    ]
    _store_manual_roster_entries(hass, entries)
    await _refresh_and_push_phonebook(hass)
    _LOGGER.info("Phonebook contact removed: %s (%d removed)", name, before - len(entries))


async def _handle_phonebook_set_contacts_service(call: ServiceCall) -> None:
    from .roster import parse_roster_json

    hass: HomeAssistant = call.hass
    entries = parse_roster_json(str(call.data.get("roster_json") or "[]"))
    _store_manual_roster_entries(hass, entries)
    await _refresh_and_push_phonebook(hass)
    _LOGGER.info("Phonebook manual contacts replaced: %d entries", len(entries))


async def _handle_phonebook_clear_service(call: ServiceCall) -> None:
    hass: HomeAssistant = call.hass
    _store_manual_roster_entries(hass, [])
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
            "state": "export_phonebook",
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
        state = hass.states.get("sensor.voip_phonebook")
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
    sensor = hass.states.get("sensor.voip_phonebook")
    roster_json = str(sensor.attributes.get("roster_json") or "") if sensor is not None else ""
    contacts = parse_roster_json(roster_json) if roster_json else []
    route = resolve_target(
        target,
        contacts,
        ha_host=local_ip,
        ha_sip_port=int(cfg["sip_port"]),
        force_ha=force_ha_bridge or bool(call.data.get("ha_bridge", False)),
    )
    trunk = hass.data.get(DOMAIN, {}).get("sip_trunk")
    trunk_cfg = _get_trunk_config(hass)
    trunk_ready = _trunk_enabled(trunk_cfg) and bool(getattr(trunk, "registered", False))
    use_trunk = route.action is RouteAction.TRUNK and trunk_ready
    use_softphone_codecs = bool(route.entry is not None and route.entry.kind == "softphone")
    if route.action is RouteAction.TRUNK and not use_trunk:
        raise ServiceValidationError(f"{target} requires a registered SIP trunk")
    route_uri = route.sip_uri
    if use_trunk:
        trunk_target = route.target or target
        route_uri = (
            f"sip:{trunk_target}@{trunk_cfg[CONF_TRUNK_SERVER]}:{int(trunk_cfg[CONF_TRUNK_PORT])};"
            f"transport={str(trunk_cfg[CONF_TRUNK_TRANSPORT]).lower()}"
        )
    if not use_trunk and (route.action not in {RouteAction.DIRECT, RouteAction.BRIDGE} or not route_uri):
        raise ServiceValidationError(f"cannot resolve SIP target: {target}")
    await _async_prepare_ha_outbound_call(hass)
    uri = parse_sip_uri(route_uri)
    remote_tx_formats = (
        _device_formats(dest_device, "tx_formats")
        if dest_device is not None
        else _roster_entry_formats(route.entry, "tx_formats")
    )
    remote_rx_formats = (
        _device_formats(dest_device, "rx_formats")
        if dest_device is not None
        else _roster_entry_formats(route.entry, "rx_formats")
    )
    sip_send_formats, sip_recv_formats = _sip_target_audio_profile(
        remote_tx_formats=remote_tx_formats,
        remote_rx_formats=remote_rx_formats,
        target=target,
    )
    if use_trunk or use_softphone_codecs:
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
        include_common_codecs=use_trunk or use_softphone_codecs,
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
        callee=target,
        peer_name=target,
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
    )
    if result == TerminalReason.TRANSPORT_UNREACHABLE.value and route.entry is not None and route.entry.kind == "softphone":
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
            callee=target,
            peer_name=target,
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
            sip_uri=route_uri,
        )
    elif public_result not in {CallState.REMOTE_RINGING.value, CallState.IN_CALL.value}:
        terminal_reason = _sip_terminal_reason(result, public_result)
        _set_ha_softphone_call_state(
            hass,
            public_result,
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


def _set_pending_route_decision(hass: HomeAssistant, data: dict) -> None:
    """Apply an automation dial-plan decision to a pending inbound SIP route."""
    from homeassistant.exceptions import ServiceValidationError

    call_id = str(data.get("call_id") or "").strip()
    if not call_id:
        raise ServiceValidationError("call_id is required")
    action = str(data.get("action") or "default").strip().lower()
    destination = str(
        data.get("destination") or data.get("target") or data.get("call") or ""
    ).strip()
    if action in {"forward", "bridge"} and not destination:
        raise ServiceValidationError(f"{action} requires destination, target, or call")
    route = _pending_routes(hass).get(call_id)
    if route is None:
        raise ServiceValidationError(f"no pending SIP route for call_id {call_id}")
    future = route.get("future")
    if future is None or future.done():
        raise ServiceValidationError(f"SIP route for call_id {call_id} is no longer decidable")
    future.set_result(
        {
            "action": action,
            "destination": destination,
            "status": int(data.get("status") or 0),
            "reason": str(data.get("reason") or "").strip(),
            "decline_reason": str(data.get("decline_reason") or "").strip(),
        }
    )
    invite = route.get("invite")
    if action in {"decline", "busy", "cancel"} and invite is not None:
        status = int(data.get("status") or 0)
        app_reason = str(data.get("decline_reason") or "").strip()
        if action == "busy":
            status = status or 486
            app_reason = app_reason or TerminalReason.BUSY.value
            state = CallState.BUSY.value
        elif action == "cancel":
            status = status or 487
            app_reason = app_reason or TerminalReason.CANCELLED.value
            state = CallState.CANCELLED.value
        else:
            status = status or 603
            app_reason = app_reason or TerminalReason.DECLINED.value
            state = "declined"
        _set_ha_softphone_call_state(
            hass,
            state,
            session_device_id=HA_SOFTPHONE_DEVICE_ID,
            caller=getattr(invite, "caller", ""),
            callee=getattr(invite, "target", ""),
            peer_name=getattr(invite, "caller", ""),
            direction="incoming",
            call_id=call_id,
            reason=app_reason,
            terminal_reason=app_reason,
            origin="self",
            sip_status_code=status,
            last_sip_event="SIP_RESPONSE",
        )
    elif action in {"answer_ha", "default"} and invite is not None:
        _set_ha_softphone_call_state(
            hass,
            CallState.CONNECTING.value,
            session_device_id=HA_SOFTPHONE_DEVICE_ID,
            caller=getattr(invite, "caller", ""),
            callee=getattr(invite, "target", ""),
            peer_name=getattr(invite, "caller", ""),
            direction="incoming",
            call_id=call_id,
            selected_tx_format=invite.send_format.audio_format.wire_token(),
            selected_rx_format=invite.recv_format.audio_format.wire_token(),
            selected_tx_rtp_format=invite.send_format.wire_token(),
            selected_rx_rtp_format=invite.recv_format.wire_token(),
            audio_mode="full_duplex",
            sip_status_code=180,
            last_sip_event="SIP_RESPONSE",
        )
    elif action in {"forward", "bridge"} and invite is not None:
        _set_sip_bridge_call_state(
            hass,
            CallState.CONNECTING.value,
            caller=getattr(invite, "caller", ""),
            callee=destination or getattr(invite, "target", ""),
            peer_name=getattr(invite, "caller", ""),
            call_id=call_id,
            selected_tx_format=invite.send_format.audio_format.wire_token(),
            selected_rx_format=invite.recv_format.audio_format.wire_token(),
            selected_tx_rtp_format=invite.send_format.wire_token(),
            selected_rx_rtp_format=invite.recv_format.wire_token(),
            audio_mode="full_duplex",
            sip_status_code=180,
            last_sip_event="SIP_RESPONSE",
        )
    _LOGGER.info(
        "SIP route decision call_id=%s action=%s destination=%s",
        call_id,
        action,
        destination or "-",
    )


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


async def _handle_sip_account_create_service(call: ServiceCall) -> None:
    from homeassistant.exceptions import ServiceValidationError
    from .sip_registrar import SipAccount, dump_account, generate_password, normalize_username

    hass = call.hass
    username = normalize_username(str(call.data["username"]))
    display_name = str(call.data.get("display_name") or username).strip()
    replace_existing = bool(call.data.get("replace", False))
    accounts = _sip_account_dicts(hass)
    if any(str(item.get("username") or "").lower() == username.lower() for item in accounts) and not replace_existing:
        raise ServiceValidationError(f"SIP account {username} already exists")
    provided_password = str(call.data.get("password") or "").strip()
    password = provided_password or generate_password()
    account = SipAccount(username=username, display_name=display_name, password=password, enabled=bool(call.data.get("enabled", True)))
    accounts = [item for item in accounts if str(item.get("username") or "").lower() != username.lower()]
    accounts.append(dump_account(account))
    _update_sip_accounts(hass, accounts)
    await _refresh_and_push_phonebook(hass)
    _fire_call_event(
        hass,
        {"state": "sip_account_created", "username": username, "display_name": display_name, "password": password},
        "sip",
    )
    if not provided_password:
        persistent_notification.async_create(
            hass,
            (
                f"SIP account `{username}` created for `{display_name}`.\n\n"
                f"Password: `{password}`\n\n"
                "This generated password is shown only now. Save it in the softphone "
                "configuration or rotate the account password later."
            ),
            title="VoIP Stack SIP Account",
            notification_id=f"{DOMAIN}_sip_account_{username.lower()}",
        )
    _LOGGER.info("SIP local account created username=%s enabled=%s", username, account.enabled)


async def _handle_sip_account_remove_service(call: ServiceCall) -> None:
    from .sip_registrar import normalize_username

    hass = call.hass
    username = normalize_username(str(call.data["username"]))
    accounts = [item for item in _sip_account_dicts(hass) if str(item.get("username") or "").lower() != username.lower()]
    _update_sip_accounts(hass, accounts)
    registrar = hass.data.get(DOMAIN, {}).get("sip_registrar")
    if registrar is not None:
        registrar.registrations.pop(username, None)
    await _refresh_and_push_phonebook(hass)
    _LOGGER.info("SIP local account removed username=%s", username)


async def _handle_sip_account_rotate_password_service(call: ServiceCall) -> None:
    from homeassistant.exceptions import ServiceValidationError
    from .sip_registrar import generate_password, normalize_username

    hass = call.hass
    username = normalize_username(str(call.data["username"]))
    password = generate_password()
    found = False
    accounts = []
    for item in _sip_account_dicts(hass):
        if str(item.get("username") or "").lower() == username.lower():
            item["password"] = password
            found = True
        accounts.append(item)
    if not found:
        raise ServiceValidationError(f"SIP account {username} does not exist")
    _update_sip_accounts(hass, accounts)
    registrar = hass.data.get(DOMAIN, {}).get("sip_registrar")
    if registrar is not None:
        registrar.registrations.pop(username, None)
    await _refresh_and_push_phonebook(hass)
    _fire_call_event(hass, {"state": "sip_account_password_rotated", "username": username, "password": password}, "sip")
    _LOGGER.info("SIP local account password rotated username=%s", username)


async def _handle_enable_accountd_service(call: ServiceCall, *, enabled: bool) -> None:
    from homeassistant.exceptions import ServiceValidationError
    from .sip_registrar import normalize_username

    hass = call.hass
    username = normalize_username(str(call.data["username"]))
    found = False
    accounts = []
    for item in _sip_account_dicts(hass):
        if str(item.get("username") or "").lower() == username.lower():
            item["enabled"] = enabled
            found = True
        accounts.append(item)
    if not found:
        raise ServiceValidationError(f"SIP account {username} does not exist")
    _update_sip_accounts(hass, accounts)
    if not enabled:
        registrar = hass.data.get(DOMAIN, {}).get("sip_registrar")
        if registrar is not None:
            registrar.registrations.pop(username, None)
    await _refresh_and_push_phonebook(hass)
    _LOGGER.info("SIP local account %s username=%s", "enabled" if enabled else "disabled", username)


async def _handle_export_accounts_service(call: ServiceCall) -> None:
    accounts = [
        {
            "username": item.get("username", ""),
            "display_name": item.get("display_name", ""),
            "enabled": bool(item.get("enabled", True)),
        }
        for item in _sip_account_dicts(call.hass)
    ]
    _fire_call_event(call.hass, {"state": "export_accounts", "accounts": accounts}, "sip")


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

    async def handle_phonebook_remove_contact(call: ServiceCall) -> None:
        await _handle_phonebook_remove_contact_service(call)

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

    async def handle_sip_route(call: ServiceCall) -> None:
        await _handle_sip_route_service(call)

    async def handle_sip_account_create(call: ServiceCall) -> None:
        await _handle_sip_account_create_service(call)

    async def handle_sip_account_remove(call: ServiceCall) -> None:
        await _handle_sip_account_remove_service(call)

    async def handle_sip_account_rotate_password(call: ServiceCall) -> None:
        await _handle_sip_account_rotate_password_service(call)

    async def handle_enable_account(call: ServiceCall) -> None:
        await _handle_enable_accountd_service(call, enabled=True)

    async def handle_disable_account(call: ServiceCall) -> None:
        await _handle_enable_accountd_service(call, enabled=False)

    async def handle_export_accounts(call: ServiceCall) -> None:
        await _handle_export_accounts_service(call)

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
        {
            **target_fields,
            vol.Optional("source"): cv.string,
            vol.Optional("source_device_id"): cv.string,
            vol.Optional("source_name"): cv.string,
            vol.Optional("call_id", default=""): cv.string,
        },
        extra=vol.PREVENT_EXTRA,
    )
    sip_decline_schema = vol.Schema(
        {
            **target_fields,
            vol.Optional("source"): cv.string,
            vol.Optional("source_device_id"): cv.string,
            vol.Optional("source_name"): cv.string,
            vol.Optional("call_id", default=""): cv.string,
            vol.Optional("status", default=603): vol.Coerce(int),
            vol.Optional("reason", default="Decline"): cv.string,
            vol.Optional("decline_reason", default=""): cv.string,
        },
        extra=vol.PREVENT_EXTRA,
    )
    sip_hangup_schema = vol.Schema(
        {
            **target_fields,
            vol.Optional("source"): cv.string,
            vol.Optional("source_device_id"): cv.string,
            vol.Optional("source_name"): cv.string,
            vol.Optional("call_id", default=""): cv.string,
            vol.Optional("reason", default="local_hangup"): cv.string,
        },
        extra=vol.PREVENT_EXTRA,
    )
    sip_call_schema = vol.Schema(
        {
            **target_fields,
            vol.Optional("source"): cv.string,
            vol.Optional("source_device_id"): cv.string,
            vol.Optional("source_name"): cv.string,
            vol.Optional("call_id", default=""): cv.string,
            vol.Optional("destination"): cv.string,
            vol.Optional("target"): cv.string,
            vol.Optional("call"): cv.string,
            vol.Optional("ha_bridge", default=False): cv.boolean,
        },
        extra=vol.PREVENT_EXTRA,
    )
    sip_route_schema = vol.Schema(
        {
            vol.Required("call_id"): cv.string,
            vol.Optional("action", default="default"): vol.In(
                ["answer_ha", "decline", "busy", "forward", "bridge", "default", "cancel"]
            ),
            vol.Optional("destination"): cv.string,
            vol.Optional("target"): cv.string,
            vol.Optional("call"): cv.string,
            vol.Optional("status", default=0): vol.Coerce(int),
            vol.Optional("reason", default=""): cv.string,
            vol.Optional("decline_reason", default=""): cv.string,
        },
        extra=vol.PREVENT_EXTRA,
    )
    phonebook_add_schema = vol.Schema(
        {
            vol.Required("name"): cv.string,
            vol.Optional("id", default=""): cv.string,
            vol.Optional("kind", default="esp"): vol.In(["ha", "esp", "phone", "softphone", "group"]),
            vol.Optional("address", default=""): cv.string,
            vol.Optional("sip_uri", default=""): cv.string,
            vol.Optional("number", default=""): cv.string,
            vol.Optional("ha_bridge", default=False): cv.boolean,
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
    phonebook_remove_schema = vol.Schema(
        {vol.Required("name"): cv.string},
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
    sip_account_create_schema = vol.Schema(
        {
            vol.Required("username"): cv.string,
            vol.Optional("display_name", default=""): cv.string,
            vol.Optional("password", default=""): cv.string,
            vol.Optional("enabled", default=True): cv.boolean,
            vol.Optional("replace", default=False): cv.boolean,
        },
        extra=vol.PREVENT_EXTRA,
    )
    sip_account_name_schema = vol.Schema({vol.Required("username"): cv.string}, extra=vol.PREVENT_EXTRA)
    hass.services.async_register(DOMAIN, "purge_devices", handle_purge_devices, schema=purge_schema)
    hass.services.async_register(DOMAIN, "answer", handle_sip_answer, schema=sip_answer_schema)
    hass.services.async_register(DOMAIN, "decline", handle_sip_decline, schema=sip_decline_schema)
    hass.services.async_register(DOMAIN, "hangup", handle_sip_hangup, schema=sip_hangup_schema)
    hass.services.async_register(DOMAIN, "call", handle_sip_call, schema=sip_call_schema)
    hass.services.async_register(DOMAIN, "forward", handle_sip_forward, schema=sip_call_schema)
    hass.services.async_register(DOMAIN, "route", handle_sip_route, schema=sip_route_schema)
    hass.services.async_register(
        DOMAIN, "add_contact", handle_phonebook_add_contact, schema=phonebook_add_schema
    )
    hass.services.async_register(
        DOMAIN, "remove_contact", handle_phonebook_remove_contact, schema=phonebook_remove_schema
    )
    hass.services.async_register(
        DOMAIN, "set_contacts", handle_phonebook_set_contacts, schema=phonebook_set_schema
    )
    hass.services.async_register(DOMAIN, "clear_contacts", handle_phonebook_clear)
    hass.services.async_register(DOMAIN, "export_phonebook", handle_phonebook_export)
    hass.services.async_register(DOMAIN, "push_phonebook", handle_phonebook_push)
    hass.services.async_register(DOMAIN, "set_dnd", handle_sip_set_dnd, schema=set_dnd_schema)
    hass.services.async_register(DOMAIN, "create_account", handle_sip_account_create, schema=sip_account_create_schema)
    hass.services.async_register(DOMAIN, "remove_account", handle_sip_account_remove, schema=sip_account_name_schema)
    hass.services.async_register(
        DOMAIN, "rotate_account_password", handle_sip_account_rotate_password, schema=sip_account_name_schema
    )
    hass.services.async_register(DOMAIN, "enable_account", handle_enable_account, schema=sip_account_name_schema)
    hass.services.async_register(DOMAIN, "disable_account", handle_disable_account, schema=sip_account_name_schema)
    hass.services.async_register(DOMAIN, "export_accounts", handle_export_accounts)


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
    cfg = _get_trunk_config(hass)
    if not _trunk_enabled(cfg):
        hass.data.setdefault(DOMAIN, {}).pop("sip_trunk", None)
        return True
    from .sip_trunk import SipTrunkClient, SipTrunkConfig

    local_ip = await _ha_advertise_host(hass)
    if not local_ip:
        _LOGGER.warning("SIP trunk disabled: HA advertise IP is unknown")
        return False
    trunk = SipTrunkClient(
        config=SipTrunkConfig(
            enabled=True,
            transport=str(cfg[CONF_TRUNK_TRANSPORT]),
            server=str(cfg[CONF_TRUNK_SERVER]),
            port=int(cfg[CONF_TRUNK_PORT]),
            domain=str(cfg[CONF_TRUNK_DOMAIN]),
            username=str(cfg[CONF_TRUNK_USERNAME]),
            auth_username=str(cfg[CONF_TRUNK_AUTH_USERNAME]),
            password=str(cfg[CONF_TRUNK_PASSWORD]),
            expires=int(cfg[CONF_TRUNK_EXPIRES]),
            outbound_proxy=str(cfg[CONF_TRUNK_OUTBOUND_PROXY]),
        ),
        local_ip=local_ip,
        local_sip_port=int(_get_transport_config(hass)["sip_port"]),
    )
    endpoint = hass.data.get(DOMAIN, {}).get("sip_endpoint")
    if endpoint is not None:
        trunk.attach_endpoint_manager(endpoint)
    hass.data.setdefault(DOMAIN, {})["sip_trunk"] = trunk
    try:
        await trunk.start()
    except Exception as err:
        _LOGGER.warning("SIP trunk registration failed: %s", err)
    return True


async def _async_stop_sip_trunk(hass: HomeAssistant) -> None:
    trunk = hass.data.get(DOMAIN, {}).pop("sip_trunk", None)
    if trunk is None:
        return
    try:
        await trunk.stop()
    except Exception:
        _LOGGER.debug("Ignoring SIP trunk stop error", exc_info=True)


async def _async_start_sip_endpoint(hass: HomeAssistant) -> bool:
    """Bind the enabled SIP signaling listeners for HA softphone and bridge calls."""
    from .roster import RosterEntry, resolve_target
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
        await _async_stop_sip_endpoint(hass)

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

    def _roster_from_peers(peers: list[Peer]) -> list[RosterEntry]:
        from .roster import merge_roster_overrides

        entries: list[RosterEntry] = []
        for peer in peers:
            entries.append(
                RosterEntry(
                    id=peer.name,
                    name=peer.name,
                    kind="ha" if peer.is_ha else "esp",
                    address=peer.host,
                    metadata={
                        "sip_transport": (
                            str((peer.device or {}).get("sip_transport") or "tcp").lower()
                            if peer.is_ha or peer.device is not None
                            else ""
                        ),
                        "sip_port": peer.sip_port,
                        "rtp_port": peer.rtp_port,
                        "audio_mode": peer.audio_mode,
                    },
                )
            )
        entries = merge_roster_overrides(entries, _manual_roster_entries(hass))
        entries.extend(_registered_roster_entries(hass))
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

    def _peer_audio_formats(peer: Peer | None, key: str) -> list[AudioFormat]:
        if peer is None:
            return []
        raw = ";".join(str(item) for item in (peer.tx_formats if key == "tx_formats" else peer.rx_formats) or [])
        if not raw.strip():
            return []
        try:
            return parse_audio_format_list(raw)
        except ValueError as err:
            _LOGGER.warning("Ignoring invalid peer %s on %s: %s", key, peer.name, err)
            return []

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
            _roster_from_peers(peers),
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

        decision = resolve_target(destination, _roster_from_peers(peers), ha_bridge=False)
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
        decision = resolve_target(invite.target, _roster_from_peers(peers), ha_bridge=True)
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
            decision = resolve_target(route_destination, _roster_from_peers(peers), ha_bridge=False)
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
            elif decision.reason in {RouteReason.TRUNK_UNAVAILABLE, RouteReason.NO_DIRECT_TRANSPORT}:
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
        if not force_ha_softphone and (
            bridge_to_trunk or (decision.action in {RouteAction.DIRECT, RouteAction.BRIDGE, RouteAction.GROUP} and decision.entry is not None)
        ):
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
                    bridge_handled, source_call_id, dest_call_id, _client_closed, source_bye = await _terminate_sip_bridge(
                        hass,
                        client.dialog_ids.call_id,
                    )
                    if bridge_handled:
                        terminal_reason = (
                            TerminalReason.REMOTE_HANGUP.value
                            if terminal == "remote_hangup"
                            else _sip_terminal_reason(terminal, _sip_public_state(terminal))
                        )
                        _set_sip_bridge_call_state(
                            hass,
                            CallState.IDLE.value,
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

                hass.async_create_task(_finish_bridge(result))
                return SipInviteResult(180, "Ringing", to_tag="", defer_final=True)
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


async def _async_stop_sip_endpoint(hass: HomeAssistant) -> None:
    registry = _call_registry(hass)
    relays = dict(registry.relays)
    for relay in list(relays.values()):
        try:
            await relay.stop()
        except Exception:
            _LOGGER.debug("Ignoring SIP RTP relay stop error", exc_info=True)
    clients = dict(registry.sip_clients)
    for client in list(clients.values()):
        try:
            client.bye()
            await client.close()
        except Exception:
            _LOGGER.debug("Ignoring SIP client stop error", exc_info=True)
    registry.clear_runtime()
    endpoint = hass.data.get(DOMAIN, {}).pop("sip_endpoint", None)
    hass.data.get(DOMAIN, {}).pop("sip_server", None)
    hass.data.get(DOMAIN, {}).pop("sip_tcp_server", None)
    if endpoint is not None:
        await endpoint.stop()
