"""SIP trunk registration for provider/PBX interop."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import socket
import time
from typing import Any

from . import sip
from .sip_auth import build_digest_authorization
from .sip_client import _SipClientProtocol, _read_sip_stream_message


_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SipTrunkConfig:
    enabled: bool
    transport: str
    server: str
    port: int
    domain: str
    username: str
    auth_username: str
    password: str
    expires: int
    outbound_proxy: str = ""


class SipTrunkClient:
    def __init__(self, *, config: SipTrunkConfig, local_ip: str, local_sip_port: int) -> None:
        self.config = config
        self.local_ip = local_ip
        self.local_sip_port = int(local_sip_port)
        self.transport_name = (config.transport or "udp").upper()
        self.queue: asyncio.Queue[tuple[bytes, tuple[str, int]]] = asyncio.Queue()
        self.protocol: _SipClientProtocol | None = None
        self.transport: asyncio.DatagramTransport | None = None
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self.call_id = sip.make_call_id("trunk-register")
        self.local_tag = sip.make_tag()
        self.cseq = 1
        self.registered = False
        self.status_code = 0
        self.status_reason = ""
        self.last_sip_event = ""
        self.expires_at = 0.0
        self._refresh_task: asyncio.Task | None = None

    @property
    def registrar_host(self) -> str:
        return self.config.outbound_proxy or self.config.server

    @property
    def domain(self) -> str:
        return self.config.domain or self.config.server

    @property
    def contact_uri(self) -> str:
        return str(sip.SipUri(self.config.username, self.local_ip, self.local_sip_port, params=(("transport", self.transport_name.lower()),)))

    @property
    def address_uri(self) -> str:
        return str(sip.SipUri(self.config.username, self.domain))

    async def start(self) -> None:
        if self.transport_name == "TCP":
            await self._connect_tcp()
        elif self.transport is None:
            loop = asyncio.get_running_loop()
            self.protocol = _SipClientProtocol(self.queue)
            transport, _ = await loop.create_datagram_endpoint(
                lambda: self.protocol,
                local_addr=("0.0.0.0", 0),
                family=socket.AF_INET,
            )
            self.transport = transport  # type: ignore[assignment]
        result = await self.register(timeout=2.0)
        _LOGGER.info("SIP trunk register result=%s status=%s %s", result, self.status_code, self.status_reason)
        if self.registered:
            self._refresh_task = asyncio.create_task(self._refresh_loop())

    async def stop(self) -> None:
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            try:
                await self._refresh_task
            except asyncio.CancelledError:
                pass
            self._refresh_task = None
        if self.registered:
            try:
                await self.register(expires=0, timeout=1.5)
            except Exception:
                _LOGGER.debug("Ignoring SIP trunk unregister failure", exc_info=True)
        if self.transport is not None:
            self.transport.close()
            self.transport = None
        if self.writer is not None:
            self.writer.close()
            try:
                await self.writer.wait_closed()
            except Exception:
                pass
            self.writer = None
            self.reader = None

    async def _refresh_loop(self) -> None:
        while True:
            delay = max(30.0, min(float(self.config.expires) * 0.8, max(1.0, self.expires_at - time.time() - 10.0)))
            await asyncio.sleep(delay)
            await self.register(timeout=2.0)

    async def _connect_tcp(self) -> None:
        if self.writer is not None:
            return
        self.reader, self.writer = await asyncio.open_connection(self.registrar_host, int(self.config.port))

    async def _send_raw(self, raw: bytes) -> None:
        if self.transport_name == "TCP":
            await self._connect_tcp()
            assert self.writer is not None
            self.writer.write(raw)
            await self.writer.drain()
            return
        assert self.transport is not None
        self.transport.sendto(raw, (self.registrar_host, int(self.config.port)))

    async def _read_response(self, timeout: float) -> sip.SipMessage | None:
        if self.transport_name == "TCP":
            if self.reader is None:
                return None
            raw = await asyncio.wait_for(_read_sip_stream_message(self.reader), timeout=timeout)
            return sip.parse_message(raw) if raw else None
        data, _addr = await asyncio.wait_for(self.queue.get(), timeout=timeout)
        return sip.parse_message(data)

    async def register(self, *, expires: int | None = None, timeout: float = 2.0) -> str:
        expires_value = int(self.config.expires if expires is None else expires)
        auth_value = ""
        retried = False
        while True:
            self.cseq += 1
            request_uri = self.address_uri
            headers = self._register_headers(expires_value, auth_value=auth_value)
            raw = sip.build_request("REGISTER", request_uri, headers, b"")
            await self._send_raw(raw)
            self.last_sip_event = "REGISTER"
            _LOGGER.info("SIP trunk TX REGISTER %s expires=%s", self.domain, expires_value)
            try:
                msg = await self._read_response(timeout)
            except asyncio.TimeoutError:
                self.registered = False
                self.status_code = 0
                self.status_reason = "timeout"
                return "timeout"
            except Exception as err:
                self.registered = False
                self.status_code = 0
                self.status_reason = str(err)
                return "transport_unreachable"
            if msg is None or not msg.is_response or msg.status_code is None:
                continue
            self.status_code = int(msg.status_code)
            self.status_reason = msg.reason
            self.last_sip_event = "SIP_RESPONSE"
            _LOGGER.info("SIP trunk RX %s %s", msg.status_code, msg.reason)
            if msg.status_code in {401, 407} and not retried:
                retried = True
                challenge = msg.header("Proxy-Authenticate" if msg.status_code == 407 else "WWW-Authenticate")
                auth_value = build_digest_authorization(
                    challenge_header=challenge,
                    username=self.config.username,
                    auth_username=self.config.auth_username,
                    password=self.config.password,
                    method="REGISTER",
                    uri=request_uri,
                )
                if msg.status_code == 407:
                    auth_value = "Proxy-Authorization: " + auth_value
                else:
                    auth_value = "Authorization: " + auth_value
                continue
            if 200 <= msg.status_code < 300:
                self.registered = expires_value > 0
                self.expires_at = time.time() + expires_value if self.registered else 0.0
                return "registered" if self.registered else "unregistered"
            self.registered = False
            return sip.sip_failure_reason(msg.status_code)

    def _register_headers(self, expires: int, *, auth_value: str = "") -> list[tuple[str, str]]:
        local_uri = self.address_uri
        dialog = sip.SipDialogIds(
            call_id=self.call_id,
            local_tag=self.local_tag,
            cseq=self.cseq,
            branch=sip.make_branch(),
        )
        headers = sip.dialog_headers(
            request_uri=self.address_uri,
            local_uri=local_uri,
            remote_uri=local_uri,
            dialog=dialog,
            method="REGISTER",
            contact_uri=self.contact_uri,
            transport=self.transport_name,
        )
        headers.append(("Expires", str(int(expires))))
        if auth_value:
            key, value = auth_value.split(":", 1)
            headers.append((key.strip(), value.strip()))
        return headers

    def snapshot(self) -> dict[str, Any]:
        return {
            "trunk_enabled": bool(self.config.enabled),
            "trunk_registered": self.registered,
            "trunk_status_code": self.status_code,
            "trunk_status_reason": self.status_reason,
            "trunk_expires_at": self.expires_at,
            "trunk_last_sip_event": self.last_sip_event,
            "trunk_transport": self.transport_name.lower(),
            "trunk_server": self.config.server,
        }
