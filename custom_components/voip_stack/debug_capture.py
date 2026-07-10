"""Bounded, path-safe storage helpers for opt-in audio diagnostics."""

from __future__ import annotations

import contextlib
import hashlib
from pathlib import Path


DEBUG_CAPTURE_DIR = Path.home() / ".cache" / "voip_stack_debug"
DEBUG_CAPTURE_MAX_FILES = 24
DEBUG_CAPTURE_MAX_BYTES = 64 * 1024 * 1024


def ensure_debug_capture_dir(directory: Path = DEBUG_CAPTURE_DIR) -> None:
    """Create the opt-in capture directory with user-only permissions."""

    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    directory.chmod(0o700)


def safe_capture_name(value: object) -> str:
    """Return a collision-resistant stem that cannot escape the debug directory."""

    original = str(value or "call")
    readable = "".join(ch if ch.isascii() and (ch.isalnum() or ch in {"-", "_"}) else "_" for ch in original)
    readable = readable.strip("_")[:64] or "call"
    digest = hashlib.sha256(original.encode("utf-8", errors="replace")).hexdigest()[:12]
    return f"{readable}_{digest}"


def wav_pcm_payload(fmt: object, payload: bytes | bytearray) -> tuple[int, bytes]:
    """Return a WAV-compatible sample width and little-endian PCM payload."""

    width = int(getattr(fmt, "container_bytes_per_sample", 2))
    pcm_format = getattr(getattr(fmt, "pcm_format", None), "value", "")
    if pcm_format != "s24le_in_s32":
        return width, bytes(payload)

    # The runtime format stores sign-extended 24-bit samples right-aligned in a
    # 32-bit container. WAV's packed 24-bit representation omits that fourth
    # sign byte; writing the container as 32-bit PCM would attenuate playback.
    raw = memoryview(payload)
    samples = len(raw) // 4
    packed = bytearray(samples * 3)
    packed[0::3] = raw[0 : samples * 4 : 4]
    packed[1::3] = raw[1 : samples * 4 : 4]
    packed[2::3] = raw[2 : samples * 4 : 4]
    return 3, bytes(packed)


def prune_debug_captures(
    directory: Path = DEBUG_CAPTURE_DIR,
    *,
    max_files: int = DEBUG_CAPTURE_MAX_FILES,
    max_bytes: int = DEBUG_CAPTURE_MAX_BYTES,
) -> None:
    """Keep only the newest bounded set of WAV/JSON diagnostic artifacts."""

    if not directory.exists():
        return
    candidates: list[tuple[int, int, Path]] = []
    for path in directory.iterdir():
        if not path.is_file() or path.suffix.lower() not in {".wav", ".json"}:
            continue
        with contextlib.suppress(OSError):
            stat = path.stat()
            candidates.append((stat.st_mtime_ns, stat.st_size, path))
    candidates.sort(reverse=True)
    kept_files = 0
    kept_bytes = 0
    for _mtime, size, path in candidates:
        if kept_files < max(0, int(max_files)) and kept_bytes + size <= max(0, int(max_bytes)):
            kept_files += 1
            kept_bytes += size
            continue
        with contextlib.suppress(OSError):
            path.unlink()
