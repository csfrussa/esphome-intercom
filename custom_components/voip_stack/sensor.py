"""Sensor platform for VoIP Stack.

HA publishes one SIP dial-plan roster.

Entity names:
  - sensor.voip_phonebook       format per row:
      name|ip|sip_port|rtp_port|audio_mode|tx_formats|rx_formats|sip_tcp

ESP YAMLs subscribe to the unified sensor and normalize it locally into their
SIP dial plan.
"""
import logging
import json

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event

from .audio_format import HA_SIP_PCM_FORMATS
from .const import DOMAIN, HA_SOFTPHONE_ENDPOINT_ENTITY_ID

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0
UNAVAILABLE_STATES = {"", "unknown", "unavailable"}


def _state_is_available(state) -> bool:
    return state is not None and str(state.state or "").strip().lower() not in UNAVAILABLE_STATES


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    ha_endpoint_sensor = HaSoftphoneEndpointSensor(hass)
    unified_sensor = VoipPhonebookSensor(hass)
    async_add_entities([ha_endpoint_sensor, unified_sensor], True)
    bucket = hass.data.setdefault(DOMAIN, {})
    bucket["ha_softphone_endpoint_sensor"] = ha_endpoint_sensor
    bucket["phonebook_sensor"] = unified_sensor


class HaSoftphoneEndpointSensor(SensorEntity):
    """Local HA softphone endpoint, published in the same shape as ESP endpoints."""

    _attr_has_entity_name = False
    _attr_should_poll = False
    _attr_icon = "mdi:phone-voip"

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._attr_unique_id = "voip_stack_ha_softphone_voip_endpoint"
        self._attr_name = "VoIP Stack HA Softphone Endpoint"
        self.entity_id = HA_SOFTPHONE_ENDPOINT_ENTITY_ID
        self._attr_native_value = "unknown"
        self._attr_extra_state_attributes = {"local_ha": True}

    async def async_update(self) -> None:
        from . import _get_transport_config, _ha_advertise_host
        from .websocket_api import _ha_peer_name, _ha_softphone_groups

        host = await _ha_advertise_host(self.hass)
        if not host:
            self._attr_native_value = "unavailable"
            self._attr_extra_state_attributes = {"local_ha": True, "available": False}
            if self.hass and self.entity_id:
                self.async_write_ha_state()
            return

        cfg = _get_transport_config(self.hass)
        groups = _ha_softphone_groups(self.hass)
        tx = ";".join(fmt.wire_token() for fmt in HA_SIP_PCM_FORMATS)
        rx = tx
        endpoint = (
            f"{_ha_peer_name(self.hass)}|{host}|{int(cfg['sip_port'])}|{int(cfg['rtp_port'])}|"
            f"full_duplex|{tx}|{rx}|sip_tcp|"
            f"|{groups['conference_group']}|{groups['ring_group']}|"
            f"{1 if groups['conference_ring'] else 0}"
        )
        self._attr_native_value = endpoint
        self._attr_extra_state_attributes = {
            "local_ha": True,
            "available": True,
            "ring_group": groups["ring_group"],
            "conference_group": groups["conference_group"],
            "conference_ring": bool(groups["conference_ring"]),
        }
        if self.hass and self.entity_id:
            self.async_write_ha_state()


class VoipPhonebookSensor(SensorEntity):
    """Authoritative SIP phonebook publisher."""

    _attr_has_entity_name = False
    _attr_should_poll = False
    _attr_icon = "mdi:phone-voip"

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._attr_unique_id = "voip_stack_phonebook"
        self._attr_name = "VoIP Phonebook"
        self.entity_id = "sensor.voip_phonebook"
        self._attr_native_value = "0 entries"
        self._phonebook = ""
        self._roster_json = '{"version":1,"contacts":[]}'
        self._softphone_targets_json = "[]"
        self._count = 0
        self._tracked_entities: set[str] = set()
        self._unsub_state = None
        self._unsub_registry = None

    @property
    def extra_state_attributes(self) -> dict[str, object]:
        return {
            "phonebook": self._phonebook,
            "roster_json": self._roster_json,
            "softphone_targets_json": self._softphone_targets_json,
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
        new_set.add(HA_SOFTPHONE_ENDPOINT_ENTITY_ID)
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
            old_avail = _state_is_available(old_state)
            new_avail = _state_is_available(new_state)
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
        from . import _async_build_peer_snapshot
        from .phonebook_runtime import (
            format_entry_unified,
            push_roster_json_to_esps,
            registered_roster_entries,
        )
        from .endpoint_routing import roster_from_peers, softphone_targets_from_roster
        from .roster import dump_roster_json
        from .websocket_api import async_prune_ha_softphone_groups

        peers = await _async_build_peer_snapshot(self.hass)
        entries = [format_entry_unified(p) for p in peers]
        roster_entries = roster_from_peers(self.hass, peers, registered_roster_entries(self.hass))
        if await async_prune_ha_softphone_groups(self.hass, roster_entries):
            peers = await _async_build_peer_snapshot(self.hass)
            entries = [format_entry_unified(p) for p in peers]
            roster_entries = roster_from_peers(self.hass, peers, registered_roster_entries(self.hass))
        phonebook = ",".join(entries)
        roster_json = dump_roster_json(roster_entries)
        softphone_targets_json = json.dumps(
            softphone_targets_from_roster(roster_entries),
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        visible_count = len(roster_entries)
        new_value = f"{visible_count} entry" if visible_count == 1 else f"{visible_count} entries"
        if (
            new_value != self._attr_native_value
            or phonebook != self._phonebook
            or roster_json != self._roster_json
            or softphone_targets_json != self._softphone_targets_json
        ):
            self._attr_native_value = new_value
            self._phonebook = phonebook
            self._roster_json = roster_json
            self._softphone_targets_json = softphone_targets_json
            self._count = visible_count
            _LOGGER.debug(
                "Phonebook recomputed (%d entries)", visible_count
            )
            if self.hass and self.entity_id:
                self.async_write_ha_state()
                self.hass.async_create_task(push_roster_json_to_esps(self.hass, roster_json))

    async def async_update(self) -> None:
        await self._recompute()
