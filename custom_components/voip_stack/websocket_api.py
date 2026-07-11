"""SIP-only WebSocket API for VoIP Stack."""

from __future__ import annotations

import logging
from typing import Any, Dict

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from .call_registry import CallRegistry
from .const import (
    CONF_DEBUG_MODE,
    DOMAIN,
    HA_PEER_FALLBACK_NAME,
    HA_SOFTPHONE_DEVICE_ID,
)
from .fsm import CallState, TerminalReason, sip_phone_state, sip_public_state as _sip_public_state

_LOGGER = logging.getLogger(__name__)

CALL_EVENT = "voip_stack.call_event"
SIP_CALL_STATE_EVENT = "voip_stack.call_state"
SIP_INCOMING_CALL_EVENT = "voip_stack.incoming_call"
SIP_ROUTE_REQUEST_EVENT = "voip_stack.route_request"
SIP_CALL_ENDED_EVENT = "voip_stack.call_ended"
SIP_DTMF_EVENT = "voip_stack.dtmf"
HA_SOFTPHONE_STORE_KEY = f"{DOMAIN}_ha_softphone"
HA_SOFTPHONE_STORE_VERSION = 1

WS_TYPE_LIST = f"{DOMAIN}/list_devices"
WS_TYPE_RESOLVE_DEVICE = f"{DOMAIN}/resolve_device"
WS_TYPE_HA_SOFTPHONE_START = f"{DOMAIN}/ha_softphone_start"
WS_TYPE_HA_SOFTPHONE_STATE = f"{DOMAIN}/ha_softphone_state"
WS_TYPE_SUBSCRIBE_CALL_EVENTS = f"{DOMAIN}/subscribe_call_events"


def _clean_group_token(value: object) -> str:
    return (
        str(value or "")
        .replace("|", " ")
        .replace(";", " ")
        .replace("\r", " ")
        .replace("\n", " ")
        .strip()[:32]
    )


def _clean_group_name(value: object) -> str:
    groups: list[str] = []
    for raw in str(value or "").split(","):
        group = _clean_group_token(raw)
        if group and group not in groups:
            groups.append(group)
    return ", ".join(groups)


def _clean_endpoint_field(value: object, *, max_len: int = 32) -> str:
    return (
        str(value or "")
        .replace("|", " ")
        .replace(",", " ")
        .replace(";", " ")
        .replace("\r", " ")
        .replace("\n", " ")
        .strip()
    )[:max_len]


def _ha_peer_name(hass: HomeAssistant) -> str:
    return (hass.config.location_name or "").strip() or HA_PEER_FALLBACK_NAME


def _direction_for_local(local_name: str, caller: str, callee: str) -> str:
    local = (local_name or "").strip().lower()
    if local and local == (caller or "").strip().lower():
        return "outgoing"
    if local and local == (callee or "").strip().lower():
        return "incoming"
    return ""


def _canonical_session_fields(
    *,
    state: str,
    local_name: str = "",
    peer_name: str = "",
    caller: str = "",
    callee: str = "",
    direction: str = "",
    call_id: str = "",
) -> dict[str, str]:
    caller = (caller or "").strip()
    callee = (callee or "").strip()
    local_name = (local_name or "").strip()
    peer_name = (peer_name or "").strip()
    if not direction and caller and callee:
        direction = _direction_for_local(local_name, caller, callee)
    if not peer_name:
        peer_name = callee if direction == "outgoing" else caller
    if not local_name:
        local_name = caller if direction == "outgoing" else callee
    role = "caller" if direction == "outgoing" else "callee" if direction == "incoming" else ""
    sip_state = _sip_public_state(state)
    return {
        "call_id": call_id or (f"{caller}<->{callee}" if caller and callee else ""),
        "caller": caller,
        "callee": callee,
        "local_name": local_name,
        "peer_name": peer_name,
        "direction": direction,
        "role": role,
        "state": sip_state,
        "sip_state": sip_state,
    }


def _call_event_type(state: str, reason: str | None = None) -> str:
    state = _sip_public_state(state)
    reason = (reason or "").strip().lower()
    if state in (CallState.RINGING.value, CallState.REMOTE_RINGING.value):
        return "ringing"
    if state == CallState.CALLING.value:
        return "outgoing"
    if state == CallState.IN_CALL.value:
        return "answered"
    if state in (
        "error",
        CallState.MEDIA_INCOMPATIBLE.value,
        CallState.TRANSPORT_UNREACHABLE.value,
        CallState.AUTH_REQUIRED_UNSUPPORTED.value,
    ):
        return "failed"
    if state in (
        CallState.IDLE.value,
        CallState.BUSY.value,
        CallState.DECLINED.value,
        CallState.CANCELLED.value,
    ):
        return "missed" if reason == TerminalReason.TIMEOUT.value else "ended"
    return state or "state"


def _json_event_value(value: Any) -> Any:
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, list):
        return [_json_event_value(item) for item in value]
    if isinstance(value, tuple):
        return [_json_event_value(item) for item in value]
    if isinstance(value, dict):
        return {
            str(key): clean
            for key, item in value.items()
            if (clean := _json_event_value(item)) is not None
        }
    return None


def _fire_call_event(hass: HomeAssistant, payload: dict[str, Any], scope: str) -> None:
    event = _json_event_value(payload) or {}
    event["scope"] = scope
    event["state"] = _sip_public_state(str(event.get("state") or ""))
    event["sip_state"] = event["state"]
    reason = event.get("reason") or event.get("terminal_reason")
    event["type"] = _call_event_type(event["state"], str(reason) if reason is not None else None)
    hass.bus.async_fire(CALL_EVENT, event)
    hass.bus.async_fire(SIP_CALL_STATE_EVENT, event)
    if event["state"] == "route_requested":
        hass.bus.async_fire(SIP_ROUTE_REQUEST_EVENT, event)
    if event.get("direction") == "incoming" and event["state"] in (
        "route_requested",
        CallState.RINGING.value,
    ):
        hass.bus.async_fire(SIP_INCOMING_CALL_EVENT, event)
    if event["type"] in {"ended", "missed", "failed"}:
        hass.bus.async_fire(SIP_CALL_ENDED_EVENT, event)


def _ha_softphone_store(hass: HomeAssistant) -> dict[str, Any]:
    return hass.data.setdefault(DOMAIN, {}).setdefault("ha_softphone", {"dnd": False})


def _ha_softphone_groups(hass: HomeAssistant) -> dict[str, Any]:
    store = _ha_softphone_store(hass)
    groups = store.setdefault("groups", {})
    return {
        "ring_group": _clean_group_name(groups.get("ring_group")),
        "conference_group": _clean_group_name(groups.get("conference_group")),
        "conference_ring": bool(groups.get("conference_ring", False)),
    }


def _ha_softphone_extension(hass: HomeAssistant) -> str:
    return _clean_endpoint_field(_ha_softphone_store(hass).get("extension"))


async def async_set_ha_softphone_settings(
    hass: HomeAssistant,
    *,
    extension: object = None,
    ring_group: object = None,
    conference_group: object = None,
    conference_ring: object = None,
) -> dict[str, Any]:
    store = _ha_softphone_store(hass)
    if extension is not None:
        store["extension"] = _clean_endpoint_field(extension)
    groups = _ha_softphone_groups(hass)
    if ring_group is not None:
        groups["ring_group"] = _clean_group_name(ring_group)
    if conference_group is not None:
        groups["conference_group"] = _clean_group_name(conference_group)
    if conference_ring is not None:
        groups["conference_ring"] = bool(conference_ring)
    if not groups["conference_group"]:
        groups["conference_ring"] = False
    store["groups"] = groups
    await _async_save_ha_softphone_store(hass)
    endpoint_sensor = hass.data.get(DOMAIN, {}).get("ha_softphone_endpoint_sensor")
    if endpoint_sensor is not None:
        await endpoint_sensor.async_update()
    state = _ha_softphone_state(hass)
    return state


def _sip_bridge_store(hass: HomeAssistant) -> dict[str, Any]:
    return hass.data.setdefault(DOMAIN, {}).setdefault("sip_bridge_state", {})


async def _async_shutdown_all(hass: HomeAssistant) -> None:
    """Clear HA softphone volatile state before SIP transports are stopped."""
    bucket = hass.data.setdefault(DOMAIN, {})
    registry = bucket.get("call_registry")
    if not isinstance(registry, CallRegistry):
        registry = CallRegistry()
        bucket["call_registry"] = registry
    for route in list(registry.pending_routes.values()):
        future = route.get("future") if isinstance(route, dict) else None
        if future is not None and not future.done():
            future.cancel()
    bucket.pop("sip_bridge_state", None)
    bucket.pop("audio_ws_owners", None)
    store = _ha_softphone_store(hass)
    store.update(
        {
            "state": CallState.IDLE.value,
            "sip_state": CallState.IDLE.value,
            "terminal_reason": TerminalReason.LOCAL_HANGUP.value,
            "last_sip_event": "shutdown",
        }
    )
    for key in (
        "session_device_id",
        "target_device_id",
        "selected_tx_format",
        "selected_rx_format",
        "audio_mode",
        "route_kind",
        "sip_uri",
        "media_debug",
    ):
        store.pop(key, None)


async def _async_load_ha_softphone_store(hass: HomeAssistant) -> None:
    from homeassistant.helpers.storage import Store

    store = Store(hass, HA_SOFTPHONE_STORE_VERSION, HA_SOFTPHONE_STORE_KEY)
    data = await store.async_load() or {}
    runtime = _ha_softphone_store(hass)
    runtime["storage"] = store
    runtime["dnd"] = bool(data.get("dnd", runtime.get("dnd", False)))
    runtime["extension"] = _clean_endpoint_field(data.get("extension", runtime.get("extension", "")))
    stored_groups = data.get("groups") if isinstance(data.get("groups"), dict) else {}
    runtime["groups"] = {
        "ring_group": _clean_group_name(stored_groups.get("ring_group")),
        "conference_group": _clean_group_name(stored_groups.get("conference_group")),
        "conference_ring": bool(stored_groups.get("conference_ring", False)),
    }


async def _async_save_ha_softphone_store(hass: HomeAssistant) -> None:
    runtime = _ha_softphone_store(hass)
    store = runtime.get("storage")
    if store is not None:
        await store.async_save({
            "dnd": bool(runtime.get("dnd", False)),
            "extension": _ha_softphone_extension(hass),
            "groups": _ha_softphone_groups(hass),
        })


def _ha_softphone_dnd(hass: HomeAssistant) -> bool:
    return bool(_ha_softphone_store(hass).get("dnd"))


_MEDIA_COUNTER_KEYS = (
    "rtp_tx_packets",
    "rtp_rx_packets",
    "rtp_tx_bytes",
    "rtp_rx_bytes",
)


def _runtime_counter(store: dict[str, Any], runtime: dict[str, Any], key: str) -> int:
    store_call_id = str(store.get("call_id") or "")
    active_call_ids = {str(call_id) for call_id in runtime.get("active_call_ids", [])}
    pending_call_ids = {str(call_id) for call_id in runtime.get("pending_call_ids", [])}
    store_value = int(store.get(key, 0) or 0)
    runtime_value = int(runtime.get(key, 0) or 0)
    if store_call_id and store_call_id in active_call_ids | pending_call_ids:
        return max(store_value, runtime_value)
    return store_value or runtime_value


def _sip_runtime_snapshot(hass: HomeAssistant) -> dict[str, Any]:
    bucket = hass.data.get(DOMAIN, {})
    registry = bucket.get("call_registry")
    if not isinstance(registry, CallRegistry):
        registry = None
    endpoint = bucket.get("sip_endpoint")
    data: dict[str, Any] = {
        "sip_udp_ready": False,
        "sip_tcp_ready": False,
        "pending_transactions": 0,
        "active_dialogs": 0,
        "pending_call_ids": [],
        "active_call_ids": [],
        "last_sip_event": "",
        "last_sip_status_code": 0,
        "last_sip_reason": "",
        "rtp_tx_packets": 0,
        "rtp_rx_packets": 0,
        "rtp_tx_bytes": 0,
        "rtp_rx_bytes": 0,
        "rtp_dropped_packets": 0,
        "rtp_relays": {},
        "sip_client_dialogs": {},
        "sip_trunk": {},
        "sip_registrar": {},
        "media_debug": {},
    }
    if endpoint is None:
        endpoint = None
    snapshot = getattr(endpoint, "snapshot", None)
    if callable(snapshot):
        snap = snapshot()
        data.update(
            {
                "sip_udp_ready": bool(getattr(snap, "udp_ready", False)),
                "sip_tcp_ready": bool(getattr(snap, "tcp_ready", False)),
                "pending_transactions": int(getattr(snap, "pending_transactions", getattr(snap, "pending_invites", 0))),
                "active_dialogs": int(getattr(snap, "active_dialogs", 0)),
                "pending_call_ids": list(getattr(snap, "pending_call_ids", ()) or ()),
                "active_call_ids": list(getattr(snap, "active_call_ids", ()) or ()),
                "last_sip_event": str(getattr(snap, "last_sip_event", "") or ""),
                "last_sip_status_code": int(getattr(snap, "last_sip_status_code", 0) or 0),
                "last_sip_reason": str(getattr(snap, "last_sip_reason", "") or ""),
            }
        )
    for call_id, relay in dict((registry.relays if registry is not None else {}) or {}).items():
        snap = getattr(relay, "snapshot", None)
        if not callable(snap):
            continue
        relay_data = dict(snap())
        data["rtp_relays"][call_id] = relay_data
        data["rtp_rx_packets"] += int(relay_data.get("left_rx_packets", 0)) + int(relay_data.get("right_rx_packets", 0))
        data["rtp_rx_bytes"] += int(relay_data.get("left_rx_bytes", 0)) + int(relay_data.get("right_rx_bytes", 0))
        data["rtp_tx_packets"] += int(relay_data.get("left_tx_packets", 0)) + int(relay_data.get("right_tx_packets", 0))
        data["rtp_tx_bytes"] += int(relay_data.get("left_tx_bytes", 0)) + int(relay_data.get("right_tx_bytes", 0))
        data["rtp_dropped_packets"] += int(relay_data.get("dropped_packets", 0))
    for key, client in dict((registry.sip_clients if registry is not None else {}) or {}).items():
        snap = getattr(client, "snapshot", None)
        if not callable(snap):
            continue
        client_data = dict(snap())
        data["sip_client_dialogs"][key] = client_data
        if client_data.get("dialog_active"):
            data["active_dialogs"] += 1
            call_id = str(client_data.get("call_id") or "")
            if call_id and call_id not in data["active_call_ids"]:
                data["active_call_ids"].append(call_id)
        elif client_data.get("pending_invite"):
            data["pending_transactions"] += 1
            call_id = str(client_data.get("call_id") or "")
            if call_id and call_id not in data["pending_call_ids"]:
                data["pending_call_ids"].append(call_id)
        if client_data.get("last_sip_event"):
            data["last_sip_event"] = str(client_data.get("last_sip_event") or data["last_sip_event"])
            data["last_sip_status_code"] = int(client_data.get("last_sip_status_code") or data["last_sip_status_code"] or 0)
            data["last_sip_reason"] = str(client_data.get("last_sip_reason") or data["last_sip_reason"])
    trunk = bucket.get("sip_trunk")
    trunk_snapshot = getattr(trunk, "snapshot", None)
    if callable(trunk_snapshot):
        data["sip_trunk"] = dict(trunk_snapshot())
        if data["sip_trunk"].get("trunk_last_sip_event"):
            data["last_sip_event"] = str(data["sip_trunk"].get("trunk_last_sip_event") or data["last_sip_event"])
        if data["sip_trunk"].get("trunk_status_code"):
            data["last_sip_status_code"] = int(data["sip_trunk"].get("trunk_status_code") or 0)
            data["last_sip_reason"] = str(data["sip_trunk"].get("trunk_status_reason") or "")
    registrar = bucket.get("sip_registrar")
    registrar_snapshot = getattr(registrar, "snapshot", None)
    if callable(registrar_snapshot):
        data["sip_registrar"] = dict(registrar_snapshot())
        if data["sip_registrar"].get("registrar_last_sip_event"):
            data["last_sip_event"] = str(data["sip_registrar"].get("registrar_last_sip_event") or data["last_sip_event"])
        if data["sip_registrar"].get("registrar_last_sip_status_code"):
            data["last_sip_status_code"] = int(data["sip_registrar"].get("registrar_last_sip_status_code") or 0)
            data["last_sip_reason"] = str(data["sip_registrar"].get("registrar_last_sip_reason") or "")
    data["pending_call_ids"] = sorted(set(data["pending_call_ids"]))
    data["active_call_ids"] = sorted(set(data["active_call_ids"]))
    return data


def _ha_softphone_state(hass: HomeAssistant) -> dict[str, Any]:
    store = _ha_softphone_store(hass)
    runtime = _sip_runtime_snapshot(hass)
    debug_mode = bool(hass.data.get(DOMAIN, {}).get(CONF_DEBUG_MODE, False))
    active_softphone = store.get("state") in {
        CallState.CALLING.value,
        CallState.REMOTE_RINGING.value,
        CallState.RINGING.value,
        CallState.CONNECTING.value,
        CallState.IN_CALL.value,
        CallState.TERMINATING.value,
    }
    last_status = store.get("sip_status_code", "") or runtime["last_sip_status_code"] or ""
    last_event = store.get("last_sip_event", "") or runtime["last_sip_event"]
    caller = store.get("caller", "") or store.get("last_terminal_caller", "")
    callee = store.get("callee", "") or store.get("last_terminal_callee", "")
    connected_party = str(store.get("connected_party", "") or store.get("answered_by", ""))
    peer_name = store.get("peer_name", "") or store.get("last_terminal_peer_name", "")
    if store.get("state") == CallState.IN_CALL.value and connected_party:
        peer_name = connected_party
    dialed_target = store.get("dialed_target", "") or store.get("last_terminal_dialed_target", "")
    direction = store.get("direction", "") or store.get("last_terminal_direction", "")
    call_id = store.get("call_id", "") or store.get("last_terminal_call_id", "")
    phone = sip_phone_state(
        state=store.get("state", CallState.IDLE.value),
        call_id=call_id,
        direction=direction,
        caller=caller,
        callee=callee,
        local_uri=store.get("local_uri", ""),
        remote_uri=store.get("remote_uri", ""),
        contact=peer_name,
        sip_transport=store.get("sip_transport", "udp+tcp"),
        sip_status_code=int(last_status or 0),
        terminal_reason=store.get("terminal_reason", ""),
        selected_tx_format=store.get("selected_tx_format", ""),
        selected_rx_format=store.get("selected_rx_format", ""),
        rtp_tx_packets=_runtime_counter(store, runtime, "rtp_tx_packets"),
        rtp_rx_packets=_runtime_counter(store, runtime, "rtp_rx_packets"),
        rtp_tx_bytes=_runtime_counter(store, runtime, "rtp_tx_bytes"),
        rtp_rx_bytes=_runtime_counter(store, runtime, "rtp_rx_bytes"),
        last_sip_event=last_event,
    )
    return {
        **phone,
        "device_id": HA_SOFTPHONE_DEVICE_ID,
        "session_device_id": store.get("session_device_id", ""),
        "dnd": _ha_softphone_dnd(hass),
        "extension": _ha_softphone_extension(hass),
        "groups": _ha_softphone_groups(hass),
        "busy": bool(store.get("session_device_id") and active_softphone),
        "state": store.get("state", CallState.IDLE.value),
        "sip_state": store.get("sip_state", store.get("state", CallState.IDLE.value)),
        "caller": caller,
        "callee": callee,
        "local_name": store.get("local_name", _ha_peer_name(hass)),
        "peer_name": peer_name,
        "dialed_target": dialed_target,
        "connected_party": store.get("connected_party", ""),
        "answered_by": store.get("answered_by", ""),
        "direction": direction,
        "role": store.get("role", ""),
        "call_id": call_id,
        "target_device_id": store.get("target_device_id", ""),
        "selected_tx_format": store.get("selected_tx_format", ""),
        "selected_rx_format": store.get("selected_rx_format", ""),
        "selected_tx_rtp_format": store.get("selected_tx_rtp_format", ""),
        "selected_rx_rtp_format": store.get("selected_rx_rtp_format", ""),
        "audio_mode": store.get("audio_mode", ""),
        "sip_transport": store.get("sip_transport", "udp+tcp"),
        "sip_status_code": last_status,
        "terminal_reason": store.get("terminal_reason", ""),
        "last_sip_event": last_event,
        "last_sip_reason": store.get("last_sip_reason", "") or runtime["last_sip_reason"],
        "sip_udp_ready": runtime["sip_udp_ready"],
        "sip_tcp_ready": runtime["sip_tcp_ready"],
        "pending_transactions": runtime["pending_transactions"],
        "active_dialogs": runtime["active_dialogs"],
        "pending_call_ids": runtime["pending_call_ids"],
        "active_call_ids": runtime["active_call_ids"],
        "rtp_tx_packets": _runtime_counter(store, runtime, "rtp_tx_packets"),
        "rtp_rx_packets": _runtime_counter(store, runtime, "rtp_rx_packets"),
        "rtp_tx_bytes": _runtime_counter(store, runtime, "rtp_tx_bytes"),
        "rtp_rx_bytes": _runtime_counter(store, runtime, "rtp_rx_bytes"),
        "rtp_dropped_packets": runtime["rtp_dropped_packets"],
        "rtp_relays": runtime["rtp_relays"],
        "sip_client_dialogs": runtime["sip_client_dialogs"],
        "sip_trunk": runtime["sip_trunk"],
        "debug_mode": debug_mode,
        "media_debug": dict(store.get("media_debug") or {}) if debug_mode else {},
    }


def _set_ha_softphone_call_state(
    hass: HomeAssistant,
    state: str,
    *,
    session_device_id: str = "",
    caller: str = "",
    callee: str = "",
    peer_name: str = "",
    direction: str = "",
    call_id: str = "",
    **extra: Any,
) -> None:
    store = _ha_softphone_store(hass)
    state = _sip_public_state(state)
    local_name = str(extra.pop("local_name", _ha_peer_name(hass)))
    terminal = state in {
        CallState.IDLE.value,
        CallState.BUSY.value,
        CallState.DECLINED.value,
        CallState.CANCELLED.value,
        CallState.MEDIA_INCOMPATIBLE.value,
        CallState.TRANSPORT_UNREACHABLE.value,
        CallState.AUTH_REQUIRED_UNSUPPORTED.value,
        "error",
    }
    canonical = _canonical_session_fields(
        state=state,
        local_name=local_name,
        peer_name=peer_name,
        caller=caller or str(store.get("caller", "")),
        callee=callee or str(store.get("callee", "")),
        direction=direction or str(store.get("direction", "")),
        call_id=call_id or str(store.get("call_id", "")),
    )
    previous_call_id = str(store.get("call_id") or "")
    next_call_id = str(canonical.get("call_id") or "")
    previous_state = str(store.get("state") or "").strip().lower()
    if (
        previous_call_id
        and next_call_id
        and next_call_id != previous_call_id
        and previous_state
        in {
            CallState.CALLING.value,
            CallState.REMOTE_RINGING.value,
            CallState.RINGING.value,
            CallState.CONNECTING.value,
            CallState.IN_CALL.value,
            CallState.TERMINATING.value,
        }
    ):
        _LOGGER.info(
            "Ignoring stale HA softphone state=%s call_id=%s; active call_id=%s state=%s",
            state,
            next_call_id,
            previous_call_id,
            previous_state,
        )
        return
    if terminal:
        store["terminal_reason"] = extra.get("reason") or extra.get("terminal_reason") or state
        store["sip_status_code"] = extra.get("code") or extra.get("sip_status_code") or store.get("sip_status_code", "")
        store["last_terminal_call_id"] = canonical.get("call_id", "")
        store["last_terminal_direction"] = canonical.get("direction", "")
        store["last_terminal_caller"] = canonical.get("caller", "")
        store["last_terminal_callee"] = canonical.get("callee", "")
        store["last_terminal_peer_name"] = canonical.get("peer_name", "")
        store["last_terminal_dialed_target"] = (
            extra.get("dialed_target")
            or store.get("dialed_target")
            or canonical.get("callee", "")
        )
        if extra.get("last_sip_event"):
            store["last_sip_event"] = extra["last_sip_event"]
        if extra.get("last_sip_reason"):
            store["last_sip_reason"] = extra["last_sip_reason"]
        for key in (
            "session_device_id",
            "caller",
            "callee",
            "local_name",
            "peer_name",
            "dialed_target",
            "connected_party",
            "answered_by",
            "direction",
            "role",
            "call_id",
            "target_device_id",
            "selected_tx_format",
            "selected_rx_format",
            "selected_tx_rtp_format",
            "selected_rx_rtp_format",
            "audio_mode",
            "route_kind",
            "sip_uri",
            "media_debug",
        ):
            store.pop(key, None)
        store["state"] = state
        store["sip_state"] = state
    else:
        if next_call_id and next_call_id != previous_call_id:
            for key in _MEDIA_COUNTER_KEYS:
                store[key] = 0
            store["media_debug"] = {}
        for key in (
            "last_terminal_call_id",
            "last_terminal_direction",
            "last_terminal_caller",
            "last_terminal_callee",
            "last_terminal_peer_name",
            "last_terminal_dialed_target",
        ):
            store.pop(key, None)
        store.update(canonical)
        store["session_device_id"] = session_device_id
        store["state"] = state
        store["sip_state"] = state
        store["terminal_reason"] = ""
        for key, value in extra.items():
            if value not in (None, ""):
                store[key] = value

    payload = {"device_id": HA_SOFTPHONE_DEVICE_ID, "session_device_id": session_device_id or ""}
    payload.update(canonical)
    payload.update({k: v for k, v in extra.items() if v not in (None, "")})
    if terminal and store.get("terminal_reason"):
        payload["terminal_reason"] = store["terminal_reason"]
    if terminal and store.get("last_terminal_dialed_target"):
        payload["dialed_target"] = store["last_terminal_dialed_target"]
    _LOGGER.info(
        "HA softphone state=%s call_id=%s direction=%s caller=%s callee=%s peer=%s reason=%s event=%s",
        state,
        canonical.get("call_id", ""),
        canonical.get("direction", ""),
        canonical.get("caller", ""),
        canonical.get("callee", ""),
        canonical.get("peer_name", ""),
        store.get("terminal_reason", "") if terminal else "",
        extra.get("last_sip_event", store.get("last_sip_event", "")),
    )
    _fire_call_event(hass, payload, "session")


def _set_sip_bridge_call_state(
    hass: HomeAssistant,
    state: str,
    *,
    call_id: str = "",
    dest_call_id: str = "",
    caller: str = "",
    callee: str = "",
    peer_name: str = "",
    target: str = "",
    **extra: Any,
) -> None:
    """Publish SIP bridge/B2BUA state without mutating the HA softphone session."""
    store = _sip_bridge_store(hass)
    state = _sip_public_state(state)
    terminal = state in {
        CallState.IDLE.value,
        CallState.BUSY.value,
        CallState.DECLINED.value,
        CallState.CANCELLED.value,
        CallState.MEDIA_INCOMPATIBLE.value,
        CallState.TRANSPORT_UNREACHABLE.value,
        CallState.AUTH_REQUIRED_UNSUPPORTED.value,
        "error",
    }
    payload = {
        "state": state,
        "sip_state": state,
        "call_id": call_id,
        "dest_call_id": dest_call_id,
        "caller": caller,
        "callee": callee,
        "peer_name": peer_name or target or callee,
        "target": target or callee,
        "terminal_reason": extra.get("terminal_reason") or extra.get("reason") or "",
    }
    payload.update({k: v for k, v in extra.items() if v not in (None, "")})
    store.update(payload)
    if terminal:
        store["last_terminal_call_id"] = call_id
        store["last_terminal_dest_call_id"] = dest_call_id
    _LOGGER.info(
        "SIP bridge state=%s call_id=%s dest_call_id=%s caller=%s callee=%s target=%s reason=%s event=%s",
        state,
        call_id,
        dest_call_id,
        caller,
        callee,
        target,
        payload.get("terminal_reason", ""),
        payload.get("last_sip_event", ""),
    )
    _fire_call_event(hass, payload, "sip_bridge")


def _ha_softphone_device(hass: HomeAssistant) -> dict[str, Any]:
    state = _ha_softphone_state(hass)
    return {
        "device_id": HA_SOFTPHONE_DEVICE_ID,
        "name": _ha_peer_name(hass),
        "host": "",
        "sip_transport": "udp+tcp",
        "softphone": True,
        "ha_softphone": state,
    }


async def _get_voip_devices(hass: HomeAssistant) -> list[dict[str, Any]]:
    from .device_resolver import get_resolver

    return await get_resolver(hass).list_devices()


def async_register_websocket_api(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, websocket_subscribe_call_events)
    websocket_api.async_register_command(hass, websocket_ha_softphone_start)
    websocket_api.async_register_command(hass, websocket_ha_softphone_state)
    websocket_api.async_register_command(hass, websocket_list_devices)
    websocket_api.async_register_command(hass, websocket_resolve_device)


@websocket_api.websocket_command({vol.Required("type"): WS_TYPE_SUBSCRIBE_CALL_EVENTS})
@callback
def websocket_subscribe_call_events(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: Dict[str, Any],
) -> None:
    msg_id = msg["id"]

    @callback
    def forward_call_event(event) -> None:
        connection.send_event(msg_id, {"event_type": CALL_EVENT, "data": event.data})

    connection.subscriptions[msg_id] = hass.bus.async_listen(CALL_EVENT, forward_call_event)
    connection.send_result(msg_id)


@websocket_api.websocket_command(
    {
        vol.Required("type"): WS_TYPE_HA_SOFTPHONE_START,
        vol.Optional("target_device_id", default=""): str,
        vol.Optional("target_name", default=""): str,
        vol.Optional("callee", default=""): str,
        vol.Optional("call_id", default=""): str,
    }
)
@websocket_api.async_response
async def websocket_ha_softphone_start(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: Dict[str, Any],
) -> None:
    selector = str(msg.get("target_name") or msg.get("callee") or msg.get("target_device_id") or "").strip()
    if not selector:
        connection.send_error(msg["id"], "target_required", "SIP target is required")
        return
    try:
        await hass.services.async_call(DOMAIN, "call", {"target": selector, "call": selector}, blocking=True)
    except Exception as err:
        connection.send_error(msg["id"], "sip_call_failed", str(err))
        return
    connection.send_result(msg["id"], {"success": True, **_ha_softphone_state(hass)})


@websocket_api.websocket_command({vol.Required("type"): WS_TYPE_HA_SOFTPHONE_STATE})
@websocket_api.async_response
async def websocket_ha_softphone_state(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: Dict[str, Any],
) -> None:
    connection.send_result(msg["id"], _ha_softphone_state(hass))


@websocket_api.websocket_command({vol.Required("type"): WS_TYPE_LIST})
@websocket_api.async_response
async def websocket_list_devices(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: Dict[str, Any],
) -> None:
    connection.send_result(msg["id"], {"devices": [_ha_softphone_device(hass), *(await _get_voip_devices(hass))]})


@websocket_api.websocket_command(
    {vol.Required("type"): WS_TYPE_RESOLVE_DEVICE, vol.Required("device_id"): str}
)
@websocket_api.async_response
async def websocket_resolve_device(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: Dict[str, Any],
) -> None:
    device_id = str(msg.get("device_id") or "")
    if device_id == HA_SOFTPHONE_DEVICE_ID:
        connection.send_result(msg["id"], {"device": _ha_softphone_device(hass)})
        return
    for device in await _get_voip_devices(hass):
        if str(device.get("device_id") or "") == device_id:
            connection.send_result(msg["id"], {"device": device})
            return
    connection.send_result(msg["id"], {"device": None})
