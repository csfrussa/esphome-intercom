# Documentation

Welcome. These pages cover everything beyond the project pitch on the [top-level README](../README.md).

## Pick your path

- 🚀 **Start here**: [DEPLOYMENT_GUIDE.md](DEPLOYMENT_GUIDE.md) is the decision
  tree that maps hardware and features (intercom only, VA + MWW, touch display,
  2-mic Speech Enhancement) to the right ready-to-flash config under
  [`yamls/`](../yamls/).

- 🧭 **Quick examples**: the top-level [README](../README.md#quick-start-examples)
  includes practical doorbell, room-to-room intercom, ESP static contacts and
  HA-managed central roster examples.

- 🧾 **Release / upgrade notes**: [BREAKING_CHANGES.md](BREAKING_CHANGES.md)
  starts from the current SIP/VoIP breaking migration. The full incoming release
  note is [RELEASE_2026_7_0_DEV.md](RELEASE_2026_7_0_DEV.md).

- 📚 **Configuration reference**: [reference.md](reference.md) covers every
  `esphome_voip_stack`, `esp_aec` and `esp_afe` option, every action and condition,
  Home Assistant services, and worked automation examples.

- 🔌 **Wire protocol**: [INTERCOM_PROTOCOL.md](INTERCOM_PROTOCOL.md) is a
  tombstone for the retired proprietary protocol. Current call control is SIP,
  SDP and RTP.

- ☎️ **Optional SIP trunk**: [SIP_TRUNK.md](SIP_TRUNK.md) documents provider
  registration, outbound external routing and inbound DTMF target selection.

- 📒 **Phonebook protocol**: [PHONEBOOK_PROTOCOL.md](PHONEBOOK_PROTOCOL.md)
  documents canonical endpoint rows, `audio_mode`, `tx_formats`/`rx_formats`
  and how HA shapes direct SIP or HA-bridged routes for each ESP.

- 🧱 **Architecture**: [ARCHITECTURE.md](ARCHITECTURE.md) describes component
  decomposition, threading/core affinity, per-frame data flow, the
  `audio_processor` contract, and the drain protocol for glitch-free config
  changes.

- 🧯 **Troubleshooting**: [troubleshooting.md](troubleshooting.md) lists common
  symptoms (no devices found, no audio, echo, latency, ringing without connect,
  phonebook/routing issues) with concrete checks.

- ✅ **SIP migration audit**: [MIGRATION_AUDIT.md](MIGRATION_AUDIT.md) records
  the current public contract and deferred verification matrix.

- 🖼️ **Media refresh plan**: [MEDIA_SHOT_LIST.md](MEDIA_SHOT_LIST.md) lists
  screenshots, photos, GIFs and demo scenes that should replace obsolete README
  media.

## Per-component docs

Each ESPHome component ships its own README with the full option list, YAML snippets and component-specific notes:

- [`esphome_voip_stack`](../esphome/components/esphome_voip_stack/README.md), the ESP SIP
  phone component and Home Assistant bridge integration surface.
- [`esp_audio_stack`](../esphome/components/esp_audio_stack/README.md), the
  coordinated full-duplex audio backend for shared codec buses, dual I2S
  MEMS/amp boards that need software reference handling, Espressif
  rate/layout conversion, AEC reference capture, PSRAM placement and
  post-processor mic output. On AEC/AFE profiles, its standard ESPHome
  microphone facade is the cleaned stream consumed by MWW, Voice Assistant and
  intercom while media/TTS keeps playing through the speaker.
- Full-experience media now uses ESPHome's source-based `speaker_source` path:
  HA media, announcements, local files and optional Sendspin streams feed one
  media player before the mixer arbitrates with intercom and Voice Assistant.
  The local [`speaker`](../esphome/components/speaker/README.md) fork remains
  documented for custom YAMLs that still use `platform: speaker`.
- [`runtime_fsm`](../esphome/components/runtime_fsm/README.md), a generic
  YAML-programmed reducer used by maintained full-experience profiles to derive
  LED, LVGL/display, audio ducking, ringtone and timer policies from one state
  snapshot. It is control-plane only and does not process audio samples.
- [`esp_aec`](../esphome/components/esp_aec/README.md), standalone ESP-SR echo cancellation.
- [`esp_afe`](../esphome/components/esp_afe/README.md), the full Espressif AFE pipeline (AEC + NS + VAD + AGC, optional dual-mic Speech Enhancement).
- [`audio_processor`](../esphome/components/audio_processor/README.md), the abstract processor interface that `esp_aec` and `esp_afe` implement.
