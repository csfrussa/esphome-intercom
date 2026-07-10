"""Pure validation helpers shared by the VoIP Stack config flow and tests."""

from collections.abc import Mapping
from typing import Any

from .const import CONF_PHONEBOOK_CONTACTS


def extension_conflicts(extension: str, existing: Mapping[str, Any]) -> bool:
    """Return whether an extension collides with a persisted route."""
    wanted = str(extension).strip()
    for key in ("sip_accounts", CONF_PHONEBOOK_CONTACTS):
        for item in existing.get(key, []) or []:
            if not isinstance(item, Mapping):
                continue
            values = (item.get("extension"), item.get("number"), item.get("username"))
            if wanted in {str(value).strip() for value in values if value not in (None, "")}:
                return True
    return False
