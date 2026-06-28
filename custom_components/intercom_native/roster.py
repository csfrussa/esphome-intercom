"""Canonical JSON roster and SIP routing decisions for phase-1 VoIP."""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import re
from typing import Any, Literal


RosterKind = Literal["ha", "esp", "phone", "sip", "group"]
RouteKind = Literal["direct", "bridge", "requires_bridge", "group", "trunk"]
_PHONE_RE = re.compile(r"^[+0-9][0-9 .()/-]{2,}$")


class RosterError(ValueError):
    """Invalid roster data."""


@dataclass(frozen=True, slots=True)
class RosterEntry:
    id: str
    name: str = ""
    kind: RosterKind = "esp"
    address: str = ""
    sip_uri: str = ""
    number: str = ""
    ha_bridge: bool = False
    enabled: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def display_name(self) -> str:
        return self.name or self.id


@dataclass(frozen=True, slots=True)
class RouteDecision:
    kind: RouteKind
    target: str
    sip_uri: str
    entry: RosterEntry | None = None
    reason: str = ""


def _entry_from_mapping(raw: dict[str, Any]) -> RosterEntry:
    entry_id = str(raw.get("id") or raw.get("name") or "").strip()
    if not entry_id:
        raise RosterError("roster entry missing id")
    if not raw.get("kind"):
        raise RosterError(f"roster entry {entry_id!r} missing kind")
    kind = str(raw.get("kind") or "").strip().lower()
    if kind not in {"ha", "esp", "phone", "sip", "group"}:
        raise RosterError(f"unsupported roster kind {kind!r}")
    return RosterEntry(
        id=entry_id,
        name=str(raw.get("name") or entry_id).strip(),
        kind=kind,  # type: ignore[arg-type]
        address=str(raw.get("address") or raw.get("host") or "").strip(),
        sip_uri=str(raw.get("sip_uri") or "").strip(),
        number=str(raw.get("number") or "").strip(),
        ha_bridge=bool(raw.get("ha_bridge", False)),
        enabled=bool(raw.get("enabled", True)),
        metadata=dict(raw.get("metadata") or {}),
    )


def parse_roster_json(value: str | bytes | dict[str, Any] | list[dict[str, Any]]) -> list[RosterEntry]:
    if isinstance(value, bytes):
        value = value.decode("utf-8", errors="strict")
    if isinstance(value, str):
        loaded = json.loads(value or "[]")
    else:
        loaded = value
    if isinstance(loaded, dict):
        entries = loaded.get("contacts") or loaded.get("entries") or []
    else:
        entries = loaded
    if not isinstance(entries, list):
        raise RosterError("roster JSON must contain a list of contacts")
    out = [_entry_from_mapping(item) for item in entries if isinstance(item, dict)]
    ids = [entry.id.lower() for entry in out]
    if len(ids) != len(set(ids)):
        raise RosterError("duplicate roster id")
    return out


def dump_roster_json(entries: list[RosterEntry]) -> str:
    payload = {
        "version": 1,
        "contacts": [
            {
                "id": entry.id,
                "name": entry.name,
                "kind": entry.kind,
                "address": entry.address,
                "sip_uri": entry.sip_uri,
                "number": entry.number,
                "ha_bridge": entry.ha_bridge,
                "enabled": entry.enabled,
                "metadata": entry.metadata,
            }
            for entry in entries
        ],
    }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def find_entry(entries: list[RosterEntry], target: str) -> RosterEntry | None:
    def norm(value: str) -> str:
        return "".join(ch for ch in value.strip().lower() if ch.isalnum())

    wanted = norm(target)
    for entry in entries:
        if norm(entry.id) == wanted or norm(entry.name) == wanted:
            return entry
    return None


def _ha_entry(entries: list[RosterEntry]) -> RosterEntry | None:
    for entry in entries:
        if entry.kind == "ha" and entry.address and entry.enabled:
            return entry
    return None


def _looks_phone(value: str) -> bool:
    return bool(_PHONE_RE.match(value.strip()))


def _port_suffix(port: Any) -> str:
    try:
        value = int(port)
    except (TypeError, ValueError):
        return ""
    return "" if value in (0, 5060) else f":{value}"


def _sip_transport(entry: RosterEntry | None) -> str:
    metadata = entry.metadata if entry is not None else {}
    value = str(
        metadata.get("sip_transport")
        or metadata.get("signaling_transport")
        or ""
    ).strip().lower()
    if value in {"tcp", "udp"}:
        return value
    return ""


def _sip_uri_transport(uri: str) -> str:
    marker = ";transport="
    lower = uri.lower()
    if marker not in lower:
        return ""
    value = lower.split(marker, 1)[1].split(";", 1)[0].strip()
    return value if value in {"tcp", "udp"} else ""


def _sip_uri(user: str, host: str, port: Any = None, transport: str = "") -> str:
    suffix = f";transport={transport.lower()}" if transport.lower() in {"tcp", "udp"} else ""
    return f"sip:{user}@{host}{_port_suffix(port)}{suffix}"


def _entry_sip_port(entry: RosterEntry | None) -> Any:
    return (entry.metadata or {}).get("sip_port") if entry is not None else None


def resolve_target(
    target: str,
    entries: list[RosterEntry],
    *,
    ha_bridge: bool = False,
    ha_host: str = "",
    ha_sip_port: int = 5060,
    force_ha: bool | None = None,
) -> RouteDecision:
    target = target.strip()
    if not target:
        raise RosterError("empty call target")
    if force_ha is not None:
        ha_bridge = bool(force_ha)
    if target.lower().startswith("sip:") and "@" in target:
        return RouteDecision("direct", target, target)

    explicit_name = target
    explicit_host = ""
    if "@" in target:
        explicit_name, explicit_host = target.split("@", 1)
        explicit_name = explicit_name.strip()
        explicit_host = explicit_host.strip()
        if explicit_name and explicit_host:
            uri = f"sip:{explicit_name}@{explicit_host}"
            return RouteDecision("direct", explicit_name, uri)

    entry = find_entry(entries, target)
    ha = _ha_entry(entries)
    if ha is None and ha_host:
        ha = RosterEntry(id="HA", name="HA", kind="ha", address=ha_host, metadata={"sip_port": ha_sip_port})
    if entry is not None and not entry.enabled:
        return RouteDecision("bridge", target, "", entry=entry, reason="disabled")

    if entry is not None:
        if entry.kind == "phone":
            number = entry.number or entry.id
            if ha is None:
                return RouteDecision("requires_bridge", number, "", entry=entry, reason="ha_required")
            return RouteDecision("requires_bridge", number, _sip_uri(number, ha.address, _entry_sip_port(ha), _sip_transport(ha)), entry=entry)
        if entry.kind == "sip" and entry.sip_uri:
            if not _sip_uri_transport(entry.sip_uri) and not _sip_transport(entry):
                if ha is not None:
                    return RouteDecision(
                        "bridge",
                        entry.id,
                        _sip_uri(entry.id, ha.address, _entry_sip_port(ha), _sip_transport(ha)),
                        entry=entry,
                        reason="missing_direct_transport",
                    )
                return RouteDecision("bridge", entry.id, "", entry=entry, reason="missing_direct_transport")
            return RouteDecision("direct", entry.id, entry.sip_uri, entry=entry)
        if entry.kind == "group":
            return RouteDecision("group", entry.id, "", entry=entry)
        if (ha_bridge or entry.ha_bridge or not entry.address) and ha is not None and entry.kind != "ha":
            return RouteDecision("bridge", entry.id, _sip_uri(entry.id, ha.address, _entry_sip_port(ha), _sip_transport(ha)), entry=entry)
        if entry.address:
            transport = _sip_transport(entry)
            if entry.kind == "esp" and not transport:
                if ha is not None:
                    return RouteDecision(
                        "bridge",
                        entry.id,
                        _sip_uri(entry.id, ha.address, _entry_sip_port(ha), _sip_transport(ha)),
                        entry=entry,
                        reason="missing_direct_transport",
                    )
                return RouteDecision("bridge", entry.id, "", entry=entry, reason="missing_direct_transport")
            return RouteDecision("direct", entry.id, _sip_uri(entry.id, entry.address, _entry_sip_port(entry), transport), entry=entry)

    if _looks_phone(target):
        if ha is None:
            return RouteDecision("requires_bridge", target, "", reason="ha_required")
        return RouteDecision("requires_bridge", target, _sip_uri(target, ha.address, _entry_sip_port(ha), _sip_transport(ha)))

    if ha is None:
        return RouteDecision("bridge", target, "", reason="ha_required")
    return RouteDecision("bridge", target, _sip_uri(target, ha.address, _entry_sip_port(ha), _sip_transport(ha)))
