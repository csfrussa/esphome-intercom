# Phase 00V: ESPHome Intercom Virtual Device

This phase is mandatory before the next deep refactor. The project needs a host
simulation target that runs the product runtime without physical ESP hardware.
Unit tests and isolated mocks are not enough: the simulator must exercise the
same VoIP runtime and SIP/RTP protocol, Home Assistant API surface, audio
policy, presentation state, and UI decisions used by firmware.

## Source Basis

ESPHome `host:` compiles and runs an ESPHome configuration as a Linux/macOS
process. ESPHome documents this as intended for development, integration tests,
and CI, with the same code path running end-to-end without flashing hardware.
It does not automatically provide unavailable hardware components, so this
project must provide virtual backends for product hardware.

Reference: https://esphome.io/components/host/

## Architecture Rule

The core runtime must not know whether it runs on ESP32-S3, ESP32-P4, or Linux.
It must depend on interfaces:

- `AudioBackend`
- `CodecBackend`
- `DisplayBackend`
- `InputBackend`
- `RuntimeClock`
- `TaskExecutor`
- `NetworkBackend`
- `FaultInjector`

Implementations live under platform-specific backend directories:

- `esphome/components/.../backends/esp_idf/`
- `esphome/components/.../backends/host/`

Do not scatter `#ifdef USE_HOST` through FSM, session, policy, protocol, or
presentation logic. Platform switches belong in factories and backend files.

## Required Host Capabilities

The host backend must provide:

- microphone input from WAV/PCM;
- speaker output to WAV/PCM;
- virtual AEC reference;
- codec state model;
- framebuffer snapshots, initially PNG files, later SDL window support;
- injectable touch/buttons;
- real Linux TCP/UDP sockets;
- virtual clock;
- fault injection;
- runtime/audio/session snapshots.

## Control Surface

The simulator process is controlled through local JSON-RPC over a Unix socket.
The required methods are:

- `inject_event`
- `inject_pcm`
- `press_button`
- `touch`
- `advance_time`
- `set_network_condition`
- `inject_fault`
- `get_snapshot`
- `reset`
- `shutdown`

Every command must return a typed result. Scenario tests must fail on missing
methods, stale callbacks, unexpected state transitions, unexpected audio
counters, queue health regressions, and UI state mismatches.

## Generated Profiles

Virtual YAMLs are generated from the same profile registry used for real device
profiles. Maintaining hand-written virtual profiles is forbidden. Generation
preserves feature/package composition and replaces only the hardware layer:

- `esp32` -> `host`;
- I2S/microphone/speaker hardware -> `virtual_audio`;
- codec -> `virtual_codec`;
- MIPI/LVGL/touch/GPIO -> virtual display/input;
- AFE/AEC hardware -> deterministic audio model.

Generated local Host outputs are stored under `yamls/host/`.

## Required Commands

`scripts/run_virtual_device_tests.sh` is the public entrypoint:

```bash
./scripts/run_virtual_device_tests.sh --changed
./scripts/run_virtual_device_tests.sh --all
./scripts/run_virtual_device_tests.sh --scenario NAME
./scripts/run_virtual_device_tests.sh --repeat 1000 --seed 1234
```

A single host build must be able to execute all compatible scenarios without a
recompile per scenario.

## Scenario Assertions

Every new feature in the refactor must include at least one virtual scenario or
be explicitly marked hardware-only. Scenarios should assert:

- runtime state;
- presentation state;
- audio policy;
- audio owner/lease;
- speaker state;
- media state;
- voice assistant state;
- VoIP session;
- Home Assistant entities;
- queue/counter health;
- stale callback rejection.

## Hardware Still Required

The virtual device cannot prove physical properties:

- real I2S clocking;
- DMA/cache behaviour;
- acoustic AEC quality;
- microphone noise;
- codec initialization on the real bus;
- PSRAM contention;
- MIPI DSI electrical behaviour;
- touch controller behaviour;
- amplifier and enable pins;
- FreeRTOS/multicore timing races.

Those stay in a smaller HIL suite. HIL must validate physical properties, not
repeat the full product matrix.

## Completion Gate

No new refactor feature is complete unless:

- it has a host backend or is marked hardware-only;
- it has at least one simulator scenario;
- it exposes snapshots that scenarios can assert;
- it does not require physical upload for ordinary regression testing.
