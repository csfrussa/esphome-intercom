# What's New

This page is a short release index. The complete notes for each version live in
the linked versioned document, which is also the canonical GitHub release body.

## 2026.7.1: Assist By Phone, Groups And A Stronger VoIP Stack

`2026.7.1` contains only changes made after stable `2026.7.0`.

The main additions are telephone access to any native Home Assistant Assist
pipeline, HA-owned ring groups and conference rooms, extension and group
settings for HA and registered SIP endpoints, an expanded ESP mirror keypad and
Options view, a resizable phonebook mode integrated into the same card, and
more predictable SIP, browser-audio and ESP real-time behavior. The final
release pass also stabilizes P4 audio and Assist UI transitions under concurrent
media, aligns software-AEC presets, and makes cards recover dynamically when an
ESP disconnects and returns. Hang Up remains available during outbound call
setup, including slow or unreachable targets.

The qualification covered real WS3, Spotpear and Waveshare P4 hardware, a multi-turn Assist
call from an external mobile number through the configured trunk, a registered
SIP account calling Assist over Opus, HA and ESP calls, unknown SIP callers,
groups, conferences, trunk cancellation, 8/16/48 kHz media and concurrent
music, TTS and VoIP load.

![Voice assistant extension and Assist pipeline configuration](images/voice-assistant-extension.png)

_The optional setup step uses an extension chosen by the user and either HA's
preferred Assist pipeline or a specific pipeline._

Read the complete user-facing release note:

- [`2026.7.1` release notes](RELEASE_2026_7_1.md)
- [Breaking Changes](BREAKING_CHANGES.md)

## 2026.7.0: ESPHome Devices Are VoIP Phones Now

This is the release where the project changes category.

It is no longer just a full-duplex ESPHome intercom. It is now a local SIP/VoIP
system built around Home Assistant.

ESP devices are real SIP phones. Home Assistant is a SIP endpoint too, but it
also acts as the router, bridge, resampler, central phonebook publisher, local
SIP registrar and optional trunk client.

That means you can now build setups that previously required an external PBX:

- an ESP doorbell that rings Home Assistant;
- Home Assistant answering from browser, tablet or Companion app;
- ESP-to-ESP room calls;
- Home Assistant calling ESP devices;
- Zoiper, Linphone, baresip or pjsua registering directly to Home Assistant;
- ESP devices calling registered SIP endpoints;
- Home Assistant calling real phone numbers through a SIP trunk;
- external calls reaching Home Assistant and being routed to ESPs or local
  contacts.

The old intercom use case is still there. It is just sitting on a much bigger
engine now.

Flash a YAML, add the ESP to Home Assistant, install the card, and you already
have a working full-duplex VoIP endpoint. Add the phonebook, local accounts or
a trunk when you want the system to grow.

This release is ready for field testing, and the direction is clear. The next
rounds will focus on consolidating this VoIP foundation, improving routing and
diagnostics, and building higher-level features such as group calls and richer
dial-plan automation.

Read the full release notes here:

- [`docs/RELEASE_2026_7_0.md`](RELEASE_2026_7_0.md)

Component note: the reusable ESP audio backend has been split into
[`esphome-audio-stack`](https://github.com/n-IA-hane/esphome-audio-stack).
This repository stays focused on the VoIP product layer, Home Assistant
integration, card and ready YAMLs.

Main highlights:

- ESP devices speak SIP/SDP/RTP for call control and media.
- Home Assistant can ring and answer as its own VoIP endpoint.
- Home Assistant can now act as a small PBX: local SIP endpoint accounts,
  extension aliases, registered SIP phones, ring groups, conference groups and
  one optional trunk all resolve through the same central phonebook.
- With a trunk, Home Assistant can be called from a real phone number and answer
  from the Lovelace card.
- Home Assistant can route and bridge calls between ESPs, the HA softphone,
  local SIP accounts and an optional trunk.
- ESP devices can call registered SIP endpoints and external numbers through Home
  Assistant routing.
- Ring groups implement the standard "ring many, first answer wins" model.
- Conference groups are HA-hosted SIP conference rooms; participants call the
  group contact to join and members with `conference_ring` enabled are invited
  when the room starts.
- The central phonebook is now the normal dial plan. `name` is required;
  direct endpoint fields, numbers and route metadata are optional.
- Standard SIP endpoints such as Zoiper, Linphone, baresip or pjsua can register
  to Home Assistant with local SIP accounts.
- Home Assistant can register one optional trunk for inbound/outbound external
  calls.
- Home Assistant can create local SIP accounts for standard SIP endpoints; when
  they register, they appear in the central phonebook and are pushed to ESPs.
- Inbound trunk calls can ring HA by default or be routed to a local contact
  through route hints/DTMF and automations.
- VoIP Stack supports Home Assistant's native Reconfigure flow, so ports, debug
  mode, Assist intents, local SIP accounts and trunk settings can be changed
  without deleting the integration.
- Audio formats are negotiated per direction, so each leg can use the best
  compatible quality instead of forcing one global format.
- Browser/app audio uses the dedicated binary websocket plus adaptive buffering,
  reducing periodic gap/dropout artifacts on remote HA app sessions.
- The Lovelace card mirrors the backend phone state instead of running its own
  call-control model.
- The HA softphone card now includes a manual keypad/text target view for calls
  outside the visible contact selector.
- ESP mirror cards now also expose a keypad/manual target view. It calls the
  selected ESP's own `start_call` service, so the ESP first uses its local
  synced phonebook and routes unresolved targets through HA just like a physical
  button would.
- ESP and HA group membership is dynamic. ESPs expose editable
  `voip_ring_groups`, `voip_conference_groups` and `voip_conference_ring`
  entities; HA softphone settings are exposed through the card/service and
  republished as HA's virtual endpoint.
- The central phonebook is pushed automatically to online ESPs when HA contacts,
  ESP endpoints or registered SIP endpoints change.
- HA also refreshes and pushes the phonebook when ESPHome registers a
  `*_set_roster_json` service after reboot, closing the timing window where an
  ESP could come back online before its roster action was ready.
- SIP digest-auth INVITE retries now rebuild the transaction with a fresh Via
  branch, improving interoperability with stricter PBX/FRITZ!Box behavior.
- Full-experience YAMLs move further toward the source-based media path,
  runtime reducer and shared audio arbitration model.
