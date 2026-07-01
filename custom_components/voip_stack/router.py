"""Deterministic SIP routing contracts for the HA VoIP router."""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import re
from typing import Literal

from .roster import RosterEntry, find_entry


class RouteAction(StrEnum):
    DIRECT = "direct"
    ANSWER_HA = "answer_ha"
    FORWARD = "forward"
    BRIDGE = "bridge"
    TRUNK = "trunk"
    GROUP = "group"
    REJECT = "reject"
    BUSY = "busy"
    DECLINE = "decline"


class RouteReason(StrEnum):
    DEFAULT_HA = "default_ha"
    EXPLICIT_ROUTE = "explicit_route"
    NUMBER_VIA_HA = "number_via_ha"
    NAME_VIA_HA = "name_via_ha"
    DIRECT_URI = "direct_uri"
    TARGET_DISABLED = "target_disabled"
    ROUTE_NOT_FOUND = "route_not_found"
    DIALPLAN_TIMEOUT = "dialplan_timeout"
    MEDIA_INCOMPATIBLE = "media_incompatible"
    TRUNK_UNAVAILABLE = "trunk_unavailable"
    NO_DIRECT_TRANSPORT = "no_direct_transport"


class RouteHintSource(StrEnum):
    NONE = "none"
    REQUEST_URI = "request_uri"
    TO_HEADER = "to_header"
    DTMF = "dtmf"
    SIP_INFO = "sip_info"
    AUTOMATION = "automation"
    MANUAL = "manual"


class TargetClass(StrEnum):
    SIP_URI = "sip_uri"
    NAME_AT_HOST = "name_at_host"
    NUMERIC = "numeric"
    NAME = "name"


@dataclass(frozen=True, slots=True)
class CallContext:
    call_id: str
    direction: Literal["inbound", "outbound", "internal"]
    origin: Literal["esp", "ha_softphone", "ha_router", "trunk", "softphone"]
    caller: str = ""
    called_did: str = ""
    requested_target: str = ""
    route_hint: str = ""
    route_hint_source: RouteHintSource = RouteHintSource.NONE
    source_host: str = ""

    @property
    def has_explicit_route_hint(self) -> bool:
        return bool(self.route_hint.strip())


@dataclass(frozen=True, slots=True)
class RouteDecision:
    action: RouteAction
    target: str = ""
    sip_uri: str = ""
    status: int = 0
    reason: RouteReason = RouteReason.DEFAULT_HA
    source: Literal["builtin", "phonebook", "automation", "trunk"] = "builtin"
    entry: RosterEntry | None = None


_PUBLIC_NUMBER_RE = re.compile(r"^\+?[0-9][0-9 .()/-]{2,}$")
_NUMERIC_RE = re.compile(r"^[0-9][0-9 .()/-]*$")


def classify_target(target: str) -> TargetClass:
    raw = (target or "").strip()
    lower = raw.lower()
    if lower.startswith("sip:") and "@" in raw:
        return TargetClass.SIP_URI
    if "@" in raw and raw.split("@", 1)[0].strip() and raw.split("@", 1)[1].strip():
        return TargetClass.NAME_AT_HOST
    if _NUMERIC_RE.match(raw):
        return TargetClass.NUMERIC
    return TargetClass.NAME


def looks_public_number(target: str) -> bool:
    return bool(_PUBLIC_NUMBER_RE.match((target or "").strip()))


def to_sip_uri(target: str) -> str:
    raw = (target or "").strip()
    if raw.lower().startswith("sip:"):
        return raw
    if "@" in raw:
        return f"sip:{raw}"
    return raw


def _entry_transport(entry: RosterEntry | None) -> str:
    value = str((entry.metadata if entry is not None else {}).get("sip_transport") or "").strip().lower()
    return value if value in {"tcp", "udp"} else ""


def _entry_port(entry: RosterEntry | None) -> int:
    try:
        return int((entry.metadata if entry is not None else {}).get("sip_port") or 5060)
    except (TypeError, ValueError):
        return 5060


def _uri(user: str, host: str, port: int = 5060, transport: str = "") -> str:
    suffix = "" if int(port) == 5060 else f":{int(port)}"
    transport_suffix = f";transport={transport}" if transport in {"tcp", "udp"} else ""
    return f"sip:{user}@{host}{suffix}{transport_suffix}"


def ha_uri_for(target: str, entries: list[RosterEntry], ha_uri: str = "") -> str:
    ha = _ha_entry(entries, ha_uri)
    if ha is None or not ha.address:
        return ha_uri
    transport = _entry_transport(ha)
    return _uri(target or ha.id or "HA", ha.address, _entry_port(ha), transport)


def _ha_entry(entries: list[RosterEntry], ha_uri: str = "") -> RosterEntry | None:
    for entry in entries:
        if entry.kind == "ha" and entry.enabled and entry.address:
            return entry
    if ha_uri:
        try:
            user_host = ha_uri[4:] if ha_uri.lower().startswith("sip:") else ha_uri
            user, host = user_host.split("@", 1)
            host = host.split(";", 1)[0]
            if ":" in host:
                host, port = host.rsplit(":", 1)
            else:
                port = "5060"
            return RosterEntry(id=user or "HA", kind="ha", address=host, metadata={"sip_port": int(port)})
        except Exception:
            return None
    return None


def resolve_esp_origin(target: str, entries: list[RosterEntry], ha_uri: str) -> RouteDecision:
    target = (target or "").strip()
    target_class = classify_target(target)
    if target_class in {TargetClass.SIP_URI, TargetClass.NAME_AT_HOST}:
        return RouteDecision(RouteAction.DIRECT, target=target, sip_uri=to_sip_uri(target), reason=RouteReason.DIRECT_URI)
    if target_class == TargetClass.NUMERIC:
        return RouteDecision(RouteAction.BRIDGE, target=target, sip_uri=ha_uri_for(target, entries, ha_uri), reason=RouteReason.NUMBER_VIA_HA)

    entry = find_entry(entries, target)
    if entry is None:
        return RouteDecision(RouteAction.BRIDGE, target=target, sip_uri=ha_uri, reason=RouteReason.NAME_VIA_HA)
    if not entry.enabled:
        return RouteDecision(RouteAction.REJECT, target=target, status=403, reason=RouteReason.TARGET_DISABLED, entry=entry)
    if entry.kind == "group":
        return RouteDecision(RouteAction.GROUP, target=entry.id, source="phonebook", entry=entry)
    transport = _entry_transport(entry)
    direct_uri = entry.sip_uri or (_uri(entry.id, entry.address, _entry_port(entry), transport) if entry.address else "")
    if direct_uri and transport and not entry.ha_bridge:
        return RouteDecision(RouteAction.DIRECT, target=entry.id, sip_uri=direct_uri, source="phonebook", entry=entry)
    bridge_target = entry.number or entry.id
    return RouteDecision(RouteAction.BRIDGE, target=bridge_target, sip_uri=ha_uri_for(bridge_target, entries, ha_uri), source="phonebook", entry=entry)


def resolve_ha_router(target: str, entries: list[RosterEntry], *, trunk_ready: bool = False) -> RouteDecision:
    target = (target or "").strip()
    target_class = classify_target(target)
    if target_class in {TargetClass.SIP_URI, TargetClass.NAME_AT_HOST}:
        return RouteDecision(RouteAction.DIRECT, target=target, sip_uri=to_sip_uri(target), reason=RouteReason.DIRECT_URI)

    entry = find_entry(entries, target)
    if entry is not None and not entry.enabled:
        return RouteDecision(RouteAction.REJECT, target=target, status=403, reason=RouteReason.TARGET_DISABLED, entry=entry)
    if entry is not None:
        if entry.kind == "ha":
            return RouteDecision(RouteAction.ANSWER_HA, target=entry.id, reason=RouteReason.DEFAULT_HA, source="phonebook", entry=entry)
        if entry.kind == "group":
            return RouteDecision(RouteAction.GROUP, target=entry.id, source="phonebook", entry=entry)
        if entry.kind == "phone":
            number = entry.number or entry.id
            if trunk_ready:
                return RouteDecision(RouteAction.TRUNK, target=number, source="trunk", entry=entry)
            return RouteDecision(RouteAction.REJECT, target=number, status=503, reason=RouteReason.TRUNK_UNAVAILABLE, entry=entry)
        transport = _entry_transport(entry)
        sip_uri = entry.sip_uri or (_uri(entry.id, entry.address, _entry_port(entry), transport) if entry.address else "")
        if entry.kind == "esp" and entry.address and not transport and not entry.sip_uri:
            return RouteDecision(RouteAction.REJECT, target=entry.id, status=480, reason=RouteReason.NO_DIRECT_TRANSPORT, entry=entry)
        if sip_uri:
            return RouteDecision(RouteAction.FORWARD, target=entry.id, sip_uri=sip_uri, source="phonebook", entry=entry)
        return RouteDecision(RouteAction.REJECT, target=target, status=404, reason=RouteReason.ROUTE_NOT_FOUND, entry=entry)
    if looks_public_number(target):
        if trunk_ready:
            return RouteDecision(RouteAction.TRUNK, target=target, source="trunk")
        return RouteDecision(RouteAction.REJECT, target=target, status=503, reason=RouteReason.TRUNK_UNAVAILABLE)
    return RouteDecision(RouteAction.REJECT, target=target, status=404, reason=RouteReason.ROUTE_NOT_FOUND)


def route_inbound_trunk(ctx: CallContext, entries: list[RosterEntry], *, trunk_ready: bool = False) -> RouteDecision:
    if not ctx.has_explicit_route_hint:
        return RouteDecision(RouteAction.ANSWER_HA, reason=RouteReason.DEFAULT_HA)
    resolved = resolve_ha_router(ctx.route_hint, entries, trunk_ready=trunk_ready)
    if resolved.action is RouteAction.REJECT:
        return RouteDecision(RouteAction.REJECT, target=ctx.route_hint, status=404, reason=RouteReason.ROUTE_NOT_FOUND)
    return resolved
