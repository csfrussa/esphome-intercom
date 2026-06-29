"""Sensor platform for Home Assistant VoIP Stack.

HA publishes one SIP dial-plan roster.

Entity names:
  - sensor.voip_phonebook       format per row:
      name|ip|sip_port|rtp_port|audio_mode|tx_formats|rx_formats|sip_tcp

ESP YAMLs subscribe to the unified sensor and normalize it locally into their
SIP dial plan.
"""
import logging

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    unified_sensor = VoipPhonebookSensor(hass)
    async_add_entities([unified_sensor], True)
    bucket = hass.data.setdefault(DOMAIN, {})
    bucket["phonebook_sensor"] = unified_sensor


class VoipPhonebookSensor(SensorEntity):
    """Authoritative SIP phonebook publisher."""

    _attr_has_entity_name = False
    _attr_should_poll = False
    _attr_icon = "mdi:phone-voip"

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._attr_unique_id = "homeassistant_voip_stack_phonebook"
        self._attr_name = "VoIP Phonebook"
        self.entity_id = "sensor.voip_phonebook"
        self._attr_native_value = "0 entries"
        self._phonebook = ""
        self._roster_json = '{"version":1,"contacts":[]}'
        self._count = 0
        self._tracked_entities: set[str] = set()
        self._unsub_state = None
        self._unsub_registry = None

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        return {
            "phonebook": self._phonebook,
            "roster_json": self._roster_json,
            "count": self._count,
        }

    async def async_added_to_hass(self) -> None:
        @callback
        def _on_registry_change(event) -> None:
            entity_id = event.data.get("entity_id") or ""
            if "voip_state" not in entity_id and "voip_endpoint" not in entity_id:
                return
            self.hass.async_create_task(self._refresh_tracked_entities())

        self._unsub_registry = self.hass.bus.async_listen(
            "entity_registry_updated", _on_registry_change
        )
        await self._refresh_tracked_entities(initial=True)

    async def async_will_remove_from_hass(self) -> None:
        if self._unsub_state:
            self._unsub_state()
            self._unsub_state = None
        if self._unsub_registry:
            self._unsub_registry()
            self._unsub_registry = None

    async def _refresh_tracked_entities(self, initial: bool = False) -> None:
        entity_registry = er.async_get(self.hass)
        new_set = {
            e.entity_id
            for e in entity_registry.entities.values()
            if "voip_state" in e.entity_id or "voip_endpoint" in e.entity_id
        }
        if new_set == self._tracked_entities and not initial:
            return
        self._tracked_entities = new_set

        @callback
        def _on_state_change(event) -> None:
            entity_id = event.data.get("entity_id") or ""
            new_state = event.data.get("new_state")
            old_state = event.data.get("old_state")
            if "voip_endpoint" in entity_id:
                old_value = old_state.state if old_state is not None else None
                new_value = new_state.state if new_state is not None else None
                if old_value != new_value:
                    self.hass.async_create_task(self._recompute())
                return
            old_avail = old_state is not None and old_state.state != "unavailable"
            new_avail = new_state is not None and new_state.state != "unavailable"
            if old_avail == new_avail:
                return
            self.hass.async_create_task(self._recompute())

        if self._unsub_state:
            self._unsub_state()
            self._unsub_state = None
        if new_set:
            self._unsub_state = async_track_state_change_event(
                self.hass, list(new_set), _on_state_change
            )
        await self._recompute()

    async def _recompute(self) -> None:
        # Reuse the existing peer builders from __init__.py so routing logic
        # stays in one place.
        from . import (
            _async_build_peer_snapshot,
            _format_entry_unified,
            _push_roster_json_to_esps,
            _registered_roster_entries,
        )
        from .roster import RosterEntry, dump_roster_json, merge_roster_overrides

        peers = await _async_build_peer_snapshot(self.hass)
        entries = [_format_entry_unified(p) for p in peers]
        roster_entries = [
            RosterEntry(
                id=p.name,
                name=p.name,
                kind="ha" if p.is_ha else "esp",
                address=p.host,
                metadata={
                    "sip_transport": (
                        str((p.device or {}).get("sip_transport") or "tcp").lower()
                        if p.is_ha or p.device is not None
                        else ""
                    ),
                    "sip_port": p.sip_port,
                    "rtp_port": p.rtp_port,
                    "audio_mode": p.audio_mode,
                    "tx_formats": p.tx_formats or [],
                    "rx_formats": p.rx_formats or [],
                },
            )
            for p in peers
        ]
        manual_entries: list[RosterEntry] = []
        for raw in self.hass.data.get(DOMAIN, {}).get("manual_roster_entries", []):
            if isinstance(raw, RosterEntry):
                manual_entries.append(raw)
        roster_entries = merge_roster_overrides(roster_entries, manual_entries)
        roster_entries.extend(_registered_roster_entries(self.hass))
        phonebook = ",".join(entries)
        roster_json = dump_roster_json(roster_entries)
        visible_count = len(roster_entries)
        new_value = f"{visible_count} entry" if visible_count == 1 else f"{visible_count} entries"
        if (
            new_value != self._attr_native_value
            or phonebook != self._phonebook
            or roster_json != self._roster_json
        ):
            self._attr_native_value = new_value
            self._phonebook = phonebook
            self._roster_json = roster_json
            self._count = visible_count
            _LOGGER.debug(
                "Phonebook recomputed (%d entries)", visible_count
            )
            if self.hass and self.entity_id:
                self.async_write_ha_state()
                self.hass.async_create_task(_push_roster_json_to_esps(self.hass, roster_json))

    async def async_update(self) -> None:
        await self._recompute()
