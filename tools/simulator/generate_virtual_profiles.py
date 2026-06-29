#!/usr/bin/env python3
"""Generate ESPHome Host counterparts for maintained device profiles."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path
import re


OUT = Path("yamls/host")
MIC_INPUT = Path("tests/simulator/audio/mic_input.pcm")
MAX_ESPHOME_NAME = 31


def _profile_paths() -> list[Path]:
    roots = [
        Path("yamls/experimental"),
        Path("yamls/full-experience"),
        Path("yamls/voip-only"),
        Path("yamls/untested"),
    ]
    paths: list[Path] = []
    for root in roots:
        if not root.exists():
            continue
        paths.extend(
            path
            for path in root.glob("**/*.yaml")
            if ".esphome" not in path.parts
            and "generated" not in path.parts
            and path.name != "secrets.yaml"
        )
    return sorted(paths)


def _slug(value: str) -> str:
    value = value.lower().replace("_", "-")
    value = re.sub(r"[^a-z0-9-]+", "-", value)
    value = re.sub(r"-+", "-", value).strip("-")
    return value


def _short_path_slug(path: Path) -> str:
    rel = path.with_suffix("")
    parts = list(rel.parts[1:])
    if not parts:
        return _slug(path.stem)
    category = {
        "full-experience": "full",
        "voip-only": "voip",
        "experimental": "exper",
        "untested": "untested",
    }.get(parts[0], parts[0])
    useful = [category, *parts[1:]]
    return _slug("-".join(useful))


def _host_name(path: Path) -> str:
    if path.as_posix() == "yamls/full-experience/single-bus/spotpear-ball-v2-full-afe.yaml":
        return "spotpear-voip-host"
    base = f"{_short_path_slug(path)}-host"
    if len(base) <= MAX_ESPHOME_NAME:
        return base
    digest = hashlib.sha1(path.as_posix().encode("utf-8")).hexdigest()[:6]
    suffix = f"-{digest}-host"
    return f"{base[: MAX_ESPHOME_NAME - len(suffix)].rstrip('-')}{suffix}"


def _file_name(path: Path) -> str:
    return f"{_short_path_slug(path)}-host.yaml"


def _mac_address(path: Path) -> str:
    digest = hashlib.sha1(path.as_posix().encode("utf-8")).digest()
    return "06:%02x:%02x:%02x:%02x:%02x" % (
        digest[0],
        digest[1],
        digest[2],
        digest[3],
        digest[4],
    )


def _device_profile(path: Path) -> str:
    return _slug(path.stem).replace("-", "_")


def _friendly_name(path: Path, host_name: str) -> str:
    rel = path.with_suffix("").as_posix().removeprefix("yamls/")
    title = rel.replace("/", " ").replace("-", " ").replace("_", " ").title()
    return f"{title} Host ({host_name})"


def _audio_caps(path: Path, text: str) -> tuple[bool, bool, bool]:
    name = path.name
    if "mic-only" in name:
        return True, False, False
    if "speaker-only" in name:
        return False, True, False
    has_voice_assistant = "voice_assistant:" in text
    has_mic = "microphone:" in text or has_voice_assistant
    has_speaker = "speaker:" in text or "media_player:" in text or has_voice_assistant
    return has_mic, has_speaker, has_voice_assistant and has_mic and has_speaker


def _api_block(has_voice_assistant: bool) -> str:
    if not has_voice_assistant:
        return "api:\n"
    return """api:
  services:
    - service: start_va
      then:
        - voice_assistant.start:
            id: va
            silence_detection: true
    - service: stop_va
      then:
        - voice_assistant.stop:
            id: va
"""


def _microphone_block(enabled: bool) -> str:
    if not enabled:
        return ""
    return f"""
microphone:
  - platform: virtual_microphone
    id: mic_host
    input_path: "{MIC_INPUT.as_posix()}"
    sample_rate: 16000
    bits_per_sample: 16
    num_channels: 1
    frame_ms: 20
    repeat: false
"""


def _speaker_block(enabled: bool, host_name: str) -> str:
    if not enabled:
        return ""
    return f"""
speaker:
  - platform: virtual_speaker
    id: speaker_host
    output_path: "test_runs/simulator/{host_name}_va_speaker_output.pcm"
    sample_rate: 16000
    bits_per_sample: 16
    num_channels: 1
"""


def _voice_assistant_block(enabled: bool) -> str:
    if not enabled:
        return ""
    return """
voice_assistant:
  id: va
  microphone: mic_host
  speaker: speaker_host
  use_wake_word: false
"""


def _profile_content(path: Path) -> str:
    text = path.read_text(encoding="utf-8")
    host_name = _host_name(path)
    has_mic, has_speaker, has_voice_assistant = _audio_caps(path, text)
    device_profile = _device_profile(path)
    return f"""# Generated ESPHome Host counterpart.
# Source profile: {path.as_posix()}
# This is a local test target: ESPHome API, virtual microphone/speaker and the
# voip simulator replace ESP32-only hardware so HA can see a separate node.
# No wifi: block is needed on ESPHome Host; it uses the Linux host network.

esphome:
  name: {host_name}
  friendly_name: {_friendly_name(path, host_name)}

host:
  mac_address: "{_mac_address(path)}"

logger:
  level: DEBUG

{_api_block(has_voice_assistant)}
external_components:
  - source: ../../esphome/components

voip_simulator:
  device_profile: "{device_profile}"
  source_profile: "{path.as_posix()}"
  socket_path: "test_runs/simulator/{host_name}-sim.sock"
  speaker_output_path: "test_runs/simulator/{host_name}_speaker_output.pcm"
  microphone_input_path: "{MIC_INPUT.as_posix()}"
  framebuffer_path: "test_runs/simulator/{host_name}_framebuffer.png"
{_microphone_block(has_mic)}{_speaker_block(has_speaker, host_name)}{_voice_assistant_block(has_voice_assistant)}
"""


def generate(limit: int | None = None, clean: bool = True) -> list[Path]:
    OUT.mkdir(parents=True, exist_ok=True)
    if clean and limit is None:
        for old in OUT.glob("*.yaml"):
            old.unlink()
    written: list[Path] = []
    for path in _profile_paths()[:limit]:
        out = OUT / _file_name(path)
        out.write_text(_profile_content(path), encoding="utf-8")
        written.append(out)
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int)
    parser.add_argument("--no-clean", action="store_true")
    args = parser.parse_args()
    for path in generate(args.limit, clean=not args.no_clean):
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
