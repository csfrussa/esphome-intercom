"""SIP trunk registration for provider/PBX interop."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
import logging
import socket
import time
from typing import Any, Awaitable, Callable

from . import sip
from .sip_auth import build_digest_authorization
from .sip_client import _SipClientProtocol, _read_sip_stream_message
from .sip_tcp_io import SipTcpWriter


_LOGGER = logging.getLogger(__name__)

TrunkRequestHandler = Callable[[bytes, tuple[str, int]], Awaitable[None]]


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
        self.responses: asyncio.Queue[sip.SipMessage] = asyncio.Queue()
        self.protocol: _SipClientProtocol | None = None
        self.transport: asyncio.DatagramTransport | None = None
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self._tcp_writer: SipTcpWriter | None = None
        self.call_id = sip.make_call_id("trunk-register")
        self.local_tag = sip.make_tag()
        self.cseq = 1
        self.registered = False
        self.status_code = 0
        self.status_reason = ""
        self.last_sip_event = ""
        self.expires_at = 0.0
        self._refresh_task: asyncio.Task | None = None
        self._receive_task: asyncio.Task | None = None
        self.request_handler: TrunkRequestHandler | None = None
        self.inbound_endpoint: Any | None = None

    def _ensure_receive_task(self) -> None:
        if self._receive_task is None or self._receive_task.done():
            self._receive_task = asyncio.create_task(self._receive_loop())

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
        self._ensure_receive_task()
        await self.register(timeout=2.0)
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
        if self._receive_task is not None:
            self._receive_task.cancel()
            try:
                await self._receive_task
            except asyncio.CancelledError:
                pass
            self._receive_task = None
        self.request_handler = None
        self.inbound_endpoint = None
        if self.transport is not None:
            self.transport.close()
            self.transport = None
        if self.writer is not None:
            if self._tcp_writer is not None:
                await self._tcp_writer.close()
                self._tcp_writer = None
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
            if self.transport_name == "TCP" and (self.writer is None or self.writer.is_closing()):
                await self._connect_tcp()
            self._ensure_receive_task()
            await self.register(timeout=2.0)

    async def _connect_tcp(self) -> None:
        if self.writer is not None and not self.writer.is_closing():
            return
        if self._tcp_writer is not None:
            await self._tcp_writer.close()
            self._tcp_writer = None
        if self.writer is not None:
            self.writer.close()
            with contextlib.suppress(Exception):
                await self.writer.wait_closed()
            self.writer = None
            self.reader = None
        self.reader, self.writer = await asyncio.wait_for(
            asyncio.open_connection(self.registrar_host, int(self.config.port)),
            timeout=2.0,
        )
        self._tcp_writer = SipTcpWriter(self.writer, label=f"trunk {self.registrar_host}:{self.config.port}")

    async def _send_raw(self, raw: bytes) -> None:
        if self.transport_name == "TCP":
            await self._connect_tcp()
            assert self._tcp_writer is not None
            await self._tcp_writer.send(raw)
            return
        assert self.transport is not None
        self.transport.sendto(raw, (self.registrar_host, int(self.config.port)))

    async def _read_response(self, timeout: float) -> sip.SipMessage | None:
        return await asyncio.wait_for(self.responses.get(), timeout=timeout)

    def set_request_handler(self, handler: TrunkRequestHandler | None) -> None:
        self.request_handler = handler

    def attach_endpoint_manager(self, manager: Any) -> None:
        """Route inbound trunk SIP requests through the HA SIP endpoint policy."""
        from .sip_listener import SipUdpEndpoint

        endpoint = SipUdpEndpoint(
            local_ip=manager.local_ip,
            local_sip_port=manager.port,
            local_rtp_port=manager.local_rtp_port,
            supported_formats=manager.supported_formats,
            supported_send_formats=manager.supported_send_formats,
            supported_recv_formats=manager.supported_recv_formats,
            on_invite=manager.on_invite,
            on_terminated=manager.on_terminated,
            on_register=getattr(manager, "on_register", None),
            send_override=self.send_response,
            signaling_transport=self.transport_name,
        )
        self.inbound_endpoint = endpoint
        self.set_request_handler(endpoint._handle_datagram)

    def send_response(self, raw: bytes, addr: tuple[str, int]) -> None:
        if self.transport_name == "TCP":
            if self._tcp_writer is not None:
                self._tcp_writer.send_nowait(raw)
            return
        if self.transport is not None:
            self.transport.sendto(raw, addr)

    def _remote_addr(self) -> tuple[str, int]:
        if self.writer is not None:
            peer = self.writer.get_extra_info("peername")
            if peer:
                return (str(peer[0]), int(peer[1]))
        return (self.registrar_host, int(self.config.port))

    async def _receive_loop(self) -> None:
        try:
            while True:
                if self.transport_name == "TCP":
                    if self.reader is None:
                        await asyncio.sleep(0)
                        continue
                    raw = await _read_sip_stream_message(self.reader)
                    if raw is None:
                        raise ConnectionError("SIP trunk TCP connection closed")
                    addr = self._remote_addr()
                else:
                    raw, addr = await self.queue.get()
                try:
                    msg = sip.parse_message(raw)
                except Exception as err:
                    _LOGGER.info("SIP trunk RX malformed from %s:%s: %s", addr[0], addr[1], err)
                    continue
                if msg.is_response:
                    await self.responses.put(msg)
                    continue
                _LOGGER.info("SIP trunk RX %s %s from %s:%s", msg.method, msg.uri, addr[0], addr[1])
                self.last_sip_event = msg.method or "SIP_REQUEST"
                if self.request_handler is None:
                    _LOGGER.warning("SIP trunk inbound request ignored: no SIP endpoint is attached")
                    continue
                try:
                    await self.request_handler(raw, addr)
                except Exception as err:
                    _LOGGER.exception(
                        "SIP trunk inbound request failed method=%s uri=%s from=%s:%s error=%s",
                        msg.method,
                        msg.uri,
                        addr[0],
                        addr[1],
                        err,
                    )
        except asyncio.CancelledError:
            raise
        except Exception as err:
            self.registered = False
            self.status_code = 0
            self.status_reason = str(err)
            _LOGGER.warning("SIP trunk receive loop stopped server=%s transport=%s error=%s", self.config.server, self.transport_name, err)
        finally:
            if self.transport_name == "TCP":
                writer = self.writer
                self.reader = None
                self.writer = None
                if writer is not None and not writer.is_closing():
                    writer.close()

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
                _LOGGER.warning(
                    "SIP trunk registration timed out server=%s transport=%s expires=%s",
                    self.config.server,
                    self.transport_name,
                    expires_value,
                )
                return "timeout"
            except Exception as err:
                self.registered = False
                self.status_code = 0
                self.status_reason = str(err)
                _LOGGER.warning(
                    "SIP trunk registration transport error server=%s transport=%s error=%s",
                    self.config.server,
                    self.transport_name,
                    err,
                )
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
                if self.registered:
                    _LOGGER.info(
                        "SIP trunk registered server=%s transport=%s expires=%ss status=%s %s",
                        self.config.server,
                        self.transport_name,
                        expires_value,
                        msg.status_code,
                        msg.reason,
                    )
                else:
                    _LOGGER.info(
                        "SIP trunk registration ended server=%s transport=%s status=%s %s",
                        self.config.server,
                        self.transport_name,
                        msg.status_code,
                        msg.reason,
                    )
                return "registered" if self.registered else "unregistered"
            self.registered = False
            result = sip.sip_failure_reason(msg.status_code)
            _LOGGER.warning(
                "SIP trunk registration rejected server=%s transport=%s status=%s %s reason=%s",
                self.config.server,
                self.transport_name,
                msg.status_code,
                msg.reason,
                result,
            )
            return result

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
