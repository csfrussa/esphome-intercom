"""RTP v2 packet helpers for PCM audio."""

from __future__ import annotations

from dataclasses import dataclass
import struct


RTP_VERSION = 2
RTP_HEADER_SIZE = 12
MAX_RTP_PAYLOAD_BYTES = 1400
_RTP_HEADER = struct.Struct("!BBHII")


class RtpError(ValueError):
    """Malformed RTP packet."""


@dataclass(frozen=True, slots=True)
class RtpPacket:
    payload_type: int
    sequence: int
    timestamp: int
    ssrc: int
    payload: bytes
    marker: bool = False


def build_packet(packet: RtpPacket) -> bytes:
    if not 0 <= packet.payload_type <= 127:
        raise RtpError("RTP payload type out of range")
    if not 0 <= packet.sequence <= 0xFFFF:
        raise RtpError("RTP sequence out of range")
    if not 0 <= packet.timestamp <= 0xFFFFFFFF:
        raise RtpError("RTP timestamp out of range")
    if not 0 <= packet.ssrc <= 0xFFFFFFFF:
        raise RtpError("RTP SSRC out of range")
    if len(packet.payload) > MAX_RTP_PAYLOAD_BYTES:
        raise RtpError("RTP payload too large")
    b0 = RTP_VERSION << 6
    b1 = (0x80 if packet.marker else 0) | packet.payload_type
    return _RTP_HEADER.pack(b0, b1, packet.sequence, packet.timestamp, packet.ssrc) + packet.payload


def parse_packet(data: bytes) -> RtpPacket:
    if len(data) < RTP_HEADER_SIZE:
        raise RtpError("RTP packet too short")
    b0, b1, sequence, timestamp, ssrc = _RTP_HEADER.unpack_from(data)
    version = b0 >> 6
    if version != RTP_VERSION:
        raise RtpError(f"unsupported RTP version {version}")
    has_padding = bool(b0 & 0x20)
    has_extension = bool(b0 & 0x10)
    csrc_count = b0 & 0x0F
    off = RTP_HEADER_SIZE + csrc_count * 4
    if has_extension:
        if len(data) < off + 4:
            raise RtpError("RTP extension header truncated")
        ext_words = struct.unpack_from("!H", data, off + 2)[0]
        off += 4 + ext_words * 4
    if len(data) < off:
        raise RtpError("RTP CSRC/extension truncated")
    end = len(data)
    if has_padding:
        pad = data[-1]
        if pad == 0 or pad > len(data) - off:
            raise RtpError("invalid RTP padding")
        end -= pad
    payload = data[off:end]
    if len(payload) > MAX_RTP_PAYLOAD_BYTES:
        raise RtpError("RTP payload too large")
    return RtpPacket(
        payload_type=b1 & 0x7F,
        marker=bool(b1 & 0x80),
        sequence=sequence,
        timestamp=timestamp,
        ssrc=ssrc,
        payload=payload,
    )


def next_sequence(sequence: int) -> int:
    return (int(sequence) + 1) & 0xFFFF


def next_timestamp(timestamp: int, samples_per_frame: int) -> int:
    return (int(timestamp) + int(samples_per_frame)) & 0xFFFFFFFF
