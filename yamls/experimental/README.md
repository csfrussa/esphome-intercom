# Experimental YAMLs

Configurations for hardware topologies or historical branches that have not
been validated end-to-end against the current codebase. They compile or reflect
the intended wiring, but they are not release baselines. Treat them as starting
points for hardware bring-up and regression comparison.

## Contents

- [`dual-bus/`](dual-bus/) - Boards that route the microphone and the speaker on **different** I²S controllers (e.g. INMP441 mic on bus 0, MAX98357 amp on bus 1). The maintained path now uses `esp_audio_stack` `rx_bus` and `tx_bus` with a software AEC reference. Tested boards live in [`../intercom-only/single-bus/`](../intercom-only/single-bus/) and [`../full-experience/single-bus/`](../full-experience/single-bus/).

## Status

| YAML | Hardware | What's missing |
|------|----------|----------------|
| `dual-bus/intercom-only/generic-s3-dual-intercom.yaml` | Generic S3 dual-bus | end-to-end intercom call test |

## Contributing

If you flash one of these on the matching hardware and complete a successful call, please open an issue or PR with logs from both ends so we can promote the YAML to the tested tree. Audio quality on dual-bus is intrinsically lower than single-bus (no shared I²S clock, AEC reference comes from a software ring buffer instead of the codec or TDM ADC), but the protocol stack is identical.
