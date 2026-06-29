# 2026.7.0-dev — negotiated audio formats, speaker-source media path and Sendspin

This is a **development prerelease** for field testing the next intercom and
media generation before it becomes a stable release. It contains large changes
in Home Assistant, the Lovelace card, the ESP SIP phone and the full-experience
audio path.

Use it if you want to help test. Stay on the latest stable release if the
device is installed somewhere where temporary audio glitches or call-routing
regressions would be a problem.

## 🏠 Intercom Native / Home Assistant

- 🗣️ **Intercom Native now is able to call other intercoms via Voice
  Assistant.** An optional Assist intent adapter can be enabled from the
  Intercom Native setup/reconfigure dialog. With the provided custom sentences,
  voice satellites can call a phonebook contact, hang up, answer or decline
  using the satellite that heard the sentence. Spoken contact names are resolved
  dynamically against the live phonebook, so `call kitchen speaker` can resolve
  to the canonical `Kitchen Speaker` contact without changing low-level
  intercom matching. If no phonebook contact matches, the adapter can also call
  the single intercom device assigned to a matching Home Assistant area name.
  Multiple intercom devices per area are intentionally not voice-dialed in this
  prerelease; group/area calls are left for a later release.
- 🧬 Added negotiated PCM audio formats to the HA SIP softphone/bridge stack.
- 🔁 Audio formats are now **per direction**, not one global device format:
  `tx_format` describes device-to-wire audio and `rx_format` describes
  wire-to-device audio.
- 🎚️ HA can bridge endpoints with different TX/RX formats by converting the
  audio explicitly instead of pretending every call is 16 kHz/s16/mono.
- 🧮 HA bridge conversion is vectorized through NumPy and uses anti-aliased
  sample-rate conversion for downsampling.
- 🧩 SIP/SDP offer-answer now carries explicit PCM audio capabilities.
- 🧭 HA now validates endpoint-declared formats before routing calls. Incompatible
  peers fail with a readable `incompatible_audio_format` reason instead of
  producing distorted audio.
- 📡 SIP/TCP and SIP/UDP endpoints understand the negotiated format selected
  for each call leg.
- 🌉 HA SIP bridge legs preserve the original call identity when routing toward
  the destination, so caller/destination labels stay stable across TCP ↔ UDP
  and mixed-format bridges.
- 🚪 Inbound SIP INVITE messages do **not** require the caller to exist in the
  callee phonebook. The phonebook is the outbound dial plan; inbound
  caller/destination identity comes from SIP headers. This keeps VPN/routed
  callers and HA bridges working without pre-seeding every callee phonebook.
- ☎️ Intercom Native can optionally register one SIP trunk account. When
  disabled, trunk code is inactive. When enabled, unresolved outbound numbers
  can route through the trunk and inbound provider calls can select HA or a
  local phonebook target with RFC2833/telephone-event DTMF.
- 🛡️ Browser audio WebSocket sessions remain server-authoritative: if the socket
  is truly gone, the server ends the call.
- 🔄 Browser/card reload during an active HA softphone call can explicitly rebind
  to the existing server session within a short grace window, avoiding the old
  "dashboard refresh killed or desynced the softphone call" behavior.
- 📞 ESP -> HA browser-answer calls now initialize the browser audio pipeline
  only after the HA SIP answer/control reply carries the negotiated TX/RX formats.
  This keeps the ESP-caller / HA-responder path aligned with HA-originated
  calls and avoids deep/slow audio caused by a browser worklet using stale
  16 kHz framing against a negotiated 48 kHz leg.
- 🧭 ESP caller devices now also apply the selected RX speaker format before
  activation and again when the SIP answer arrives. ESP-originated calls answered
  by HA therefore use the negotiated speaker rate instead of a stale/default
  playback format.
- 🚫 Duplicate terminal/bridge events were tightened so cards should see one
  final reason instead of compensating for repeated `disconnected` events.
- 🧪 SIP negotiated-format testing now expects a standard external softphone or
  private local debug helper, rather than a distributed project softphone.

## 🎛️ Lovelace Card

- 🧠 The card keeps the page-level audio engine model from the previous dev
  cycle: one browser audio engine per page, cards as views.
- 🔌 Audio still uses the dedicated binary WebSocket, not the shared HA frontend
  WebSocket.
- 📦 The card receives the negotiated effective formats and configures capture
  and playback from that contract.
- 🎧 Capture and playback worklets support dynamic PCM settings instead of
  assuming 16 kHz `Int16Array` everywhere.
- 🔄 Softphone calls survive normal Lovelace reload/reconnect flows when the
  browser rebinds inside the server grace window.
- 🧭 Device identity remains `device_id` based in the frontend. Legacy
  name/esphome-id matching is resolved server-side, once.
- 🔔 HA softphone has an idle-only Options panel for Auto Answer, DND and
  browser ringtone. Ringtone is a per-browser preference; HA softphone DND is
  stored in HA state.
- 🪞 Hybrid cards keep the original mirror semantics: the card represents the
  selected ESP, mirrors ESP ringing/in_call state, and becomes the HA
  softphone leg only when that ESP is calling Home Assistant. A separate
  `ha_softphone` card represents HA itself and shows Answer/Decline only for
  calls addressed to HA.
- 🧹 Ringing/In-call screens hide runtime options, so only the call actions
  relevant to the current state are visible.
- 🧾 Terminal text uses the active call peer. A card or display no longer reports
  the currently selected phonebook contact as the caller that just hung up.
- 🧹 Ringtone and AudioWorklet cache keys are derived from the loaded card module
  URL/version. The frontend no longer needs a manually bumped audio asset
  constant during prerelease testing.
- 🧪 The card version exposed in Lovelace is `v2026.7.0-dev`.

## 📞 SIP Phone Profile

- 🧾 SIP/SDP audio capability is described with explicit PCM tokens such as
  `16000:s16le:1:16`, `16000:s16le:1:32` and `48000:s16le:1:10`.
- ⏱️ SIP/SDP negotiates one common packet time per dialog. TX and RX sample
  rates can differ, but both selected directions must share a compatible
  `frame_ms`/`ptime`.
- 🔀 TX and RX capabilities are advertised separately, so a device can expose
  16 kHz microphone audio while accepting 48 kHz speaker audio.
- ✅ The SIP answer confirms the effective caller-to-destination and
  destination-to-caller formats. The caller no longer has to guess how the
  remote side will send audio.
- 🧯 Unsupported high-rate or oversized media profiles are rejected with a SIP
  terminal reason instead of playing garbage.
- 📚 `docs/INTERCOM_PROTOCOL.md` and `docs/PHONEBOOK_PROTOCOL.md` were updated
  for the format-aware endpoint rows and negotiated call setup.

## 🔊 ESP Audio Runtime

- 🧱 Runtime audio now works in explicit **audio frames/chunks** derived from the
  negotiated stream format instead of treating every path as fixed
  milliseconds around 16 kHz/s16/mono.
- 📐 Intercom TX/RX chunk sizes, ring sizes and frame validation are derived from
  `AudioFormat`.
- 🎚️ ESP speaker stream info is updated from the selected RX format before
  intercom playback, so native speaker paths can receive higher-rate PCM.
- 🎙️ AFE/AEC microphone branches remain 16 kHz/s16/mono because that is the
  Espressif AFE/AEC output contract.
- 🔄 Native ESPHome microphone/speaker paths can advertise higher-rate PCM when
  they bypass AFE/AEC.
- 🧼 TCP accept/error logs and several numeric intercom errors were made clearer
  for users reading logs.
- 🧰 Shared socket helpers and smaller transport cleanups reduced duplicated TCP
  and UDP code.
- 🧱 ESP audio stack dependencies and stream setup were tightened for cleaner
  build and runtime behavior.
- ⚙️ Maintained full YAMLs now enable ESPHome performance optimization through
  YAML build options, not one-off local build commands.

## 🎚️ Full-Experience Media Pipeline

- 🔊 Maintained full-experience presets now use the ESPHome `speaker_source`
  media path as the default media architecture.
- 🧠 Maintained full-experience presets now use the generic `runtime_fsm`
  reducer for LED/display/ducking/ringtone/timer arbitration. YAML callbacks
  report events; one reducer snapshot decides the visible state and audio
  policy, so media, TTS, timers, mute, connectivity and intercom no longer
  race each other through separate scripts.
- 🧩 Normal HA media, announcements, timer sounds, local files and Sendspin all
  enter one media player/source path, then the mixer arbitrates against
  intercom and Voice Assistant.
- 🗣️ Voice Assistant reply state is now tied to real TTS/media-player
  announcement lifecycle callbacks. Slow local TTS engines keep the blue
  response state while the URL is pending, switch cleanly when playback starts,
  and restore media/ducking state when the announcement actually ends.
- ⏱️ The project-local `voice_assistant` fork temporarily exposes
  `tts_playback_start_timeout`; maintained full profiles set it to `10s` to
  tolerate slower local XTTS responses that can exceed ESPHome's historical
  2-second playback-start watchdog.
- 🔁 Wake-word barge-in during an active VA TTS response stops only the VA
  announcement path and restarts the assistant from real component states,
  without using fixed delay windows as the normal decision path.
- 🗣️ Maintained full YAMLs now expose an optional package for local Assist
  silence commands. It lets `shut up` style commands stop only the active
  VA/TTS announcement on the satellite that heard the sentence, without
  stopping background media. Intercom call/hangup/answer/decline commands are
  handled by the optional Intercom Native Assist intent adapter instead of
  YAML automations.
- 🖼️ Sendspin artwork is promoted for display profiles that opt in to the
  artwork package. Spotpear and Waveshare P4 render Music Assistant album art
  when Sendspin exposes it, with a neutral media fallback when no artwork is
  available. The package pins ESPHome development image support from
  [esphome/esphome#16057](https://github.com/esphome/esphome/pull/16057) until
  it lands in an ESPHome release. Thanks to
  [issue #58](https://github.com/n-IA-hane/esphome-intercom/issues/58) for the
  FYI that surfaced this capability.
- 🥇 Intercom keeps priority through its dedicated mixer source.
- ⏲️ Timer/ringtone/media playback now follow the same source arbitration model
  instead of drifting through separate speaker paths.
- 🧯 Runtime reducer state dumps are disabled by default in production package
  callbacks. Enable `debug: true` or call the diagnostic dump service when
  developing a profile.
- 🧹 The project-local speaker fork remains available for custom YAMLs,
  but maintained full profiles are moving to the `speaker_source` path.
- 🔇 Mute paths and source priority rules were kept aligned with the full UI
  state machine.

## 🎵 Sendspin / Music Assistant

- 🎵 Sendspin is included in full-experience profiles as a Music Assistant
  source.
- 🎧 The default stream format is 48 kHz mono PCM.
- 🧠 Decode buffers are configured for PSRAM where supported.
- ⏱️ Playback feedback is tied to I2S/DMA completion instead of software-ring
  enqueue timing, matching ESPHome's native speaker-source timing model.
- 👥 Grouped-speaker playback was validated on WS3, Spotpear and P4.

## 📡 Endpoint / Phonebook / Network Recovery

- 🌐 ESP endpoints now wait for a real ESPHome network IPv4 address before
  publishing the canonical endpoint sensor.
- 🔁 Endpoint rows are requested again when Wi-Fi or Ethernet IP changes, instead
  of requiring a reboot or relying on YAML-level automations.
- 🧭 HA rebuilds the phonebook from endpoint sensors and routes through canonical
  rows.
- 📥 Incoming calls are not phonebook-gated on the receiving ESP. Unknown but
  protocol-valid external callers can ring/stream; use DND or explicit routing
  policy when you want to reject them.
- 🧾 Device terminal screens preserve the caller sensor long enough for hangup
  and failed-call callbacks, then clear it after the UI has rendered the final
  peer/reason.
- 🧹 Documentation was cleaned up to state the HA-managed phonebook model once,
  instead of repeating the same "do not rely on mDNS as the primary contract"
  guidance in several places.

## 📦 YAML Presets

- 🧩 All maintained non-native full-experience presets now use the `runtime_fsm`
  package path. Native ESPHome full presets remain on their native component
  path.
- 🧪 Native ESPHome intercom-only presets now advertise 48 kHz PCM where the
  native I2S path can support it.
- 🚦 TCP native intercom presets use larger 20 ms PCM frames.
- 🚦 UDP native intercom presets use 10 ms frames by default to stay under the
  conservative UDP payload ceiling.
- 🎙️ AFE/AEC full profiles keep 16 kHz TX from the AFE branch and can use
  higher-rate RX toward the speaker branch.
- 🔊 WS3 and Spotpear full profiles are aligned around 48 kHz RX for better
  HA-to-ESP playback quality.
- 🔁 Spotpear UDP mirrors the TCP display cleanup on HA API disconnect. P4 UDP
  landscape uses the same high-performance AFE mode as the TCP landscape
  profile.
- 🧱 The local P4 MIPI DSI wrapper accepts ESPHome's current multi-value display
  dimension API, keeping P4 builds compatible with ESPHome 2026.6.x.
- ⚙️ Full profiles include the shared performance build option baseline.
- 🧭 Release YAML references for this prerelease point to the `dev` branch.

## 🚧 UDP Payload Policy

- 📦 UDP sends one complete PCM frame per datagram.
- 🛡️ The default safe payload ceiling is `1200` bytes to avoid depending on IP
  fragmentation across Wi-Fi, VPNs or routed networks.
- ✅ ESPHome validation rejects UDP audio formats whose frame size exceeds the
  configured ceiling and tells the user to use TCP, reduce the format/frame size,
  or opt in with `udp_max_payload`.
- ⚠️ Larger LAN/Jumbo-frame deployments can override the limit, but both ESPHome
  and Home Assistant must be configured consistently and tested on that LAN.

## 📚 Documentation

- 📘 Added a dedicated `runtime_fsm` component README with the reducer model,
  YAML syntax, policy/action examples, debug mode and host-test command.
- 📖 README was reworked for the 2026.7.0-dev audio/media direction.
- 📘 `docs/reference.md`, architecture docs, protocol docs and troubleshooting
  docs were audited for stale 16 kHz-only assumptions.
- 🧭 YAML selection docs now call out native high-rate paths, AFE/AEC 16 kHz
  branches, TCP vs UDP tradeoffs and Sendspin validation status.
- 🧾 Breaking/compatibility notes were updated for negotiated formats and the
  new media path.
- 🛒 HACS guidance was refreshed now that the project is accepted in the HACS
  default repository flow. Custom repository instructions remain useful for
  development/prerelease testing.
- 💚 Donation/support messaging was made more visible near the top of the README
  while keeping the existing footer banner.

## ⚠️ Compatibility / Upgrade Notes from 2026.6.3

- Custom ESP YAMLs using `intercom_api` should review the new `audio.tx` and
  `audio.rx` settings if they want anything other than the default 16 kHz/s16/mono.
- AFE/AEC TX remains 16 kHz/s16/mono. This is expected and should not be treated
  as a global SIP media limitation.
- HA bridging is the supported way to connect devices with incompatible direct
  audio formats.
- UDP high-rate audio is limited by datagram size. Use TCP unless the UDP frame
  size is known to fit the configured payload ceiling.
- Sendspin/Music Assistant grouped playback was validated on WS3, Spotpear and
  P4. Keep testing other boards before treating every custom hardware profile
  as covered.
- The HA custom integration now requires NumPy. HA OS wheel availability was
  checked for modern Python/aarch64/x86_64 targets, but this remains one of the
  areas to watch during prerelease testing.
- Mixed transport behavior is explicit: direct SIP calls require a compatible
  signaling transport, while HA bridging is the supported route for SIP/UDP to
  SIP/TCP calls. If the media format cannot be confirmed safely, the call is
  rejected or ended.
- Receiving ESPs do not reject valid inbound SIP INVITE messages just because
  the caller is absent from the local phonebook. Custom security/policy behavior
  should be implemented explicitly; missing phonebook rows are not an inbound
  access-control mechanism.

## 🧪 Suggested Field Test Matrix

- 🏠 HA softphone -> ESP and ESP -> HA on AFE/AEC devices at 16 kHz.
- 🔄 Browser refresh/reload during an active HA softphone call; the card should
  converge back to the server session.
- 🎙️ Native intercom-only TCP at 48 kHz both directions.
- 📡 Native intercom-only UDP at 48 kHz/10 ms, verifying HA accepts the endpoint
  and the frame size stays below `udp_max_payload`.
- 🔀 ESP-to-ESP direct calls with matching formats.
- 🌉 ESP-to-ESP calls through HA SIP bridging with different TX/RX formats.
- ✅ Auto Answer on/off from both sides: ESP ringing, HA ringing, Answer,
  Decline and Hangup.
- 📥 Inbound TCP and UDP calls from callers not present in the callee phonebook:
  the callee should ring/answer and terminal UI should show the actual caller.
- 🎛️ Hybrid card behavior: Call from HA, Answer/Decline mirrored to the attached
  ESP where applicable.
- 🔊 Full-experience media playback, timer sound, ringtone and intercom priority.
- 🎵 Sendspin single speaker, duplicate cards open and grouped speakers.
- 🧯 Long playback/call sessions with logs checked for underruns, stuck media
  sources, stale sessions and warning spam.
