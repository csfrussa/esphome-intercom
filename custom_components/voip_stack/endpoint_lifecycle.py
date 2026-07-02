"""SIP endpoint lifecycle helpers."""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant

from .call_registry import CallRegistry
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


def call_registry(hass: HomeAssistant) -> CallRegistry:
    bucket = hass.data.setdefault(DOMAIN, {})
    registry = bucket.get("call_registry")
    if not isinstance(registry, CallRegistry):
        registry = CallRegistry()
        bucket["call_registry"] = registry
    return registry


async def async_stop_sip_endpoint(hass: HomeAssistant) -> None:
    registry = call_registry(hass)
    relays = dict(registry.relays)
    for relay in list(relays.values()):
        try:
            await relay.stop()
        except Exception:
            _LOGGER.debug("Ignoring SIP RTP relay stop error", exc_info=True)
    clients = dict(registry.sip_clients)
    for client in list(clients.values()):
        try:
            client.bye()
            await client.close()
        except Exception:
            _LOGGER.debug("Ignoring SIP client stop error", exc_info=True)
    registry.clear_runtime()
    endpoint = hass.data.get(DOMAIN, {}).pop("sip_endpoint", None)
    hass.data.get(DOMAIN, {}).pop("sip_server", None)
    hass.data.get(DOMAIN, {}).pop("sip_tcp_server", None)
    if endpoint is not None:
        await endpoint.stop()
