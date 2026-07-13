"""Pending inbound SIP route decision handling."""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError

from .const import HA_SOFTPHONE_DEVICE_ID
from .endpoint_lifecycle import call_registry
from .fsm import CallState, TerminalReason
from .websocket_api import _set_ha_softphone_call_state, _set_sip_bridge_call_state

_LOGGER = logging.getLogger(__name__)


def set_pending_route_decision(hass: HomeAssistant, data: dict) -> None:
    """Apply an automation dial-plan decision to a pending inbound SIP route."""
    call_id = str(data.get("call_id") or "").strip()
    if not call_id:
        raise ServiceValidationError("call_id is required")
    action = str(data.get("action") or "default").strip().lower()
    destination = str(
        data.get("destination") or data.get("target") or data.get("call") or ""
    ).strip()
    if action in {"forward", "bridge"} and not destination:
        raise ServiceValidationError(f"{action} requires destination, target, or call")
    registry = call_registry(hass)
    route = registry.pending_routes.get(call_id)
    if route is None:
        raise ServiceValidationError(f"no pending SIP route for call_id {call_id}")
    context = registry.event_context(call_id)
    expected_state = str(data.get("expected_state") or "").strip().lower()
    expected_sequence = int(data.get("expected_sequence") or 0)
    if expected_state and (context is None or context.state != expected_state):
        actual = context.state if context is not None else "ended"
        raise ServiceValidationError(
            f"call_id {call_id} is {actual}, expected {expected_state}"
        )
    if expected_sequence and (
        context is None or context.sequence != expected_sequence
    ):
        actual = context.sequence if context is not None else 0
        raise ServiceValidationError(
            f"call_id {call_id} sequence is {actual}, expected {expected_sequence}"
        )
    if action in {"forward", "bridge"} and context is not None and len(context.route_history) >= 8:
        raise ServiceValidationError(f"call_id {call_id} exceeded 8 routing hops")
    future = route.get("future")
    if future is None or future.done():
        raise ServiceValidationError(f"SIP route for call_id {call_id} is no longer decidable")
    registry.record_route(
        call_id,
        action=action,
        destination=destination,
        source="automation",
    )
    if action in {"forward", "bridge"}:
        session = registry.sessions.get(registry.resolve_session_id(call_id))
        if session is not None:
            registry.transition(
                call_id,
                state=CallState.CONNECTING.value,
                owner="router",
                callee=destination,
                expected_revision=session.revision,
                expected_owner=session.owner,
            )
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
            direction="incoming",
            selected_tx_format=invite.send_format.audio_format.wire_token(),
            selected_rx_format=invite.recv_format.audio_format.wire_token(),
            selected_tx_rtp_format=invite.send_format.wire_token(),
            selected_rx_rtp_format=invite.recv_format.wire_token(),
            audio_mode="full_duplex",
            sip_status_code=180,
            event_type="forwarding",
            last_sip_event="SIP_RESPONSE",
        )
    _LOGGER.info(
        "SIP route decision call_id=%s action=%s destination=%s",
        call_id,
        action,
        destination or "-",
    )
