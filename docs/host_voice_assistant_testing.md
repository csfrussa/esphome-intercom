# ESPHome Host Voice Assistant Testing

The Spotpear Host profile runs as a Linux ESPHome node and can be added to Home Assistant like a normal ESPHome device.

## Profile

Use:

```bash
esphome run yamls/generated/virtual/spotpear-ball-v2-full-afe-sip-host.yaml
```

The profile exposes:

- ESPHome API on port `6053`
- `voice_assistant`
- `microphone.virtual_microphone`, reading 16 kHz s16 mono PCM from `tests/simulator/audio/mic_input.pcm`
- `speaker.virtual_speaker`, writing 16 kHz s16 mono PCM to `test_runs/simulator/spotpear_host_va_speaker_output.pcm`
- `intercom_simulator` JSON-RPC snapshots on `test_runs/simulator/spotpear-host-sim.sock`

## Home Assistant

Add the Host node as an ESPHome integration using the machine IP and port `6053`.

For this workspace the current Host IP was `192.168.1.48`, but it can change with DHCP.

The generated Assist satellite entity is:

```text
assist_satellite.spotpear_ball_v2_full_afe_sip_host_assist_satellite
```

The profile also exposes custom ESPHome services:

```text
esphome.spotpear_sip_host_start_va
esphome.spotpear_sip_host_stop_va
```

These services simulate a hardware button starting/stopping Voice Assistant on the device.

## Automated Test

Run with a Home Assistant token from the environment:

```bash
HA_URL=https://homeassistant.example \
HA_TOKEN=... \
python tools/simulator/ha_voice_assistant_host_test.py --insecure
```

The script:

1. Clears the previous virtual speaker PCM output.
2. Calls `esphome.spotpear_sip_host_start_va`.
3. Waits for the Assist satellite state to leave `idle` and return to `idle`.
4. Verifies that TTS/audio output was written.
5. Converts the raw PCM output to WAV for inspection.

The generated WAV is:

```text
test_runs/simulator/spotpear_host_va_speaker_output.wav
```

This tests the real Home Assistant Assist pipeline, not only simulator-internal state transitions.

Run repeated cycles without restarting the Host process:

```bash
HA_URL=https://homeassistant.example \
HA_TOKEN=... \
python tools/simulator/ha_voice_assistant_host_test.py --insecure --repeat 3
```

Repeated runs write numbered artifacts:

```text
test_runs/simulator/spotpear_host_va_speaker_output_001.wav
test_runs/simulator/spotpear_host_va_speaker_output_002.wav
test_runs/simulator/spotpear_host_va_speaker_output_003.wav
```

## Microphone Input

The virtual microphone reads the profile input file:

```text
tests/simulator/audio/mic_input.pcm
```

The runner can replace that file before a test:

```bash
python tools/simulator/ha_voice_assistant_host_test.py \
  --mic-pcm tests/simulator/audio/my_command.pcm
```

or convert WAV to the raw PCM input expected by the Host device:

```bash
python tools/simulator/ha_voice_assistant_host_test.py \
  --mic-wav tests/simulator/audio/my_command.wav
```

The input WAV must match the Host audio format: 16 kHz, 16-bit, mono.
