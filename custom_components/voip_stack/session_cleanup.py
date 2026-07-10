"""Shared SIP runtime cleanup primitives."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
import logging
from typing import Any


_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class SipRuntimeCleanupResult:
    """Result of an idempotent SIP runtime cleanup pass."""

    watcher_cancelled: bool = False
    client_closed: bool = False
    relay_stopped: bool = False


async def async_cleanup_sip_runtime(
    *,
    relay: Any = None,
    client: Any = None,
    watcher: asyncio.Task | None = None,
    terminate_client: bool = True,
    relay_first: bool = False,
) -> SipRuntimeCleanupResult:
    """Stop the common watcher/client/relay trio used by bridged SIP calls."""

    result = SipRuntimeCleanupResult()

    async def _stop_relay() -> None:
        if relay is None or result.relay_stopped:
            return
        try:
            await relay.stop()
            result.relay_stopped = True
        except Exception:
            _LOGGER.debug("Ignoring SIP RTP relay cleanup error", exc_info=True)

    if relay_first:
        await _stop_relay()

    if watcher is not None:
        current_task = asyncio.current_task()
        if watcher is not current_task:
            watcher.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await watcher
            result.watcher_cancelled = True

    if client is not None:
        if terminate_client:
            try:
                await client.terminate()
            except Exception:
                _LOGGER.debug("Ignoring SIP client terminate cleanup error", exc_info=True)
        try:
            await client.close()
            result.client_closed = True
        except Exception:
            _LOGGER.debug("Ignoring SIP client close cleanup error", exc_info=True)

    if not relay_first:
        await _stop_relay()

    return result
