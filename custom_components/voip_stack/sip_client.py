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
from .queue_utils import put_drop_oldest
from . import sdp, sip
from .sip_auth import build_digest_authorization
from .sip_tcp_io import SipTcpWriter, read_sip_stream_message as _read_sip_stream_message

_LOGGER = logging.getLogger(__name__)

SIP_T1 = 0.5
SIP_T2 = 4.0
SIP_TIMER_B = 64 * SIP_T1
_S24_SIGN_EXTENSION = bytes(0xFF if value & 0x80 else 0x00 for value in range(256))


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
        out = bytearray(len(data))
        out[0::2] = data[1::2]
        out[1::2] = data[0::2]
        return bytes(out)
    if fmt.pcm_format == PcmFormat.S24LE:
        if len(data) % 3:
            raise ValueError("s24le frame length is not sample-aligned")
        out = bytearray(len(data))
        out[0::3] = data[2::3]
        out[1::3] = data[1::3]
        out[2::3] = data[0::3]
        return bytes(out)
    if fmt.pcm_format == PcmFormat.S24LE_IN_S32:
        if len(data) % 4:
            raise ValueError("s24le_in_s32 frame length is not sample-aligned")
        samples = len(data) // 4
        out = bytearray(samples * 3)
        out[0::3] = data[2::4]
        out[1::3] = data[1::4]
        out[2::3] = data[0::4]
        return bytes(out)
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
        out = bytearray(len(payload))
        out[0::2] = payload[1::2]
        out[1::2] = payload[0::2]
        return bytes(out)
    if fmt.pcm_format == PcmFormat.S24LE:
        if len(payload) % 3:
            raise ValueError("L24 payload length is not sample-aligned")
        out = bytearray(len(payload))
        out[0::3] = payload[2::3]
        out[1::3] = payload[1::3]
        out[2::3] = payload[0::3]
        return bytes(out)
    if fmt.pcm_format == PcmFormat.S24LE_IN_S32:
        if len(payload) % 3:
            raise ValueError("L24 payload length is not sample-aligned")
        samples = len(payload) // 3
        out = bytearray(samples * 4)
        out[0::4] = payload[2::3]
        out[1::4] = payload[1::3]
        out[2::4] = payload[0::3]
        out[3::4] = payload[0::3].translate(_S24_SIGN_EXTENSION)
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
    remote_target_uri: str = ""
    dtmf_payload_type: int | None = None

    @property
    def selected_format(self) -> sdp.RtpPcmFormat:
        return self.send_format


class _SipClientProtocol(asyncio.DatagramProtocol):
    def __init__(self, queue: asyncio.Queue[tuple[bytes, tuple[str, int]]]) -> None:
        self.queue = queue
        self.transport: asyncio.DatagramTransport | None = None
        self.dropped_packets = 0

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr) -> None:
        if put_drop_oldest(self.queue, (data, addr)):
            self.dropped_packets += 1


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
        self._tcp_reuse_send: Callable[[bytes], bool | None] | None = None
        self._tcp_reuse_responses: asyncio.Queue[bytes] | None = None
        self._tcp_reuse_close: Callable[[], None] | None = None
        self.queue: asyncio.Queue[tuple[bytes, tuple[str, int]]] = asyncio.Queue(maxsize=128)
        self.dialog_ids = sip.SipDialogIds(call_id=sip.make_call_id("ha"), local_tag=sip.make_tag())
        self.dialog: SipDialog | None = None
        self.on_info_dtmf: Callable[[str], None] | None = None
        self._invite_cseq = self.dialog_ids.cseq
        self._pending_target = ""
        self._pending_remote_host = ""
        self._pending_remote_sip_port = 5060
        self._pending_request_uri = ""
        self._pending_local_uri = ""
        self._pending_remote_uri = ""
        self._invite_transaction_active = False
        self._cancel_requested = False
        self._cancel_sent = False
        self._received_provisional = False
        self._invite_task: asyncio.Task[str] | None = None
        self._bye_cseq = 0
        self._bye_branch = ""
        self.last_sip_event = ""
        self.last_sip_status_code = 0
        self.last_sip_reason = ""

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
        if self._invite_transaction_active:
            with contextlib.suppress(Exception):
                self.cancel()
            self._invite_transaction_active = False
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
        send: Callable[[bytes], bool | None],
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
                if self._tcp_reuse_send(raw) is False:
                    raise ConnectionError("reused SIP TCP connection is not writable")
                return
            await self._connect_tcp(remote_host, remote_sip_port)
            if self._tcp_writer is None:
                raise ConnectionError("SIP TCP writer is not available")
            if not await self._tcp_writer.send(raw):
                raise ConnectionError("SIP TCP connection is not writable")
            return
        if self.transport is None:
            raise ConnectionError("SIP UDP transport is not available")
        host, port = self._signaling_target(remote_host, int(remote_sip_port))
        self.transport.sendto(raw, (host, port))

    def _has_signaling_path(self) -> bool:
        if self.signaling_transport == "TCP":
            return self.writer is not None or self._tcp_reuse_send is not None
        return self.transport is not None

    def _send_dialog_request(self, raw: bytes, host: str, port: int) -> bool:
        try:
            if self.signaling_transport == "TCP":
                if self._tcp_reuse_send is not None:
                    return self._tcp_reuse_send(raw) is not False
                if self.writer is None:
                    return False
                if self._tcp_writer is not None:
                    return self._tcp_writer.send_nowait(raw)
                return False
            if self.transport is not None:
                host, port = self._signaling_target(host, int(port))
                self.transport.sendto(raw, (host, port))
                return True
        except (ConnectionError, OSError, RuntimeError) as err:
            _LOGGER.debug("SIP dialog send failed for %s:%s: %s", host, port, err)
        return False

    def _dialog_next_hop(self, request_uri: str, fallback_host: str, fallback_port: int) -> tuple[str, int]:
        """Resolve an in-dialog target while preserving an explicit proxy."""
        if self.outbound_proxy:
            return fallback_host, int(fallback_port)
        try:
            target = sip.parse_sip_uri(request_uri)
        except (TypeError, ValueError, sip.SipError):
            return fallback_host, int(fallback_port)
        return target.host, int(target.port or 5060)

    def _response_matches_transaction(
        self,
        message: sip.SipMessage,
        *,
        method: str,
        cseq: int,
        branch: str,
    ) -> bool:
        if not message.is_response:
            return False
        try:
            response_cseq = sip.parse_cseq(message.header("CSeq"))
            via_values = message.header_values("Via")
            response_branch = sip.parse_via(via_values[0] if via_values else "").branch
        except (TypeError, ValueError, sip.SipError):
            return False
        return (
            response_cseq.method == method.upper()
            and response_cseq.number == int(cseq)
            and bool(branch)
            and response_branch == branch
        )

    def _ack_retransmitted_invite_2xx(self, message: sip.SipMessage) -> bool:
        dialog = self.dialog
        if (
            dialog is None
            or message.status_code is None
            or not 200 <= message.status_code < 300
            or not self._response_matches_transaction(
                message,
                method="INVITE",
                cseq=self._invite_cseq,
                branch=self.dialog_ids.branch,
            )
        ):
            return False
        self._send_ack(
            dialog.remote_host,
            dialog.remote_sip_port,
            dialog.remote_target_uri or dialog.remote_uri,
            dialog.local_uri,
            dialog.remote_uri,
        )
        return True

    def _send_response_to_request(
        self,
        request: sip.SipMessage,
        host: str,
        port: int,
        status: int,
        reason: str,
        *,
        extra_headers: tuple[tuple[str, str], ...] = (),
    ) -> None:
        headers = [
            *(("Via", value) for value in request.header_values("Via")),
            ("From", request.header("From")),
            ("To", request.header("To")),
            ("Call-ID", request.header("Call-ID")),
            ("CSeq", request.header("CSeq")),
            *extra_headers,
        ]
        raw = sip.build_response(status, reason, headers, b"")
        if not self._send_dialog_request(raw, host, int(port)):
            _LOGGER.warning("SIP TX %s %s dropped: signaling path unavailable", status, reason)
            return
        sip.mark_sip_event(self, "SIP_RESPONSE", int(status), reason)
        _LOGGER.info("SIP TX %s %s to %s:%s", status, reason, host, port)

    def _request_matches_dialog(self, request: sip.SipMessage, host: str, method: str) -> bool:
        dialog = self.dialog
        if dialog is None:
            return False
        try:
            cseq = sip.parse_cseq(request.header("CSeq"))
        except (TypeError, ValueError, sip.SipError):
            return False
        remote_host = getattr(dialog, "remote_host", host)
        remote_port = int(getattr(dialog, "remote_sip_port", 5060))
        remote_target = getattr(dialog, "remote_target_uri", "")
        allowed_hosts = {
            remote_host,
            self._signaling_target(remote_host, remote_port)[0],
            self._dialog_next_hop(
                remote_target,
                remote_host,
                remote_port,
            )[0],
        }
        return (
            cseq.method == method.upper()
            and cseq.number > 0
            and host in allowed_hosts
            and sip.extract_tag(request.header("From")) == self.dialog_ids.remote_tag
            and sip.extract_tag(request.header("To")) == self.dialog_ids.local_tag
        )

    def _transport_failure(self, err: BaseException, target: str, remote_host: str, remote_sip_port: int) -> str:
        self._invite_transaction_active = False
        sip.mark_sip_event(self, "TRANSPORT_ERROR", 0, str(err))
        _LOGGER.info(
            "SIP transport unreachable target=%s host=%s:%s transport=%s error=%s",
            target,
            remote_host,
            remote_sip_port,
            self.signaling_transport,
            err,
        )
        return "transport_unreachable"

    async def _read_response(self, timeout: float) -> tuple[sip.SipMessage, tuple[str, int]] | None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + max(0.0, float(timeout))
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return None
            try:
                if self.signaling_transport == "TCP":
                    if self._tcp_reuse_responses is not None:
                        raw = await asyncio.wait_for(self._tcp_reuse_responses.get(), timeout=remaining)
                    else:
                        if self.reader is None:
                            return None
                        raw = await asyncio.wait_for(_read_sip_stream_message(self.reader), timeout=remaining)
                        if raw is None:
                            return None
                    addr = (self._pending_remote_host, self._pending_remote_sip_port)
                else:
                    raw, addr = await asyncio.wait_for(self.queue.get(), timeout=remaining)
            except asyncio.TimeoutError:
                return None

            message = sip.parse_message(raw)
            response_call_id = message.header("Call-ID")
            if response_call_id != self.dialog_ids.call_id:
                _LOGGER.debug(
                    "SIP message ignored for stale call_id=%s current=%s",
                    response_call_id or "(empty)",
                    self.dialog_ids.call_id,
                )
                continue
            return message, addr

    async def invite(
        self,
        *,
        target: str,
        remote_host: str,
        remote_sip_port: int,
        request_uri: str = "",
        timeout: float = 8.0,
    ) -> str:
        """Run one owned INVITE transaction that survives caller-task cancellation."""
        if self._invite_task is not None and not self._invite_task.done():
            raise RuntimeError("INVITE transaction already active")
        task = asyncio.create_task(
            self._run_invite(
                target=target,
                remote_host=remote_host,
                remote_sip_port=remote_sip_port,
                request_uri=request_uri,
                timeout=timeout,
            )
        )
        self._invite_task = task
        try:
            return await asyncio.shield(task)
        except asyncio.CancelledError:
            self.request_cancel()
            raise

    async def _run_invite(
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
        except (ConnectionError, OSError, RuntimeError) as err:
            return self._transport_failure(err, target, remote_host, remote_sip_port)
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
        self._invite_transaction_active = True
        self._cancel_requested = False
        self._cancel_sent = False
        self._received_provisional = False
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
        sip.mark_sip_event(self, "INVITE")
        try:
            await self._send_raw(raw, remote_host, int(remote_sip_port))
        except (ConnectionError, OSError, RuntimeError) as err:
            self._invite_transaction_active = False
            return self._transport_failure(err, target, remote_host, remote_sip_port)
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
        received_provisional = False
        while True:
            if self._cancel_requested and received_provisional and not self._cancel_sent:
                self._send_cancel()
            now = loop.time()
            remaining = deadline - now
            if remaining <= 0:
                return "timeout"
            read_timeout = remaining
            if self.signaling_transport != "TCP" and not received_provisional:
                read_timeout = min(read_timeout, max(0.0, next_retransmit - now))
            try:
                received = await self._read_response(read_timeout)
                if received is None:
                    if self.signaling_transport != "TCP" and not received_provisional and loop.time() < deadline:
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
            except (ConnectionError, OSError, RuntimeError) as err:
                return self._transport_failure(err, target, remote_host, remote_sip_port)
            except Exception as err:
                _LOGGER.info("SIP RX malformed: %s", err)
                continue
            if not msg.is_response:
                continue
            if not self._response_matches_transaction(
                msg,
                method="INVITE",
                cseq=self._invite_cseq,
                branch=self.dialog_ids.branch,
            ):
                _LOGGER.debug("SIP response ignored for non-active INVITE transaction")
                continue
            sip.mark_sip_event(self, "SIP_RESPONSE", int(msg.status_code or 0), msg.reason)
            _LOGGER.info("SIP RX %s %s from %s:%s", msg.status_code, msg.reason, addr[0], addr[1])
            if msg.status_code is not None and 100 <= msg.status_code < 200:
                received_provisional = True
                self._received_provisional = True
                if self._cancel_requested and not self._cancel_sent:
                    self._send_cancel()
                if _is_invite_progress_response(msg.status_code):
                    if self._cancel_requested:
                        continue
                    return "ringing"
                continue
            if msg.status_code and 200 <= msg.status_code < 300:
                if not self._commit_200_ok(msg, target, remote_host, int(remote_sip_port), request_uri, local_uri, remote_uri):
                    return "media_incompatible"
                if self._cancel_requested:
                    self.bye()
                    return "cancelled"
                return "in_call"
            if msg.status_code in {401, 407} and self.password and not auth_retried:
                self._send_invite_error_ack(msg, addr[0], addr[1])
                if self._cancel_requested:
                    self._invite_transaction_active = False
                    return "cancelled"
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
                sip.mark_sip_event(self, "INVITE")
                try:
                    await self._send_raw(raw, remote_host, int(remote_sip_port))
                except (ConnectionError, OSError, RuntimeError) as err:
                    return self._transport_failure(err, target, remote_host, remote_sip_port)
                retransmit_interval = SIP_T1
                next_retransmit = loop.time() + retransmit_interval
                received_provisional = False
                continue
            if msg.status_code and msg.status_code >= 300:
                self._send_invite_error_ack(msg, addr[0], addr[1])
                self._invite_transaction_active = False
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
            if not self._response_matches_transaction(
                msg,
                method="INVITE",
                cseq=self._invite_cseq,
                branch=self.dialog_ids.branch,
            ):
                continue
            sip.mark_sip_event(self, "SIP_RESPONSE", int(msg.status_code), msg.reason)
            _LOGGER.info("SIP RX %s %s from %s:%s", msg.status_code, msg.reason, addr[0], addr[1])
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
                self._invite_transaction_active = False
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
                    sip.mark_sip_event(self, "SIP_RESPONSE", int(msg.status_code), msg.reason)
                    _LOGGER.info("SIP RX %s %s from %s:%s", msg.status_code, msg.reason, addr[0], addr[1])
                    self._ack_retransmitted_invite_2xx(msg)
                continue
            if msg.method == "BYE":
                _LOGGER.info("SIP RX BYE from %s:%s", addr[0], addr[1])
                if not self._request_matches_dialog(msg, addr[0], "BYE"):
                    self._send_response_to_request(msg, addr[0], addr[1], 481, "Call/Transaction Does Not Exist")
                    continue
                self._send_response_to_request(msg, addr[0], addr[1], 200, "OK")
                self.dialog = None
                return "remote_hangup"
            if msg.method == "CANCEL":
                _LOGGER.info("SIP RX CANCEL from %s:%s", addr[0], addr[1])
                # CANCEL only applies to an early INVITE transaction. Once the
                # dialog is confirmed it must not tear the call down.
                self._send_response_to_request(msg, addr[0], addr[1], 481, "Call/Transaction Does Not Exist")
                continue
            if msg.method == "ACK":
                continue
            if msg.method == "INVITE":
                if not self._request_matches_dialog(msg, addr[0], "INVITE"):
                    self._send_response_to_request(msg, addr[0], addr[1], 481, "Call/Transaction Does Not Exist")
                    continue
                # Re-INVITE/hold is not part of the current media profile.
                # Reject the new offer without changing the established
                # dialog; RFC 3261 section 14 requires the old session to
                # continue after a non-2xx response.
                self._send_response_to_request(
                    msg,
                    addr[0],
                    addr[1],
                    488,
                    "Not Acceptable Here",
                    extra_headers=(("Warning", f'399 {self.local_ip} "Session renegotiation is not supported"'),),
                )
                continue
            if msg.method in {"INFO", "OPTIONS"}:
                if msg.method == "INFO":
                    from .dtmf import parse_sip_info_digit

                    digit = parse_sip_info_digit(msg.header("Content-Type"), msg.body)
                    if digit and self.on_info_dtmf is not None:
                        try:
                            self.on_info_dtmf(digit)
                        except Exception as err:  # noqa: BLE001 - event consumers cannot break the SIP dialog.
                            _LOGGER.warning("SIP INFO DTMF callback failed: %s", err)
                self._send_response_to_request(msg, addr[0], addr[1], 200, "OK")
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
        self._invite_transaction_active = False
        if not request_uri:
            request_uri = str(sip.SipUri(target or "voip", remote_host, remote_sip_port))
        transport_param = (("transport", self.signaling_transport.lower()),)
        if not local_uri:
            local_uri = str(sip.SipUri(self.local_name, self.local_ip, self.local_sip_port, params=transport_param))
        if not remote_uri:
            remote_uri = request_uri
        remote_target_uri = request_uri
        contact = msg.header("Contact")
        if contact:
            try:
                remote_target_uri = str(sip.parse_sip_uri(contact))
            except (TypeError, ValueError, sip.SipError):
                _LOGGER.info("SIP 200 OK has invalid Contact; retaining original remote target")
        negotiation_error: Exception | None = None
        try:
            selected = sdp.negotiate_answer_directional(
                msg.body,
                self.supported_send_formats,
                self.supported_recv_formats,
            )
        except Exception as err:
            selected = None
            negotiation_error = err
        if selected is None:
            try:
                offered = ", ".join(sdp.offered_media_descriptions(msg.body))
            except Exception as err:
                offered = f"unparseable SDP media: {err}"
            _LOGGER.info(
                "SIP 200 OK rejected: no compatible answer media offered=[%s] error=%s",
                offered,
                negotiation_error or "none",
            )
            self.dialog_ids.remote_tag = sip.extract_tag(msg.header("To"))
            self._send_ack(remote_host, int(remote_sip_port), remote_target_uri, local_uri, remote_uri)
            self._send_bye_request(remote_host, int(remote_sip_port), remote_target_uri, local_uri, remote_uri)
            return False
        parsed = sdp.parse_sdp(msg.body)
        dtmf_formats = sdp.offered_dtmf_formats(msg.body)
        self.dialog_ids.remote_tag = sip.extract_tag(msg.header("To"))
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
            remote_target_uri=remote_target_uri,
            dtmf_payload_type=dtmf_formats[0].payload_type if dtmf_formats else None,
        )
        _LOGGER.info(
            "SIP 200 OK media selected call_id=%s tx=%s rx=%s answer=[%s]",
            self.dialog_ids.call_id,
            selected.send.wire_token(),
            selected.recv.wire_token(),
            ", ".join(sdp.offered_media_descriptions(msg.body)),
        )
        self._send_ack(remote_host, int(remote_sip_port), remote_target_uri, local_uri, remote_uri)
        return True

    def _send_ack(self, host: str, port: int, request_uri: str, local_uri: str, remote_uri: str) -> bool:
        if not self._has_signaling_path():
            return False
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
        next_host, next_port = self._dialog_next_hop(request_uri, host, int(port))
        if not self._send_dialog_request(raw, next_host, next_port):
            _LOGGER.warning("SIP TX ACK dropped: signaling path unavailable")
            return False
        sip.mark_sip_event(self, "ACK")
        _LOGGER.info("SIP TX ACK %s:%s", next_host, next_port)
        return True

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
            remote_tag=sip.extract_tag(msg.header("To")),
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
        if not self._send_dialog_request(raw, host, int(port)):
            _LOGGER.warning("SIP TX ACK final INVITE error dropped: signaling path unavailable")
            return
        sip.mark_sip_event(self, "ACK")
        _LOGGER.info("SIP TX ACK final INVITE error %s:%s", host, port)

    def _send_bye_request(
        self,
        host: str,
        port: int,
        request_uri: str,
        local_uri: str,
        remote_uri: str,
    ) -> bool:
        if not self._has_signaling_path() or not request_uri or not local_uri or not remote_uri:
            return False
        bye_ids = sip.SipDialogIds(
            call_id=self.dialog_ids.call_id,
            local_tag=self.dialog_ids.local_tag,
            remote_tag=self.dialog_ids.remote_tag,
            cseq=self._invite_cseq + 1,
            branch=sip.make_branch(),
        )
        headers = sip.dialog_headers(
            request_uri=request_uri,
            local_uri=local_uri,
            remote_uri=remote_uri,
            dialog=bye_ids,
            method="BYE",
            contact_uri=local_uri,
            transport=self.signaling_transport,
        )
        raw = sip.build_request("BYE", request_uri, headers, b"")
        next_host, next_port = self._dialog_next_hop(request_uri, host, int(port))
        if not self._send_dialog_request(raw, next_host, next_port):
            _LOGGER.warning("SIP TX BYE dropped: signaling path unavailable")
            return False
        self._bye_cseq = bye_ids.cseq
        self._bye_branch = bye_ids.branch
        sip.mark_sip_event(self, "BYE")
        _LOGGER.info("SIP TX BYE %s:%s", next_host, next_port)
        return True

    def bye(self) -> bool:
        if self.dialog is None:
            return False
        return self._send_bye_request(
            self.dialog.remote_host,
            self.dialog.remote_sip_port,
            self.dialog.remote_target_uri or self.dialog.remote_uri,
            self.dialog.local_uri,
            self.dialog.remote_uri,
        )

    def request_cancel(self) -> bool:
        """Request cancellation by the coroutine that owns the INVITE transaction."""
        if (
            not self._invite_transaction_active
            or not self._has_signaling_path()
            or not self._pending_request_uri
        ):
            _LOGGER.info(
                "SIP CANCEL skipped: no signaling path call_id=%s transport=%s pending_uri=%s",
                self.dialog_ids.call_id,
                self.signaling_transport,
                bool(self._pending_request_uri),
            )
            return False
        self._cancel_requested = True
        if self._received_provisional:
            return self._send_cancel()
        return True

    def _send_cancel(self) -> bool:
        """Send CANCEL after the INVITE has entered the proceeding state."""
        if self._cancel_sent:
            return True
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
        if not self._send_dialog_request(raw, self._pending_remote_host, self._pending_remote_sip_port):
            _LOGGER.warning("SIP TX CANCEL dropped: signaling path unavailable")
            return False
        self._cancel_sent = True
        sip.mark_sip_event(self, "CANCEL")
        _LOGGER.info("SIP TX CANCEL %s:%s", self._pending_remote_host, self._pending_remote_sip_port)
        return True

    def cancel(self) -> bool:
        """Cancel now, or defer until the INVITE receives a provisional response."""
        sent_or_deferred = self.request_cancel()
        if sent_or_deferred and not self._received_provisional:
            _LOGGER.info("SIP CANCEL deferred until provisional call_id=%s", self.dialog_ids.call_id)
        return sent_or_deferred

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
            if not self.bye():
                return "transport_unreachable"
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
                sip.mark_sip_event(self, "SIP_RESPONSE", int(msg.status_code), msg.reason)
                _LOGGER.info("SIP RX %s %s from %s:%s", msg.status_code, msg.reason, addr[0], addr[1])
                if self._ack_retransmitted_invite_2xx(msg):
                    continue
                if self._response_matches_transaction(
                    msg,
                    method="BYE",
                    cseq=self._bye_cseq,
                    branch=self._bye_branch,
                ) and 200 <= msg.status_code < 300:
                    return "remote_hangup"
            return "timeout"

        invite_task = self._invite_task
        if invite_task is not None and not invite_task.done():
            if not self.request_cancel():
                return "transport_unreachable"
            try:
                return await asyncio.wait_for(asyncio.shield(invite_task), timeout=timeout)
            except asyncio.TimeoutError:
                invite_task.add_done_callback(
                    lambda _task: asyncio.create_task(self.close())
                )
                return "cancel_pending"

        sent_cancel = self.cancel()
        if not sent_cancel:
            return "transport_unreachable"
        saw_cancel_ok = False
        saw_invite_terminated = False
        cancel_race_bye_sent = False
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
            sip.mark_sip_event(self, "SIP_RESPONSE", int(msg.status_code), msg.reason)
            _LOGGER.info("SIP RX %s %s from %s:%s", msg.status_code, msg.reason, addr[0], addr[1])
            try:
                cseq = sip.parse_cseq(msg.header("CSeq"))
                via_values = msg.header_values("Via")
                response_branch = sip.parse_via(via_values[0] if via_values else "").branch
            except (TypeError, ValueError, sip.SipError):
                continue
            cseq_method = cseq.method
            if cseq_method in {"CANCEL", "INVITE"}:
                if cseq.number != self._invite_cseq or response_branch != self.dialog_ids.branch:
                    continue
            elif cseq_method == "BYE":
                if not self._response_matches_transaction(
                    msg,
                    method="BYE",
                    cseq=self._bye_cseq,
                    branch=self._bye_branch,
                ):
                    continue
            else:
                continue
            if cseq_method == "CANCEL" and 200 <= msg.status_code < 300:
                saw_cancel_ok = True
            elif cseq_method == "INVITE" and 200 <= msg.status_code < 300:
                # The final 2xx won the race with CANCEL. RFC 3261 requires us
                # to accept it, ACK it, then end the confirmed dialog with BYE.
                committed = self._commit_200_ok(
                    msg,
                    self._pending_target,
                    self._pending_remote_host or addr[0],
                    self._pending_remote_sip_port,
                    self._pending_request_uri,
                    self._pending_local_uri,
                    self._pending_remote_uri,
                )
                if committed:
                    self.bye()
                cancel_race_bye_sent = True
            elif cseq_method == "INVITE" and msg.status_code >= 300:
                self._send_invite_error_ack(msg, addr[0], addr[1])
                self._invite_transaction_active = False
                saw_invite_terminated = True
            elif cseq_method == "BYE" and cancel_race_bye_sent and 200 <= msg.status_code < 300:
                return "cancelled"
            if saw_cancel_ok and saw_invite_terminated:
                return "cancelled"
        if saw_cancel_ok or saw_invite_terminated or cancel_race_bye_sent:
            return "cancelled"
        return "timeout"

    def snapshot(self) -> dict[str, Any]:
        dialog = self.dialog
        return {
            "call_id": self.dialog_ids.call_id,
            "local_uri": dialog.local_uri if dialog is not None else self._pending_local_uri,
            "remote_uri": dialog.remote_uri if dialog is not None else self._pending_remote_uri,
            "remote_target_uri": (
                (dialog.remote_target_uri or dialog.remote_uri)
                if dialog is not None
                else self._pending_request_uri
            ),
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
            "pending_invite": bool(self._invite_transaction_active and dialog is None),
            "sip_transport": self.signaling_transport.lower(),
            "last_sip_event": self.last_sip_event,
            "last_sip_status_code": self.last_sip_status_code,
            "last_sip_reason": self.last_sip_reason,
        }
