"""Session-owned RTP relay/resampler for SIP PCM calls."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
from pathlib import Path
import secrets
from typing import Any
import wave

from . import rtp
from .audio_format import AudioFormat
from .audio_pcm import PcmFrameConverter
from .sdp import RtpPcmFormat, audio_format_to_rtp
from .sip_client import RtpPayloadDecoder, RtpPayloadEncoder

_LOGGER = logging.getLogger(__name__)
_DEBUG_CAPTURE_DIR = Path("/tmp/voip_stack_debug")
_DEBUG_CAPTURE_SECONDS = 8


@dataclass(slots=True)
class RtpPeer:
    host: str
    port: int
    payload_type: int
    audio_format: AudioFormat
    rtp_format: RtpPcmFormat | None = None
    send_payload_type: int | None = None
    send_audio_format: AudioFormat | None = None
    send_rtp_format: RtpPcmFormat | None = None
    sequence: int = field(default_factory=lambda: secrets.randbelow(0x10000))
    timestamp: int = field(default_factory=lambda: secrets.randbelow(0x100000000))
    ssrc: int = field(default_factory=lambda: secrets.randbelow(0x100000000))

    @property
    def outbound_payload_type(self) -> int:
        return self.send_payload_type if self.send_payload_type is not None else self.payload_type

    @property
    def outbound_audio_format(self) -> AudioFormat:
        return self.send_audio_format if self.send_audio_format is not None else self.audio_format

    @property
    def inbound_rtp_format(self) -> RtpPcmFormat:
        return self.rtp_format if self.rtp_format is not None else audio_format_to_rtp(self.audio_format, self.payload_type)

    @property
    def outbound_rtp_format(self) -> RtpPcmFormat:
        if self.send_rtp_format is not None:
            return self.send_rtp_format
        return audio_format_to_rtp(self.outbound_audio_format, self.outbound_payload_type)


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

    Each SIP leg keeps its negotiated PCM shape. HA converts between the two
    negotiated RTP formats when needed, including sample-rate and frame-size
    changes that are exact for the supported PCM profile.
    """

    def __init__(
        self,
        *,
        left: RtpPeer,
        right: RtpPeer,
        left_port: int,
        right_port: int,
        debug_capture: bool = False,
        capture_name: str = "",
    ) -> None:
        self.left = left
        self.right = right
        self.left_port = int(left_port)
        self.right_port = int(right_port)
        self.left_transport: asyncio.DatagramTransport | None = None
        self.right_transport: asyncio.DatagramTransport | None = None
        self.forwarded = 0
        self.dropped = 0
        self.left_rx_packets = 0
        self.left_rx_bytes = 0
        self.left_tx_packets = 0
        self.left_tx_bytes = 0
        self.right_rx_packets = 0
        self.right_rx_bytes = 0
        self.right_tx_packets = 0
        self.right_tx_bytes = 0
        self.left_to_right = PcmFrameConverter(left.audio_format, right.outbound_audio_format)
        self.right_to_left = PcmFrameConverter(right.audio_format, left.outbound_audio_format)
        self.left_decoder = RtpPayloadDecoder(left.inbound_rtp_format)
        self.right_decoder = RtpPayloadDecoder(right.inbound_rtp_format)
        self.left_encoder = RtpPayloadEncoder(left.outbound_rtp_format)
        self.right_encoder = RtpPayloadEncoder(right.outbound_rtp_format)
        self._capture_buffers: dict[str, bytearray] = {}
        self._capture_paths: dict[str, Path] = {}
        self._capture_formats: dict[str, AudioFormat] = {}
        self._capture_frames: dict[str, int] = {"left": 0, "right": 0}
        if debug_capture:
            self._prepare_debug_capture(capture_name)

    def _prepare_debug_capture(self, capture_name: str) -> None:
        safe_name = "".join(ch if ch.isalnum() or ch in {"-", "_"} else "_" for ch in capture_name)[:80]
        if not safe_name:
            safe_name = f"relay_{self.left_port}_{self.right_port}"
        for side, fmt in (("left", self.left.audio_format), ("right", self.right.audio_format)):
            path = _DEBUG_CAPTURE_DIR / f"{safe_name}_{side}_rx.wav"
            self._capture_buffers[side] = bytearray()
            self._capture_paths[side] = path
            self._capture_formats[side] = fmt
            _LOGGER.info("SIP RTP debug capture prepared side=%s path=%s format=%s", side, path, fmt.wire_token())

    def _capture_pcm(self, side: str, pcm: bytes) -> None:
        buffer = self._capture_buffers.get(side)
        if buffer is None:
            return
        fmt = self.left.audio_format if side == "left" else self.right.audio_format
        max_frames = int(fmt.sample_rate * _DEBUG_CAPTURE_SECONDS)
        current = self._capture_frames.get(side, 0)
        if current >= max_frames:
            return
        samples = len(pcm) // max(1, fmt.container_bytes_per_sample * fmt.channels)
        if current + samples > max_frames:
            keep = (max_frames - current) * fmt.container_bytes_per_sample * fmt.channels
            pcm = pcm[:keep]
            samples = max_frames - current
        buffer.extend(pcm)
        self._capture_frames[side] = current + samples

    def _write_debug_capture_files(self) -> None:
        if not self._capture_buffers:
            return
        _DEBUG_CAPTURE_DIR.mkdir(parents=True, exist_ok=True)
        for side, pcm in self._capture_buffers.items():
            fmt = self._capture_formats[side]
            path = self._capture_paths[side]
            with wave.open(str(path), "wb") as wav:
                wav.setnchannels(fmt.channels)
                wav.setsampwidth(2)
                wav.setframerate(fmt.sample_rate)
                wav.writeframes(bytes(pcm))
            _LOGGER.info(
                "SIP RTP debug capture wrote side=%s path=%s bytes=%d format=%s",
                side,
                path,
                len(pcm),
                fmt.wire_token(),
            )

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
        _LOGGER.info(
            "SIP RTP relay listening left=%s right=%s left=%s->%s right=%s->%s",
            self.left_port,
            self.right_port,
            self.left.audio_format.wire_token(),
            self.right.outbound_audio_format.wire_token(),
            self.right.audio_format.wire_token(),
            self.left.outbound_audio_format.wire_token(),
        )

    async def stop(self) -> None:
        _LOGGER.info(
            "SIP RTP relay stopped left=%s right=%s forwarded=%s dropped=%s left_rx=%s right_rx=%s left_tx=%s right_tx=%s",
            self.left_port,
            self.right_port,
            self.forwarded,
            self.dropped,
            self.left_rx_packets,
            self.right_rx_packets,
            self.left_tx_packets,
            self.right_tx_packets,
        )
        if self.left_transport is not None:
            self.left_transport.close()
            self.left_transport = None
        if self.right_transport is not None:
            self.right_transport.close()
            self.right_transport = None
        await asyncio.to_thread(self._write_debug_capture_files)
        self._capture_buffers.clear()

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
            decoder = self.left_decoder if side == "left" else self.right_decoder
            pcm = decoder.decode(packet.payload)
            if not pcm:
                return
            self._capture_pcm(side, pcm)
            converter = self.left_to_right if side == "left" else self.right_to_left
            converted_frames = converter.convert(pcm)
            out_format = dest.outbound_audio_format
            encoder = self.right_encoder if side == "left" else self.left_encoder
            outgoing: list[bytes] = []
            sequence = dest.sequence
            timestamp = dest.timestamp
            for frame in converted_frames:
                outgoing.append(
                    rtp.build_packet(
                        rtp.RtpPacket(
                            payload_type=dest.outbound_payload_type,
                            sequence=sequence,
                            timestamp=timestamp,
                            ssrc=dest.ssrc,
                            payload=encoder.encode(frame),
                        )
                    )
                )
                sequence = rtp.next_sequence(sequence)
                timestamp = rtp.next_timestamp(timestamp, out_format.nominal_frame_samples)
        except Exception as err:
            self.dropped += 1
            _LOGGER.debug("RTP relay drop: %s", err)
            return
        if side == "left":
            self.left_rx_packets += 1
            self.left_rx_bytes += len(data)
        else:
            self.right_rx_packets += 1
            self.right_rx_bytes += len(data)
        for out in outgoing:
            transport.sendto(out, (dest.host, dest.port))
            if side == "left":
                self.right_tx_packets += 1
                self.right_tx_bytes += len(out)
            else:
                self.left_tx_packets += 1
                self.left_tx_bytes += len(out)
            self.forwarded += 1
        dest.sequence = sequence
        dest.timestamp = timestamp

    def snapshot(self) -> dict[str, Any]:
        return {
            "left_port": self.left_port,
            "right_port": self.right_port,
            "left_peer": f"{self.left.host}:{self.left.port}",
            "right_peer": f"{self.right.host}:{self.right.port}",
            "forwarded_packets": self.forwarded,
            "dropped_packets": self.dropped,
            "left_rx_packets": self.left_rx_packets,
            "left_rx_bytes": self.left_rx_bytes,
            "left_tx_packets": self.left_tx_packets,
            "left_tx_bytes": self.left_tx_bytes,
            "right_rx_packets": self.right_rx_packets,
            "right_rx_bytes": self.right_rx_bytes,
            "right_tx_packets": self.right_tx_packets,
            "right_tx_bytes": self.right_tx_bytes,
            "left_rx_format": self.left.audio_format.wire_token(),
            "left_tx_format": self.left.outbound_audio_format.wire_token(),
            "right_rx_format": self.right.audio_format.wire_token(),
            "right_tx_format": self.right.outbound_audio_format.wire_token(),
        }
