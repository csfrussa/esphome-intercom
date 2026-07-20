"""Shared command boundary for Home Assistant phone call services."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ServiceValidationError

from .call_scope import (
    call_belongs_to_endpoint,
    single_pending_route_call_id,
)
from .const import HA_SOFTPHONE_DEVICE_ID
from .endpoint_lifecycle import call_registry
from .esphome_actions import (
    async_call_action,
    async_press_device_button,
    async_resolve_command_phone,
    has_action,
)
from .service_endpoints import (
    async_require_phone_service_control,
    browser_endpoint_name,
    service_browser_endpoint,
)
from .websocket_api import _ha_softphone_store


@dataclass(frozen=True, slots=True)
class BrowserCallCommand:
    """Resolved browser-phone command and its authoritative call scope."""

    endpoint_id: str
    endpoint: Any
    endpoint_name: str
    device_id: str
    call_id: str
    registry: Any


def bind_service_call_controller(
    registry: Any,
    call_id: str,
    call: ServiceCall,
    *,
    endpoint_id: str = "",
) -> None:
    """Bind the initiating HA context before any call events are published."""

    try:
        registry.bind_controller(
            call_id,
            context=getattr(call, "context", None),
            endpoint_id=endpoint_id,
        )
    except ValueError as err:
        raise ServiceValidationError(str(err)) from err


async def async_resolve_browser_call_command(
    hass: HomeAssistant,
    call: ServiceCall,
) -> BrowserCallCommand:
    """Resolve, authorize and scope one browser-phone call command."""

    endpoint_id, endpoint = service_browser_endpoint(hass, call, strict=True)
    await async_require_phone_service_control(hass, call, endpoint=endpoint)
    call_id = str(call.data.get("call_id") or "").strip()
    if not call_id:
        call_id = single_pending_route_call_id(hass, endpoint_id) or str(
            _ha_softphone_store(hass, endpoint_id).get("call_id") or ""
        ).strip()
    registry = call_registry(hass)
    if call_id and not call_belongs_to_endpoint(registry, call_id, endpoint_id):
        raise ServiceValidationError(
            f"call_id {call_id} belongs to another phone endpoint"
        )
    return BrowserCallCommand(
        endpoint_id=endpoint_id,
        endpoint=endpoint,
        endpoint_name=browser_endpoint_name(hass, endpoint_id, endpoint),
        device_id=str(
            getattr(endpoint, "device_id", "") or HA_SOFTPHONE_DEVICE_ID
        ),
        call_id=call_id,
        registry=registry,
    )


async def async_try_esp_answer(call: ServiceCall) -> bool:
    """Answer through a selected ESP phone, returning False for browser calls."""

    hass = call.hass
    device = await async_resolve_command_phone(hass, call)
    if device is None:
        return False
    call_button = str((device.get("entities") or {}).get("call") or "").strip()
    await async_require_phone_service_control(
        hass,
        call,
        device=device,
        action_entity_ids=(call_button,) if call_button else (),
    )
    if not await async_press_device_button(
        hass,
        device,
        "call",
        "SIP answer",
        context=call.context,
    ):
        raise ServiceValidationError(
            f"{device.get('name') or 'ESP phone'} has no answer/call button"
        )
    return True


async def async_try_esp_end_call(
    call: ServiceCall,
    *,
    operation: str,
) -> bool:
    """Decline or hang up through a selected ESP phone."""

    hass = call.hass
    device = await async_resolve_command_phone(hass, call)
    if device is None:
        return False
    decline_button = str(
        (device.get("entities") or {}).get("decline") or ""
    ).strip()
    await async_require_phone_service_control(
        hass,
        call,
        device=device,
        action_entity_ids=(decline_button,) if decline_button else (),
    )
    default_reason = "local_hangup" if operation == "hangup" else ""
    reason = str(
        call.data.get("reason")
        or call.data.get("decline_reason")
        or default_reason
    ).strip()
    if has_action(hass, device, "decline_call"):
        await async_call_action(
            hass,
            device,
            "decline_call",
            {"reason": reason},
            context=call.context,
        )
    elif not await async_press_device_button(
        hass,
        device,
        "decline",
        f"SIP {operation}",
        context=call.context,
    ):
        missing_control = (
            "decline control" if operation == "decline" else "hangup/decline control"
        )
        raise ServiceValidationError(
            f"{device.get('name') or 'ESP phone'} has no {missing_control}"
        )
    return True
