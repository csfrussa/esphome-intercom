# Intercom API Component

ESPHome component for bidirectional full-duplex audio streaming. One product mode (PBX-lite) on top of two transports: TCP (default, framed protocol on `tcp_port`, default 6054) and UDP (`MessageHeader` framing on `udp_control_port`, raw L16 PCM on `udp_audio_port`). A separate `mode: raw_udp` strips PBX-lite signaling for go2rtc-style audio-only consumers.

## Overview

`intercom_api` is transport-agnostic at the FSM level. PBX-lite signaling is the same on TCP and on UDP (control socket). PBX-lite is the implicit default: phonebook / contacts / destination / caller entities are always exposed (the "single doorbell" case is just a phonebook with one entry). Omit the `mode:` key for the default behaviour.

`mode: raw_udp` is the only opt-in: an audio-only UDP path that bypasses signaling (go2rtc, two-room direct link).

Routing policy lives on the ESP, runtime per-device:

- `routing_mode: device_independent` (default): ESP dials the phonebook entry directly (peer ip + port from contacts).
- `routing_mode: ha_pbx`: ESP always dials the HA peer named by `hass.config.location_name`; HA bridges to the real destination. `dest_name` is preserved in the payload.

The HA peer name is `hass.config.location_name` (NEVER hardcoded "Home Assistant" or a localized default). The default `ha_peer_name_` on the ESP is empty; `ha_pbx` routing without it logs an ERROR rather than guessing. Standard YAML packages learn the live name from the `Name|ha|...` row inside `sensor.intercom_phonebook`; custom YAML that bypasses the standard subscription can still call `esphome.<slug>_set_ha_peer_name`.

## Features

- **PBX-lite signaling** on TCP and UDP (control), with the same `MessageHeader` wire format
- **Per-device routing** (`device_independent` vs `ha_pbx`), runtime-toggleable
- **Phonebook with name-based dedup** (last writer wins on endpoint conflict)
- **FreeRTOS Tasks** for non-blocking audio processing
- **Finite State Machine** for call states (Idle -> Ringing -> Streaming)
- **Audio Processor Integration** via `esp_aec`, or `esp_afe` when the microphone path is fed by `i2s_audio_duplex`
- **Persistent Settings** saved to flash
- **ESPHome Native Platforms** for switches, numbers, sensors

## Transports

| Aspect | `protocol: tcp` (default) | `protocol: udp` (PBX-lite) | `mode: raw_udp` |
|--------|---------------------------|---------------------------|------------------|
| Wire format | 3 B header (u8 type + u16 length LE) + payload, framed | Same `MessageHeader` on control, raw L16 on audio | Raw L16 PCM 16-bit mono 16 kHz, one frame per datagram, no header |
| Ports | `tcp_port` (default 6054) | `udp_audio_port` (default 6054, different protocol stack) + `udp_control_port` (default 6055) | `listen_port` / `remote_port` |
| Signaling | START/STOP/RING/ANSWER + PING/PONG keepalive | Same | None - switch on/off both ends |
| Loss behaviour | TCP retransmit, no audible glitch | Datagram drop = glitch on audio, control retried | Datagram drop = glitch (no recovery) |
| Use cases | Default for routed/VLAN/container networks, HA broker, browser card, PBX-style routing | Simple LANs where low latency is preferred and packet loss is controlled | go2rtc, two-room direct link, baby monitor |
| Wire compat | Stable since v1.0 | Stable | Standard raw L16 PCM stream (go2rtc-compatible) |

TCP and UDP expose the same PBX-lite call model, but they are not the same
networking tradeoff. TCP is the recommended default when the path crosses
routing, VLANs, container networking or filtered Wi-Fi. UDP is best for simple
local LANs where latency is the main goal and the audio/control ports are known
to pass cleanly.

`raw_udp` example (two ESPs talking directly, no HA in path):

```yaml
intercom_api:
  protocol: udp
  mode: raw_udp                         # bypass PBX-lite signaling
  remote_ip: !secret peer_intercom_ip   # IP of the OTHER ESP
  remote_port: 6054
  listen_port: 6054
  microphone: mic_main
  speaker: intercom_speaker
```

For go2rtc, point `remote_ip` at the go2rtc host and configure go2rtc with a
`udp://0.0.0.0:6054?codec=L16&rate=16000&channels=1` source.

## AEC quality: standalone vs i2s_audio_duplex

Echo cancellation quality depends on how the speaker reference is captured:

- **With `i2s_audio_duplex`** (recommended): mic and speaker share the same
  I2S bus and the duplex driver hands an already-decimated, phase-coherent
  reference to the AEC each frame. After the TX-side decimation refactor in
  v4.0.0 the residual echo is essentially gone.
- **Standalone `intercom_api`** (mic + speaker on separate components): the
  speaker reference is captured into a 80 ms ring buffer (`spk_ref_buffer_`)
  and consumed by the mic AEC pass. Producer and consumer run on different
  threads with jitter between them, so phase coherence is looser and a small
  residual echo can remain. AEC still works and the call is intelligible,
  but the duplex setup measurably wins.

For a "doorbell + voice assistant" composite device, prefer the duplex setup
described in the top-level [README](../../../README.md#audio-components).

## Architecture

```
┌─────────────────────────────────────────────────────────────────┐
│                      intercom_api Component                      │
│                                                                  │
│  ┌──────────────────────────┐                                   │
│  │  IntercomTransport       │  ← Tcp / Udp implementation       │
│  │  (recv loop on Core 1)   │                                   │
│  │  • accept / recvfrom     │                                   │
│  │  • dispatch AUDIO/control│                                   │
│  └────────┬─────────────────┘                                   │
│           │ on_audio_frame / on_control / on_connection_change  │
│           ▼                                                      │
│  ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐ │
│  │   FSM + setup   │  │    tx_task      │  │  speaker_task   │ │
│  │  (main loop)    │  │   (Core 0, p5)  │  │   (Core 0, p4)  │ │
│  │ • Call state    │  │ • mic_buffer_   │  │ • speaker_buf   │ │
│  │ • set_active_   │  │ • audio_proc.   │  │ • I2S write     │ │
│  │ • Triggers      │  │   process()     │  │ • AEC ref feed  │ │
│  │                 │  │ • transport→TX  │  │                 │ │
│  └─────────────────┘  └────────┬────────┘  └────────┬────────┘ │
│                                │                    │          │
│                                ▼                    ▼          │
│  ┌─────────────────────────────────────────────────────────────┐│
│  │                    Ring Buffers                             ││
│  │  mic_buffer_        │  spk_ref_buffer_         │  speaker_ ││
│  │  mic→TX             │  speaker ref for AEC     │  net→spk  ││
│  └─────────────────────────────────────────────────────────────┘│
└─────────────────────────────────────────────────────────────────┘
                              │
            ┌─────────────────┼─────────────────┐
            ▼ TCP :6054       ▼ UDP :6054       ▼ UDP :6054
   ┌─────────────────┐  ┌─────────────────┐  ┌─────────────────┐
   │  Home Assistant │  │   Other ESP     │  │     go2rtc      │
   │  (TCP client)   │  │  (raw PCM peer) │  │  (raw L16 src)  │
   └─────────────────┘  └─────────────────┘  └─────────────────┘
```

## Configuration

### Basic Configuration

```yaml
external_components:
  - source:
      type: git
      url: https://github.com/n-IA-hane/esphome-intercom
      ref: main
    components: [audio_processor, intercom_api, esp_aec]
    # Or with esp_afe (full pipeline: AEC + NS + VAD + AGC):
    # components: [audio_processor, intercom_api, esp_afe]

intercom_api:
  id: intercom
  microphone: mic_component
  speaker: spk_component
```

### Complete Configuration

```yaml
intercom_api:
  id: intercom
  # mode: omitted - PBX-lite is the default and only mode (use raw_udp for audio-only UDP)
  routing_mode: device_independent     # or ha_pbx
  microphone: mic_component
  speaker: spk_component
  processor_id: aec_processor          # Optional: echo cancellation
  dc_offset_removal: true              # For mics with DC bias
  ringing_timeout: 30s                 # Auto-decline timeout

  # Event callbacks
  on_outgoing_call:
    - logger.log: "Outgoing call"
  on_ringing:
    - logger.log: "Ringing"
  on_dest_ringing:
    - logger.log: "Destination is ringing"
  on_destination_changed:
    - logger.log: "Destination changed"
  on_streaming:
    - logger.log: "Streaming started"
  on_idle:
    - logger.log: "Idle"
  on_hangup:
    - logger.log:
        format: "Hangup: %s"
        args: ['reason.c_str()']
  on_call_failed:
    - logger.log:
        format: "Failed: %s"
        args: ['reason.c_str()']
```

### Configuration Options

| Option | Type | Default | Description |
|--------|------|---------|-------------|
| `id` | ID | Required | Component ID for referencing |
| `mode` | string | _unset_ (PBX-lite) | Optional opt-in. Only accepted value: `raw_udp` (audio-only UDP, no signaling). Omit for the default PBX-lite behaviour. |
| `routing_mode` | string | `device_independent` | `device_independent` (ESP dials peers directly from phonebook) or `ha_pbx` (ESP always dials the HA peer named by `hass.config.location_name`, HA bridges). Runtime-toggleable via the `Routing Mode` switch when exposed. |
| `use_ha_as_first_contact` | bool | `false` | After the first post-boot phonebook batch containing the learned `Name\|ha\|...` row, select the HA peer as the initial destination. Invalid with `mode: raw_udp`. |
| `announce` | bool | `false` | Opt-in ESP-side mDNS announce. Standard HA-managed YAMLs leave this disabled; ESP-only deployments enable it through `packages/intercom/mdns_discovery.yaml`. |
| `discovery.mdns` | bool/map | `false` | Opt-in ESP-side peer discovery. When `true`, a small background task queries `_intercom-tcp._tcp` / `_intercom-udp._udp`, reads TXT `endpoint=<Name|protocol|ip|ports>`, and merges matching peers into the phonebook. |
| `microphone` | ID | Required | Reference to microphone component |
| `speaker` | ID | Required | Reference to speaker component |
| `processor_id` | ID | - | Reference to an audio processor. **Use `esp_aec` only** when `intercom_api` runs without `i2s_audio_duplex` in front of it (the standalone dual-bus MEMS + amp setup). `esp_afe` is type-compatible but its feed/fetch tasks need the fixed-cadence frames that only `i2s_audio_duplex` produces; pairing `esp_afe` with standalone `intercom_api` will silently fail to process audio. With `i2s_audio_duplex` in the chain both processors are valid. |
| `aec_reference_delay_ms` | int | 80 | AEC ring buffer pre-fill delay (10-200ms). Tune for your hardware if echo cancellation is poor. |
| `dc_offset_removal` | bool | false | Remove DC offset from mic signal |
| `ringing_timeout` | time | 0s | Auto-decline after timeout (0 = disabled) |
| `tasks_stack_in_psram` | bool | false | Place the server / tx / speaker task stacks in PSRAM (saves ~28 KB of internal heap on S3/PSRAM builds where AFE/MWW/LVGL compete for it). Requires PSRAM and `CONFIG_SPIRAM_ALLOW_STACK_EXTERNAL_MEMORY: "y"`. Leave default `false` on plain ESP32 boards without PSRAM, otherwise the tasks fail to start and the component is disabled. Board YAMLs should enable it only after validating their PSRAM stack policy. |
| `frame_buffers_in_psram` | bool | false | Place the working frame buffers (`aec_mic`, `aec_ref`, `aec_out`, ~3 KB total) in PSRAM. These buffers stage every audio frame fed to whichever processor (`esp_aec` or `esp_afe`) is wired via `processor_id`. Default `false` keeps them in internal RAM, saving ~20 us/frame on Core 0 (each buffer is written and read every AEC frame). Set `true` to save 3 KB of internal RAM at the cost of Core 0 PSRAM traffic; on systems without PSRAM the allocator falls back to internal automatically. The full-experience and intercom-only YAMLs ship with this `true` to replicate the historical behaviour. |

## Product mode

PBX-lite is the only product mode. Phonebook / contacts / destination / caller entities are always exposed; the "single doorbell" deployment is just a phonebook with one entry. Omit the `mode:` key for the default PBX-lite behaviour.

The only configuration that suppresses signaling and phonebook entities is `mode: raw_udp` (audio-only UDP, used for go2rtc / two-room direct links).

HA `intercom_native` bridges through `BridgeSession` queues, with per-leg transport selection.

## State Machine

```
                    ┌──────────────────────────┐
                    │                          │
                    ▼                          │
              ┌──────────┐                     │
              │   IDLE   │◄────────────────────┤
              └────┬─────┘                     │
                   │                           │
        ┌──────────┼──────────┐                │
        │          │          │                │
   START (ring)  START     start()             │
        │      (auto_ans)     │                │
        ▼          │          ▼                │
   ┌─────────┐     │    ┌──────────┐           │
   │ RINGING │     │    │ OUTGOING │           │
   └────┬────┘     │    └────┬─────┘           │
        │          │         │                 │
  answer_call()    │    ANSWER received        │
        │          │         │                 │
        ▼          ▼         ▼                 │
      ┌────────────────────────┐               │
      │       STREAMING        │── stop() ─────┘
      │  (bidirectional audio) │
      └────────────────────────┘
```

### States

| State | Description | Triggers |
|-------|-------------|----------|
| `Idle` | No active call | Initial, after hangup |
| `Ringing` | Incoming call waiting | START message with ring flag |
| `Outgoing` | Outgoing call waiting | `start()` called |
| `Streaming` | Active audio call | Answer or auto-answer |

## Platform Entities

### Switch Platform

```yaml
switch:
  - platform: intercom_api
    intercom_api_id: intercom
    auto_answer:
      id: auto_answer_switch
      name: "Auto Answer"
      restore_mode: RESTORE_DEFAULT_OFF
    aec:
      id: aec_switch
      name: "Echo Cancellation"
      restore_mode: RESTORE_DEFAULT_ON
    active:
      id: intercom_active_switch
      name: "Intercom Enabled"
      restore_mode: RESTORE_DEFAULT_ON
```

| Switch sub-key | Effect |
|----------------|--------|
| `auto_answer` | Auto-accept any incoming call. |
| `aec` | Toggle the linked audio processor on or off at runtime. |
| `active` | Master enable for the intercom. When off, the TCP server stops accepting connections and outgoing calls are blocked. Useful as a privacy switch or for night mode. |

### Number Platform

```yaml
number:
  - platform: intercom_api
    intercom_api_id: intercom
    speaker_volume:
      id: master_volume
      name: "Master Volume"
      # Range: 0-100%
    mic_gain:
      id: mic_gain
      name: "Mic Gain"
      # Range: -20 to +20 dB
```

> **Note**: When `i2s_audio_duplex` is also present, `i2s_audio_duplex` owns the `mic_gain` and Master Volume number entities with full dB-scale control and persistence. In the standard packages the user-facing volume id is `master_volume`; the `speaker_volume` key is only the component's technical config option. In that case, `intercom_api`'s number entities serve as **fallback only** for non-duplex setups. A `FINAL_VALIDATE_SCHEMA` at compile time prevents conflicts by detecting when both components try to own the same functionality (dual AEC, dual DC offset).

## Actions

Use these actions in automations or lambdas:

For Home Assistant-facing controls in shared/public packages, prefer standard
template buttons that call the intercom actions. This avoids making the basic
YAML path depend on ESPHome external-component platform cache state. The native
`button: platform: intercom_api` platform is still available for custom local
YAMLs that explicitly want it.

```yaml
button:
  - platform: template
    name: "Call"
    on_press:
      - intercom_api.call_toggle: intercom

  - platform: template
    name: "Next Contact"
    on_press:
      - intercom_api.next_contact: intercom

  - platform: template
    name: "Previous Contact"
    on_press:
      - intercom_api.prev_contact: intercom

  - platform: template
    name: "Decline"
    on_press:
      - intercom_api.decline_call: intercom
```

### intercom_api.start

Start an outgoing call to the currently selected contact.

```yaml
binary_sensor:
  - platform: gpio
    pin: GPIO0
    on_press:
      - intercom_api.start:
          id: intercom
```

### intercom_api.stop

Hangup the current call.

```yaml
- intercom_api.stop:
    id: intercom
```

### intercom_api.answer_call

Answer an incoming call (when auto_answer is OFF).

```yaml
- intercom_api.answer_call:
    id: intercom
```

### intercom_api.decline_call

Decline an incoming call.

```yaml
- intercom_api.decline_call:
    id: intercom
```

### intercom_api.call_toggle

Smart action: idle→call, ringing→answer, streaming→hangup.

```yaml
- intercom_api.call_toggle:
    id: intercom
```

### intercom_api.next_contact / prev_contact

Navigate the phonebook.

```yaml
- intercom_api.next_contact:
    id: intercom

- intercom_api.prev_contact:
    id: intercom
```

### intercom_api.set_contact / call_contact

Select a specific contact by name, or select-and-call atomically. `call_contact`
is the safer building block for GPIO-triggered direct calls: if the contact name
is not in the phonebook it fails without falling through to another destination.

```yaml
- intercom_api.set_contact:
    id: intercom
    contact: "Waveshare S3 Audio"

- intercom_api.call_contact:
    id: intercom
    contact: "Waveshare S3 Audio"
```

> **Important: Name matching is exact (case-sensitive).** The `contact` value must match the device name exactly as it appears in the contacts list. The contacts list is populated from the `name:` substitution in each device's YAML. For example, if the target device has `substitutions: name: waveshare-s3-audio`, then the contact name in Home Assistant will be `Waveshare S3 Audio` (HA converts hyphens to spaces and capitalizes words). Always verify the exact name in the `sensor.{name}_destination` entity.

#### Example: Multi-Button Intercom (Apartment Doorbell)

Each GPIO button calls a different room, like a condominium intercom panel:

```yaml
binary_sensor:
  # Button 1: Call Kitchen
  - platform: gpio
    pin:
      number: GPIO4
      mode: INPUT_PULLUP
      inverted: true
    on_press:
      - intercom_api.call_contact:
          id: intercom
          contact: "Kitchen Intercom"

  # Button 2: Call Living Room
  - platform: gpio
    pin:
      number: GPIO5
      mode: INPUT_PULLUP
      inverted: true
    on_press:
      - intercom_api.call_contact:
          id: intercom
          contact: "Living Room Intercom"

  # Button 3: Call Bedroom
  - platform: gpio
    pin:
      number: GPIO6
      mode: INPUT_PULLUP
      inverted: true
    on_press:
      - intercom_api.call_contact:
          id: intercom
          contact: "Bedroom Intercom"
```

Each device name must match the target device's YAML `substitutions.name` exactly as displayed in HA. For example:
- YAML: `name: kitchen-intercom` → HA device name: `Kitchen Intercom` → contact: `"Kitchen Intercom"`
- YAML: `name: waveshare-s3-audio` → HA device name: `Waveshare S3 Audio` → contact: `"Waveshare S3 Audio"`

If `set_contact` fails to find the name, it fires `on_call_failed` with `"Contact not found: <name>"`.

### intercom_api.set_contacts / add_contact / remove_contact / flush_contacts

Replace, append, drop or wipe the local phonebook from YAML. These are native
ESPHome YAML actions, not entries in the HA-callable API actions list used by
the standard packages. Normal runtime sync flows through the unified
`sensor.intercom_phonebook` subscription; manual mutation is for local scripts,
diagnostics, and controlled utility YAML.

Protocol-aware rows are the current contract:

```text
Name|tcp|ip|tcp_port
Name|udp|ip|udp_audio_port|udp_control_port
Name|ha|ip|tcp_port|udp_audio_port|udp_control_port
```

Short manual rows without an explicit protocol are accepted and interpreted
according to the local transport.

The standard phonebook package calls `intercom_api.update_contacts`, which applies the subscribed `sensor.intercom_phonebook` rows internally. Custom YAML can still call these actions directly when it intentionally owns local mutation. Dedup is by name only; endpoint conflict = last writer wins (see `phonebook.h`).

Manual boot phonebook for a fixed local setup:

```yaml
esphome:
  on_boot:
    priority: -100
    then:
      - intercom_api.flush_contacts:
          id: intercom
      - intercom_api.set_contacts:
          id: intercom
          contacts_csv: >-
            Home|ha|192.168.1.10|6054|6054|6055,
            Kitchen Intercom|tcp|192.168.1.21|6054,
            Garage Intercom|udp|192.168.1.22|6054|6055
      - intercom_api.set_ha_peer_name:
          id: intercom
          name: "Home"
```

If `packages/intercom/phonebook_subscribe.yaml` is also included, HA can merge
newer rows after boot. Omit that package for a fully static local phonebook.

### ESP-side mDNS discovery

For HA-managed installs, keep using `packages/intercom/phonebook_subscribe.yaml`:
ESP devices publish `intercom_endpoint` over the native ESPHome API, and HA
publishes the authoritative `sensor.intercom_phonebook`.

Do not enable ESP-side `announce` / `discovery.mdns` in those HA-managed builds.
They are for installs without a central HA phonebook; combining them with HA API
traffic and intercom sockets adds avoidable lwIP/socket load, especially while
HA is disconnecting or restarting.

For direct ESP-only installs, enable the opt-in discovery package:

```yaml
packages:
  mdns_discovery: !include packages/intercom/mdns_discovery.yaml
```

or inline:

```yaml
intercom_api:
  announce: true
  discovery:
    mdns: true
```

The discovery task is intentionally narrow. It queries only intercom services,
reads TXT `endpoint=<Name|protocol|ip|ports>`, and merges matching endpoint
rows into the normal phonebook:

```text
Name|tcp|ip|port
Name|udp|ip|audio_port|control_port
```

Advanced form:

```yaml
intercom_api:
  announce: true
  discovery:
    mdns:
      startup_scan: true
      interval: 60s
      query_timeout: 1000ms
      max_results: 8
      protocols: [tcp]  # omit to scan the local transport only
```

### intercom_api.set_ha_peer_name

Set the phonebook entry name that represents HA. Standard packages derive it from the `Name|ha|...` row in `sensor.intercom_phonebook`, so manual calls are normally unnecessary. Default `ha_peer_name_` is empty; calling `ha_pbx` routing without it logs an ERROR.

```yaml
action: esphome.<slug>_set_ha_peer_name
data:
  name: "Beach House"   # whatever hass.config.location_name is
```

## Conditions

Use in `if:` blocks:

```yaml
- if:
    condition:
      intercom_api.is_idle:
        id: intercom
    then:
      - logger.log: "Intercom is idle"
```

| Condition | True when |
|-----------|-----------|
| `is_idle` | State is Idle |
| `is_ringing` | State is Ringing (incoming call, before answer) |
| `is_calling` | State is Outgoing (waiting for the remote end to pick up) |
| `is_streaming` | Audio is actively streaming both ways |
| `is_in_call` | State is Streaming |
| `is_incoming` | Pending incoming call (Ringing) |
| `destination_is` | Currently selected contact name matches the `destination:` argument |

## Triggers run on the main loop

All `on_ringing`, `on_outgoing_call`, `on_dest_ringing`, `on_destination_changed`, `on_streaming`, `on_idle`, `on_hangup` and `on_call_failed` triggers fire from the ESPHome main loop, not from a transport task. Internally the component calls `Component::defer()` to hand the trigger off to the scheduler, so a lambda or action sequence attached to a trigger can safely touch any ESPHome entity, including LVGL widgets, text sensors, switches and media players, without the main-loop-only constraints that FreeRTOS tasks would hit.

Concretely this means you can write:

```yaml
on_ringing:
  - lvgl.label.update:
      id: status_label
      text: "Incoming call"
  - light.turn_on: status_led
```

and the trigger will run in a context where those calls are valid, even though the call state transition itself was detected on the network task.

## Lambda API

Access component methods from lambdas:

```cpp
// Get current state
const char* state = id(intercom).get_state_str();
// Returns: "Idle", "Ringing", "Outgoing", "Streaming", etc.

// Get current destination
std::string dest = id(intercom).get_current_destination();

// Get caller name (during incoming call)
std::string caller = id(intercom).get_caller();

// Get contacts as CSV
std::string contacts = id(intercom).get_contacts_csv();

// Control methods
id(intercom).start();
id(intercom).stop();
id(intercom).answer_call();
id(intercom).set_volume(0.8f);        // 0.0 - 1.0
id(intercom).set_mic_gain_db(6.0f);   // -20 to +20
id(intercom).set_aec_enabled(true);
id(intercom).set_auto_answer(false);
```

## Wire protocol

### Header (3 bytes, little-endian)

```
┌──────────────┬───────────────────────┬───────────────────────────┐
│ type (u8)    │ length (u16 LE)       │ payload (length bytes)    │
└──────────────┴───────────────────────┴───────────────────────────┘
```

### Body shape (PBX-lite control messages)

```
call_id_len (u8) | call_id (UTF-8) | per-type tail
```

PING/PONG carry `call_id_len = 0` (socket-level keepalive).

### Message types

| Type | Value | Body tail | Description |
|------|-------|-----------|-------------|
| AUDIO   | 0x01 | raw L16 PCM | Audio payload (no call_id prefix). |
| START   | 0x02 | caller_route, caller_name, dest_route, dest_name (each lp-string) | Initiate call. |
| HANGUP  | 0x03 | (just the call_id prefix) | Established-call BYE. |
| PING    | 0x04 | empty | Keepalive. |
| PONG    | 0x05 | empty | Keepalive response. |
| ERROR   | 0x06 | error_code (u8), detail (lp-string) | Technical fault. |
| RING    | 0x07 | (just the call_id prefix) | Provisional: dest is presenting. |
| ANSWER  | 0x08 | (just the call_id prefix) | Final: dest accepted. |
| DECLINE | 0x09 | reason (lp-string, may be empty) | Setup-phase reject; empty reason = silent remote_hangup. |

`lp-string` = `len(u8) | bytes[len]` UTF-8.

### Audio Format

- Sample rate: 16000 Hz
- Bit depth: 16-bit signed PCM
- Channels: Mono
- Chunk size: 1024 bytes (512 samples = 32 ms)

## Auto-created Sensors

The component automatically creates these text sensors under PBX-lite (i.e. unless `mode: raw_udp`):

| Sensor | Entity ID | Description |
|--------|-----------|-------------|
| Intercom State | `sensor.{name}_intercom_state` | Idle, Ringing, Outgoing, Streaming |
| Destination | `sensor.{name}_destination` | Currently selected contact (phonebook is always exposed). |
| Caller | `sensor.{name}_caller` | Incoming caller name. |
| Contacts | `sensor.{name}_contacts` | Contact count. |

`mode: raw_udp` suppresses everything except `intercom_state` (no signaling, no phonebook).

## Entity State Publishing

Entities publish their state when the ESPHome API connects. Use `on_client_connected` to ensure values are visible in Home Assistant:

```yaml
api:
  on_client_connected:
    - lambda: 'id(intercom).publish_entity_states();'
```

## Hardware Requirements

- **ESP32-S3** or **ESP32-P4** with PSRAM (required for AEC)
- I2S microphone component
- I2S speaker component
- ESP-IDF framework

## Memory Usage

| Component | Approximate RAM |
|-----------|-----------------|
| mic_buffer_ | 4 KB |
| mic_converted_ | 1 KB, only when `dc_offset_removal` or intercom-owned mic gain is active |
| speaker_buffer_ | 8 KB, only when `processor_id` is set on `intercom_api` |
| spk_ref_buffer_ | Configured AEC reference delay, only when `processor_id` is set |
| FreeRTOS tasks | `tx_task` 12 KB always; `speaker_task` 8 KB only with `processor_id` |
| AEC/AFE processor | Owned by the selected processor component, not by transport-only intercom |

## FreeRTOS Task Configuration

| Task | Core | Priority | Stack | Notes |
|------|------|----------|-------|-------|
| server_task | 1 | 5 | 8192 | Always created. Handles transport RX/control, call FSM handoff and YAML callback defers. |
| tx_task | 0 | 5 | 12288 | Always created. Drains `mic_buffer_` and keeps network sends out of the microphone callback path. |
| speaker_task | 0 | 4 | 8192 | **Only created when `processor_id` is set on `intercom_api`**. Network→speaker, processor ref. |

> **Task elimination**: When `intercom_api` does NOT have its own `processor_id` (the standard case, where audio processing is handled by `i2s_audio_duplex`), `speaker_task`, `speaker_buffer_`, `spk_audio_chunk_`, `spk_ref_scaled_` and the speaker-stop semaphore are not created. `tx_task` stays alive in both modes because it decouples microphone callbacks from TCP/UDP sends. Incoming audio is played directly via `speaker_->play()`.

## Logging

The component splits its log output across four sub-tags so users can mute pieces without losing the rest. Each tag is namespaced under `intercom_api.*` and works as a normal `logger.logs:` entry.

| Tag | Source file | Function |
|---|---|---|
| `intercom_api` | `intercom_api.cpp` | Setup, `dump_config`, lifecycle (`Intercom API ready on …`, `Remote endpoint updated`), call_state timeouts |
| `intercom_api.fsm` | `intercom_fsm.cpp` | PBX-lite FSM transitions, call lifecycle (`calling…`, `answering call`, `incoming call`, `remote hung up`), collision/glare/busy DECLINEs |
| `intercom_api.audio` | `intercom_audio.cpp` | Mic source / speaker sink wiring, AEC reference handoff |
| `intercom_api.settings` | `intercom_settings.cpp` | Persisted-settings load/save (volume, mic gain, AEC mode, auto-answer) |
| `intercom_api.tcp` | `tcp_transport.cpp` | TCP server lifecycle, framed RX/TX, connection adoption |
| `intercom_api.udp` | `udp_transport.cpp` | UDP audio / control sockets, peer resolution |

**Default log levels** (consistent with the project-wide [Logging contract](../../../README.md#logging)):

- `ERROR` - config invalid (HA-PBX without phonebook entry, raw_udp on TCP)
- `WARN` - collision / glare / busy DECLINE, malformed frame, send error (rate-limited 1st + every 100th)
- `INFO` - call lifecycle visible to the user, "ready on port", "remote endpoint updated", FSM call-state transitions
- `DEBUG` - protocol idempotent re-acks (`START retransmit`, `ANSWER repeat`), `PONG no-op`, terminal-DECLINE replay, transient state-stack details

**Mute one sub-tag without disabling the rest**

```yaml
logger:
  level: DEBUG
  logs:
    intercom_api.audio: INFO   # quiet audio plumbing chatter
    intercom_api.settings: INFO
    intercom_api.fsm: DEBUG    # full FSM trace (default at DEBUG)
```

**WARN throttling on hot paths**

`udp_transport.cpp` and `tcp_transport.cpp` rate-limit repetitive WARN messages (audio send errors, sendto failures) to the 1st-5th occurrence and every 100th after that. Each message includes a `[n=…]` suffix so you can tell whether you're seeing an isolated fault or a sustained storm without reading every line.

## Troubleshooting

### "Connection refused" on port 6054

- Verify no other service is bound to port 6054 on the device.
- Check that the `active` switch (if you exposed it) is on.
- Check that the device is on the network and reachable from Home Assistant.

The socket pool is sized automatically: `intercom_api` calls `socket.consume_sockets(N)` at validation time, so ESPHome bumps `CONFIG_LWIP_MAX_SOCKETS` to fit the intercom transport plus the rest of the device's network components. Full-experience packages also reserve extra headroom for overlapping HA API/logging, media HTTP, TTS HTTP and intercom sockets; you should not need to set `CONFIG_LWIP_MAX_SOCKETS` by hand.

### Audio glitches

- Ensure PSRAM is enabled for AEC
- Check WiFi signal strength
- Reduce log level to WARN

### AEC not working

- Verify `processor_id` is linked to `esp_aec` when `intercom_api` owns the mic/speaker path. Use `esp_afe` only behind `i2s_audio_duplex`.
- Check the audio processor component is configured
- Ensure AEC switch is ON

### State stuck in "Ringing"

- Check `ringing_timeout` is set
- Verify `auto_answer` setting
- Look for connection errors in logs

## Example: Button Control

```yaml
binary_sensor:
  - platform: gpio
    pin:
      number: GPIO0
      mode: INPUT_PULLUP
      inverted: true
    on_multi_click:
      # Single click: call/answer/hangup
      - timing:
          - ON for 50ms to 500ms
          - OFF for at least 400ms
        then:
          - intercom_api.call_toggle:
              id: intercom
      # Double click: next contact
      - timing:
          - ON for 50ms to 500ms
          - OFF for 50ms to 400ms
          - ON for 50ms to 500ms
        then:
          - intercom_api.next_contact:
              id: intercom
```

## License

MIT License
