"""Resolve and authorize phone endpoints selected by Home Assistant services."""

from __future__ import annotations

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError

from .authorization import (
    async_require_service_endpoint_control,
    async_require_service_entity_control,
)
from .const import (
    DOMAIN,
    HA_PEER_FALLBACK_NAME,
    HA_SOFTPHONE_DEVICE_ID,
    HA_SOFTPHONE_ENDPOINT_ENTITY_ID,
)
from .phone_endpoint import (
    DEFAULT_ENDPOINT_ID,
    EndpointKind,
    PhoneEndpoint,
)
from .websocket_api import _endpoint_id_from_selector


def _values(raw: object) -> tuple[str, ...]:
    if isinstance(raw, (list, tuple, set, frozenset)):
        values = raw
    else:
        values = (raw,)
    return tuple(
        text for value in values if (text := str(value or "").strip())
    )


def _endpoint_from_entity_registry(hass: HomeAssistant, registry, entity_id: str):
    try:
        from homeassistant.helpers import entity_registry as er

        entity_entry = er.async_get(hass).async_get(entity_id)
    except (AttributeError, ImportError):
        entity_entry = None
    device_id = str(getattr(entity_entry, "device_id", "") or "")
    return registry.by_device_id(device_id) if device_id else None


def service_browser_endpoint(
    hass: HomeAssistant,
    call: ServiceCall,
    *,
    strict: bool = False,
):
    """Resolve the logical HA/browser phone originating a service action."""
    registry = hass.data.get(DOMAIN, {}).get("endpoint_registry")
    explicit_endpoint_id = call.data.get("endpoint_id")
    source_device_id = call.data.get("source_device_id")

    def _browser_selectors(raw: object, lookup_name: str) -> tuple[str, ...]:
        lookup = getattr(registry, lookup_name, None)
        selected: list[str] = []
        for value in _values(raw):
            if value in {
                HA_SOFTPHONE_DEVICE_ID,
                HA_SOFTPHONE_ENDPOINT_ENTITY_ID,
            }:
                selected.append(value)
                continue
            endpoint = lookup(value) if callable(lookup) else None
            if (
                endpoint is None
                and lookup_name == "by_entity_id"
                and registry is not None
            ):
                endpoint = _endpoint_from_entity_registry(hass, registry, value)
            if getattr(endpoint, "kind", None) is EndpointKind.BROWSER:
                selected.append(value)
        return tuple(selected)

    selected_device_ids = _browser_selectors(source_device_id, "by_device_id")
    if not selected_device_ids:
        # ``device_id`` is also Home Assistant's historical target selector.
        # Treat it as the browser source only when it resolves to a browser.
        selected_device_ids = _browser_selectors(
            call.data.get("device_id"),
            "by_device_id",
        )
    selected_entity_ids = _browser_selectors(
        call.data.get("entity_id"),
        "by_entity_id",
    )
    if strict and not explicit_endpoint_id:
        supplied_device_ids = _values(source_device_id or call.data.get("device_id"))
        supplied_entity_ids = _values(call.data.get("entity_id"))
        if (
            supplied_device_ids
            and not selected_device_ids
            or supplied_entity_ids
            and not selected_entity_ids
        ):
            raise ServiceValidationError(
                "The selected Device or Entity is not a Home Assistant browser phone"
            )
    try:
        endpoint_id = _endpoint_id_from_selector(
            hass,
            endpoint_id=explicit_endpoint_id,
            device_id=selected_device_ids,
            entity_id=selected_entity_ids,
        )
    except ValueError as err:
        raise ServiceValidationError(str(err)) from err
    endpoint = None
    get_endpoint = getattr(registry, "get", None)
    if callable(get_endpoint):
        try:
            endpoint = get_endpoint(endpoint_id)
        except (KeyError, ValueError):
            endpoint = None
    if (
        endpoint is not None
        and getattr(endpoint, "kind", None) is not EndpointKind.BROWSER
    ):
        endpoint = None
    return endpoint_id, endpoint


def service_configured_endpoint(hass: HomeAssistant, call: ServiceCall):
    """Resolve one integration-owned browser or registrar-account phone."""
    registry = hass.data.get(DOMAIN, {}).get("endpoint_registry")

    explicit = str(call.data.get("endpoint_id") or "").strip()
    selected: dict[str, object] = {}
    unresolved: list[str] = []
    if explicit:
        endpoint = registry.get(explicit) if registry is not None else None
        if endpoint is None and explicit.casefold() == DEFAULT_ENDPOINT_ID:
            endpoint = (
                registry.get(DEFAULT_ENDPOINT_ID) if registry is not None else None
            )
        if endpoint is None and registry is not None:
            unresolved.append(explicit)
        elif endpoint is not None:
            selected[endpoint.endpoint_id] = endpoint

    for lookup_name, raw, legacy in (
        ("by_device_id", call.data.get("device_id"), HA_SOFTPHONE_DEVICE_ID),
        (
            "by_entity_id",
            call.data.get("entity_id"),
            HA_SOFTPHONE_ENDPOINT_ENTITY_ID,
        ),
    ):
        lookup = getattr(registry, lookup_name, None)
        for value in _values(raw):
            if value == legacy:
                if registry is None:
                    continue
                endpoint = registry.get(DEFAULT_ENDPOINT_ID)
            else:
                endpoint = lookup(value) if callable(lookup) else None
                if (
                    endpoint is None
                    and lookup_name == "by_entity_id"
                    and registry is not None
                ):
                    endpoint = _endpoint_from_entity_registry(hass, registry, value)
            if endpoint is None:
                unresolved.append(value)
                continue
            selected[endpoint.endpoint_id] = endpoint

    if registry is None:
        if unresolved or (
            explicit and explicit.casefold() != DEFAULT_ENDPOINT_ID
        ):
            raise ServiceValidationError("Unknown Home Assistant phone selector")
        return DEFAULT_ENDPOINT_ID, None
    if unresolved:
        raise ServiceValidationError(
            "Unknown Home Assistant phone selector: " + ", ".join(unresolved)
        )
    if not selected:
        endpoint = registry.get(DEFAULT_ENDPOINT_ID)
        if endpoint is None:
            raise ServiceValidationError(
                "The default Home Assistant phone is unavailable"
            )
        selected[endpoint.endpoint_id] = endpoint
    if len(selected) != 1:
        raise ServiceValidationError(
            "Selected endpoint, Device and Entity do not identify the same phone"
        )
    endpoint = next(iter(selected.values()))
    if endpoint.kind not in {EndpointKind.BROWSER, EndpointKind.SIP_ACCOUNT}:
        raise ServiceValidationError(
            "The selected Device or Entity is not an integration-owned phone"
        )
    return endpoint.endpoint_id, endpoint


def browser_endpoint_name(
    hass: HomeAssistant,
    endpoint_id: str,
    endpoint=None,
) -> str:
    """Return the stable display name of a browser phone."""
    del endpoint_id
    fallback = (hass.config.location_name or "").strip() or HA_PEER_FALLBACK_NAME
    return str(getattr(endpoint, "name", "") or fallback).strip()


async def async_require_phone_service_control(
    hass: HomeAssistant,
    call: ServiceCall,
    *,
    endpoint=None,
    device: dict | None = None,
    action_entity_ids: tuple[str, ...] | None = None,
) -> None:
    """Apply per-phone HA permissions after the integration-wide boundary."""
    if endpoint is None and device is not None:
        registry = hass.data.get(DOMAIN, {}).get("endpoint_registry")
        endpoint = (
            registry.by_device_id(str(device.get("device_id") or ""))
            if registry is not None
            else None
        )
        if endpoint is None:
            device_id = str(device.get("device_id") or "").strip()
            entities = frozenset(
                str(value)
                for value in (device.get("entities") or {}).values()
                if isinstance(value, str) and "." in value
            )
            # Resolution can precede roster discovery. Authorize against an
            # ephemeral descriptor instead of failing open on a global entity.
            endpoint = PhoneEndpoint(
                endpoint_id=str(
                    device.get("endpoint_id") or f"esphome:{device_id}"
                ),
                name=str(device.get("name") or device_id or "ESP phone"),
                kind=EndpointKind.ESPHOME,
                device_id=device_id,
                entity_ids=entities,
                capabilities=frozenset({"audio", "dtmf"}),
            )
    if endpoint is not None:
        await async_require_service_endpoint_control(hass, call, endpoint)
    if action_entity_ids is not None:
        await async_require_service_entity_control(hass, call, action_entity_ids)
