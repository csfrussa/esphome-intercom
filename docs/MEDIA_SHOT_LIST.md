# Media shot list

This is the working list for refreshing README and documentation media for the
`2026.7.0-dev` SIP/VoIP release.

## Primary README media

| Asset | Type | Purpose | Capture notes |
|---|---|---|---|
| `docs/images/dashboard.gif` | GIF | First visual impression and dashboard call flow | Captured: card scrolls contacts, calls Casa, then hangs up |
| `docs/images/esp-mirror-card.png` | Screenshot | ESP mirror Lovelace mode | Captured: card bound to an ESP endpoint |
| `docs/images/ha-softphone-card.png` | Screenshot | HA softphone Lovelace mode | Captured: HA card with destination selector |
| `docs/images/ha-softphone-options.jpg` | Screenshot | HA softphone options panel | Captured: Auto Answer, DND and ringtone controls |
| `docs/images/ha-softphone-keypad.jpg` | Screenshot | HA softphone keypad/manual dial view | Captured: direct name/extension/number entry |
| `docs/images/assistant-animated.gif` | GIF | Device assistant animation | Captured from Spotpear Ball v2, converted from MP4 and stripped |
| `docs/images/assistant-speaking.jpg` | Photo | TTS answer on device | Captured from Spotpear Ball v2 |
| `docs/images/lvgl-audio-volume.jpg` | Photo | Device audio controls | Captured from LVGL audio page |
| `docs/images/lvgl-hangup-reason.jpg` | Photo | Device call end reason | Captured from LVGL call state |

## Call flow media

| Asset | Type | Purpose | Capture notes |
|---|---|---|---|
| `docs/images/call-from-home-assistant-to-esp.gif` | GIF | Browser card or HA calls an ESP | Captured: target ESP card only, ring, answer, hangup |
| `docs/images/call-from-esp-to-homeassistant.gif` | GIF | ESP calls Home Assistant | Captured: ESP scrolls contacts, calls Casa, HA rings, answer or hangup sequence visible |
| `docs/images/mobile-notification-answer.gif` | GIF | Android notification answers an ESP call | Captured: notification actions, HA app opening, card in full-duplex call |
| `docs/images/cross-transport-bridge.gif` | GIF | HA SIP bridge | Captured: WS3 SIP/UDP calls Spotpear SIP/TCP through HA, ringing, answer, hangup |
| `docs/images/decline-reason.png` | Screenshot | Reason propagation | Decline with custom reason, DND or busy visible in card/sensor |
| `docs/images/busy-reason.png` | Screenshot | Busy behavior | One call active, second caller rejected with `busy` |

## Installation and configuration media

These screenshots must be recaptured before the stable release whenever the HA
setup flow, card editor, card labels or entity names change. During prerelease
work, diagram/mock assets are acceptable only when they do not document removed
options or old component names.

| Asset | Type | Purpose | Capture notes |
|---|---|---|---|
| `docs/images/hacs-custom-repository.png` | Screenshot | HACS custom repository setup | Captured: custom repository URL and Integration type |
| `docs/images/hacs-download-voip-stack.png` | Screenshot | HACS download step | Captured: VoIP Stack HACS entry and download menu |
| `docs/images/voip-stack-add-integration.png` | Screenshot | HA Add Integration search | Captured: VoIP Stack entry in Settings -> Integrations |
| `docs/images/voip-stack-config-flow.png` / `.svg` | Setup image | HA integration setup | Current asset: SIP/RTP ports, advertise host, Assist/debug, registrar and trunk toggles. Recapture a real HA screenshot before stable release. |
| `docs/images/create-account-service.png` | Screenshot | SIP account service form | Captured: Developer Tools -> Actions account creation |
| `docs/images/create-account-service-filled.png` | Screenshot | Filled SIP account service form | Captured: example local softphone account |
| `docs/images/create-account-notification.png` | Screenshot | Generated account notification | Captured: one-time generated password notification |
| `docs/images/esphome-add-device.png` | Screenshot | Add ESPHome device | Recapture if entity names, device names or ESPHome onboarding UI changed. |
| `docs/images/card-selection.png` | Screenshot | Card picker | Must show `VoIP Stack Card`, not old Intercom card names. |
| `docs/images/card-configuration.png` | Screenshot | Card YAML/UI config | Must show current `voip-stack-card` modes and options. |

## Full voice device media

| Asset | Type | Purpose | Capture notes |
|---|---|---|---|
| `docs/images/afe-controls.png` | Screenshot | Audio controls in HA | Volume cascade, AFE/AEC switches, wake word, VAD sensor |
| `docs/images/assistant-happy.jpg` | Photo | Mood-aware positive answer | Captured from Spotpear Ball v2 |
| `docs/images/assistant-neutral.jpg` | Photo | Mood-aware neutral answer | Captured from Spotpear Ball v2 |
| `docs/images/assistant-angry.jpg` | Photo | Mood-aware negative answer | Captured from Spotpear Ball v2 |
| `docs/images/p4-intercom-assistant.jpg` | Photo | P4 intercom and assistant panel | Captured from Waveshare P4 Touch |
| `docs/images/p4-audio-settings.jpg` | Photo | P4 runtime audio settings | Captured from Waveshare P4 Touch |
| `docs/images/ducking-barge-in.gif` | GIF | Music, wake word, ducking, barge-in | Captured from Spotpear Ball v2 |

## Still missing

No blocking README media is currently missing. Optional future captures can
replace individual photos or GIFs if the UI changes before release.

## Architecture and documentation diagrams

| Asset | Type | Purpose | Capture notes |
|---|---|---|---|
| `docs/images/sip-topology.png` / `.svg` | Diagram | SIP/VoIP topology | Created: ESP SIP phones, HA softphone/router/B2BUA, local registrar, registered softphones and optional provider trunk |
| `docs/images/phonebook-endpoint.png` / `.svg` | Diagram | SIP phonebook and dial plan | Created: endpoint publication, manual/static contacts, SIP account registration, direct/bridge/trunk/reject routing |
| `docs/images/tcp-udp-choice.png` / `.svg` | Diagram | ESP SIP transport guidance | Created: ESP SIP/TCP vs SIP/UDP signaling choice, RTP always UDP, HA bridge path when routing needs it |
| `docs/images/audio-stack.png` | Diagram | Audio components | Created: `esp_audio_stack`, ESPHome consumers, `esp_afe` / `esp_aec`, codec/no-codec output paths |

## Capture order for live demo

1. Dashboard clean idle state with all four devices.
2. P4 calls Spotpear and Spotpear rings.
3. Spotpear answers, then hangup.
4. P4 calls WS3 through HA bridge.
5. DND rejection.
6. Busy rejection.
7. HA/browser card calls an ESP.
8. ESP calls HA/browser card.
9. Ducking and barge-in on a full voice device.

## Director checklist

Use this section during capture when driving the demo from HA services,
ESPHome actions or the Lovelace card.

| Scene | Trigger | Expected visible state |
|---|---|---|
| P4 to Spotpear ringing | P4 selects Spotpear and calls | P4 shows outgoing, Spotpear shows incoming caller, HA card shows ringing |
| Spotpear answers P4 | Spotpear answer button or HA card answer | Both ESPs show in-call, call duration advances, SIP transport label visible |
| Spotpear declines P4 | Spotpear decline button | P4 returns idle with decline reason, HA card reason updates |
| P4 TCP to WS3 UDP | P4 calls WS3 through HA | HA bridge state visible, both endpoints show correct remote name |
| DND rejection | Enable DND on target, then call it | Caller receives `dnd` or equivalent reason, target does not enter in-call |
| Busy rejection | Keep one call active, call busy endpoint from another device | Second caller receives busy reason, active call is not disturbed |
| Browser to ESP | Lovelace card starts call | ESP rings, browser card gets answer/decline controls |
| ESP to browser/HA | ESP calls HA location name | Lovelace card rings with ESP caller name |
| Ducking and barge-in | Start media, trigger wake word, interrupt while assistant speaks | Media ducks, assistant can be interrupted, final media state is correct |

## Practical capture notes

- Use stable device names: `Waveshare P4 Touch`, `Waveshare S3 Audio`,
  `Generic S3`, `Spotpear Ball v2`, and HA peer name from
  `hass.config.location_name`.
- Hide debug-only entities unless the screenshot is specifically about
  diagnostics.
- Prefer one desktop viewport size for HA screenshots.
- Keep GIFs short: 6 to 12 seconds where possible.
- Capture a still image from each GIF-worthy scene so the README can use either
  static or animated media depending on file size.
