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
  Piper, Wyoming, cloud providers, Codex Assist and other HA agents use the same
  route without provider-specific code.
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
created from live membership and disappear when no endpoint declares them.

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
- Incoming digit routing accepts both RTP `telephone-event` and standard SIP
  INFO DTMF, including four-digit trunk destinations entered at human speed.
- Ring-group legs, conference members, RTP ports, registrations and transaction
  caches all have explicit limits instead of growing without bound.

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
- The AFE worker is persistent and event-driven. It sleeps on notifications
  while idle; no periodic delay was added to hide contention.
- Single-microphone and dual-microphone AFE paths retain their appropriate
  Espressif processing routes.
- Music, TTS and bidirectional VoIP were exercised together while monitoring
  heap, PSRAM and main-loop latency.

## ✅ Validation

- Home Assistant, integration, card and tooling: 302 tests plus 25 subtests.
- ESP VoIP stack: 57 tests.
- Audio and AFE: 19 tests.
- Runtime controller: 6 tests.
- Virtual call scenarios: 27 passed.
- Qualification matrix: 2,162 valid combinations.
- Terminal-state regression: 1,000 seeded repetitions.
- Real WS3 and Spotpear calls covered HA-to-ESP, ESP-to-HA, ESP-to-ESP,
  registered SIP endpoints, callers absent from the phonebook, ring groups,
  conferences, DND, Auto Answer, trunk cancellation and immediate reuse.
- Assist validation covered a local HA-registered SIP account over Opus 48 kHz
  and an external mobile caller over trunk PCMA 8 kHz. The external call kept
  one conversation across three spoken turns, including a Home Assistant
  control request, with zero RTP drops or media errors.
- Both S3 targets completed clean ESPHome 2026.6.5 builds, concurrent OTA
  deployment and return-to-online checks.

## ⚠️ Boundaries To Know Before Testing

Read [Breaking Changes](BREAKING_CHANGES.md) when updating an earlier
development snapshot. In particular:

- structured ESP contacts use `ip` and `transport`; richer number, extension
  and group fields belong to the HA phonebook;
- the phonebook is an outbound dial plan, not an inbound caller allowlist;
- full hold/resume renegotiation is not implemented;
- trunk digit routing accepts RTP `telephone-event` and SIP INFO DTMF; acoustic
  in-band DTMF tones are not decoded;
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
