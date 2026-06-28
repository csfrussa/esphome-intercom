# Breaking changes

## SIP consolidation audit

The active branch is intentionally SIP-first and breaking:

- ESP `intercom_api` is a SIP phone. `protocol: udp|tcp` means SIP signaling
  transport only.
- HA `intercom_native` is a SIP softphone, dial-plan authority, SIP/RTP bridge
  and optional SIP trunk endpoint.
- The retired proprietary intercom protocol is not a compatibility layer.
- ESP discovery/scanning between standalone peers is not a functional
  primitive. Use explicit SIP URIs, local `phonebook` entries or the HA roster.
- Cards mirror their owner. HA cards mirror HA softphone state. ESP cards mirror
  ESPHome entities and controls.
- Optional HA trunk support is disabled by default. If disabled, no provider
  registration, external route or DTMF routing path starts.
- Inbound trunk routing uses standard SIP plus RFC2833/telephone-event DTMF to
  select local phonebook targets.

## 2026.7.0-dev: source-based full media path and native 48 kHz intercom presets

`2026.7.0-dev` is a prerelease. It is intended for users who want to test the
new full-experience audio/media path and Sendspin/Music Assistant integration
before the next stable release.

Maintained full-experience YAMLs now use ESPHome's `speaker_source` media
player path. Media, announcements, timer sounds, local audio files and optional
Sendspin streams enter one media player and then flow through the existing
mixer. Intercom still has its own mixer source and keeps priority through the
existing call-state logic. Custom YAMLs copied from older full-experience files
should migrate away from `platform: speaker` media-player blocks and local
`files:` entries toward `media_source` plus `media_player.play_media` with
`audio-file://...` URLs.

Maintained non-native full-experience YAMLs now use the generic `runtime_fsm`
component for runtime state arbitration. Custom full-experience YAMLs copied
from older releases should migrate away from `update_status`,
`timer_ringing`, local VA pending flags and callback-local LED/display/ducking
decisions. The new pattern is: callbacks send `runtime_fsm.event`, the reducer
sets activities, and policies drive LED/display/audio/timer outputs from one
committed snapshot.

Voice Assistant response state is now tied to TTS/media-player announcement
lifecycle callbacks through `runtime_fsm`. Slow local TTS backends can exceed
ESPHome's historical 2-second playback-start watchdog, especially XTTS running
locally. This prerelease temporarily ships a project-local `voice_assistant`
fork that exposes `tts_playback_start_timeout`; maintained full profiles set
it to `10s`. Custom full profiles using the maintained `core_cpp` package get
that setting automatically.

Sendspin is included in maintained full-experience profiles as a Music Assistant
media source. It is not required for normal HA media, TTS, timer sounds,
ringtones, Voice Assistant or intercom calls. WS3, Spotpear and P4 grouped
playback were validated with the shared `speaker_source` path and the
hardware-clocked ESP audio stack timing model.

Native ESPHome intercom-only presets now use 48 kHz PCM where the actual
native I2S microphone or speaker path supports it. TCP native profiles use
20 ms frames. UDP native profiles use 10 ms frames so 48 kHz/s16/mono remains
below the default 1200-byte UDP datagram limit. AFE/AEC-backed profiles keep
16 kHz TX because that is the Espressif AFE/AEC output format; this is a local
branch constraint, not a global intercom transport limit.

UDP custom formats are validated against `udp_max_payload` at build time and by
Home Assistant when publishing the phonebook. The default is intentionally
conservative at 1200 bytes per audio frame. If you deliberately run larger LAN
datagrams, set the same larger `udp_max_payload` in the ESPHome YAML and the
Intercom Native integration options; otherwise use TCP for high-rate, stereo or
32-bit PCM.

ESP endpoint publication now waits for a valid IPv4 address from ESPHome's
network API and republishes on Wi-Fi or Ethernet IP events. Endpoint sensors
should no longer publish incomplete rows before the board has a usable address.

Inbound SIP calls are now treated consistently across SIP/TCP and SIP/UDP: the
callee does not require the caller to be present in its local phonebook. The
phonebook is the outbound dial plan; inbound INVITE already carries caller and
destination identity. This matters for HA SIP bridges, routed subnets, VPN
callers and direct SIP clients. If a custom automation previously assumed that
an unknown inbound caller would be rejected by missing phonebook state, replace
that policy with DND, routing rules or an explicit bridge/service check.

Call-ended UI now preserves the real incoming caller through the terminal
callback before clearing the caller sensor. Device displays and the HA card
should show the peer from the active call, not whichever phonebook contact is
currently selected for the next outbound call.

The HA softphone card exposes Auto Answer, DND and browser ringtone behind an
idle-only Options panel. Browser ringtone is a per-browser localStorage
preference; HA softphone DND is stored in Home Assistant state.

Hybrid and HA-softphone Lovelace modes have intentionally separate semantics.
`hybrid` mirrors one ESP endpoint and exposes controls from that ESP's
perspective. `ha_softphone` represents Home Assistant itself. If you previously
depended on one card mixing both models, split it into one hybrid ESP card and
one optional HA softphone card.

Browser audio setup now waits for the HA SIP softphone control response and
uses the negotiated TX/RX formats from that response before creating the AudioWorklets.
Custom frontend code that starts microphone/playback worklets before the
control reply should be updated; otherwise ESP -> HA answered calls can use the
wrong frame size when one direction negotiates 48 kHz.

ESP caller playback now applies the negotiated RX speaker format before the
call is activated and re-applies it when the SIP answer confirms the effective
direction formats. Custom ESP integrations that bypass the maintained
`intercom_api` speaker setup must do the same before feeding high-rate PCM to
the local speaker path.

The Lovelace frontend derives ringtone/worklet cache keys from the loaded card
module version instead of a manually bumped audio asset constant. During custom
frontend testing, clear the browser cache or change the card resource URL when
serving files outside the packaged release flow.

Older upgrade notes are kept in their original GitHub release pages. This file
tracks only the current upgrade delta from `2026.6.3` to `2026.7.0-dev`.
