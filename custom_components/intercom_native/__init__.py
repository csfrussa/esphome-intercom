"""Intercom Native integration for Home Assistant.

PBX-lite over TCP and/or UDP between browser, HA, and ESPHome devices.
HA participates as a regular peer (location_name) and bridges across
transports when needed; routing policy lives in the phonebook (target-shaped)
and in routing_mode on each ESP (device_independent vs ha_pbx).
"""

import logging

import voluptuous as vol

from homeassistant.components.zeroconf import async_get_async_instance
from homeassistant.core import HomeAssistant, CoreState, Event, ServiceCall
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform, EVENT_HOMEASSISTANT_STARTED
from homeassistant.exceptions import ConfigEntryError

PLATFORMS: list[Platform] = [Platform.SENSOR]
from homeassistant.helpers import config_validation as cv
from homeassistant.components import network
from homeassistant.util import slugify
from zeroconf.asyncio import AsyncServiceInfo

from .const import (
    CONF_ASSIST_INTENTS,
    DOMAIN,
    HA_PEER_FALLBACK_NAME,
    HA_SOFTPHONE_DEVICE_ID,
    INTEGRATION_VERSION,
    INTERCOM_PORT,
    INTERCOM_RTP_PORT,
    INTERCOM_SIP_PORT,
    INTERCOM_UDP_AUDIO_PORT,
    INTERCOM_UDP_CONTROL_PORT,
)
from .device_resolver import get_resolver
from .audio_format import (
    AudioFormat,
    HA_BROWSER_RX_FORMATS,
    HA_BROWSER_TX_FORMATS,
    LEGACY_AUDIO_FORMAT,
    UDP_SAFE_PAYLOAD_BYTES,
    parse_audio_format_list,
    require_udp_safe_formats,
)
from .peer import Peer
from .websocket_api import (
    async_register_websocket_api,
    _async_load_ha_softphone_store,
    _get_intercom_devices,
    _stop_device_sessions,
    _find_bridge_by_source,
    _fire_call_event,
    _ha_softphone_dnd,
    _set_ha_softphone_call_state,
    _session_get,
    _session_pop,
    _session_register,
    _sessions,
    _bridges,
    IntercomSession,
    BridgeSession,
)

CONFIG_SCHEMA = cv.config_entry_only_config_schema(DOMAIN)


from dataclasses import dataclass


@dataclass
class InboundStart:
    """Inbound-call contract from any transport listener to the HA router.

    `transport` is set when the source leg is already adopted (TCP).
    """
    host: str
    caller_name: str
    caller_route: str
    dest_name: str
    dest_route: str
    call_id: str
    port: int = 0
    transport: object | None = None  # set by TCP listener (adopted leg)
    caller_tx_formats: list[AudioFormat] | None = None
    caller_rx_formats: list[AudioFormat] | None = None

_LOGGER = logging.getLogger(__name__)
_INTERCOM_UDP_SERVICE_TYPE = "_intercom-udp._udp.local."
_INTERCOM_TCP_SERVICE_TYPE = "_intercom-tcp._tcp.local."


def _device_formats(device: dict | None, key: str):
    if not device:
        return [LEGACY_AUDIO_FORMAT]
    value = device.get(key)
    if isinstance(value, str):
        raw = value
    else:
        raw = ";".join(value or [])
    try:
        formats = parse_audio_format_list(raw)
        if device.get("transport") == "udp":
            require_udp_safe_formats(
                formats,
                context=f"{device.get('name') or device.get('device_id')} UDP {key}",
                max_payload=int(device.get("udp_max_payload") or UDP_SAFE_PAYLOAD_BYTES),
            )
        return formats
    except ValueError as err:
        _LOGGER.warning(
            "Ignoring invalid %s on %s: %s",
            key,
            (device or {}).get("name") or (device or {}).get("device_id"),
            err,
        )
        return [LEGACY_AUDIO_FORMAT]


def _ha_peer_name(hass: HomeAssistant) -> str:
    """Return the HA phonebook peer name.

    HA normally always has a configured location_name. The fallback is only for
    malformed/empty local config and avoids a hardcoded "Home Assistant" peer
    identity.
    """
    return (hass.config.location_name or "").strip() or HA_PEER_FALLBACK_NAME


def _entry_transport_config(entry: ConfigEntry | None = None) -> dict:
    """Normalised transport config; defaults preserve pre-port-toggle entries."""
    data = entry.data if entry is not None else {}
    return {
        "use_tcp": data.get("use_tcp", True),
        "use_sip": data.get("use_sip", True),
        "sip_port": int(data.get("sip_port", INTERCOM_SIP_PORT)),
        "rtp_port": int(data.get("rtp_port", INTERCOM_RTP_PORT)),
        "use_udp": data.get("use_udp", False),
        "tcp_port": int(data.get("tcp_port", INTERCOM_PORT)),
        "udp_audio_port": int(data.get("udp_audio_port", INTERCOM_UDP_AUDIO_PORT)),
        "udp_control_port": int(data.get("udp_control_port", INTERCOM_UDP_CONTROL_PORT)),
        "udp_max_payload": int(data.get("udp_max_payload", UDP_SAFE_PAYLOAD_BYTES)),
        "advertise_host": (data.get("advertise_host") or "").strip(),
    }


def _get_transport_config(hass: HomeAssistant) -> dict:
    """Return current HA-side network config (transport flags + ports)."""
    return hass.data.get(DOMAIN, {}).get(
        "transport_config",
        {
            "use_tcp": True,
            "use_sip": True,
            "sip_port": INTERCOM_SIP_PORT,
            "rtp_port": INTERCOM_RTP_PORT,
            "use_udp": False,
            "tcp_port": INTERCOM_PORT,
            "udp_audio_port": INTERCOM_UDP_AUDIO_PORT,
            "udp_control_port": INTERCOM_UDP_CONTROL_PORT,
            "udp_max_payload": UDP_SAFE_PAYLOAD_BYTES,
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
    """Per-host transport choice; omit `host` for the global default."""
    from .transport_helpers import configured_transport_type
    return configured_transport_type(hass, host)


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
        "outgoing",
        "calling",
        "ringing",
        "streaming",
    }


def _device_entity_state(hass: HomeAssistant, device: dict, key: str) -> str:
    entity_id = (device.get("entities") or {}).get(key)
    if not entity_id:
        return ""
    state = hass.states.get(entity_id)
    value = (state.state if state is not None else "").strip()
    return "" if value.lower() in ("unknown", "unavailable") else value


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


def _device_has_direct_esp_incoming(hass: HomeAssistant, device: dict) -> bool:
    state = _device_entity_state(hass, device, "intercom_state").lower()
    if state not in ("ringing", "incoming"):
        return False
    caller = _device_entity_state(hass, device, "incoming_caller")
    return bool(caller and not _is_ha_inbound_destination(hass, caller))


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


async def _decline_inbound_start(
    hass: HomeAssistant,
    inbound: "InboundStart",
    reason: str,
) -> None:
    """Send a terminal DECLINE for an unsolicited START.

    This is the router-level safety net: every START that HA refuses must get
    a protocol response, otherwise the caller remains stuck in OUTGOING.
    """
    from .transport_helpers import TransportCallbacks, build_transport

    callbacks = TransportCallbacks()
    transport = inbound.transport
    created_transport = transport is None

    if transport is None:
        transport = build_transport(
            hass,
            inbound.host,
            "udp",
            callbacks,
        )
    else:
        transport.set_callbacks(callbacks)

    transport.set_call_context(inbound.call_id, inbound.caller_name)

    try:
        if not transport.is_connected and not await transport.connect():
            _LOGGER.warning(
                "Cannot send DECLINE(%s) to %s: transport connect failed",
                reason,
                inbound.host,
            )
            return

        sent = await transport.send_decline(reason)
        if not sent:
            _LOGGER.warning(
                "DECLINE(%s) to %s was not acknowledged by transport",
                reason,
                inbound.host,
            )

        # UDP terminal control frames are retry-backed. Keep the short-lived
        # notification transport alive long enough for its retry window.
        if getattr(transport, "transport_name", "") == "udp":
            import asyncio
            await asyncio.sleep(0.45)
    finally:
        try:
            await transport.disconnect()
        except Exception:
            _LOGGER.debug("Ignoring disconnect error after DECLINE(%s)", reason, exc_info=True)
        if created_transport:
            _LOGGER.debug(
                "Inbound START from %s declined with reason=%s",
                inbound.host,
                reason,
            )


def _device_transport(hass: HomeAssistant, d: dict, udp_manager=None) -> str:
    """Read the endpoint-declared device transport."""
    return d.get("transport") if d.get("transport") in ("udp", "tcp", "sip") else "tcp"


def _udp_peer_ports(hass: HomeAssistant, host: str, cfg: dict | None = None) -> tuple[int, int]:
    """Return endpoint-declared UDP (audio, control) ports for a peer."""
    for device in get_resolver(hass)._devices or []:
        if device.get("host") == host and device.get("transport") == "udp":
            audio = device.get("udp_audio_port")
            control = device.get("udp_control_port")
            if audio and control:
                return int(audio), int(control)
    if cfg is None:
        cfg = _get_transport_config(hass)
    return cfg["udp_audio_port"], cfg["udp_control_port"]


def _tcp_peer_port(hass: HomeAssistant, host: str, cfg: dict | None = None) -> int:
    """Return endpoint-declared TCP port for a peer."""
    for device in get_resolver(hass)._devices or []:
        if device.get("host") == host and device.get("transport") == "tcp":
            port = device.get("tcp_port")
            if port:
                return int(port)
    if cfg is None:
        cfg = _get_transport_config(hass)
    return cfg["tcp_port"]


async def _async_build_peer_snapshot(hass: HomeAssistant) -> list[Peer]:
    """Snapshot of every online peer (ESPs + HA itself).

    HA is appended last as kind="ha". Consumers format this into either the
    HA phonebook sensor or the HA endpoint mDNS record.
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
        transport = _device_transport(hass, d)
        udp_audio_port, udp_control_port = _udp_peer_ports(hass, host, cfg)
        out.append(Peer(
            kind="esp",
            device=d,
            name=name,
            host=host,
            transport=transport,
            tcp_port=_tcp_peer_port(hass, host, cfg),
            udp_audio_port=udp_audio_port,
            udp_control_port=udp_control_port,
            sip_port=int(d.get("sip_port") or cfg["sip_port"]),
            rtp_port=int(d.get("rtp_port") or cfg["rtp_port"]),
            audio_mode=d.get("audio_mode", "full_duplex"),
            tx_formats=list(d.get("tx_formats") or []),
            rx_formats=list(d.get("rx_formats") or []),
        ))
    ha_host = await _ha_advertise_host(hass)
    if ha_host:
        out.append(Peer(
            kind="ha",
            device=None,
            name=_ha_peer_name(hass),
            host=ha_host,
            transport="tcp",
            tcp_port=cfg["tcp_port"],
            udp_audio_port=cfg["udp_audio_port"],
            udp_control_port=cfg["udp_control_port"],
            sip_port=cfg["sip_port"],
            rtp_port=cfg["rtp_port"],
            audio_mode="full_duplex",
        ))
    else:
        # No announce IP -> HA can't be in the phonebook, ha_pbx will fail.
        _LOGGER.warning(
            "Cannot determine HA announce IP (network.async_get_announce_addresses "
            "returned empty); HA will not appear in the ESP phonebook and ha_pbx "
            "routing will be unavailable until this is fixed."
        )
    return out


def _format_entry_unified(peer: Peer) -> str:
    """Protocol-aware phonebook entry.

    This is the authoritative roster shape. It preserves each peer's real
    protocol instead of hiding cross-transport routes behind HA-shaped rows.
    """
    name = peer.name
    peer_ip = peer.host or ""
    if not peer_ip:
        return name
    if peer.is_ha:
        return (
            f"{name}|ha|{peer_ip}|{peer.tcp_port}|"
            f"{peer.udp_audio_port}|{peer.udp_control_port}|"
            f"{peer.sip_port or 5060}|{peer.rtp_port or 40000}"
        )
    tx = ";".join(peer.tx_formats or [])
    rx = ";".join(peer.rx_formats or [])
    if peer.transport == "udp":
        return (
            f"{name}|udp|{peer_ip}|{peer.udp_audio_port}|"
            f"{peer.udp_control_port}|{peer.audio_mode}|{tx}|{rx}"
        )
    if peer.transport == "sip":
        return (
            f"{name}|sip|{peer_ip}|{peer.sip_port or 5060}|"
            f"{peer.rtp_port or 40000}|{peer.audio_mode}|{tx}|{rx}"
        )
    return f"{name}|tcp|{peer_ip}|{peer.tcp_port}|{peer.audio_mode}|{tx}|{rx}"


async def _async_build_service_info(
    hass: HomeAssistant, kind: str = "udp"
) -> AsyncServiceInfo:
    """Compose the mDNS AsyncServiceInfo for the given transport kind.

    kind = 'udp' -> _intercom-udp._udp on udp_audio_port
    kind = 'tcp' -> _intercom-tcp._tcp on tcp_port
    Both publish the same canonical HA endpoint TXT.
    """
    cfg = _get_transport_config(hass)
    location_name = _ha_peer_name(hass)
    hostname = f"{slugify(location_name) or 'intercom-native'}.local."
    addresses = await network.async_get_announce_addresses(hass)
    advertise_host = await _ha_advertise_host(hass)
    properties = {
        "audio_port": str(cfg["udp_audio_port"]),
        "control_port": str(cfg["udp_control_port"]),
        "tcp_port": str(cfg["tcp_port"]),
        "sip_port": str(cfg["sip_port"]),
        "rtp_port": str(cfg["rtp_port"]),
        "friendly_name": location_name,
        "role": "ha",
        "version": INTEGRATION_VERSION,
    }
    if kind == "tcp":
        service_type = _INTERCOM_TCP_SERVICE_TYPE
        port = cfg["tcp_port"]
    else:
        service_type = _INTERCOM_UDP_SERVICE_TYPE
        port = cfg["udp_audio_port"]
    if advertise_host:
        properties["endpoint"] = (
            f"{location_name}|ha|{advertise_host}|{cfg['tcp_port']}|"
            f"{cfg['udp_audio_port']}|{cfg['udp_control_port']}|"
            f"{cfg['sip_port']}|{cfg['rtp_port']}"
        )
    return AsyncServiceInfo(
        service_type,
        name=f"{location_name}.{service_type}",
        server=hostname,
        parsed_addresses=addresses,
        port=port,
        properties=properties,
    )


async def _async_register_udp_mdns_service(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> None:
    """Advertise HA as a UDP intercom endpoint."""
    service_info = await _async_build_service_info(hass)
    aiozc = await async_get_async_instance(hass)
    await aiozc.async_register_service(service_info)
    entry_data = hass.data.setdefault(DOMAIN, {}).setdefault(entry.entry_id, {})
    entry_data["udp_mdns_service_info"] = service_info
    _LOGGER.info(
        "Registered UDP mDNS service for %s (endpoint=%s)",
        service_info.name.split(".")[0],
        service_info.properties.get("endpoint", "(none)"),
    )


async def _async_unregister_udp_mdns_service(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> None:
    """Remove the Home Assistant intercom UDP mDNS advertisement."""
    entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    service_info = entry_data.pop("udp_mdns_service_info", None)
    if service_info is None:
        return
    aiozc = await async_get_async_instance(hass)
    await aiozc.async_unregister_service(service_info)
    _LOGGER.info("Unregistered UDP mDNS service for entry %s", entry.entry_id)


async def _async_register_tcp_mdns_service(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> None:
    """Advertise HA on _intercom-tcp._tcp (symmetric to the UDP record)."""
    service_info = await _async_build_service_info(hass, kind="tcp")
    aiozc = await async_get_async_instance(hass)
    await aiozc.async_register_service(service_info)
    entry_data = hass.data.setdefault(DOMAIN, {}).setdefault(entry.entry_id, {})
    entry_data["tcp_mdns_service_info"] = service_info
    _LOGGER.info(
        "Registered TCP mDNS service for %s on port %s",
        service_info.name.split(".")[0], service_info.port,
    )


async def _async_unregister_tcp_mdns_service(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> None:
    """Remove the _intercom-tcp._tcp mDNS advertisement."""
    entry_data = hass.data.get(DOMAIN, {}).get(entry.entry_id, {})
    service_info = entry_data.pop("tcp_mdns_service_info", None)
    if service_info is None:
        return
    aiozc = await async_get_async_instance(hass)
    await aiozc.async_unregister_service(service_info)
    _LOGGER.info("Unregistered TCP mDNS service for entry %s", entry.entry_id)


async def _resolve_target_device(hass: HomeAssistant, call: ServiceCall) -> dict | None:
    """Thin wrapper over IntercomDeviceResolver.resolve_target."""
    return await get_resolver(hass).resolve_target(call)


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


async def _handle_answer_service(call: ServiceCall, device: dict) -> None:
    hass: HomeAssistant = call.hass
    device_id = device["device_id"]
    host = device["host"]

    session = _session_get(device_id)
    if session:
        await session.answer()
        return

    for bridge in _bridges.values():
        if bridge.dest_device_id == device_id:
            if await bridge.answer_dest():
                return

    if _device_has_direct_esp_incoming(hass, device):
        if await _press_device_button(hass, device, "call", "call"):
            return

    # No session: ESP may have called HA directly; open one and answer.
    session = IntercomSession(
        hass=hass,
        device_id=device_id,
        host=host,
        transport_type=_select_transport_type(hass, host),
        local_name=_ha_peer_name(hass),
        peer_name=device.get("name") or "",
        direction="incoming",
        audio_mode=device.get("audio_mode", "full_duplex"),
        local_tx_formats=list(HA_BROWSER_TX_FORMATS),
        local_rx_formats=list(HA_BROWSER_RX_FORMATS),
        peer_tx_formats=_device_formats(device, "tx_formats"),
        peer_rx_formats=_device_formats(device, "rx_formats"),
    )
    result = await session.answer_esp_call()
    if result == "streaming":
        _session_register(device_id, session)
        _LOGGER.info("Answered ESP call via service: %s", device["name"])
    else:
        _LOGGER.error("Failed to answer call on %s", device["name"])


async def _handle_decline_service(call: ServiceCall, device: dict) -> None:
    """Decline a call. With `reason` the text reaches both ends; without, falls back to hangup."""
    hass: HomeAssistant = call.hass
    device_id = device["device_id"]
    reason = (call.data.get("reason") or "").strip()

    if not reason:
        stopped = await _stop_device_sessions(device_id, hass=hass)
        _LOGGER.info("Decline via service: %s (stopped=%s)", device["name"], stopped)
        return

    session = _session_get(device_id)
    if session is not None:
        ok = await session.decline(reason)
        if ok:
            _session_pop(device_id)
            _LOGGER.info(
                "Decline via service (P2P, reason=%r): %s",
                reason, device["name"],
            )
            return

    # Bridge case: emit DECLINE on BOTH legs so each ESP fires
    # on_call_failed and surfaces the reason on its ended screen.
    bridge = next(
        (b for b in _bridges.values()
         if b.source_device_id == device_id or b.dest_device_id == device_id),
        None,
    )
    if bridge is not None:
        leg = "source" if bridge.source_device_id == device_id else "dest"
        for client in (bridge._source_client, bridge._dest_client):
            if client is None:
                continue
            try:
                await client.send_decline(reason)
            except Exception:
                _LOGGER.exception("Bridge decline send failed for %s", device["name"])
        await bridge.stop(send_signaling=False)
        _bridges.pop(bridge.bridge_id, None)
        bridge._fire_state_event("declined", reason=reason, origin=leg)
        _LOGGER.info(
            "Decline via service (bridge %s leg, reason=%r): %s",
            leg, reason, device["name"],
        )
        return

    slug = _resolve_esphome_route_id(hass, device["host"])
    if slug:
        service_slug = _resolve_esphome_service_slug(hass, slug, "decline_call")
        service_name = f"{service_slug}_decline_call"
        if service_name in _available_esphome_services(hass):
            try:
                await hass.services.async_call(
                    "esphome",
                    service_name,
                    {"reason": reason},
                    blocking=True,
                )
                _LOGGER.info(
                    "Decline via ESPHome action: esphome.%s(reason=%r) on %s",
                    service_name, reason, device["name"],
                )
                return
            except Exception as err:
                _LOGGER.error(
                    "Failed to invoke esphome.%s on %s: %s",
                    service_name, device["host"], err,
                )

    # No live call: fall back to a clean stop.
    stopped = await _stop_device_sessions(device_id, hass=hass)
    _LOGGER.info(
        "Decline via service (no live call): %s (stopped=%s)",
        device["name"], stopped,
    )


async def _handle_hangup_service(call: ServiceCall, device: dict) -> None:
    hass: HomeAssistant = call.hass
    stopped = await _stop_device_sessions(device["device_id"], hass=hass)
    _LOGGER.info("Hangup via service: %s (stopped=%s)", device["name"], stopped)


async def _handle_call_service(call: ServiceCall, dest_device: dict) -> None:
    """Start a call. With `source`, builds an ESP-to-ESP bridge; otherwise a P2P session."""
    hass: HomeAssistant = call.hass
    source_device_id = call.data.get("source")

    if source_device_id:
        # The source ESP originates its own call (so it actually transitions to
        # OUTGOING). It must use its phonebook entry as-is: same-transport
        # entries dial the peer directly, while cross-transport entries are
        # already target-shaped to HA.
        intercom_devices = await _get_intercom_devices(hass)
        source_device = next(
            (d for d in intercom_devices if d["device_id"] == source_device_id),
            None,
        )
        if not source_device:
            _LOGGER.error("Source device not found: %s", source_device_id)
            return

        slug = _resolve_esphome_route_id(hass, source_device["host"])
        if not slug:
            _LOGGER.error(
                "Cannot start call: no ESPHome integration entry matches source host %s",
                source_device["host"],
            )
            return
        service_slug = _resolve_esphome_service_slug(hass, slug, "start_call")
        try:
            # blocking so the ESP enters OUTGOING and dials HA before we return
            # when the selected phonebook entry points to HA. For same-transport
            # calls HA deliberately stays out of the media/signaling path.
            await hass.services.async_call(
                "esphome",
                f"{service_slug}_start_call",
                {"dest": dest_device["name"]},
                blocking=True,
            )
            _LOGGER.info(
                "Asked source ESP to start call: esphome.%s_start_call(dest=%s) [%s -> %s]",
                service_slug, dest_device["name"],
                source_device["name"], dest_device["name"],
            )
        except Exception as err:
            _LOGGER.error(
                "Failed to invoke esphome.%s_start_call on %s: %s",
                service_slug, source_device["host"], err,
            )
        return

    # P2P mode: HA to ESP.
    device_id = dest_device["device_id"]
    dest_host = dest_device["host"]
    await _stop_device_sessions(device_id, hass=hass)

    if dest_device.get("transport") == "sip":
        from .sip_client import SipCallClient

        cfg = _get_transport_config(hass)
        local_ip = await _ha_advertise_host(hass)
        if not local_ip:
            _LOGGER.error("Cannot start SIP call: HA advertise IP is unknown")
            return
        client = SipCallClient(
            local_ip=local_ip,
            local_name=_ha_peer_name(hass),
            local_sip_port=int(cfg["sip_port"]),
            local_rtp_port=int(cfg["rtp_port"]),
            supported_formats=list(_device_formats(dest_device, "rx_formats")),
        )
        result = await client.invite(
            target=dest_device.get("name") or "intercom",
            remote_host=dest_host,
            remote_sip_port=int(dest_device.get("sip_port") or cfg["sip_port"]),
        )
        bucket = hass.data.setdefault(DOMAIN, {})
        active = bucket.setdefault("sip_clients", {})
        if result in {"ringing", "streaming"}:
            active[client.dialog_ids.call_id] = client
        else:
            await client.close()
        _fire_call_event(
            hass,
            {
                "state": result,
                "scope": "sip",
                "call_id": client.dialog_ids.call_id,
                "target": dest_device.get("name") or "",
            },
            "sip",
        )
        _LOGGER.info("SIP P2P call via service: -> %s (%s)", dest_device["name"], result)
        return

    session = IntercomSession(
        hass=hass,
        device_id=device_id,
        host=dest_host,
        transport_type=_select_transport_type(hass, dest_host),
        local_name=_ha_peer_name(hass),
        peer_name=dest_device.get("name") or "",
        direction="outgoing",
        audio_mode=dest_device.get("audio_mode", "full_duplex"),
        local_tx_formats=list(HA_BROWSER_TX_FORMATS),
        local_rx_formats=list(HA_BROWSER_RX_FORMATS),
        peer_tx_formats=_device_formats(dest_device, "tx_formats"),
        peer_rx_formats=_device_formats(dest_device, "rx_formats"),
    )
    result = await session.start()

    if result in ("streaming", "ringing"):
        _session_register(device_id, session)
        _LOGGER.info(
            "P2P call via service: -> %s (%s)", dest_device["name"], result
        )
    else:
        _LOGGER.error("P2P call failed: -> %s", dest_device["name"])


async def _handle_forward_service(call: ServiceCall, source_device: dict) -> None:
    """Forward an active or ringing call. Target = source, `forward_to` = new dest."""
    hass: HomeAssistant = call.hass
    forward_to_id = call.data.get("forward_to")
    if not forward_to_id:
        _LOGGER.warning("forward_to field is required")
        return

    intercom_devices = await _get_intercom_devices(hass)
    dest_device = next(
        (d for d in intercom_devices if d["device_id"] == forward_to_id),
        None,
    )
    if not dest_device:
        _LOGGER.warning("Forward destination device not found: %s", forward_to_id)
        return

    if dest_device["device_id"] == source_device["device_id"]:
        _LOGGER.warning("Cannot forward to self")
        return
    if _device_has_ha_call(dest_device["device_id"]) or _state_entity_is_busy(hass, dest_device):
        _LOGGER.info(
            "Forward rejected: destination %s is busy",
            dest_device["name"],
        )
        return

    bridge = _find_bridge_by_source(source_device["device_id"])
    if bridge:
        result = await bridge.forward_to(
            dest_device["device_id"],
            dest_device["host"],
            dest_device["name"],
            _select_transport_type(hass, dest_device["host"]),
            dest_device.get("audio_mode", "full_duplex"),
            _device_formats(dest_device, "tx_formats"),
            _device_formats(dest_device, "rx_formats"),
        )
        _LOGGER.info(
            "Forward via service: %s -> %s (%s)",
            source_device["name"], dest_device["name"], result,
        )
        return

    # No active bridge: open one (source -> new dest). Covers the
    # ESP-called-HA-now-route-it-to-another-ESP case.
    bridge_id = f"{source_device['device_id']}_{dest_device['device_id']}"
    await _stop_device_sessions(source_device["device_id"], hass=hass)

    new_bridge = BridgeSession(
        hass=hass,
        bridge_id=bridge_id,
        source_device_id=source_device["device_id"],
        source_host=source_device["host"],
        source_name=source_device["name"],
        dest_device_id=dest_device["device_id"],
        dest_host=dest_device["host"],
        dest_name=dest_device["name"],
        source_transport_type=_select_transport_type(hass, source_device["host"]),
        dest_transport_type=_select_transport_type(hass, dest_device["host"]),
        source_audio_mode=source_device.get("audio_mode", "full_duplex"),
        dest_audio_mode=dest_device.get("audio_mode", "full_duplex"),
        source_tx_formats=_device_formats(source_device, "tx_formats"),
        source_rx_formats=_device_formats(source_device, "rx_formats"),
        dest_tx_formats=_device_formats(dest_device, "tx_formats"),
        dest_rx_formats=_device_formats(dest_device, "rx_formats"),
    )
    _bridges[bridge_id] = new_bridge
    result = await new_bridge.start()

    if result in ("connected", "ringing"):
        _LOGGER.info(
            "Forward (new bridge) via service: %s -> %s (%s)",
            source_device["name"], dest_device["name"], result,
        )
    else:
        _bridges.pop(bridge_id, None)
        _LOGGER.error(
            "Forward failed: %s -> %s",
            source_device["name"], dest_device["name"],
        )


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
    server = hass.data.get(DOMAIN, {}).get("sip_server")
    if invite is None or server is None:
        _LOGGER.warning("sip_answer: no pending SIP call %s", call_id or "(current)")
        return

    cfg = _get_transport_config(hass)
    local_ip = await _ha_advertise_host(hass)
    from .sdp import build_answer
    answer = build_answer(local_ip, local_ip, int(cfg["rtp_port"]), invite.selected_format)
    if not server.send_final_response(call_id, 200, "OK", answer_sdp=answer):
        _LOGGER.warning("sip_answer: SIP transaction not found for %s", call_id)
        return

    _LOGGER.info("SIP answered call_id=%s", call_id)
    _set_ha_softphone_call_state(
        hass,
        "streaming",
        session_device_id=HA_SOFTPHONE_DEVICE_ID,
        caller=invite.caller,
        callee=_ha_peer_name(hass),
        peer_name=invite.caller,
        direction="incoming",
        call_id=call_id,
    )


async def _handle_sip_decline_service(call: ServiceCall) -> None:
    hass: HomeAssistant = call.hass
    call_id = str(call.data.get("call_id") or "").strip()
    status = int(call.data.get("status") or 486)
    reason = str(call.data.get("reason") or "Busy Here").strip() or "Busy Here"
    pending = hass.data.get(DOMAIN, {}).setdefault("sip_pending", {})
    if not call_id and len(pending) == 1:
        call_id = next(iter(pending))
    pending.pop(call_id, None)
    server = hass.data.get(DOMAIN, {}).get("sip_server")
    if not call_id or server is None or not server.send_final_response(call_id, status, reason):
        _LOGGER.warning("sip_decline: no pending SIP call %s", call_id or "(current)")
        return

    _LOGGER.info("SIP declined call_id=%s status=%s reason=%s", call_id, status, reason)
    _set_ha_softphone_call_state(
        hass,
        "declined",
        session_device_id=HA_SOFTPHONE_DEVICE_ID,
        reason=reason,
        call_id=call_id,
    )


async def _handle_sip_hangup_service(call: ServiceCall) -> None:
    hass: HomeAssistant = call.hass
    call_id = str(call.data.get("call_id") or "").strip()
    bucket = hass.data.get(DOMAIN, {})
    clients = bucket.setdefault("sip_clients", {})
    relays = bucket.setdefault("sip_relays", {})
    if not call_id and len(clients) == 1:
        call_id = next(iter(clients))
    client = clients.pop(call_id, None) if call_id else None
    relay = relays.pop(call_id, None) if call_id else None
    if client is not None:
        client.bye()
        await client.close()
    if relay is not None:
        await relay.stop()
    _fire_call_event(
        hass,
        {"state": "ended", "scope": "sip", "call_id": call_id},
        "sip",
    )
    _LOGGER.info("SIP hangup call_id=%s client=%s relay=%s", call_id, client is not None, relay is not None)


async def _refresh_phonebook_sensor(hass: HomeAssistant) -> None:
    sensor = hass.data.get(DOMAIN, {}).get("phonebook_sensor")
    if sensor is not None:
        await sensor.async_update()


async def _handle_phonebook_add_contact_service(call: ServiceCall) -> None:
    from .roster import RosterEntry

    hass: HomeAssistant = call.hass
    entry = RosterEntry(
        id=str(call.data["id"]).strip(),
        name=str(call.data.get("name") or call.data["id"]).strip(),
        kind=str(call.data.get("kind") or "esp").strip().lower(),
        address=str(call.data.get("address") or "").strip(),
        sip_uri=str(call.data.get("sip_uri") or "").strip(),
        number=str(call.data.get("number") or "").strip(),
        route_via_ha=bool(call.data.get("route_via_ha", False)),
    )
    bucket = hass.data.setdefault(DOMAIN, {}).setdefault("manual_roster_entries", [])
    bucket[:] = [item for item in bucket if getattr(item, "id", "").lower() != entry.id.lower()]
    bucket.append(entry)
    await _refresh_phonebook_sensor(hass)
    _LOGGER.info("Phonebook contact added: %s (%s)", entry.id, entry.kind)


async def _handle_phonebook_set_contacts_service(call: ServiceCall) -> None:
    from .roster import parse_roster_json

    hass: HomeAssistant = call.hass
    entries = parse_roster_json(str(call.data.get("roster_json") or "[]"))
    hass.data.setdefault(DOMAIN, {})["manual_roster_entries"] = entries
    await _refresh_phonebook_sensor(hass)
    _LOGGER.info("Phonebook manual contacts replaced: %d entries", len(entries))


async def _handle_phonebook_clear_service(call: ServiceCall) -> None:
    hass: HomeAssistant = call.hass
    hass.data.setdefault(DOMAIN, {})["manual_roster_entries"] = []
    await _refresh_phonebook_sensor(hass)
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


async def _handle_sip_call_target_service(call: ServiceCall) -> None:
    """Originate a standards SIP call from HA to a roster target or URI-shaped target."""
    from homeassistant.exceptions import ServiceValidationError

    from .roster import parse_roster_json, resolve_target
    from .sip import parse_sip_uri
    from .sip_client import SipCallClient

    hass: HomeAssistant = call.hass
    target = str(call.data.get("target") or call.data.get("call") or "").strip()
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
        force_ha=bool(call.data.get("route_via_ha", False)),
    )
    if route.kind == "requires_pbx":
        raise ServiceValidationError(f"{target} requires Asterisk/PBX trunk routing")
    if route.kind not in {"direct", "via_ha"} or not route.sip_uri:
        raise ServiceValidationError(f"cannot resolve SIP target: {target}")
    uri = parse_sip_uri(route.sip_uri)
    client = SipCallClient(
        local_ip=local_ip,
        local_name=_ha_peer_name(hass),
        local_sip_port=int(cfg["sip_port"]),
        local_rtp_port=int(cfg["rtp_port"]),
        supported_formats=list(HA_BROWSER_TX_FORMATS),
    )
    result = await client.invite(
        target=uri.user,
        remote_host=uri.host,
        remote_sip_port=uri.port or int(cfg["sip_port"]),
    )
    bucket = hass.data.setdefault(DOMAIN, {})
    active = bucket.setdefault("sip_clients", {})
    if result in {"ringing", "streaming"}:
        active[client.dialog_ids.call_id] = client
        if result == "ringing":
            async def _watch_sip_final() -> None:
                final = await client.wait_for_final()
                _fire_call_event(
                    hass,
                    {
                        "state": final,
                        "scope": "sip",
                        "call_id": client.dialog_ids.call_id,
                        "target": target,
                        "sip_uri": route.sip_uri,
                    },
                    "sip",
                )
                if final not in {"ringing", "streaming"}:
                    active.pop(client.dialog_ids.call_id, None)
                    await client.close()

            hass.async_create_task(_watch_sip_final())
    else:
        await client.close()
    _fire_call_event(
        hass,
        {
            "state": result,
            "scope": "sip",
            "call_id": client.dialog_ids.call_id,
            "target": target,
            "sip_uri": route.sip_uri,
        },
        "sip",
    )
    _LOGGER.info("SIP call target=%s uri=%s result=%s", target, route.sip_uri, result)


async def _async_register_services(hass: HomeAssistant) -> None:
    """Register HA services for intercom control."""

    @_with_target_device("answer")
    async def handle_answer(call: ServiceCall, device: dict) -> None:
        await _handle_answer_service(call, device)

    @_with_target_device("decline")
    async def handle_decline(call: ServiceCall, device: dict) -> None:
        await _handle_decline_service(call, device)

    @_with_target_device("hangup")
    async def handle_hangup(call: ServiceCall, device: dict) -> None:
        await _handle_hangup_service(call, device)

    @_with_target_device("call")
    async def handle_call(call: ServiceCall, dest_device: dict) -> None:
        await _handle_call_service(call, dest_device)

    @_with_target_device("forward")
    async def handle_forward(call: ServiceCall, source_device: dict) -> None:
        await _handle_forward_service(call, source_device)

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

    async def handle_sip_call(call: ServiceCall) -> None:
        await _handle_sip_call_target_service(call)

    # PREVENT_EXTRA so unknown fields raise instead of being silently dropped.
    # Target selectors are still valid payload fields here because the custom
    # card calls services directly with data={"device_id": ...}; the resolver
    # also accepts HA-native call.target for automations/UI actions.
    target_fields = {
        vol.Optional("device_id"): vol.Any(cv.string, [cv.string]),
        vol.Optional("entity_id"): vol.Any(cv.entity_id, [cv.entity_id]),
        vol.Optional("name"): cv.string,
        vol.Optional("friendly_name"): cv.string,
    }
    target_schema = vol.All(
        vol.Schema(target_fields, extra=vol.PREVENT_EXTRA),
        _require_service_target,
    )
    decline_schema = vol.Schema(
        {**target_fields, vol.Optional("reason", default=""): cv.string},
        extra=vol.PREVENT_EXTRA,
    )
    call_schema = vol.Schema(
        {**target_fields, vol.Optional("source"): cv.string},
        extra=vol.PREVENT_EXTRA,
    )
    decline_schema = vol.All(decline_schema, _require_service_target)
    call_schema = vol.All(call_schema, _require_service_target)
    forward_schema = vol.All(
        vol.Schema(
            {**target_fields, vol.Required("forward_to"): cv.string},
            extra=vol.PREVENT_EXTRA,
        ),
        _require_service_target,
    )
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
        },
        extra=vol.PREVENT_EXTRA,
    )
    sip_hangup_schema = vol.Schema(
        {vol.Optional("call_id", default=""): cv.string},
        extra=vol.PREVENT_EXTRA,
    )
    sip_call_schema = vol.Schema(
        {
            vol.Optional("target"): cv.string,
            vol.Optional("call"): cv.string,
            vol.Optional("route_via_ha", default=False): cv.boolean,
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
            vol.Optional("route_via_ha", default=False): cv.boolean,
        },
        extra=vol.PREVENT_EXTRA,
    )
    phonebook_set_schema = vol.Schema(
        {vol.Required("roster_json"): cv.string},
        extra=vol.PREVENT_EXTRA,
    )
    hass.services.async_register(DOMAIN, "answer", handle_answer, schema=target_schema)
    hass.services.async_register(DOMAIN, "decline", handle_decline, schema=decline_schema)
    hass.services.async_register(DOMAIN, "hangup", handle_hangup, schema=target_schema)
    hass.services.async_register(DOMAIN, "call", handle_call, schema=call_schema)
    hass.services.async_register(DOMAIN, "forward", handle_forward, schema=forward_schema)
    hass.services.async_register(DOMAIN, "purge_devices", handle_purge_devices, schema=purge_schema)
    hass.services.async_register(DOMAIN, "sip_answer", handle_sip_answer, schema=sip_answer_schema)
    hass.services.async_register(DOMAIN, "sip_decline", handle_sip_decline, schema=sip_decline_schema)
    hass.services.async_register(DOMAIN, "sip_hangup", handle_sip_hangup, schema=sip_hangup_schema)
    hass.services.async_register(DOMAIN, "sip_call", handle_sip_call, schema=sip_call_schema)
    hass.services.async_register(
        DOMAIN, "phonebook_add_contact", handle_phonebook_add_contact, schema=phonebook_add_schema
    )
    hass.services.async_register(
        DOMAIN, "phonebook_set_contacts", handle_phonebook_set_contacts, schema=phonebook_set_schema
    )
    hass.services.async_register(DOMAIN, "phonebook_clear", handle_phonebook_clear)
    hass.services.async_register(DOMAIN, "phonebook_export", handle_phonebook_export)


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

    _LOGGER.info("Intercom Native loaded (PBX-lite, TCP+UDP listeners, roster-driven routing)")


async def async_setup(hass: HomeAssistant, config: dict) -> bool:
    """Set up Intercom Native defaults from configuration.yaml."""
    hass.data.setdefault(DOMAIN, {})["transport_config"] = {
        "use_tcp": True,
        "use_sip": True,
        "sip_port": INTERCOM_SIP_PORT,
        "rtp_port": INTERCOM_RTP_PORT,
        "use_udp": False,
        "tcp_port": INTERCOM_PORT,
        "udp_audio_port": INTERCOM_UDP_AUDIO_PORT,
        "udp_control_port": INTERCOM_UDP_CONTROL_PORT,
        "udp_max_payload": UDP_SAFE_PAYLOAD_BYTES,
    }
    hass.data[DOMAIN]["tcp_port"] = INTERCOM_PORT
    hass.data[DOMAIN]["sip_port"] = INTERCOM_SIP_PORT
    await _async_setup_shared(hass, config)
    return True


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up Intercom Native from a config entry (UI setup)."""
    cfg = _entry_transport_config(entry)
    hass.data.setdefault(DOMAIN, {})["transport_config"] = cfg
    hass.data[DOMAIN]["tcp_port"] = cfg["tcp_port"]
    hass.data[DOMAIN]["sip_port"] = cfg["sip_port"]
    await _async_setup_shared(hass)
    await _async_apply_assist_intents(
        hass,
        bool(entry.data.get(CONF_ASSIST_INTENTS, False)),
    )
    if cfg["use_tcp"]:
        if not await _async_start_tcp_socket_manager(hass):
            raise ConfigEntryError(
                f"Failed to bind TCP port {cfg['tcp_port']}. Another process or "
                "another HA integration is already listening on that port."
            )
        await _async_register_tcp_mdns_service(hass, entry)
    if cfg["use_sip"]:
        if not await _async_start_sip_udp_server(hass):
            raise ConfigEntryError(
                f"Failed to bind SIP UDP port {cfg['sip_port']}. Another SIP "
                "endpoint may already be listening on that port."
            )
    if cfg["use_udp"]:
        if not await _async_start_udp_socket_manager(hass):
            raise ConfigEntryError(
                f"Failed to bind UDP audio={cfg['udp_audio_port']} / "
                f"control={cfg['udp_control_port']}. Another process is "
                "already listening on one of those ports."
            )
        await _async_register_udp_mdns_service(hass, entry)
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

    await _async_unregister_udp_mdns_service(hass, entry)
    await _async_unregister_tcp_mdns_service(hass, entry)
    await _async_stop_sip_udp_server(hass)
    await _async_stop_udp_socket_manager(hass)
    await _async_stop_tcp_socket_manager(hass)
    hass.data.get(DOMAIN, {}).pop(entry.entry_id, None)
    return unload_ok


async def _async_start_sip_udp_server(hass: HomeAssistant) -> bool:
    """Bind the SIP/UDP endpoint for standards-compatible phase-1 calls."""
    from .roster import RosterEntry, resolve_target
    from .sdp import build_answer
    from .sip import parse_sip_uri
    from .sip_client import SipCallClient
    from .sip_listener import SipInvite, SipInviteResult, SipUdpServer
    from .sip_rtp_bridge import RtpPeer, SipRtpRelay

    if hass.data.get(DOMAIN, {}).get("sip_server") is not None:
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
                        "transport": peer.transport,
                        "tcp_port": peer.tcp_port,
                        "udp_audio_port": peer.udp_audio_port,
                        "udp_control_port": peer.udp_control_port,
                        "sip_port": peer.sip_port,
                        "rtp_port": peer.rtp_port,
                        "audio_mode": peer.audio_mode,
                    },
                )
            )
        return entries

    async def _on_invite(invite: SipInvite) -> SipInviteResult:
        peers = await _async_build_peer_snapshot(hass)
        decision = resolve_target(invite.target, _roster_from_peers(peers), route_via_ha=True)
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
        if decision.kind == "requires_pbx":
            return SipInviteResult(503, "Service Unavailable", to_tag="")
        if decision.reason == "ha_required":
            return SipInviteResult(404, "Not Found", to_tag="")
        if decision.kind in {"direct", "via_ha", "group"} and decision.entry is not None:
            decision_uri = parse_sip_uri(decision.sip_uri) if decision.sip_uri else None
            if decision_uri is not None and decision_uri.host != local_ip:
                bucket = hass.data.setdefault(DOMAIN, {})
                next_port = int(bucket.get("sip_rtp_next_port", int(cfg["rtp_port"]) + 2))
                source_relay_port = next_port
                dest_relay_port = next_port + 2
                bucket["sip_rtp_next_port"] = next_port + 4
                client = SipCallClient(
                    local_ip=local_ip,
                    local_name=_ha_peer_name(hass),
                    local_sip_port=int(cfg["sip_port"]),
                    local_rtp_port=dest_relay_port,
                    supported_formats=[invite.selected_format.audio_format],
                )
                result = await client.invite(
                    target=decision_uri.user,
                    remote_host=decision_uri.host,
                    remote_sip_port=decision_uri.port or int(cfg["sip_port"]),
                )
                active = bucket.setdefault("sip_clients", {})
                active[client.dialog_ids.call_id] = client

                async def _finish_bridge(initial_result: str) -> None:
                    final = initial_result
                    if final == "ringing":
                        final = await client.wait_for_final()
                    server = hass.data.get(DOMAIN, {}).get("sip_server")
                    if final != "streaming" or client.dialog is None or server is None:
                        if server is not None:
                            server.send_final_response(invite.call_id, 486, "Busy Here")
                        active.pop(client.dialog_ids.call_id, None)
                        await client.close()
                        return
                    relay = SipRtpRelay(
                        left=RtpPeer(
                            host=invite.remote_rtp_host,
                            port=invite.remote_rtp_port,
                            payload_type=invite.selected_format.payload_type,
                            audio_format=invite.selected_format.audio_format,
                        ),
                        right=RtpPeer(
                            host=client.dialog.remote_rtp_host,
                            port=client.dialog.remote_rtp_port,
                            payload_type=client.dialog.selected_format.payload_type,
                            audio_format=client.dialog.selected_format.audio_format,
                        ),
                        left_port=source_relay_port,
                        right_port=dest_relay_port,
                    )
                    await relay.start()
                    bucket.setdefault("sip_relays", {})[invite.call_id] = relay
                    answer = build_answer(
                        local_ip,
                        local_ip,
                        source_relay_port,
                        invite.selected_format,
                    )
                    server.send_final_response(invite.call_id, 200, "OK", answer_sdp=answer)
                    _fire_call_event(
                        hass,
                        {
                            "state": "streaming",
                            "scope": "sip_bridge",
                            "call_id": invite.call_id,
                            "target": invite.target,
                            "dest_call_id": client.dialog_ids.call_id,
                        },
                        "sip",
                    )

                hass.async_create_task(_finish_bridge(result))
                return SipInviteResult(180, "Ringing", to_tag="", defer_final=True)
            hass.data.setdefault(DOMAIN, {}).setdefault("sip_pending", {})[invite.call_id] = invite
            _set_ha_softphone_call_state(
                hass,
                "ringing",
                session_device_id=HA_SOFTPHONE_DEVICE_ID,
                caller=invite.caller,
                callee=invite.target,
                peer_name=invite.caller,
                direction="incoming",
                call_id=invite.call_id,
                route_kind=decision.kind,
                sip_uri=decision.sip_uri,
            )
            return SipInviteResult(180, "Ringing", to_tag="", defer_final=True)
        answer = build_answer(
            local_ip,
            local_ip,
            int(cfg["rtp_port"]),
            invite.selected_format,
        )
        return SipInviteResult(200, "OK", answer_sdp=answer, to_tag="")

    server = SipUdpServer(
        host="0.0.0.0",
        port=int(cfg["sip_port"]),
        local_ip=local_ip,
        local_rtp_port=int(cfg["rtp_port"]),
        supported_formats=[
            AudioFormat(48000, "s16le", 1, 20),
            AudioFormat(32000, "s16le", 1, 20),
            AudioFormat(16000, "s16le", 1, 20),
        ],
        on_invite=_on_invite,
    )
    if not await server.start():
        return False
    hass.data[DOMAIN]["sip_server"] = server
    _LOGGER.info("SIP endpoint enabled on UDP/%s (RTP base %s)", cfg["sip_port"], cfg["rtp_port"])
    return True


async def _async_stop_sip_udp_server(hass: HomeAssistant) -> None:
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
    server = hass.data.get(DOMAIN, {}).pop("sip_server", None)
    if server is not None:
        await server.stop()


async def _async_start_udp_socket_manager(hass: HomeAssistant) -> bool:
    """Bind the shared UDP sockets and route unsolicited inbound calls.

    True on success or already-running; False on bind failure (caller
    surfaces via ConfigEntryError).
    """
    from .udp_socket_manager import IntercomUdpSocketManager

    if hass.data.get(DOMAIN, {}).get("udp_manager") is not None:
        return True

    cfg = _get_transport_config(hass)
    manager = IntercomUdpSocketManager(
        hass,
        audio_port=cfg["udp_audio_port"],
        control_port=cfg["udp_control_port"],
        max_payload=cfg["udp_max_payload"],
    )
    if not await manager.start():
        _LOGGER.error("Failed to start UdpSocketManager; UDP path disabled")
        return False
    hass.data[DOMAIN]["udp_manager"] = manager

    async def _on_unsolicited(
        caller_name: str,
        caller_route: str,
        dest_name: str,
        dest_route: str,
        call_id: str,
        host: str,
        port: int,
        caller_tx_formats: list[AudioFormat],
        caller_rx_formats: list[AudioFormat],
    ) -> None:
        inbound = InboundStart(
            host=host,
            caller_name=caller_name,
            caller_route=caller_route,
            dest_name=dest_name,
            dest_route=dest_route,
            call_id=call_id,
            port=port,
            transport=None,
            caller_tx_formats=caller_tx_formats,
            caller_rx_formats=caller_rx_formats,
        )
        try:
            require_udp_safe_formats(
                caller_tx_formats or [LEGACY_AUDIO_FORMAT],
                context=f"UDP caller {caller_name or host} tx_formats",
                max_payload=cfg["udp_max_payload"],
            )
            require_udp_safe_formats(
                caller_rx_formats or [LEGACY_AUDIO_FORMAT],
                context=f"UDP caller {caller_name or host} rx_formats",
                max_payload=cfg["udp_max_payload"],
            )
        except ValueError as err:
            _LOGGER.warning("Rejecting UDP START from %s: %s", host, err)
            await _decline_inbound_start(hass, inbound, "unsupported_udp_audio_format")
            return
        await _route_inbound_call_pbx_lite(
            hass,
            inbound,
        )

    manager.set_unsolicited_callback(_on_unsolicited)
    return True


async def _async_stop_udp_socket_manager(hass: HomeAssistant) -> None:
    """Tear down the shared UDP sockets on entry unload."""
    manager = hass.data.get(DOMAIN, {}).pop("udp_manager", None)
    if manager is not None:
        await manager.stop()


def _is_ha_inbound_destination(hass: HomeAssistant, dest_name: str) -> bool:
    """True when an inbound START is addressed to the HA softphone/card."""
    dest_key = (dest_name or "").strip().lower()
    if not dest_key:
        return True
    return dest_key in {"home assistant", _ha_peer_name(hass).strip().lower()}


def _find_inbound_dest_device(
    devices: list[dict],
    dest_name: str,
    dest_route: str,
) -> dict | None:
    """Resolve an inbound bridge destination by friendly name, then route id.

    Friendly name is the public intercom identity. `dest_route` is only a
    compatibility hint; never let a technical route shadow the visible
    destination name selected by the caller.
    """
    dest_key = (dest_name or "").strip().lower()
    if not dest_key:
        return None

    dest_device = next(
        (d for d in devices if (d.get("name") or "").strip().lower() == dest_key),
        None,
    )
    route_key = (dest_route or "").strip().lower()
    if dest_device is None and route_key and route_key != dest_key:
        dest_device = next(
            (d for d in devices if (d.get("route_id") or "").strip().lower() == route_key),
            None,
        )
    return dest_device


def _find_inbound_source_device(
    devices: list[dict],
    host: str,
    caller_name: str,
    caller_route: str,
) -> tuple[dict | None, str]:
    """Resolve an inbound caller.

    The socket peer IP is the strongest match on flat LANs, but routed/VPN/NAT
    installs can expose a different source address to HA. PBX-lite START already
    carries the caller route/name, so use those as identity fallbacks instead of
    rejecting a valid call as unregistered.
    """
    host_key = (host or "").strip()
    if host_key:
        device = next((d for d in devices if d.get("host") == host_key), None)
        if device is not None:
            return device, "host"

    route_key = (caller_route or "").strip().lower()
    if route_key:
        device = next(
            (d for d in devices if (d.get("route_id") or "").strip().lower() == route_key),
            None,
        )
        if device is not None:
            return device, "caller_route"

    name_key = (caller_name or "").strip().lower()
    if name_key:
        device = next(
            (d for d in devices if (d.get("name") or "").strip().lower() == name_key),
            None,
        )
        if device is not None:
            return device, "caller_name"

    return None, ""


def _external_inbound_source_device(
    hass: HomeAssistant,
    inbound: "InboundStart",
) -> dict:
    """Build a peer identity for callers outside HA's ESP device registry."""
    caller_name = (
        (inbound.caller_name or "").strip()
        or (inbound.caller_route or "").strip()
        or inbound.host
        or "External caller"
    )
    route_id = (inbound.caller_route or "").strip() or slugify(caller_name)
    transport = "tcp" if inbound.transport is not None else "udp"
    device_id = f"external_{slugify(route_id or caller_name or inbound.host)}"
    return {
        "device_id": device_id,
        "name": caller_name,
        "route_id": route_id,
        "host": inbound.host,
        "transport": transport,
        "tcp_port": INTERCOM_PORT if transport == "tcp" else None,
        "udp_audio_port": None,
        "udp_control_port": getattr(inbound, "port", None) or None,
        "audio_mode": "full_duplex",
        "esphome_id": "",
        "entities": {},
        "external": True,
    }


def _destination_busy_reason(hass: HomeAssistant, dest_device: dict) -> str | None:
    """Return a log-friendly busy reason for a bridge destination."""
    dest_device_id = dest_device["device_id"]
    dest_bridge = _bridge_for_device(dest_device_id)
    if dest_bridge is not None:
        return f"bridge {dest_bridge.bridge_id}"
    if _session_get(dest_device_id) is not None:
        return "HA session"
    if _state_entity_is_busy(hass, dest_device):
        return (dest_device.get("entities") or {}).get("intercom_state", "ESP state")
    return None


async def _bridge_inbound_call_pbx_lite(
    hass: HomeAssistant,
    inbound: "InboundStart",
    source_device: dict,
    dest_device: dict,
) -> None:
    """Bridge an unsolicited START from one ESP to another ESP."""
    observed_host = inbound.host
    source_host = source_device.get("host") or observed_host
    caller_name = inbound.caller_name
    call_id = inbound.call_id
    inbound_transport = inbound.transport
    source_device_id = source_device["device_id"]
    dest_device_id = dest_device["device_id"]

    bridge_id = call_id or f"{source_device_id}_{dest_device_id}"
    if bridge_id in _bridges:
        _LOGGER.debug("Bridge %s already exists, replaying current START response", bridge_id)
        await _bridges[bridge_id].replay_source_start(inbound_transport)
        return

    if _device_has_ha_call(source_device_id):
        _LOGGER.info(
            "Unsolicited MSG_START from %s rejected: source %s already has an HA call",
            observed_host,
            source_device["name"],
        )
        await _decline_inbound_start(hass, inbound, "busy")
        return

    busy_reason = _destination_busy_reason(hass, dest_device)
    if busy_reason is not None:
        _LOGGER.info(
            "Unsolicited MSG_START from %s (%s) rejected: dest %s is busy (%s)",
            observed_host,
            caller_name or "unknown",
            dest_device["name"],
            busy_reason,
        )
        await _decline_inbound_start(hass, inbound, "busy")
        return

    _LOGGER.info(
        "Unsolicited MSG_START from %s as %s (caller=%s) -> bridging to %s (call_id=%s)",
        observed_host, source_host, caller_name or "unknown", dest_device["name"], bridge_id,
    )
    bridge = BridgeSession(
        hass=hass,
        bridge_id=bridge_id,
        source_device_id=source_device_id,
        source_host=source_host,
        source_name=source_device["name"],
        dest_device_id=dest_device_id,
        dest_host=dest_device["host"],
        dest_name=dest_device["name"],
        source_transport_type="tcp" if inbound_transport is not None else _device_transport(hass, source_device),
        dest_transport_type=_select_transport_type(hass, dest_device["host"]),
        source_transport=inbound_transport,
        source_call_id=call_id,
        source_audio_mode=source_device.get("audio_mode", "full_duplex"),
        dest_audio_mode=dest_device.get("audio_mode", "full_duplex"),
        source_tx_formats=inbound.caller_tx_formats or _device_formats(source_device, "tx_formats"),
        source_rx_formats=inbound.caller_rx_formats or _device_formats(source_device, "rx_formats"),
        dest_tx_formats=_device_formats(dest_device, "tx_formats"),
        dest_rx_formats=_device_formats(dest_device, "rx_formats"),
    )
    _bridges[bridge_id] = bridge
    result = await bridge.start()
    if result not in ("connected", "ringing"):
        _bridges.pop(bridge_id, None)
        _LOGGER.error(
            "Unsolicited bridge start failed: %s -> %s",
            source_device["name"], dest_device["name"],
        )


async def _ring_ha_for_inbound_call(
    hass: HomeAssistant,
    inbound: "InboundStart",
    source_device: dict,
) -> None:
    """Route an unsolicited START to the HA softphone/card."""
    observed_host = inbound.host
    source_host = source_device.get("host") or observed_host
    caller_name = inbound.caller_name
    call_id = inbound.call_id
    inbound_transport = inbound.transport
    source_device_id = source_device["device_id"]

    def _fire_ha_reject(reason: str) -> None:
        _set_ha_softphone_call_state(
            hass,
            "declined",
            session_device_id=source_device_id,
            caller=caller_name or source_device.get("name") or "",
            callee=_ha_peer_name(hass),
            peer_name=source_device.get("name") or caller_name or "",
            direction="incoming",
            call_id=call_id,
            reason=reason,
        )

    if _device_has_ha_call(source_device_id):
        _LOGGER.info(
            "Unsolicited MSG_START from %s rejected: source %s already has an HA call",
            observed_host,
            source_device["name"],
        )
        _fire_ha_reject("busy")
        await _decline_inbound_start(hass, inbound, "busy")
        return

    if _sessions:
        _LOGGER.info(
            "Unsolicited MSG_START from %s rejected: HA softphone already has a session",
            observed_host,
        )
        _fire_ha_reject("busy")
        await _decline_inbound_start(hass, inbound, "busy")
        return

    if _ha_softphone_dnd(hass):
        _LOGGER.info(
            "Unsolicited MSG_START from %s rejected: HA softphone DND is enabled",
            observed_host,
        )
        _fire_ha_reject("DND")
        await _decline_inbound_start(hass, inbound, "DND")
        return

    _LOGGER.info(
        "Unsolicited MSG_START from %s as %s (caller=%s, call_id=%s) - ringing on HA card as %s",
        observed_host, source_host, caller_name or "unknown", call_id or "-", source_device_id,
    )
    transport_type = "tcp" if inbound_transport is not None else _device_transport(hass, source_device)
    session = IntercomSession(
        hass=hass,
        device_id=source_device_id,
        host=source_host,
        transport_type=transport_type,
        transport=inbound_transport,
        call_id=call_id,
        caller_name=caller_name,
        local_name=_ha_peer_name(hass),
        peer_name=source_device.get("name") or caller_name or "",
        direction="incoming",
        audio_mode=source_device.get("audio_mode", "full_duplex"),
        local_tx_formats=list(HA_BROWSER_TX_FORMATS),
        local_rx_formats=list(HA_BROWSER_RX_FORMATS),
        peer_tx_formats=inbound.caller_tx_formats or _device_formats(source_device, "tx_formats"),
        peer_rx_formats=inbound.caller_rx_formats or _device_formats(source_device, "rx_formats"),
    )
    if await session.start_ringing(caller_name=caller_name):
        _session_register(source_device_id, session)
        _set_ha_softphone_call_state(
            hass,
            "ringing",
            session_device_id=source_device_id,
            caller=caller_name or source_device.get("name") or "",
            callee=_ha_peer_name(hass),
            peer_name=source_device.get("name") or caller_name or "",
            direction="incoming",
            call_id=call_id,
        )
        return

    _LOGGER.error("Unsolicited start_ringing failed for %s", observed_host)
    if inbound_transport is not None:
        try:
            await inbound_transport.disconnect()
        except Exception:
            pass


async def _route_inbound_call_pbx_lite(
    hass: HomeAssistant,
    inbound: "InboundStart",
) -> None:
    """Dispatch an unsolicited MSG_START.

    dest empty / HA location_name -> ring HA card.
    dest friendly name matches an intercom device -> open BridgeSession.
    dest set but no match -> DECLINE("unreachable").
    """
    host = inbound.host
    dest_name = inbound.dest_name
    dest_route = inbound.dest_route
    devices = await _get_intercom_devices(hass)
    ha_destination = _is_ha_inbound_destination(hass, dest_name)
    source_device, source_match = _find_inbound_source_device(
        devices,
        host,
        inbound.caller_name,
        inbound.caller_route,
    )
    if source_device is None:
        source_device = _external_inbound_source_device(
            hass,
            inbound,
        )
        source_match = "external"
        _LOGGER.info(
            "Unsolicited MSG_START from %s accepted as external caller "
            "(caller_route=%s, caller_name=%s, target=%s)",
            host,
            inbound.caller_route or "-",
            inbound.caller_name or "-",
            "HA" if ha_destination else (dest_name or dest_route or "-"),
        )
    if source_match != "host":
        _LOGGER.info(
            "Unsolicited MSG_START from %s matched source %s by %s "
            "(endpoint host=%s, caller_route=%s, caller_name=%s)",
            host,
            source_device["name"],
            source_match,
            source_device.get("host") or "-",
            inbound.caller_route or "-",
            inbound.caller_name or "-",
        )
    if inbound.transport is None and source_device.get("transport") == "udp":
        manager = hass.data.get(DOMAIN, {}).get("udp_manager")
        if manager is not None:
            manager.alias_peer(
                host,
                source_device.get("host") or host,
                audio_port=source_device.get("udp_audio_port"),
                control_port=getattr(inbound, "port", None),
            )

    if not ha_destination:
        dest_clean = (dest_name or "").strip()
        dest_device = _find_inbound_dest_device(devices, dest_clean, dest_route)
        if dest_device is None:
            _LOGGER.warning(
                "Unsolicited MSG_START from %s wants bridge to '%s' (route=%s) but no phonebook "
                "device matches; declining unreachable",
                host, dest_clean, dest_route or "-",
            )
            await _decline_inbound_start(hass, inbound, "unreachable")
            return
        await _bridge_inbound_call_pbx_lite(hass, inbound, source_device, dest_device)
        return

    await _ring_ha_for_inbound_call(hass, inbound, source_device)


async def _async_start_tcp_socket_manager(hass: HomeAssistant) -> bool:
    """Bind the shared TCP listener; True on success or already-running."""
    from .tcp_socket_manager import IntercomTcpSocketManager
    if hass.data.get(DOMAIN, {}).get("tcp_manager") is not None:
        return True
    port = hass.data.get(DOMAIN, {}).get("tcp_port", INTERCOM_PORT)
    manager = IntercomTcpSocketManager(hass, port=port)
    if not await manager.start():
        _LOGGER.error("Failed to start IntercomTcpSocketManager; TCP inbound disabled")
        return False
    hass.data[DOMAIN]["tcp_manager"] = manager

    async def _on_unsolicited_tcp(
        caller_name: str,
        caller_route: str,
        dest_name: str,
        dest_route: str,
        call_id: str,
        host: str,
        transport,
        caller_tx_formats: list[AudioFormat],
        caller_rx_formats: list[AudioFormat],
    ) -> None:
        await _route_inbound_call_pbx_lite(
            hass,
            InboundStart(
                host=host,
                caller_name=caller_name,
                caller_route=caller_route,
                dest_name=dest_name,
                dest_route=dest_route,
                call_id=call_id,
                port=0,
                transport=transport,
                caller_tx_formats=caller_tx_formats,
                caller_rx_formats=caller_rx_formats,
            ),
        )

    manager.set_unsolicited_callback(_on_unsolicited_tcp)
    return True


async def _async_stop_tcp_socket_manager(hass: HomeAssistant) -> None:
    manager = hass.data.get(DOMAIN, {}).pop("tcp_manager", None)
    if manager is not None:
        await manager.stop()
