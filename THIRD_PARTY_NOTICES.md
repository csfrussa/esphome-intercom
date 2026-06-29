# Third-Party Notices

This repository is primarily licensed under the MIT License in `LICENSE`.
Some files, generated firmware builds, or runtime integrations also use
third-party projects under their own licenses. Those licenses remain in force.

This file is a practical attribution and redistribution notice for this
repository. It is not legal advice.

## Project Code

| Area | License | Notes |
|---|---|---|
| Repository-native ESPHome components, Home Assistant integration, Lovelace card, YAML packages and documentation | MIT | Covered by `LICENSE` unless a file or directory states otherwise. |

## ESPHome-Derived Components

ESPHome is licensed under Apache-2.0. Some local components are forks,
adaptations, or compatibility copies of ESPHome components and remain subject
to Apache-2.0 requirements, including preservation of copyright, license and
notice information.

The Apache-2.0 license text is included in `licenses/APACHE-2.0.txt`.

| Local path | Upstream | License | Notes |
|---|---|---|---|
| `esphome/components/speaker/` | ESPHome `esphome/components/speaker` baseline recorded in `esphome/components/speaker/UPSTREAM.md` | Apache-2.0 | Local fork for media-pipeline pause/release behavior. |
| `esphome/components/audio/` | ESPHome audio component family | Apache-2.0 | Local compatibility/adaptation layer used by maintained audio profiles. |
| `esphome/components/ring_buffer/` | ESPHome ring buffer component | Apache-2.0 | Local compatibility/adaptation layer used by audio paths. |
| `esphome/components/voice_assistant/` | ESPHome voice assistant component | Apache-2.0 | Local compatibility/adaptation layer when selected by YAML packages. |

When rebasing these directories against ESPHome, update the relevant upstream
record and keep the Apache-2.0 license text available in this repository.

## Home Assistant And HACS Integration Surface

The custom integration under `custom_components/homeassistant_voip_stack/` uses Home
Assistant integration APIs and is distributed as project code under MIT unless
a file states otherwise. Home Assistant itself and HACS are not vendored in
this repository.

The integration currently declares this runtime Python requirement:

| Dependency | License | Used by | Notes |
|---|---|---|---|
| `numpy>=2.0.0` | BSD-3-Clause | Home Assistant VoIP/audio conversion paths | Installed by Home Assistant from the package index, not vendored here. |

## Espressif IDF Component Manager Dependencies

ESP firmware builds may resolve Espressif components through ESPHome/ESP-IDF
Component Manager. This repository does not vendor Espressif source or binary
libraries for these components.

| Component | Used by | License family | Notes |
|---|---|---|---|
| `espressif/esp-sr` | `esp_aec`, `esp_afe`, Micro Wake Word adjacent audio profiles | Espressif MIT-style/custom license, restricted to Espressif products | Provides AEC/AFE/SE/VAD/NS/AGC functionality. Some implementation is shipped by Espressif as target libraries. |
| `espressif/gmf_ai_audio` | `esp_afe` | Espressif modified MIT-style/custom license, restricted to Espressif products | Provides GMF AFE elements and manager APIs used by the AFE wrapper. |
| `espressif/gmf_core` | Transitive GMF dependency | Espressif modified MIT-style/custom license, restricted to Espressif products | Base GMF runtime used by GMF audio components. |
| `espressif/esp_audio_effects` | `esp_audio_stack` | Espressif MIT-style/custom license, restricted to Espressif products | Provides rate conversion, bit-depth conversion and layout conversion primitives. |
| `espressif/esp_codec_dev` | Codec-backed `esp_audio_stack` profiles | Apache-2.0 | Provides codec control and codec-backed I2S read/write paths. |
| `espressif/esp-dsp` | AEC/AFE support and selected media/artwork packages | Apache-2.0 | Pulled by ESP-IDF Component Manager when selected by the YAML profile. |

Firmware built with Espressif-restricted components is intended for Espressif
SoCs/products. If this project ever distributes prebuilt firmware images, the
firmware distribution must include the applicable Espressif notices and license
texts for the exact resolved component versions.

See `docs/ESPRESSIF_COMPONENTS.md` for the detailed technical audit and
component boundary notes.

## ESPHome IDF Audio Codec Dependencies

Some ESPHome audio/media paths may resolve small codec components such as
`esphome/micro-wav`, `esphome/micro-mp3`, `esphome/micro-flac`,
`esphome/micro-opus` and `esphome/micro-decoder` through ESPHome's normal build
flow when the corresponding YAML feature is enabled. They are not vendored in
this repository; their upstream licenses apply.

## Trademark Notes

ESPHome, Home Assistant, HACS, Espressif, ESP-IDF and related project names are
used descriptively. This repository is not an official Espressif, ESPHome, Home
Assistant or HACS product unless those upstream projects state otherwise.

Do not use upstream logos or trade dress in this repository without following
the relevant upstream trademark and brand guidelines.
