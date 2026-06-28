# intercom_api

`intercom_api` is the ESP SIP phone component. It owns SIP signaling, SDP
offer/answer, RTP media, phonebook selection and call snapshots. It does not
own echo cancellation.

It can run in two supported shapes:

- **Standalone native ESPHome audio**: bind `intercom_api` directly to standard
  ESPHome `microphone` and/or `speaker` components. This is the right path for
  devices whose hardware already returns processed audio, for example XMOS
  front-ends, DSP codecs, or simple tests with native I2S mic/speaker.
- **Audio stack facade**: bind it to the microphone/speaker exposed by
  `esp_audio_stack`. This is the maintained path when software AEC, AFE, codec
  routing, TDM reference, dual-bus sync, media player, Voice Assistant or Micro
  Wake Word share the same audio backend.

The component negotiates explicit PCM per direction. SIP/SDP selects one
dialog `ptime` shared by both directions, while TX and RX sample rate/PCM
format can still differ. Maintained YAMLs publish their real capabilities; the
internal no-audio placeholder is `16000:s16le:1:16` and is not used to repair
missing endpoint formats. AFE/AEC-backed branches remain
16 kHz/s16/mono because Espressif esp-sr exposes that surface.

## Audio Capabilities

The endpoint capability is inferred from the YAML wiring:

| YAML wiring | Published endpoint mode | Runtime behavior |
|---|---|---|
| `microphone`/`microphone_source` + `speaker` | `full_duplex` | Sends mic audio and plays remote audio. |
| `microphone`/`microphone_source` only | `mic_only` | Sends mic audio; incoming audio is ignored. |
| `speaker` only | `speaker_only` | Plays incoming audio; no mic TX task is created. |
| neither mic nor speaker | `control_only` | Signaling/phonebook only. |

The endpoint sensor publishes a SIP phone row. SIP is implicit; the final token
selects signaling transport:

```text
Name|192.168.1.40|5060|40000|sip_tcp
Name|192.168.1.41|5060|40000|sip_udp
```

It may also append per-direction audio capabilities:

```text
Name|192.168.1.40|5060|40000|full_duplex|16000:s16le:1:16|48000:s16le:1:10;16000:s16le:1:16|sip_tcp
Name|192.168.1.41|5060|40000|speaker_only|16000:s16le:1:16|16000:s16le:1:16|sip_udp
```

Home Assistant consumes these fields for routing/card display, format
negotiation and avoiding audio directions that cannot exist.

## Compile-Time Shape

`intercom_api` now defines its own local compile flags from YAML:

- `USE_INTERCOM_API_MIC` is emitted only when the component has
  `microphone:` or `microphone_source:`.
- `USE_INTERCOM_API_SPEAKER` is emitted only when the component has `speaker:`.

This keeps the intercom TX path out of speaker-only builds and keeps the
intercom RX-to-speaker code out of mic-only builds, even if some other ESPHome
component in the same firmware uses a microphone or speaker.

The only always-present pieces are the finite-state machine, phonebook,
transport listener/client and control signaling.

## Minimal Full-Duplex Example

```yaml
microphone:
  - platform: i2s_audio
    id: native_mic
    adc_type: external
    i2s_audio_id: rx_i2s
    i2s_din_pin: GPIO11
    channel: right
    sample_rate: 48000
    bits_per_sample: 32bit

speaker:
  - platform: i2s_audio
    id: native_speaker
    dac_type: external
    i2s_audio_id: tx_i2s
    i2s_dout_pin: GPIO14
    channel: mono
    sample_rate: 48000
    bits_per_sample: 16bit

intercom_api:
  id: intercom
  microphone_source:
    microphone: native_mic
    bits_per_sample: 16
    channels: [0]
  speaker: native_speaker
  audio:
    tx:
      sample_rate: 48000
      pcm_format: s16le
      channels: 1
      frame_ms: 20
    rx:
      sample_rate: 48000
      pcm_format: s16le
      channels: 1
      frame_ms: 20
```

Use `microphone:` directly instead of `microphone_source:` only when the
referenced microphone already publishes the configured `audio.tx` format.
AFE/AEC-backed branches should publish the processor output actually delivered
to `intercom_api`; maintained profiles use `16000:s16le:1:16`. Native ESPHome
microphone/speaker branches can advertise their real format.

## Mic-Only And Speaker-Only

Mic-only:

```yaml
intercom_api:
  id: intercom
  microphone: processed_mic
```

Speaker-only:

```yaml
intercom_api:
  id: intercom
  speaker: local_speaker
```

These are first-class modes, not degraded modes. They are useful for split
installations, paging speakers, monitor/listen devices, and hardware that
already exposes only one audio direction.

## Software AEC / AFE

Standalone `intercom_api` AEC has been removed.

Removed options:

| Removed | Replacement |
|---|---|
| `intercom_api.processor_id` | Configure `processor_id` on `esp_audio_stack`. |
| `intercom_api.aec_reference_delay_ms` | Configure AEC/AFE reference buffering on `esp_audio_stack`. |
| `switch: platform: intercom_api, aec:` | Use `esp_audio_stack`, `esp_aec` or `esp_afe` controls. |

Reason: `intercom_api` is not the right owner for I2S timing, speaker reference
capture or AFE cadence. When software processing is required, `esp_audio_stack`
owns the audio graph and exposes normal ESPHome microphone/speaker interfaces
back to `intercom_api`.

## Transport

SIP is the only call-control protocol. `protocol` selects the SIP signaling
transport exposed by this phone.

SIP/UDP signaling:

```yaml
intercom_api:
  id: intercom
  protocol: udp
  sip_port: 5060
  rtp_port: 40000
```

SIP/TCP signaling:

```yaml
intercom_api:
  id: intercom
  protocol: tcp
  sip_port: 5060
  rtp_port: 40000
```

RTP media remains UDP for both signaling transports.

## Phonebook And HA Routing

Contacts can be declared directly in `intercom_api` for installs that do not
want HA to be the only phonebook source:

```yaml
intercom_api:
  id: intercom
  protocol: udp
  phonebook:
    - name: Casa di nonna
      ip: 192.168.1.44
      port: 5060
      rtp_port: 40000
    - name: Cancello
```

`name` is required. `ip`, `port`, `rtp_port`, and `sip_transport` are optional.
When `sip_transport` is omitted, the contact uses the component SIP signaling
transport (`protocol: udp` or `protocol: tcp` on `intercom_api`). A name-only
contact is a logical target that can later be upgraded by the HA phonebook or
routed through HA.

Each ESP publishes an `intercom_endpoint` text sensor. Home Assistant builds the
central `sensor.intercom_phonebook` from those endpoints and adds itself as the
HA peer.

Inbound ESP calls are not rejected just because the caller is missing from the
callee phonebook. The phonebook is the outbound SIP dial plan; inbound INVITE
carries caller and destination identity. When HA is in the path, it resolves
inbound SIP callers by:

1. socket source IP when it matches the endpoint host;
2. SIP From URI user/name;
3. caller friendly name from SIP headers.

That keeps routed subnet/NAT/VPN installs working when HA sees a socket source
address that differs from the ESP endpoint IP, while preserving the endpoint IP
as the address other peers should dial.

`advertise_host` in the HA integration is an advanced override for HA
multihomed/LXC/NAT installs where Home Assistant would otherwise publish an
address ESP devices cannot reach. It is not required just because ESPs and HA
are on different routed subnets.

ESP devices never register to the optional HA/provider trunk. If HA has a trunk,
HA maps external numbers or inbound DTMF route digits to local phonebook
targets and then calls ESP devices as normal SIP phones.

## SIP Automation Hooks

SIP-aware hooks expose the call identity directly:

```yaml
intercom_api:
  id: intercom
  on_incoming_call:
    then:
      - logger.log:
          format: "incoming call_id=%s caller=%s callee=%s uri=%s"
          args: [call_id.c_str(), caller.c_str(), callee.c_str(), uri.c_str()]
  on_outgoing_call:
    then:
      - logger.log:
          format: "outgoing call_id=%s caller=%s callee=%s uri=%s"
          args: [call_id.c_str(), caller.c_str(), callee.c_str(), uri.c_str()]
  on_bridge_request:
    then:
      - logger.log:
          format: "bridge request call_id=%s caller=%s callee=%s uri=%s"
          args: [call_id.c_str(), caller.c_str(), callee.c_str(), uri.c_str()]
```

`on_bridge_request` fires for ESP-originated routes that explicitly target the
HA peer. HA-side dial-plan decisions are exposed as Home Assistant bus events
and services by `intercom_native`.

## Auto Entities

`auto_entities: true` can create the common HA entities for minimal YAMLs:

- `auto_answer`
- `dnd`
- `master_volume`, only when a speaker is configured
- `mic_gain`, only when a mic is configured

Existing maintained YAMLs generally declare entities through packages for
stable names and UI layout.

## Resource Notes

- Mic ring buffer and TX chunk are allocated only when a mic path is configured.
- No speaker ring, speaker task or AEC reference buffer exists in
  `intercom_api`.
- Incoming audio is written directly to the configured ESPHome speaker.
- `buffers_in_psram: true` affects only intercom-owned staging buffers.
- `task_stacks_in_psram: true` applies to the intercom TX task and transport
  tasks where supported by the transport.

## Experimental Native Dual-Bus Test

See:

```text
yamls/intercom-only/dual-bus/generic-s3-intercom.yaml
```

That profile intentionally avoids `esp_audio_stack` and binds `intercom_api` to
native ESPHome `i2s_audio` microphone/speaker components. It is for regression
testing standalone full-duplex, mic-only and speaker-only modes.
