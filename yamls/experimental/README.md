# Experimental YAMLs

Configurations for hardware topologies or integration experiments that have not
been promoted to release baselines. They compile or reflect the intended wiring,
but they are not release targets. Treat them as starting points for bring-up and
regression comparison.

## Contents

- [`home-assistant-voice-pe/`](home-assistant-voice-pe/) - Voice PE intercom
  integration proof.

Dual-bus MEMS+amp profiles are no longer experimental; the maintained SIP phone
profiles live in [`../voip-only/dual-bus/`](../voip-only/dual-bus/) and
use `esp_audio_stack` `rx_bus` / `tx_bus`.

## Status

| YAML | Hardware | What's missing |
|------|----------|----------------|
| `home-assistant-voice-pe/home-assistant-voice-pe-voip.yaml` | Home Assistant Voice PE | runtime intercom validation |

## Contributing

If you flash one of these on the matching hardware and complete a successful
call, please open an issue or PR with logs from both ends so we can promote the
YAML to the tested tree.
