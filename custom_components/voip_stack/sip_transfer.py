"""RFC 3515 REFER/NOTIFY message semantics without transport ownership."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import re
from urllib.parse import quote

from . import sip


SIPFRAG_CONTENT_TYPE = "message/sipfrag"
REFER_EVENT = "refer"
_SIPFRAG_STATUS = re.compile(r"^SIP/2\.0\s+([1-6][0-9]{2})(?:\s+.*)?$", re.IGNORECASE)


class SubscriptionPhase(StrEnum):
    ACTIVE = "active"
    PENDING = "pending"
    TERMINATED = "terminated"


@dataclass(frozen=True, slots=True)
class ReferNotify:
    status: int
    phase: SubscriptionPhase
    reason: str = ""
    retry_after: int | None = None

    @property
    def final(self) -> bool:
        return self.phase is SubscriptionPhase.TERMINATED


def build_refer_to(target_uri: str, *, replaces: str = "") -> str:
    """Build a name-address Refer-To, encoding an attended Replaces value."""

    target = str(sip.parse_sip_uri(str(target_uri or "").strip()))
    replacement = str(replaces or "").strip()
    if not replacement:
        return f"<{target}>"
    if any(ord(char) < 0x21 for char in replacement):
        raise ValueError("Replaces must not contain whitespace or control characters")
    separator = "&" if "?" in target else "?"
    return f"<{target}{separator}Replaces={quote(replacement, safe='')}>"


def refer_headers(
    target_uri: str,
    *,
    replaces: str = "",
    referred_by: str = "",
) -> tuple[tuple[str, str], ...]:
    headers: list[tuple[str, str]] = [
        ("Refer-To", build_refer_to(target_uri, replaces=replaces)),
        ("Event", REFER_EVENT),
    ]
    if referred_by:
        headers.append(("Referred-By", f"<{sip.parse_sip_uri(referred_by)}>"))
    return tuple(headers)


def _parse_subscription_state(value: str) -> tuple[SubscriptionPhase, str, int | None]:
    parts = [part.strip() for part in str(value or "").split(";") if part.strip()]
    if not parts:
        raise ValueError("NOTIFY missing Subscription-State")
    try:
        phase = SubscriptionPhase(parts[0].lower())
    except ValueError as err:
        raise ValueError("invalid REFER Subscription-State") from err
    reason = ""
    retry_after: int | None = None
    for parameter in parts[1:]:
        name, separator, raw = parameter.partition("=")
        if not separator:
            continue
        if name.lower() == "reason":
            reason = raw
        elif name.lower() == "retry-after":
            retry_after = int(raw)
            if retry_after < 0:
                raise ValueError("invalid REFER retry-after")
    return phase, reason, retry_after


def parse_refer_notify(message: sip.SipMessage) -> ReferNotify:
    """Validate a REFER NOTIFY and return its sipfrag progress status."""

    if message.is_response or message.method != "NOTIFY":
        raise ValueError("expected NOTIFY request")
    if message.header("Event").split(";", 1)[0].strip().lower() != REFER_EVENT:
        raise ValueError("NOTIFY is not for the refer event")
    content_type = message.header("Content-Type").split(";", 1)[0].strip().lower()
    if content_type != SIPFRAG_CONTENT_TYPE:
        raise ValueError("REFER NOTIFY requires message/sipfrag")
    try:
        status_line = message.body.decode("utf-8", errors="strict").splitlines()[0].strip()
    except (IndexError, UnicodeDecodeError) as err:
        raise ValueError("invalid REFER sipfrag body") from err
    match = _SIPFRAG_STATUS.fullmatch(status_line)
    if match is None:
        raise ValueError("invalid REFER sipfrag status line")
    phase, reason, retry_after = _parse_subscription_state(
        message.header("Subscription-State")
    )
    return ReferNotify(int(match.group(1)), phase, reason, retry_after)
