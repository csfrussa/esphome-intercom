# Automation Dial Plan

> Experimental in `2026.7.2-dev`. The normal phonebook route remains the
> default and does not require automations.

VoIP Stack has one canonical dial plan: the shared phonebook. Home Assistant
automations may override a call only at the explicit decision points described
below. Disabling automation routing restores the ordinary phonebook path
without changing contacts, extensions or the configured inbound destination.

The Lovelace card never chooses or filters a route. It mirrors the authoritative
backend state and sends user actions to the same router.

## Configure Incoming Trunk Routing

Reconfigure VoIP Stack and choose one **Incoming routing** mode:

| Mode | What the caller experiences | Normal route |
| --- | --- | --- |
| **Route immediately** | No DTMF collection or pre-answer delay. | The call follows the fallback destination unless an automation selects another destination. |
| **Collect extension with DTMF** | HA answers the trunk leg and collects negotiated telephone-event or SIP INFO digits for the configured timeout. | A valid extension wins. With no digits, automation gets a decision point before the fallback. |

**Fallback destination** accepts the same values as the rest of the dial
plan: a phonebook name, HA, an extension, a group, a registered SIP phone, an
Assist extension, a SIP URI or a routable number.

The optional **Allow experimental automation routing overrides** switch is
independent from the incoming mode and is off by default. When enabled:

- Direct mode exposes a 1.5 second `route_requested` decision point before the
  configured default route.
- DTMF mode exposes the same decision point only when the caller enters no
  digits.
- Explicit DTMF digits never pass through automation routing. They always use
  the phonebook, and an unknown explicit extension ends as `route_not_found`
  instead of silently ringing another destination.
- If no matching automation acts during the decision window, the configured
  phonebook route continues unchanged.

Existing configurations migrate transparently. A previous setup with DTMF
enabled and a non-zero timeout becomes DTMF mode. Other setups become Direct
mode. Automation overrides remain disabled until explicitly enabled, while the
existing credentials, target and timeout are preserved.

## Override The Initial Destination

This complete automation routes a trunk call to `Waveshare S3 Audio` before
the default destination. It uses Home Assistant's native Event Entity trigger,
so it needs no Jinja, Call-ID or helper timer:

```yaml
alias: VoIP - Route incoming call to WS3
mode: parallel
max: 10
triggers:
  - trigger: event.received
    target:
      entity_id: event.voip_stack_call
    options:
      event_type:
        - route_requested
actions:
  - action: voip_stack.select_inbound_destination
    data:
      destination: Waveshare S3 Audio
```

Use ordinary Home Assistant conditions between the trigger and action for
presence, time, alarm mode or any other entity. For example, a state condition
can route to an indoor ESP only while someone is home. If the condition is
false, no action runs and the default target takes over when the short decision
window expires.

This action is only for the initial `route_requested` decision. When exactly
one route is waiting, no Call-ID template is required. With concurrent pending
routes, pass the `call_id` from the event explicitly. The configured fallback
remains authoritative when no automation acts. Use `voip_stack.forward` only
after a call has already been delivered to a ringing or connected endpoint.

## Forward An Unanswered HA Call To Assist

Initial routing and no-answer forwarding are separate operations. Once the HA
softphone is ringing, its durable state sensor supports Home Assistant's native
`for:` timing:

```yaml
alias: VoIP - HA unanswered to Assist
mode: parallel
max: 10
triggers:
  - trigger: state
    entity_id: sensor.voip_stack_call_state
    to: ringing
    for: "00:00:30"
actions:
  - action: voip_stack.forward
    data:
      destination: "1666"
      on_failure: resume
```

Replace `1666` with any destination understood by the phonebook. When exactly
one HA-owned call is forwardable, the backend resolves its Call-ID and current
revision itself. The source call remains open while VoIP Stack releases the HA
softphone, cancels any replaced ringing leg with SIP CANCEL, and attaches the
new destination.

If multiple calls are simultaneously forwardable, an advanced automation must
identify the intended `call_id`. Ambiguous requests fail explicitly instead of
guessing.

With multiple logical phones, select the call-state entity attached to the
phone that owns the ringing leg. The migrated default phone deliberately keeps
the compatibility entity ID `sensor.voip_stack_call_state`; additional phones
receive normal generated IDs such as `sensor.test_call_state` (localized HA
installations may use a translated form). Always select the entity from the
phone Device in the automation editor instead of guessing its ID.

Logical ringing is independent from browser connectivity. A browser softphone
that belongs to a ring group is allowed to enter `ringing` while its
connectivity entity says `Disconnected`; no physical card rings, but state
timers and missed-call automations still run. Opening the matching card during
that window makes the call answerable. DND and administratively disabled phones
are not ring candidates.

For example, the Casa phone can fall through to the Cucina tablet without
matching caller names or inspecting the global event stream:

```yaml
alias: VoIP - Casa unanswered to Cucina
mode: parallel
max: 10
triggers:
  - trigger: state
    entity_id: sensor.voip_stack_call_state  # Call state entity on Device Casa
    to: ringing
    for: "00:00:30"
conditions:
  - condition: template
    value_template: "{{ trigger.to_state.attributes.direction == 'incoming' }}"
actions:
  - action: voip_stack.forward
    data:
      destination: Cucina
      on_failure: resume
```

## Native Automation Entities

### Event Entity

Every integration-owned phone Device exposes its own call Event Entity, for
example `event.casa_call` or `event.test_call` (the visible/entity names are
localized). It publishes only occurrences involving that phone. Use it for a
doorbell notification, a missed-call log, or behavior specific to one room.

`event.voip_stack_call` remains the aggregate PBX-wide surface and publishes
stateless occurrences for every HA-owned call:

- `route_requested`, `incoming_call`, `outgoing_call`, `calling`
- `ringing`, `remote_ringing`, `forwarding`
- `answered`, `connected`
- `calling_timeout_requested`, `ringing_timeout_requested`
- `dtmf`
- `ended`, `missed`, `failed`, `state_changed`

Select these through the `event.received` trigger in the automation editor.
Each occurrence includes call metadata such as caller, callee, direction,
route kind, owner and controllability. The aggregate entity is useful for
initial `route_requested` decisions and advanced inspection; prefer the phone's
own Event Entity for room-specific logic.

### Durable State Sensor

Each logical browser/SIP-account phone exposes an enum call-state Sensor Entity.
The default phone keeps `sensor.voip_stack_call_state` for backward
compatibility. Each sensor follows only its phone through ringing, bridging and
Assist. Its stable states are:

- `idle`
- `ringing`
- `calling`
- `remote_ringing`
- `connecting`
- `in_call`
- `terminating`

Attributes include `call_id`, `caller`, `callee`, `direction`, `dialed_target`,
`peer_name`, `sequence`, `revision`, `owner` and `terminal_reason`. Ordinary
single-call automations do not need to read them.

The phone Device itself is the Home Assistant registry container; entities are
its triggerable state/event surfaces. The per-phone Event Entity, durable
sensor, WebSocket stream and card are all derived from the same backend call
session.

## DTMF During A Connected Call

Initial trunk extension selection and established-call DTMF are deliberately
separate:

- Digits used before routing select a phonebook extension and do not become
  in-call automation events.
- During an HA-bridged established call, each negotiated key emits one `dtmf`
  occurrence while audio continues.
- DTMF processing remains HA-side and adds no work to ESP firmware.

This supports actions such as opening a gate when a participant presses a key,
without turning the keypad into a second routing state machine.

## Advanced Concurrency Controls

Every HA-owned logical call has one owner and a monotonic `revision`. Control
changes such as route selection, destination replacement and ownership handoff
advance the revision even if the visible state string stays the same. Delayed
callbacks cannot restore an older state.

For expert scripts that manage several concurrent calls, `call_id`,
`expected_state` and `expected_sequence` remain accepted. Explicit deadlines
also remain available for multi-stage policies, but they are unnecessary for a
normal no-answer forward.

## Boundaries

- Direct ESP-to-ESP calls remain peer-to-peer and observable only. HA cannot
  redirect media it does not own.
- The current operation is an HA B2BUA redirect, not a SIP phone transfer.
- Supported signaling includes INVITE, ACK, BYE, CANCEL, REGISTER, OPTIONS,
  SIP INFO DTMF, RTP telephone-event and peer-initiated UPDATE on HA-owned
  dialogs.
- REFER/NOTIFY transfer, locally originated renegotiation, offerless re-INVITE
  delayed offer/answer, PRACK/100rel and session timers are not implemented.
- Legacy `voip_stack.call_event`, `voip_stack.route_request` and
  `voip_stack.dtmf` bus events remain for compatibility. New automations should
  prefer the entities above.
