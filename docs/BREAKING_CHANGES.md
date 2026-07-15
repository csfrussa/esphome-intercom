# Breaking changes

## 2026.7.2-dev: Optional ESPHome entities are explicit platforms

The ESPHome `voip_stack` core no longer accepts `auto_entities`. ESPHome's
upstream contribution rules prohibit using `AUTO_LOAD` for primary entity
platforms such as `switch`, `number`, `button`, `text` and `text_sensor`.
Declare the required `platform: voip_stack` entities explicitly, or include
`packages/voip/auto_entities.yaml` in maintained full-duplex configurations.
Configurations that do not need those entities now compile without declaring
empty platform sections.

## 2026.7.1: Contract Updates

The stable `2026.7.0` migration below remains the main breaking change.
`2026.7.1` additionally makes several previously implicit
contracts explicit. Review them before upgrading an older custom YAML,
automation, SIP client or card fork.

- **ESP contact schema.** Structured ESP `static_contacts` and
  `voip_stack.add_contact` entries use `name`, optional `ip`, `port`,
  `rtp_port` and `transport`. `address`, `sip_uri`, `extension`, `number`,
  groups and media metadata are HA central-phonebook fields.
- **No inbound phonebook allowlist.** An unknown or unregistered SIP peer with
  network reachability can call HA or an ESP. Normal destination, busy/DND and
  SDP checks still apply. Enforce caller admission at a firewall, VLAN, VPN or
  SBC boundary when required.
- **Constrained in-dialog renegotiation.** ESP endpoints still return `488 Not
  Acceptable Here` for media-changing re-INVITEs. HA-owned dialogs accept
  compatible peer-initiated UPDATE/re-INVITE changes, including hold/resume,
  but cannot add/remove video, change its established codec or originate a
  renegotiation. A rejected offer leaves the previous media/dialog active.
- **DTMF route input.** The trunk digit router accepts RTP `telephone-event`
  and compatible legacy SIP INFO DTMF. It does not decode acoustic in-band tones from
  the call audio.
- **Processor bypass semantics.** When the configured audio processor is
  enabled, unavailable processed output fails closed to silence. Disabling the
  parent AEC switch explicitly publishes converted raw microphone audio on the
  same public microphone surface.
- **Software-AEC reference lifecycle.** Maintained generic single- and dual-bus
  profiles now default to `aec_reference: previous_frame`. The optional
  `ring_buffer` mode remains supported, but its reference is session-scoped: it
  is cleared when the first microphone consumer starts and when the last one
  stops, and it is not filled while no microphone consumer exists. Custom code
  must not treat old speaker samples as a reference for a later call.
- **Bounded reentrant runtime events.** Runtime-controller events/actions
  created during a drain stay queued for the next main-loop turn. Custom
  automations must not depend on an unbounded synchronous self-trigger chain.
- **Plaintext local media boundary.** ESP SIP is UDP/TCP without TLS and RTP is
  UDP without SRTP. Do not expose an ESP listener directly to an untrusted
  network.

The full change and validation summary is in
[`RELEASE_2026_7_1.md`](RELEASE_2026_7_1.md).

## 2026.7.0: ESPHome devices are SIP phones now

This release is the SIP/VoIP migration. It is not a small protocol tweak and it
is intentionally not backward compatible with the retired project-specific
call-control path.

The practical change:

- ESP `voip_stack` devices are now SIP phones.
- Home Assistant `voip_stack` is now a SIP softphone, dial-plan authority,
  SIP router/B2BUA, RTP bridge/resampler and optional SIP trunk client.
- The Home Assistant integration domain and services are `voip_stack.*`.
  Existing automations that used older development names must be updated.
- Older development installs used the Home Assistant domains `intercom_native`
  or `homeassistant_voip_stack`. The HACS repository now exposes only
  `custom_components/voip_stack`; remove old test config entries and stale
  old-domain folders before adding the current VoIP Stack integration.
- Home Assistant no longer exposes local SIP transport toggles. Configure ports
  and optional features, not listener modes.
- VoIP Stack supports Home Assistant's native Reconfigure action. You no longer
  need to remove and re-add the integration to change ports, debug mode,
  local SIP registrar support, Assist intents or optional trunk settings.
- Standard softphones can register to Home Assistant as local SIP accounts and
  become phonebook contacts.
- Local SIP account services now describe generic SIP endpoint accounts, not
  only "softphones". Real VoIP phones, ATAs, baresip, pjsua, Zoiper and Linphone
  all use the same account model.
- Home Assistant can register one provider/PBX trunk, so ESPs and the HA
  softphone can make and receive external calls without requiring Asterisk next
  to the integration.
- The shared phonebook is now a SIP dial plan: explicit `sip:name@host` /
  `name@host` routes go direct, known local endpoints can be bridged by HA, and
  external numbers can use the trunk when configured.
- Phonebook `extension` values are the internal dial-plan aliases. Static DTMF
  route maps are not a second routing table; inbound DTMF and numeric local
  calls resolve against roster extensions.
- SIP call reasons are surfaced to ESP displays, HA state and cards: busy, DND,
  declined, cancelled, timeout, media-incompatible, transport-unreachable and
  route errors are no longer hidden behind the old intercom FSM.
- The reusable ESP audio backend has moved out of this repository. If you are
  looking for `esp_audio_stack`, `esp_aec` or `esp_afe`, use the dedicated
  [`esphome-audio-stack`](https://github.com/n-IA-hane/esphome-audio-stack)
  repository. This repository now consumes it as the audio engine for full
  voice/VoIP products.

Migration impact:

- Rebuild ESP firmware from the maintained 2026.7.0 YAMLs or update custom
  YAMLs to the SIP `voip_stack` contract.
- Update custom `external_components` entries that previously pointed at this
  repository for `esp_audio_stack`, `esp_aec` or `esp_afe`; those components now
  resolve from `github://n-IA-hane/esphome-audio-stack@main`.
- `transport: udp|tcp` still exists in ESP YAML, but it means SIP signaling
  transport only. RTP media is UDP.
- ESP devices do not REGISTER to a provider/PBX and do not require SIP auth.
  Provider/PBX registration belongs to Home Assistant's optional trunk client.
- The old ESP-only network scanning/discovery path is gone. Use explicit SIP
  URIs, ESP `static_contacts` entries or the HA-managed roster.
- The old project-specific VoIP call-control protocol is not a fallback.
- ESP group membership is not packed into the endpoint sensor anymore. The
  endpoint sensor stays short and stable; group membership is read from optional
  sibling ESPHome entities (`voip_ring_groups`, `voip_conference_groups` and
  `voip_conference_ring`) when those entities are exposed by the HA integration
  package.
- `voip_stack:` alone is only the ESP SIP/RTP engine. HA discovery, mirror-card
  state, group membership and phonebook synchronization require the maintained
  `packages/voip/ha_integration.yaml` entity surface or equivalent manual
  `platform: voip_stack` entities.

## SIP consolidation audit

The active branch is intentionally SIP-first and breaking:

- ESP `voip_stack` is a SIP phone. `transport: udp|tcp` means SIP signaling
  transport only.
- HA `voip_stack` is a SIP softphone, dial-plan authority, SIP/RTP
  bridge and optional SIP trunk endpoint. Only trunk/provider transport remains
  configurable.
- The retired proprietary intercom protocol is not a compatibility layer.
- ESP discovery/scanning between standalone peers is not a functional
  primitive. Use explicit SIP URIs, ESP `static_contacts` entries or the HA
  roster.
- Cards mirror their owner. HA cards mirror HA softphone state. ESP cards mirror
  ESPHome entities and controls.
- Optional HA trunk support is disabled by default. If disabled, no provider
  registration, external route or DTMF routing path starts.
- Inbound trunk routing uses standard SIP plus RFC2833/telephone-event DTMF to
  select local phonebook targets.

## 2026.7.0: source-based full media path and native 48 kHz VoIP presets

`2026.7.0` introduces the new full-experience audio/media path and
Sendspin/Music Assistant integration.

Maintained full-experience YAMLs now use ESPHome's `speaker_source` media
player path. Media, announcements, timer sounds, local audio files and optional
Sendspin streams enter one media player and then flow through the existing
mixer. VoIP call audio still has its own mixer source and keeps priority through the
existing call-state logic. Custom YAMLs copied from older full-experience files
should migrate away from `platform: speaker` media-player blocks and local
`files:` entries toward `media_source` plus `media_player.play_media` with
`audio-file://...` URLs.

Maintained non-native full-experience YAMLs now use the generic `runtime_controller`
component for runtime state arbitration. Custom full-experience YAMLs copied
from older releases should migrate away from `update_status`,
`timer_ringing`, local VA pending flags and callback-local LED/display/ducking
decisions. The new pattern is: callbacks send `runtime_controller.event`, the reducer
sets activities, and policies drive LED/display/audio/timer outputs from one
committed snapshot.

Voice Assistant response state is now tied to TTS/media-player announcement
lifecycle callbacks through `runtime_controller`. Slow local TTS backends can
exceed ESPHome's historical 2-second playback-start watchdog, especially XTTS
running locally. This release temporarily ships a project-local
`voice_assistant` fork that exposes `tts_playback_start_timeout`; maintained
full profiles set it to `10s` through the full voice/runtime packages.

Sendspin is included in maintained full-experience profiles as a Music Assistant
media source. It is not required for normal HA media, TTS, timer sounds,
ringtones, Voice Assistant or VoIP calls. WS3, Spotpear and P4 grouped
playback were validated with the shared `speaker_source` path and the
hardware-clocked ESP audio stack timing model.

Native ESPHome voip-only presets now use 48 kHz PCM where the actual
native I2S microphone or speaker path supports it. SIP/TCP profiles may use
larger packet times; SIP/UDP profiles use short packet times such as 10 ms so
48 kHz/s16/mono remains below the default 1200-byte UDP datagram limit.
AFE/AEC-backed profiles keep 16 kHz TX because that is the Espressif AFE/AEC
output format; this is a local branch constraint, not a global SIP media limit.

UDP custom formats are validated against `udp_max_payload` at build time and by
Home Assistant when publishing the phonebook. The default is intentionally
conservative at 1200 bytes per audio frame. If you deliberately run larger LAN
datagrams, set the same larger `udp_max_payload` in the ESPHome YAML and the
VoIP Stack integration options; otherwise use TCP for high-rate, stereo or
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

ESP mirror and HA-softphone Lovelace modes have intentionally separate
semantics. `esp_mirror` mirrors one ESP endpoint and exposes controls from that
ESP's perspective. `ha_softphone` represents Home Assistant itself. If you
previously depended on one card mixing both models, split it into one ESP mirror
card and one optional HA softphone card.

ESP mirror cards now have a keypad/manual target view. This still calls the
mirrored ESP's own `start_call` action; it is not a separate HA-side contact or
number path. The ESP uses its local synced phonebook first and sends unresolved
targets to HA for central dial-plan/trunk/group routing.

Browser audio setup now waits for the HA SIP softphone control response and
uses the negotiated TX/RX formats from that response before creating the AudioWorklets.
Custom frontend code that starts microphone/playback worklets before the
control reply should be updated; otherwise ESP -> HA answered calls can use the
wrong frame size when one direction negotiates 48 kHz.

ESP caller playback now applies the negotiated RX speaker format before the
call is activated and re-applies it when the SIP answer confirms the effective
direction formats. Custom ESP integrations that bypass the maintained
`voip_stack` speaker setup must do the same before feeding high-rate PCM to
the local speaker path.

The Lovelace frontend derives ringtone/worklet cache keys from the loaded card
module version instead of a manually bumped audio asset constant. During custom
frontend testing, clear the browser cache or change the card resource URL when
serving files outside the packaged release flow.

Older upgrade notes are kept in their original GitHub release pages. This file
tracks only the current upgrade delta from `2026.6.3` to `2026.7.0`.
