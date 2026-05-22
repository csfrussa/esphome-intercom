# Findings and known issues

A short index of upstream issues we depend on and design decisions worth pinning. Not bug bashing: each entry exists because someone reading the code or YAML may otherwise re-derive the wrong conclusion.

## Upstream: esp-sr VAD stuck at SILENCE without a wake event

**Symptom**: With AFE configured for VAD only (`vad_init=true`, `wakenet_init=false`), the `vad_state` field in `afe_fetch_result_t` stays at `VAD_SILENCE` indefinitely, even on loud speech. As soon as a wake word is detected (or WakeNet is initialized), VAD starts reporting transitions normally.

**Root cause**: esp-sr 2.4.x ties VAD evaluation to the WakeNet inference path internally. When WakeNet is not initialized, the VAD state machine is never advanced. Tracked upstream as [espressif/esp-sr#187](https://github.com/espressif/esp-sr/issues/187).

**Impact on this project**: Our `esp_afe` component exposes `vad_enabled` as a runtime switch and lets users configure thresholds via YAML, but **VAD readings are only reliable on builds that also enable WakeNet** (i.e. micro_wake_word configured against the same AFE). Standalone "VAD-only" pipelines are not supported by upstream and we should not work around it locally.

**What not to do**: Do not synthesize VAD transitions from raw mic energy in
`esp_afe.cpp`. That diverges from the upstream VAD state machine under noise
and makes the sensor less trustworthy than no VAD reading at all. Wait for the
upstream state machine to report real VAD transitions.

## Why a custom `esp_audio_stack` component instead of stock `i2s_audio`

**The stock split**: ESPHome's `i2s_audio` instantiates a separate I2S controller for `microphone` and for `speaker`. On a board where mic and speaker live on different I2S buses (MEMS mic on one bus, I2S amp on another), that is fine for simple capture/playback.

**Where it breaks**: Audio codecs like ES8311, ES8388, WM8960 expose mic and speaker on the **same I2S bus**, sharing pin lines and a clock. Two `i2s_audio` instances cannot bind the same I2S controller; they collide on the driver layer and the second one silently fails or produces glitches. The dual-instance setup also cannot do AEC properly: there is no shared frame cadence between the two paths, so the reference and mic streams are not phase-coherent.

**What `esp_audio_stack` provides** that stock `i2s_audio` does not:

- **Single I2S controller, both directions**: TX and RX share the same `i2s_chan_handle_t` pair, configured once at start time. Required for any single-bus codec.
- **Frame cadence guarantee**: TX and RX run lock-step in one task. The AEC reference is the previous TX frame, rate-converted on the TX side with Espressif's converter (matches Espressif's `esp-gmf aec_rec` pipeline). Phase coherent, no skew, no ghost-tail residual.
- **TDM hardware reference**: When paired with ES7210 in TDM mode, captures the DAC analog feedback on a dedicated ADC slot. Sample-aligned with mic data. The board YAML chooses which slot via `tdm_ref_slot` (Korvo-2 baseline = slot 2 / MIC3, Waveshare P4 Touch = slot 1 / MIC2). The audio task watches the chosen slot's RMS while the speaker is active and emits a one-shot WARN ("TDM AEC reference silent for N frames...") if it stays below -60 dBFS for ~3.2 s, so wiring or PGA mistakes surface immediately instead of running a dead reference forever.
- **Stereo digital reference (ES8311)**: Stereo I2S frame with L=DAC ref, R=ADC mic. Same single-bus codec, sample-accurate ref without extra hardware.
- **Multi-rate operation**: I2S bus at 48 kHz (for high-quality DAC), mic/AEC/VA at 16 kHz via Espressif `esp_ae_rate_cvt`. Not expressible in stock `i2s_audio`.
- **Cross-component validation**: A `FINAL_VALIDATE_SCHEMA` rejects configurations that would create dual processors or dual DC-offset removal between `esp_audio_stack` and `intercom_api`. Catches errors at compile time instead of as runtime audio garbage.

**When stock `i2s_audio` is still the right choice**: Simple one-way capture or playback where no shared AEC reference, media/intercom coexistence or full-duplex lifecycle is required. For maintained intercom profiles, even true dual-bus MEMS+amp hardware now uses `esp_audio_stack` with separate `rx_bus` / `tx_bus` so AEC cadence, speaker reference, ESPHome microphone/speaker facades and runtime hooks stay under one owner. `intercom_api`'s standalone AEC path remains as a compatibility/bring-up mode, not the preferred product architecture.

## Codec baseline vs board-specific override

`packages/codec/es7210_tdm.yaml` is a **baseline** modeled on the Espressif Korvo-2 reference (MIC3 / slot 2 = AEC ref @ 30 dB, MIC4 PGA 0 dB). It is not a "configures every ES7210" package. Boards that route the ES8311 DAC to a different ADC slot must override the affected PGA register from their own `on_boot` lambda **after** the baseline script runs:

| Board | Wiring | YAML setting | PGA override |
|---|---|---|---|
| WS3 / Spotpear / Korvo-2 | DAC -> MIC3 / slot 2 | `tdm_ref_slot: 2` (baseline default) | none (baseline already sets MIC3 = 30 dB) |
| Waveshare P4 Touch | DAC -> MIC2 / slot 1 | `tdm_ref_slot: 1` | reset MIC2 PGA to 0 dB; MIC3 stays at baseline |

If the ref slot RMS stays silent while the speaker is active, the audio stack driver emits a WARN (see troubleshooting). That is the canary for "you forgot to override the PGA for your board".

## P4 esp-sr generation

The P4 audio components are now aligned on esp-sr 2.4.4.

- `esp_aec` wraps the low-level `afe_aec` helper from esp-sr 2.4.4.
- `esp_afe` uses `espressif/gmf_ai_audio` 0.8.2, which depends on esp-sr 2.4.4
  and provides Espressif's AFE manager for feed/fetch/suspend/runtime AEC
  toggles.
- The P4 full-AEC YAML is kept under `yamls/experimental/` as a reference.
  Public P4 presets should be validated against the AFE path.
