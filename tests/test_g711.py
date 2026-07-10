"""Reference and level tests for the G.711 payload codecs."""

from __future__ import annotations

import importlib.util
import math
from pathlib import Path
import struct


MODULE_PATH = (
    Path(__file__).resolve().parents[1] / "custom_components" / "voip_stack" / "g711.py"
)
SPEC = importlib.util.spec_from_file_location("voip_stack_g711_test", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
G711 = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(G711)
alaw_to_s16le = G711.alaw_to_s16le
s16le_to_alaw = G711.s16le_to_alaw


def _pack(samples: tuple[int, ...]) -> bytes:
    return struct.pack(f"<{len(samples)}h", *samples)


def _unpack(pcm: bytes) -> tuple[int, ...]:
    return struct.unpack(f"<{len(pcm) // 2}h", pcm)


def _rms(samples: tuple[int, ...]) -> float:
    return math.sqrt(sum(sample * sample for sample in samples) / len(samples))


def test_alaw_encoder_matches_reference_vectors() -> None:
    """Match the canonical A-law bytes emitted by common SIP codecs."""
    samples = (-32768, -30000, -10000, -1000, -1, 0, 1, 1000, 10000, 30000, 32767)
    expected = bytes((0x2A, 0x28, 0x36, 0x7A, 0x55, 0xD5, 0xD5, 0xFA, 0xB6, 0xA8, 0xAA))

    assert s16le_to_alaw(_pack(samples)) == expected


def test_alaw_round_trip_does_not_attenuate_speech_by_six_db() -> None:
    """A full-scale telephone-band signal must retain its nominal level."""
    source = tuple(
        round(30000 * math.sin(2 * math.pi * 1000 * index / 8000))
        for index in range(800)
    )
    decoded = _unpack(alaw_to_s16le(s16le_to_alaw(_pack(source))))
    level_delta_db = 20 * math.log10(_rms(decoded) / _rms(source))

    assert abs(level_delta_db) < 0.25
    assert max(abs(sample) for sample in decoded) >= 30000
