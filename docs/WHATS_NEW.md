# What's New

## 2026.7.0-dev: ESPHome Devices Are VoIP Phones Now

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
- ESP devices calling registered softphones;
- Home Assistant calling real phone numbers through a SIP trunk;
- external calls reaching Home Assistant and being routed to ESPs or local
  contacts.

The old intercom use case is still there. It is just sitting on a much bigger
engine now.

Flash a YAML, add the ESP to Home Assistant, install the card, and you already
have a working full-duplex VoIP endpoint. Add the phonebook, local accounts or
a trunk when you want the system to grow.

This prerelease is still for field testing, but the direction is clear. The next
rounds will focus on consolidating this VoIP foundation, improving routing and
diagnostics, and building higher-level features such as group calls and richer
dial-plan automation.

Read the full prerelease notes here:

- [`docs/RELEASE_2026_7_0_DEV.md`](RELEASE_2026_7_0_DEV.md)

Component note: the reusable ESP audio backend has been split into
[`esphome-audio-stack`](https://github.com/n-IA-hane/esphome-audio-stack).
This repository stays focused on the VoIP product layer, Home Assistant
integration, card and ready YAMLs.

Main highlights:

- ESP devices speak SIP/SDP/RTP for call control and media.
- Home Assistant can ring and answer as its own VoIP endpoint.
- With a trunk, Home Assistant can be called from a real phone number and answer
  from the Lovelace card.
- Home Assistant can route and bridge calls between ESPs, the HA softphone,
  local SIP accounts and an optional trunk.
- ESP devices can call registered softphones and external numbers through Home
  Assistant routing.
- The central phonebook is now the normal dial plan. `name` is required;
  direct endpoint fields, numbers and route metadata are optional.
- Standard softphones such as Zoiper, Linphone, baresip or pjsua can register
  to Home Assistant with local SIP accounts.
- Home Assistant can register one optional trunk for inbound/outbound external
  calls.
- Home Assistant can create local SIP accounts for standard softphones; when
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
- The central phonebook is pushed automatically to online ESPs when HA contacts,
  ESP endpoints or registered softphones change.
- Full-experience YAMLs move further toward the source-based media path,
  runtime reducer and shared audio arbitration model.
