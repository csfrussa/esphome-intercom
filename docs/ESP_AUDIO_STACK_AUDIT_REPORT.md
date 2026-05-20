# ESP Audio Stack Audit Report

Audit date: 2026-05-20

Baseline refs:

- `origin/main`: `f160c40` (`docs: clarify dashboard view path placeholder`)
- published `origin/dev`: `dece38e` (`docs: align espressif audio profiles`)
- current refactor branch: `gmf-backend-prototype`, same commit as `origin/dev` plus working-tree refactor

## External Espressif Findings

Primary sources checked:

- ESP-IDF I2S driver documentation: `esp_driver_i2s` supports standard/TDM full-duplex by registering TX and RX channels on the same I2S port; the channels share BCLK and WS. PDM full-duplex is not supported because TX/RX clocks differ. Source: <https://docs.espressif.com/projects/esp-idf/en/latest/esp32/api-reference/peripherals/i2s.html>
- ESP-IDF performance guidance: Wi-Fi runs at priority 23 on Core 0 by default, lwIP TCP/IP at priority 18, and priority 19 on Core 0 is acceptable for time-critical tasks that do not do network operations. Source: <https://docs.espressif.com/projects/esp-idf/en/v5.1/esp32/api-guides/performance/speed.html#task-priorities>
- `esp_audio_effects`: official modules cover sample-rate conversion, bit conversion, channel conversion, data weaving, mixer, ALC, DRC, MBC, EQ, fade, sonic and howling suppression. Source: <https://components.espressif.com/components/espressif/esp_audio_effects>
- `esp_codec_dev`: official codec abstraction supports unified playback/record APIs and devices including ES8311 and ES7210. Source: <https://components.espressif.com/components/espressif/esp_codec_dev>
- `gmf_ai_audio`: official AFE manager owns feed/fetch tasks, dynamic feature toggles and suspend/resume. Source: <https://components.espressif.com/components/espressif/gmf_ai_audio>
- ESP-GMF `aec_rec` example: playback pipeline uses `CODEC_DEV_TX`, capture pipeline uses `CODEC_DEV_RX -> Rate_cvt -> AEC`. Source: <https://components.espressif.com/components/espressif/gmf_ai_audio/versions/0.8.1/examples/aec_rec>
- ESP-GMF `wwe` example: wake/AFE pipeline uses `CODEC_DEV_RX -> GMF_AFE -> GMF_PORT`; troubleshooting requires AFE feed/fetch tasks on different CPU cores. Source: <https://components.espressif.com/components/espressif/gmf_ai_audio/versions/0.8.1/examples/wwe>
- ESP-GMF source defaults: `ESP_AFE_MANAGER_FEED_TASK_CORE=0`, `FETCH_TASK_CORE=1`, both priority 5, stack 3072. Source: <https://github.com/espressif/esp-gmf/blob/main/elements/gmf_ai_audio/include/esp_gmf_afe_manager.h>

Conclusion: the correct Espressif-native layering is not "one GMF graph owns everything" yet. It is also not a grab bag of unrelated IDF examples. The practical stack must stay inside the ESP-IDF/ESP-GMF audio ecosystem:

1. `esp_driver_i2s` for official I2S channel ownership, DMA policy, STD/TDM setup and full-duplex TX/RX handles.
2. `esp_codec_dev` for codec/no-codec data devices and codec volume/gain/mute; this is also the codec layer used by GMF IO.
3. `gmf_io/io_codec_dev` for codec read/write IO abstraction, matching `CODEC_DEV_RX` / `CODEC_DEV_TX` in GMF examples.
4. `gmf_audio` as the GMF audio element family; its rate/bit/channel/data-weaver elements are backed by `esp_audio_effects`.
5. `esp_audio_effects` C APIs only where ESPHome still owns the loop and a full GMF element graph would add copies. This is not an external replacement library; it is the official implementation layer behind GMF audio effects.
6. `gmf_ai_audio/esp_gmf_afe_manager` for AFE feed/fetch scheduling and runtime feature control.
7. ESPHome compatibility glue for microphone, speaker, mixer, VA, MWW and intercom fanout.

`esp_board_manager/periph_i2s` was audited and rejected for the runtime backend for now. It is official Espressif code, but its current adapter hardcodes the IDF default channel config (`dma_desc_num=6`, `dma_frame_num=240`) and enables channels during peripheral ref. That loses the existing 10 ms DMA policy and the cleaner ESPHome prepare/open/close lifecycle. Using `esp_driver_i2s` directly is still an Espressif-native path; it is the official lower layer that `periph_i2s` uses internally, not a legacy fallback.

Architectural rule: do not import audio libraries from arbitrary examples or separate projects just because the API fits. A dependency is acceptable only if it is either part of ESP-IDF itself, part of ESP-GMF, or an official component used by ESP-GMF/ADF for the same layer. If an official GMF element can own the data path without breaking ESPHome semantics, prefer that. If ESPHome must keep ownership of the loop, use the official lower-level C API from the same Espressif stack rather than maintaining custom DSP.

## Main -> Published Dev Summary

`main` already had the custom `i2s_audio_duplex` concept:

- direct `esp_driver_i2s` ownership;
- ESPHome microphone/speaker facade;
- software previous-frame or ring-buffer AEC reference;
- stereo ES8311 reference and TDM ES7210 reference;
- custom/esp-dsp FIR decimation;
- lifecycle hooks, consumer registry, speaker buffer, mic gain, volume controls and telemetry/debug paths.

Published `dev` had already moved several pieces toward Espressif:

- `esp_codec_dev` codec backend for ES8311/ES7210;
- `esp_audio_effects` rate converter, but pinned at `1.0.2`;
- `esp-sr`/`gmf_ai_audio` based AFE work;
- dual-bus `rx_bus`/`tx_bus`;
- better WS3/P4/Spotpear profile tuning and PSRAM placement.

Published `dev` still kept:

- public component name `i2s_audio_duplex`;
- direct I2S channel read/write/enable/disable lifecycle in the component;
- pinned registry deps for at least the audio stack path;
- no public GMF IO knobs;
- no GMF IO wrapper on codec read/write;
- some stale debug-probe documentation.

## Current Refactor Summary From Published Dev

Diff from `origin/dev` at audit time is a large working-tree refactor across the component, packages, YAML examples and docs. Use `git diff --stat` for the exact current count because this audit file and the refactor plan are new untracked docs until committed.

Major changes:

- renamed public component and namespace from `i2s_audio_duplex` to `esp_audio_stack`;
- migrated packages from `packages/i2s_audio_duplex` to `packages/esp_audio_stack`;
- migrated YAMLs to `esp_audio_stack:` and `platform: esp_audio_stack`;
- removed all old public symbols/names from active code (`i2s_audio_duplex`, `i2s_duplex`, `I2SAudioDuplex`, `USE_I2S_AUDIO_DUPLEX`, old microphone/speaker class names);
- changed dependency policy to registry latest, no pins, unless a concrete upstream regression is found;
- added `gmf_io` dependency;
- included built-in `esp_driver_i2s`;
- enabled `CONFIG_I2S_ISR_IRAM_SAFE`;
- kept I2S ownership on official `esp_driver_i2s`, but removed stale custom policy around read/write by feeding the handles into `esp_codec_dev` and GMF IO;
- creates channels on start, lets `esp_codec_dev_open()` enable them, then closes GMF IO/codec and deletes channels after the audio task parks;
- kept dual-bus mode compile-gated through `USE_ESP_AUDIO_STACK_DUAL_BUS`;
- wrapped codec devices in GMF `io_codec_dev` for read/write instead of direct audio-data-path `esp_codec_dev_read/write`;
- exposed `gmf_io.reader.*` and `gmf_io.writer.*` official knobs;
- validates GMF IO async mode so `io_size`, `buffer_size` and `task_stack_size` are either all zero for synchronous IO or all non-zero for GMF data-bus/task mode;
- replaced custom TX/RX format expansion paths with `esp_ae_bit_cvt` and `esp_ae_data_weaver` where applicable;
- kept `esp_ae_rate_cvt` and added official complexity/perf knobs;
- exposed GMF AFE manager feed/fetch task knobs in `esp_afe`;
- enforced feed/fetch on different cores at config-validation time;
- updated WS3 full AFE TCP/UDP profile to preserve the prior balanced PSRAM/task profile with the new knobs;
- removed stale `debug_probe` package guidance and mapped deep debug to telemetry plus ESP-IDF tracing/task stats.

## Feature Coverage Matrix

| Main/dev capability | Current coverage | Espressif layer now used | Status |
| --- | --- | --- | --- |
| Single-bus full duplex I2S | `esp_audio_stack` same public behavior | official `esp_driver_i2s` channel pair | Covered |
| Dual I2S bus RX/TX | `rx_bus` + `tx_bus`, different I2S ports, compile gated | official `esp_driver_i2s` simplex channels per port | Covered |
| Codec-less MEMS mic + I2S amp | Same YAML knobs, same ESPHome mic/speaker API | `esp_driver_i2s` + `esp_codec_dev` with `codec_if = NULL`; software reference bridge remains ESPHome glue | Covered |
| ES8311 playback/record codec | `codec.output` / `codec.input` ES8311 support | `esp_codec_dev` + `gmf_io/io_codec_dev` | Covered |
| ES7210 TDM dual mic | `tdm_mic_slots`, `codec.input.type: es7210` | `esp_driver_i2s` TDM config + `esp_codec_dev` | Covered |
| Hardware AEC reference from ES8311 stereo feedback | `use_stereo_aec_reference`, `reference_channel` | `esp_codec_dev` data path + `esp_ae_deintlv_process` | Covered |
| Hardware AEC reference from TDM slot | `use_tdm_reference`, `tdm_ref_slot`, slot-level diagnostics | TDM via `esp_driver_i2s`; slot split via `esp_audio_effects` | Covered |
| No-codec software reference | `aec_reference: previous_frame` or `ring_buffer` | No direct official equivalent; kept as bridge glue | Covered as compatibility glue |
| Sample-rate conversion 48 kHz -> 16 kHz | `output_sample_rate`, `audio_effects.rate_cvt_*` | GMF `aud_rate_cvt` equivalent via `esp_ae_rate_cvt` | Covered |
| Bit-width conversion 16/24/32 bus formats | TX/RX conversion path | GMF `aud_bit_cvt` equivalent via `esp_ae_bit_cvt` | Covered |
| Interleave/deinterleave stereo/TDM layout | Mic/ref slot selection and TX slot layout | GMF data-weaver equivalent via `esp_ae_data_weaver` APIs | Covered |
| Custom FIR / esp-dsp decimator selection | Removed public backend selector | `esp_ae_rate_cvt` only | Replaced |
| ESPHome speaker platform | `speaker: platform: esp_audio_stack` | ESPHome API glue; codec writes via GMF IO | Covered |
| ESPHome microphone platform | `microphone: platform: esp_audio_stack` | ESPHome API glue; codec reads via GMF IO | Covered |
| ESPHome mixer/ducking | Still uses ESPHome mixer | Intentional: Espressif mixer not imported to avoid fighting ESPHome media pipeline | Covered by existing ESPHome layer |
| MWW / VA / intercom fanout | Mic callbacks and consumer registry preserved | ESPHome compatibility glue | Covered |
| AEC standalone | `esp_aec` remains | `esp-sr` low-level AEC | Covered |
| Full AFE AEC/NS/AGC/VAD/SE | `esp_afe` remains | `gmf_ai_audio/esp_gmf_afe_manager` + `esp-sr` | Covered |
| AFE feed/fetch scheduling | New YAML knobs and validation | official GMF AFE manager task settings | Covered |
| AFE suspend/resume when no consumers | `esp_afe` active/idle manager control | `esp_gmf_afe_manager_suspend` + read callback install/remove | Covered |
| Task priority/core tuning | `esp_audio_stack`, `esp_afe`, `gmf_io` knobs | ESP-IDF/GMF task settings | Covered |
| PSRAM placement for heavy WS3 profile | Existing stack/buffer knobs plus AFE feed/fetch ring choices | ESP-IDF PSRAM stack option + component alloc caps | Covered |
| Runtime start/stop hooks | `on_start`, `on_idle`, `on_state`, mic/speaker edges | ESPHome automation glue | Covered |
| Speaker PA power gating hooks | Packages migrated to `esp_audio_stack` | ESPHome automation glue | Covered |
| TDM reference silent canary | Preserved diagnostic path | ESPHome sensor/log glue | Covered |
| Telemetry | Compile-gated telemetry still present | ESP-IDF timing APIs + processor telemetry | Covered |
| Old `debug_probe` PCM dump | Removed | Replaced by `telemetry: true` plus IDF trace/task/heap debug package | Replaced, not equivalent |
| WakeNet | Not ported | Intentional: ESPHome uses TensorFlow Micro Wake Word | Out of scope |
| Full GMF element graph owns whole app | Not implemented | Current GMF use is IO + AFE manager + GMF audio-effect implementation APIs | Deferred |

## Current Best-Practice Alignment

Aligned:

- I2S ownership now goes through Espressif's official `esp_driver_i2s` channel API with explicit DMA/auto-clear policy, not through private or example-local code.
- `esp_board_manager/periph_i2s` was evaluated as the higher-level official adapter, but the current component stays on `esp_driver_i2s` because that preserves DMA sizing, dual-bus, STD/TDM asymmetry and lifecycle control without leaving the Espressif stack.
- Codec read/write is now GMF IO wrapped over `esp_codec_dev`, matching the `CODEC_DEV_RX` / `CODEC_DEV_TX` direction of Espressif examples.
- Format conversion uses the same official `esp_audio_effects` implementation family that GMF `gmf_audio` elements wrap.
- AFE scheduling uses GMF AFE manager feed/fetch tasks, with exposed task core/priority/stack knobs.
- WS3 heavy profile keeps feed/fetch manager tasks split across cores and keeps `audio_stack` at Core 0 priority 19: below Wi-Fi 23, above lwIP 18, no TCP/IP work inside that task.
- Registry dependencies are not pinned.
- ESPHome mixer remains the user-facing mixer, because it already handles media player, ducking and ESPHome API expectations.

Still custom, by design:

- The ESPHome microphone/speaker compatibility layer.
- The consumer registry and fanout to MWW, VA and intercom.
- The no-codec software AEC reference bridge. Espressif examples assume hardware loopback/reference in the capture stream for their cleanest AEC path; our discrete MEMS + I2S amp boards need this compatibility bridge.
- The speaker staging ring and lifecycle hooks for ESPHome media player behavior.

## Audit Gaps / Risk

- No firmware build was run in this environment because the `esphome` CLI is not installed.
- Hardware validation is still required for four scenarios: single-bus codec, single-bus no-codec, dual-bus no-codec, ES7210/ES8311 TDM dual-mic.
- `esp_driver_i2s` DMA sizing is computed internally as six descriptors of about 10 ms each, clamped to the 4092-byte descriptor limit. The old public schema did not expose DMA knobs, so no user-facing feature was lost, but underrun/glitch telemetry should be checked on hardware.
- `gmf_io` is intentionally synchronous by default for AFE profiles. Async reader/writer tasks are exposed, but should only be enabled after measuring starvation because they add data-bus buffering and latency between mic/ref and AFE.
- A full GMF element graph is deferred. Doing that correctly would require a GMF port bridge for ESPHome speaker/microphone callbacks; partially wrapping only some elements would add copies without eliminating the ESPHome compatibility layer. Until then, the rule is to use GMF IO, GMF AFE manager and the official GMF audio-effect implementation APIs, not unrelated third-party or example-local code.

## Verification Performed

- `git fetch origin --prune` completed.
- Compared `origin/main..origin/dev`.
- Compared current working tree against `origin/dev`.
- Searched active repo for old public names: no matches for `i2s_audio_duplex`, `i2s_duplex`, `I2SAudioDuplex`, `USE_I2S_AUDIO_DUPLEX`, `duplex_microphone`, `duplex_speaker`, or `debug_probe`.
- Searched active code for stale board-manager/periph strings and GMF delay macros; active code has no `board_manager`, `periph_i2s`, `ESP_BOARD_*` or `ESP_GMF_MAX_DELAY` references.
- Searched active code for direct I2S channel data-path calls. The active stack uses `esp_driver_i2s` for channel ownership; codec read/write goes through GMF IO.
- `python -m py_compile` passed for `esp_audio_stack`, `esp_afe`, `esp_aec`.
- `git diff --check` passed.
