"""SIP call-state vocabulary shared by HA sessions and bridges."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum


class CallState(StrEnum):
    IDLE = "idle"
    CALLING = "calling"
    REMOTE_RINGING = "remote_ringing"
    RINGING = "ringing"
    CONNECTING = "connecting"
    IN_CALL = "in_call"
    TERMINATING = "terminating"
    BUSY = "busy"
    DECLINED = "declined"
    CANCELLED = "cancelled"
    MEDIA_INCOMPATIBLE = "media_incompatible"
    TRANSPORT_UNREACHABLE = "transport_unreachable"
    AUTH_REQUIRED_UNSUPPORTED = "auth_required_unsupported"


class TerminalReason(StrEnum):
    LOCAL_HANGUP = "local_hangup"
    REMOTE_HANGUP = "remote_hangup"
    DECLINED = "declined"
    CANCELLED = "cancelled"
    TIMEOUT = "timeout"
    BUSY = "busy"
    TRANSPORT_UNREACHABLE = "transport_unreachable"
    MEDIA_INCOMPATIBLE = "media_incompatible"
    AUTH_REQUIRED_UNSUPPORTED = "auth_required_unsupported"
    PROXY_AUTH_REQUIRED_UNSUPPORTED = "proxy_auth_required_unsupported"
    PROTOCOL_ERROR = "protocol_error"


@dataclass(slots=True)
class SipPhoneState:
    """Public SIP phone state exposed by HA and mirrored by ESP snapshots."""

    state: str = CallState.IDLE.value
    call_id: str = ""
    direction: str = ""
    caller: str = ""
    callee: str = ""
    local_uri: str = ""
    remote_uri: str = ""
    contact: str = ""
    sip_transport: str = "udp"
    sip_status_code: int = 0
    terminal_reason: str = ""
    selected_tx_format: str = ""
    selected_rx_format: str = ""
    rtp_tx_packets: int = 0
    rtp_rx_packets: int = 0
    rtp_tx_bytes: int = 0
    rtp_rx_bytes: int = 0
    last_sip_event: str = ""

    def as_dict(self) -> dict[str, str | int]:
        return asdict(self)


def sip_phone_state(**values: object) -> dict[str, str | int]:
    """Return a complete SipPhoneState dict, ignoring unknown keys."""

    allowed = SipPhoneState.__dataclass_fields__
    clean = {key: value for key, value in values.items() if key in allowed}
    return SipPhoneState(**clean).as_dict()


def terminal_state_for_decline(reason: str) -> str:
    """Map SIP final/terminal reasons to public SIP call states."""
    reason = (reason or "").strip()
    if not reason:
        return CallState.IDLE.value
    if reason == TerminalReason.BUSY.value:
        return CallState.BUSY.value
    if reason == TerminalReason.CANCELLED.value:
        return CallState.CANCELLED.value
    if reason == TerminalReason.MEDIA_INCOMPATIBLE.value:
        return CallState.MEDIA_INCOMPATIBLE.value
    if reason in (
        TerminalReason.AUTH_REQUIRED_UNSUPPORTED.value,
        TerminalReason.PROXY_AUTH_REQUIRED_UNSUPPORTED.value,
    ):
        return CallState.AUTH_REQUIRED_UNSUPPORTED.value
    if reason == TerminalReason.TRANSPORT_UNREACHABLE.value:
        return CallState.TRANSPORT_UNREACHABLE.value
    return CallState.DECLINED.value


def terminal_reason_for_decline(reason: str) -> str:
    """Normalize SIP terminal reason for HA events."""
    return reason or TerminalReason.REMOTE_HANGUP.value


def is_hangup_reason(reason: str) -> bool:
    return reason in (
        TerminalReason.LOCAL_HANGUP.value,
        TerminalReason.REMOTE_HANGUP.value,
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
