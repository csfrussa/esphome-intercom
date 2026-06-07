"""PBX-lite FSM vocabulary shared by HA sessions and bridges.

This mirrors the ESP-side `intercom_fsm.h` state/reason vocabulary. The enum
names are implementation detail; the string values are the wire/UI contract.
"""

from __future__ import annotations

from enum import StrEnum


class CallState(StrEnum):
    IDLE = "idle"
    OUTGOING = "outgoing"
    RINGING = "ringing"
    STREAMING = "streaming"


class TerminalReason(StrEnum):
    LOCAL_HANGUP = "local_hangup"
    REMOTE_HANGUP = "remote_hangup"
    REMOTE_DEVICE_LOST = "remote_device_lost"
    DECLINED = "declined"
    TIMEOUT = "timeout"
    BUSY = "busy"
    UNREACHABLE = "unreachable"
    PROTOCOL_ERROR = "protocol_error"
    BRIDGE_ERROR = "bridge_error"
    DND = "DND"


def terminal_state_for_decline(reason: str) -> str:
    """Empty DECLINE is normal remote hangup; non-empty is declined."""
    return "declined" if reason else "idle"


def terminal_reason_for_decline(reason: str) -> str:
    """Normalize DECLINE reason for HA events."""
    return reason or TerminalReason.REMOTE_HANGUP.value


def is_hangup_reason(reason: str) -> bool:
    return reason in (
        TerminalReason.LOCAL_HANGUP.value,
        TerminalReason.REMOTE_HANGUP.value,
        TerminalReason.REMOTE_DEVICE_LOST.value,
    )


def localize_bridge_reason(
    role: str,
    reason: str | None,
    origin: str | None,
) -> str | None:
    """Translate a bridge terminal reason into one device's perspective."""
    if origin not in ("source", "dest"):
        return reason
    if reason in (
        TerminalReason.LOCAL_HANGUP.value,
        TerminalReason.REMOTE_HANGUP.value,
    ):
        return (
            TerminalReason.LOCAL_HANGUP.value
            if role == origin
            else TerminalReason.REMOTE_HANGUP.value
        )
    return reason
