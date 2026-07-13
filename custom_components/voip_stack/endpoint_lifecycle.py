"""SIP endpoint lifecycle helpers."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Coroutine
from typing import Any

from homeassistant.core import HomeAssistant

from .call_registry import CallRegistry
from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)


def create_runtime_task(hass: HomeAssistant, coro: Coroutine[Any, Any, Any]) -> asyncio.Task:
    """Create a detached integration task that is cancelled on endpoint reload."""

    tasks: set[asyncio.Task] = hass.data.setdefault(DOMAIN, {}).setdefault("runtime_tasks", set())
    task = hass.async_create_task(coro)
    tasks.add(task)
    task.add_done_callback(tasks.discard)
    return task


async def cancel_runtime_tasks(hass: HomeAssistant) -> None:
    tasks = set(hass.data.setdefault(DOMAIN, {}).pop("runtime_tasks", set()))
    current = asyncio.current_task()
    for task in tasks:
        if task is not current:
            task.cancel()
    if tasks:
        await asyncio.gather(*(task for task in tasks if task is not current), return_exceptions=True)


def call_registry(hass: HomeAssistant) -> CallRegistry:
    bucket = hass.data.setdefault(DOMAIN, {})
    registry = bucket.get("call_registry")
    if not isinstance(registry, CallRegistry):
        registry = CallRegistry()
        bucket["call_registry"] = registry
    return registry


async def async_stop_sip_endpoint(hass: HomeAssistant) -> None:
    registry = call_registry(hass)
    bucket = hass.data.get(DOMAIN, {})
    await cancel_runtime_tasks(hass)
    bucket.pop("async_forward_call", None)
    bucket.pop("forward_tasks", None)
    bucket.pop("forward_claims", None)
    bucket.pop("call_deadlines", None)
    endpoint = bucket.pop("sip_endpoint", None)
    bucket.pop("sip_server", None)
    bucket.pop("sip_tcp_server", None)

    if endpoint is not None:
        snapshot = endpoint.snapshot()
        for call_id in snapshot.pending_call_ids:
            endpoint.send_final_response(call_id, 503, "Service Unavailable", decline_reason="shutdown")
        for call_id in snapshot.active_call_ids:
            endpoint.send_bye(call_id)

    watchers = {task for task in registry.client_watchers.values() if isinstance(task, asyncio.Task)}
    current = asyncio.current_task()
    for task in watchers:
        if task is not current:
            task.cancel()
    if watchers:
        await asyncio.gather(*(task for task in watchers if task is not current), return_exceptions=True)

    manager = bucket.pop("conference_manager", None)
    if manager is not None:
        try:
            await manager.close(reason="local_hangup")
        except Exception:
            _LOGGER.debug("Ignoring conference shutdown error", exc_info=True)

    async def _stop_relay(relay) -> None:
        try:
            await relay.stop()
        except Exception:
            _LOGGER.debug("Ignoring SIP RTP relay stop error", exc_info=True)

    async def _stop_client(client) -> None:
        try:
            await client.terminate()
        except Exception:
            _LOGGER.debug("Ignoring SIP client terminate error", exc_info=True)
        finally:
            with contextlib.suppress(Exception):
                await client.close()

    relays = {id(relay): relay for relay in registry.relays.values()}.values()
    clients = {id(client): client for client in registry.sip_clients.values()}.values()
    await asyncio.gather(
        *(_stop_relay(relay) for relay in relays),
        *(_stop_client(client) for client in clients),
    )
    if endpoint is not None:
        try:
            await endpoint.stop()
        except Exception:
            _LOGGER.debug("Ignoring SIP endpoint stop error", exc_info=True)
    registry.clear_runtime()
