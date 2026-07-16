"""Pure validation helpers shared by the VoIP Stack config flow and tests."""

from collections.abc import Iterable, Mapping
from typing import Any

from .const import CONF_PHONEBOOK_CONTACTS
from .roster import normalize_roster_key


ROUTE_ALIAS_FIELDS = ("id", "name", "extension", "number", "username")
GROUP_ALIAS_FIELDS = ("ring_group", "conference_group")


def normalized_aliases(values: Iterable[object]) -> set[str]:
    """Return non-empty aliases normalized exactly like the SIP router."""
    return {
        key
        for value in values
        if (key := normalize_roster_key(str(value or "")))
    }


def mapping_route_aliases(item: Mapping[str, Any]) -> set[str]:
    """Return every dialable alias claimed by one persisted mapping."""
    return normalized_aliases(item.get(field) for field in ROUTE_ALIAS_FIELDS)


def mapping_group_aliases(item: Mapping[str, Any]) -> set[str]:
    """Return group routes declared by one mapping (comma-separated allowed)."""
    values: list[str] = []
    metadata = item.get("metadata")
    metadata = metadata if isinstance(metadata, Mapping) else {}
    for field in GROUP_ALIAS_FIELDS:
        raw = item.get(field, metadata.get(field))
        values.extend(part.strip() for part in str(raw or "").split(","))
    return normalized_aliases(values)


def route_namespace_conflicts(
    *,
    candidate_routes: Iterable[object] = (),
    candidate_groups: Iterable[object] = (),
    existing: Iterable[Mapping[str, Any]] = (),
) -> bool:
    """Check routes and group names as one canonical routing namespace.

    Reusing a group name on multiple members is intentional. A phone/contact
    alias may not shadow a group, and a newly declared group may not shadow an
    existing phone/contact alias.
    """
    wanted_routes = normalized_aliases(candidate_routes)
    wanted_groups = normalized_aliases(candidate_groups)
    if wanted_routes & wanted_groups:
        return True
    existing_routes: set[str] = set()
    existing_groups: set[str] = set()
    for item in existing:
        if not isinstance(item, Mapping):
            continue
        existing_routes.update(mapping_route_aliases(item))
        existing_groups.update(mapping_group_aliases(item))
    return bool(
        wanted_routes & (existing_routes | existing_groups)
        or wanted_groups & existing_routes
    )


def extension_conflicts(extension: str, existing: Mapping[str, Any]) -> bool:
    """Return whether an extension collides with a persisted route."""
    wanted = normalize_roster_key(str(extension))
    for key in ("sip_accounts", CONF_PHONEBOOK_CONTACTS):
        for item in existing.get(key, []) or []:
            if not isinstance(item, Mapping):
                continue
            values = (item.get("extension"), item.get("number"), item.get("username"))
            if wanted in normalized_aliases(values):
                return True
    return False
