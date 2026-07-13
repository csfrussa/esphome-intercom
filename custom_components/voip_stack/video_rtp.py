"""RFC 6184 H.264 RTP packetization for the experimental HA video phone."""

from __future__ import annotations

from dataclasses import dataclass

from . import rtp


ANNEX_B_START_CODE = b"\x00\x00\x00\x01"
DEFAULT_MAX_RTP_PAYLOAD = 1200
MAX_ACCESS_UNIT_BYTES = 4 * 1024 * 1024
MAX_NAL_UNIT_BYTES = 2 * 1024 * 1024
MAX_ACCESS_UNIT_NALS = 512


class H264RtpError(ValueError):
    """Malformed or unsupported RFC 6184 payload."""


@dataclass(frozen=True, slots=True)
class H264AccessUnit:
    """One Annex B access unit reconstructed from one RTP timestamp."""

    data: bytes
    timestamp: int
    key_frame: bool


def split_annex_b(data: bytes) -> list[bytes]:
    """Split an Annex B byte stream into NAL units without start codes."""

    if not data:
        return []
    starts: list[tuple[int, int]] = []
    index = 0
    end = len(data)
    while index + 3 <= end:
        if data[index : index + 4] == ANNEX_B_START_CODE:
            starts.append((index, 4))
            index += 4
            continue
        if data[index : index + 3] == b"\x00\x00\x01":
            starts.append((index, 3))
            index += 3
            continue
        index += 1
    if not starts:
        raise H264RtpError("H.264 access unit is not Annex B")
    out: list[bytes] = []
    for position, (start, prefix_len) in enumerate(starts):
        nal_start = start + prefix_len
        nal_end = starts[position + 1][0] if position + 1 < len(starts) else end
        while nal_end > nal_start and data[nal_end - 1] == 0:
            nal_end -= 1
        nal = data[nal_start:nal_end]
        if nal:
            if len(nal) > MAX_NAL_UNIT_BYTES:
                raise H264RtpError("H.264 NAL unit exceeds safety limit")
            out.append(nal)
    if not out:
        raise H264RtpError("H.264 access unit has no NAL units")
    if len(out) > MAX_ACCESS_UNIT_NALS:
        raise H264RtpError("H.264 access unit has too many NAL units")
    return out


def join_annex_b(nal_units: list[bytes]) -> bytes:
    """Join validated NAL units as an Annex B access unit."""

    if not nal_units:
        raise H264RtpError("cannot build an empty H.264 access unit")
    total = sum(len(nal) + len(ANNEX_B_START_CODE) for nal in nal_units)
    if total > MAX_ACCESS_UNIT_BYTES:
        raise H264RtpError("H.264 access unit exceeds safety limit")
    return b"".join(ANNEX_B_START_CODE + nal for nal in nal_units)


class H264Depacketizer:
    """Reassemble RFC 6184 mode-1 packets into Annex B access units."""

    def __init__(self, parameter_sets: list[bytes] | None = None) -> None:
        self._timestamp: int | None = None
        self._expected_sequence: int | None = None
        self._nals: list[bytes] = []
        self._fu: bytearray | None = None
        self._damaged = False
        self._sps: bytes | None = None
        self._pps: bytes | None = None
        self.dropped_access_units = 0
        self.sequence_gaps = 0
        for nal in parameter_sets or []:
            if not nal:
                continue
            nal_type = nal[0] & 0x1F
            if nal_type == 7:
                self._sps = bytes(nal)
            elif nal_type == 8:
                self._pps = bytes(nal)

    def reset(self) -> None:
        self._timestamp = None
        self._expected_sequence = None
        self._nals.clear()
        self._fu = None
        self._damaged = False

    def push(self, packet: rtp.RtpPacket) -> H264AccessUnit | None:
        """Consume one packet and return an access unit on its marker."""

        if not packet.payload:
            self._damage()
            return self._finish(packet.timestamp) if packet.marker else None
        if self._timestamp is None:
            self._timestamp = packet.timestamp
        elif packet.timestamp != self._timestamp:
            if self._nals or self._fu is not None:
                self.dropped_access_units += 1
            self._timestamp = packet.timestamp
            self._expected_sequence = None
            self._nals.clear()
            self._fu = None
            self._damaged = False
        if self._expected_sequence is not None and packet.sequence != self._expected_sequence:
            self.sequence_gaps += 1
            self._damage()
        self._expected_sequence = rtp.next_sequence(packet.sequence)

        try:
            self._consume_payload(packet.payload)
        except H264RtpError:
            self._damage()
        return self._finish(packet.timestamp) if packet.marker else None

    def _damage(self) -> None:
        self._damaged = True
        self._fu = None

    def _append_nal(self, nal: bytes) -> None:
        if not nal or len(nal) > MAX_NAL_UNIT_BYTES:
            raise H264RtpError("invalid H.264 NAL unit size")
        if len(self._nals) >= MAX_ACCESS_UNIT_NALS:
            raise H264RtpError("too many H.264 NAL units")
        nal_type = nal[0] & 0x1F
        if nal_type == 7:
            self._sps = nal
        elif nal_type == 8:
            self._pps = nal
        self._nals.append(nal)
        if sum(len(item) + 4 for item in self._nals) > MAX_ACCESS_UNIT_BYTES:
            raise H264RtpError("H.264 access unit exceeds safety limit")

    def _consume_payload(self, payload: bytes) -> None:
        nal_type = payload[0] & 0x1F
        if 1 <= nal_type <= 23:
            if self._fu is not None:
                raise H264RtpError("single NAL interrupted an FU-A")
            self._append_nal(payload)
            return
        if nal_type == 24:  # STAP-A
            if self._fu is not None:
                raise H264RtpError("STAP-A interrupted an FU-A")
            offset = 1
            appended = 0
            while offset < len(payload):
                if offset + 2 > len(payload):
                    raise H264RtpError("truncated STAP-A length")
                size = int.from_bytes(payload[offset : offset + 2], "big")
                offset += 2
                if size == 0 or offset + size > len(payload):
                    raise H264RtpError("invalid STAP-A NAL size")
                self._append_nal(payload[offset : offset + size])
                appended += 1
                offset += size
            if not appended:
                raise H264RtpError("empty STAP-A")
            return
        if nal_type == 28:  # FU-A
            if len(payload) < 3:
                raise H264RtpError("truncated FU-A")
            indicator = payload[0]
            header = payload[1]
            start = bool(header & 0x80)
            end = bool(header & 0x40)
            reconstructed_type = header & 0x1F
            if reconstructed_type == 0 or start and end:
                raise H264RtpError("invalid FU-A header")
            fragment = payload[2:]
            if not fragment:
                raise H264RtpError("empty FU-A fragment")
            if start:
                if self._fu is not None:
                    raise H264RtpError("nested FU-A start")
                self._fu = bytearray(((indicator & 0xE0) | reconstructed_type,))
            elif self._fu is None:
                raise H264RtpError("FU-A continuation without start")
            assert self._fu is not None
            self._fu.extend(fragment)
            if len(self._fu) > MAX_NAL_UNIT_BYTES:
                raise H264RtpError("FU-A NAL exceeds safety limit")
            if end:
                nal = bytes(self._fu)
                self._fu = None
                self._append_nal(nal)
            return
        raise H264RtpError(f"unsupported RFC 6184 NAL packet type {nal_type}")

    def _finish(self, timestamp: int) -> H264AccessUnit | None:
        nals = self._nals
        damaged = self._damaged or self._fu is not None or not nals
        self._timestamp = None
        self._expected_sequence = None
        self._nals = []
        self._fu = None
        self._damaged = False
        if damaged:
            self.dropped_access_units += 1
            return None
        key_frame = any((nal[0] & 0x1F) == 5 for nal in nals)
        if key_frame:
            present = {nal[0] & 0x1F for nal in nals}
            prefix: list[bytes] = []
            if 7 not in present and self._sps is not None:
                prefix.append(self._sps)
            if 8 not in present and self._pps is not None:
                prefix.append(self._pps)
            nals = [*prefix, *nals]
        try:
            data = join_annex_b(nals)
        except H264RtpError:
            self.dropped_access_units += 1
            return None
        return H264AccessUnit(data=data, timestamp=timestamp, key_frame=key_frame)


def packetize_annex_b(
    access_unit: bytes,
    *,
    payload_type: int,
    sequence: int,
    timestamp: int,
    ssrc: int,
    max_payload: int = DEFAULT_MAX_RTP_PAYLOAD,
) -> list[rtp.RtpPacket]:
    """Packetize one Annex B access unit using single NAL and FU-A packets."""

    if not 3 <= int(max_payload) <= rtp.MAX_RTP_PAYLOAD_BYTES:
        raise H264RtpError("invalid H.264 RTP payload limit")
    nals = split_annex_b(access_unit)
    packets: list[rtp.RtpPacket] = []
    current_sequence = int(sequence) & 0xFFFF
    for nal in nals:
        if len(nal) <= max_payload:
            packets.append(
                rtp.RtpPacket(
                    payload_type=payload_type,
                    sequence=current_sequence,
                    timestamp=timestamp,
                    ssrc=ssrc,
                    payload=nal,
                )
            )
            current_sequence = rtp.next_sequence(current_sequence)
            continue
        nal_header = nal[0]
        fu_indicator = (nal_header & 0xE0) | 28
        nal_type = nal_header & 0x1F
        fragment_size = max_payload - 2
        body = memoryview(nal)[1:]
        offset = 0
        while offset < len(body):
            end = min(len(body), offset + fragment_size)
            fu_header = nal_type
            if offset == 0:
                fu_header |= 0x80
            if end == len(body):
                fu_header |= 0x40
            packets.append(
                rtp.RtpPacket(
                    payload_type=payload_type,
                    sequence=current_sequence,
                    timestamp=timestamp,
                    ssrc=ssrc,
                    payload=bytes((fu_indicator, fu_header)) + bytes(body[offset:end]),
                )
            )
            current_sequence = rtp.next_sequence(current_sequence)
            offset = end
    if not packets:
        raise H264RtpError("H.264 access unit produced no RTP packets")
    last = packets[-1]
    packets[-1] = rtp.RtpPacket(
        payload_type=last.payload_type,
        sequence=last.sequence,
        timestamp=last.timestamp,
        ssrc=last.ssrc,
        payload=last.payload,
        marker=True,
    )
    return packets
