# Reference

## ESP `intercom_api`

```yaml
intercom_api:
  id: intercom
  protocol: udp  # SIP signaling transport only; audio is always RTP/UDP.
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
| `protocol` | SIP signaling transport: `udp` or `tcp`. SIP is implicit; this is not a protocol-family selector and does not move audio to TCP. RTP audio remains UDP. |
| `sip_port` | Local SIP listener port. |
| `rtp_port` | Local RTP media port. |
| `static_contacts` | Optional local contacts loaded at boot. HA-managed `sensor.intercom_phonebook` is recommended for normal installs. |
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

Static and runtime contacts accept `sip_transport: udp|tcp` when one contact
must use a different signaling transport from the phone's own `protocol`.

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
- `intercom_native.phonebook_export`
- `intercom_native.phonebook_push`
- `intercom_native.sip_account_create`
- `intercom_native.sip_account_remove`
- `intercom_native.sip_account_rotate_password`

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
`max_payload_bytes`. HA updates `sensor.intercom_phonebook` and pushes the
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
`intercom_native.call_event` stream with `state: sip_account_created`.
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
The HA softphone snapshot also exposes `sip_trunk` when a trunk client exists,
including registration status, last SIP status and last trunk SIP event.

## SIP State Values

Public states: `idle`, `calling`, `remote_ringing`, `ringing`, `connecting`,
`in_call`, `terminating`, `busy`, `declined`, `cancelled`,
`media_incompatible`, `transport_unreachable`, and
`auth_required_unsupported`.
