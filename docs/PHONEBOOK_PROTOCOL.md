# SIP Phonebook Contract

The phonebook is the SIP dial plan shared by ESP devices and Home Assistant.

## ESP Local Phonebook

Declare local entries directly in `intercom_api`:

```yaml
intercom_api:
  id: intercom
  protocol: udp
  phonebook:
    - name: Kitchen
      ip: 192.168.1.42
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
```

Rules:

- `name` is required.
- `ip`, `port`, `rtp_port`, and `protocol` are optional.
- If `protocol` is omitted, the contact uses the ESP phone signaling transport.
- Name-only entries are logical SIP targets and can be resolved or bridged by HA.

## HA Roster

HA owns the central `sensor.intercom_phonebook` roster. It contains ESP peers,
HA itself, manual SIP endpoints, phone numbers, and groups.

Roster entries use JSON fields:

- `id`
- `name`
- `kind`: `ha`, `esp`, `phone`, `sip`, or `group`
- `address`
- `sip_uri`
- `number`
- `ha_bridge`
- `metadata`, including `sip_transport`, `sip_port`, `rtp_port`, and audio
  format metadata

## Routing

- `sip:name@host[:port]` and `name@host[:port]` route direct.
- `name` resolves through the phonebook.
- Phone/number targets require HA routing.
- `ha_bridge: true` forces HA to act as a SIP bridge.
- Missing or incompatible media routes must fail explicitly with SIP terminal
  reasons such as `media_incompatible` or `transport_unreachable`.
