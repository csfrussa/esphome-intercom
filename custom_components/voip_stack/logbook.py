"""Human-readable Logbook entries for completed VoIP calls."""

from __future__ import annotations

from collections.abc import Callable, Mapping
import math
from typing import Any

from homeassistant.components.logbook import (
    LOGBOOK_ENTRY_ENTITY_ID,
    LOGBOOK_ENTRY_ICON,
    LOGBOOK_ENTRY_MESSAGE,
    LOGBOOK_ENTRY_NAME,
)
from homeassistant.core import Event, HomeAssistant, callback

from .const import DOMAIN, SIP_CALL_ENDED_EVENT

CALL_EVENT_ENTITY_ID = "event.voip_stack_call"


def _party(data: Mapping[str, Any], *keys: str) -> str:
    """Return the first useful human-facing party label."""

    for key in keys:
        value = str(data.get(key) or "").strip()
        if not value:
            continue
        if value.lower().startswith("sip:"):
            value = value[4:].split(";", 1)[0]
        return value
    return ""


def _format_duration(value: Any) -> str:
    """Format a recorded call duration without locale-dependent ambiguity."""

    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return ""
    if not math.isfinite(numeric) or numeric < 0:
        return ""
    seconds = round(numeric)
    hours, remainder = divmod(seconds, 3600)
    minutes, seconds = divmod(remainder, 60)
    if hours:
        return f"{hours} h {minutes:02d} min {seconds:02d} s"
    if minutes:
        return f"{minutes} min {seconds:02d} s"
    return f"{seconds} s"


def _route_destination(data: Mapping[str, Any]) -> str:
    """Return the last explicit PBX destination selected for the call."""

    history = data.get("route_history")
    if not isinstance(history, list):
        return ""
    for item in reversed(history):
        if not isinstance(item, Mapping):
            continue
        destination = str(item.get("destination") or "").strip()
        if destination:
            return destination
    return ""


def _describe_call(data: Mapping[str, Any]) -> dict[str, str]:
    """Build one durable summary from the terminal event payload."""

    caller = _party(data, "caller", "peer_name") or "Unknown caller"
    destination_keys = (
        ("answered_by", "connected_party", "local_name", "callee", "dialed_target")
        if str(data.get("direction") or "").strip().lower() == "incoming"
        else ("answered_by", "connected_party", "callee", "dialed_target", "local_name")
    )
    route_destination = (
        _route_destination(data)
        if str(data.get("direction") or "").strip().lower() == "incoming"
        else ""
    )
    destination = route_destination or _party(data, *destination_keys)
    destination = destination or "unknown destination"
    event_type = str(data.get("type") or "ended").strip().lower()
    duration = _format_duration(data.get("duration_seconds"))

    if event_type == "missed":
        message = f"Missed call from {caller} to {destination}"
    elif event_type == "failed":
        reason = str(
            data.get("terminal_reason") or data.get("reason") or "call failed"
        ).strip()
        message = f"Call from {caller} to {destination} failed"
        if reason:
            message += f" · {reason.replace('_', ' ')}"
    else:
        message = f"{caller} called {destination}"
        if duration:
            message += f" · {duration}"

    return {
        LOGBOOK_ENTRY_NAME: "VoIP Stack",
        LOGBOOK_ENTRY_MESSAGE: message,
        LOGBOOK_ENTRY_ENTITY_ID: CALL_EVENT_ENTITY_ID,
        LOGBOOK_ENTRY_ICON: "mdi:phone-log",
    }


@callback
def async_describe_events(
    hass: HomeAssistant,
    async_describe_event: Callable[
        [str, str, Callable[[Event[dict[str, Any]]], dict[str, str]]], None
    ],
) -> None:
    """Describe completed calls without recording every signaling transition."""

    @callback
    def async_describe_call_event(
        event: Event[dict[str, Any]],
    ) -> dict[str, str]:
        return _describe_call(event.data)

    async_describe_event(DOMAIN, SIP_CALL_ENDED_EVENT, async_describe_call_event)
