# Automation Dial Plan

VoIP Stack keeps the phonebook as the default dial plan. Home Assistant
automations are an optional override: with no matching automation, calls keep
the same routing behavior as before.

The integration exposes one native event entity, `event.voip_stack_call`.
Every occurrence changes its timestamp and publishes an `event_type` plus a
stable call envelope. Use an ordinary state trigger on that entity, then match
the attributes you need.

## Event Types

The event entity advertises these lifecycle types in the automation editor:

- `incoming_call`, `outgoing_call`, `calling`, `ringing`
- `answered`, `connected`
- `calling_timeout_requested`, `ringing_timeout_requested`
- `dtmf`
- `ended`, `missed`, `failed`, `state_changed`

Useful attributes include:

- `call_id`: stable logical call ID used by the actions below.
- `sequence`: increments only when the canonical call state changes.
- `previous_state`, `state`, `direction`, `caller`, `callee`.
- `dialed_target`, `route_kind`, `route_history`.
- `automation_control`: `routable`, `ha_anchored`, or `observed`.

Copy both `state` and `sequence` into routing actions. They prevent an old or
slow automation run from changing a call that has already answered, ended or
moved to another route.

## Immediate Conditional Forward

This example overrides the normal route only for an incoming trunk call from a
specific caller. All other calls continue through the phonebook.

```yaml
alias: VoIP - Send one caller to Spotpear
mode: parallel
triggers:
  - trigger: state
    entity_id: event.voip_stack_call
conditions:
  - condition: template
    value_template: >-
      {{ trigger.to_state.attributes.event_type == 'incoming_call'
         and trigger.to_state.attributes.direction == 'incoming'
         and trigger.to_state.attributes.automation_control == 'routable'
         and trigger.to_state.attributes.caller == '426' }}
actions:
  - action: voip_stack.forward
    data:
      call_id: "{{ trigger.to_state.attributes.call_id }}"
      destination: Spotpear
      expected_state: "{{ trigger.to_state.attributes.state }}"
      expected_sequence: "{{ trigger.to_state.attributes.sequence }}"
      on_failure: resume
```

`on_failure: resume` restores the original HA ringing call if the selected
phone is unreachable. `terminate` ends it; `busy` ends it with a busy result.

## Forward Unanswered HA Calls To Assist

Timeouts are explicit and event-only. Arming one never forwards or terminates a
call by itself. The first automation starts a 30-second deadline whenever the
HA softphone begins ringing:

```yaml
alias: VoIP - Arm HA ringing timeout
mode: parallel
triggers:
  - trigger: state
    entity_id: event.voip_stack_call
conditions:
  - condition: template
    value_template: >-
      {{ trigger.to_state.attributes.event_type == 'ringing'
         and trigger.to_state.attributes.direction == 'incoming'
         and trigger.to_state.attributes.automation_control in ['routable', 'ha_anchored'] }}
actions:
  - action: voip_stack.set_deadline
    data:
      call_id: "{{ trigger.to_state.attributes.call_id }}"
      phase: ringing
      timeout: 30
      expected_state: "{{ trigger.to_state.attributes.state }}"
      expected_sequence: "{{ trigger.to_state.attributes.sequence }}"
```

The second automation moves the same still-active call to the configured Assist
extension. If the call answered, ended or changed route during those 30
seconds, the deadline becomes stale and emits nothing.

```yaml
alias: VoIP - Unanswered HA call to Assist
mode: parallel
triggers:
  - trigger: state
    entity_id: event.voip_stack_call
conditions:
  - condition: template
    value_template: >-
      {{ trigger.to_state.attributes.event_type == 'ringing_timeout_requested' }}
actions:
  - action: voip_stack.forward
    data:
      call_id: "{{ trigger.to_state.attributes.call_id }}"
      destination: "1666"
      expected_state: "{{ trigger.to_state.attributes.armed_state }}"
      expected_sequence: "{{ trigger.to_state.attributes.armed_sequence }}"
      on_failure: resume
```

Replace `1666` with the Assist extension configured in VoIP Stack. Assist is a
normal phonebook destination; the automation does not need a separate media or
SIP path.

## Routing Boundaries

- Calls anchored by HA can be forwarded repeatedly while ringing. VoIP Stack
  sends SIP CANCEL to the old destination before starting the new leg.
- Direct ESP-to-ESP calls remain peer-to-peer. HA can observe their mirrored
  state, but cannot move media it does not own; their `automation_control` is
  `observed`.
- Initial trunk extension selection and established-call DTMF are separate.
  Digits used to choose an extension do not become automation key events.
- During an HA-bridged established call, every negotiated DTMF key emits one
  `dtmf` event without interrupting media.
- The legacy `voip_stack.call_event`, `voip_stack.route_request` and
  `voip_stack.dtmf` bus events remain available for compatibility. New
  automations should prefer `event.voip_stack_call`.
