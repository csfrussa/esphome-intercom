# Home Assistant Services

The `voip_stack.*` services are the public control surface for the HA
softphone, central phonebook, local SIP endpoint accounts and automation
fallback routing.

The card uses these services too. The card does not implement private routing
logic.

## Softphone Services

### `voip_stack.call`

Originate a call from Home Assistant.

Accepted target fields:

- `destination`
- `target`
- `call`

All three are aliases. The value can be a roster name, extension, group name,
public number, `user@host` or `sip:user@host`.

Optional:

- `ha_bridge`: force HA to anchor the route when that is valid.

### `voip_stack.forward`

With `call_id`, applies a forward decision to a pending inbound
`route_requested` call.

Without `call_id`, originates a HA-anchored call using the same central dial
plan as `voip_stack.call`.

Important rule: forwarding to a registered SIP endpoint keeps that endpoint's
current registration Contact as the destination. It must not rewrite to
`sip:<target>@<ha-ip>`.

### `voip_stack.answer`

Answer a pending call addressed to the HA softphone.

If `call_id` is omitted and exactly one call is pending, that call is answered.

For conference calls, `call_id: conference:<room>` joins the HA softphone to
the room.

### `voip_stack.decline`

Reject a pending call.

Fields:

- `call_id`: optional when only one call is pending.
- `status`: SIP final status, default normally `486`/`603` depending path.
- `reason`: SIP reason phrase.
- `decline_reason`: application terminal reason propagated in Reason headers
  and state.

### `voip_stack.hangup`

Hang up the active call or a specific `call_id`.

It stops SIP client legs, relay/media reservations, pending invites and HA
softphone media where applicable.

### `voip_stack.set_dnd`

Enable or disable DND on the HA softphone.

When DND is enabled, calls targeting HA return `486 Busy Here` with terminal
reason `dnd`.

### `voip_stack.set_ha_softphone_settings`

Publish HA softphone identity and group membership.

Fields:

- `extension`: internal extension published in the central roster.
- `ring_group`: comma-separated ring group memberships.
- `conference_group`: comma-separated conference group memberships.
- `conference_ring`: whether HA rings when another participant starts one of
  its conference groups.

Changing these settings updates HA's virtual endpoint sensor. The phonebook
sensor observes that change, rebuilds the central roster and pushes updates to
online ESP devices.

## Phonebook Services

### `voip_stack.add_contact`

Add or replace one manual central contact.

Useful fields:

- `name`: required.
- `id`: optional stable ID, defaults to name.
- `address`: direct endpoint host/IP.
- `sip_uri`: complete SIP URI.
- `extension`: internal dial-plan alias.
- `number`: external trunk number.
- `ha_bridge`: force HA routing.
- `transport`, `port`, `rtp_port`: direct SIP/RTP endpoint metadata.
- `tx_formats`, `rx_formats`, `max_payload_bytes`: optional media metadata.
- `ring_group`, `conference_group`, `conference_ring`: group membership.

### `voip_stack.remove_contact`

Remove one manual contact by name, ID, extension or number.

### `voip_stack.set_contacts`

Replace all manual contacts with a JSON roster document.

Dynamic ESP, HA and registered SIP endpoint entries are not manual contacts and
are rebuilt from their live sources.

### `voip_stack.clear_contacts`

Clear manual contacts only.

### `voip_stack.export_phonebook`

Emit the current central JSON phonebook as a `voip_stack.call_event`.

### `voip_stack.push_phonebook`

Push the current central JSON phonebook to online ESP devices.

The normal path is automatic: phonebook changes trigger a rebuild and push.
Home Assistant also refreshes and pushes again when an ESPHome
`*_set_roster_json` service is registered, so rebooting ESPs receive the roster
after their action surface is ready. Use this service for diagnostics or manual
recovery.

### `voip_stack.purge_devices`

Remove unavailable ESPHome VoIP devices from the HA device registry.

Without `device_id`, only unavailable/unknown devices older than
`min_unavailable_hours` are purged. A large value is a safe no-op test.

## Local SIP Endpoint Account Services

These accounts are for standard SIP endpoints registering to HA: phones,
softphones, ATAs, baresip, pjsua and similar clients.

### `voip_stack.create_account`

Create or replace a local SIP endpoint account.

Fields:

- `username`: SIP username and roster ID.
- `display_name`: optional friendly name.
- `password`: optional. If omitted, HA generates one and shows it once.
- `enabled`: default true.
- `replace`: allow replacing an existing account.
- `extension`: optional internal extension. When set, the endpoint can be
  called by name or extension.
- `ring_group`, `conference_group`, `conference_ring`: group membership.

If a manual password is provided, HA preserves it exactly and creates a
notification without echoing the password back.

### `voip_stack.remove_account`

Remove a local SIP endpoint account and active registration.

### `voip_stack.enable_account`

Enable an account.

### `voip_stack.disable_account`

Disable an account and clear active registration.

### `voip_stack.rotate_account_password`

Generate a new password, clear active registration and show the replacement
once in both the call event stream and a persistent notification.

### `voip_stack.list_accounts` / `voip_stack.export_accounts`

Emit configured accounts without passwords.

## Automation Route Service

### `voip_stack.route`

Apply a decision to a pending inbound SIP route request.

Fields:

- `call_id`: required.
- `action`: `answer_ha`, `decline`, `busy`, `forward`, `bridge`, `default`,
  `cancel`.
- `destination`, `target`, `call`: aliases for forward/bridge destination.
- `status`, `reason`, `decline_reason`: optional terminal response metadata.

Use this only for the automation fallback path. Known roster targets should be
handled by the central dial plan without waiting for this service.

## In-call DTMF Automation Event

During an established call bridged by HA, each negotiated RFC 4733
`telephone-event` key or compatible in-dialog SIP INFO key fires
`voip_stack.dtmf`. This does not invoke the initial trunk extension router and
does not transfer the active call.
Detection and event publication are HA-backend features; they add no DTMF
parser, callback or media-loop work to ESP endpoints.

```yaml
triggers:
  - trigger: event
    event_type: voip_stack.dtmf
    event_data:
      source: Cordless
      digit: "1"
actions:
  - action: lock.unlock
    target:
      entity_id: lock.gate
```

The event carries one key at a time plus both call IDs, caller, callee,
`source`, `source_leg`, diagnostic `side`, and the transport (`rtp_event` or
`sip_info`). It is available only for calls that pass through VoIP Stack in
HA; direct peer-to-peer ESP calls remain invisible to HA.
