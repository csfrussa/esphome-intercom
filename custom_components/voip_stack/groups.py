"""Phonebook group aggregation for HA-anchored SIP routing."""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import Iterable

from .roster import RosterEntry, normalize_roster_key

_LOGGER = logging.getLogger(__name__)

GROUP_TYPE_CONFERENCE = "conference"
GROUP_TYPE_RING = "ring"


@dataclass(slots=True)
class GroupDef:
    name: str
    group_type: str
    members: list[str] = field(default_factory=list)
    ring_members: list[str] = field(default_factory=list)
    auto: bool = True


def _append_member(group: GroupDef, member: str) -> None:
    if member and member not in group.members:
        group.members.append(member)


def _append_ring_member(group: GroupDef, member: str) -> None:
    _append_member(group, member)
    if member and member not in group.ring_members:
        group.ring_members.append(member)


def _metadata_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    return str(value or "").strip().lower() in {"1", "true", "yes", "on"}


def _declare(
    groups: dict[str, GroupDef],
    *,
    name: str,
    group_type: str,
    member: str,
    ring: bool = False,
) -> None:
    group_name = (name or "").strip()
    if not group_name or not member:
        return
    key = normalize_roster_key(group_name)
    existing = groups.get(key)
    if existing is not None and existing.group_type != group_type:
        if existing.group_type != GROUP_TYPE_CONFERENCE and group_type == GROUP_TYPE_CONFERENCE:
            _LOGGER.warning("Group %s declared as both ring and conference; conference wins", group_name)
            existing.group_type = GROUP_TYPE_CONFERENCE
            existing.members.clear()
            existing.ring_members.clear()
            (_append_ring_member if ring else _append_member)(existing, member)
        else:
            _LOGGER.warning("Group %s declared as both conference and ring; ignoring ring declaration", group_name)
        return
    if existing is None:
        existing = GroupDef(name=group_name, group_type=group_type)
        groups[key] = existing
    (_append_ring_member if ring else _append_member)(existing, member)


def _entry_group(entry: RosterEntry, key: str) -> str:
    return str((entry.metadata or {}).get(key) or "").strip()


def collect_groups(
    peers,
    manual_entries: Iterable[RosterEntry],
    registered_entries: Iterable[RosterEntry],
    *,
    existing_entries: Iterable[RosterEntry] = (),
) -> dict[str, GroupDef]:
    """Collect auto group definitions from ESP peers and roster metadata."""
    groups: dict[str, GroupDef] = {}
    for peer in peers:
        member = str(getattr(peer, "name", "") or "").strip()
        _declare(
            groups,
            name=str(getattr(peer, "conference_group", "") or ""),
            group_type=GROUP_TYPE_CONFERENCE,
            member=member,
            ring=bool(getattr(peer, "conference_ring", False)),
        )
        _declare(groups, name=str(getattr(peer, "ring_group", "") or ""), group_type=GROUP_TYPE_RING, member=member)
    for entry in list(manual_entries) + list(registered_entries):
        member = entry.id or entry.name
        metadata = entry.metadata or {}
        _declare(
            groups,
            name=_entry_group(entry, "conference_group"),
            group_type=GROUP_TYPE_CONFERENCE,
            member=member,
            ring=_metadata_bool(metadata.get("conference_ring")),
        )
        _declare(groups, name=_entry_group(entry, "ring_group"), group_type=GROUP_TYPE_RING, member=member)

    existing = {normalize_roster_key(entry.id) for entry in existing_entries}
    existing |= {normalize_roster_key(entry.name) for entry in existing_entries}
    existing.discard("")
    for key in list(groups):
        if key in existing:
            _LOGGER.warning("Skipping group %s because it collides with an existing roster entry", groups[key].name)
            groups.pop(key, None)
    return {group.name: group for group in groups.values()}
