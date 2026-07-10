"""G.711 PCMA/PCMU conversion for SIP trunk interop."""

from __future__ import annotations

_BIAS = 0x84
_CLIP = 32635
_ULAW_SEG_END = (0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF, 0x1FFF, 0x3FFF, 0x7FFF)
_ALAW_SEG_END = (0x1F, 0x3F, 0x7F, 0xFF, 0x1FF, 0x3FF, 0x7FF, 0xFFF)


def _search(value: int, table: tuple[int, ...]) -> int:
    for index, end in enumerate(table):
        if value <= end:
            return index
    return len(table)


def _clip_s16(value: int) -> int:
    return max(-32768, min(32767, value))


def _decode_alaw_byte(value: int) -> int:
    value ^= 0x55
    sample = (value & 0x0F) << 4
    segment = (value & 0x70) >> 4
    if segment == 0:
        sample += 8
    elif segment == 1:
        sample += 0x108
    else:
        sample += 0x108
        sample <<= segment - 1
    return _clip_s16(sample if value & 0x80 else -sample)


def _decode_ulaw_byte(value: int) -> int:
    value = (~value) & 0xFF
    sample = ((value & 0x0F) << 3) + _BIAS
    sample <<= (value & 0x70) >> 4
    return _clip_s16(_BIAS - sample if value & 0x80 else sample - _BIAS)


def _encode_alaw_sample(sample: int) -> int:
    # ITU-T G.711 linear PCM input is reduced to 13 significant bits before
    # segment selection.  Searching ``sample >> 4`` while quantizing the
    # original 16-bit value shifts every segment up by one and costs roughly
    # 6 dB on decoded telephone audio.
    sample >>= 3
    if sample >= 0:
        mask = 0xD5
    else:
        mask = 0x55
        sample = -sample - 1
    segment = _search(sample, _ALAW_SEG_END)
    if segment >= 8:
        return 0x7F ^ mask
    encoded = segment << 4
    if segment < 2:
        encoded |= (sample >> 1) & 0x0F
    else:
        encoded |= (sample >> segment) & 0x0F
    return encoded ^ mask


def _encode_ulaw_sample(sample: int) -> int:
    if sample < 0:
        sample = _BIAS - sample
        mask = 0x7F
    else:
        sample = _BIAS + sample
        mask = 0xFF
    if sample > _CLIP:
        sample = _CLIP
    segment = _search(sample, _ULAW_SEG_END)
    if segment >= 8:
        return 0x7F ^ mask
    encoded = (segment << 4) | ((sample >> (segment + 3)) & 0x0F)
    return encoded ^ mask


def _iter_s16le(data: bytes):
    if len(data) % 2:
        raise ValueError("s16le frame length is not sample-aligned")
    for offset in range(0, len(data), 2):
        yield int.from_bytes(data[offset:offset + 2], "little", signed=True)


def _pack_s16le(samples) -> bytes:
    out = bytearray()
    for sample in samples:
        out.extend(int(sample).to_bytes(2, "little", signed=True))
    return bytes(out)


def alaw_to_s16le(payload: bytes) -> bytes:
    return _pack_s16le(_decode_alaw_byte(value) for value in payload)


def ulaw_to_s16le(payload: bytes) -> bytes:
    return _pack_s16le(_decode_ulaw_byte(value) for value in payload)


def s16le_to_alaw(pcm: bytes) -> bytes:
    return bytes(_encode_alaw_sample(sample) for sample in _iter_s16le(pcm))


def s16le_to_ulaw(pcm: bytes) -> bytes:
    return bytes(_encode_ulaw_sample(sample) for sample in _iter_s16le(pcm))
