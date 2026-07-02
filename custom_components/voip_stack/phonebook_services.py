"""HA service handlers for the central VoIP phonebook."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import logging

from homeassistant.core import ServiceCall

from .const import DOMAIN
from .phonebook_runtime import push_roster_json_to_esps
from .roster import RosterEntry, parse_roster_json
from .store import manual_roster_entries, store_manual_roster_entries
from .websocket_api import _fire_call_event

_LOGGER = logging.getLogger(__name__)


def build_phonebook_service_handlers(
    refresh_and_push_phonebook: Callable[[object], Awaitable[None]],
) -> dict[str, Callable[[ServiceCall], Awaitable[None]]]:
    """Build phonebook service handlers with refresh behavior injected."""

    async def add_contact(call: ServiceCall) -> None:
        hass = call.hass
        name = str(call.data["name"]).strip()
        entry_id = str(call.data.get("id") or name).strip()

        def metadata_value(key: str):
            value = call.data.get(key)
            if value in (None, ""):
                return None
            return value

        metadata = {
            key: metadata_value(key)
            for key in (
                "transport",
                "rtp_port",
                "tx_rate",
                "rx_rate",
                "tx_formats",
                "rx_formats",
                "max_payload_bytes",
            )
            if key in call.data and metadata_value(key) is not None
        }
        address = str(call.data.get("address") or "").strip()
        sip_uri = str(call.data.get("sip_uri") or "").strip()
        extension = str(call.data.get("extension") or "").strip()
        number = str(call.data.get("number") or "").strip()
        port = int(call.data.get("port") or 0)
        entry = RosterEntry(
            id=entry_id,
            name=name,
            address=address,
            sip_uri=sip_uri,
            extension=extension,
            number=number,
            port=port,
            ha_bridge=bool(call.data.get("ha_bridge", False)),
            metadata=metadata,
        )
        entries = [
            item for item in manual_roster_entries(hass)
            if getattr(item, "id", "").lower() != entry.id.lower()
            and getattr(item, "name", "").lower() != entry.name.lower()
        ]
        entries.append(entry)
        store_manual_roster_entries(hass, entries)
        await refresh_and_push_phonebook(hass)
        _LOGGER.info("Phonebook contact added: %s", entry.id)

    async def remove_contact(call: ServiceCall) -> None:
        hass = call.hass
        name = str(call.data["name"]).strip()
        wanted = name.lower()
        entries = manual_roster_entries(hass)
        before = len(entries)
        entries = [
            item
            for item in entries
            if getattr(item, "id", "").lower() != wanted
            and getattr(item, "name", "").lower() != wanted
            and getattr(item, "extension", "").lower() != wanted
            and getattr(item, "number", "").lower() != wanted
        ]
        store_manual_roster_entries(hass, entries)
        await refresh_and_push_phonebook(hass)
        _LOGGER.info("Phonebook contact removed: %s (%d removed)", name, before - len(entries))

    async def set_contacts(call: ServiceCall) -> None:
        hass = call.hass
        entries = parse_roster_json(str(call.data.get("roster_json") or "[]"))
        store_manual_roster_entries(hass, entries)
        await refresh_and_push_phonebook(hass)
        _LOGGER.info("Phonebook manual contacts replaced: %d entries", len(entries))

    async def clear_contacts(call: ServiceCall) -> None:
        hass = call.hass
        store_manual_roster_entries(hass, [])
        await refresh_and_push_phonebook(hass)
        _LOGGER.info("Phonebook manual contacts cleared")

    async def export_phonebook(call: ServiceCall) -> None:
        hass = call.hass
        sensor = hass.data.get(DOMAIN, {}).get("phonebook_sensor")
        if sensor is not None:
            await sensor.async_update()
            roster_json = sensor.extra_state_attributes.get("roster_json", "")
        else:
            roster_json = ""
        _fire_call_event(
            hass,
            {
                "state": "export_phonebook",
                "roster_json": roster_json,
                "call_id": "",
            },
            "phonebook",
        )
        _LOGGER.info("Phonebook exported (%d bytes)", len(roster_json))

    async def push_phonebook(call: ServiceCall) -> None:
        hass = call.hass
        sensor = hass.data.get(DOMAIN, {}).get("phonebook_sensor")
        if sensor is not None:
            await sensor.async_update()
            roster_json = sensor.extra_state_attributes.get("roster_json", "")
        else:
            state = hass.states.get("sensor.voip_phonebook")
            roster_json = str(state.attributes.get("roster_json") or "") if state is not None else ""
        await push_roster_json_to_esps(hass, roster_json)
        _LOGGER.info("Phonebook push requested (%d bytes)", len(roster_json))

    return {
        "add_contact": add_contact,
        "set_contacts": set_contacts,
        "remove_contact": remove_contact,
        "clear_contacts": clear_contacts,
        "export_phonebook": export_phonebook,
        "push_phonebook": push_phonebook,
    }
