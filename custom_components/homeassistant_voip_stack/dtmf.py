"""DTMF collection helpers for SIP trunk inbound routing."""

from __future__ import annotations

import asyncio
import logging
import socket
import time
from typing import Callable


_LOGGER = logging.getLogger(__name__)
_EVENT_DIGITS = {
    0: "0",
    1: "1",
    2: "2",
    3: "3",
    4: "4",
    5: "5",
    6: "6",
    7: "7",
    8: "8",
    9: "9",
    10: "*",
    11: "#",
}


def parse_dtmf_route_map(value: object) -> dict[str, str]:
    if value in (None, ""):
        return {}
    if isinstance(value, dict):
        return {
            str(key).strip(): str(dest).strip()
            for key, dest in value.items()
            if str(key).strip() and str(dest).strip()
        }
    out: dict[str, str] = {}
    for raw in str(value).replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if "=" in line:
            key, dest = line.split("=", 1)
        elif ":" in line:
            key, dest = line.split(":", 1)
        else:
            raise ValueError(f"invalid DTMF route line {line!r}; use code=destination")
        key = key.strip()
        dest = dest.strip()
        if key and dest:
            out[key] = dest
    return out


def _match_dtmf(buffer: str, routes: dict[str, str], *, terminator: str = "") -> tuple[str, bool]:
    if terminator and buffer.endswith(terminator):
        code = buffer[: -len(terminator)]
        return (routes.get(code, ""), True)
    if buffer in routes:
        ambiguous = any(key != buffer and key.startswith(buffer) for key in routes)
        if not ambiguous:
            return (routes[buffer], True)
    return ("", False)


class _DtmfProtocol(asyncio.DatagramProtocol):
    def __init__(self, payload_type: int, on_digit: Callable[[str], None]) -> None:
        self.payload_type = int(payload_type)
        self.on_digit = on_digit
        self._seen_events: set[tuple[int, int]] = set()

    def datagram_received(self, data: bytes, addr) -> None:
        if len(data) < 16:
            return
        version = data[0] >> 6
        if version != 2:
            return
        cc = data[0] & 0x0F
        header_len = 12 + cc * 4
        if len(data) < header_len + 4:
            return
        payload_type = data[1] & 0x7F
        if payload_type != self.payload_type:
            return
        sequence = int.from_bytes(data[2:4], "big")
        timestamp = int.from_bytes(data[4:8], "big")
        event = data[header_len]
        ended = bool(data[header_len + 1] & 0x80)
        digit = _EVENT_DIGITS.get(event)
        if not digit:
            return
        key = (timestamp, event)
        if key in self._seen_events:
            return
        self._seen_events.add(key)
        _LOGGER.debug("SIP trunk DTMF RX digit=%s seq=%s end=%s from=%s:%s", digit, sequence, ended, addr[0], addr[1])
        self.on_digit(digit)


class DtmfCollector:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        payload_type: int,
        routes: dict[str, str],
        timeout: float,
        terminator: str = "",
    ) -> None:
        self.host = host
        self.port = int(port)
        self.payload_type = int(payload_type)
        self.routes = routes
        self.timeout = max(0.1, float(timeout))
        self.terminator = terminator
        self.buffer = ""
        self.transport: asyncio.DatagramTransport | None = None
        self._done: asyncio.Future[str] | None = None

    async def collect(self) -> tuple[str, str]:
        loop = asyncio.get_running_loop()
        self._done = loop.create_future()
        protocol = _DtmfProtocol(self.payload_type, self._on_digit)
        transport, _ = await loop.create_datagram_endpoint(
            lambda: protocol,
            local_addr=(self.host, self.port),
            family=socket.AF_INET,
        )
        self.transport = transport  # type: ignore[assignment]
        started = time.monotonic()
        try:
            try:
                destination = await asyncio.wait_for(self._done, timeout=self.timeout)
            except asyncio.TimeoutError:
                destination = self.routes.get(self.buffer, "")
            _LOGGER.info(
                "SIP trunk DTMF collection finished buffer=%s destination=%s elapsed_ms=%d",
                self.buffer or "-",
                destination or "-",
                int((time.monotonic() - started) * 1000),
            )
            return self.buffer, destination
        finally:
            transport.close()
            self.transport = None

    def _on_digit(self, digit: str) -> None:
        if self._done is None or self._done.done():
            return
        self.buffer += digit
        destination, terminal = _match_dtmf(self.buffer, self.routes, terminator=self.terminator)
        if terminal:
            self._done.set_result(destination)
