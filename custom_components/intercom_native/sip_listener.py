"""Async SIP/UDP endpoint for the phase-1 VoIP intercom profile."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import re
from typing import Awaitable, Callable

from .audio_format import AudioFormat, LEGACY_AUDIO_FORMAT
from .const import INTERCOM_RTP_PORT
from . import sdp, sip


_LOGGER = logging.getLogger(__name__)
_TAG_RE = re.compile(r"(?:^|;)tag=([^;>\s]+)")


@dataclass(frozen=True, slots=True)
class SipInvite:
    source_host: str
    source_port: int
    request_uri: sip.SipUri
    caller_uri: sip.SipUri | None
    target: str
    caller: str
    call_id: str
    cseq: str
    remote_sdp: bytes
    selected_format: sdp.RtpPcmFormat
    remote_rtp_host: str
    remote_rtp_port: int


@dataclass(frozen=True, slots=True)
class SipInviteResult:
    status: int
    reason: str
    answer_sdp: str = ""
    to_tag: str = ""
    defer_final: bool = False


@dataclass(slots=True)
class _PendingInvite:
    request: sip.SipMessage
    addr: tuple[str, int]
    to_tag: str


InviteHandler = Callable[[SipInvite], Awaitable[SipInviteResult]]


def _extract_tag(header: str) -> str:
    match = _TAG_RE.search(header or "")
    return match.group(1) if match else ""


def _uri_from_header(header: str) -> sip.SipUri | None:
    value = (header or "").strip()
    if "<" in value and ">" in value:
        value = value[value.index("<") + 1:value.index(">")]
    try:
        return sip.parse_sip_uri(value)
    except Exception:
        return None


def _response_headers(request: sip.SipMessage, *, to_tag: str = "") -> list[tuple[str, str]]:
    headers: list[tuple[str, str]] = []
    for name in ("Via", "From", "Call-ID", "CSeq"):
        value = request.header(name)
        if value:
            headers.append((name, value))
    to_value = request.header("To")
    if to_value and to_tag and "tag=" not in to_value:
        to_value = f"{to_value};tag={to_tag}"
    if to_value:
        headers.insert(2, ("To", to_value))
    return headers


class SipUdpEndpoint(asyncio.DatagramProtocol):
    """Small SIP/UDP user agent.

    The protocol object is intentionally policy-light: it validates SIP/SDP and
    delegates call routing to `on_invite`. It sends only standards-compliant SIP
    responses.
    """

    def __init__(
        self,
        *,
        local_ip: str,
        local_rtp_port: int,
        supported_formats: list[AudioFormat],
        on_invite: InviteHandler,
    ) -> None:
        self.local_ip = local_ip
        self.local_rtp_port = local_rtp_port or INTERCOM_RTP_PORT
        self.supported_formats = supported_formats or [LEGACY_AUDIO_FORMAT]
        self.on_invite = on_invite
        self.transport: asyncio.DatagramTransport | None = None
        self.pending_invites: dict[str, _PendingInvite] = {}

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]
        _LOGGER.info("SIP UDP listener ready on %s", transport.get_extra_info("sockname"))

    def connection_lost(self, exc: Exception | None) -> None:
        if exc:
            _LOGGER.warning("SIP UDP listener closed with error: %s", exc)
        else:
            _LOGGER.info("SIP UDP listener closed")
        self.transport = None

    def datagram_received(self, data: bytes, addr) -> None:
        asyncio.create_task(self._handle_datagram(data, addr))

    def error_received(self, exc: Exception) -> None:
        _LOGGER.warning("SIP UDP socket error: %s", exc)

    def _send(self, data: bytes, addr) -> None:
        if self.transport is not None:
            self.transport.sendto(data, addr)

    def _send_response(
        self,
        request: sip.SipMessage,
        addr,
        status: int,
        reason: str,
        *,
        body: bytes = b"",
        to_tag: str = "",
    ) -> None:
        headers = _response_headers(request, to_tag=to_tag)
        if body:
            headers.append(("Content-Type", "application/sdp"))
        raw = sip.build_response(status, reason, headers, body)
        _LOGGER.info("SIP TX %s %s to %s:%s", status, reason, addr[0], addr[1])
        self._send(raw, addr)

    async def _handle_datagram(self, data: bytes, addr) -> None:
        try:
            request = sip.parse_message(data)
        except Exception as err:
            _LOGGER.info("SIP RX malformed from %s:%s: %s", addr[0], addr[1], err)
            return
        if not request.is_request:
            _LOGGER.info("SIP RX response ignored from %s:%s", addr[0], addr[1])
            return

        _LOGGER.info("SIP RX %s %s from %s:%s", request.method, request.uri, addr[0], addr[1])
        if request.method == "OPTIONS":
            self._send_response(request, addr, 200, "OK")
            return
        if request.method in {"CANCEL", "BYE"}:
            call_id = request.header("Call-ID")
            pending = self.pending_invites.pop(call_id, None)
            self._send_response(request, addr, 200, "OK")
            if pending is not None:
                self._send_response(pending.request, pending.addr, 487, "Request Terminated", to_tag=pending.to_tag)
            return
        if request.method == "ACK":
            return
        if request.method != "INVITE":
            self._send_response(request, addr, 481, "Call/Transaction Does Not Exist")
            return

        self._send_response(request, addr, 100, "Trying")
        invite = self._parse_invite(request, addr)
        if invite is None:
            self._send_response(request, addr, 488, "Not Acceptable Here", to_tag=sip.make_tag())
            return

        result = await self.on_invite(invite)
        to_tag = result.to_tag or sip.make_tag()
        if result.defer_final:
            self.pending_invites[invite.call_id] = _PendingInvite(request, addr, to_tag)
            self._send_response(request, addr, result.status, result.reason, to_tag=to_tag)
            return

        body = result.answer_sdp.encode("utf-8") if result.answer_sdp else b""
        self._send_response(
            request,
            addr,
            result.status,
            result.reason,
            body=body,
            to_tag=to_tag,
        )

    def send_final_response(self, call_id: str, status: int, reason: str, *, answer_sdp: str = "") -> bool:
        pending = self.pending_invites.pop(call_id, None)
        if pending is None:
            return False
        body = answer_sdp.encode("utf-8") if answer_sdp else b""
        self._send_response(
            pending.request,
            pending.addr,
            status,
            reason,
            body=body,
            to_tag=pending.to_tag,
        )
        return True

    def _parse_invite(self, request: sip.SipMessage, addr) -> SipInvite | None:
        try:
            request_uri = sip.parse_sip_uri(request.uri)
            from_uri = _uri_from_header(request.header("From"))
            selected = sdp.negotiate(request.body, self.supported_formats)
            if selected is None:
                _LOGGER.info("SIP INVITE rejected: no compatible PCM media in SDP")
                return None
            remote = sdp.parse_sdp(request.body)
            caller = from_uri.user if from_uri is not None else ""
            return SipInvite(
                source_host=addr[0],
                source_port=int(addr[1]),
                request_uri=request_uri,
                caller_uri=from_uri,
                target=request_uri.user,
                caller=caller,
                call_id=request.header("Call-ID"),
                cseq=request.header("CSeq"),
                remote_sdp=request.body,
                selected_format=selected,
                remote_rtp_host=remote["connection_ip"],
                remote_rtp_port=remote["media_port"],
            )
        except Exception as err:
            _LOGGER.info("SIP INVITE parse failed: %s", err)
            return None


class SipUdpServer:
    def __init__(
        self,
        *,
        host: str,
        port: int,
        local_ip: str,
        local_rtp_port: int,
        supported_formats: list[AudioFormat],
        on_invite: InviteHandler,
    ) -> None:
        self.host = host
        self.port = port
        self.local_ip = local_ip
        self.local_rtp_port = local_rtp_port
        self.supported_formats = supported_formats
        self.on_invite = on_invite
        self.transport: asyncio.DatagramTransport | None = None
        self.endpoint: SipUdpEndpoint | None = None

    async def start(self) -> bool:
        if self.transport is not None:
            return True
        loop = asyncio.get_running_loop()
        try:
            def _factory() -> SipUdpEndpoint:
                self.endpoint = SipUdpEndpoint(
                    local_ip=self.local_ip,
                    local_rtp_port=self.local_rtp_port,
                    supported_formats=self.supported_formats,
                    on_invite=self.on_invite,
                )
                return self.endpoint

            transport, _ = await loop.create_datagram_endpoint(
                _factory,
                local_addr=(self.host, self.port),
            )
        except OSError as err:
            _LOGGER.error("Failed to bind SIP UDP %s:%s: %s", self.host, self.port, err)
            return False
        self.transport = transport  # type: ignore[assignment]
        return True

    def send_final_response(self, call_id: str, status: int, reason: str, *, answer_sdp: str = "") -> bool:
        return self.endpoint is not None and self.endpoint.send_final_response(
            call_id,
            status,
            reason,
            answer_sdp=answer_sdp,
        )

    async def stop(self) -> None:
        if self.transport is not None:
            self.transport.close()
            self.transport = None
        self.endpoint = None
