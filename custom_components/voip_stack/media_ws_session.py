"""Media-agnostic ownership lifecycle for browser WebSocket sessions."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
import logging
from typing import Any

from .session_cleanup import async_wait_for_cleanup
from .websocket_owner import (
    MediaWebSocketOwner,
    async_claim_call_media_owner,
    async_release_local_media_if_unowned,
    async_release_media_owner,
)


_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class MediaWebSocketSession:
    """One claimed audio/video owner plus an optional local bridge lease."""

    bucket: dict[str, Any]
    owners: dict[str, object]
    owner_lock: asyncio.Lock
    owner_key: str
    owner: MediaWebSocketOwner
    publish_state: Callable[[], None]
    local_bridge: Any | None = None
    local_lease: Any | None = None
    _close_task: asyncio.Task[None] | None = field(default=None, init=False)

    def own_local_lease(self, bridge: Any, lease: Any) -> None:
        if self.local_lease is not None:
            raise RuntimeError("media WebSocket session already owns a local lease")
        self.local_bridge = bridge
        self.local_lease = lease

    async def _run_close(self) -> None:
        try:
            await async_release_media_owner(
                self.owners,
                self.owner_lock,
                self.owner_key,
                self.owner,
            )
        finally:
            try:
                if self.local_lease is not None:
                    await async_release_local_media_if_unowned(
                        self.bucket,
                        self.local_bridge,
                        self.local_lease,
                    )
            finally:
                try:
                    self.publish_state()
                except Exception:  # observer failure cannot leak media ownership
                    _LOGGER.exception(
                        "Media WebSocket final state publication failed owner=%s",
                        self.owner_key,
                    )

    async def close(self) -> None:
        """Release owner, lease, then observable state exactly once."""

        if self._close_task is None:
            self._close_task = asyncio.create_task(
                self._run_close(),
                name=f"voip-media-ws-session-close-{self.owner_key}",
            )
        await async_wait_for_cleanup(self._close_task)


@asynccontextmanager
async def async_media_websocket_session(
    bucket: dict[str, Any],
    registry: Any,
    call_id: str,
    endpoint_id: str,
    owner: MediaWebSocketOwner,
    *,
    channel: str,
    timeout: float,
    shutdown_event: asyncio.Event | None,
    pin_client_identity: bool,
    local_bridge: Any | None,
    publish_state: Callable[[], None],
):
    """Claim and always release one media channel through a shared barrier."""

    owners, owner_lock, owner_key = await async_claim_call_media_owner(
        bucket,
        registry,
        call_id,
        endpoint_id,
        owner,
        channel=channel,
        timeout=timeout,
        shutdown_event=shutdown_event,
        pin_client_identity=pin_client_identity,
        local_bridge=local_bridge,
    )
    session = MediaWebSocketSession(
        bucket,
        owners,
        owner_lock,
        owner_key,
        owner,
        publish_state,
    )
    try:
        yield session
    finally:
        await session.close()
