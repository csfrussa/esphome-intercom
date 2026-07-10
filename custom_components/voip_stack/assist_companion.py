"""Provision the native HA VoIP Assist satellite behind VoIP Stack's B2BUA."""

from __future__ import annotations

import asyncio
import logging

from homeassistant.components import network
from homeassistant.config_entries import ConfigEntry, ConfigEntryState
from homeassistant.const import STATE_ON
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import entity_registry as er

from .const import (
    VOIP_STACK_ASSIST_BRIDGE_USER,
    VOIP_STACK_ASSIST_SIP_PORT,
)

_LOGGER = logging.getLogger(__name__)
_NATIVE_DOMAIN = "voip"
_NATIVE_CONF_SIP_PORT = "sip_port"


async def _wait_entry_loaded(hass: HomeAssistant, entry: ConfigEntry, timeout: float = 15) -> None:
    async with asyncio.timeout(timeout):
        while entry.state not in {
            ConfigEntryState.LOADED,
            ConfigEntryState.SETUP_ERROR,
            ConfigEntryState.SETUP_RETRY,
        }:
            await asyncio.sleep(0.05)
    if entry.state is not ConfigEntryState.LOADED:
        raise ConfigEntryError(f"Native VoIP companion failed to load: {entry.state}")


async def _native_entry(hass: HomeAssistant) -> tuple[ConfigEntry, bool]:
    entries = hass.config_entries.async_entries(_NATIVE_DOMAIN)
    if entries:
        return entries[0], False
    result = await hass.config_entries.flow.async_init(
        _NATIVE_DOMAIN,
        context={"source": "user"},
        data={},
    )
    entry = result.get("result") if isinstance(result, dict) else None
    if not isinstance(entry, ConfigEntry):
        raise ConfigEntryError("Unable to create native VoIP companion entry")
    return entry, True


async def _ensure_bridge_allowed(
    hass: HomeAssistant,
    native_entry: ConfigEntry,
    *,
    native_port: int,
    stack_sip_port: int,
) -> None:
    from voip_utils import CallInfo
    from voip_utils.sip import get_sip_endpoint

    source_ip = await network.async_get_source_ip(hass)
    caller = get_sip_endpoint(
        host=source_ip,
        port=stack_sip_port,
        username=VOIP_STACK_ASSIST_BRIDGE_USER,
    )
    local = get_sip_endpoint(host="127.0.0.1", port=native_port, username="assist")
    call_info = CallInfo(
        caller_endpoint=caller,
        local_endpoint=local,
        caller_rtp_port=9,
        server_ip="127.0.0.1",
        headers={"user-agent": "VoIP Stack Assist Bridge"},
        contact_endpoint=caller,
    )
    devices = native_entry.runtime_data.domain_data.devices
    device = devices.async_get_or_create(call_info)
    unique_id = f"{device.voip_id}-allow_call"
    entity_id = None
    async with asyncio.timeout(5):
        while entity_id is None:
            entity_id = er.async_get(hass).async_get_entity_id("switch", _NATIVE_DOMAIN, unique_id)
            if entity_id is None:
                await asyncio.sleep(0.05)
    state = hass.states.get(entity_id)
    if state is None or state.state != STATE_ON:
        await hass.services.async_call("switch", "turn_on", {"entity_id": entity_id}, blocking=True)
    _LOGGER.info(
        "Native VoIP Assist bridge ready port=%s identity=%s allow_entity=%s",
        native_port,
        device.voip_id,
        entity_id,
    )


async def async_prepare_assist_companion(hass: HomeAssistant, *, stack_sip_port: int) -> int:
    """Ensure one native Assist SIP endpoint accepts the fixed B2BUA identity."""
    entry, created = await _native_entry(hass)
    if created:
        await _wait_entry_loaded(hass, entry)
        hass.config_entries.async_update_entry(
            entry,
            options={
                **entry.options,
                _NATIVE_CONF_SIP_PORT: VOIP_STACK_ASSIST_SIP_PORT,
            },
        )
        await hass.config_entries.async_reload(entry.entry_id)
    await _wait_entry_loaded(hass, entry)
    native_port = int(entry.options.get(_NATIVE_CONF_SIP_PORT, 5060))
    if native_port == int(stack_sip_port):
        raise ConfigEntryError("Native VoIP and VoIP Stack cannot listen on the same SIP port")
    await _ensure_bridge_allowed(
        hass,
        entry,
        native_port=native_port,
        stack_sip_port=stack_sip_port,
    )
    return native_port
