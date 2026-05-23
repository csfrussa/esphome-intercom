# Breaking changes

`2026.5.0` is a major upgrade from the `4.x` line. The project moved from
"PBX-like" wiring to a real **PBX-lite** protocol: still deliberately small, but
with the pieces an intercom system needs in practice: endpoint-aware phonebook
rows, explicit call state, ringing, answer, decline, hangup, error reasons,
direct same-transport ESP calls, HA bridge/PBX routing and browser softphone
legs.

If you are upgrading from a working installation, apply these edits before you
flash the new firmware or restart Home Assistant.

## YAML: `intercom_api`

| Was | Is | Action |
|---|---|---|
| `mode: simple` | _(unset)_ | Remove the line. PBX-lite is the implicit default. |
| `mode: full` | _(unset)_ | Remove the line. PBX-lite is the implicit default. |
| `mode: webrtc` | `mode: raw_udp` | Rename. Same semantics: audio-only UDP, no signaling. |

The `mode:` key is now optional and only accepts `raw_udp`. Any other value fails ESPHome validation at compile time.

PBX-lite is the default. The old `simple` / `full` distinction is gone because
there is no longer a separate "doorbell mode" versus "full intercom mode": a
doorbell is just a phonebook with one HA/browser destination, and a room
intercom is the same state machine with more contacts.

## YAML tree: dual-bus boards

The never-validated generic dual-bus YAML was replaced by maintained
`esp_audio_stack` TCP/UDP profiles. These profiles use `rx_bus` / `tx_bus`
instead of the old `i2s_audio` + standalone `intercom_api.processor_id` path.

| Old path | New path |
|---|---|
| `yamls/intercom-only/dual-bus/generic-s3-dual-intercom_NOT_READY.yaml` | `yamls/intercom-only/dual-bus/generic-s3-intercom-tcp.yaml` or `yamls/intercom-only/dual-bus/generic-s3-intercom-udp.yaml` |
| `yamls/experimental/dual-bus/intercom-only/generic-s3-dual-intercom.yaml` | `yamls/intercom-only/dual-bus/generic-s3-intercom-tcp.yaml` or `yamls/intercom-only/dual-bus/generic-s3-intercom-udp.yaml` |

Update any local fork or symlink that pointed at the old paths.

## Home Assistant: bus events

The separate HA bus events were replaced by one unified call event. If you have
automations or scripts triggering on these, update the trigger:

| Was | Is |
|---|---|
| `intercom_state` / `intercom_native_state_changed` | `intercom_native.call_event` with `scope: session` |
| `intercom_bridge_state` / `intercom_native_bridge_state_changed` | `intercom_native.call_event` with `scope: bridge` |
| `intercom_forward_state` / `intercom_native_forward_state_changed` | `intercom_native.call_event` with `scope: forward` |

Use `type` for automations (`outgoing`, `ringing`, `answered`, `ended`,
`missed`, `failed`) and `state` when you need the exact internal state. The
state-text ESPHome sensor (`sensor.<name>_intercom_state`) is unchanged.

## Home Assistant: phonebook and HA peer name

The phonebook is now endpoint-first. ESPs publish
`sensor.<device>_intercom_endpoint` as `Name|protocol|ip|ports`, HA builds the
canonical `sensor.intercom_phonebook`, and firmware packages subscribe to that
single roster.

Do not rely on a hardcoded `"Home Assistant"` contact anymore. The HA peer name
is `hass.config.location_name`, so the contact can be `"Home"`, `"Office"`,
`"Beach House"` or any other name chosen in HA settings.

## C++: `intercom_api` namespace

Only relevant if you have downstream code against the `IntercomApi` C++ class:

- `IntercomApi::set_full_mode(bool)` removed. It was a no-op since the simple/full distinction was retired.
- `IntercomApi::set_webrtc_mode(bool)` renamed to `set_raw_udp_mode(bool)`.
- Protected member `webrtc_mode_` renamed to `raw_udp_mode_`.
