# Third-Party Notices

Project code in this repository is MIT-licensed unless a file states otherwise.

This file only records the third-party code or build-time dependencies that
matter for normal source and firmware redistribution.

## ESPHome-Derived Code

ESPHome's license is included in `licenses/ESPHOME-LICENSE.txt`.

| Local path | Upstream | Notes |
|---|---|---|
| `esphome/components/speaker/` | ESPHome `speaker` component | Fork with local pause/release and decoder-source scheduling patches. See `esphome/components/speaker/UPSTREAM.md`. |
| `esphome/components/voice_assistant/` | ESPHome `voice_assistant` component | Fork with configurable TTS playback-start timeout. See `esphome/components/voice_assistant/UPSTREAM.md`. |
| `esphome/components/audio/` | ESPHome audio component family | Local copy/adaptation used by maintained media profiles; resolves ESPHome audio codec libraries at build time. |
| `esphome/components/ring_buffer/` | ESPHome ring buffer component | Local CAPS-aware copy used by audio/VoIP paths. |
| `esphome/components/mipi_dsi/` | ESPHome community component lineage | Local P4 display support and panel models. |

When rebasing a fork, update its `UPSTREAM.md` and keep the ESPHome license
available.

## Runtime Python Dependency

| Dependency | Used by | License |
|---|---|---|
| `numpy>=2.0.0` | Home Assistant audio conversion paths | BSD-3-Clause |

`numpy` is installed by Home Assistant from the package index; it is not
vendored in this repository.

## IDF Component Manager Dependencies

ESP firmware builds may resolve these dependencies through ESPHome/ESP-IDF
Component Manager. They are not vendored here; their upstream licenses apply.

| Component | Used by |
|---|---|
| `esphome/esp-audio-libs`, `esphome/micro-decoder`, `esphome/micro-flac`, `esphome/micro-mp3`, `esphome/micro-opus`, `esphome/micro-wav` | ESPHome audio/media decoder paths. |
| `espressif/esp_audio_effects` | ESP Audio Stack rate/format/channel conversion. |
| `espressif/esp_codec_dev` | ESP Audio Stack codec-backed I2S paths. |
| `espressif/esp-dsp`, `espressif/esp-sr`, `espressif/gmf_ai_audio` | ESP AEC/AFE profiles. |

Firmware using Espressif-restricted components is intended for Espressif
products/SoCs.

## Documentation Assets

Images and videos under `docs/images/` are project documentation assets unless
a file-specific notice says otherwise.
