# Programmable Home Assistant Dial Plan — 2026.7.2 Plan

## Goal

Keep the shared phonebook as the complete default dial plan, while allowing
Home Assistant automations to observe and override individual calls using
standard SIP-shaped primitives.

The automation layer must not become another phone engine. SIP transactions,
dialogs, media bridges and deadlines remain owned by VoIP Stack. Home Assistant
receives immutable call snapshots and invokes explicit actions against a
`call_id`.

## Design Rules

1. **Phonebook first.** With no automation, every current route behaves exactly
   as it does today.
2. **One call owner.** Automations request decisions; they never own SIP
   sockets, timers or RTP.
3. **Per-call variables, not mutable globals.** PBX-style variables are a
   snapshot attached to one call and one event.
4. **Standard signaling.** Early forwarding, CANCEL, BYE, REFER and redirect
   are distinct operations and must not be represented by one ambiguous
   `forward` flag.
5. **Event driven.** No card polling and no temporary binary sensors pretending
   to be events.
6. **Race safe.** Every mutating action can carry the expected state and event
   sequence so a stale automation cannot forward a call that was already
   answered.
7. **Bounded decisions.** Automation override windows have explicit deadlines;
   silence always falls back to a documented default.
8. **No secrets.** Passwords, digest responses, nonces and authorization
   headers never enter HA events or templates.

## Home Assistant Surface

Home Assistant currently recommends event entities for browsable integration
events. New custom device automations are discouraged; they are only wrappers
around events, states and actions. VoIP Stack should therefore expose:

- `event.voip_stack_call`: call lifecycle and routing events;
- `event.voip_stack_dtmf`: one event per in-call DTMF key;
- normal `voip_stack.*` actions, described in `services.yaml`, available in the
  automation editor and fully templatable;
- existing call state entities for current status and diagnostics;
- the existing `voip_stack.call_event` bus event during a compatibility period.

The event entity is a discovery surface, not history storage. Recorder and
automation traces provide history; `CallRegistry` remains runtime authority.

## Canonical Call Snapshot

Every lifecycle event uses the same versioned envelope. Missing data is an
empty value, not a renamed field.

```yaml
schema_version: 1
event: ringing
sequence: 7
timestamp: "2026-07-12T22:30:00.000Z"

call_id: "provider-call-id"
session_id: "canonical-session-id"
leg_id: "outbound-leg-call-id"
parent_call_id: ""

direction: incoming       # incoming | outgoing | internal
origin: trunk             # trunk | ha | esp | registered | direct
scope: session            # session | leg | route | media
state: ringing
previous_state: connecting

from: "+390000000000"
from_display: "Daniele"
from_uri: "sip:+390000000000@provider.example"
to: "420"
to_uri: "sip:420@home.example"
request_uri: "sip:420@home.example"

requested_target: "Casa"
resolved_target: "Home Assistant"
connected_party: ""
answered_by: ""

source_host: "192.0.2.10"
source_port: 5060
transport: tcp
user_agent: "provider"
trunk: "home_trunk"
account: ""

route_kind: trunk
route_source: phonebook
route_hops: 0
route_history: []

sip_status_code: 180
sip_reason: "Ringing"
terminal_reason: ""

calling_deadline: ""
ringing_deadline: "2026-07-12T22:30:30.000Z"
decision_deadline: ""

tx_format: "PCMA/8000/1/20"
rx_format: "PCMA/8000/1/20"
```

### Identity meanings

- `from` / `from_uri`: original SIP caller identity. A phonebook display name
  may enrich it but never erase the original value.
- `to` / `to_uri`: original SIP To identity.
- `request_uri`: actual target of the current SIP request.
- `requested_target`: name, extension, URI or number dialed by the user.
- `resolved_target`: dial-plan destination selected for the current leg.
- `connected_party`: real peer after a bridge is established.
- `answered_by`: endpoint that won a ring group or answered the call.
- `trunk_username` and digest auth username remain internal configuration;
  only a non-secret trunk identifier is exposed.

These fields replace vague uses of `caller`, `callee`, `peer` and “real
callee” without removing the old aliases during migration.

## Lifecycle Events

Initial event types:

| Event | Meaning |
|---|---|
| `created` | Canonical session allocated. |
| `route_requested` | Default route is known; automation override window open. |
| `calling` | Outbound INVITE active, no remote ringing response yet. |
| `progress` | SIP provisional progress such as 100 or 183. |
| `ringing` | A local or remote endpoint is actually ringing. |
| `calling_timeout_requested` | Calling deadline expired; timeout decision window open. |
| `ringing_timeout_requested` | Ringing deadline expired; timeout decision window open. |
| `forwarding` | Existing call is acquiring a replacement destination leg. |
| `answered` | A destination answered; media setup may still be completing. |
| `connected` | Bidirectional media path established. |
| `dtmf` | One in-call key received; also published by the DTMF event entity. |
| `ending` | CANCEL/BYE/decline teardown started. |
| `ended` | Final immutable result and terminal reason. |

Raw SIP messages are not public automation events. Stable semantic events are.
Debug mode may log sanitized headers separately.

## Timeout Semantics

### Calling timeout

Starts when an outbound INVITE transaction is sent. It ends at the first
meaningful ringing response (`180` or ringing-capable `183`), answer, rejection
or cancellation. `100 Trying` proves that the server received the request but
does not mean the destination is ringing.

### Ringing timeout

Starts when an incoming target is told to ring or an outgoing destination
returns ringing progress. It ends on answer, decline, cancellation, forwarding
or terminal failure.

### Timeout decision window

At the deadline VoIP Stack emits `*_timeout_requested` and opens a short,
explicit decision window. During this window the existing call and early SIP
dialog stay alive.

An automation may:

- forward the existing call;
- answer on HA;
- decline/busy/cancel it;
- extend or replace the deadline;
- accept the configured default timeout behavior.

If no decision arrives, VoIP Stack terminates the call with terminal reason
`calling_timeout` or `ringing_timeout`. The decision-window duration is a
configuration value, not an undocumented sleep.

Timeout values use monotonic runtime deadlines. Wall-clock timestamps are only
published for templates and diagnostics.

### Configuration hierarchy

Most specific wins:

1. action override for this `call_id`;
2. phonebook contact/group timeout policy;
3. endpoint timeout policy;
4. integration defaults.

`0`/`never` disables an optional guard. SIP protocol hard limits still apply.

## Actions

### Existing actions retained

- `voip_stack.call`
- `voip_stack.answer`
- `voip_stack.decline`
- `voip_stack.hangup`
- `voip_stack.route`
- `voip_stack.forward`

### Required extensions

#### `voip_stack.forward`

Forward an existing pending or ringing call by `call_id` to a phonebook name,
extension, group, SIP URI or trunk number. VoIP Stack creates the outbound leg,
keeps the original early call alive and bridges it only when the new target
answers.

Fields:

```yaml
call_id: required
destination: required
strategy: bridge            # bridge initially; redirect later
on_failure: resume           # resume | terminate | busy
expected_state: ringing
expected_sequence: 7
```

This is the primitive needed for “ring HA for 30 seconds, then forward the
same call to Assist extension 1666”.

#### `voip_stack.set_deadline`

Set, extend or disable a per-call calling/ringing deadline.

```yaml
call_id: required
phase: ringing              # calling | ringing
timeout: 30                 # seconds, or 0/never
expected_sequence: 7
```

#### `voip_stack.continue`

Accept the default route or default timeout behavior for a pending decision.
This avoids overloading `route(action=default)` with unrelated phases.

#### Future, not silently emulated

- `voip_stack.redirect`: SIP 3xx before answer, only where the caller can
  follow Contact redirects.
- `voip_stack.transfer`: established-dialog REFER with NOTIFY outcome.
- attended transfer: REFER + Replaces.

Forwarding a B2BUA call is not called REFER unless REFER is actually sent.

## Example: Secretary After HA Does Not Answer

Target behavior:

```yaml
triggers:
  - trigger: event.received
    target:
      entity_id: event.voip_stack_call
    options:
      event_type:
        - ringing_timeout_requested

conditions:
  - condition: template
    value_template: >-
      {{ trigger.event.data.direction == 'incoming'
         and trigger.event.data.resolved_target == 'Home Assistant' }}

actions:
  - action: voip_stack.forward
    data:
      call_id: "{{ trigger.event.data.call_id }}"
      destination: "1666"
      strategy: bridge
      on_failure: terminate
      expected_state: ringing
      expected_sequence: "{{ trigger.event.data.sequence }}"

mode: parallel
max: 10
```

The exact event-entity trigger payload will be verified against the HA version
used during implementation. The stable contract is the event data envelope and
the action fields above.

## Additional Scenarios Enabled

- Daytime: ring HA and selected ESPs; nighttime: route directly to Assist.
- Unknown external caller: ring HA briefly, then forward to a polite pipeline.
- Known family caller: ring a dedicated group longer before fallback.
- Door station: if unanswered, forward to Assist with original caller identity.
- Busy HA: immediately forward to another registered endpoint.
- DTMF during a connected call: run an HA automation without changing legs.
- Conditional outbound routing: choose trunk or block public calls based on
  time, presence, alarm state or caller policy.
- Retry/failover: if one destination returns busy/unreachable, try a second
  phonebook target while preserving route history and loop limits.

## Loop And Safety Controls

- Maximum route hops per session, initially 8.
- Reject a destination already present in `route_history` unless explicitly
  allowed.
- Idempotent action result when the requested decision was already applied.
- Reject stale `expected_state` or `expected_sequence` with a clear HA action
  error.
- Unknown callers remain valid SIP callers; security policies are optional
  automation conditions, not a hardcoded allow-list.
- Public-number forwarding requires a configured/registered trunk.
- Examples involving locks, gates or alarms must filter caller, callee and
  call direction; DTMF alone is not authentication.

## ESP Endpoint Hooks

The ESP component already supports `ringing_timeout` and `calling_timeout` at
the configuration level. The 2026.7.2 work should make their semantics match
HA and expose explicit callbacks:

- `on_calling_timeout`
- `on_ringing_timeout`

Both report the terminal phase/reason through the normal VoIP state surface.
They must not create an ESP-only routing engine. If HA is present, the same
semantic event is mirrored to the canonical call event; standalone ESP YAML may
still use the local callback for display, LEDs or a deliberate local action.

## Compatibility

- Existing phonebook routing remains unchanged.
- Existing `voip_stack.call_event` and payload aliases remain for at least one
  release with deprecation notes.
- Existing `route_requested` automations continue working.
- New event entities and actions are additive.
- Default timeout behavior matches current behavior unless the user enables an
  automation decision policy.
- Card remains a view/controller and contains no routing decisions.

## Implementation Phases

### Phase 1 — Canonical session envelope

- Add versioned snapshot builder in one module.
- Add per-session monotonic sequence.
- Normalize identity, target, route and terminal fields.
- Route all current event producers through it.

### Phase 2 — HA event entities and action descriptions

- Add call and DTMF event entities.
- Keep compatibility bus events.
- Complete `services.yaml`, translations and action response/errors.

### Phase 3 — Deadline engine

- Per-leg calling and ringing deadlines in `CallRegistry` sessions.
- Timeout decision future with explicit expiration.
- Default termination and stale-decision protection.

### Phase 4 — Forward existing call

- Reuse the current B2BUA outbound-leg/relay primitive.
- Preserve original incoming leg until the replacement answers.
- Support failure policy, route history and loop prevention.

### Phase 5 — ESP parity

- Align timeout definitions in `esphome-voip-stack`.
- Add timeout callbacks and canonical HA state propagation.
- Update maintained YAML examples without adding lambdas where native actions
  exist.

### Phase 6 — Qualification

- Transaction tests for every timeout state and race boundary.
- Automation tests with concurrent call IDs and stale actions.
- Real calls: trunk → HA → Assist fallback; registered phone → HA fallback;
  ESP/HA/group normal calls unchanged.
- Verify CANCEL/BYE/487, RTP ownership, port release and card state.
- Document event payloads, actions, recipes and security guidance.

## Explicitly Out Of Scope For The First Slice

- Full FreeSWITCH variable compatibility.
- Arbitrary SIP header injection from templates.
- Attended transfer before REFER/Replaces is implemented and tested.
- IVR/audio prompt authoring engine.
- Replacing the phonebook with automation-only routing.

