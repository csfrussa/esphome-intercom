"""Small RTCP feedback profile used by experimental SIP video."""

from __future__ import annotations

from dataclasses import dataclass
import struct


class RtcpError(ValueError):
    """Malformed or unsupported RTCP packet."""


@dataclass(frozen=True, slots=True)
class RtcpPacket:
    packet_type: int
    fmt: int
    payload: bytes


def parse_compound(data: bytes) -> list[RtcpPacket]:
    """Parse the common headers of one RTCP compound datagram."""

    packets: list[RtcpPacket] = []
    offset = 0
    while offset < len(data):
        if offset + 4 > len(data):
            raise RtcpError("truncated RTCP header")
        first, packet_type, length = struct.unpack_from("!BBH", data, offset)
        if first >> 6 != 2:
            raise RtcpError("unsupported RTCP version")
        size = (int(length) + 1) * 4
        if size < 4 or offset + size > len(data):
            raise RtcpError("truncated RTCP packet")
        packets.append(RtcpPacket(packet_type, first & 0x1F, data[offset + 4 : offset + size]))
        offset += size
    return packets


def _header(fmt: int, packet_type: int, payload: bytes) -> bytes:
    if len(payload) % 4:
        raise RtcpError("RTCP payload must be word aligned")
    return struct.pack("!BBH", 0x80 | (fmt & 0x1F), packet_type, len(payload) // 4) + payload


def build_pli(sender_ssrc: int, media_ssrc: int) -> bytes:
    """Build RFC 4585 Picture Loss Indication feedback."""

    return _header(1, 206, struct.pack("!II", sender_ssrc & 0xFFFFFFFF, media_ssrc & 0xFFFFFFFF))


def build_fir(sender_ssrc: int, media_ssrc: int, sequence: int) -> bytes:
    """Build RFC 5104 Full Intra Request feedback."""

    payload = struct.pack(
        "!III4B",
        sender_ssrc & 0xFFFFFFFF,
        0,
        media_ssrc & 0xFFFFFFFF,
        sequence & 0xFF,
        0,
        0,
        0,
    )
    return _header(4, 206, payload)


def build_receiver_report(
    sender_ssrc: int,
    media_ssrc: int,
    *,
    fraction_lost: int = 0,
    cumulative_lost: int = 0,
    highest_sequence: int = 0,
    jitter: int = 0,
) -> bytes:
    """Build one RFC 3550 receiver report block without sender timing data."""

    cumulative = max(-0x800000, min(0x7FFFFF, int(cumulative_lost))) & 0xFFFFFF
    lost_word = ((int(fraction_lost) & 0xFF) << 24) | cumulative
    block = struct.pack(
        "!IIIIII",
        media_ssrc & 0xFFFFFFFF,
        lost_word,
        highest_sequence & 0xFFFFFFFF,
        jitter & 0xFFFFFFFF,
        0,
        0,
    )
    return _header(1, 201, struct.pack("!I", sender_ssrc & 0xFFFFFFFF) + block)


def build_sdes_cname(sender_ssrc: int, cname: str) -> bytes:
    """Build the mandatory SDES CNAME chunk for a compound RTCP packet."""

    encoded = str(cname).encode("utf-8")
    if not encoded or len(encoded) > 255:
        raise RtcpError("RTCP CNAME must contain between 1 and 255 bytes")
    payload = bytearray(struct.pack("!I", sender_ssrc & 0xFFFFFFFF))
    payload.extend((1, len(encoded)))
    payload.extend(encoded)
    payload.append(0)
    payload.extend(b"\x00" * (-len(payload) % 4))
    return _header(1, 202, bytes(payload))


def build_receiver_compound(
    sender_ssrc: int,
    media_ssrc: int,
    *,
    fraction_lost: int = 0,
    cumulative_lost: int = 0,
    highest_sequence: int = 0,
    jitter: int = 0,
    feedback: bytes = b"",
) -> bytes:
    """Build an RFC 3550 compound RR/SDES packet with optional AVPF feedback."""

    report = build_receiver_report(
        sender_ssrc,
        media_ssrc,
        fraction_lost=fraction_lost,
        cumulative_lost=cumulative_lost,
        highest_sequence=highest_sequence,
        jitter=jitter,
    )
    cname = build_sdes_cname(sender_ssrc, f"voip-stack-{sender_ssrc & 0xFFFFFFFF:08x}")
    if feedback:
        parse_compound(feedback)
    return report + cname + feedback
