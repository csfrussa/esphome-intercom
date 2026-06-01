# Breaking changes

## 2026.6.1: audio stack / GMF migration

`2026.6.1` continues the `2026.5.0` migration and changes the maintained audio
backend. The supported full-experience profiles and current maintained
intercom-only profiles are now based on `esp_audio_stack`, the new shared audio
backend built around ESP-IDF/Espressif audio libraries. This is a backend
migration, not just a YAML rename: I2S ownership, codec IO, AEC reference
handling, rate conversion and microphone/speaker lifecycle now live behind the
audio stack facade.

If you are already on a late `2026.5.0` test build from `dev`, most changes are
YAML/package-level. If you are upgrading from `4.x`, read this section and then
the `2026.5.0` section below.

### Minimum versions

- ESPHome `2026.5.x` or newer is required for the maintained YAMLs.
- Home Assistant Core `2026.5.0` or newer is required for the bundled
  `intercom_native` integration/card.
- HACS metadata now declares the same Home Assistant minimum instead of the old
  permissive `2024.4.0` value.

### YAML tree: maintained audio profiles

Use the maintained YAMLs under `yamls/full-experience/` and
`yamls/intercom-only/`. Local configs that still include pre-2026.6.0 audio
packages should be migrated to the matching `esp_audio_stack` profile.

| Old assumption | New behavior |
|---|---|
| A board-local duplex audio component owns the supported audio path | `esp_audio_stack` owns supported maintained profiles |
| `intercom_api` can be the standalone full-duplex audio backend for maintained profiles | `intercom_api` should consume/provide call logic through the shared audio stack on maintained profiles |
| Codec setup is local/manual package glue | Codec boards use `esp_codec_dev` through the audio stack |
| No-codec boards share the same codec-oriented assumptions | No-codec boards use direct `esp_driver_i2s` read/write through `esp_audio_stack` |

The user-facing YAML surface is more explicit than before. Custom profiles may
need to carry over these options from the nearest maintained preset:

| Area | New / important options |
|---|---|
| Dual-bus no-codec | `rx_bus`, `tx_bus`, `rx_slot_mode`, `mic_channel`, `slot_bit_width` |
| Codec devices | `codec.input`, `codec.output`, `use_stereo_aec_reference`, `reference_channel` |
| TDM boards | `tdm_mic_slots`, `tdm_ref_slot`, `tdm_tx_slot`, `use_tdm_reference` |
| Speaker output | `num_channels`, `speaker_channels`, `tx_channel` |
| AEC reference | `aec_reference`, `aec_reference_buffer_ms`, `aec_ref_ring_in_psram` |
| Runtime resources | `buffers_in_psram`, `audio_task_stack_in_psram`, `gmf_io.reader.*`, `gmf_io.writer.*` |

For INMP441-style modules strapped to one STD stereo slot, use
`rx_slot_mode: stereo` plus `mic_channel: left/right`. `rx_slot_mode: stereo`
does not mean stereo microphone output; it means "read both wire slots and
select the configured mono mic slot in software".

### YAML: Generic profiles split by flash size

Generic S3 profiles are intentionally split:

| Profile | Intended target | Notes |
|---|---|---|
| `generic-s3-full-aec-*` | 4 MB devices | Lightweight AEC path. No full AFE/timer sound payload assumptions. |
| `generic-s3-full-afe-*` | boards with app slot larger than 4 MB | Full AFE path with the richer feature set. Prefer 8 MB or 16 MB flash layouts. |

Do not replace a 4 MB Generic AEC device with the Generic AFE profile unless the
partition layout actually has enough app-slot headroom.

### YAML: dual-mic AFE boards

Waveshare S3 and P4 dual-mic AFE profiles now expose an AEC-off output policy.
When AEC is disabled, the profile selects a raw ESP-SR output channel so the
device really returns a non-AEC mic stream instead of a still-processed BSS/AEC
output.

If you maintain a custom dual-mic YAML, check the `esp_afe:` block and carry
over the `aec_off_output` setting from the closest maintained profile.

### YAML: P4 Touch mic gain range

P4 Touch profiles now expose post-AFE `mic_gain` as `-20..30 dB` instead of
`-20..0 dB`. The ES7210 hardware gain remains configured in the codec block;
the Home Assistant/LVGL Mic Gain control is the runtime user trim.

### OTA behavior on full LVGL/audio devices

Full LVGL/audio profiles enter an OTA maintenance mode before flashing: media,
Voice Assistant, Micro Wake Word, intercom, audio stack and LVGL are paused or
stopped before the OTA write begins. If you copied only pieces of the full YAML,
make sure your local package set includes the OTA maintenance package used by
the maintained profiles.

### Home Assistant card/events

The Home Assistant integration/card now expects the unified
`intercom_native.call_event` model introduced during `2026.5.0`. Legacy event
names are not kept as a compatibility layer. Update automations to the unified
event if you skipped the `2026.5.0` migration.

The card state model also changed around unavailable devices and rapid call
cleanup. Dashboards should use the current bundled card; old copied card files
can keep stale `unavailable` handling or browser-audio teardown behavior.

### Home Assistant: phonebook sensor state moved to an attribute

`sensor.intercom_phonebook` no longer stores the full CSV roster in its state.
The state is now a short summary such as `4 entries`; the authoritative CSV
roster is in the `phonebook` attribute.

This avoids Home Assistant's 255-character entity-state limit. Large rosters
such as apartment panels, mixed TCP/UDP installs and multi-subnet deployments
would otherwise overflow the state field quickly.

| Was | Is |
|---|---|
| `states('sensor.intercom_phonebook')` returns the CSV roster | `states('sensor.intercom_phonebook')` returns `N entries` |
| ESP YAML subscribes to the sensor state | ESP YAML subscribes to `attribute: phonebook` |
| Automations parse the sensor state | Automations should read `state_attr('sensor.intercom_phonebook', 'phonebook')` |

All maintained YAMLs use `packages/intercom/phonebook_subscribe.yaml`, which
already subscribes to the `phonebook` attribute. Custom YAMLs that declared their
own Home Assistant text sensor must add:

```yaml
text_sensor:
  - platform: homeassistant
    entity_id: sensor.intercom_phonebook
    attribute: phonebook
```

### YAML: standalone `intercom_api` audio

`intercom_api` remains usable without `esp_audio_stack`, but only as the
transport/FSM layer over standard ESPHome audio components. It no longer owns a
standalone software AEC path.

| Removed | Replacement |
|---|---|
| `intercom_api.processor_id` | Use native ESPHome `microphone`/`speaker` directly, or put `processor_id` on `esp_audio_stack` for software AEC/AFE. |
| `intercom_api.aec_reference_delay_ms` | Configure reference buffering on `esp_audio_stack`. |
| `switch: - platform: intercom_api, aec:` | Use the controls exposed by `esp_audio_stack`, `esp_aec` or `esp_afe`. |

Speaker-only, mic-only and full-duplex standalone intercom are supported by the
same YAML keys:

- only `speaker:` -> `speaker_only`
- only `microphone:` or `microphone_source:` -> `mic_only`
- both directions -> `full_duplex`

This is intended for devices with hardware/DSP-processed audio such as XMOS
front-ends, and for simple native ESPHome I2S tests. If you need software echo
cancellation, use an `esp_audio_stack` profile.

New ESPHome-native example YAMLs are provided for this path:

- `yamls/full-experience/esphome-native/generic-s3-full-esphome-native-tcp.yaml`
- `yamls/full-experience/esphome-native/generic-s3-full-esphome-native-udp.yaml`
- `yamls/intercom-only/esphome-native/` for full-duplex, mic-only and
  speaker-only intercom-only variants.

These examples are a starting point for native ESPHome audio hardware. They were
tested with an INMP441-style microphone plus MAX98357A-style I2S amplifier on
separate I2S buses. They should also be useful for XMOS / Voice PE-like hardware
where the front-end already provides processed microphone audio, but that exact
hardware path still needs user feedback.

### Home Assistant: routed subnet and mixed-protocol calls

Inbound calls from ESPs on routed subnets/VPN/NAT are matched by PBX-lite caller
identity when the socket source address does not equal the published endpoint
host. UDP additionally tracks the observed packet source as the return path
without rewriting the phonebook. HA still publishes the endpoint IP that peers
should dial; the fallback only affects how HA recognises and answers an already
connected or observed caller.

This was tested across multiple subnets, TCP/UDP combinations, HA PBX on/off and
NAT/routed return paths.

The optional HA `advertise_host` config-flow field is only for cases where HA's
automatically announced address is not reachable by ESP devices.

### Build cache

After moving to `2026.6.1`, clear ESPHome build caches once before compiling.
This matters because the audio backend, IDF managed components and generated
sdkconfig can change at the same time.

```bash
find . -type d -name .esphome -prune -exec rm -rf {} +
```

## 2026.5.0: PBX-lite protocol migration

`2026.5.0` is a major upgrade from the `4.x` line. The project moved from
"PBX-like" wiring to a real **PBX-lite** protocol: still deliberately small, but
with the pieces an intercom system needs in practice: endpoint-aware phonebook
rows, explicit call state, ringing, answer, decline, hangup, error reasons,
direct same-transport ESP calls, HA bridge/PBX routing and browser softphone
legs.

If you are upgrading from a working installation, apply these edits before you
flash the new firmware or restart Home Assistant.

## YAML: `intercom_api`

| Was | Is | Action |
|---|---|---|
| `mode: simple` | _(unset)_ | Remove the line. PBX-lite is the implicit default. |
| `mode: full` | _(unset)_ | Remove the line. PBX-lite is the implicit default. |
| `mode: webrtc` | `mode: raw_udp` | Rename. Same semantics: audio-only UDP, no signaling. |

The `mode:` key is now optional and only accepts `raw_udp`. Any other value fails ESPHome validation at compile time.

PBX-lite is the default. The old `simple` / `full` distinction is gone because
there is no longer a separate "doorbell mode" versus "full intercom mode": a
doorbell is just a phonebook with one HA/browser destination, and a room
intercom is the same state machine with more contacts.

## YAML tree: dual-bus boards

The never-validated generic dual-bus YAML was replaced by maintained
`esp_audio_stack` TCP/UDP profiles. These profiles use `rx_bus` / `tx_bus`
instead of the old `i2s_audio` + standalone `intercom_api.processor_id` path.

| Old path | New path |
|---|---|
| `yamls/intercom-only/dual-bus/generic-s3-dual-intercom_NOT_READY.yaml` | `yamls/intercom-only/dual-bus/generic-s3-intercom-tcp.yaml` or `yamls/intercom-only/dual-bus/generic-s3-intercom-udp.yaml` |
| `yamls/experimental/dual-bus/intercom-only/generic-s3-dual-intercom.yaml` | `yamls/intercom-only/dual-bus/generic-s3-intercom-tcp.yaml` or `yamls/intercom-only/dual-bus/generic-s3-intercom-udp.yaml` |

Update any local fork or symlink that pointed at the old paths.

## Home Assistant: bus events

The separate HA bus events were replaced by one unified call event. If you have
automations or scripts triggering on these, update the trigger:

| Was | Is |
|---|---|
| `intercom_state` / `intercom_native_state_changed` | `intercom_native.call_event` with `scope: session` |
| `intercom_bridge_state` / `intercom_native_bridge_state_changed` | `intercom_native.call_event` with `scope: bridge` |
| `intercom_forward_state` / `intercom_native_forward_state_changed` | `intercom_native.call_event` with `scope: forward` |

Use `type` for automations (`outgoing`, `ringing`, `answered`, `ended`,
`missed`, `failed`) and `state` when you need the exact internal state. The
state-text ESPHome sensor (`sensor.<name>_intercom_state`) is unchanged.

## Home Assistant: phonebook and HA peer name

The phonebook is now endpoint-first. ESPs publish
`sensor.<device>_intercom_endpoint` as `Name|protocol|ip|ports`, HA builds
`sensor.intercom_phonebook`, and firmware packages subscribe to its `phonebook`
attribute.

Do not rely on a hardcoded `"Home Assistant"` contact anymore. The HA peer name
is `hass.config.location_name`, so the contact can be `"Home"`, `"Office"`,
`"Beach House"` or any other name chosen in HA settings.

## C++: `intercom_api` namespace

Only relevant if you have downstream code against the `IntercomApi` C++ class:

- `IntercomApi::set_full_mode(bool)` removed. It was a no-op since the simple/full distinction was retired.
- `IntercomApi::set_webrtc_mode(bool)` renamed to `set_raw_udp_mode(bool)`.
- Protected member `webrtc_mode_` renamed to `raw_udp_mode_`.
