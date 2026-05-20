# Release notes 2026.5.0

## From PBX-like to PBX-lite

This release changes the shape of ESPHome Intercom.

It is no longer only an ESPHome audio experiment that can place a full-duplex
call. It now has a small but real PBX-lite model: every ESP is treated as an
independent extension, Home Assistant can join as another extension, the
browser card can act as a softphone, and HA can bridge calls when the two sides
cannot talk directly.

The important part: the simple doorbell and intercom use case is still simple.
If you want one ESP button that rings Home Assistant, start from
`yamls/intercom-only/`, adapt the board pins, install `intercom_native`, add the
ESP through the ESPHome integration, and call the HA destination. PBX-lite is
the model underneath, not a requirement to design a phone system.

## What PBX-lite means here

- ESPs own their own call state: idle, ringing, outgoing, in call, declined,
  busy and ended.
- Devices dial by phonebook name, not by hardcoded IP glue.
- Same-transport ESPs can call each other directly.
- Home Assistant can act as a peer, a phonebook publisher and a bridge.
- TCP and UDP devices can live in the same setup, with HA bridging incompatible
  legs.
- The Lovelace card can ring, answer, decline and hang up as a browser
  softphone.
- Call reasons and errors are propagated through ESP sensors, HA state and the
  card UI.

This is why the release moves from PBX-like behavior to PBX-lite architecture.
The protocol is still intentionally small, but the pieces that matter for a
home intercom are now there.

## What this unlocks

You can now build:

- a one-button full-duplex doorbell that rings Home Assistant;
- room-to-room ESP intercoms;
- direct ESP-to-ESP calls on the same transport;
- HA-bridged calls across TCP, UDP and browser audio;
- a browser or mobile dashboard softphone;
- full voice devices with intercom, media player, Piper TTS, Micro Wake Word,
  Voice Assistant, AFE/AEC and ducking on the same ESP;
- ESP-only discovery setups with optional mDNS packages when HA is not meant to
  be the phonebook authority.

The same firmware model scales from "press a GPIO and talk to Home Assistant"
to a multi-room intercom where ESPs call each other, Home Assistant bridges
mixed transports, and the dashboard can participate as a call endpoint.

## Transport model

TCP and UDP are both first-class presets.

TCP is the safer default for routed networks, VLANs, containers, filtered Wi-Fi
and setups where packet delivery matters more than the lowest possible latency.

UDP is useful on simple LANs where latency is the priority and packet loss is
under control. It is lighter, but routing and firewall mistakes show up faster
as audible glitches.

Both transports expose the same PBX-lite behavior. You do not need to choose a
different product mode, only the transport that fits the network.

`mode: raw_udp` remains available as an explicit audio-only path for go2rtc and
raw PCM links. It bypasses PBX-lite signaling on purpose.

## Home Assistant integration

`intercom_native` has been reworked into the PBX-lite hub:

- TCP listener;
- UDP socket manager;
- HA endpoint advertisement;
- phonebook publisher;
- bridge sessions;
- WebSocket state for the Lovelace card;
- validated services and clearer failure handling.

Home Assistant is no longer just a helper around ESPHome. In PBX mode it
becomes an active call participant and bridge, while direct ESP-to-ESP calls can
still avoid HA in the media path.

The HA peer name is `hass.config.location_name`, so the firmware does not need a
hardcoded "Home Assistant" label. Standard HA-managed YAMLs use HA as the
phonebook authority; ESP-side mDNS announce and discovery remain opt-in for
setups that deliberately run without HA managing the roster.

## Lovelace card and frontend

The dashboard card now follows the PBX-lite state model instead of only
mirroring a narrow direct-call path.

It can show ringing, outgoing, in-call and ended states, surface hangup reasons,
call ESP contacts by name, and participate in HA-bridged calls. The current
README media was refreshed around this model so the visible demo matches the
runtime behavior of the release.

## Audio engine rebuild

The audio side was almost completely rebuilt.

The full-experience devices now keep media playback, Piper TTS, Voice
Assistant, wake word, AFE/AEC and intercom in the same runtime instead of
treating them as isolated demos.

Main changes:

- early allocation of large stacks and buffers to preserve contiguous internal
  RAM;
- retained working buffers instead of repeated allocation during audio
  transitions;
- cleaner I2S lifecycle with hot-path enable and disable;
- native ESPHome speaker and microphone semantics wherever possible;
- improved mixer, ducking and AEC reference behavior;
- better socket accounting for overlapping HA API, logging, media HTTP, TTS
  HTTP and intercom sockets;
- media playback can be paused from Home Assistant and resumed later;
- barge-in, TTS, media pause and assistant responses were validated together
  instead of as separate happy paths.

## Board audio improvements

- Codec-backed boards keep their hardware codec path.
- Codec-less Generic S3 boards use the duplex software volume path.
- Media volume and Master Volume now behave as a cascade.
- Software attenuation is real across the range, not just mute versus full
  scale.
- AEC reference capture, rate conversion and single-bus duplex handling were
  tightened across supported boards.
- ES7210 TDM reference-slot behavior is documented and guarded with runtime
  warnings.

`esp_audio_stack` is not only for intercom. If you are building your own
ESPHome Voice Assistant and need a shared microphone and speaker I2S path,
speaker reference handling, or a cleaner audio lifecycle, the component can be
used as part of that build too.

## YAML and release packaging

The public YAMLs are intended to be downloadable presets. A user should be able
to download one YAML, adjust the hardware options that are meant to be changed,
and let ESPHome fetch packages and external components from this repository.

For release, the managed YAMLs point to `main`. Local development can still use
the path-switching script to compile against the working tree.

## Validated release targets

Validated targets for this release:

- Waveshare S3 Audio full AFE TCP and UDP;
- Spotpear Ball v2 full AFE TCP and UDP;
- Generic ESP32-S3 full AEC UDP as the codec-less reference target;
- Home Assistant OS with the native integration and Lovelace card.

Waveshare P4 Touch remains experimental in this release. The UI and audio stack
are present, but the ESP32-P4 runtime profile still deserves a separate
validation pass.

## Upgrade note

This is a breaking release for `4.x` users. Read
[`docs/BREAKING_CHANGES.md`](BREAKING_CHANGES.md) before upgrading firmware or
Home Assistant integration files.
