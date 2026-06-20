# runtime_fsm

`runtime_fsm` is a generic ESPHome reducer for devices where many independent
runtime facts must drive the same outputs.

It is not a Voice Assistant component, not a LED component and not an intercom
component. It is a small programmable state computer:

1. YAML tells it which facts can be active.
2. YAML tells it which events turn those facts on or off.
3. YAML tells it how active facts map to outputs.
4. The component computes one committed snapshot and runs the matching ESPHome
   automations.

The maintained full-experience intercom profiles use it for LED status,
display status, media ducking, ringtone and timer alarm arbitration, but those
names are just YAML policy names. A different project can use the same reducer
for relays, text sensors, display pages, motor modes, HVAC presets or any other
stateful output.

## Why This Exists

ESPHome automations are event-driven. That is good, but composite devices often
receive overlapping events:

- media is playing;
- a timer starts ringing;
- Home Assistant disconnects;
- a Voice Assistant response is queued;
- intercom starts ringing;
- mute switches change;
- an old media source resumes after an announcement ends.

If every callback directly writes LEDs, display text and audio ducking, the
last callback wins even when it is not the most important one. This creates
short flashes, stuck LEDs, released ducking during TTS, wrong ringtone state,
or UI that says "media" while an intercom call is active.

`runtime_fsm` fixes the ownership model:

- callbacks report facts;
- the reducer owns the combined state;
- outputs are derived from one snapshot;
- priority is explicit and readable;
- output automations only run when the resolved policy changes.

It does not guess time, sleep, poll or wait for hardcoded delays. Normal
decisions are made from events and current activities.

## Core Concepts

### Activity

An activity is a named boolean fact:

```yaml
activities:
  media:
    priority: 100
  intercom_streaming:
    priority: 700
  va_responding:
    priority: 800
```

Activities can overlap. If media is playing while Voice Assistant is speaking,
both `media` and `va_responding` can be active at the same time. The reducer
does not collapse that into one state too early.

### Event

An event is an input transition. It changes one or more activities:

```yaml
events:
  media_playing:
    activate: media
  media_idle:
    deactivate: media
```

Callbacks invoke events:

```yaml
on_play:
  - runtime_fsm.event:
      id: runtime
      event: media_playing
```

### Policy

A policy is a named output decision. The reducer does not reserve policy names.
You choose them:

```yaml
policies:
  led_status:
    output: g_led_state
  audio_policy:
    output: g_audio_mode
  display_page:
    output: g_display_page
```

Activities declare which policy value they want:

```yaml
activities:
  media:
    priority: 100
    policies:
      led_status: media
      audio_policy: normal
  va_responding:
    priority: 800
    policies:
      led_status: responding
      audio_policy: duck
```

If multiple active activities set the same policy, the highest-priority active
activity wins for that policy. This is how intercom can override the LED while
media remains active underneath.

### Action

An action is a named ESPHome automation the reducer can request after a state
change:

```yaml
actions:
  voice_start:
    - voice_assistant.start:
  voice_stop:
    - voice_assistant.stop:
```

Events can request actions:

```yaml
events:
  wake_word:
    activate: va_starting
    action: voice_start
```

The action runs after the reducer commits the new snapshot. This avoids
publishing half-updated state.

## Runtime Order

When `runtime_fsm.event` is called, the component does this:

1. Finds the configured event.
2. Tests event `cases` against the current activity snapshot.
3. Applies the first matching case, or the default event rule.
4. Recomputes derived activities.
5. Resolves every policy from active activities and explicit priorities.
6. Writes configured global outputs.
7. Fires `on_change` and per-policy-value automations only when a policy
   actually changed.
8. Runs the requested named action, if any.
9. Processes any event that arrived reentrantly while outputs were running.

The important property is "run to completion": a policy automation can trigger
another event, but that event is queued until the current commit is finished.

## A First Useful Example

This example drives a single LED output and an audio ducking flag. It is small
enough to understand but already solves a real priority problem: intercom and
assistant response override media.

```yaml
globals:
  - id: g_activity_mask
    type: uint32_t
    restore_value: false
    initial_value: "0"
  - id: g_fsm_seq
    type: uint32_t
    restore_value: false
    initial_value: "0"
  - id: g_led_state
    type: int
    restore_value: false
    initial_value: "0"
  - id: g_audio_duck
    type: int
    restore_value: false
    initial_value: "0"

script:
  - id: apply_led
    parameters:
      value: int
    then:
      - logger.log:
          format: "LED policy resolved to %d"
          args: [value]

runtime_fsm:
  id: runtime
  debug: false
  state_outputs:
    activity_mask: g_activity_mask
    sequence: g_fsm_seq

  activities:
    idle:
      initial: true
      priority: 0
      policies:
        led_status: idle
        audio_policy: normal

    media:
      priority: 100
      policies:
        led_status: media
        audio_policy: normal

    assistant_response:
      priority: 800
      policies:
        led_status: assistant
        audio_policy: duck

    intercom_streaming:
      priority: 900
      policies:
        led_status: intercom
        audio_policy: duck

  events:
    media_started:
      activate: media
    media_stopped:
      deactivate: media
    assistant_started:
      activate: assistant_response
    assistant_finished:
      deactivate: assistant_response
    call_started:
      activate: intercom_streaming
    call_ended:
      deactivate: intercom_streaming

  policies:
    led_status:
      output: g_led_state
      on_change:
        - script.execute:
            id: apply_led
            value: !lambda "return value;"
      values:
        idle: 0
        media: 12
        assistant: 7
        intercom: 10

    audio_policy:
      output: g_audio_duck
      values:
        normal:
          value: 0
          then:
            - logger.log: "Audio policy: normal"
        duck:
          value: 1
          then:
            - logger.log: "Audio policy: duck"
```

If media is active and `assistant_started` arrives, both facts are true:

```text
media=true, assistant_response=true
```

The reducer resolves:

```text
led_status=assistant
audio_policy=duck
```

When `assistant_finished` arrives, media was never forgotten, so the reducer
returns to:

```text
led_status=media
audio_policy=normal
```

No callback needs to remember "what was underneath".

## How To Design A Reducer Configuration

Start from the facts, not from the outputs.

### Step 1: List facts that can be true together

Good activity names describe reality:

```text
media
announcement
timer_ringing
intercom_ringing
intercom_streaming
va_listening
va_thinking
va_responding
mic_muted
speaker_muted
no_wifi
no_ha
```

Avoid names that are already output decisions:

```yaml
blue_led
show_page_3
duck_now
```

Those should be policy values, not activities.

### Step 2: Assign priority by policy intent

Priority is only used when two active activities set the same policy.
Typical full-device ordering is:

```text
boot / connectivity / fatal error
mute
voice assistant listening / thinking / responding
intercom ringing / streaming
timer ringing
announcement
media
idle
```

You do not need numeric gaps to be perfect. Leave space so future activities can
fit between existing ones:

```yaml
media: 100
announcement: 200
timer_ringing: 500
intercom_ringing: 700
va_responding: 800
no_ha: 980
boot: 1000
```

### Step 2b: Use groups for mutually exclusive phases

Some facts should not overlap. For example, a Voice Assistant cannot be
listening and thinking at the same time. Put those activities in a group:

```yaml
runtime_fsm:
  groups:
    va: [va_starting, va_listening, va_thinking, va_responding]
```

When an activity in a group is activated, the reducer automatically deactivates
the other activities in that same group. Other activities outside the group,
such as `media` or `intercom:streaming`, are not touched.

### Step 3: Define output policies

Policies should be named after the output domain:

```yaml
led_status
display_status
audio_policy
ringtone
timer_alarm
```

Then map human-readable policy values to integers or automations:

```yaml
policies:
  led_status:
    output: g_led_state
    values:
      idle: 0
      media: 12
      responding: 7
      intercom_ringing: 8

  ringtone:
    values:
      stop:
        then:
          - script.stop: intercom_ringing_loop
      play:
        then:
          - script.execute: intercom_ringing_loop
```

If a policy must stop something, declare the stop value explicitly on the
fallback activity. The reducer does not infer that a missing policy means
"stop".

### Step 4: Convert callbacks into events

Keep component callbacks short. They should report what happened:

```yaml
media_player:
  - platform: speaker_source
    id: speaker_media_player
    on_play:
      - runtime_fsm.event:
          id: runtime
          event: media_playing
    on_idle:
      - runtime_fsm.event:
          id: runtime
          event: media_idle
```

The callback should not also set LEDs, display pages and ducking. That would
create a second state machine beside the reducer.

## Event Cases

Cases let one event behave differently depending on the current activities.
This is useful for barge-in, cancellation and cleanup.

```yaml
events:
  wake_word:
    activate: va_starting
    action: voice_start
    cases:
      - any: [va_responding, announcement]
        activate: va_starting
        deactivate: announcement
        action: voice_restart_response
      - any: [va_listening, va_thinking]
        activate: va_starting
        action: voice_restart_live
```

The reducer evaluates cases before the default event rule:

- if a response or announcement is active, the wake word restarts the response;
- if the assistant is already listening/thinking, it restarts the live pipeline;
- otherwise it uses the default `voice_start` action.

Case predicates:

```yaml
any: activity_or_list
all: activity_or_list
none: activity_or_list
```

Case effects:

```yaml
activate: activity_or_list
deactivate: activity_or_list
action: action_name
```

## Grouped Updates

When one callback needs to change several activities, use
`runtime_fsm.set_activities` so they are committed as one reducer snapshot:

```yaml
- runtime_fsm.set_activities:
    id: runtime
    set:
      media: false
      announcement: false
      va_responding: false
```

This avoids visible intermediate states such as "media" for one loop before
"idle".

For one fact, use `runtime_fsm.set_activity`:

```yaml
- runtime_fsm.set_activity:
    id: runtime
    activity: no_ha
    active: true
```

## Auto Events

`auto_events` defaults to `true`. With it enabled, every activity also gets a
simple event with the same name that activates it:

```yaml
activities:
  no_ha:
    priority: 980

# This exists automatically:
- runtime_fsm.event:
    id: runtime
    event: no_ha
```

For larger configurations, prefer explicit `events:` because they document what
each callback means. Disable auto events if you want every accepted event name
to be declared manually:

```yaml
runtime_fsm:
  auto_events: false
```

## Derived Activities

Derived activities are computed from other activities. They are useful when a
combined state should override both inputs.

```yaml
runtime_fsm:
  derived_activities:
    - name: both_muted
      when:
        all_active: [mic_muted, speaker_muted]

  activities:
    both_muted:
      priority: 960
      policies:
        led_status: muted
        display_status: muted
```

Do not send events for derived activities. They are owned by the reducer.

## Optional Intercom Observer

The component is generic, but this project also provides an optional
`intercom_api` observer because intercom state is already centralized in the
native component.

```yaml
runtime_fsm:
  id: runtime
  intercom:
    id: intercom
    activity_prefix: "intercom:"
    states:
      ringing:
        priority: 700
        policies:
          led_status: intercom_ringing
          display_status: intercom_ringing
          audio_policy: duck
          ringtone: play
      streaming:
        priority: 650
        policies:
          led_status: intercom_streaming
          display_status: intercom_streaming
          audio_policy: duck
          ringtone: stop
```

This automatically creates activities named `intercom:ringing`,
`intercom:outgoing`, `intercom:streaming`, etc. If `intercom:` is omitted, no
intercom observer code is compiled.

Intercom signaling and transport still belong to `intercom_api`; `runtime_fsm`
only observes state and resolves outputs.

## Actions Reference

### `runtime_fsm.event`

Runs a configured event:

```yaml
- runtime_fsm.event:
    id: runtime
    event: media_playing
```

`event` is templatable:

```yaml
- runtime_fsm.event:
    id: runtime
    event: !lambda 'return id(my_sensor).state ? "active" : "idle";'
```

Optional fields:

```yaml
dump: true       # dump reducer state after the event
reason: "debug" # text included in debug dump
```

### `runtime_fsm.set_activity`

Sets one activity directly:

```yaml
- runtime_fsm.set_activity:
    id: runtime
    activity: no_wifi
    active: true
```

`activity` and `active` are templatable.

### `runtime_fsm.set_activities`

Sets several activities atomically:

```yaml
- runtime_fsm.set_activities:
    id: runtime
    set:
      media: false
      announcement: false
      va_responding: false
```

### `runtime_fsm.request_action`

Runs a named action without changing activities:

```yaml
- runtime_fsm.request_action:
    id: runtime
    action: voice_stop
```

### `runtime_fsm.dump`

Logs the current reducer snapshot:

```yaml
- runtime_fsm.dump:
    id: runtime
    reason: "button pressed"
```

### `runtime_fsm.is_active`

Condition for automations:

```yaml
if:
  condition:
    runtime_fsm.is_active:
      id: runtime
      activity: intercom:streaming
  then:
    - logger.log: "Intercom is active"
```

## Policies Reference

A policy value may be a plain integer:

```yaml
policies:
  led_status:
    output: g_led_state
    values:
      idle: 0
      listening: 6
```

Or an object with `value` and `then`:

```yaml
policies:
  audio_policy:
    output: g_ducking_active
    values:
      normal:
        value: 0
        then:
          - mixer_speaker.apply_ducking:
              id: media_mixer_input
              decibel_reduction: 0
              duration: 500ms
      duck:
        value: 1
        then:
          - mixer_speaker.apply_ducking:
              id: media_mixer_input
              decibel_reduction: 20
              duration: 200ms
```

`on_change` receives the resolved integer as `value`:

```yaml
script:
  - id: apply_led_status
    parameters:
      value: int
    then:
      - logger.log:
          format: "LED status %d"
          args: [value]

runtime_fsm:
  policies:
    led_status:
      output: g_led_state
      on_change:
        - script.execute:
            id: apply_led_status
            value: !lambda "return value;"
      values:
        idle: 0
        media: 12
```

Use `on_change` when one script can handle all values. Use per-value `then`
when every value needs different ESPHome actions.

## Resource Model And Limits

The component is designed for embedded use. It uses fixed-size arrays instead
of heap-heavy dynamic containers in the hot path.

Current limits:

```text
activities:            32
policies resolved:      8
policies per activity:  4
event direct updates:   64
event case rules:       32
actions:               16
queued reentrant events: 16
```

If you hit one of those limits, it is usually a sign that the configuration can
be simplified by using groups, derived activities, or fewer output policies.

## Debugging

Set `debug: true` during profile development:

```yaml
runtime_fsm:
  id: runtime
  debug: true
```

Debug mode compiles extra reducer logs:

- incoming events;
- matching cases;
- activity mask changes;
- derived activity changes;
- policy changes;
- queued reentrant events;
- sequence number.

You can dump state manually:

```yaml
- runtime_fsm.dump:
    id: runtime
    reason: "after wake word"
```

Keep `debug: false` for release YAMLs unless you want verbose runtime logs.

## Host Test

The pure reducer logic can be tested without flashing an ESP:

```bash
g++ -std=c++17 -Wall -Wextra -I. \
  tests/runtime_fsm_state_test.cpp \
  esphome/components/runtime_fsm/runtime_fsm_state.cpp \
  -o /tmp/runtime_fsm_state_test
/tmp/runtime_fsm_state_test
```

The maintained test covers:

- policy priority;
- grouped activity updates;
- explicit stop policies;
- derived activities;
- media plus announcement;
- Voice Assistant response over media;
- intercom priority over media;
- cleanup back to the underlying state.

## Design Checklist

Before flashing a complex profile, check these points:

- Every callback reports an event or activity, instead of writing outputs
  directly.
- There is one owner for LED/display/audio policy.
- Activities are facts, not output names.
- Policies are output domains, not input events.
- Every long-running output has an explicit stop value.
- Priority is readable and documented by the YAML order.
- Barge-in or restart paths use event cases, not fixed delays.
- Multi-activity cleanup uses `set_activities`.
- Debug logs show one transition for one logical event.

## Anti-Patterns

Avoid these:

```yaml
# Bad: callback writes LED directly and also tells the reducer.
on_play:
  - light.turn_on: status_led
  - runtime_fsm.event:
      id: runtime
      event: media_playing
```

```yaml
# Bad: activity is an output decision, not a fact.
activities:
  blue_led:
    priority: 800
```

```yaml
# Bad: missing stop policy leaves the previous automation alive.
activities:
  ringtone:
    policies:
      ringtone: play
  idle:
    policies:
      led_status: idle
      # ringtone: stop is missing
```

```yaml
# Bad: delay guesses lifecycle instead of using component events.
on_tts_start:
  - delay: 2s
  - runtime_fsm.event:
      id: runtime
      event: tts_finished
```

Prefer event-driven terminal states:

```yaml
on_announcement:
  - runtime_fsm.event:
      id: runtime
      event: announcement_started
on_idle:
  - runtime_fsm.event:
      id: runtime
      event: media_idle
```

## Maintained Full Profiles

The maintained full-experience profiles keep the reusable reducer configuration
in:

```text
packages/runtime_fsm/full_controller.yaml
```

Voice Assistant, media player, timer, mute, connectivity and intercom packages
only forward events into that reducer. LED/LVGL/audio output scripts consume
the resolved policy globals. This keeps hardware-specific rendering separate
from runtime state ownership.

Use that package as a larger real-world example after reading the smaller
examples above.
