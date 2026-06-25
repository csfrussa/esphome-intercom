"""Session-owned RTP relay for SIP phase-1 PCM calls."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
import secrets

from . import rtp
from .audio_format import AudioFormat
from .sip_client import pcm_to_rtp_payload, rtp_payload_to_pcm

_LOGGER = logging.getLogger(__name__)


@dataclass(slots=True)
class RtpPeer:
    host: str
    port: int
    payload_type: int
    audio_format: AudioFormat
    sequence: int = field(default_factory=lambda: secrets.randbelow(0x10000))
    timestamp: int = field(default_factory=lambda: secrets.randbelow(0x100000000))
    ssrc: int = field(default_factory=lambda: secrets.randbelow(0x100000000))


class _RelayProtocol(asyncio.DatagramProtocol):
    def __init__(self, relay: "SipRtpRelay", side: str) -> None:
        self.relay = relay
        self.side = side
        self.transport: asyncio.DatagramTransport | None = None

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]

    def datagram_received(self, data: bytes, addr) -> None:
        self.relay.handle_packet(self.side, data, addr)


class SipRtpRelay:
    """Bidirectional RTP relay with explicit peer ownership.

    The relay does not transcode. Both legs must negotiate the same PCM shape.
    That is intentional for phase 1: HA may bridge TCP/UDP/SIP transport types,
    but it must not hide incompatible audio contracts.
    """

    def __init__(self, *, left: RtpPeer, right: RtpPeer, left_port: int, right_port: int) -> None:
        self.left = left
        self.right = right
        self.left_port = int(left_port)
        self.right_port = int(right_port)
        self.left_transport: asyncio.DatagramTransport | None = None
        self.right_transport: asyncio.DatagramTransport | None = None
        self.forwarded = 0
        self.dropped = 0

    async def start(self) -> None:
        loop = asyncio.get_running_loop()
        self.left_transport, _ = await loop.create_datagram_endpoint(
            lambda: _RelayProtocol(self, "left"),
            local_addr=("0.0.0.0", self.left_port),
        )
        self.right_transport, _ = await loop.create_datagram_endpoint(
            lambda: _RelayProtocol(self, "right"),
            local_addr=("0.0.0.0", self.right_port),
        )
        _LOGGER.info("SIP RTP relay listening left=%s right=%s", self.left_port, self.right_port)

    async def stop(self) -> None:
        if self.left_transport is not None:
            self.left_transport.close()
            self.left_transport = None
        if self.right_transport is not None:
            self.right_transport.close()
            self.right_transport = None

    def handle_packet(self, side: str, data: bytes, addr) -> None:
        source = self.left if side == "left" else self.right
        dest = self.right if side == "left" else self.left
        transport = self.right_transport if side == "left" else self.left_transport
        if transport is None:
            self.dropped += 1
            return
        if addr[0] != source.host or int(addr[1]) != int(source.port):
            self.dropped += 1
            _LOGGER.debug("RTP relay rejected packet from unexpected %s:%s", addr[0], addr[1])
            return
        try:
            packet = rtp.parse_packet(data)
            if packet.payload_type != source.payload_type:
                raise ValueError(f"payload type {packet.payload_type} != expected {source.payload_type}")
            pcm = rtp_payload_to_pcm(packet.payload, source.audio_format)
            payload = pcm_to_rtp_payload(pcm, dest.audio_format)
            out = rtp.build_packet(
                rtp.RtpPacket(
                    payload_type=dest.payload_type,
                    sequence=dest.sequence,
                    timestamp=dest.timestamp,
                    ssrc=dest.ssrc,
                    payload=payload,
                )
            )
        except Exception as err:
            self.dropped += 1
            _LOGGER.debug("RTP relay drop: %s", err)
            return
        dest.sequence = rtp.next_sequence(dest.sequence)
        dest.timestamp = rtp.next_timestamp(dest.timestamp, dest.audio_format.nominal_frame_samples)
        transport.sendto(out, (dest.host, dest.port))
        self.forwarded += 1
