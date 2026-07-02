# Phase 00V: Contract Simulation And Target Diagnostics

This phase is not a Linux-host device plan anymore. A Linux process is useful
for some component development, but it does not reproduce ESP32-S3 FreeRTOS
timing, PSRAM pressure, Wi-Fi/lwIP behaviour, I2S DMA, AFE/AEC cost, LVGL
pressure or watchdog failure modes closely enough for this project.

The project therefore uses two separate tools:

- deterministic contract simulation for fast local regressions;
- real target diagnostics for timing, starvation, heap and task-state analysis.

## Contract Simulator

`tools/simulator/contract_simulator.py` is a deterministic JSON-RPC runner. It
does not pretend to be a physical device. It validates the contracts around:

- VoIP call state;
- HA softphone/card state;
- terminal reasons;
- routing decisions;
- phonebook propagation semantics;
- media ownership;
- display/LED state transitions;
- audio counter expectations.

Run it through:

```bash
./scripts/run_virtual_device_tests.sh --all
./scripts/run_virtual_device_tests.sh --scenario NAME
./scripts/run_virtual_device_tests.sh --repeat 1000 --seed 1234
```

The runner starts the contract simulator automatically and executes JSON
scenarios from `tests/simulator/scenarios/`.

## Real Target Diagnostics

Physical behaviour must be observed on real devices. This includes:

- FreeRTOS task scheduling;
- task priority/core/state;
- stack high watermark;
- internal/DMA/PSRAM heap pressure;
- loop stalls;
- API reconnect timing;
- sendspin starvation;
- audio queue health;
- AFE/AEC and media pipeline contention;
- watchdog failures.

Use `tools/jtag_snapshots.py` with ESP32-S3 USB-JTAG for intrusive
stop/resume snapshots when a bug needs exact task backtraces:

```bash
.venv/bin/python tools/jtag_snapshots.py --local --sudo-openocd \
  --device spotpear --samples 20 --interval 0.5 --bt-depth 12
```

This halts both CPUs briefly for each sample. Audio glitches and inflated
`main_loop_max_time` are expected during the capture; the point is to see what
each task was doing at the sampled instant.

Use runtime diagnostics and serial logs for non-intrusive observation. Do not
publish high-rate diagnostic sensors to Home Assistant on a device that is
already starved; prefer serial snapshots, JTAG snapshots, or low-rate counters.

## Hardware Still Required

No local simulator proves physical audio quality. These stay in HIL tests:

- real I2S clocking;
- DMA/cache behaviour;
- acoustic AEC quality;
- microphone noise;
- codec initialization on the real bus;
- PSRAM contention;
- MIPI DSI electrical behaviour;
- touch controller behaviour;
- amplifier and enable pins;
- Wi-Fi airtime and RSSI behaviour.

## Completion Gate

Before a release candidate:

- fast contract scenarios pass locally;
- Python tests pass;
- representative ESP YAMLs compile;
- Spotpear and WS3 pass the live call matrix;
- at least one real-device diagnostic capture exists for heavy scenarios
  involving media playback, voice assistant and VoIP.
