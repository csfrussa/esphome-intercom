"""Pure automation-facing routing primitives."""

from __future__ import annotations

from typing import Any


AUTOMATION_EVENT_TYPES = [
    "incoming_call",
    "outgoing_call",
    "calling",
    "ringing",
    "calling_timeout_requested",
    "ringing_timeout_requested",
    "answered",
    "connected",
    "dtmf",
    "ended",
    "missed",
    "failed",
    "state_changed",
]


def automation_event_type(payload: dict[str, Any]) -> str:
    """Map the stable call envelope to one browsable HA event type."""
    explicit = str(payload.get("event_type") or "").strip().lower()
    if explicit in AUTOMATION_EVENT_TYPES:
        return explicit
    state = str(payload.get("state") or "").strip().lower()
    legacy_type = str(payload.get("type") or "").strip().lower()
    if state == "route_requested":
        return "incoming_call" if payload.get("direction") == "incoming" else "calling"
    if state == "calling":
        return "outgoing_call"
    if state == "connecting":
        return "incoming_call" if payload.get("direction") == "incoming" else "calling"
    if state in {"ringing", "remote_ringing"}:
        return "ringing"
    if state == "in_call":
        return "connected" if payload.get("direction") == "outgoing" else "answered"
    if state in {"calling_timeout_requested", "ringing_timeout_requested"}:
        return state
    if legacy_type in {"ended", "missed", "failed"}:
        return legacy_type
    return "state_changed"


def deadline_is_current(
    current_state: str,
    current_sequence: int,
    *,
    armed_state: str,
    armed_sequence: int,
) -> bool:
    """Return whether a deadline still belongs to the same call phase."""
    return (
        str(current_state or "") == str(armed_state or "")
        and int(current_sequence) == int(armed_sequence)
    )
