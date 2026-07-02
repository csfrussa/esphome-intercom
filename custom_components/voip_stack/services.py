"""Home Assistant service schemas and registration for VoIP Stack."""

from __future__ import annotations

import voluptuous as vol

from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.helpers import config_validation as cv

from .const import DOMAIN


async def async_register_services(hass: HomeAssistant, handlers: dict[str, object]) -> None:
    target_fields = {
        vol.Optional("device_id"): vol.Any(cv.string, [cv.string]),
        vol.Optional("entity_id"): vol.Any(cv.entity_id, [cv.entity_id]),
        vol.Optional("name"): cv.string,
        vol.Optional("friendly_name"): cv.string,
    }
    purge_schema = vol.Schema(
        {**target_fields, vol.Optional("min_unavailable_hours", default=0): vol.Coerce(float)},
        extra=vol.PREVENT_EXTRA,
    )
    sip_answer_schema = vol.Schema(
        {
            **target_fields,
            vol.Optional("source"): cv.string,
            vol.Optional("source_device_id"): cv.string,
            vol.Optional("source_name"): cv.string,
            vol.Optional("call_id", default=""): cv.string,
        },
        extra=vol.PREVENT_EXTRA,
    )
    sip_decline_schema = vol.Schema(
        {
            **target_fields,
            vol.Optional("source"): cv.string,
            vol.Optional("source_device_id"): cv.string,
            vol.Optional("source_name"): cv.string,
            vol.Optional("call_id", default=""): cv.string,
            vol.Optional("status", default=603): vol.Coerce(int),
            vol.Optional("reason", default="Decline"): cv.string,
            vol.Optional("decline_reason", default=""): cv.string,
        },
        extra=vol.PREVENT_EXTRA,
    )
    sip_hangup_schema = vol.Schema(
        {
            **target_fields,
            vol.Optional("source"): cv.string,
            vol.Optional("source_device_id"): cv.string,
            vol.Optional("source_name"): cv.string,
            vol.Optional("call_id", default=""): cv.string,
            vol.Optional("reason", default="local_hangup"): cv.string,
        },
        extra=vol.PREVENT_EXTRA,
    )
    sip_call_schema = vol.Schema(
        {
            **target_fields,
            vol.Optional("source"): cv.string,
            vol.Optional("source_device_id"): cv.string,
            vol.Optional("source_name"): cv.string,
            vol.Optional("call_id", default=""): cv.string,
            vol.Optional("destination"): cv.string,
            vol.Optional("target"): cv.string,
            vol.Optional("call"): cv.string,
            vol.Optional("ha_bridge", default=False): cv.boolean,
        },
        extra=vol.PREVENT_EXTRA,
    )
    sip_route_schema = vol.Schema(
        {
            vol.Required("call_id"): cv.string,
            vol.Optional("action", default="default"): vol.In(
                ["answer_ha", "decline", "busy", "forward", "bridge", "default", "cancel"]
            ),
            vol.Optional("destination"): cv.string,
            vol.Optional("target"): cv.string,
            vol.Optional("call"): cv.string,
            vol.Optional("status", default=0): vol.Coerce(int),
            vol.Optional("reason", default=""): cv.string,
            vol.Optional("decline_reason", default=""): cv.string,
        },
        extra=vol.PREVENT_EXTRA,
    )
    phonebook_add_schema = vol.Schema(
        {
            vol.Required("name"): cv.string,
            vol.Optional("id", default=""): cv.string,
            vol.Optional("kind", default=""): vol.Any("", vol.In(["ha", "esp", "phone", "softphone", "group"])),
            vol.Optional("address", default=""): cv.string,
            vol.Optional("sip_uri", default=""): cv.string,
            vol.Optional("number", default=""): cv.string,
            vol.Optional("ha_bridge", default=False): cv.boolean,
            vol.Optional("sip_transport", default=""): vol.Any("", vol.In(["tcp", "udp"])),
            vol.Optional("signaling_transport", default=""): vol.Any("", vol.In(["tcp", "udp"])),
            vol.Optional("sip_port"): vol.Coerce(int),
            vol.Optional("rtp_port"): vol.Coerce(int),
            vol.Optional("tx_rate"): vol.Any("auto", vol.Coerce(int)),
            vol.Optional("rx_rate"): vol.Any("auto", vol.Coerce(int)),
            vol.Optional("tx_formats"): vol.Any(cv.string, [cv.string]),
            vol.Optional("rx_formats"): vol.Any(cv.string, [cv.string]),
            vol.Optional("max_payload_bytes"): vol.Coerce(int),
            vol.Optional("audio_mode", default=""): cv.string,
        },
        extra=vol.PREVENT_EXTRA,
    )
    phonebook_remove_schema = vol.Schema({vol.Required("name"): cv.string}, extra=vol.PREVENT_EXTRA)
    phonebook_set_schema = vol.Schema({vol.Required("roster_json"): cv.string}, extra=vol.PREVENT_EXTRA)
    set_dnd_schema = vol.Schema({vol.Required("dnd"): cv.boolean}, extra=vol.PREVENT_EXTRA)
    sip_account_create_schema = vol.Schema(
        {
            vol.Required("username"): cv.string,
            vol.Optional("display_name", default=""): cv.string,
            vol.Optional("password", default=""): cv.string,
            vol.Optional("enabled", default=True): cv.boolean,
            vol.Optional("replace", default=False): cv.boolean,
        },
        extra=vol.PREVENT_EXTRA,
    )
    sip_account_name_schema = vol.Schema({vol.Required("username"): cv.string}, extra=vol.PREVENT_EXTRA)

    def handler_for(name: str):
        async def _handle(call: ServiceCall) -> None:
            await handlers[name](call)

        return _handle

    hass.services.async_register(DOMAIN, "purge_devices", handler_for("purge_devices"), schema=purge_schema)
    hass.services.async_register(DOMAIN, "answer", handler_for("answer"), schema=sip_answer_schema)
    hass.services.async_register(DOMAIN, "decline", handler_for("decline"), schema=sip_decline_schema)
    hass.services.async_register(DOMAIN, "hangup", handler_for("hangup"), schema=sip_hangup_schema)
    hass.services.async_register(DOMAIN, "call", handler_for("call"), schema=sip_call_schema)
    hass.services.async_register(DOMAIN, "forward", handler_for("forward"), schema=sip_call_schema)
    hass.services.async_register(DOMAIN, "route", handler_for("route"), schema=sip_route_schema)
    hass.services.async_register(DOMAIN, "add_contact", handler_for("add_contact"), schema=phonebook_add_schema)
    hass.services.async_register(DOMAIN, "remove_contact", handler_for("remove_contact"), schema=phonebook_remove_schema)
    hass.services.async_register(DOMAIN, "set_contacts", handler_for("set_contacts"), schema=phonebook_set_schema)
    hass.services.async_register(DOMAIN, "clear_contacts", handler_for("clear_contacts"))
    hass.services.async_register(DOMAIN, "export_phonebook", handler_for("export_phonebook"))
    hass.services.async_register(DOMAIN, "push_phonebook", handler_for("push_phonebook"))
    hass.services.async_register(DOMAIN, "set_dnd", handler_for("set_dnd"), schema=set_dnd_schema)
    hass.services.async_register(DOMAIN, "create_account", handler_for("create_account"), schema=sip_account_create_schema)
    hass.services.async_register(DOMAIN, "remove_account", handler_for("remove_account"), schema=sip_account_name_schema)
    hass.services.async_register(DOMAIN, "rotate_account_password", handler_for("rotate_account_password"), schema=sip_account_name_schema)
    hass.services.async_register(DOMAIN, "enable_account", handler_for("enable_account"), schema=sip_account_name_schema)
    hass.services.async_register(DOMAIN, "disable_account", handler_for("disable_account"), schema=sip_account_name_schema)
    hass.services.async_register(DOMAIN, "export_accounts", handler_for("export_accounts"))
