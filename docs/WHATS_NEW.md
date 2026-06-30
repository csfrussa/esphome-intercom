# What's New

## 2026.7.0-dev

This is the SIP/VoIP migration prerelease, and it is the biggest architectural
jump this project has made so far.

The big change: this is no longer an intercom protocol pretending to be a phone
system. ESP devices are real local SIP phones now. Home Assistant is a real SIP
endpoint too, but it can also act as the router, bridge, resampler, local SIP
registrar and optional trunk client for the whole system.

That means the project is no longer limited to "ESP calls HA". The same stack
can now support ESP-to-ESP calls, HA-to-ESP calls, ESP-to-HA calls, registered
softphones such as Zoiper or Linphone, and external calls through a SIP trunk.

Yes, this is the part that changes the scale of the project: with a trunk you
can call your Home Assistant instance from a real phone number and answer from
the Lovelace card. From Home Assistant or from an ESP you can call another ESP,
a registered softphone, or an external phone number. A phone app can register
directly to Home Assistant and become a contact that ESP devices can call from
the same central phonebook.

In practice, this opens a completely different class of setups:

- your ESP can call another ESP directly;
- your ESP can call Home Assistant and make the Lovelace card ring;
- Home Assistant can call an ESP like a real softphone;
- a phone app can register to Home Assistant and become a local VoIP contact;
- your mobile/landline number can call the HA trunk and make HA ring;
- an ESP can call a mobile or landline number through the configured trunk;
- an external call can arrive from the trunk, ring Home Assistant, or be routed
  to an ESP;
- all of this can happen without carrying Asterisk beside the project.

This is why the migration matters: SIP/VoIP turns the project from a local
intercom into a small Home Assistant / ESPHome centered phone system. The simple
one-button intercom use case still works, but the foundation now supports far
more ambitious routing, automation and calling scenarios.

This prerelease is still for field testing, but the direction is clear. The next
rounds will focus on consolidating this VoIP foundation, improving routing and
diagnostics, and building higher-level features such as group calls and richer
dial-plan automation.

Read the full prerelease notes here:

- [`docs/RELEASE_2026_7_0_DEV.md`](RELEASE_2026_7_0_DEV.md)

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
- Audio formats are negotiated per direction, so each leg can use the best
  compatible quality instead of forcing one global format.
- The Lovelace card mirrors the backend phone state instead of running its own
  call-control model.
- Full-experience YAMLs move further toward the source-based media path,
  runtime reducer and shared audio arbitration model.
