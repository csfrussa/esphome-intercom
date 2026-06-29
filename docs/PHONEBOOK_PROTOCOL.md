# SIP Phonebook Contract

The phonebook is the SIP dial plan shared by ESP devices, Home Assistant,
registered local softphones and the optional trunk. SIP is implicit everywhere:
fields named `transport`, `sip_transport` or `transport` only choose SIP/TCP or
SIP/UDP signaling, never a second call-control protocol.

## ESP Static Contacts

Declare static local entries directly in `esphome_voip_stack` only when an ESP must
have contacts before HA sync, work offline, or keep a tiny fixed local roster:

```yaml
esphome_voip_stack:
  id: voip_phone
  transport: udp  # SIP signaling transport only; audio is always RTP/UDP.
  static_contacts:
    - name: Kitchen
      ip: 192.168.1.42
      sip_transport: udp
      port: 5060
      rtp_port: 40000
    - name: Gate
```

Runtime actions use the same model:

```yaml
on_press:
  - esphome_voip_stack.add_contacts:
      name: Kitchen
      ip: 192.168.1.42
      sip_transport: udp
```

Rules:

- `name` is required.
- `ip`, `port`, `rtp_port`, and `sip_transport` are optional.
- If `sip_transport` is omitted, the contact uses the ESP phone signaling transport.
- Name-only entries are logical targets and can be resolved or bridged by HA.
- A numeric name/number from an ESP is routed to HA. HA decides whether it is a
  local extension or an external trunk number.
- HA-managed sync through `sensor.voip_phonebook` is the recommended path.
  Static contacts are local additions/fallbacks, not a second central roster.

## HA Roster

HA owns the central `sensor.voip_phonebook` roster. It contains ESP peers,
HA itself, local softphones registered to HA, manual phone endpoints,
trunk-routed external targets when configured, and groups.

Roster entries use JSON fields:

- `id`
- `name`
- `kind`: `ha`, `esp`, `phone`, `softphone`, or `group`
- `address`
- `sip_uri`
- `number`
- `ha_bridge`
- `metadata`, including `sip_transport`, `sip_port`, `rtp_port`, and audio
  format metadata

`kind: softphone` is for a standard SIP client registered to HA's local
registrar, for example Zoiper, Linphone or baresip. Do not use `kind: sip`;
SIP is the shared protocol, not a roster kind.

Manual contacts and service calls use the same minimum contract:

```yaml
service: homeassistant_voip_stack.phonebook_add_contact
data:
  name: MobileOffice
  kind: softphone
  number: "210"
```

`name` is the only required field. `number` is an optional extension/alias.
`address`, `sip_uri`, `sip_transport`, `sip_port`, and `rtp_port` are optional
and are filled by ESP endpoint publication, manual entries or SIP account
registration when available.

Central roster services:

- `homeassistant_voip_stack.phonebook_add_contact`: add or replace one manual central
  contact. `name` is the only required field.
- `homeassistant_voip_stack.phonebook_remove_contact`: remove one manual central contact
  by name.
- `homeassistant_voip_stack.phonebook_set_contacts`: replace manual contacts from a JSON
  roster document.
- `homeassistant_voip_stack.phonebook_clear`: clear manual central contacts.
- `homeassistant_voip_stack.phonebook_push`: push the current roster immediately to
  online ESP devices.
- `homeassistant_voip_stack.phonebook_export`: emit the current roster as an HA event for
  diagnostics/backup.

Local softphone accounts are created with `homeassistant_voip_stack.sip_account_create`.
The `username` becomes the SIP username and central roster ID. If `password` is
omitted, HA generates one and shows it once in a persistent notification and in
the `homeassistant_voip_stack.call_event` stream. Registered clients publish a dynamic
Contact into the roster so ESP devices can call them by name.

## Routing

- `sip:name@host[:port]` and `name@host[:port]` route direct.
- `name` resolves through the phonebook.
- Phone/number targets require HA routing.
- `ha_bridge: true` forces HA to act as a SIP bridge.
- If HA has a registered trunk, external numbers and unresolved number-like
  targets can route through the trunk.
- Inbound trunk DTMF routes map digit strings to the same local target namespace
  as the phonebook. No DTMF route hint means "ring HA". A received explicit
  route hint that cannot be resolved terminates as `route_not_found`; it does
  not silently fall back to HA.
- HA automations can override a pending route request by listening for
  `homeassistant_voip_stack.sip_route_request` and calling `homeassistant_voip_stack.sip_route`.
- Missing or incompatible media routes must fail explicitly with SIP terminal
  reasons such as `media_incompatible` or `transport_unreachable`.

## Default Routing Rules

ESP-origin calls:

- explicit `sip:user@host` or `user@host` route direct;
- known ESP contact with direct URI/host/transport routes direct unless
  `ha_bridge` is set;
- known contact without direct route data routes through HA;
- unknown names route through HA;
- numeric targets route through HA.

HA-router calls:

- local HA target rings the HA softphone;
- ESP targets forward to their SIP URI/host;
- local softphones forward to their registered Contact while registered;
- phone/external numbers use the trunk only when the trunk is registered;
- disabled entries reject with a SIP terminal reason.

Inbound trunk calls:

- if no route hint arrives, HA softphone rings;
- if a DTMF/SIP route hint resolves, HA bridges to that local target;
- if a DTMF/SIP route hint is explicit but does not resolve, HA terminates the
  answered trunk leg with `route_not_found`.
