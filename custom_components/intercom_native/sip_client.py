"""Outbound SIP/RTP primitives for the phase-1 VoIP intercom profile."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import socket

from .audio_format import AudioFormat, LEGACY_AUDIO_FORMAT, PcmFormat
from . import rtp, sdp, sip

_LOGGER = logging.getLogger(__name__)


def pcm_to_rtp_payload(data: bytes, fmt: AudioFormat) -> bytes:
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


def rtp_payload_to_pcm(payload: bytes, fmt: AudioFormat) -> bytes:
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
    selected_format: sdp.RtpPcmFormat


class _SipClientProtocol(asyncio.DatagramProtocol):
    def __init__(self, queue: asyncio.Queue[tuple[bytes, tuple[str, int]]]) -> None:
        self.queue = queue
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr) -> None:
        self.queue.put_nowait((data, addr))


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
    ) -> None:
        self.local_ip = local_ip
        self.local_name = local_name
        self.local_sip_port = int(local_sip_port)
        self.local_rtp_port = int(local_rtp_port)
        self.supported_formats = supported_formats or [LEGACY_AUDIO_FORMAT]
        self.transport: asyncio.DatagramTransport | None = None
        self.protocol: _SipClientProtocol | None = None
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

    async def start(self) -> None:
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

    async def close(self) -> None:
        if self.transport is not None:
            self.transport.close()
            self.transport = None

    async def invite(
        self,
        *,
        target: str,
        remote_host: str,
        remote_sip_port: int,
        timeout: float = 8.0,
    ) -> str:
        await self.start()
        request_uri = str(sip.SipUri(target, remote_host, int(remote_sip_port)))
        local_uri = str(sip.SipUri(self.local_name, self.local_ip, self.local_sip_port))
        remote_uri = str(sip.SipUri(target, remote_host, int(remote_sip_port)))
        self._pending_target = target
        self._pending_remote_host = remote_host
        self._pending_remote_sip_port = int(remote_sip_port)
        self._pending_request_uri = request_uri
        self._pending_local_uri = local_uri
        self._pending_remote_uri = remote_uri
        body = sdp.build_offer(self.local_ip, self.local_ip, self.local_rtp_port, self.supported_formats).encode()
        headers = sip.dialog_headers(
            request_uri=request_uri,
            local_uri=local_uri,
            remote_uri=remote_uri,
            dialog=self.dialog_ids,
            method="INVITE",
            contact_uri=local_uri,
            content_type="application/sdp",
        )
        self._invite_cseq = self.dialog_ids.cseq
        raw = sip.build_request("INVITE", request_uri, headers, body)
        assert self.transport is not None
        self.transport.sendto(raw, (remote_host, int(remote_sip_port)))
        _LOGGER.info("SIP TX INVITE %s@%s:%s", target, remote_host, remote_sip_port)
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return "timeout"
            data, addr = await asyncio.wait_for(self.queue.get(), timeout=remaining)
            try:
                msg = sip.parse_message(data)
            except Exception as err:
                _LOGGER.info("SIP RX malformed from %s:%s: %s", addr[0], addr[1], err)
                continue
            if not msg.is_response:
                continue
            _LOGGER.info("SIP RX %s %s from %s:%s", msg.status_code, msg.reason, addr[0], addr[1])
            if msg.status_code == 180:
                return "ringing"
            if msg.status_code and 200 <= msg.status_code < 300:
                if not self._commit_200_ok(msg, target, remote_host, int(remote_sip_port), request_uri, local_uri, remote_uri):
                    return "incompatible_audio_format"
                return "streaming"
            if msg.status_code and msg.status_code >= 300:
                return f"sip_{msg.status_code}"

    async def wait_for_final(self, timeout: float = 60.0) -> str:
        """Continue an INVITE transaction after the first 180 Ringing."""
        if self.dialog is not None:
            return "streaming"
        deadline = asyncio.get_running_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_running_loop().time()
            if remaining <= 0:
                return "timeout"
            data, addr = await asyncio.wait_for(self.queue.get(), timeout=remaining)
            try:
                msg = sip.parse_message(data)
            except Exception:
                continue
            if not msg.is_response or msg.status_code is None:
                continue
            _LOGGER.info("SIP RX %s %s from %s:%s", msg.status_code, msg.reason, addr[0], addr[1])
            if msg.status_code == 180:
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
                    return "incompatible_audio_format"
                return "streaming"
            if msg.status_code >= 300:
                return f"sip_{msg.status_code}"

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
        selected = sdp.negotiate(msg.body, self.supported_formats)
        if selected is None:
            return False
        parsed = sdp.parse_sdp(msg.body)
        self.dialog_ids.remote_tag = _extract_tag(msg.header("To"))
        if not request_uri:
            request_uri = str(sip.SipUri(target or "intercom", remote_host, remote_sip_port))
        if not local_uri:
            local_uri = str(sip.SipUri(self.local_name, self.local_ip, self.local_sip_port))
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
            selected_format=selected,
        )
        self._send_ack(remote_host, int(remote_sip_port), request_uri, local_uri, remote_uri)
        return True

    def _send_ack(self, host: str, port: int, request_uri: str, local_uri: str, remote_uri: str) -> None:
        if self.transport is None:
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
        )
        raw = sip.build_request("ACK", request_uri, headers, b"")
        self.transport.sendto(raw, (host, port))
        _LOGGER.info("SIP TX ACK %s:%s", host, port)

    def bye(self) -> None:
        if self.transport is None or self.dialog is None:
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
        )
        raw = sip.build_request("BYE", self.dialog.remote_uri, headers, b"")
        self.transport.sendto(raw, (self.dialog.remote_host, self.dialog.remote_sip_port))
        _LOGGER.info("SIP TX BYE %s:%s", self.dialog.remote_host, self.dialog.remote_sip_port)


def _extract_tag(header: str) -> str:
    for part in (header or "").split(";"):
        part = part.strip()
        if part.startswith("tag="):
            return part.removeprefix("tag=")
    return ""
