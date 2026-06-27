"""SIP-only WebSocket API for Intercom Native."""

from __future__ import annotations

from typing import Any, Dict

import voluptuous as vol

from homeassistant.components import websocket_api
from homeassistant.core import HomeAssistant, callback

from .const import DOMAIN, HA_PEER_FALLBACK_NAME, HA_SOFTPHONE_DEVICE_ID
from .fsm import CallState, TerminalReason, sip_phone_state

CALL_EVENT = "intercom_native.call_event"
SIP_CALL_STATE_EVENT = "intercom_native.sip_call_state"
SIP_INCOMING_CALL_EVENT = "intercom_native.sip_incoming_call"
SIP_ROUTE_REQUEST_EVENT = "intercom_native.sip_route_request"
SIP_CALL_ENDED_EVENT = "intercom_native.sip_call_ended"
HA_SOFTPHONE_STORE_KEY = f"{DOMAIN}_ha_softphone"
HA_SOFTPHONE_STORE_VERSION = 1

WS_TYPE_LIST = f"{DOMAIN}/list_devices"
WS_TYPE_RESOLVE_DEVICE = f"{DOMAIN}/resolve_device"
WS_TYPE_HA_SOFTPHONE_START = f"{DOMAIN}/ha_softphone_start"
WS_TYPE_HA_SOFTPHONE_STATE = f"{DOMAIN}/ha_softphone_state"
WS_TYPE_SET_HA_SOFTPHONE_DND = f"{DOMAIN}/set_ha_softphone_dnd"
WS_TYPE_SUBSCRIBE_CALL_EVENTS = f"{DOMAIN}/subscribe_call_events"

# Kept only as inert compatibility imports for __init__.py while the SIP
# endpoint owns all real dialog/session state.
_sessions: dict[str, object] = {}
_bridges: dict[str, object] = {}


def _session_pop(_selector: str) -> None:
    return None


def _ha_peer_name(hass: HomeAssistant) -> str:
    return (hass.config.location_name or "").strip() or HA_PEER_FALLBACK_NAME


def _sip_public_state(state: str) -> str:
    value = (state or "").strip().lower()
    mapping = {
        "": CallState.IDLE.value,
        "idle": CallState.IDLE.value,
        "calling": CallState.CALLING.value,
        "ringing": CallState.RINGING.value,
        "remote_ringing": CallState.REMOTE_RINGING.value,
        "connecting": CallState.CONNECTING.value,
        "in_call": CallState.IN_CALL.value,
        "terminating": CallState.TERMINATING.value,
        "busy": CallState.BUSY.value,
        "declined": CallState.DECLINED.value,
        "cancelled": CallState.CANCELLED.value,
        "media_incompatible": CallState.MEDIA_INCOMPATIBLE.value,
        "transport_unreachable": CallState.TRANSPORT_UNREACHABLE.value,
        "auth_required_unsupported": CallState.AUTH_REQUIRED_UNSUPPORTED.value,
        "proxy_auth_required_unsupported": CallState.AUTH_REQUIRED_UNSUPPORTED.value,
    }
    return mapping.get(value, value or CallState.IDLE.value)


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


def _fire_call_event(hass: HomeAssistant, payload: dict[str, Any], scope: str) -> None:
    event = dict(payload)
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


async def _async_load_ha_softphone_store(hass: HomeAssistant) -> None:
    from homeassistant.helpers.storage import Store

    store = Store(hass, HA_SOFTPHONE_STORE_VERSION, HA_SOFTPHONE_STORE_KEY)
    data = await store.async_load() or {}
    runtime = _ha_softphone_store(hass)
    runtime["storage"] = store
    runtime["dnd"] = bool(data.get("dnd", runtime.get("dnd", False)))


async def _async_save_ha_softphone_store(hass: HomeAssistant) -> None:
    runtime = _ha_softphone_store(hass)
    store = runtime.get("storage")
    if store is not None:
        await store.async_save({"dnd": bool(runtime.get("dnd", False))})


def _ha_softphone_dnd(hass: HomeAssistant) -> bool:
    return bool(_ha_softphone_store(hass).get("dnd"))


def _sip_runtime_snapshot(hass: HomeAssistant) -> dict[str, Any]:
    bucket = hass.data.get(DOMAIN, {})
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
    for call_id, relay in dict(bucket.get("sip_relays", {}) or {}).items():
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
    for key, client in dict(bucket.get("sip_clients", {}) or {}).items():
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
    data["pending_call_ids"] = sorted(set(data["pending_call_ids"]))
    data["active_call_ids"] = sorted(set(data["active_call_ids"]))
    return data


def _ha_softphone_state(hass: HomeAssistant) -> dict[str, Any]:
    store = _ha_softphone_store(hass)
    runtime = _sip_runtime_snapshot(hass)
    last_status = store.get("sip_status_code", "") or runtime["last_sip_status_code"] or ""
    last_event = store.get("last_sip_event", "") or runtime["last_sip_event"]
    caller = store.get("caller", "") or store.get("last_terminal_caller", "")
    callee = store.get("callee", "") or store.get("last_terminal_callee", "")
    peer_name = store.get("peer_name", "") or store.get("last_terminal_peer_name", "")
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
        rtp_tx_packets=int(store.get("rtp_tx_packets", runtime["rtp_tx_packets"]) or runtime["rtp_tx_packets"] or 0),
        rtp_rx_packets=int(store.get("rtp_rx_packets", runtime["rtp_rx_packets"]) or runtime["rtp_rx_packets"] or 0),
        rtp_tx_bytes=int(store.get("rtp_tx_bytes", runtime["rtp_tx_bytes"]) or runtime["rtp_tx_bytes"] or 0),
        rtp_rx_bytes=int(store.get("rtp_rx_bytes", runtime["rtp_rx_bytes"]) or runtime["rtp_rx_bytes"] or 0),
        last_sip_event=last_event,
    )
    return {
        **phone,
        "device_id": HA_SOFTPHONE_DEVICE_ID,
        "session_device_id": store.get("session_device_id", ""),
        "dnd": _ha_softphone_dnd(hass),
        "busy": bool(store.get("session_device_id") or runtime["pending_transactions"] or runtime["active_dialogs"]),
        "state": store.get("state", CallState.IDLE.value),
        "sip_state": store.get("sip_state", store.get("state", CallState.IDLE.value)),
        "caller": caller,
        "callee": callee,
        "local_name": store.get("local_name", _ha_peer_name(hass)),
        "peer_name": peer_name,
        "direction": direction,
        "role": store.get("role", ""),
        "call_id": call_id,
        "target_device_id": store.get("target_device_id", ""),
        "selected_tx_format": store.get("selected_tx_format", ""),
        "selected_rx_format": store.get("selected_rx_format", ""),
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
        "rtp_tx_packets": int(store.get("rtp_tx_packets", runtime["rtp_tx_packets"]) or runtime["rtp_tx_packets"] or 0),
        "rtp_rx_packets": int(store.get("rtp_rx_packets", runtime["rtp_rx_packets"]) or runtime["rtp_rx_packets"] or 0),
        "rtp_tx_bytes": int(store.get("rtp_tx_bytes", runtime["rtp_tx_bytes"]) or runtime["rtp_tx_bytes"] or 0),
        "rtp_rx_bytes": int(store.get("rtp_rx_bytes", runtime["rtp_rx_bytes"]) or runtime["rtp_rx_bytes"] or 0),
        "rtp_dropped_packets": runtime["rtp_dropped_packets"],
        "rtp_relays": runtime["rtp_relays"],
        "sip_client_dialogs": runtime["sip_client_dialogs"],
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
    if terminal:
        store["terminal_reason"] = extra.get("reason") or extra.get("terminal_reason") or state
        store["sip_status_code"] = extra.get("code") or extra.get("sip_status_code") or store.get("sip_status_code", "")
        store["last_terminal_call_id"] = canonical.get("call_id", "")
        store["last_terminal_direction"] = canonical.get("direction", "")
        store["last_terminal_caller"] = canonical.get("caller", "")
        store["last_terminal_callee"] = canonical.get("callee", "")
        store["last_terminal_peer_name"] = canonical.get("peer_name", "")
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
            "direction",
            "role",
            "call_id",
            "target_device_id",
            "selected_tx_format",
            "selected_rx_format",
            "audio_mode",
            "route_kind",
            "sip_uri",
        ):
            store.pop(key, None)
        store["state"] = state
        store["sip_state"] = state
    else:
        for key in (
            "last_terminal_call_id",
            "last_terminal_direction",
            "last_terminal_caller",
            "last_terminal_callee",
            "last_terminal_peer_name",
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
    _fire_call_event(hass, payload, "session")


def _ha_softphone_device(hass: HomeAssistant) -> dict[str, Any]:
    state = _ha_softphone_state(hass)
    return {
        "device_id": HA_SOFTPHONE_DEVICE_ID,
        "name": _ha_peer_name(hass),
        "host": "",
        "transport": "sip",
        "sip_transport": "udp+tcp",
        "softphone": True,
        "ha_softphone": state,
    }


async def _get_intercom_devices(hass: HomeAssistant) -> list[dict[str, Any]]:
    from .device_resolver import get_resolver

    return await get_resolver(hass).async_devices()


def async_register_websocket_api(hass: HomeAssistant) -> None:
    websocket_api.async_register_command(hass, websocket_subscribe_call_events)
    websocket_api.async_register_command(hass, websocket_ha_softphone_start)
    websocket_api.async_register_command(hass, websocket_ha_softphone_state)
    websocket_api.async_register_command(hass, websocket_set_ha_softphone_dnd)
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
    call_id = str(msg.get("call_id") or "")
    if not selector:
        connection.send_error(msg["id"], "target_required", "SIP target is required")
        return
    try:
        await hass.services.async_call(DOMAIN, "sip_call", {"target": selector, "call": selector}, blocking=True)
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


@websocket_api.websocket_command(
    {vol.Required("type"): WS_TYPE_SET_HA_SOFTPHONE_DND, vol.Required("dnd"): bool}
)
@websocket_api.async_response
async def websocket_set_ha_softphone_dnd(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: Dict[str, Any],
) -> None:
    _ha_softphone_store(hass)["dnd"] = bool(msg["dnd"])
    await _async_save_ha_softphone_store(hass)
    state = _ha_softphone_state(hass)
    _fire_call_event(hass, state, "session")
    connection.send_result(msg["id"], state)


@websocket_api.websocket_command({vol.Required("type"): WS_TYPE_LIST})
@websocket_api.async_response
async def websocket_list_devices(
    hass: HomeAssistant,
    connection: websocket_api.ActiveConnection,
    msg: Dict[str, Any],
) -> None:
    connection.send_result(msg["id"], {"devices": [_ha_softphone_device(hass), *(await _get_intercom_devices(hass))]})


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
    for device in await _get_intercom_devices(hass):
        if str(device.get("device_id") or "") == device_id:
            connection.send_result(msg["id"], {"device": device})
            return
    connection.send_result(msg["id"], {"device": None})
