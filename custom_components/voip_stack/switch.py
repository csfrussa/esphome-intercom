"""Switch entities for logical VoIP Stack phone endpoints."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import inspect

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .const import DOMAIN
from .endpoint_device import async_link_endpoint_entity, endpoint_device_info
from .endpoint_entity_manager import (
    EndpointEntityManager,
    register_endpoint_entity_manager,
)


async def async_set_endpoint_dnd(
    hass: HomeAssistant,
    endpoint_id: str,
    enabled: bool,
) -> None:
    """Set DND through the runtime hook or the browser compatibility store."""
    bucket = hass.data.setdefault(DOMAIN, {})
    handler: Callable[[str, bool], Awaitable[None] | None] | None = bucket.get(
        "async_set_endpoint_dnd"
    )
    if handler is not None:
        result = handler(endpoint_id, bool(enabled))
        if inspect.isawaitable(result):
            await result

    registry = bucket.get("endpoint_registry")
    endpoint = registry.get(endpoint_id) if registry is not None else None
    if endpoint is None:
        raise ValueError(f"Unknown phone endpoint: {endpoint_id}")

    if handler is None:
        # Config subentries are the canonical preference store for every
        # integration-owned phone.  Keep the switch durable even when no
        # transport-specific runtime hook is installed (notably registrar
        # accounts, which do not have a browser compatibility store).
        from .phone_config import CONF_PHONE_DND, update_browser_phone_subentry
        from .store import config_entry

        entry = config_entry(hass)
        if entry is not None:
            update_browser_phone_subentry(
                hass,
                entry,
                endpoint_id,
                {CONF_PHONE_DND: bool(enabled)},
            )

    if handler is None and str(getattr(endpoint.kind, "value", endpoint.kind)) == "browser":
        from .websocket_api import (
            _async_save_ha_softphone_store,
            _ha_softphone_store,
            _publish_ha_softphone_state,
        )

        _ha_softphone_store(hass, endpoint_id)["dnd"] = bool(enabled)
        await _async_save_ha_softphone_store(hass, endpoint_id)
        _publish_ha_softphone_state(hass, endpoint_id=endpoint_id)
    current = registry.get(endpoint_id)
    if current is not None and bool(current.dnd) != bool(enabled):
        registry.update(endpoint_id, dnd=bool(enabled))


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    manager = EndpointEntityManager(
        hass,
        entry,
        async_add_entities,
        PhoneEndpointDndSwitch,
    )
    manager.async_setup()
    bucket = hass.data.setdefault(DOMAIN, {})
    register_endpoint_entity_manager(
        entry, bucket, "endpoint_dnd_entity_manager", manager
    )


class PhoneEndpointDndSwitch(SwitchEntity):
    """Do-not-disturb policy for one logical phone."""

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key = "phone_endpoint_dnd"

    def __init__(self, hass, endpoint, registry) -> None:
        self.endpoint = endpoint
        self.registry = registry
        self._attr_unique_id = f"phone_endpoint_{endpoint.endpoint_id}_dnd"
        self._attr_device_info = endpoint_device_info(endpoint)
        self._attr_is_on = bool(endpoint.dnd)

    async def async_added_to_hass(self) -> None:
        await super().async_added_to_hass()
        async_link_endpoint_entity(
            self.registry, self.endpoint.endpoint_id, self.entity_id
        )

    @callback
    def apply_endpoint(self, endpoint) -> None:
        self.endpoint = endpoint
        self._attr_is_on = bool(endpoint.dnd)
        if self.hass is not None:
            self.async_write_ha_state()

    async def async_turn_on(self, **kwargs) -> None:
        await async_set_endpoint_dnd(self.hass, self.endpoint.endpoint_id, True)

    async def async_turn_off(self, **kwargs) -> None:
        await async_set_endpoint_dnd(self.hass, self.endpoint.endpoint_id, False)
