"""Deterministic ownership handoff for browser media WebSockets."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import inspect
import logging
from typing import Any


_LOGGER = logging.getLogger(__name__)


class WebSocketOwnerBusyError(RuntimeError):
    """Raised when an existing media WebSocket cannot release ownership."""


@dataclass(slots=True)
class MediaWebSocketOwner:
    """One browser media owner and its deterministic release notification."""

    websocket: Any | None = None
    transport: asyncio.BaseTransport | None = None
    user_id: str = ""
    client_id: str = ""
    token: object = field(default_factory=object)
    handoff_requested: asyncio.Event = field(default_factory=asyncio.Event)
    released: asyncio.Event = field(default_factory=asyncio.Event)

    def revoke(self) -> None:
        """Wake the previous request so it can release RTP resources."""

        self.handoff_requested.set()
        if self.websocket is not None:
            try:
                self.websocket.force_close()
            except Exception:  # noqa: BLE001 - teardown must continue.
                _LOGGER.debug("Failed to force-close replaced media WebSocket", exc_info=True)
        if self.transport is not None:
            try:
                self.transport.close()
            except Exception:  # noqa: BLE001 - teardown must continue.
                _LOGGER.debug("Failed to close replaced media transport", exc_info=True)


async def async_claim_media_owner(
    owners: dict[str, object],
    owner_lock: asyncio.Lock,
    call_id: str,
    owner: MediaWebSocketOwner,
    *,
    timeout: float,
    shutdown_event: asyncio.Event | None = None,
) -> None:
    """Replace a stale browser owner only after its teardown completes."""

    async with owner_lock:
        if shutdown_event is not None and shutdown_event.is_set():
            raise WebSocketOwnerBusyError(call_id)
        previous_owner = owners.get(call_id)

    if isinstance(previous_owner, MediaWebSocketOwner):
        # Media contains private microphone/camera/call audio.  A reconnect is
        # a handoff only when it comes from the exact authenticated browser
        # instance that already owns the stream.  Merely having general
        # control permission must never allow another user or tab to evict it.
        if (
            previous_owner.user_id != owner.user_id
            or previous_owner.client_id != owner.client_id
        ):
            raise WebSocketOwnerBusyError(call_id)
        previous_owner.revoke()
        try:
            await asyncio.wait_for(previous_owner.released.wait(), timeout=timeout)
        except TimeoutError as err:
            raise WebSocketOwnerBusyError(call_id) from err
    elif previous_owner is not None:
        # Compatibility with an owner created before an integration reload.
        raise WebSocketOwnerBusyError(call_id)

    async with owner_lock:
        if shutdown_event is not None and shutdown_event.is_set():
            raise WebSocketOwnerBusyError(call_id)
        if call_id in owners:
            # Another reconnect won the race while this request was waiting.
            raise WebSocketOwnerBusyError(call_id)
        owners[call_id] = owner


async def async_release_media_owner(
    owners: dict[str, object],
    owner_lock: asyncio.Lock,
    call_id: str,
    owner: MediaWebSocketOwner,
) -> None:
    """Release only this request's ownership and wake every waiter."""

    async def release() -> None:
        try:
            async with owner_lock:
                if owners.get(call_id) is owner:
                    owners.pop(call_id, None)
        finally:
            # Handoff waiters must always wake, including shutdown/cancellation
            # while this release is queued behind the ownership lock.
            owner.released.set()

    task = asyncio.create_task(
        release(),
        name=f"voip-media-owner-release-{call_id}",
    )
    caller_cancelled = False
    while True:
        try:
            await asyncio.shield(task)
            break
        except asyncio.CancelledError:
            if task.cancelled():
                raise
            caller_cancelled = True
    if caller_cancelled:
        raise asyncio.CancelledError


async def async_revoke_media_owners(
    owners: dict[str, object],
    owner_lock: asyncio.Lock,
    *,
    timeout: float,
) -> set[str]:
    """Request teardown of every owner and report unconfirmed releases."""

    async with owner_lock:
        snapshot = dict(owners)
    structured = {
        call_id: owner
        for call_id, owner in snapshot.items()
        if isinstance(owner, MediaWebSocketOwner)
    }
    legacy = {
        call_id: owner
        for call_id, owner in snapshot.items()
        if not isinstance(owner, MediaWebSocketOwner)
    }
    for owner in structured.values():
        owner.revoke()

    async def _revoke_legacy(call_id: str, owner: object) -> str | None:
        """Best-effort cleanup for mappings created by an older reload."""

        failed = False
        targets = (owner, getattr(owner, "websocket", None), getattr(owner, "transport", None))
        seen: set[int] = set()
        for target in targets:
            if target is None or id(target) in seen:
                continue
            seen.add(id(target))
            close = getattr(target, "force_close", None) or getattr(target, "close", None)
            if not callable(close):
                continue
            try:
                result = close()
                if inspect.isawaitable(result):
                    await result
            except Exception:  # noqa: BLE001 - unload must continue.
                failed = True
                _LOGGER.debug(
                    "Failed to close legacy media WebSocket owner call_id=%s",
                    call_id,
                    exc_info=True,
                )
        async with owner_lock:
            if owners.get(call_id) is owner:
                owners.pop(call_id, None)
        return call_id if failed else None

    async def _wait(call_id: str, owner: MediaWebSocketOwner) -> str | None:
        try:
            await asyncio.wait_for(owner.released.wait(), timeout=timeout)
        except TimeoutError:
            return call_id
        return None

    results = await asyncio.gather(
        *(_wait(call_id, owner) for call_id, owner in structured.items()),
        *(_revoke_legacy(call_id, owner) for call_id, owner in legacy.items()),
    )
    return {call_id for call_id in results if call_id is not None}
