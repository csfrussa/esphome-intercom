"""Outbound SIP/RTP primitives for the phase-1 VoIP Stack profile."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
import logging
import socket
from typing import Any, Callable

from .audio_format import AudioFormat, HA_SIP_PCM_FORMATS, PcmFormat
from . import g711
from .opus_codec import OpusDecoder, OpusEncoder
from . import rtp, sdp, sip
from .sip_auth import build_digest_authorization
from .sip_tcp_io import SipTcpWriter

_LOGGER = logging.getLogger(__name__)

SIP_T1 = 0.5
SIP_T2 = 4.0
SIP_TIMER_B = 64 * SIP_T1


def _rtp_encoding(fmt: AudioFormat | sdp.RtpPcmFormat) -> str:
    return getattr(fmt, "encoding", "")


def _audio_format(fmt: AudioFormat | sdp.RtpPcmFormat) -> AudioFormat:
    return fmt.audio_format if isinstance(fmt, sdp.RtpPcmFormat) else fmt


def pcm_to_rtp_payload(data: bytes, fmt: AudioFormat | sdp.RtpPcmFormat) -> bytes:
    encoding = _rtp_encoding(fmt)
    if encoding == "PCMA":
        return g711.s16le_to_alaw(data)
    if encoding == "PCMU":
        return g711.s16le_to_ulaw(data)
    if encoding == "OPUS":
        return OpusEncoder(fmt.sample_rate, fmt.channels).encode(data)
    fmt = _audio_format(fmt)
    if fmt.pcm_format == PcmFormat.S16LE:
        if len(data) % 2:
            raise ValueError("s16le frame length is not sample-aligned")
        return b"".join(data[i + 1:i + 2] + data[i:i + 1] for i in range(0, len(data), 2))
    if fmt.pcm_format == PcmFormat.S24LE:
        if len(data) % 3:
            raise ValueError("s24le frame length is not sample-aligned")
        return b"".join(data[i + 2:i + 3] + data[i + 1:i + 2] + data[i:i + 1] for i in range(0, len(data), 3))
    if fmt.pcm_format == PcmFormat.S24LE_IN_S32:
        if len(data) % 4:
            raise ValueError("s24le_in_s32 frame length is not sample-aligned")
        return b"".join(data[i + 2:i + 3] + data[i + 1:i + 2] + data[i:i + 1] for i in range(0, len(data), 4))
    raise ValueError(f"{fmt.pcm_format.value} has no phase-1 RTP mapping")


def rtp_payload_to_pcm(payload: bytes, fmt: AudioFormat | sdp.RtpPcmFormat) -> bytes:
    encoding = _rtp_encoding(fmt)
    if encoding == "PCMA":
        return g711.alaw_to_s16le(payload)
    if encoding == "PCMU":
        return g711.ulaw_to_s16le(payload)
    if encoding == "OPUS":
        return OpusDecoder(fmt.sample_rate, fmt.channels).decode(payload)
    fmt = _audio_format(fmt)
    if fmt.pcm_format == PcmFormat.S16LE:
        if len(payload) % 2:
            raise ValueError("L16 payload length is not sample-aligned")
        return b"".join(payload[i + 1:i + 2] + payload[i:i + 1] for i in range(0, len(payload), 2))
    if fmt.pcm_format == PcmFormat.S24LE:
        if len(payload) % 3:
            raise ValueError("L24 payload length is not sample-aligned")
        return b"".join(payload[i + 2:i + 3] + payload[i + 1:i + 2] + payload[i:i + 1] for i in range(0, len(payload), 3))
    if fmt.pcm_format == PcmFormat.S24LE_IN_S32:
        if len(payload) % 3:
            raise ValueError("L24 payload length is not sample-aligned")
        out = bytearray()
        for i in range(0, len(payload), 3):
            out.extend((payload[i + 2], payload[i + 1], payload[i], 0xFF if payload[i] & 0x80 else 0x00))
        return bytes(out)
    raise ValueError(f"{fmt.pcm_format.value} has no phase-1 RTP mapping")


class RtpPayloadDecoder:
    def __init__(self, fmt: sdp.RtpPcmFormat) -> None:
        self.fmt = fmt
        self._opus = OpusDecoder(fmt.sample_rate, fmt.channels) if fmt.encoding == "OPUS" else None

    def decode(self, payload: bytes) -> bytes:
        if self._opus is not None:
            return self._opus.decode(payload)
        return rtp_payload_to_pcm(payload, self.fmt)


class RtpPayloadEncoder:
    def __init__(self, fmt: sdp.RtpPcmFormat) -> None:
        self.fmt = fmt
        self._opus = OpusEncoder(fmt.sample_rate, fmt.channels) if fmt.encoding == "OPUS" else None

    def encode(self, pcm: bytes) -> bytes:
        if self._opus is not None:
            return self._opus.encode(pcm)
        return pcm_to_rtp_payload(pcm, self.fmt)


def _sip_decline_reason(msg: sip.SipMessage) -> str:
    direct = (msg.header("X-Voip-Stack-Decline-Reason") or "").strip()
    if direct:
        return direct
    reason = msg.header("Reason")
    marker = "text="
    idx = reason.find(marker)
    if idx < 0:
        return ""
    value = reason[idx + len(marker) :].strip()
    if not value:
        return ""
    if value[0] != '"':
        return value.split(";", 1)[0].strip()
    out: list[str] = []
    escaped = False
    for ch in value[1:]:
        if escaped:
            out.append(ch)
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            continue
        if ch == '"':
            break
        out.append(ch)
    return "".join(out).strip()


def _is_invite_progress_response(status_code: int | None) -> bool:
    return status_code is not None and 100 < int(status_code) < 200


def _sip_header_token(value: str) -> str:
    return "".join(
        ch
        for ch in str(value or "").strip()
        if ch.isalnum() or ch in " _-."
    ).strip()


@dataclass(slots=True)
class SipDialog:
    target: str
    remote_host: str
    remote_sip_port: int
    remote_rtp_host: str
    remote_rtp_port: int
    local_rtp_port: int
    call_id: str
    local_uri: str
    remote_uri: str
    send_format: sdp.RtpPcmFormat
    recv_format: sdp.RtpPcmFormat

    @property
    def selected_format(self) -> sdp.RtpPcmFormat:
        return self.send_format


class _SipClientProtocol(asyncio.DatagramProtocol):
    def __init__(self, queue: asyncio.Queue[tuple[bytes, tuple[str, int]]]) -> None:
        self.queue = queue
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr) -> None:
        self.queue.put_nowait((data, addr))


async def _read_sip_stream_message(reader: asyncio.StreamReader) -> bytes | None:
    try:
        head = await reader.readuntil(b"\r\n\r\n")
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError):
        return None
    try:
        text = head.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None
    content_length = 0
    for line in text.split("\r\n")[1:]:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        if key.strip().lower() == "content-length":
            try:
                content_length = int(value.strip())
            except ValueError:
                return None
    if content_length < 0 or content_length > sip.MAX_SIP_BODY_BYTES:
        return None
    body = await reader.readexactly(content_length) if content_length else b""
    return head + body


class SipCallClient:
    """One outbound SIP dialog.

    This is intentionally small and standards-shaped. It can call an ESP or HA
    SIP URI and expose the negotiated RTP parameters to a relay/session owner.
    """

    def __init__(
        self,
        *,
        local_ip: str,
        local_name: str,
        local_sip_port: int,
        local_rtp_port: int,
        supported_formats: list[AudioFormat] | None = None,
        supported_send_formats: list[AudioFormat] | None = None,
        supported_recv_formats: list[AudioFormat] | None = None,
        signaling_transport: str = "UDP",
        auth_username: str = "",
        username: str = "",
        password: str = "",
        outbound_proxy: str = "",
        include_common_codecs: bool = False,
    ) -> None:
        self.local_ip = local_ip
        self.local_name = local_name
        self.local_sip_port = int(local_sip_port)
        self.local_rtp_port = int(local_rtp_port)
        base_formats = supported_formats or list(HA_SIP_PCM_FORMATS)
        self.supported_send_formats = supported_send_formats or base_formats
        self.supported_recv_formats = supported_recv_formats or base_formats
        self.signaling_transport = (signaling_transport or "UDP").upper()
        self.auth_username = auth_username
        self.username = username or local_name
        self.password = password
        self.outbound_proxy = outbound_proxy
        self.include_common_codecs = bool(include_common_codecs)
        self.transport: asyncio.DatagramTransport | None = None
        self.protocol: _SipClientProtocol | None = None
        self.reader: asyncio.StreamReader | None = None
        self.writer: asyncio.StreamWriter | None = None
        self._tcp_writer: SipTcpWriter | None = None
        self._tcp_reuse_send: Callable[[bytes], None] | None = None
        self._tcp_reuse_responses: asyncio.Queue[bytes] | None = None
        self._tcp_reuse_close: Callable[[], None] | None = None
        self.queue: asyncio.Queue[tuple[bytes, tuple[str, int]]] = asyncio.Queue()
        self.dialog_ids = sip.SipDialogIds(call_id=sip.make_call_id("ha"), local_tag=sip.make_tag())
        self.dialog: SipDialog | None = None
        self._invite_cseq = self.dialog_ids.cseq
        self._pending_target = ""
        self._pending_remote_host = ""
        self._pending_remote_sip_port = 5060
        self._pending_request_uri = ""
        self._pending_local_uri = ""
        self._pending_remote_uri = ""
        self.last_sip_event = ""
        self.last_sip_status_code = 0
        self.last_sip_reason = ""

    def _mark_sip_event(self, event: str, status: int = 0, reason: str = "") -> None:
        self.last_sip_event = event
        if status:
            self.last_sip_status_code = int(status)
            self.last_sip_reason = reason or ""

    async def start(self) -> None:
        if self.signaling_transport == "TCP":
            return
        if self.transport is not None:
            return
        loop = asyncio.get_running_loop()
        self.protocol = _SipClientProtocol(self.queue)
        transport, _ = await loop.create_datagram_endpoint(
            lambda: self.protocol,
            local_addr=("0.0.0.0", 0),
            family=socket.AF_INET,
        )
        self.transport = transport  # type: ignore[assignment]
        sockname = transport.get_extra_info("sockname")
        if sockname and len(sockname) >= 2 and int(sockname[1]) > 0:
            self.local_sip_port = int(sockname[1])

    async def close(self) -> None:
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
        if self._tcp_reuse_close is not None:
            self._tcp_reuse_close()
            self._tcp_reuse_close = None
        self._tcp_reuse_send = None
        self._tcp_reuse_responses = None

    def use_reused_tcp_connection(
        self,
        *,
        send: Callable[[bytes], None],
        responses: asyncio.Queue[bytes],
        close: Callable[[], None],
    ) -> None:
        self._tcp_reuse_send = send
        self._tcp_reuse_responses = responses
        self._tcp_reuse_close = close

    async def _connect_tcp(self, remote_host: str, remote_sip_port: int) -> None:
        if self._tcp_reuse_send is not None:
            return
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
        host, port = self._signaling_target(remote_host, int(remote_sip_port))
        self.reader, self.writer = await asyncio.open_connection(host, port)
        self._tcp_writer = SipTcpWriter(self.writer, label=f"client {host}:{port}")
        sock = self.writer.get_extra_info("socket")
        if sock is not None:
            sockname = sock.getsockname()
            if sockname and len(sockname) >= 2 and int(sockname[1]) > 0:
                self.local_sip_port = int(sockname[1])

    def _signaling_target(self, remote_host: str, remote_sip_port: int) -> tuple[str, int]:
        proxy = str(self.outbound_proxy or "").strip()
        if not proxy:
            return remote_host, int(remote_sip_port)
        if proxy.startswith("sip:"):
            proxy = proxy[4:]
        proxy = proxy.split(";", 1)[0].strip()
        if "@" in proxy:
            proxy = proxy.rsplit("@", 1)[1]
        if ":" in proxy and proxy.count(":") == 1:
            host, port = proxy.rsplit(":", 1)
            try:
                return host.strip(), int(port)
            except ValueError:
                return host.strip(), int(remote_sip_port)
        return proxy, int(remote_sip_port)

    async def _send_raw(self, raw: bytes, remote_host: str, remote_sip_port: int) -> None:
        if self.signaling_transport == "TCP":
            if self._tcp_reuse_send is not None:
                self._tcp_reuse_send(raw)
                return
            await self._connect_tcp(remote_host, remote_sip_port)
            assert self._tcp_writer is not None
            await self._tcp_writer.send(raw)
            return
        assert self.transport is not None
        host, port = self._signaling_target(remote_host, int(remote_sip_port))
        self.transport.sendto(raw, (host, port))

    def _has_signaling_path(self) -> bool:
        if self.signaling_transport == "TCP":
            return self.writer is not None or self._tcp_reuse_send is not None
        return self.transport is not None

    def _send_dialog_request(self, raw: bytes, host: str, port: int) -> None:
        if self.signaling_transport == "TCP":
            if self._tcp_reuse_send is not None:
                self._tcp_reuse_send(raw)
                return
            if self.writer is None:
                return
            if self._tcp_writer is not None:
                self._tcp_writer.send_nowait(raw)
            return
        if self.transport is not None:
            host, port = self._signaling_target(host, int(port))
            self.transport.sendto(raw, (host, port))

    def _send_response_to_request(self, request: sip.SipMessage, host: str, port: int, status: int, reason: str) -> None:
        headers = [
            ("Via", request.header("Via")),
            ("From", request.header("From")),
            ("To", request.header("To")),
            ("Call-ID", request.header("Call-ID")),
            ("CSeq", request.header("CSeq")),
        ]
        raw = sip.build_response(status, reason, headers, b"")
        self._send_dialog_request(raw, host, int(port))
        self._mark_sip_event("SIP_RESPONSE", int(status), reason)
        _LOGGER.info("SIP TX %s %s to %s:%s", status, reason, host, port)

    async def _read_response(self, timeout: float) -> tuple[sip.SipMessage, tuple[str, int]] | None:
        if self.signaling_transport == "TCP":
            if self._tcp_reuse_responses is not None:
                try:
                    raw = await asyncio.wait_for(self._tcp_reuse_responses.get(), timeout=timeout)
                except asyncio.TimeoutError:
                    return None
                return sip.parse_message(raw), (self._pending_remote_host, self._pending_remote_sip_port)
            if self.reader is None:
                return None
            try:
                raw = await asyncio.wait_for(_read_sip_stream_message(self.reader), timeout=timeout)
            except asyncio.TimeoutError:
                return None
            if raw is None:
                return None
            return sip.parse_message(raw), (self._pending_remote_host, self._pending_remote_sip_port)
        try:
            data, addr = await asyncio.wait_for(self.queue.get(), timeout=timeout)
        except asyncio.TimeoutError:
            return None
        return sip.parse_message(data), addr

    async def invite(
        self,
        *,
        target: str,
        remote_host: str,
        remote_sip_port: int,
        request_uri: str = "",
        timeout: float = 8.0,
    ) -> str:
        try:
            if self.signaling_transport == "TCP" and self._tcp_reuse_send is None:
                await self._connect_tcp(remote_host, int(remote_sip_port))
            else:
                await self.start()
        except OSError as err:
            self._mark_sip_event("TRANSPORT_ERROR", 0, str(err))
            _LOGGER.info(
                "SIP transport unreachable target=%s host=%s:%s transport=%s error=%s",
                target,
                remote_host,
                remote_sip_port,
                self.signaling_transport,
                err,
            )
            return "transport_unreachable"
        transport_param = (("transport", self.signaling_transport.lower()),)
        request_uri = request_uri or str(sip.SipUri(target, remote_host, int(remote_sip_port), params=transport_param))
        sip.parse_sip_uri(request_uri)
        local_uri = str(sip.SipUri(self.local_name, self.local_ip, self.local_sip_port, params=transport_param))
        remote_uri = request_uri
        self._pending_target = target
        self._pending_remote_host = remote_host
        self._pending_remote_sip_port = int(remote_sip_port)
        self._pending_request_uri = request_uri
        self._pending_local_uri = local_uri
        self._pending_remote_uri = remote_uri
        body = sdp.build_offer_directional(
            self.local_ip,
            self.local_ip,
            self.local_rtp_port,
            self.supported_send_formats,
            self.supported_recv_formats,
            include_common_codecs=self.include_common_codecs,
        ).encode()
        headers = sip.dialog_headers(
            request_uri=request_uri,
            local_uri=local_uri,
            remote_uri=remote_uri,
            dialog=self.dialog_ids,
            method="INVITE",
            contact_uri=local_uri,
            content_type="application/sdp",
            transport=self.signaling_transport,
        )
        caller_name = _sip_header_token(self.local_name)
        dest_name = _sip_header_token(target)
        if caller_name:
            headers.append(("X-Voip-Stack-Caller-Name", caller_name))
            headers.append(("X-Voip-Stack-Caller-Route", caller_name))
        if dest_name:
            headers.append(("X-Voip-Stack-Dest-Name", dest_name))
            headers.append(("X-Voip-Stack-Dest-Route", dest_name))
        self._invite_cseq = self.dialog_ids.cseq
        raw = sip.build_request("INVITE", request_uri, headers, body)
        self._mark_sip_event("INVITE")
        await self._send_raw(raw, remote_host, int(remote_sip_port))
        _LOGGER.info(
            "SIP TX INVITE %s@%s:%s offered=[%s]",
            target,
            remote_host,
            remote_sip_port,
            ", ".join(sdp.offered_media_descriptions(body)),
        )
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        retransmit_interval = SIP_T1
        next_retransmit = loop.time() + retransmit_interval
        udp_invite_retransmits = 0
        auth_retried = False
        while True:
            now = loop.time()
            remaining = deadline - now
            if remaining <= 0:
                return "timeout"
            read_timeout = remaining
            if self.signaling_transport != "TCP":
                read_timeout = min(read_timeout, max(0.0, next_retransmit - now))
            try:
                received = await self._read_response(read_timeout)
                if received is None:
                    if self.signaling_transport != "TCP" and loop.time() < deadline:
                        await self._send_raw(raw, remote_host, int(remote_sip_port))
                        udp_invite_retransmits += 1
                        _LOGGER.debug(
                            "SIP UDP retransmit INVITE #%d %s@%s:%s",
                            udp_invite_retransmits,
                            target,
                            remote_host,
                            remote_sip_port,
                        )
                        retransmit_interval = min(retransmit_interval * 2, SIP_T2)
                        next_retransmit = loop.time() + retransmit_interval
                        continue
                    return "timeout"
                msg, addr = received
            except Exception as err:
                _LOGGER.info("SIP RX malformed: %s", err)
                continue
            if not msg.is_response:
                continue
            self._mark_sip_event("SIP_RESPONSE", int(msg.status_code or 0), msg.reason)
            _LOGGER.info("SIP RX %s %s from %s:%s", msg.status_code, msg.reason, addr[0], addr[1])
            if _is_invite_progress_response(msg.status_code):
                return "ringing"
            if msg.status_code and 200 <= msg.status_code < 300:
                if not self._commit_200_ok(msg, target, remote_host, int(remote_sip_port), request_uri, local_uri, remote_uri):
                    return "media_incompatible"
                return "in_call"
            if msg.status_code in {401, 407} and self.password and not auth_retried:
                self._send_invite_error_ack(msg, addr[0], addr[1])
                auth_retried = True
                auth_header = "Proxy-Authorization" if msg.status_code == 407 else "Authorization"
                challenge = msg.header("Proxy-Authenticate" if msg.status_code == 407 else "WWW-Authenticate")
                try:
                    auth_value = build_digest_authorization(
                        challenge_header=challenge,
                        username=self.username,
                        auth_username=self.auth_username,
                        password=self.password,
                        method="INVITE",
                        uri=request_uri,
                    )
                except Exception as err:
                    _LOGGER.info("SIP digest auth failed to build INVITE response: %s", err)
                    return sip.sip_failure_reason(msg.status_code)
                self.dialog_ids.cseq += 1
                self.dialog_ids.branch = sip.make_branch()
                self._invite_cseq = self.dialog_ids.cseq
                retry_headers = sip.dialog_headers(
                    request_uri=request_uri,
                    local_uri=local_uri,
                    remote_uri=remote_uri,
                    dialog=self.dialog_ids,
                    method="INVITE",
                    contact_uri=local_uri,
                    content_type="application/sdp",
                    transport=self.signaling_transport,
                )
                if caller_name:
                    retry_headers.append(("X-Voip-Stack-Caller-Name", caller_name))
                    retry_headers.append(("X-Voip-Stack-Caller-Route", caller_name))
                if dest_name:
                    retry_headers.append(("X-Voip-Stack-Dest-Name", dest_name))
                    retry_headers.append(("X-Voip-Stack-Dest-Route", dest_name))
                retry_headers.append((auth_header, auth_value))
                raw = sip.build_request("INVITE", request_uri, retry_headers, body)
                self._mark_sip_event("INVITE")
                await self._send_raw(raw, remote_host, int(remote_sip_port))
                retransmit_interval = SIP_T1
                next_retransmit = loop.time() + retransmit_interval
                continue
            if msg.status_code and msg.status_code >= 300:
                self._send_invite_error_ack(msg, addr[0], addr[1])
                return _sip_decline_reason(msg) or sip.sip_failure_reason(msg.status_code)

    async def wait_for_final(self, timeout: float = 60.0) -> str:
        """Continue an INVITE transaction after the first provisional progress response."""
        if self.dialog is not None:
            return "in_call"
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return "timeout"
            try:
                received = await self._read_response(remaining)
                if received is None:
                    return "timeout"
                msg, addr = received
            except Exception:
                continue
            if not msg.is_response or msg.status_code is None:
                continue
            self._mark_sip_event("SIP_RESPONSE", int(msg.status_code), msg.reason)
            _LOGGER.info("SIP RX %s %s from %s:%s", msg.status_code, msg.reason, addr[0], addr[1])
            if "CANCEL" in msg.header("CSeq").upper():
                continue
            if _is_invite_progress_response(msg.status_code):
                continue
            if 200 <= msg.status_code < 300:
                if not self._commit_200_ok(
                    msg,
                    self._pending_target,
                    self._pending_remote_host or addr[0],
                    self._pending_remote_sip_port,
                    self._pending_request_uri,
                    self._pending_local_uri,
                    self._pending_remote_uri,
                ):
                    return "media_incompatible"
                return "in_call"
            if msg.status_code >= 300:
                self._send_invite_error_ack(msg, addr[0], addr[1])
                return _sip_decline_reason(msg) or sip.sip_failure_reason(msg.status_code)

    async def wait_for_dialog_termination(self, timeout: float | None = None) -> str:
        """Wait for a remote BYE on a confirmed outbound dialog.

        Outbound HA-originated calls keep their SIP client alive after 200 OK so
        the same signaling path can receive the peer's BYE. When that happens we
        must acknowledge it and let the owner remove this client from its active
        dialog registry; otherwise the HA endpoint remains falsely busy.
        """
        if self.dialog is None:
            return "not_in_call"
        deadline = None if timeout is None else asyncio.get_running_loop().time() + float(timeout)
        while True:
            wait_timeout = 3600.0
            if deadline is not None:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    return "timeout"
                wait_timeout = max(0.05, remaining)
            try:
                received = await self._read_response(wait_timeout)
            except asyncio.TimeoutError:
                return "timeout" if deadline is not None else "remote_hangup"
            except Exception as err:
                _LOGGER.debug("SIP dialog termination wait ignored malformed message: %s", err)
                continue
            if received is None:
                return "remote_hangup"
            msg, addr = received
            if msg.is_response:
                if msg.status_code is not None:
                    self._mark_sip_event("SIP_RESPONSE", int(msg.status_code), msg.reason)
                    _LOGGER.info("SIP RX %s %s from %s:%s", msg.status_code, msg.reason, addr[0], addr[1])
                continue
            if msg.method == "BYE":
                _LOGGER.info("SIP RX BYE from %s:%s", addr[0], addr[1])
                self._send_response_to_request(msg, addr[0], addr[1], 200, "OK")
                self.dialog = None
                return "remote_hangup"
            if msg.method == "CANCEL":
                _LOGGER.info("SIP RX CANCEL from %s:%s", addr[0], addr[1])
                self._send_response_to_request(msg, addr[0], addr[1], 200, "OK")
                return "cancelled"
            if msg.method == "ACK":
                continue
            self._send_response_to_request(msg, addr[0], addr[1], 405, "Method Not Allowed")

    def _commit_200_ok(
        self,
        msg: sip.SipMessage,
        target: str,
        remote_host: str,
        remote_sip_port: int,
        request_uri: str,
        local_uri: str,
        remote_uri: str,
    ) -> bool:
        selected = sdp.negotiate_answer_directional(
            msg.body,
            self.supported_send_formats,
            self.supported_recv_formats,
        )
        if selected is None:
            try:
                offered = ", ".join(sdp.offered_media_descriptions(msg.body))
            except Exception as err:
                offered = f"unparseable SDP media: {err}"
            _LOGGER.info("SIP 200 OK rejected: no compatible answer media offered=[%s]", offered)
            return False
        parsed = sdp.parse_sdp(msg.body)
        self.dialog_ids.remote_tag = _extract_tag(msg.header("To"))
        if not request_uri:
            request_uri = str(sip.SipUri(target or "voip", remote_host, remote_sip_port))
        transport_param = (("transport", self.signaling_transport.lower()),)
        if not local_uri:
            local_uri = str(sip.SipUri(self.local_name, self.local_ip, self.local_sip_port, params=transport_param))
        if not remote_uri:
            remote_uri = request_uri
        self.dialog = SipDialog(
            target=target,
            remote_host=remote_host,
            remote_sip_port=int(remote_sip_port),
            remote_rtp_host=parsed["connection_ip"],
            remote_rtp_port=int(parsed["media_port"]),
            local_rtp_port=self.local_rtp_port,
            call_id=self.dialog_ids.call_id,
            local_uri=local_uri,
            remote_uri=remote_uri,
            send_format=selected.send,
            recv_format=selected.recv,
        )
        _LOGGER.info(
            "SIP 200 OK media selected call_id=%s tx=%s rx=%s answer=[%s]",
            self.dialog_ids.call_id,
            selected.send.wire_token(),
            selected.recv.wire_token(),
            ", ".join(sdp.offered_media_descriptions(msg.body)),
        )
        self._send_ack(remote_host, int(remote_sip_port), request_uri, local_uri, remote_uri)
        return True

    def _send_ack(self, host: str, port: int, request_uri: str, local_uri: str, remote_uri: str) -> None:
        if not self._has_signaling_path():
            return
        ack_ids = sip.SipDialogIds(
            call_id=self.dialog_ids.call_id,
            local_tag=self.dialog_ids.local_tag,
            remote_tag=self.dialog_ids.remote_tag,
            cseq=self._invite_cseq,
            branch=sip.make_branch(),
        )
        headers = sip.dialog_headers(
            request_uri=request_uri,
            local_uri=local_uri,
            remote_uri=remote_uri,
            dialog=ack_ids,
            method="ACK",
            contact_uri=local_uri,
            transport=self.signaling_transport,
        )
        raw = sip.build_request("ACK", request_uri, headers, b"")
        self._send_dialog_request(raw, host, port)
        self._mark_sip_event("ACK")
        _LOGGER.info("SIP TX ACK %s:%s", host, port)

    def _send_invite_error_ack(self, msg: sip.SipMessage, host: str, port: int) -> None:
        if not self._has_signaling_path():
            return
        request_uri = self._pending_request_uri
        local_uri = self._pending_local_uri
        remote_uri = self._pending_remote_uri
        if not request_uri or not local_uri or not remote_uri:
            return
        ack_ids = sip.SipDialogIds(
            call_id=self.dialog_ids.call_id,
            local_tag=self.dialog_ids.local_tag,
            remote_tag=_extract_tag(msg.header("To")),
            cseq=self._invite_cseq,
            branch=self.dialog_ids.branch,
        )
        headers = sip.dialog_headers(
            request_uri=request_uri,
            local_uri=local_uri,
            remote_uri=remote_uri,
            dialog=ack_ids,
            method="ACK",
            contact_uri=local_uri,
            transport=self.signaling_transport,
        )
        raw = sip.build_request("ACK", request_uri, headers, b"")
        self._send_dialog_request(raw, host, int(port))
        self._mark_sip_event("ACK")
        _LOGGER.info("SIP TX ACK final INVITE error %s:%s", host, port)

    def bye(self) -> None:
        if not self._has_signaling_path() or self.dialog is None:
            return
        bye_ids = sip.SipDialogIds(
            call_id=self.dialog_ids.call_id,
            local_tag=self.dialog_ids.local_tag,
            remote_tag=self.dialog_ids.remote_tag,
            cseq=self._invite_cseq + 1,
            branch=sip.make_branch(),
        )
        headers = sip.dialog_headers(
            request_uri=self.dialog.remote_uri,
            local_uri=self.dialog.local_uri,
            remote_uri=self.dialog.remote_uri,
            dialog=bye_ids,
            method="BYE",
            contact_uri=self.dialog.local_uri,
            transport=self.signaling_transport,
        )
        raw = sip.build_request("BYE", self.dialog.remote_uri, headers, b"")
        self._send_dialog_request(raw, self.dialog.remote_host, self.dialog.remote_sip_port)
        self._mark_sip_event("BYE")
        _LOGGER.info("SIP TX BYE %s:%s", self.dialog.remote_host, self.dialog.remote_sip_port)

    def cancel(self) -> bool:
        """Cancel an INVITE transaction before a final 2xx response.

        SIP uses CANCEL, not BYE, while the INVITE is still in early dialog.
        The CANCEL reuses the INVITE CSeq number and top Via branch so the
        peer can match it to the pending transaction.
        """
        if not self._has_signaling_path() or not self._pending_request_uri:
            _LOGGER.info(
                "SIP CANCEL skipped: no signaling path call_id=%s transport=%s pending_uri=%s",
                self.dialog_ids.call_id,
                self.signaling_transport,
                bool(self._pending_request_uri),
            )
            return False
        cancel_ids = sip.SipDialogIds(
            call_id=self.dialog_ids.call_id,
            local_tag=self.dialog_ids.local_tag,
            remote_tag="",
            cseq=self._invite_cseq,
            branch=self.dialog_ids.branch,
        )
        headers = sip.dialog_headers(
            request_uri=self._pending_request_uri,
            local_uri=self._pending_local_uri,
            remote_uri=self._pending_remote_uri,
            dialog=cancel_ids,
            method="CANCEL",
            contact_uri=self._pending_local_uri,
            transport=self.signaling_transport,
        )
        raw = sip.build_request("CANCEL", self._pending_request_uri, headers, b"")
        self._send_dialog_request(raw, self._pending_remote_host, self._pending_remote_sip_port)
        self._mark_sip_event("CANCEL")
        _LOGGER.info("SIP TX CANCEL %s:%s", self._pending_remote_host, self._pending_remote_sip_port)
        return True

    def bye_or_cancel(self) -> None:
        if self.dialog is not None:
            self.bye()
        else:
            self.cancel()

    async def terminate(self, timeout: float = 1.5) -> str:
        """Terminate the SIP dialog/transaction and wait for the SIP response.

        A confirmed dialog ends with BYE + 200 OK. An early INVITE transaction
        ends with CANCEL + 200 OK to CANCEL and a final 487 for the INVITE.
        Keeping the socket alive for that exchange avoids leaving ESP phones in
        ringing state while HA already moved back to idle.
        """
        if self.dialog is not None:
            self.bye()
            deadline = asyncio.get_running_loop().time() + timeout
            while asyncio.get_running_loop().time() < deadline:
                try:
                    received = await self._read_response(max(0.05, deadline - asyncio.get_running_loop().time()))
                except asyncio.TimeoutError:
                    break
                except Exception:
                    continue
                if received is None:
                    break
                msg, addr = received
                if not msg.is_response or msg.status_code is None:
                    continue
                self._mark_sip_event("SIP_RESPONSE", int(msg.status_code), msg.reason)
                _LOGGER.info("SIP RX %s %s from %s:%s", msg.status_code, msg.reason, addr[0], addr[1])
                if 200 <= msg.status_code < 300:
                    return "remote_hangup"
            return "timeout"

        sent_cancel = self.cancel()
        if not sent_cancel:
            return "transport_unreachable"
        saw_cancel_ok = False
        saw_invite_terminated = False
        deadline = asyncio.get_running_loop().time() + timeout
        while asyncio.get_running_loop().time() < deadline:
            try:
                received = await self._read_response(max(0.05, deadline - asyncio.get_running_loop().time()))
            except asyncio.TimeoutError:
                break
            except Exception:
                continue
            if received is None:
                break
            msg, addr = received
            if not msg.is_response or msg.status_code is None:
                continue
            self._mark_sip_event("SIP_RESPONSE", int(msg.status_code), msg.reason)
            _LOGGER.info("SIP RX %s %s from %s:%s", msg.status_code, msg.reason, addr[0], addr[1])
            cseq = msg.header("CSeq").upper()
            if "CANCEL" in cseq and 200 <= msg.status_code < 300:
                saw_cancel_ok = True
            elif "INVITE" in cseq and msg.status_code == 487:
                saw_invite_terminated = True
            elif msg.status_code >= 300:
                saw_invite_terminated = True
            if saw_cancel_ok and saw_invite_terminated:
                return "cancelled"
        if saw_cancel_ok or saw_invite_terminated:
            return "cancelled"
        return "timeout"

    def snapshot(self) -> dict[str, Any]:
        dialog = self.dialog
        return {
            "call_id": self.dialog_ids.call_id,
            "local_uri": dialog.local_uri if dialog is not None else self._pending_local_uri,
            "remote_uri": dialog.remote_uri if dialog is not None else self._pending_remote_uri,
            "remote_host": dialog.remote_host if dialog is not None else self._pending_remote_host,
            "remote_sip_port": dialog.remote_sip_port if dialog is not None else self._pending_remote_sip_port,
            "remote_rtp_host": dialog.remote_rtp_host if dialog is not None else "",
            "remote_rtp_port": dialog.remote_rtp_port if dialog is not None else 0,
            "local_rtp_port": dialog.local_rtp_port if dialog is not None else self.local_rtp_port,
            "selected_tx_format": dialog.send_format.audio_format.wire_token() if dialog is not None else "",
            "selected_rx_format": dialog.recv_format.audio_format.wire_token() if dialog is not None else "",
            "selected_tx_rtp_format": dialog.send_format.wire_token() if dialog is not None else "",
            "selected_rx_rtp_format": dialog.recv_format.wire_token() if dialog is not None else "",
            "dialog_active": dialog is not None,
            "pending_invite": bool(self._pending_request_uri and dialog is None),
            "sip_transport": self.signaling_transport.lower(),
            "last_sip_event": self.last_sip_event,
            "last_sip_status_code": self.last_sip_status_code,
            "last_sip_reason": self.last_sip_reason,
        }


def _extract_tag(header: str) -> str:
    for part in (header or "").split(";"):
        part = part.strip()
        if part.startswith("tag="):
            return part.removeprefix("tag=")
    return ""
