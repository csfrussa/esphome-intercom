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

## Reference Components

These components are used as official reference material for board and codec
conformance, but are not currently a replacement for `i2s_audio_duplex`:

| Component or BSP | Used for | License family | Notes |
|---|---|---|---|
| `espressif/esp_codec_dev` | Reference for codec control patterns | Apache-2.0 | Abstracts codec control, I2S open/read/write, output volume, input gain and per-channel gain. |
| Waveshare `esp32_p4_wifi6_touch_lcd_x` BSP | Reference for Waveshare P4 pinout, ES8311/ES7210 setup and PA control | See upstream BSP package | The BSP uses `esp_codec_dev` and standard I2S examples. Our full-duplex TDM path still needs custom ESPHome integration for mic/ref staging, mixer, intercom and Home Assistant entities. |

## Migration Matrix

The current direction is to use Espressif code where it cleanly owns a problem,
and keep ESPHome code where it owns board wiring, callbacks, Home Assistant
entities or device-specific data routing.

| Candidate | Upstream role | Decision | Reason |
|---|---|---|---|
| `esp_gmf_afe_manager` from `gmf_ai_audio` | Owns esp-sr AFE feed/fetch tasks, suspend/resume and runtime feature toggles | Integrated now | It is the official manager layer we were reimplementing. `esp_afe` now uses it directly and leaves its default feed/fetch task settings and allocations intact. |
| `esp_gmf_afe` from `gmf_ai_audio` | Full GMF AFE element with WakeNet/VAD/command state machine | Do not integrate now | MWW and VA are ESPHome/TensorFlow consumers. Pulling this in would duplicate state machines we do not use. The lower manager gives the useful part without forcing WakeNet/command flow. |
| `esp_gmf_aec` from `gmf_ai_audio` | GMF pipeline element for standalone AEC | Defer | It is relevant to future standalone AEC cleanup, but the current `esp_aec` migration already uses the low-level esp-sr `afe_aec` API without importing a full GMF pipeline. |
| `afe_aec` from `esp-sr` | Low-level standalone AEC API | Integrated now | No-codec and single-mic AEC devices keep a direct ESPHome-friendly processor while still using Espressif's current AEC implementation. |
| `gmf_audio` / `aud_rate_cvt` | GMF audio pipeline elements, including rate conversion, interleave and deinterleave | Reference for now | Official examples run codec at 48 kHz and insert `aud_rate_cvt` before AEC. We copied the underlying converter first; a full GMF IO pipeline remains a separate design decision because ESPHome owns microphone, speaker, mixer and intercom callbacks. |
| `esp_audio_effects` / `esp_ae_rate_cvt` | Standalone C API behind GMF rate conversion | Integrated now | Replaces the default custom FIR/esp-dsp decimator with Espressif's official rate converter while keeping the ESPHome duplex task and callback routing. Multi-channel mic/ref conversion uses one handle so relative latency stays coupled. |
| `gmf_io` / `io_codec_dev` | GMF IO wrapper around `esp_codec_dev_read/write` | Defer | Useful if we move `i2s_audio_duplex` to a GMF pipeline. Not enough by itself because we still need ESPHome mic callbacks, speaker mixer, intercom and reference staging. |
| `esp_codec_dev` | Codec control and optional I2S read/write abstraction | Copy patterns first, integrate later only if it reduces code | Good source for ES8311/ES7210 gain, channel gain, volume and open/read/write semantics. It does not automatically solve the full-duplex TDM ref/mic staging we need. |
| `esp_board_manager` / `periph_i2s` | Board/peripheral lifecycle manager for I2S TX/RX STD/TDM/PDM | Copy patterns now | It confirms the TX/RX clock lifecycle constraint: TX and RX must be initialized together because TX generates the clock. It is not a complete audio processor or ESPHome duplex replacement. |
| `esp_capture` audio sources | High-level capture sources, including codec AEC capture | Do not integrate now | It owns a capture pipeline and source lifecycle intended for recording/streaming. It conflicts with ESPHome's microphone, speaker, mixer and intercom ownership. Useful as reference only. |
| Waveshare BSP | Board pinout, codec, PA and display/touch setup | Copy patterns only | It validates hardware constants and codec setup style. Its audio examples are standard I2S/codec flows, not our 48 kHz full-duplex TDM with two mics plus reference. |

## Practical Rule

When adding or updating audio features, prefer Espressif-provided managers and
codec helpers when they fit the ESPHome architecture. Keep copyright and license
notices intact, cite the component dependency in the relevant ESPHome component,
and do not vendor Espressif source or binaries into this repository unless the
license is reviewed again.
