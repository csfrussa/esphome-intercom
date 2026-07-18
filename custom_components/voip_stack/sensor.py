"""Sensor platform for VoIP Stack.

HA publishes one SIP dial-plan roster.

Entity names:
  - sensor.voip_phonebook       format per row:
      name|ip|sip_port|rtp_port|audio_mode|tx_formats|rx_formats|sip_tcp

ESP YAMLs subscribe to the unified sensor and normalize it locally into their
SIP dial plan.
"""
import asyncio
import contextlib
import logging

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import Event, HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_state_change_event

from .audio_format import HA_SIP_PCM_FORMATS
from .const import (
    DOMAIN,
    HA_SOFTPHONE_CALL_STATE_ENTITY_ID,
    HA_SOFTPHONE_ENDPOINT_ENTITY_ID,
)
from .endpoint_device import (
    async_link_endpoint_entity,
    endpoint_config_subentry_id,
    endpoint_device_info,
    endpoint_public_attributes,
    enum_value,
)
from .endpoint_entity_manager import (
    EndpointEntityManager,
    event_projects_endpoint_state,
    register_endpoint_entity_manager,
)
from .phone_endpoint import DEFAULT_ENDPOINT_ID

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 0
UNAVAILABLE_STATES = {"", "unknown", "unavailable"}
HA_ENDPOINT_AUDIO_FORMATS = tuple(fmt.wire_token() for fmt in HA_SIP_PCM_FORMATS[:8])
PHONE_CALL_STATES = [
    "offline",
    "idle",
    "calling",
    "remote_ringing",
    "ringing",
    "connecting",
    "in_call",
    "held",
    "terminating",
]
TERMINAL_CALL_STATES = {
    "idle",
    "busy",
    "declined",
    "cancelled",
    "media_incompatible",
    "transport_unreachable",
    "auth_required_unsupported",
    "protocol_error",
    "error",
}


def _state_is_available(state) -> bool:
    return state is not None and str(state.state or "").strip().lower() not in UNAVAILABLE_STATES


def _is_voip_roster_entity(entity_id: str) -> bool:
    return any(
        token in entity_id
        for token in (
            "voip_state",
            "voip_endpoint",
            "voip_ring_groups",
            "voip_conference_groups",
            "voip_ring_on_conference",
        )
    )


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    registry = hass.data.get(DOMAIN, {}).get("endpoint_registry")
    default_endpoint = registry.get(DEFAULT_ENDPOINT_ID) if registry is not None else None
    ha_endpoint_sensor = HaSoftphoneEndpointSensor(hass, default_endpoint, registry)
    call_state_sensor = HaSoftphoneCallStateSensor(
        hass, entry, default_endpoint, registry
    )
    unified_sensor = VoipPhonebookSensor(hass)
    async_add_entities(
        [ha_endpoint_sensor, call_state_sensor],
        True,
        config_subentry_id=endpoint_config_subentry_id(
            hass, DEFAULT_ENDPOINT_ID
        ),
    )
    async_add_entities([unified_sensor], True)
    endpoint_manager = EndpointEntityManager(
        hass,
        entry,
        async_add_entities,
        PhoneEndpointCallStateSensor,
        include_default=False,
    )
    endpoint_manager.async_setup()
    bucket = hass.data.setdefault(DOMAIN, {})
    bucket["ha_softphone_endpoint_sensor"] = ha_endpoint_sensor
    bucket["ha_softphone_call_state_sensor"] = call_state_sensor
    bucket["phonebook_sensor"] = unified_sensor
    register_endpoint_entity_manager(
        entry, bucket, "endpoint_call_state_entity_manager", endpoint_manager
    )


class HaSoftphoneCallStateSensor(SensorEntity):
    """Durable state of the current HA-controlled logical SIP call."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_has_entity_name = False
    _attr_options = PHONE_CALL_STATES
    _attr_should_poll = False
    _attr_icon = "mdi:phone-in-talk"

    def __init__(
        self, hass: HomeAssistant, entry: ConfigEntry, endpoint=None, registry=None
    ) -> None:
        self.hass = hass
        self._attr_unique_id = "voip_stack_ha_softphone_call_state"
        self._attr_name = "VoIP Stack Call State"
        self.entity_id = HA_SOFTPHONE_CALL_STATE_ENTITY_ID
        self._attr_native_value = "idle"
        self._attr_extra_state_attributes: dict[str, object] = {}
        self._active_call_id = ""
        self._revision = -1
        self.endpoint = endpoint
        self.endpoint_registry = registry
        self._attr_device_info = endpoint_device_info(endpoint) if endpoint is not None else DeviceInfo(
            identifiers={(DOMAIN, entry.entry_id)},
            name=entry.title or "VoIP Stack",
        )

    async def async_added_to_hass(self) -> None:
        from .websocket_api import CALL_EVENT, _ha_softphone_state

        await super().async_added_to_hass()
        async_link_endpoint_entity(
            self.endpoint_registry, DEFAULT_ENDPOINT_ID, self.entity_id
        )
        self._apply_snapshot(_ha_softphone_state(self.hass, DEFAULT_ENDPOINT_ID))
        self.async_on_remove(self.hass.bus.async_listen(CALL_EVENT, self._async_state_event))

    @callback
    def _async_state_event(self, event: Event) -> None:
        snapshot = dict(event.data)
        endpoint_id = str(snapshot.get("endpoint_id") or DEFAULT_ENDPOINT_ID)
        if endpoint_id != DEFAULT_ENDPOINT_ID:
            return
        if self.endpoint is not None:
            if not event_projects_endpoint_state(
                snapshot,
                self.endpoint,
                self.endpoint_registry,
            ):
                return
        elif str(snapshot.get("scope") or "") != "session":
            return
        call_id = str(snapshot.get("call_id") or "").strip()
        state = str(snapshot.get("state") or "idle")
        terminal = state in TERMINAL_CALL_STATES
        incoming_revision = int(snapshot.get("revision") or snapshot.get("sequence") or 0)
        if (
            call_id
            and self._active_call_id
            and call_id != self._active_call_id
            and self._attr_native_value != "idle"
        ):
            return
        if (
            not terminal
            and call_id == self._active_call_id
            and incoming_revision < self._revision
        ):
            return
        self.async_set_context(event.context)
        self._apply_snapshot(snapshot)
        self.async_write_ha_state()

    @callback
    def _apply_snapshot(self, snapshot: dict[str, object]) -> None:
        state = str(snapshot.get("state") or "idle")
        terminal_reason = str(snapshot.get("terminal_reason") or snapshot.get("reason") or "")
        call_id = str(snapshot.get("call_id") or "").strip()
        revision = int(snapshot.get("revision") or snapshot.get("sequence") or 0)
        terminal = state in TERMINAL_CALL_STATES
        self._attr_native_value = "idle" if terminal else state
        if call_id:
            self._active_call_id = call_id
            self._revision = revision
        if terminal and terminal_reason != "forwarded":
            self._active_call_id = ""
            self._revision = -1
        self._attr_extra_state_attributes = {
            key: snapshot.get(key, "")
            for key in (
                "call_id",
                "caller",
                "callee",
                "direction",
                "dialed_target",
                "peer_name",
                "sequence",
                "revision",
                "owner",
                "terminal_reason",
            )
        }


class HaSoftphoneEndpointSensor(SensorEntity):
    """Local HA softphone endpoint, published in the same shape as ESP endpoints."""

    _attr_has_entity_name = False
    _attr_should_poll = False
    _attr_icon = "mdi:phone-voip"

    def __init__(self, hass: HomeAssistant, endpoint=None, registry=None) -> None:
        self.hass = hass
        self._attr_unique_id = "voip_stack_ha_softphone_voip_endpoint"
        self._attr_name = "VoIP Stack HA Softphone Endpoint"
        self.entity_id = HA_SOFTPHONE_ENDPOINT_ENTITY_ID
        self._attr_native_value = "unknown"
        self._attr_extra_state_attributes = {"local_ha": True}
        self.endpoint_registry = registry
        if endpoint is not None:
            self._attr_device_info = endpoint_device_info(endpoint)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        async_link_endpoint_entity(
            self.endpoint_registry, DEFAULT_ENDPOINT_ID, self.entity_id
        )

    async def async_update(self) -> None:
        from . import _get_transport_config, _ha_advertise_host
        from .websocket_api import _ha_peer_name, _ha_softphone_extension, _ha_softphone_groups

        host = await _ha_advertise_host(self.hass)
        if not host:
            self._attr_native_value = "unavailable"
            self._attr_extra_state_attributes = {"local_ha": True, "available": False, "endpoint": ""}
            if self.hass and self.entity_id:
                self.async_write_ha_state()
            return

        cfg = _get_transport_config(self.hass)
        groups = _ha_softphone_groups(self.hass)
        extension = _ha_softphone_extension(self.hass)
        tx = ";".join(HA_ENDPOINT_AUDIO_FORMATS)
        rx = tx
        endpoint = (
            f"{_ha_peer_name(self.hass)}|{host}|{int(cfg['sip_port'])}|{int(cfg['rtp_port'])}|"
            f"full_duplex|{tx}|{rx}|sip_tcp|{extension}"
        )
        self._attr_native_value = "online"
        self._attr_extra_state_attributes = {
            "local_ha": True,
            "available": True,
            "endpoint": endpoint,
            "extension": extension,
            "ring_group": groups["ring_group"],
            "conference_group": groups["conference_group"],
            "conference_ring": bool(groups["conference_ring"]),
        }
        if self.hass and self.entity_id:
            self.async_write_ha_state()


class PhoneEndpointCallStateSensor(SensorEntity):
    """Durable, automation-friendly state for one logical phone endpoint."""

    _attr_device_class = SensorDeviceClass.ENUM
    _attr_has_entity_name = True
    _attr_options = PHONE_CALL_STATES
    _attr_should_poll = False
    _attr_translation_key = "phone_endpoint_call_state"

    def __init__(self, hass, endpoint, registry) -> None:
        self.endpoint = endpoint
        self.registry = registry
        self._attr_unique_id = f"phone_endpoint_{endpoint.endpoint_id}_call_state"
        self._attr_device_info = endpoint_device_info(endpoint)
        self._attr_native_value = self._idle_state(endpoint)
        self._attr_extra_state_attributes = endpoint_public_attributes(endpoint)

    async def async_added_to_hass(self) -> None:
        from .websocket_api import CALL_EVENT, _ha_softphone_state

        await super().async_added_to_hass()
        async_link_endpoint_entity(
            self.registry, self.endpoint.endpoint_id, self.entity_id
        )
        if enum_value(self.endpoint.kind) == "browser":
            self._apply_call_payload(
                _ha_softphone_state(self.hass, self.endpoint.endpoint_id)
            )
        self.async_on_remove(self.hass.bus.async_listen(CALL_EVENT, self._on_call_event))

    @staticmethod
    def _idle_state(endpoint) -> str:
        availability = enum_value(endpoint.availability)
        return "idle" if availability in {"online", "available", "registered", "connected"} else "offline"

    @callback
    def apply_endpoint(self, endpoint) -> None:
        self.endpoint = endpoint
        if not endpoint.active_call_id or self._attr_native_value in {"idle", "offline"}:
            self._attr_native_value = self._idle_state(endpoint)
        self._attr_extra_state_attributes = endpoint_public_attributes(endpoint)
        if self.hass is not None:
            self.async_write_ha_state()

    @callback
    def _on_call_event(self, event: Event) -> None:
        payload = dict(event.data)
        if not event_projects_endpoint_state(
            payload,
            self.endpoint,
            self.registry,
        ):
            return
        self._apply_call_payload(payload)
        self.async_set_context(event.context)
        self.async_write_ha_state()

    @callback
    def _apply_call_payload(self, payload: dict[str, object]) -> None:
        state = str(payload.get("state") or "idle").strip().lower()
        if state in TERMINAL_CALL_STATES:
            state = self._idle_state(self.endpoint)
        elif state not in PHONE_CALL_STATES:
            state = self._attr_native_value
        self._attr_native_value = state
        attributes = endpoint_public_attributes(self.endpoint)
        attributes.update(
            {
                key: payload.get(key, "")
                for key in (
                    "call_id",
                    "direction",
                    "peer_name",
                    "terminal_reason",
                )
                if payload.get(key) not in (None, "")
            }
        )
        self._attr_extra_state_attributes = attributes


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
        self._roster_json = '{"version":2,"capabilities":["extension","ring_group","conference_group","conference_ring"],"contacts":[]}'
        self._count = 0
        self._tracked_entities: set[str] = set()
        self._unsub_state = None
        self._unsub_registry = None
        self._recompute_task: asyncio.Task | None = None
        self._recompute_requested = False

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
            if not _is_voip_roster_entity(entity_id):
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
        if self._recompute_task is not None:
            self._recompute_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._recompute_task
            self._recompute_task = None

    @callback
    def _schedule_recompute(self) -> None:
        """Coalesce state bursts while guaranteeing a final fresh snapshot."""
        self._recompute_requested = True
        if self._recompute_task is None or self._recompute_task.done():
            self._recompute_task = self.hass.async_create_task(self._drain_recomputes())

    async def _drain_recomputes(self) -> None:
        current = asyncio.current_task()
        try:
            while self._recompute_requested:
                self._recompute_requested = False
                await self._recompute()
        finally:
            if self._recompute_task is current:
                self._recompute_task = None
                if self._recompute_requested:
                    self._schedule_recompute()

    async def _schedule_and_wait_recompute(self) -> None:
        self._schedule_recompute()
        task = self._recompute_task
        if task is not None:
            await task

    async def _refresh_tracked_entities(self, initial: bool = False) -> None:
        entity_registry = er.async_get(self.hass)
        new_set = {
            e.entity_id
            for e in entity_registry.entities.values()
            if _is_voip_roster_entity(e.entity_id)
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
                old_endpoint = (old_state.attributes or {}).get("endpoint") if old_state is not None else None
                new_endpoint = (new_state.attributes or {}).get("endpoint") if new_state is not None else None
                if old_value != new_value or old_endpoint != new_endpoint:
                    self._schedule_recompute()
                return
            if (
                "voip_ring_groups" in entity_id
                or "voip_conference_groups" in entity_id
                or "voip_ring_on_conference" in entity_id
            ):
                old_value = old_state.state if old_state is not None else None
                new_value = new_state.state if new_state is not None else None
                if old_value != new_value:
                    self._schedule_recompute()
                return
            old_avail = _state_is_available(old_state)
            new_avail = _state_is_available(new_state)
            if old_avail == new_avail:
                return
            self._schedule_recompute()

        if self._unsub_state:
            self._unsub_state()
            self._unsub_state = None
        if new_set:
            self._unsub_state = async_track_state_change_event(
                self.hass, list(new_set), _on_state_change
            )
        await self._schedule_and_wait_recompute()

    async def _recompute(self) -> None:
        from . import _async_build_peer_snapshot
        from .phonebook_runtime import (
            format_entry_unified,
            push_roster_json_to_esps,
            registered_roster_entries,
        )
        from .endpoint_routing import roster_from_peers
        from .roster import dump_roster_json

        peers = await _async_build_peer_snapshot(self.hass)
        entries = [format_entry_unified(p) for p in peers]
        roster_entries = roster_from_peers(self.hass, peers, registered_roster_entries(self.hass))
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
                await push_roster_json_to_esps(self.hass, roster_json)

    async def async_update(self) -> None:
        await self._schedule_and_wait_recompute()
