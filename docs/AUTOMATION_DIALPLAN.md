# Automation Dial Plan

VoIP Stack keeps the phonebook as the complete default dial plan. Home
Assistant automations are optional routing overrides: when no automation acts,
calls follow the phonebook exactly as before.

The normal automation surface uses two native Home Assistant entities:

- `sensor.voip_stack_call_state` is the durable HA softphone state. Use an
  ordinary state trigger, including `for:`, for ringing and no-answer policies.
- `event.voip_stack_call` publishes stateless call occurrences. Use Home
  Assistant's `event.received` trigger to select an event type in the automation
  editor.

The card, sensor, event entity and WebSocket API are all fed by the same backend
call session. The Lovelace card does not implement a separate dial plan.

## Forward An Unanswered HA Call To Assist

This is the complete automation. No template, Call-ID, sequence variable,
deadline helper or second automation is required:

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

Replace `1666` with any name, extension, number or SIP URI understood by the
phonebook. When exactly one call is forwardable, `voip_stack.forward` resolves
it automatically and takes its current state/revision guards from the backend.
`on_failure: resume` restores the original HA ringing call if the new
destination is unreachable. `terminate` ends it; `busy` returns busy.

If multiple calls are simultaneously forwardable, specify `call_id` explicitly
or route from a call-specific advanced automation. The service rejects an
ambiguous request instead of guessing.

## React To Call Events

The event entity advertises these types in Home Assistant:

- `route_requested`, `incoming_call`, `outgoing_call`, `calling`
- `ringing`, `remote_ringing`, `forwarding`
- `answered`, `connected`
- `calling_timeout_requested`, `ringing_timeout_requested`
- `dtmf`
- `ended`, `missed`, `failed`, `state_changed`

Example using Home Assistant's native Event Entity trigger:

```yaml
triggers:
  - trigger: event.received
    entity_id: event.voip_stack_call
    event_type: route_requested
```

The occurrence includes caller, callee, direction, route kind, ownership and
controllability. Use normal Home Assistant conditions for time, presence,
alarm mode and other entities. Caller-pattern conditions may still use a short
template because the caller is event data, not persistent configuration.

## Durable Call State

`sensor.voip_stack_call_state` has these stable states:

- `idle`
- `ringing`
- `calling`
- `remote_ringing`
- `connecting`
- `in_call`
- `terminating`

Its attributes include `call_id`, `caller`, `callee`, `direction`,
`dialed_target`, `peer_name`, `sequence`, `revision`, `owner` and
`terminal_reason`. The ordinary no-answer flow does not need to read them.

## Advanced Concurrency Guards

Each HA-owned logical call has one owner and a monotonic `revision`. The
revision advances for control changes such as route selection, destination
replacement and ownership handoff, even when the public state string remains
unchanged. Stale callbacks cannot restore an older state.

For expert scripts managing several concurrent calls, `call_id`,
`expected_state` and `expected_sequence` remain accepted. Explicit deadlines
also remain available for multi-stage policies, but are not needed for a normal
ringing timeout.

## Routing Boundaries

- Calls anchored by HA can be redirected repeatedly while ringing. VoIP Stack
  sends SIP CANCEL to the replaced destination before starting the new leg.
- Direct ESP-to-ESP calls remain peer-to-peer. HA can observe their mirrored
  state, but cannot redirect media it does not own.
- Initial trunk extension selection and established-call DTMF are separate.
  Digits used to select an extension do not become in-call automation events.
- During an HA-bridged established call, each negotiated DTMF key emits one
  `dtmf` occurrence without interrupting media.
- The current feature is an HA B2BUA redirect, not a SIP phone transfer.
  INVITE, ACK, BYE, CANCEL, REGISTER, OPTIONS, SIP INFO DTMF and RTP
  telephone-event are supported. REFER/NOTIFY transfer, complete SDP
  hold/resume, PRACK/100rel, UPDATE and session timers are not currently
  implemented.
- Legacy `voip_stack.call_event`, `voip_stack.route_request` and
  `voip_stack.dtmf` bus events remain for compatibility. New automations should
  prefer the entities above.
