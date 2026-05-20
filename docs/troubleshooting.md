# Troubleshooting

Common symptoms and fixes when setting up ESPHome Intercom.

## Contents
- [Card shows "No devices found"](#card-shows-no-devices-found)
- [No audio from ESP speaker](#no-audio-from-esp-speaker)
- [No audio from browser](#no-audio-from-browser)
- [Echo or feedback](#echo-or-feedback)
- [High latency](#high-latency)
- [ESP shows "Ringing" but browser doesn't connect](#esp-shows-ringing-but-browser-doesnt-connect)
- [ESP doesn't see other devices](#esp-doesnt-see-other-devices)
- [HA integration fails to start (port bind error)](#ha-integration-fails-to-start-port-bind-error)
- [WARN: cannot determine HA announce IP](#warn-cannot-determine-ha-announce-ip)
- [ERROR: ha_pbx routing without HA peer name](#error-ha_pbx-routing-without-ha-peer-name)
- [WARN: TDM AEC reference silent](#warn-tdm-aec-reference-silent)

---

## Card shows "No devices found"

1. Verify the `Intercom Native` integration is configured (UI: Settings -> Devices & Services -> Add Integration).
2. Restart Home Assistant after adding the integration.
3. Ensure the ESP device is connected via the ESPHome integration.
4. Check the ESP has `intercom_api` configured (PBX-lite is the implicit default; no `mode:` key needed).
5. Clear browser cache and reload.

## No audio from ESP speaker

1. Check speaker wiring and I2S pin configuration.
2. Verify `speaker_enable` GPIO if your amp has an enable pin.
3. Check volume level (default 80%).
4. Look for I2S errors in ESP logs.

## No audio from browser

1. Check browser microphone permissions.
2. Verify HTTPS (required for `getUserMedia`).
3. Check browser console for AudioContext errors.
4. Try a different browser (Chrome recommended).

## Echo or feedback

1. Enable AEC: create an audio processor and link it via `processor_id`. With `esp_audio_stack`, both `esp_aec` and `esp_afe` are supported. With `intercom_api` alone (no duplex in front), use `esp_aec` only.
2. Ensure the AEC switch is ON in Home Assistant.
3. Reduce Master Volume.
4. Increase physical distance between mic and speaker.

## High latency

1. Check WiFi signal strength (should be > -70 dBm).
2. Verify Home Assistant is not overloaded.
3. Check for network congestion.
4. Production YAMLs default to `logger.level: INFO`. Only flip to `DEBUG` (or enable `esp_audio_stack.telemetry: true`) while tuning, then revert.

## ESP shows "Ringing" but browser doesn't connect

1. Check the configured TCP port (default 6054) is reachable.
2. Verify no firewall blocking HA <-> ESP.
3. Check Home Assistant logs for connection errors.
4. Try restarting the ESP device.

## ESP doesn't see other devices

The phonebook is the single source of truth. Empty phonebook is normal at boot; the standard packages subscribe to HA phonebook sensors, or a YAML automation populates contacts via native `intercom_api` actions.

1. Verify the `Intercom Native` integration is enabled in HA and that `sensor.intercom_phonebook` is populated.
2. If you bypass HA phonebook sync, test with an ESPHome YAML script that calls `intercom_api.set_contacts` with `Name|tcp|ip|port,Name2|udp|ip|audio|control,...`.
3. HA is the source of truth for contacts in standard packages. Cross-protocol bridging is HA's job. If two ESPs on different protocols don't see each other directly, that is by design - HA is the bridge.
4. DHCP IP change: HA discovery picks up the new IP within seconds; HA's refresh listener follows shortly after.

## HA integration fails to start (port bind error)

`intercom_native` binds TCP and UDP listener sockets directly. If the bind fails, the config entry is set to `ConfigEntryError`.

- **HA OS / Supervised**: container runs `--network=host` by default. Should just work.
- **HA Container (Docker)**: must be started with `--network=host` (also recommended by official HA docs). Bridge mode would require manual port forwarding for `tcp_port` / `udp_audio_port` / `udp_control_port` plus mDNS reflector + `network: announced_addresses` override (not recommended).
- **HA Core in venv**: listens directly on the host LAN, no extra config.
- Port already in use: change `tcp_port` / `udp_audio_port` / `udp_control_port` in the integration options. Defaults are 6054 / 6054 / 6055 (TCP and UDP audio share number 6054 on different protocol stacks).

## WARN: cannot determine HA announce IP

> `Cannot determine HA announce IP (network.async_get_announce_addresses returned empty); HA will not appear in the ESP phonebook and ha_pbx routing will be unavailable until announce_addresses or external_url is configured.`

`network.async_get_announce_addresses` returned an empty list. The integration cannot put HA into the phonebook as a peer, so ESPs in `routing_mode: ha_pbx` cannot route their calls. Fix by either:

- Configuring `network: announced_addresses:` in `configuration.yaml` with the LAN IP HA should advertise, or
- Setting an `external_url:` (or internal URL) so HA can resolve a usable address.

`routing_mode: device_independent` ESPs are unaffected (they dial peers directly from the phonebook).

## ERROR: ha_pbx routing without HA peer name

If `routing_mode: ha_pbx` is set on the ESP but `ha_peer_name_` is empty, the ESP logs an ERROR at call time and refuses to dial. Standard packages learn it from the HA row in `sensor.intercom_phonebook`. If you bypass those packages or run a custom YAML-only setup, call it yourself once at boot:

```yaml
action: esphome.<slug>_set_ha_peer_name
data:
  name: "Beach House"   # whatever HA's location_name is
```

The ESP default `ha_peer_name_` is empty (no hardcoded "Home Assistant" or localized default) so the error is intentional - the ESP needs to know which phonebook entry represents HA.

## WARN: TDM AEC reference silent

> `TDM AEC reference silent for 100 frames while speaker active (ref -72.4 dBFS); check tdm_ref_slot wiring or set use_tdm_reference: false`

`esp_audio_stack` watches the configured TDM reference slot while the speaker is actively driving samples. If the slot RMS stays below -60 dBFS for ~3.2 s (100 frames at 32 ms), it emits a one-shot WARN. The most common causes:

- `tdm_ref_slot` does not match the board wiring. Korvo-2 baseline assumes MIC3 / slot 2; Waveshare P4 Touch uses MIC2 / slot 1. The board YAML must override `tdm_ref_slot` and (where applicable) the per-slot ES7210 PGA register.
- ES7210 PGA for the chosen ref slot is at 0 dB or muted. The Korvo-2 baseline in `packages/codec/es7210_tdm.yaml` sets MIC3 PGA to 30 dB; boards using a different ref slot must override the corresponding PGA register from their own `on_boot` lambda after the baseline script runs.
- Wiring fault between the codec DAC and the chosen ADC slot.

Workaround: set `use_tdm_reference: false` to fall back to the software ring-buffer reference. AEC quality drops but the call still works.
