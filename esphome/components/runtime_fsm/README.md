# runtime_fsm

`runtime_fsm` is a small ESPHome state reducer for composite devices where many
independent runtime facts must drive one set of outputs.

It is intentionally generic. The component does not know about Voice Assistant,
LEDs, LVGL, media players, timers or ducking unless the YAML configuration
teaches it those names. The only project-specific helper is the optional
`intercom:` observer, which can read `intercom_api` call state and map it into
normal activities such as `intercom:ringing`.

Use it when direct YAML callbacks start racing each other. A full voice device
can receive these events at nearly the same time:

- media starts or resumes underneath a TTS announcement;
- a Voice Assistant response is queued before the audio source starts playing;
- wake word barge-in cancels an old response and starts a new one;
- intercom ringing or streaming preempts media;
- timers, mute switches and connectivity states need visible priority.

Without one reducer, each callback tends to write LEDs, display state and audio
ducking independently. `runtime_fsm` keeps those decisions in one place.

## Model

The reducer has four concepts:

- **activities**: named booleans, for example `media`, `va_responding`,
  `timer_ringing`, `no_ha`.
- **events**: named input transitions, for example `media_playing`,
  `wake_word`, `ha_disconnected`.
- **policies**: derived output names, for example `led_status`,
  `display_status`, `audio_policy`.
- **actions**: named ESPHome automations the reducer may request after a state
  change.

On every event the component:

1. applies matching event cases;
2. updates activities as one snapshot;
3. resolves the highest-priority active activity for each policy;
4. writes configured output globals;
5. runs policy/action automations only after the snapshot is committed.

Policy priority is explicit. A larger activity priority wins only for policies
that both activities set. This keeps state composition readable: intercom can
override the LED while media remains active underneath.

## Minimal Example

```yaml
globals:
  - id: g_activity_mask
    type: uint32_t
    restore_value: false
    initial_value: "0"
  - id: g_runtime_seq
    type: uint32_t
    restore_value: false
    initial_value: "0"
  - id: g_led_state
    type: int
    restore_value: false
    initial_value: "0"
  - id: g_audio_policy
    type: int
    restore_value: false
    initial_value: "0"

script:
  - id: apply_led
    then:
      - logger.log:
          format: "LED state now %d"
          args: [id(g_led_state)]

runtime_fsm:
  id: runtime
  debug: false
  state_outputs:
    activity_mask: g_activity_mask
    sequence: g_runtime_seq

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
    assistant_reply:
      priority: 800
      policies:
        led_status: replying
        audio_policy: duck
    intercom_streaming:
      priority: 900
      policies:
        led_status: call
        audio_policy: duck

  events:
    media_started:
      activate: media
    media_stopped:
      deactivate: media
    tts_started:
      activate: assistant_reply
    tts_finished:
      deactivate: assistant_reply
    call_started:
      activate: intercom_streaming
    call_ended:
      deactivate: intercom_streaming

  policies:
    led_status:
      output: g_led_state
      on_change:
        - script.execute: apply_led
      values:
        idle: 0
        media: 12
        replying: 7
        call: 10
    audio_policy:
      output: g_audio_policy
      values:
        normal:
          value: 0
        duck:
          value: 1
```

Events are regular ESPHome actions:

```yaml
media_player:
  - platform: speaker_source
    # ...
    on_play:
      - runtime_fsm.event:
          id: runtime
          event: media_started
    on_idle:
      - runtime_fsm.event:
          id: runtime
          event: media_stopped
```

The component also exposes `runtime_fsm.set_activity`,
`runtime_fsm.set_activities`, `runtime_fsm.request_action`,
`runtime_fsm.dump`, and the condition `runtime_fsm.is_active`.

## Event Cases

Events can choose different transitions depending on the current activity set:

```yaml
runtime_fsm:
  id: runtime
  events:
    wake_word:
      activate: va_starting
      cases:
        - any: [va_responding, announcement]
          activate: va_starting
          deactivate: announcement
          action: restart_voice_response
      action: start_voice
```

Cases are evaluated before the default event rule. This is useful for barge-in:
`wake_word` can cancel an active response, while the default path starts a new
assistant interaction from idle.

## Policies and Outputs

Policies are named by the YAML author. The component does not reserve names
such as `led_status` or `audio_policy`; the maintained full profiles use those
names because they are easy to read.

Each policy value can be a simple integer or an automation with an integer
`value`:

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

If a policy must stop something, declare that stop explicitly on the fallback
activity. For example, the maintained profiles set `ringtone: stop` and
`timer_alarm: stop` on `idle`. The reducer does not infer that missing policy
values mean "stop".

## Optional Intercom Observer

When `intercom:` is configured, `runtime_fsm` reads the call state from
`intercom_api` and maps it into activities using the configured prefix:

```yaml
runtime_fsm:
  id: runtime
  intercom:
    id: intercom
    states:
      ringing:
        priority: 700
        policies:
          led_status: intercom_ringing
          audio_policy: duck
      streaming:
        priority: 650
        policies:
          led_status: intercom_streaming
          audio_policy: duck
```

If `intercom:` is omitted, no intercom code is compiled for the component.

## Debugging

Set `debug: true` to compile verbose reducer tracing:

```yaml
runtime_fsm:
  id: runtime
  debug: true
```

Debug logs include events, queued reentrant events, policy changes, activity
mask and sequence. Keep it enabled during profile development and disable it for
release builds if the extra logs are not needed.

## Reentrancy

ESPHome callbacks can run synchronously from `publish_state()` or from a policy
automation. `runtime_fsm` protects itself with a small internal queue: if an
event or activity update arrives while outputs are being published, it is queued
and processed after the current reducer commit finishes. This keeps each
transition run-to-completion without mutexes or blocking waits.

## Host Test

The pure reducer logic can be tested without flashing a board:

```bash
g++ -std=c++17 -Wall -Wextra -I. \
  tests/runtime_fsm_state_test.cpp \
  esphome/components/runtime_fsm/runtime_fsm_state.cpp \
  -o /tmp/runtime_fsm_state_test
/tmp/runtime_fsm_state_test
```

The maintained test covers policy priority, grouped activities, explicit stop
policies and common media/announcement/Voice Assistant/intercom combinations.
