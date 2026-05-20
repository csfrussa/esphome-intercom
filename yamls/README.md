# Device Configurations

Ready-to-flash ESPHome YAML configurations for tested hardware. Stable release
reference targets are the ESP32-S3 presets; P4 YAMLs are provided as
experimental hardware-specific targets.

## How to use

1. Download the YAML file for your device
2. Create a `secrets.yaml` with your WiFi credentials:
   ```yaml
   wifi_ssid: "your_network"
   wifi_password: "your_password"
   ```
3. Compile with ESPHome. The public YAMLs on `main` point at the GitHub copy of this repository, so components, packages, and assets are fetched automatically.

## Structure

```
yamls/
  intercom-only/         Intercom without Voice Assistant or Wake Word
    single-bus/          Devices using esp_audio_stack (mic+speaker on same I2S bus)

  full-experience/       VA + MWW + Intercom (complete voice assistant hub)
    single-bus/
      aec/               esp_audio_stack + esp_aec
      afe/               esp_audio_stack + esp_afe

  experimental/          Untested topologies (compile-only, contributions welcome)
    dual-bus/            Devices with separate I2S buses for mic and speaker
    single-bus/aec/      Historical/device-specific AEC references
```

Device-specific debug YAMLs are intentionally local-only and live in the
gitignored `yamls/debug/` directory during target-specific debug. Reusable debug
building blocks are public packages under `packages/debug/`.

## Single-bus vs Dual-bus

- **Single-bus**: mic and speaker share one I2S peripheral via `esp_audio_stack`. Used by devices with audio codecs (ES8311, ES7210+ES8311). Enables stereo AEC reference, TDM multi-mic, and 48kHz bus rate with Espressif `esp_ae_rate_cvt` conversion to 16kHz.

- **Dual-bus**: mic and speaker on separate I2S peripherals using standard ESPHome `i2s_audio`. Simpler setup for MEMS mic + class-D amp boards (SPH0645 + MAX98357A).

## Audio processor: esp_aec vs esp_afe

- **esp_aec**: Lightweight echo cancellation only (~40 KB). Recommended for intercom-only and single-mic setups.
- **esp_afe**: Full Espressif AFE pipeline (AEC + NS + VAD + AGC + optional dual-mic Speech Enhancement). Higher RAM cost, but adds the full frontend and runtime diagnostics. See the [esp_afe component README](../esphome/components/esp_afe/README.md) for details.

## Product mode

Each ESP flashed with these YAMLs is an independent extension on a peer-to-peer fabric. Same-transport devices can call each other directly from their local phonebook; in the current standard YAMLs HA is the stable phonebook authority. When HA is on the network it joins the fabric as one more extension and can also act as a PBX-style switchboard via `routing_mode: ha_pbx`.

There is one product mode: PBX-lite (implicit default). Phonebook / contacts / destination / caller entities are always exposed. The `mode:` key on `intercom_api` is optional and only takes one value: `raw_udp` (audio-only UDP, no signaling, used for go2rtc / two-room direct links). Routing policy lives on the ESP as `routing_mode: device_independent` (default; ESP dials peers from its phonebook, true peer-to-peer) or `routing_mode: ha_pbx` (ESP dials the HA peer named by `hass.config.location_name`, HA bridges).

## Production logging

Public YAMLs ship with `logger.level: INFO`. INFO covers all user-visible call-lifecycle, mic-consumer attach/detach and AFE/AEC mode-switch milestones. Flip to `DEBUG` only while developing; the per-frame telemetry path is additionally gated behind `esp_audio_stack.telemetry: true`. Audio deep-debug lives in `packages/debug/p4_audio_deep_debug.yaml`.

## P4 status

Waveshare P4 Touch YAMLs build and boot, but they are not stable release
reference targets yet. Treat them as experimental while hosted Wi-Fi/SDIO,
LVGL/PPA, media/TTS transport behavior and task scheduling are investigated.

If a P4 target resets, hangs, or loses Wi-Fi under media/TTS streaming, update
the on-board ESP32-C6 hosted Wi-Fi firmware before chasing audio bugs. The
root README's P4 section documents the validated recovery path: use a P4
`esp-serial-flasher` SDIO-ROM recovery flasher, short the C6 `IO9` pad to `GND` while
the flasher boots, program the C6 `network_adapter` firmware, then reflash the
normal ESPHome P4 YAML. Current P4 packages also enable ESPHome's native
`esp32_hosted` update entity for future C6 updates once the coprocessor is on a
modern firmware.

## Local development vs release mode

The public YAMLs in `main` are stored in **remote mode** so they compile
straight from GitHub. If you are working inside a local clone and want
them to point back at your checkout, run:

```bash
./scripts/yaml_paths.sh local
```

When you are ready to switch them back to the published form:

```bash
./scripts/yaml_paths.sh remote --branch main
```

## Not sure which one to pick?

See [../docs/DEPLOYMENT_GUIDE.md](../docs/DEPLOYMENT_GUIDE.md) for a decision tree that maps hardware and requirements to the right preset.
