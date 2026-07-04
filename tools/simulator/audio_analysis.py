#!/usr/bin/env python3
"""Small PCM/WAV analysis helper for simulator assertions."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import wave


def analyze_wav(path: Path) -> dict[str, float | int | str]:
    with wave.open(str(path), "rb") as wav:
        channels = wav.getnchannels()
        sample_width = wav.getsampwidth()
        sample_rate = wav.getframerate()
        frames = wav.getnframes()
        raw = wav.readframes(frames)
    if sample_width != 2:
        raise ValueError("only 16-bit PCM WAV is supported initially")
    samples = [
        int.from_bytes(raw[i:i + 2], "little", signed=True)
        for i in range(0, len(raw), 2)
    ]
    peak = max((abs(sample) for sample in samples), default=0)
    rms = (sum(sample * sample for sample in samples) / len(samples)) ** 0.5 if samples else 0.0
    dc = sum(samples) / len(samples) if samples else 0.0
    return {
        "path": str(path),
        "sample_rate": sample_rate,
        "channels": channels,
        "sample_width": sample_width,
        "frames": frames,
        "duration_s": frames / sample_rate if sample_rate else 0.0,
        "peak": peak,
        "rms": rms,
        "dc_offset": dc,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("wav", type=Path)
    args = parser.parse_args()
    print(json.dumps(analyze_wav(args.wav), indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
