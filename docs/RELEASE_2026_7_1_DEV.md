# 2026.7.1-dev: Assist By Phone, Groups And A Stronger VoIP Stack

<!-- Canonical source for the v2026.7.1-dev GitHub release body. -->

> [!IMPORTANT]
> This is a GitHub development pre-release for manual testing, not a HACS
> release. Keep HACS pre-release tracking disabled; the normal HACS path remains
> on stable `2026.7.0`.

`2026.7.1-dev` contains the changes made after stable `2026.7.0`. It expands
the Home Assistant phone system with real ring groups and conference rooms,
makes the Lovelace card more useful for both HA and ESP phones, and hardens the
call and audio paths for everyday use.

## 🏠 VoIP Stack / Home Assistant

- ☎️ The existing HA softphone can now publish its own extension, ring-group
  membership, conference-group membership and conference-ringing preference.
- 🔄 Changes made from the card or services are reflected through HA's virtual
  endpoint and republished to the shared phonebook.
- 💾 HA softphone extension, DND and group settings are persisted in the
  integration config entry. The card writes through the softphone service and
  reads the canonical snapshot back; the values survive integration reloads
  and full Home Assistant restarts.
- 🧑‍💼 Registered SIP phones are first-class endpoints. Their accounts can use
  extensions and join the same ring or conference groups as HA and ESP devices.
- 🌍 Incoming calls are not limited to callers stored in the phonebook.
  Reachable and protocol-compatible SIP callers can ring HA or an ESP, subject
  to normal destination, busy and DND checks.
- 🧭 Extensions, group contacts, registered phones, ESP endpoints and optional
  trunk routes all resolve through the same dial plan.
- 🧹 Endpoint departure, reboot and roster-service timing are handled
  automatically, so stale contacts and missed phonebook pushes are less likely.

## 🗣️ Call Home Assistant Assist By Phone

Enable **Include voice assistant**, choose HA's preferred Assist pipeline or a
specific one, and assign an extension. The selected assistant appears in the
same phonebook used by every other destination; no extension is assumed or
reserved automatically.

<p align="center">
  <img
    src="https://raw.githubusercontent.com/n-IA-hane/esphome-intercom/v2026.7.1-dev/docs/images/voice-assistant-extension.png"
    alt="Voice assistant extension and Assist pipeline configuration"
    width="590"
    style="max-width: 100%; height: auto;"
  >
</p>

_The optional second setup step keeps the SIP extension explicit and supports
both HA's preferred Assist pipeline and a specifically selected pipeline._

- 📞 ESP phones, locally registered SIP clients, direct compatible SIP callers
  and external callers arriving through a trunk can call the assistant.
- 👋 The assistant receives the SIP caller identity as its first turn and can
  greet first. A matching phonebook name is used when available; otherwise the
  original caller string or number is preserved.
- 🔁 After speaking, it listens for the caller, runs STT and the conversation
  agent, streams the TTS reply back into the same call, and repeats until the
  caller hangs up. Conversation context is retained between turns.
- 🧩 VoIP Stack runs the selected native HA pipeline, so its existing STT,
  conversation agent, TTS, language and voice settings remain authoritative.
  Piper, Wyoming, cloud providers and local HA agents use the same route without
  provider-specific code.
- 🏠 No separate Home Assistant VoIP integration, second SIP port or generated
  Assist satellite is required.

## 🎛️ Lovelace Card

- 🪞 ESP mirror cards keep their original meaning: each card represents one ESP
  phone and uses that ESP's own call controls and synchronized phonebook.
- ⌨️ ESP mirror cards now include a keypad for a manual phonebook name, SIP URI,
  extension or number without overwriting the contact selected on the device.
- ⚙️ The Options view exposes Auto Answer, DND, extension, ring groups,
  conference groups and conference ringing when the selected endpoint supports
  them.
- 📐 Option labels share one left edge; fields, selectors and checkboxes share a
  clean right-hand control column.
- 🏠 HA softphone cards expose the same HA-owned extension and group settings
  without creating a second routing or call-state engine in the browser.
- 📒 The main card now has a phonebook mode backed by the canonical HA roster.
  It sorts contacts alphabetically, uses the available Sections grid area and
  scrolls only when the roster is longer than that area.
- ↔️ ESP mirror, HA softphone and phonebook modes support native Home Assistant
  Sections resizing. Their controls respond to both card width and height, and
  an omitted `name` or `title` leaves no empty header row.
- 🔔 Ringtone, DND and terminal call state no longer race normal card refresh or
  cleanup paths.
- 🔌 ESP mirror availability follows the bound Home Assistant entity live. A
  card becomes unavailable when its ESP leaves and recovers when it returns,
  without a dashboard refresh or polling timer.
- 🖱️ Internal card scrolling hands the remaining wheel/trackpad movement back
  to the dashboard at the card boundary.
- 🎨 Native contact selectors remain readable on both light and dark themes,
  including the operating-system popup rows.
- 🔒 Caller and destination labels are inserted as text, so SIP display strings
  cannot become card markup.

<p align="center">
  <img
    src="https://raw.githubusercontent.com/n-IA-hane/esphome-intercom/v2026.7.1-dev/docs/images/esp-mirror-card-keypad-options.png"
    alt="ESP mirror card with keypad and endpoint options"
    width="420"
    style="max-width: 100%; height: auto;"
  >
</p>

_ESP mirror card with keypad, Auto Answer, DND, extension and group controls
expanded._

## 🔔 Ring Groups

Call a group such as `RG Home` and every available member rings together.
Home Assistant, ESP phones, registered SIP endpoints and manual contacts can all
participate.

The first member to answer wins. Other early legs are cancelled, and a late
answer cannot steal or duplicate the established call. Group contacts are
created from live membership and disappear when no endpoint declares them. The
card keeps the group name while dialing/ringing, then shows the actual endpoint
that answered, including the Home Assistant softphone.

## 🎙️ Conference Groups

Home Assistant can now host SIP conference rooms. Calling a contact such as
`CG Home` joins that room; members with conference ringing enabled can also be
invited when the room starts.

The conference focus mixes the active participants, applies additional headroom
as the room grows and cleans up invited legs and media ports when the owner or
last participant leaves.

## 📒 Phonebook And Dial Plan

- Group fields accept comma-separated membership, so one phone can belong to
  more than one room or ring group.
- The roster advertises directional media capabilities and the fields needed by
  HA, ESP and registered endpoints.
- Numeric inbound routing uses the same phonebook extensions instead of a
  separate static route table.
- HA refreshes the roster when an ESP's phonebook service appears after reboot,
  closing the window where discovery could finish before the service was ready.
- SIP authentication retries rebuild the transaction correctly for stricter
  PBX and FRITZ!Box implementations.

## 📞 More Predictable Calls

- SIP transactions now match the correct Call-ID, CSeq, branch, dialog tags and
  peer before changing a call.
- Retransmissions, decline, cancel, busy, answer, hangup and immediate redial
  have deterministic ownership and cleanup.
- The Hang Up action remains available while an outbound call is still in
  `calling`, including unreachable or slow external destinations.
- A cancel that crosses a successful answer is completed with the proper
  acknowledgement and teardown instead of leaving a ghost call.
- Reused SIP/TCP connections serialize outgoing messages and keep pending work
  bounded.
- Unsupported hold or codec-changing re-INVITE requests receive `488 Not
  Acceptable Here` without destroying the call already in progress.
- Incoming digit routing accepts both standard RTP `telephone-event` and the
  widely deployed legacy SIP INFO DTMF representation, including four-digit
  trunk destinations entered at human speed.
- During an established HA-bridged call, each RFC 4733 or compatible SIP INFO
  DTMF key is also exposed as `voip_stack.dtmf` for automations. This event
  path is separate from pre-answer extension routing and never transfers the
  call by itself.
- Ring-group legs, conference members, RTP ports, registrations and transaction
  caches all have explicit limits instead of growing without bound.

## 🔢 Use Phone Keys In Home Assistant Automations

During a call bridged by Home Assistant, every DTMF key now fires one
`voip_stack.dtmf` event. This makes practical actions such as **press 1 to open
the gate** possible while the conversation and audio continue normally.

```yaml
triggers:
  - trigger: event
    event_type: voip_stack.dtmf
    event_data:
      source: Cordless
      digit: "1"
actions:
  - action: cover.open_cover
    target:
      entity_id: cover.gate
```

Events include caller, callee, source leg, digit, both call IDs and transport,
so automations can be restricted to the intended phone and active call. This is
not a second dial plan: initial extension collection remains separate and an
in-call key never transfers a call by itself. Detection is entirely HA-side;
no parser or additional real-time work is added to ESP firmware.

## 🔊 Audio And ESP Real-Time Performance

- Browser audio has one owner per call and uses stateful codecs, absolute pacing
  and bounded queues. A short FIFO jitter buffer smooths Chrome microphone
  bursts instead of discarding adjacent speech frames.
- PCMA 8 kHz, L16 48 kHz and Opus 48 kHz paths are used where the endpoints on
  that leg support them.
- PCMA/G.711 handset audio now preserves the source speech level instead of
  losing about 6 dB during A-law encoding, so Assist replies remain audible on
  a normal phone earpiece without requiring speakerphone.
- ESP audio conversion state and working buffers are prepared outside the
  per-frame path.
- Generic software-AEC presets use a previous-frame reference by default. The
  optional ring-buffer reference is aligned to microphone-consumer sessions so
  a new call cannot begin against stale playback audio.
- The AFE worker is persistent and event-driven. It sleeps on notifications
  while idle; no periodic delay was added to hide contention.
- Single-microphone and dual-microphone AFE paths retain their appropriate
  Espressif processing routes.
- On Waveshare P4, the real-time I2S/AFE bridge stays in internal RAM and the
  calibrated AEC path remains active under Sendspin and VoIP load. Music, TTS
  and bidirectional VoIP were exercised together while monitoring heap, PSRAM
  and main-loop latency.

## 🖥️ Device Runtime And P4 Touch

- P4's phone panel now switches explicitly between **Contacts** and a local
  numeric **Keyboard**, avoiding gesture conflicts with the surrounding LVGL
  page.
- Assist, media, timer, ringtone, call, LED and ducking decisions converge
  through the shared runtime controller. Device callbacks report facts; they do
  not maintain a parallel display state machine.
- Transition probes record every committed state rather than only the final
  snapshot. Qualification rejects intermediate neutral/media screens between
  Assist thinking and replying, stale Sendspin artwork after stop, and restored
  ducking while a call is active.
- Production profiles keep verbose probes disabled; diagnostics are opt-in and
  add no delay to the real-time loop.

## ✅ Validation

- Automated release run: 308 integration/card/tooling tests plus 26 subtests,
  57 ESP VoIP tests, 19 audio-stack tests and 12 runtime-controller tests. The
  deterministic qualification matrix and seeded terminal-state regression
  remain part of the release check.
- Real WS3, Spotpear and Waveshare P4 calls covered HA-to-ESP, ESP-to-HA, ESP-to-ESP,
  registered SIP endpoints, callers absent from the phonebook, ring groups,
  conferences, DND, Auto Answer, trunk cancellation and immediate reuse.
- Assist validation covered a local HA-registered SIP account over Opus 48 kHz
  and an external mobile caller over trunk PCMA 8 kHz. The external call kept
  one conversation across three spoken turns, including a Home Assistant
  control request, with zero RTP drops or media errors.
- WS3, Spotpear, P4 and generic/native profiles completed clean ESPHome 2026.6.5
  builds and return-to-online checks. P4 additionally ran concurrent Sendspin,
  Assist/TTS and VoIP state/audio qualification.

## ⚠️ Boundaries To Know Before Testing

Read [Breaking Changes](BREAKING_CHANGES.md) when updating an earlier
development snapshot. In particular:

- structured ESP contacts use `ip` and `transport`; richer number, extension
  and group fields belong to the HA phonebook;
- the phonebook is an outbound dial plan, not an inbound caller allowlist;
- full hold/resume renegotiation is not implemented;
- trunk digit routing accepts standard RTP `telephone-event` and compatible
  legacy SIP INFO DTMF; acoustic in-band DTMF tones are not decoded;
- ESP SIP/RTP remains plaintext and belongs on a trusted LAN or VPN, or behind
  an SBC.

## 🧪 Manual Test Installation

Use the `v2026.7.1-dev` GitHub tag or a fresh `dev` checkout. Manually copy
`custom_components/voip_stack` into Home Assistant's `custom_components`
directory, restart Home Assistant and refresh the frontend cache.

For firmware testing, route the maintained project sources to their `dev`
branches before compiling:

```bash
./scripts/yaml_paths.sh remote \
  --intercom dev \
  --voip dev \
  --audio dev \
  --runtime dev
./scripts/yaml_paths.sh check
```

After restart, both the installed integration and the card footer must report
`v2026.7.1-dev`. When reporting a regression, include the YAML, ESPHome and
Home Assistant versions, the caller and destination, and the negotiated audio
formats.
