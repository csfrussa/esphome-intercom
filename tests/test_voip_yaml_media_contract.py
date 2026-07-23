"""Contracts shared by maintained ESPHome VoIP media profiles."""

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[1]
YAMLS = ROOT / "yamls"

PHYSICAL_AUDIO_STACK_PROFILES = (
    "experimental/waveshare-s3-touch-lcd-1.85c/waveshare-s3-touch-lcd-1.85c-box-full-afe.yaml",
    "full-experience/dual-bus/generic-s3-full-aec.yaml",
    "full-experience/single-bus/generic-s3-full-aec.yaml",
    "full-experience/single-bus/spotpear-ball-v2-full-afe.yaml",
    "full-experience/single-bus/waveshare-p4-touch-full-afe-landscape.yaml",
    "full-experience/single-bus/waveshare-p4-touch-full-afe-portrait.yaml",
    "full-experience/single-bus/waveshare-s3-full-afe.yaml",
    "untested/generic-s3-full-afe.yaml",
    "voip-only/dual-bus/generic-s3-voip.yaml",
    "voip-only/single-bus/generic-s3-voip.yaml",
    "voip-only/single-bus/spotpear-ball-v2-voip.yaml",
)


def _voip_stack_block(text: str) -> str:
    match = re.search(r"(?m)^voip_stack:\n", text)
    if match is None:
        return ""
    tail = text[match.end() :]
    next_top_level = re.search(r"(?m)^\S", tail)
    return tail if next_top_level is None else tail[: next_top_level.start()]


def _format_section(block: str, name: str) -> str:
    match = re.search(rf"(?m)^    {re.escape(name)}:\n", block)
    if match is None:
        return ""
    tail = block[match.end() :]
    next_peer = re.search(r"(?m)^    \S", tail)
    return tail if next_peer is None else tail[: next_peer.start()]


def _has_s16le_mono_format(section: str, sample_rate: int, frame_ms: int) -> bool:
    entries = re.split(r"(?m)^      - ", section)
    return any(
        re.search(rf"(?m)^sample_rate:\s*{sample_rate}\s*$", entry)
        and re.search(r"(?m)^        pcm_format:\s*s16le\s*$", entry)
        and re.search(r"(?m)^        channels:\s*1\s*$", entry)
        and re.search(rf"(?m)^        frame_ms:\s*{frame_ms}\s*$", entry)
        for entry in entries[1:]
    )


def test_resampling_profiles_accept_direct_esp_16khz_10ms() -> None:
    """Every explicit multi-format RX profile accepts the ESP direct-call floor."""
    missing: list[str] = []
    for path in sorted(YAMLS.rglob("*.yaml")):
        if ".esphome" in path.parts:
            continue
        block = _voip_stack_block(path.read_text())
        rx_formats = _format_section(block, "rx_formats")
        if not rx_formats:
            continue
        if not _has_s16le_mono_format(rx_formats, 16000, 10):
            missing.append(str(path.relative_to(ROOT)))

    assert not missing, (
        "VoIP profiles with explicit rx_formats must retain the direct ESP-to-ESP "
        "16000/s16le/mono/10ms compatibility floor:\n" + "\n".join(missing)
    )


def test_physical_audio_stack_speakers_bound_silent_tx_lifecycle() -> None:
    """Mixer drain must eventually release the physical I2S output."""
    missing: list[str] = []
    for relative in PHYSICAL_AUDIO_STACK_PROFILES:
        text = (YAMLS / relative).read_text()
        if not re.search(
            r"(?ms)^  - platform: esp_audio_stack\n"
            r"(?:(?!^  - platform:).)*?^    timeout:\s*1s\s*$",
            text,
        ):
            missing.append(relative)

    assert not missing, (
        "Physical esp_audio_stack speakers behind mixer/resampler sources need a "
        "bounded lifecycle timeout:\n" + "\n".join(missing)
    )


def test_ws3_uses_native_esp32_partition_configuration() -> None:
    """ESPHome 2026.7 native IDF builds ignore the legacy PlatformIO key."""
    relative = (
        "experimental/waveshare-s3-touch-lcd-1.85c/"
        "waveshare-s3-touch-lcd-1.85c-box-full-afe.yaml"
    )
    text = (YAMLS / relative).read_text()

    assert "partitions: partitions_16mb_huge_factory.csv" in text
    assert not re.search(r"(?m)^\s*board_build\.partitions:\s*", text)
