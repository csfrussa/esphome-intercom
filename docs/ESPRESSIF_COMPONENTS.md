# Espressif Components And Licenses

This project is MIT-licensed, but some ESP32 audio features use Espressif
components that are resolved by ESPHome's IDF Component Manager when users build
their own firmware.

The repository ships YAML, ESPHome components and source code. It does not ship
prebuilt firmware binaries for these Espressif audio components.

## Runtime Components

The current audio stack uses these Espressif components:

| Component | Used by | License family | Notes |
|---|---|---|---|
| `espressif/esp-sr` | `esp_aec`, `esp_afe`, pulled directly or through GMF | Espressif MIT-style license, restricted to Espressif products | Provides AEC, AFE, SE/BSS, VAD, NS and AGC libraries. Some DSP implementation is shipped by Espressif as precompiled target libraries. |
| `espressif/gmf_ai_audio` | `esp_afe` | Espressif Modified MIT, restricted to Espressif products | Provides `esp_gmf_afe_manager`, the feed/fetch/suspend/runtime-feature manager used by the dual-mic AFE backend. |
| `espressif/gmf_core` | Transitive dependency of `gmf_ai_audio` | Espressif Modified MIT, restricted to Espressif products | Base GMF object and support layer required by `gmf_ai_audio`. |
| `espressif/esp_audio_effects` | `i2s_audio_duplex` | Espressif MIT-style license, restricted to Espressif products | Provides `esp_ae_rate_cvt`, used for the 48 kHz bus to 16 kHz mic/ref conversion on P4, WS3, Spotpear and no-codec AEC builds. |
| `espressif/esp_codec_dev` | `i2s_audio_duplex` codec-backed builds | Apache-2.0 | Provides codec control plus I2S read/write. P4 and WS3 use ES7210/ES8311 through it; Spotpear single-mic uses ES8311 input/output through it. Generic no-codec AEC still uses the same bus facade with `codec_if = NULL`. |

Current generated P4 and S3 builds resolve the audio stack to:

| Component | Version |
|---|---|
| `espressif/esp-sr` | `2.4.4` |
| `espressif/gmf_ai_audio` | `0.8.2` |
| `espressif/gmf_core` | `0.8.3` |
| `espressif/esp_audio_effects` | `1.0.2` |
| `espressif/esp_codec_dev` | `1.5.4` |

There is no P4 downgrade pin for `esp-sr`. The P4 full AFE target uses the same
GMF/ESP-SR generation as the S3 dual-mic target, including the `esp32p4` and
`esp32p4_less_v3` headers and prebuilt libraries shipped by Espressif.

## Component Boundaries

The Espressif integrations are internal backends of existing ESPHome
components, not new mandatory external components layered on top of each other.
The public composition stays modular:

| ESPHome component | Espressif backend it owns | Required project components | Optional peers |
|---|---|---|---|
| `i2s_audio_duplex` | `esp_codec_dev` for codec/data I/O, `esp_audio_effects` for rate conversion | `audio_processor` helper types and ESPHome `microphone`/`speaker` surfaces | `esp_aec` or `esp_afe` through `processor_id`; `intercom_api`, MWW and VA as consumers |
| `esp_aec` | `esp-sr` low-level `afe_aec` API | `audio_processor` | `i2s_audio_duplex` or standalone `intercom_api` as the caller |
| `esp_afe` | `gmf_ai_audio` `esp_gmf_afe_manager` plus `esp-sr` | `audio_processor` | `i2s_audio_duplex` as the required steady-frame caller |
| `intercom_api` | none of the new Espressif audio libraries directly | ESPHome network/audio surfaces | `i2s_audio_duplex` as the recommended mic/speaker owner, or `esp_aec` only in standalone processor mode |

`i2s_audio_duplex` does not depend on `intercom_api`. A user can install only
`audio_processor`, `i2s_audio_duplex` and optionally `esp_aec` or `esp_afe` for
a Voice Assistant, media-player, microphone/speaker, or custom callback build.
The cross-component validation only runs when both `i2s_audio_duplex` and
`intercom_api` appear in the same YAML, to prevent double ownership of AEC or
DC-offset correction.

## Reference Components

These components are used as official reference material for board and codec
conformance, but are not currently a replacement for `i2s_audio_duplex`:

| Component or BSP | Used for | License family | Notes |
|---|---|---|---|
| Waveshare `esp32_p4_wifi6_touch_lcd_x` BSP | Reference for Waveshare P4 pinout, ES8311/ES7210 setup and PA control | See upstream BSP package | The BSP uses `esp_codec_dev` and standard I2S examples. Our full-duplex TDM path still needs custom ESPHome integration for mic/ref staging, mixer, intercom and Home Assistant entities. |

## Generated-Code Coverage

The migration is not P4-only. The current generated-code snapshots confirm:

- Waveshare P4 landscape AFE: TDM ES7210 input and ES8311 output go through
  `esp_codec_dev`; dual mic is structural with SE/BSS enabled and AEC booting
  disabled for controlled tests. It uses `gmf_ai_audio` and `esp-sr` 2.4.4,
  not the historical standalone P4 `esp-sr` 2.3.x override.
- Waveshare S3 full AFE: TDM ES7210 input and ES8311 output also go through
  `esp_codec_dev`; dual mic is structural with SE/BSS enabled.
- Spotpear single-mic AEC: ES8311 input and ES8311 output go through
  `esp_codec_dev`; stereo RX supplies mic plus playback reference, and the
  processor is standalone `esp_aec` over Espressif's `afe_aec_create("MR", ...)`
  contract.
- Generic S3 AEC: remains no-codec and uses standalone `esp_aec` over the same
  `i2s_audio_duplex` bus facade.
- Generic S3 dual-bus intercom: remains no-codec and uses the same
  `i2s_audio_duplex` facade, but creates separate ESP-IDF I2S simplex channels
  for RX and TX and separate `esp_codec_dev` data interfaces. This path is only
  compiled when YAML uses `rx_bus` and `tx_bus`.

## Source Audit Findings

The current integration has been checked against the Espressif component sources
resolved in the generated builds:

- `esp_codec_dev` accepts `codec_if = NULL` as long as `data_if` is present and
  `dev_type` is not `ESP_CODEC_DEV_TYPE_NONE`. This is the supported no-codec
  path used by generic AEC builds: `esp_codec_dev_open()` still opens the I2S
  data interface, and output falls back to software volume if no codec volume
  callback exists.
- The shared I2S data interface plus separate IN and OUT device handles is an
  official pattern. Espressif's `codec_dev_test` creates one `data_if`, one
  ES8311 DAC device and one ES8311 ADC device, then opens, closes and reopens
  record and playback in both orders.
- Spotpear's single physical ES8311 does not need to be forced into one
  `ESP_CODEC_DEV_TYPE_IN_OUT` device. Espressif's ES8311 driver tracks paired
  ADC/DAC codec instances internally, and the official test covers the same
  split-device shape we use for ESPHome's separate microphone and speaker
  surfaces.
- The TDM ES7210 plus ES8311 flow is also covered by Espressif tests: playback
  uses ES8311 OUT, record uses ES7210 IN, both share one I2S `data_if`, and the
  record side can select a non-contiguous TDM channel mask.
- Single-mic plus playback-reference AEC is covered by Espressif's low-level
  `afe_aec` contract: the input format `MR` means one microphone channel and
  one playback reference channel. Spotpear uses this direct official path after
  runtime testing showed the full AFE manager feed/fetch path saturating on the
  single-mic codec topology.
- Dual physical I2S buses are handled at the IDF channel layer, not by changing
  the ESP-SR audio contract. ESP-IDF supports simplex channel allocation by
  passing only one channel handle to `i2s_new_channel()`. The dual-bus no-codec
  path uses one RX simplex channel and one TX simplex channel on different I2S
  ports, then feeds the same mono mic plus playback reference frames to
  `afe_aec`.
- `esp_gmf_afe_manager` exposes runtime feature toggles for AEC, VAD, SE and
  WakeNet. It does not expose NS or AGC in its feature enum, so keeping NS/AGC
  changes as AFE recreate operations is deliberate while staying on the stock
  manager.
- `esp_gmf_afe_manager_get_chunk_size()` exposes the feed chunk size only. The
  wrapper learns a differing fetch size from the first result callback and bumps
  the ESPHome frame-spec revision if needed. This is a known integration edge to
  watch during runtime tests, not a reason to bypass the manager.

## PSRAM Policy

Do not treat all Espressif components the same:

- `esp-sr` AFE exposes `memory_alloc_mode` in `afe_config_t`. Public YAMLs can
  choose `more_internal`, `internal_psram_balance` or `more_psram`; the current
  default stays aligned with Espressif's PSRAM-friendly AFE profile unless a
  board-specific test proves otherwise.
- `esp_gmf_afe_manager` has no public allocation toggle. Its manager object and
  feed buffer use `heap_caps_calloc_prefer(..., MALLOC_CAP_SPIRAM,
  MALLOC_CAP_INTERNAL)`, and its feed/fetch task stacks are created in PSRAM
  when `CONFIG_SPIRAM_BOOT_INIT` is available. This is an Espressif baseline,
  not a fork point unless runtime evidence points at it.
- `gmf_core` OAL helpers generally allocate from PSRAM when
  `CONFIG_SPIRAM_BOOT_INIT` is enabled. We currently use only the GMF AFE
  manager path, not a full GMF pipeline.
- `esp_codec_dev` does not expose a PSRAM policy in its public codec/data APIs.
  Our integration passes existing IDF I2S handles and the component mostly owns
  small codec/data interface objects plus codec register state.
- `esp_audio_effects` `esp_ae_rate_cvt` exposes quality/performance knobs
  (`complexity` and `perf_type`), but no public "allocate in PSRAM" knob. Our
  wrapper controls the scratch buffers it owns and opens all converter handles
  before the first realtime frame.
- The public dual-mic full-experience targets use a hybrid bridge-buffer
  placement: keep the per-frame AFE feed scratch and fetch output ring internal,
  move the larger AFE feed staging ring to PSRAM, and place ESPHome-owned audio
  and intercom frame buffers in PSRAM. DMA descriptors and I2S driver buffers
  remain internal. This is the current WS3/P4 baseline for HTTPS media plus TTS
  plus intercom stress.

Runtime test interpretation: PSRAM use inside GMF/esp-sr is expected. If a test
crashes or glitches, first capture the stack and memory snapshot; do not fork or
override Espressif allocation policy without evidence.

## Migration Matrix

The current direction is to use Espressif code where it cleanly owns a problem,
and keep ESPHome code where it owns board wiring, callbacks, Home Assistant
entities or device-specific data routing.

| Candidate | Upstream role | Decision | Reason |
|---|---|---|---|
| `esp_gmf_afe_manager` from `gmf_ai_audio` | Owns esp-sr AFE feed/fetch tasks, suspend/resume and runtime feature toggles | Integrated now | It is the official manager layer we were reimplementing. `esp_afe` now uses it directly and leaves its default feed/fetch task settings and allocations intact. |
| `esp_gmf_afe` from `gmf_ai_audio` | Full GMF AFE element with WakeNet/VAD/command state machine | Do not integrate now | MWW and VA are ESPHome/TensorFlow consumers. Pulling this in would duplicate state machines we do not use. The lower manager gives the useful part without forcing WakeNet/command flow. |
| `esp_gmf_aec` from `gmf_ai_audio` | GMF pipeline element for standalone AEC | Defer | It is relevant to future standalone AEC cleanup, but the current `esp_aec` path already uses the same low-level esp-sr `afe_aec` engine without importing GMF port ownership into ESPHome's microphone and speaker facade. |
| `afe_aec` from `esp-sr` | Low-level standalone AEC API | Integrated now | No-codec and Spotpear single-mic AEC devices keep a direct ESPHome-friendly processor while still using Espressif's current AEC implementation and official `MR` input format. |
| `gmf_audio` / `aud_rate_cvt` | GMF audio pipeline elements, including rate conversion, interleave and deinterleave | Reference for now | Official examples run codec at 48 kHz and insert `aud_rate_cvt` before AEC. We copied the underlying converter first; a full GMF IO pipeline remains a separate design decision because ESPHome owns microphone, speaker, mixer and intercom callbacks. |
| `esp_audio_effects` / `esp_ae_rate_cvt` | Standalone C API behind GMF rate conversion | Integrated now | Replaces the old custom rate converter with Espressif's official rate converter while keeping the ESPHome duplex task and callback routing. Multi-channel mic/ref conversion uses one handle so relative latency stays coupled. |
| `gmf_io` / `io_codec_dev` | GMF IO wrapper around `esp_codec_dev_read/write` | Defer | Useful only if we move `i2s_audio_duplex` to a GMF pipeline. The lower `esp_codec_dev` read/write layer is already integrated, so `io_codec_dev` would mostly add GMF port ownership. |
| `esp_codec_dev` | Codec control plus I2S read/write abstraction | Integrated now | `i2s_audio_duplex` now creates one shared I2S data interface and separate IN/OUT codec devices, matching Espressif's test pattern. ES7210/ES8311 control, gain, channel gain, volume, mute and data read/write now go through the official component while ESPHome keeps mic/speaker callback routing. |
| `esp_board_manager` / `periph_i2s` | Board/peripheral lifecycle manager for I2S TX/RX STD/TDM/PDM | Copy patterns now | It confirms the TX/RX clock lifecycle constraint: TX and RX must be initialized together because TX generates the clock. It is not a complete audio processor or ESPHome duplex replacement. |
| `esp_capture` audio sources | High-level capture sources, including codec AEC capture | Do not integrate now | It owns a capture pipeline and source lifecycle intended for recording/streaming. It conflicts with ESPHome's microphone, speaker, mixer and intercom ownership. Useful as reference only. |
| Waveshare BSP | Board pinout, codec, PA and display/touch setup | Copy patterns only | It validates hardware constants and codec setup style. Its audio examples are standard I2S/codec flows, not our 48 kHz full-duplex TDM with two mics plus reference. |

## Practical Rule

When adding or updating audio features, prefer Espressif-provided managers and
codec helpers when they fit the ESPHome architecture. Keep copyright and license
notices intact, cite the component dependency in the relevant ESPHome component,
and do not vendor Espressif source or binaries into this repository unless the
license is reviewed again.
