"""Optional Home Assistant Assist intent handlers for Home Assistant VoIP Stack."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from homeassistant.core import HomeAssistant
from homeassistant.helpers import intent
from homeassistant.helpers.intent import Intent, IntentHandler, IntentResponse

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

INTENT_CALL = "VoipCall"
INTENT_HANGUP = "VoipHangup"
INTENT_ANSWER = "VoipAnswer"
INTENT_DECLINE = "VoipDecline"
INTENT_TYPES = (INTENT_CALL, INTENT_HANGUP, INTENT_ANSWER, INTENT_DECLINE)


@dataclass(frozen=True)
class ContactResolution:
    """Result of resolving a spoken contact name against the live phonebook."""

    canonical: str = ""
    error: str = ""
    matches: tuple[str, ...] = ()
    source: str = "contact"


def _slot_value(intent_obj: Intent, name: str) -> str:
    slot = intent_obj.slots.get(name) or {}
    value = slot.get("value", "") if isinstance(slot, dict) else ""
    return str(value or "").strip()


def _normalize_contact(value: str) -> str:
    return " ".join(str(value or "").strip().casefold().split())


def _resolve_contact_name(raw_name: str, contacts: list[str]) -> ContactResolution:
    """Resolve a spoken contact to exactly one canonical phonebook contact."""
    normalized = _normalize_contact(raw_name)
    if not normalized:
        return ContactResolution(error="missing")

    matches: list[str] = []
    seen: set[str] = set()
    for contact in contacts:
        canonical = str(contact or "").strip()
        if not canonical:
            continue
        if _normalize_contact(canonical) != normalized:
            continue
        if canonical in seen:
            continue
        seen.add(canonical)
        matches.append(canonical)

    if not matches:
        return ContactResolution(error="not_found")
    if len(matches) > 1:
        return ContactResolution(error="ambiguous", matches=tuple(matches))
    return ContactResolution(canonical=matches[0], matches=(matches[0],))


def _response(intent_obj: Intent, speech: str) -> IntentResponse:
    response = intent_obj.create_response()
    response.async_set_speech(speech)
    return response


async def _voip_devices(hass: HomeAssistant) -> list[dict[str, Any]]:
    from .websocket_api import _get_voip_devices

    return await _get_voip_devices(hass)


async def _origin_device(intent_obj: Intent) -> dict[str, Any] | None:
    device_id = str(intent_obj.device_id or "").strip()
    if not device_id:
        return None
    for device in await _voip_devices(intent_obj.hass):
        if device.get("device_id") == device_id:
            return device
    return None


async def _phonebook_contacts(hass: HomeAssistant) -> list[str]:
    devices = await _voip_devices(hass)
    contacts = [str(device.get("name") or "").strip() for device in devices]

    ha_phonebook = hass.states.get("sensor.voip_phonebook")
    raw = ""
    if ha_phonebook is not None:
        raw = str(ha_phonebook.attributes.get("phonebook") or "")
    for row in raw.split(","):
        name = row.split("|", 1)[0].strip()
        if name:
            contacts.append(name)

    out: list[str] = []
    seen: set[str] = set()
    for contact in contacts:
        if contact and contact not in seen:
            seen.add(contact)
            out.append(contact)
    return out


async def _resolve_area_contact(hass: HomeAssistant, raw_name: str) -> ContactResolution:
    """Resolve a spoken area name to one VoIP device in that HA area."""
    from homeassistant.helpers import area_registry as ar
    from homeassistant.helpers import device_registry as dr

    normalized = _normalize_contact(raw_name)
    if not normalized:
        return ContactResolution(error="missing", source="area")

    area_registry = ar.async_get(hass)
    matching_areas = [
        area
        for area in area_registry.async_list_areas()
        if _normalize_contact(area.name) == normalized
    ]
    if not matching_areas:
        return ContactResolution(error="not_found", source="area")
    if len(matching_areas) > 1:
        return ContactResolution(
            error="ambiguous_area",
            matches=tuple(area.name for area in matching_areas),
            source="area",
        )

    device_registry = dr.async_get(hass)
    area_id = matching_areas[0].id
    devices = []
    for device in await _voip_devices(hass):
        registry_device = device_registry.async_get(device["device_id"])
        if registry_device is None or registry_device.area_id != area_id:
            continue
        devices.append(str(device.get("name") or "").strip())

    devices = [name for name in devices if name]
    if not devices:
        return ContactResolution(error="area_empty", source="area")
    if len(devices) > 1:
        return ContactResolution(
            error="ambiguous_area_device",
            matches=tuple(devices),
            source="area",
        )
    return ContactResolution(canonical=devices[0], matches=(devices[0],), source="area")


async def _resolve_contact_or_area(hass: HomeAssistant, raw_name: str) -> ContactResolution:
    """Resolve target first as a phonebook contact, then as a HA area."""
    contacts = await _phonebook_contacts(hass)
    contact = _resolve_contact_name(raw_name, contacts)
    if contact.error != "not_found":
        return contact
    area = await _resolve_area_contact(hass, raw_name)
    if area.error == "not_found":
        return contact
    return area


def _ha_peer_name(hass: HomeAssistant) -> str:
    from . import _ha_peer_name as resolve_ha_peer_name

    return resolve_ha_peer_name(hass)


async def _start_call_to_ha_peer(
    hass: HomeAssistant,
    origin: dict[str, Any],
    dest_name: str,
) -> None:
    """Ask the originating ESP to call HA by its phonebook peer name."""
    from . import _available_esphome_services

    route_id = str(origin.get("route_id") or "").strip()
    if not route_id:
        raise ValueError(f"No route_id for VoIP device {origin.get('name')}")

    service_name = f"{route_id}_start_call"
    if service_name not in _available_esphome_services(hass):
        raise ValueError(f"ESPHome service esphome.{service_name} is not registered")

    await hass.services.async_call(
        "esphome",
        service_name,
        {"dest": dest_name},
        blocking=True,
    )


class _VoipIntentHandler(IntentHandler):
    """Base handler for local-satellite VoIP commands."""

    intent_type: str = ""

    async def _require_origin(self, intent_obj: Intent) -> dict[str, Any] | IntentResponse:
        origin = await _origin_device(intent_obj)
        if origin is None:
            return _response(intent_obj, "I do not know which VoIP device heard that.")
        return origin


class VoipCallIntentHandler(_VoipIntentHandler):
    """Start an VoIP call from the satellite that heard the command."""

    intent_type = INTENT_CALL

    async def async_handle(self, intent_obj: Intent) -> IntentResponse:
        origin = await self._require_origin(intent_obj)
        if isinstance(origin, IntentResponse):
            return origin

        spoken_target = _slot_value(intent_obj, "target")
        resolved = await _resolve_contact_or_area(intent_obj.hass, spoken_target)
        if resolved.error == "missing":
            return _response(intent_obj, "Which VoIP contact should I call?")
        if resolved.error == "not_found":
            return _response(intent_obj, f"I cannot find an VoIP contact named {spoken_target}.")
        if resolved.error == "ambiguous":
            return _response(intent_obj, f"The VoIP contact {spoken_target} is ambiguous.")
        if resolved.error == "ambiguous_area":
            return _response(intent_obj, f"The Home Assistant area {spoken_target} is ambiguous.")
        if resolved.error == "area_empty":
            return _response(intent_obj, f"The Home Assistant area {spoken_target} has no VoIP device.")
        if resolved.error == "ambiguous_area_device":
            return _response(
                intent_obj,
                f"The Home Assistant area {spoken_target} has more than one VoIP device.",
            )

        try:
            if _normalize_contact(resolved.canonical) == _normalize_contact(_ha_peer_name(intent_obj.hass)):
                await _start_call_to_ha_peer(intent_obj.hass, origin, resolved.canonical)
            else:
                await intent_obj.hass.services.async_call(
                    DOMAIN,
                    "call",
                    {
                        "name": resolved.canonical,
                        "source": origin["device_id"],
                    },
                    blocking=True,
                )
        except Exception as err:
            _LOGGER.error(
                "Assist VoIP call failed: %s -> %s (spoken=%r): %s",
                origin.get("name"),
                resolved.canonical,
                spoken_target,
                err,
            )
            return _response(intent_obj, f"I could not call {resolved.canonical}.")
        _LOGGER.info(
            "Assist VoIP call: %s -> %s (spoken=%r source=%s)",
            origin.get("name"),
            resolved.canonical,
            spoken_target,
            resolved.source,
        )
        return _response(intent_obj, f"Calling {resolved.canonical}.")


class VoipHangupIntentHandler(_VoipIntentHandler):
    """Hang up the call owned by the satellite that heard the command."""

    intent_type = INTENT_HANGUP

    async def async_handle(self, intent_obj: Intent) -> IntentResponse:
        origin = await self._require_origin(intent_obj)
        if isinstance(origin, IntentResponse):
            return origin
        try:
            await intent_obj.hass.services.async_call(
                DOMAIN,
                "hangup",
                {"device_id": origin["device_id"]},
                blocking=True,
            )
        except Exception as err:
            _LOGGER.error("Assist VoIP hangup failed for %s: %s", origin.get("name"), err)
            return _response(intent_obj, "I could not hang up the VoIP call.")
        return _response(intent_obj, "OK.")


class VoipAnswerIntentHandler(_VoipIntentHandler):
    """Answer the call on the satellite that heard the command."""

    intent_type = INTENT_ANSWER

    async def async_handle(self, intent_obj: Intent) -> IntentResponse:
        origin = await self._require_origin(intent_obj)
        if isinstance(origin, IntentResponse):
            return origin
        try:
            await intent_obj.hass.services.async_call(
                DOMAIN,
                "answer",
                {"device_id": origin["device_id"]},
                blocking=True,
            )
        except Exception as err:
            _LOGGER.error("Assist VoIP answer failed for %s: %s", origin.get("name"), err)
            return _response(intent_obj, "I could not answer the VoIP call.")
        return _response(intent_obj, "Answering.")


class VoipDeclineIntentHandler(_VoipIntentHandler):
    """Decline the call on the satellite that heard the command."""

    intent_type = INTENT_DECLINE

    async def async_handle(self, intent_obj: Intent) -> IntentResponse:
        origin = await self._require_origin(intent_obj)
        if isinstance(origin, IntentResponse):
            return origin
        try:
            await intent_obj.hass.services.async_call(
                DOMAIN,
                "decline",
                {
                    "device_id": origin["device_id"],
                    "reason": "declined by voice command",
                },
                blocking=True,
            )
        except Exception as err:
            _LOGGER.error("Assist VoIP decline failed for %s: %s", origin.get("name"), err)
            return _response(intent_obj, "I could not decline the VoIP call.")
        return _response(intent_obj, "Declining.")


def async_register_assist_intents(hass: HomeAssistant) -> None:
    """Register optional Assist handlers."""
    for handler in (
        VoipCallIntentHandler(),
        VoipHangupIntentHandler(),
        VoipAnswerIntentHandler(),
        VoipDeclineIntentHandler(),
    ):
        intent.async_register(hass, handler)
    hass.data.setdefault(DOMAIN, {})["assist_intents_registered"] = True
    _LOGGER.info("Home Assistant VoIP Stack Assist intents registered")


def async_unregister_assist_intents(hass: HomeAssistant) -> None:
    """Unregister optional Assist handlers."""
    for intent_type in INTENT_TYPES:
        intent.async_remove(hass, intent_type)
    hass.data.setdefault(DOMAIN, {}).pop("assist_intents_registered", None)
    _LOGGER.info("Home Assistant VoIP Stack Assist intents unregistered")
