# Troubleshooting

Start from evidence, not from a single UI symptom. For every failing call,
collect:

- HA logs around the call;
- ESP log/monitor around the call;
- HA softphone or ESP `SipPhoneState` snapshot;
- selected TX/RX formats;
- RTP packet/byte counters;
- a short WAV capture when audio quality is in question.

## ESP Does Not Ring

- Confirm the peer sends a SIP `INVITE` to the ESP `sip_port`.
- Check ESP `voip_stack` `transport` matches the peer signaling transport.
- Verify SDP offers at least one compatible PCM format.
- Inspect `sensor.*_voip_sip_snapshot` for `last_sip_event`,
  `sip_status_code`, and `terminal_reason`.
- If HA is the caller or bridge, verify HA logs show the route decision and the
  outbound INVITE to the ESP.
- If the target was a name/number, confirm whether HA or the ESP resolved it.
  Direct SIP only happens when the phonebook contains complete direct route
  data.

## HA Softphone Does Not Ring

- Confirm HA reports both implicit SIP listeners ready in logs:
  `SIP UDP listener ready`, `SIP TCP listener ready` and
  `SIP endpoint enabled on UDP+TCP/<port>`.
- Confirm the INVITE Request-URI reaches HA's advertised host and SIP port.
- Check HA softphone DND and active-call state. A second inbound call while HA
  is ringing or in-call should receive busy.
- For trunk calls, no route hint means HA/default target; an explicit unresolved
  route hint terminates as `route_not_found`.
- For local registered SIP endpoints, confirm the REGISTER Contact is present in
  HA logs and the phonebook includes the registered SIP endpoint contact.

## Unknown Or Unregistered Caller Is Rejected

The phonebook is an outbound dial plan, not an inbound caller allowlist. An ESP
or HA may therefore receive a compatible SIP INVITE from any peer that can
reach its listener, even when the caller is absent from the phonebook and has
not registered to HA. The optional HA registrar authenticates `REGISTER`; it
does not require every inbound caller to own an account.

- Check DND, busy state, Request-URI routing and SDP compatibility before
  treating an unknown caller as unauthorized.
- Keep SIP/RTP on a trusted LAN or VPN. Use firewall, VLAN, VPN or an SBC when
  caller admission policy is required; the ESP profile does not provide
  SIP/TLS, SRTP or an inbound caller allowlist.

## Call Fails With `media_incompatible`

The SDP offer/answer did not produce a usable PCM RTP format, or HA could not
build the required bridge conversion. Use explicit supported PCM profiles such
as `16000:s16le:1:16`, `16000:s16le:1:32`, `16000:s16le:1:20`, or
`48000:s16le:1:10`. A call also needs one common packet time across the selected
TX and RX directions; rates may differ, but `frame_ms`/`ptime` must match.

ESP devices are PCM-only. HA softphone/trunk legs can negotiate common VoIP
codecs where supported, but the bridge must still be able to convert the ESP
leg to a compatible PCM format.

## HA Cannot Route A Name

- Ensure `sensor.voip_phonebook` contains the target.
- If an ESP has just rebooted or been reflashed, check
  `sensor.<device>_voip_contacts`. If it is `unknown` or empty while the device
  is otherwise online, wait for HA to see the ESPHome
  `esphome.<slug>_set_roster_json` service or call `voip_stack.push_phonebook`
  for diagnostics. Current builds automatically refresh when that service is
  registered.
- For local ESP-only routing, declare the contact in
  `voip_stack.static_contacts`.
- Use a direct SIP URI (`sip:name@host:5060`) when bypassing HA.
- Use `ha_bridge: true` when HA must bridge a logical target.
- For external numbers, confirm the optional trunk is configured and registered.
- Check whether the entry is disabled. Disabled entries reject instead of
  routing through HA.
- Check `extension` aliases for local/internal targets. Numeric targets from
  ESP always go to HA; HA resolves `extension` as an internal target and
  `number` as an external trunk target.

## Registered Softphone Cannot Register To HA

- Enable the HA SIP TCP or UDP listener used by the softphone.
- Enable the local registrar in VoIP Stack setup.
- Create an account with `voip_stack.create_account`.
- If no password is supplied, read it from the `sip_account_created` event or
  the persistent notification. The generated password is shown only once.
- Configure the softphone with HA advertised host, SIP port, username and
  password. Do not configure an external PBX/outbound proxy for local HA
  registration.
- Confirm HA logs show REGISTER and a dynamic phonebook contact for the
  registered SIP endpoint.

## Busy Or DND

DND and active-call contention should produce `486 Busy Here` or a terminal
reason of `busy`. Decline should produce `603 Decline` or a configured SIP
final response.

## Hold Or Re-INVITE Receives `488`

Session-modifying in-dialog INVITE is not supported in the current ESP or HA
profile. A hold or codec-renegotiation re-INVITE receives `488 Not Acceptable
Here`; the already established dialog and media selection remain active. A
later BYE must still end that original call normally. Do not diagnose this as a
dropped call unless the original dialog or RTP also stops.

## No Audio

- Confirm RTP ports are reachable in both directions.
- Check selected TX/RX formats in the SIP snapshot.
- Check RTP packet/byte counters on both HA and ESP.
- For HA bridge calls, inspect relay logs for conversion/drop messages.
- Capture WAV from the HA websocket probe or a SIP softphone. Counters that
  increase do not prove audible audio.
- If audio is rhythmic, choppy or "machine gun" style, compare the negotiated
  `ptime`/frame size against the actual RTP payload byte size.
- If one direction is silent but counters increase, inspect the source device:
  mic-only/speaker-only mode, muted switch, low analog gain, AFE/AEC output
  surface, or silence in the room.
- For generic ESPHome-native YAMLs, verify the hardware matches the YAML. The
  reference native full/speaker examples are written around INMP441 plus a
  MAX98357A-style I2S amplifier. A PCM5102 is a line-level DAC, not a speaker
  amplifier, and may need a powered speaker or amplifier plus correct mute/XSMT
  wiring. INMP441 boards also depend on the L/R strap; if the selected
  `channels: [1]` path is silent, test `channels: [0]`.
- `speaker_only` profiles intentionally have no microphone path. They can play
  remote audio but cannot send local microphone audio back.
- If one browser works and another does not, check which browser owns the HA
  softphone media WebSocket for that active call.

## Trunk Does Not Register

- Confirm `trunk_enabled` is on; when off, no trunk runtime is created.
- Check `sip_trunk.trunk_status_code`, `trunk_status_reason` and
  `trunk_last_sip_event` in the HA softphone snapshot.
- Confirm provider transport, server, port, username/auth username and password.
- If the provider requires an outbound proxy, set `trunk_outbound_proxy`.
- INFO logs should show REGISTER, challenge if present, and final registration
  status. DEBUG logs include the detailed SIP flow.

## Inbound Trunk Call Routes To The Wrong Target

- Confirm the provider offers RFC2833/telephone-event DTMF in SDP.
- HA acknowledges SIP INFO as a supported SIP method, but digit routing reads
  RTP `telephone-event`; INFO bodies are not a DTMF input in this profile. If
  the SDP has no `telephone-event`, HA cannot read post-answer route digits.
- Check that the target exists in the central phonebook and has the matching
  `extension` value.
- Keep the inbound DTMF timeout short, normally 3 seconds. Set it to `0` when you do not want trunk pre-answer/DTMF and want inbound calls to follow the normal dialplan immediately.
- If no digits arrive, HA uses `trunk_inbound_default_target`.
- If digits arrive but do not resolve, HA logs them and terminates the answered
  trunk leg as `route_not_found`.

## Card State Looks Wrong

- ESP mirror cards should follow ESPHome entity state and ESP buttons. They do
  not own RTP counters for the HA softphone leg.
- HA softphone cards should follow `voip_stack` softphone snapshots/events.
  The card must not infer terminal state locally.
- Hard-refresh the dashboard after upgrading the frontend resource.
- If multiple browsers are open, only the browser that attached the HA
  softphone media WebSocket owns live browser audio for the active HA call.
