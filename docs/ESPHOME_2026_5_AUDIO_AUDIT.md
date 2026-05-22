# ESPHome 2026.5 Audio Audit

Date: 2026-05-22

Scope: ESPHome 2026.5.0 audio/media/speaker/microphone componentry, checked against the local `esp_audio_stack`, `intercom_api`, `esp_aec`, and `esp_afe` design. This document tracks what should be reused upstream, what is already aligned, and what should stay local because ESPHome does not expose the required contract yet.

## Immediate Changes Applied

### Audio Control Semantics

`speaker_volume` was renamed to `master_volume` in the YAML-facing number platform and in the internal C++ naming. This avoids confusing:

- ESPHome media-player/source volume, which remains the native `speaker::Speaker` volume layer.
- Board/master output volume, which is our extra hardware/software board control.

`mic_attenuation` was renamed to `input_gain`, because the same control now supports both attenuation and positive gain. Attenuation uses ESPHome/esp-audio-libs Q31 gain in the hot path; positive gain remains local saturating scalar code because the official Q31 API represents attenuation/unity, not amplification above 1.0.

`frame_buffers_in_psram` was renamed to `buffers_in_psram` for `intercom_api`, matching `esp_audio_stack` terminology.

`audio_stack_in_psram` was renamed to `audio_task_stack_in_psram`, because the option only moves the audio task stack, not the whole audio stack or GMF/ESP-SR scratch memory.

### Voice PE Package Include

The experimental Voice PE profile now imports the upstream Home Assistant Voice PE package through:

```yaml
voice_pe_base: github://esphome/home-assistant-voice-pe/home-assistant-voice.yaml@dev
```

The previous relative include pointed at a file that is not present in this repository, so `esphome config` could not validate the experimental profile at all.

### Loop Wakeups

ESPHome 2026.5 makes component `loop()` cadence honor the configured loop interval instead of being pulled forward by unrelated scheduler activity. `esp_audio_stack` microphone/speaker wrappers now call the official `enable_loop_soon_any_context()` on start/stop/finish edges, so state transitions are not delayed until the next natural ~62 Hz tick.

The microphone and speaker wrapper loops now disable themselves when stopped. They are woken only by start/stop/finish edges, matching ESPHome 2026.5's event-driven idle-loop direction and removing two steady idle loop calls from full profiles.

`intercom_api.loop()` also parks itself when there is no active call, no open phonebook cycle, and no pending mDNS/endpoint work. Call-state changes, contact updates, mDNS discovery results, and IP endpoint events wake it with `enable_loop_soon_any_context()`.

## Keep / Already Aligned

### Codec Memory Options

Full AFE profiles use:

```yaml
audio:
  codecs:
    flac:
      buffer_memory: psram
```

This matches ESPHome 2026.5 codec memory controls. Keep Generic AEC light without FLAC/timer assets.

If MP3 or Opus is added later, apply the same policy:

- MP3: `audio.codecs.mp3.buffer_memory: psram`
- Opus: consider `state_memory` and `pseudostack.buffer_memory` only on full PSRAM profiles

### Volume Curve

Hardware-codec `master_volume` is intentionally sent to Espressif's `esp_codec_dev_set_out_vol()` as the official 0..100 volume value. Espressif's documented default maps volume 1..100 to roughly -49.5 dB..0 dB, with volume 0 mapped to near-mute. Therefore 70% is not a linear 70% amplitude; it is around -15 dB and will sound much quieter than a linear UI slider.

The no-codec software path follows the same class of dB curve through ESPHome 2026.5 `esp-audio-libs` Q31 gain (`-49 dB..0 dB`). This keeps codec and non-codec boards semantically aligned.

If board UX needs a less aggressive slider, use official curve controls instead of a hidden custom scalar:

- hardware codec: expose a board/profile option that calls `esp_codec_dev_set_vol_curve()`;
- no-codec software path: expose the same YAML-facing minimum dB and feed it into the existing Q31 conversion;
- keep the default at the Espressif/ESPHome curve so existing profiles do not silently change loudness.

### Speaker Media Player Format

Current media player pipelines explicitly use `format: FLAC`. This is correct after 2026.5 because WAV decoding is no longer always included and `codec_support_enabled` is deprecated. There is no `codec_support_enabled` left in maintained YAMLs.

### ESPHome Mixer / Resampler

Keep ESPHome's `mixer` and `resampler` speaker platforms. They are the right public surface for media player, voice assistant, file playback, and intercom RX. Do not replace them with GMF mixer right now.

### MicrophoneSource

Do not replace `esp_audio_stack` microphone output with ESPHome `MicrophoneSource`. It is useful for consumer-side channel selection/bit conversion, but it cannot resample, cannot express our processor cadence, and its gain path is integer Q25 amplification, not the esp-audio-libs Q31 attenuation path we now use.

`intercom_api.microphone` now takes a direct ESPHome microphone ID. Maintained profiles already expose the required 16 kHz / 16-bit / mono stream from `esp_audio_stack`, so this removes the old `MicrophoneSource` copy/conversion stage from the hot path.

`intercom_api.microphone_source` remains available for experimental or raw microphone setups that still need ESPHome's channel/bit-depth/gain wrapper. It is mutually exclusive with `microphone`.

### Naming / Dead-Code Cleanup

The rate-conversion helper was renamed from `FirDecimator` / `fir_decimator.cpp` to `AudioEffectsRateConverter` / `audio_effects_rate_converter.cpp`. The old name implied a local FIR decimator, but the implementation is already an Espressif `esp-audio-libs` pipeline using `esp_ae_rate_cvt`, `esp_ae_bit_cvt`, and `esp_ae_data_weaver`.

`intercom_api.task_stacks_in_psram` now matches the actual resource being moved. The previous pluralization (`tasks_stack_in_psram`) was kept only in history; maintained YAMLs and component APIs use the corrected key.

`intercom_api.buffers_in_psram` now controls the standalone/staging ring buffers too. When false, intercom mic/speaker/reference rings stay internal; when true, the large staging rings prefer PSRAM. This keeps standalone builds honest and avoids always moving rings to PSRAM behind a YAML option that said otherwise.

## Candidate Migrations

### RingBufferAudioSource

ESPHome 2026.5 added `RingBufferAudioSource`, backed by `receive_acquire()` / `receive_release()`, which avoids a copy from ring buffer to transfer buffer and preserves frame alignment across wrap boundaries.

Use it where the data path is single-consumer and read-only after acquisition:

- speaker/media source bridges
- decoder/resampler input staging
- any future custom media source adapter

Do not blindly replace `ring_buffer_caps` yet. ESPHome's stock `RingBuffer::create()` supports `EXTERNAL_FIRST` and `INTERNAL_FIRST`, but it still does not expose strict `INTERNAL`, `PREFER_PSRAM`, `PSRAM_ONLY` placement with boot-time placement logs. Our helper remains useful for auditable hot-path memory.

This does not block reuse. The preferred direction is a thin local adapter:

- allocate storage with our explicit RAM policy and placement log;
- expose the same consumer shape as ESPHome's `RingBufferAudioSource`;
- reuse ESPHome's acquire/release and frame-alignment behavior where possible;
- keep the adapter small enough that upstream changes stay easy to track.

### AudioResampler / AudioTransferBuffer

ESPHome's `audio::AudioResampler` and transfer-buffer classes are reusable for file/media paths. They are less suitable inside `esp_audio_stack` RX/AEC paths because we need:

- coupled multi-channel mic/ref conversion
- TDM/stereo reference layout control
- fail-closed processor frame behavior
- explicit PSRAM/internal placement

Practical direction: reuse these in future intercom/media adapters first. For the real-time AEC/AFE core, only use them through a wrapper that lets us declare internal-vs-PSRAM placement explicitly and keeps processor frame timing fail-closed.

### audio_http + speaker_source

`audio_http` is now an ESPHome media source using microDecoder and can decode HTTP/HTTPS URLs on-device. `speaker_source` media player composes media sources and speakers.

Potential benefit:

- less custom media-source glue if we later want direct device-side HTTP playback
- cleaner separation of media sources from speaker sinks

Do not migrate maintained profiles immediately. The current `speaker` media player already supports the workflows we need, and changing to `speaker_source` would alter playback orchestration and needs runtime validation.

## Not Worth Migrating

### Stock I2S Audio

`i2s_audio` remains insufficient for maintained profiles because we need one coordinated full-duplex owner that can:

- keep RX/TX bus ownership under one backend
- handle codec RX/TX through `esp_codec_dev` / GMF IO
- extract stereo/TDM digital AEC reference
- run 48 kHz bus with 16 kHz processor output
- feed ESPHome microphone/speaker surfaces at the same time

### Full Replacement With ESPHome Audio Source APIs

ESPHome's public audio source/sink abstractions are improving, but they still do not own our core full-duplex constraints. The correct boundary remains:

- expose native ESPHome `microphone::Microphone` and `speaker::Speaker`
- keep full-duplex I2S/codec/processor ownership inside `esp_audio_stack`

## Follow-Up Work

1. Prototype `RingBufferAudioSource` on a non-critical media/intercom RX bridge and measure copy reduction.
2. Evaluate `speaker_source` + `audio_http` on a separate experimental YAML, not maintained profiles.
3. Keep `ring_buffer_caps` until ESPHome exposes strict memory placement or equivalent placement diagnostics.
4. Consider an official amplifier-needed callback surface in `esp_audio_stack`, separate from codec `pa_pin`, so board YAML can control PA timing without duplicating speaker state logic.
5. Review remaining loop-driven state machines after runtime logs; use `enable_loop_soon_any_context()` only on real event edges, not as a continuous high-frequency loop workaround.
