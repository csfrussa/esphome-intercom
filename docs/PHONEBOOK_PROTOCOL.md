# SIP Phonebook Contract

The phonebook is the SIP dial plan shared by ESP devices, Home Assistant,
registered local softphones and the optional trunk. SIP is implicit everywhere:
fields named `transport`, `sip_transport` or `protocol` only choose SIP/TCP or
SIP/UDP signaling, never a second call-control protocol.

## ESP Local Phonebook

Declare local entries directly in `intercom_api`:

```yaml
intercom_api:
  id: intercom
  protocol: udp
  phonebook:
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
  - intercom_api.add_contacts:
      name: Kitchen
      ip: 192.168.1.42
      sip_transport: udp
```

Rules:

- `name` is required.
- `ip`, `port`, `rtp_port`, and `sip_transport` are optional.
- If `sip_transport` is omitted, the contact uses the ESP phone signaling transport.
- Name-only entries are logical SIP targets and can be resolved or bridged by HA.
- A numeric name/number from an ESP is routed to HA. HA decides whether it is a
  local extension or an external trunk number.

## HA Roster

HA owns the central `sensor.intercom_phonebook` roster. It contains ESP peers,
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
service: intercom_native.phonebook_add_contact
data:
  name: MobileOffice
  kind: softphone
  number: "210"
```

`name` is the only required field. `number` is an optional extension/alias.
`address`, `sip_uri`, `sip_transport`, `sip_port`, and `rtp_port` are optional
and are filled by ESP endpoint publication, manual entries or SIP account
registration when available.

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
