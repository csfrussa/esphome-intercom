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
- Check ESP `intercom_api` `protocol` matches the peer signaling transport.
- Verify SDP offers at least one compatible PCM format.
- Inspect `sensor.*_intercom_sip_snapshot` for `last_sip_event`,
  `sip_status_code`, and `terminal_reason`.
- If HA is the caller or bridge, verify HA logs show the route decision and the
  outbound INVITE to the ESP.
- If the target was a name/number, confirm whether HA or the ESP resolved it.
  Direct SIP only happens when the phonebook contains complete direct route
  data.

## HA Softphone Does Not Ring

- Confirm HA SIP TCP/UDP listener readiness in logs.
- Confirm the INVITE Request-URI reaches HA's advertised host and SIP port.
- Check HA softphone DND and active-call state. A second inbound call while HA
  is ringing or in-call should receive busy.
- For trunk calls, no route hint means HA/default target; an explicit unresolved
  route hint terminates as `route_not_found`.
- For local registered softphones, confirm the REGISTER Contact is present in
  HA logs and the phonebook includes `kind: softphone`.

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

- Ensure `sensor.intercom_phonebook` contains the target.
- For local ESP-only routing, declare the contact in
  `intercom_api.static_contacts`.
- Use a direct SIP URI (`sip:name@host:5060`) when bypassing HA.
- Use `ha_bridge: true` when HA must bridge a logical target.
- For external numbers, confirm the optional trunk is configured and registered.
- Check whether the entry is disabled. Disabled entries reject instead of
  routing through HA.
- Check `number` aliases. Numeric targets from ESP always go to HA; HA then
  resolves the number as a local extension or external trunk target.

## Registered Softphone Cannot Register To HA

- Enable the HA SIP TCP or UDP listener used by the softphone.
- Enable the local registrar in Intercom Native setup.
- Create an account with `intercom_native.sip_account_create`.
- If no password is supplied, read it from the `sip_account_created` event or
  the persistent notification. The generated password is shown only once.
- Configure the softphone with HA advertised host, SIP port, username and
  password. Do not configure an external PBX/outbound proxy for local HA
  registration.
- Confirm HA logs show REGISTER and a dynamic phonebook contact for the
  registered softphone.

## Busy Or DND

DND and active-call contention should produce `486 Busy Here` or a terminal
reason of `busy`. Decline should produce `603 Decline` or a configured SIP
final response.

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
- If the SDP has no `telephone-event` and the provider does not send SIP INFO,
  HA cannot read post-answer digits from that provider leg.
- Check `trunk_dtmf_routes` entries use `digits=target`, one route per line.
- Keep `trunk_dtmf_timeout_ms` short, normally 1000 ms and never above 2000 ms.
- If no digits arrive, HA uses `trunk_inbound_default_target`.
- If digits arrive but do not resolve, HA logs them and terminates the answered
  trunk leg as `route_not_found`.

## Card State Looks Wrong

- ESP mirror cards should follow ESPHome entity state and ESP buttons. They do
  not own RTP counters for the HA softphone leg.
- HA softphone cards should follow `intercom_native` softphone snapshots/events.
  The card must not infer terminal state locally.
- Hard-refresh the dashboard after upgrading the frontend resource.
- If multiple browsers are open, only the browser that attached the HA
  softphone media WebSocket owns live browser audio for the active HA call.
