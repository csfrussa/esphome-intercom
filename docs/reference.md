# Reference

## ESP `intercom_api`

```yaml
intercom_api:
  id: intercom
  protocol: udp
  sip_port: 5060
  rtp_port: 40000
  phonebook:
    - name: Kitchen
      ip: 192.168.1.42
      sip_transport: udp
      port: 5060
      rtp_port: 40000
```

Important options:

| Option | Meaning |
| --- | --- |
| `protocol` | SIP signaling transport: `udp` or `tcp`. |
| `sip_port` | Local SIP listener port. |
| `rtp_port` | Local RTP media port. |
| `phonebook` | Local SIP dial plan. |
| `ha_phonebook_text_sensor_id` | HA-published central roster source. |
| `on_calling` | Automation hook for outbound INVITE state. |
| `on_ringing` | Automation hook for inbound INVITE ringing state. |
| `on_dest_ringing` | Automation hook for remote `180 Ringing`. |
| `on_incoming_call` | SIP-aware hook with `call_id`, `caller`, `callee`, `uri`. |
| `on_outgoing_call` | SIP-aware hook with `call_id`, `caller`, `callee`, `uri`. |
| `on_bridge_request` | SIP-aware hook when the selected route targets HA/bridge. |
| `on_in_call` | Automation hook for established SIP call. |
| `on_hangup` | Terminal/hangup hook. |
| `on_call_failed` | Terminal failure hook. |

Runtime actions:

- `intercom_api.start`
- `intercom_api.stop`
- `intercom_api.answer_call`
- `intercom_api.decline_call`
- `intercom_api.add_contact`
- `intercom_api.add_contacts`
- `intercom_api.remove_contact`
- `intercom_api.set_contacts`
- `intercom_api.set_roster_json`

Local phonebook contacts accept `sip_transport: udp|tcp` when one contact must
use a different signaling transport from the phone's own `protocol`.

Conditions:

- `intercom_api.is_idle`
- `intercom_api.is_calling`
- `intercom_api.is_ringing`
- `intercom_api.is_in_call`
- `intercom_api.is_incoming`

## HA Services

- `intercom_native.sip_call`
- `intercom_native.sip_answer`
- `intercom_native.sip_decline`
- `intercom_native.sip_hangup`
- `intercom_native.sip_forward`
- `intercom_native.sip_route`
- `intercom_native.sip_set_dnd`
- `intercom_native.phonebook_add_contact`
- `intercom_native.phonebook_remove_contact`
- `intercom_native.phonebook_set_contacts`
- `intercom_native.phonebook_clear`
- `intercom_native.phonebook_push`

`sip_call` accepts `destination`, `target`, or `call`. Set `ha_bridge: true` to
force the HA bridge path.

`sip_route` applies an automation decision to a pending inbound SIP route. Use
`action: answer_ha`, `decline`, `busy`, `cancel`, `forward`, `bridge`, or
`default`.

`sip_forward` with `call_id` is shorthand for `sip_route` with
`action: forward`; without `call_id` it originates a new HA bridged SIP call.

## HA SIP Events

`intercom_native` fires Home Assistant bus events for automations:

- `intercom_native.sip_call_state`: every SIP phone/bridge state update.
- `intercom_native.sip_incoming_call`: inbound call or route request.
- `intercom_native.sip_route_request`: HA dial-plan lookup request.
- `intercom_native.sip_call_ended`: terminal `ended`, `missed`, or `failed`.
- `intercom_native.call_event`: aggregate SIP call event for frontend and automations.

The payload includes the canonical SIP fields when available: `state`,
`sip_state`, `type`, `call_id`, `caller`, `callee`, `peer_name`, `direction`,
`local_name`, `target`, `sip_uri`, `route_kind`, `sip_transport`,
`sip_status_code`, `terminal_reason`, selected media formats, and RTP counters.

## SIP State Values

Public states: `idle`, `calling`, `remote_ringing`, `ringing`, `connecting`,
`in_call`, `terminating`, `busy`, `declined`, `cancelled`,
`media_incompatible`, `transport_unreachable`, and
`auth_required_unsupported`.
