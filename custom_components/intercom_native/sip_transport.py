"""IntercomTransport adapter for SIP/RTP PCM calls."""

from __future__ import annotations

import asyncio
import logging
import secrets
import socket
from typing import Callable, Optional

from homeassistant.core import HomeAssistant
from homeassistant.components import network

from . import rtp
from .audio_format import AudioFormat, LEGACY_AUDIO_FORMAT
from .const import DOMAIN, HA_PEER_FALLBACK_NAME, INTERCOM_RTP_PORT, INTERCOM_SIP_PORT
from .sdp import build_answer_directional
from .sip_client import SipCallClient, pcm_to_rtp_payload, rtp_payload_to_pcm
from .sip_listener import SipInvite, SipUdpServer
from .transport_base import IntercomTransport

_LOGGER = logging.getLogger(__name__)


class _RtpProtocol(asyncio.DatagramProtocol):
    def __init__(self, owner: "IntercomSipClient") -> None:
        self.owner = owner
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr) -> None:
        self.owner.handle_rtp(data, addr)


class IntercomSipClient(IntercomTransport):
    """SIP INVITE + RTP PCM transport for one HA-originated session."""

    transport_name = "sip"

    def __init__(
        self,
        *,
        hass: HomeAssistant,
        host: str,
        target_name: str,
        remote_sip_port: int = INTERCOM_SIP_PORT,
        local_sip_port: int = INTERCOM_SIP_PORT,
        local_rtp_port: int = INTERCOM_RTP_PORT + 20,
        on_audio: Optional[Callable[[bytes], None]] = None,
        on_disconnected: Optional[Callable[[], None]] = None,
        on_ringing: Optional[Callable[[], None]] = None,
        on_answered: Optional[Callable[[], None]] = None,
        on_stop_received: Optional[Callable[[], None]] = None,
        on_decline_received: Optional[Callable[[str], None]] = None,
        on_error_received: Optional[Callable[[int, str], None]] = None,
    ) -> None:
        super().__init__(
            host,
            on_audio=on_audio,
            on_disconnected=on_disconnected,
            on_ringing=on_ringing,
            on_answered=on_answered,
            on_stop_received=on_stop_received,
            on_decline_received=on_decline_received,
            on_error_received=on_error_received,
        )
        self.hass = hass
        self.target_name = target_name or "intercom"
        self.remote_sip_port = int(remote_sip_port or INTERCOM_SIP_PORT)
        self.local_sip_port = int(local_sip_port or INTERCOM_SIP_PORT)
        self.local_rtp_port = int(local_rtp_port or INTERCOM_RTP_PORT + 20)
        self.local_ip = ""
        self._client: SipCallClient | None = None
        self._rtp_transport: asyncio.DatagramTransport | None = None
        self._rtp_protocol: _RtpProtocol | None = None
        self._final_task: asyncio.Task | None = None
        self._disconnecting = False
        self._sequence = secrets.randbelow(0x10000)
        self._timestamp = secrets.randbelow(0x100000000)
        self._ssrc = secrets.randbelow(0x100000000)

    async def _advertise_ip(self) -> str:
        cfg = self.hass.data.get(DOMAIN, {}).get("transport_config", {})
        configured = str(cfg.get("advertise_host") or "").strip()
        if configured:
            return configured
        addresses = await network.async_get_announce_addresses(self.hass)
        return addresses[0] if addresses else ""

    async def _bind_rtp(self) -> bool:
        if self._rtp_transport is not None:
            return True
        loop = asyncio.get_running_loop()
        port = self.local_rtp_port
        last_err: OSError | None = None
        for _ in range(8):
            try:
                self._rtp_protocol = _RtpProtocol(self)
                transport, _ = await loop.create_datagram_endpoint(
                    lambda: self._rtp_protocol,
                    local_addr=("0.0.0.0", port),
                    family=socket.AF_INET,
                )
                self._rtp_transport = transport  # type: ignore[assignment]
                self.local_rtp_port = port
                _LOGGER.info("SIP RTP RX bound on UDP/%s for %s", port, self.host)
                return True
            except OSError as err:
                last_err = err
                port += 2
        _LOGGER.error("Failed to bind SIP RTP port near %s: %s", self.local_rtp_port, last_err)
        return False

    async def connect(self) -> bool:
        if self._connected:
            return True
        self._disconnecting = False
        self.local_ip = await self._advertise_ip()
        if not self.local_ip:
            _LOGGER.error("SIP connect failed for %s: HA advertise IP is unknown", self.host)
            return False
        if not await self._bind_rtp():
            return False
        self._client = SipCallClient(
            local_ip=self.local_ip,
            local_name=(self.hass.config.location_name or "").strip() or HA_PEER_FALLBACK_NAME,
            local_sip_port=self.local_sip_port,
            local_rtp_port=self.local_rtp_port,
            supported_send_formats=self.local_tx_formats or [LEGACY_AUDIO_FORMAT],
            supported_recv_formats=self.local_rx_formats or [LEGACY_AUDIO_FORMAT],
        )
        await self._client.start()
        self._set_connected(True, "sip_connect")
        return True

    async def disconnect(self) -> None:
        if self._disconnecting:
            return
        self._disconnecting = True
        await self._cancel_final_task()
        if self._client is not None:
            await self._client.close()
            self._client = None
        if self._rtp_transport is not None:
            self._rtp_transport.close()
            self._rtp_transport = None
        self._set_streaming(False, "sip_disconnect")
        self._set_ringing(False, "sip_disconnect")
        self._set_connected(False, "sip_disconnect")
        self._disconnect_notified = True

    async def _cancel_final_task(self) -> None:
        if self._final_task is None:
            return
        self._final_task.cancel()
        try:
            await self._final_task
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        self._final_task = None

    async def start_stream(self, caller_name: str = "") -> str:
        if self._client is None and not await self.connect():
            return "error"
        assert self._client is not None
        result = await self._client.invite(
            target=self.target_name,
            remote_host=self.host,
            remote_sip_port=self.remote_sip_port,
        )
        if result == "streaming":
            self._set_streaming(True, "sip_200")
            return "streaming"
        if result == "ringing":
            self._set_ringing(True, "sip_180")
            if self._on_ringing:
                self._on_ringing()
            self._final_task = self.hass.async_create_task(self._wait_for_final_answer())
            return "ringing"
        _LOGGER.error("SIP INVITE failed for %s: %s", self.host, result)
        return "error"

    async def _wait_for_final_answer(self) -> None:
        if self._client is None:
            return
        result = await self._client.wait_for_final()
        if result == "streaming":
            self._set_ringing(False, "sip_200")
            self._set_streaming(True, "sip_200")
            if self._on_answered:
                self._on_answered()
            return
        _LOGGER.error("SIP final answer failed for %s: %s", self.host, result)
        self._set_ringing(False, "sip_final_failed")
        self._set_streaming(False, "sip_final_failed")
        if self._on_error_received:
            self._on_error_received(480, result)

    async def stop_stream(self) -> None:
        await self._cancel_final_task()
        if self._client is not None:
            self._client.bye_or_cancel()
        self._set_streaming(False, "sip_stop")
        self._set_ringing(False, "sip_stop")

    async def send_answer(self) -> bool:
        return False

    async def send_answer_blind(self) -> bool:
        return False

    async def send_decline(self, reason: str = "") -> bool:
        await self.stop_stream()
        return True

    async def send_audio(self, data: bytes) -> bool:
        if not self._streaming or self._rtp_transport is None or self._client is None or self._client.dialog is None:
            self._track_audio_drop("sip_not_streaming", len(data))
            return False
        fmt = self._client.dialog.send_format.audio_format
        try:
            payload = pcm_to_rtp_payload(data, fmt)
            packet = rtp.RtpPacket(
                payload_type=self._client.dialog.send_format.payload_type,
                sequence=self._sequence,
                timestamp=self._timestamp,
                ssrc=self._ssrc,
                payload=payload,
            )
            raw = rtp.build_packet(packet)
        except Exception as err:
            self._track_audio_drop("sip_encode_error", len(data))
            _LOGGER.debug("SIP RTP encode drop: %s", err)
            return False
        self._sequence = rtp.next_sequence(self._sequence)
        self._timestamp = rtp.next_timestamp(self._timestamp, fmt.nominal_frame_samples)
        self._rtp_transport.sendto(raw, (self._client.dialog.remote_rtp_host, self._client.dialog.remote_rtp_port))
        self._audio_sent += 1
        return True

    def handle_rtp(self, data: bytes, addr) -> None:
        if self._client is None or self._client.dialog is None:
            return
        dialog = self._client.dialog
        if addr[0] != dialog.remote_rtp_host:
            _LOGGER.debug("SIP RTP rejected from unexpected host %s:%s", addr[0], addr[1])
            return
        try:
            packet = rtp.parse_packet(data)
            if packet.payload_type != dialog.recv_format.payload_type:
                _LOGGER.debug(
                    "SIP RTP decode drop: unexpected PT=%s expected=%s",
                    packet.payload_type,
                    dialog.recv_format.payload_type,
                )
                return
            pcm = rtp_payload_to_pcm(packet.payload, dialog.recv_format.audio_format)
        except Exception as err:
            _LOGGER.debug("SIP RTP decode drop: %s", err)
            return
        self._audio_recv += 1
        if self._on_audio:
            self._on_audio(pcm)

    async def _send_pong_response(self) -> None:
        return None


class IntercomSipInbound(IntercomTransport):
    """SIP/RTP transport for a SIP INVITE ringing on the HA softphone."""

    transport_name = "sip_inbound"

    def __init__(
        self,
        *,
        hass: HomeAssistant,
        invite: SipInvite,
        server: SipUdpServer,
        local_ip: str,
        local_rtp_port: int,
        on_audio: Optional[Callable[[bytes], None]] = None,
        on_disconnected: Optional[Callable[[], None]] = None,
        on_ringing: Optional[Callable[[], None]] = None,
        on_answered: Optional[Callable[[], None]] = None,
        on_stop_received: Optional[Callable[[], None]] = None,
        on_decline_received: Optional[Callable[[str], None]] = None,
        on_error_received: Optional[Callable[[int, str], None]] = None,
    ) -> None:
        super().__init__(
            invite.source_host,
            on_audio=on_audio,
            on_disconnected=on_disconnected,
            on_ringing=on_ringing,
            on_answered=on_answered,
            on_stop_received=on_stop_received,
            on_decline_received=on_decline_received,
            on_error_received=on_error_received,
        )
        self.hass = hass
        self.invite = invite
        self.server = server
        self.local_ip = local_ip
        self.local_rtp_port = int(local_rtp_port or INTERCOM_RTP_PORT + 40)
        self._rtp_transport: asyncio.DatagramTransport | None = None
        self._rtp_protocol: _RtpProtocol | None = None
        self._sequence = secrets.randbelow(0x10000)
        self._timestamp = secrets.randbelow(0x100000000)
        self._ssrc = secrets.randbelow(0x100000000)
        self.set_call_context(invite.call_id, invite.caller)
        self.peer_tx_formats = [invite.recv_format.audio_format]
        self.peer_rx_formats = [invite.send_format.audio_format]

    async def _bind_rtp(self) -> bool:
        if self._rtp_transport is not None:
            return True
        loop = asyncio.get_running_loop()
        port = self.local_rtp_port
        last_err: OSError | None = None
        for _ in range(12):
            try:
                self._rtp_protocol = _RtpProtocol(self)  # type: ignore[arg-type]
                transport, _ = await loop.create_datagram_endpoint(
                    lambda: self._rtp_protocol,
                    local_addr=("0.0.0.0", port),
                    family=socket.AF_INET,
                )
                self._rtp_transport = transport  # type: ignore[assignment]
                self.local_rtp_port = port
                _LOGGER.info("SIP inbound RTP bound on UDP/%s for call_id=%s", port, self.invite.call_id)
                return True
            except OSError as err:
                last_err = err
                port += 2
        _LOGGER.error("Failed to bind SIP inbound RTP near %s: %s", self.local_rtp_port, last_err)
        return False

    async def connect(self) -> bool:
        if self._connected:
            return True
        if not await self._bind_rtp():
            return False
        self._set_connected(True, "sip_inbound_connect")
        self._set_ringing(True, "sip_inbound_pending")
        return True

    async def disconnect(self) -> None:
        if self._rtp_transport is not None:
            self._rtp_transport.close()
            self._rtp_transport = None
        self._set_streaming(False, "sip_inbound_disconnect")
        self._set_ringing(False, "sip_inbound_disconnect")
        self._set_connected(False, "sip_inbound_disconnect")
        self._disconnect_notified = True

    async def start_stream(self, caller_name: str = "") -> str:
        return "ringing" if await self.connect() else "error"

    async def stop_stream(self) -> None:
        self.server.send_bye(self.invite.call_id)
        await self.disconnect()

    async def send_ring(self) -> bool:
        return await self.connect()

    async def send_answer(self) -> bool:
        if not await self.connect():
            return False
        answer = build_answer_directional(
            self.local_ip,
            self.local_ip,
            self.local_rtp_port,
            self.invite.send_format,
            self.invite.recv_format,
        )
        if not self.server.send_final_response(self.invite.call_id, 200, "OK", answer_sdp=answer):
            _LOGGER.warning("SIP inbound answer failed: pending INVITE not found call_id=%s", self.invite.call_id)
            return False
        self._set_ringing(False, "sip_inbound_200")
        self._set_streaming(True, "sip_inbound_200")
        return True

    async def send_answer_blind(self) -> bool:
        return await self.send_answer()

    async def send_decline(self, reason: str = "") -> bool:
        app_reason = reason or "declined"
        sent = self.server.send_final_response(
            self.invite.call_id,
            486,
            "Busy Here",
            decline_reason=app_reason,
        )
        await self.disconnect()
        return sent

    async def send_audio(self, data: bytes) -> bool:
        if not self._streaming or self._rtp_transport is None:
            self._track_audio_drop("sip_inbound_not_streaming", len(data))
            return False
        try:
            payload = pcm_to_rtp_payload(data, self.invite.send_format.audio_format)
            packet = rtp.RtpPacket(
                payload_type=self.invite.send_format.payload_type,
                sequence=self._sequence,
                timestamp=self._timestamp,
                ssrc=self._ssrc,
                payload=payload,
            )
            raw = rtp.build_packet(packet)
        except Exception as err:
            self._track_audio_drop("sip_inbound_encode_error", len(data))
            _LOGGER.debug("SIP inbound RTP encode drop: %s", err)
            return False
        self._sequence = rtp.next_sequence(self._sequence)
        self._timestamp = rtp.next_timestamp(self._timestamp, self.invite.send_format.audio_format.nominal_frame_samples)
        self._rtp_transport.sendto(raw, (self.invite.remote_rtp_host, self.invite.remote_rtp_port))
        self._audio_sent += 1
        return True

    def handle_rtp(self, data: bytes, addr) -> None:
        if addr[0] != self.invite.remote_rtp_host:
            _LOGGER.debug("SIP inbound RTP rejected from unexpected host %s:%s", addr[0], addr[1])
            return
        try:
            packet = rtp.parse_packet(data)
            if packet.payload_type != self.invite.recv_format.payload_type:
                _LOGGER.debug(
                    "SIP inbound RTP decode drop: unexpected PT=%s expected=%s",
                    packet.payload_type,
                    self.invite.recv_format.payload_type,
                )
                return
            pcm = rtp_payload_to_pcm(packet.payload, self.invite.recv_format.audio_format)
        except Exception as err:
            _LOGGER.debug("SIP inbound RTP decode drop: %s", err)
            return
        self._audio_recv += 1
        if self._on_audio:
            self._on_audio(pcm)

    async def _send_pong_response(self) -> None:
        return None
