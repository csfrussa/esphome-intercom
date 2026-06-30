"""Governed SIP/TCP writes with explicit backpressure."""

from __future__ import annotations

import asyncio
import contextlib
import logging

_LOGGER = logging.getLogger(__name__)


class SipTcpWriter:
    """Single-owner async writer for a SIP/TCP connection."""

    def __init__(self, writer: asyncio.StreamWriter, *, label: str, max_queue: int = 64) -> None:
        self.writer = writer
        self.label = label
        self.queue: asyncio.Queue[bytes | None] = asyncio.Queue(maxsize=max(1, int(max_queue)))
        self.task = asyncio.create_task(self._run())

    def send_nowait(self, data: bytes) -> bool:
        if self.writer.is_closing() or self.task.done():
            return False
        try:
            self.queue.put_nowait(data)
            return True
        except asyncio.QueueFull:
            _LOGGER.warning("SIP TCP TX queue full for %s; dropping message", self.label)
            return False

    async def send(self, data: bytes) -> bool:
        if self.writer.is_closing() or self.task.done():
            return False
        await self.queue.put(data)
        return True

    async def close(self) -> None:
        while not self.task.done():
            try:
                self.queue.put_nowait(None)
                break
            except asyncio.QueueFull:
                with contextlib.suppress(asyncio.QueueEmpty):
                    self.queue.get_nowait()
        try:
            await asyncio.wait_for(self.task, timeout=1.0)
        except asyncio.TimeoutError:
            self.task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self.task

    async def _run(self) -> None:
        while True:
            data = await self.queue.get()
            if data is None:
                return
            try:
                self.writer.write(data)
                await self.writer.drain()
            except (ConnectionError, RuntimeError, OSError) as err:
                _LOGGER.debug("SIP TCP write failed for %s: %s", self.label, err)
                return
