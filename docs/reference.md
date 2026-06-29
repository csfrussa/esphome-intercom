# Reference

## ESP `esphome_voip_stack`

```yaml
esphome_voip_stack:
  id: voip_phone
  transport: udp  # SIP signaling transport only; audio is always RTP/UDP.
  sip_port: 5060
  rtp_port: 40000
  static_contacts:
    - name: Kitchen
      ip: 192.168.1.42
      sip_transport: udp
      port: 5060
      rtp_port: 40000
```

Important options:

| Option | Meaning |
| --- | --- |
| `transport` | SIP signaling transport: `udp` or `tcp`. SIP is implicit; this is not a protocol-family selector and does not move audio to TCP. RTP audio remains UDP. |
| `sip_port` | Local SIP listener port. |
| `rtp_port` | Local RTP media port. |
| `static_contacts` | Optional local contacts loaded at boot. HA-managed `sensor.voip_phonebook` is recommended for normal installs. |
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

- `esphome_voip_stack.start`
- `esphome_voip_stack.stop`
- `esphome_voip_stack.answer_call`
- `esphome_voip_stack.decline_call`
- `esphome_voip_stack.add_contact`
- `esphome_voip_stack.add_contacts`
- `esphome_voip_stack.remove_contact`
- `esphome_voip_stack.set_contacts`
- `esphome_voip_stack.set_roster_json`

Static and runtime contacts accept `sip_transport: udp|tcp` when one contact
must use a different signaling transport from the phone's own `transport`.

Conditions:

- `esphome_voip_stack.is_idle`
- `esphome_voip_stack.is_calling`
- `esphome_voip_stack.is_ringing`
- `esphome_voip_stack.is_in_call`
- `esphome_voip_stack.is_incoming`

## HA Services

- `homeassistant_voip_stack.sip_call`
- `homeassistant_voip_stack.sip_answer`
- `homeassistant_voip_stack.sip_decline`
- `homeassistant_voip_stack.sip_hangup`
- `homeassistant_voip_stack.sip_forward`
- `homeassistant_voip_stack.sip_route`
- `homeassistant_voip_stack.sip_set_dnd`
- `homeassistant_voip_stack.phonebook_add_contact`
- `homeassistant_voip_stack.phonebook_remove_contact`
- `homeassistant_voip_stack.phonebook_set_contacts`
- `homeassistant_voip_stack.phonebook_clear`
- `homeassistant_voip_stack.phonebook_export`
- `homeassistant_voip_stack.phonebook_push`
- `homeassistant_voip_stack.sip_account_create`
- `homeassistant_voip_stack.sip_account_remove`
- `homeassistant_voip_stack.sip_account_rotate_password`

`sip_call` accepts `destination`, `target`, or `call`. Set `ha_bridge: true` to
force the HA bridge path.

`sip_route` applies an automation decision to a pending inbound SIP route. Use
`action: answer_ha`, `decline`, `busy`, `cancel`, `forward`, `bridge`, or
`default`.

`sip_forward` with `call_id` is shorthand for `sip_route` with
`action: forward`; without `call_id` it originates a new HA bridged SIP call.

`phonebook_add_contact` requires only `name`. Optional fields are `id`, `kind`,
`address`, `sip_uri`, `number`, `ha_bridge`, `sip_transport`, `sip_port`,
`rtp_port`, `tx_rate`, `rx_rate`, `tx_formats`, `rx_formats`, and
`max_payload_bytes`. HA updates `sensor.voip_phonebook` and pushes the
roster to online ESP devices.

`phonebook_remove_contact` removes one manual central contact by `name`.
`phonebook_set_contacts` replaces manual central contacts from JSON.
`phonebook_clear` removes manual central contacts. `phonebook_push` republishes
the current merged roster without changing it.

`sip_account_create` creates or replaces a local account for Zoiper, Linphone,
baresip, pjsua or another standard SIP softphone registering directly to HA. The
`username` is the SIP username and central roster ID; `display_name`,
`password`, `enabled`, and `replace` are optional. If `password` is omitted, HA
generates one and shows it once in a persistent notification and in the
`homeassistant_voip_stack.call_event` stream with `state: sip_account_created`.
Registered clients appear in the central phonebook and are pushed to ESPs.

## HA Setup Options

The setup flow has two layers:

| Option | Meaning |
| --- | --- |
| `sip_tcp_enabled` | Listen for SIP/TCP on the HA SIP port. Enabled by default. |
| `sip_udp_enabled` | Listen for SIP/UDP on the HA SIP port. Disabled by default. |
| `sip_port` | HA SIP listener port shared by enabled transports. |
| `rtp_port` | Base HA RTP UDP port used by HA softphone media and relays. |
| `advertise_host` | Optional Contact/SDP host override for routed, VPN, LXC, Docker or multihomed installs. |
| `assist_intents` | Optional Assist intents for call, answer, decline and hangup. |
| `trunk_enabled` | Enables the second setup step for provider/PBX registration. When false, no trunk client, registration, external route or DTMF collector starts. |

When `trunk_enabled` is true, the second step adds:

| Option | Meaning |
| --- | --- |
| `trunk_transport` | SIP transport used toward the provider: `udp` or `tcp`. |
| `trunk_server` | Provider registrar/proxy host. |
| `trunk_port` | Provider SIP port, normally `5060`. |
| `trunk_domain` | Optional SIP realm/domain; defaults to `trunk_server`. |
| `trunk_username` | SIP account user and default incoming Request-URI user. |
| `trunk_auth_username` | Optional digest auth username when different from `trunk_username`. |
| `trunk_password` | Digest auth password. |
| `trunk_register_expires` | REGISTER expiration in seconds. |
| `trunk_outbound_proxy` | Optional proxy host or `sip:host:port` used as signaling next hop. |
| `trunk_inbound_default_target` | Local target used when no DTMF route hint arrives. Default `HA`. |
| `trunk_dtmf_enabled` | Enable inbound RFC2833/telephone-event digit collection. |
| `trunk_dtmf_timeout_ms` | Digit collection window, clamped to 100-2000 ms. Default 1000 ms. |
| `trunk_dtmf_terminator` | Optional terminator digit such as `#`. Empty means timeout or exact route match decides. |
| `trunk_dtmf_routes` | Newline-separated `digits=target` routes, for example `100=Cucina`. |

Ambiguous DTMF digit prefixes are not rejected at setup. HA collects within the
timeout and tries the final buffer. If no digits arrive, HA uses
`trunk_inbound_default_target`. If explicit digits arrive and do not resolve,
HA logs the digits and terminates the answered leg as `route_not_found`.

## HA SIP Events

`homeassistant_voip_stack` fires Home Assistant bus events for automations:

- `homeassistant_voip_stack.sip_call_state`: every SIP phone/bridge state update.
- `homeassistant_voip_stack.sip_incoming_call`: inbound call or route request.
- `homeassistant_voip_stack.sip_route_request`: HA dial-plan lookup request.
- `homeassistant_voip_stack.sip_call_ended`: terminal `ended`, `missed`, or `failed`.
- `homeassistant_voip_stack.call_event`: aggregate SIP call event for frontend and automations.

The payload includes the canonical SIP fields when available: `state`,
`sip_state`, `type`, `call_id`, `caller`, `callee`, `peer_name`, `direction`,
`local_name`, `target`, `sip_uri`, `route_kind`, `sip_transport`,
`sip_status_code`, `terminal_reason`, selected media formats, and RTP counters.
The HA softphone snapshot also exposes `sip_trunk` when a trunk client exists,
including registration status, last SIP status and last trunk SIP event.

## SIP State Values

Public states: `idle`, `calling`, `remote_ringing`, `ringing`, `connecting`,
`in_call`, `terminating`, `busy`, `declined`, `cancelled`,
`media_incompatible`, `transport_unreachable`, and
`auth_required_unsupported`.
