# Troubleshooting

## ESP Does Not Ring

- Confirm the peer sends a SIP `INVITE` to the ESP `sip_port`.
- Check ESP `intercom_api` `protocol` matches the peer signaling transport.
- Verify SDP offers at least one compatible PCM format.
- Inspect `sensor.*_intercom_sip_snapshot` for `last_sip_event`,
  `sip_status_code`, and `terminal_reason`.

## Call Fails With `media_incompatible`

The SDP offer/answer did not produce a usable PCM RTP format, or HA could not
build the required bridge conversion. Use explicit supported PCM profiles such
as `16000:s16le:1:16`, `16000:s16le:1:32`, `16000:s16le:1:20`, or
`48000:s16le:1:10`. A call also needs one common packet time across the selected
TX and RX directions; rates may differ, but `frame_ms`/`ptime` must match.

## HA Cannot Route A Name

- Ensure `sensor.intercom_phonebook` contains the target.
- For local ESP-only routing, declare the contact in `intercom_api.phonebook`.
- Use a direct SIP URI (`sip:name@host:5060`) when bypassing HA.
- Use `ha_bridge: true` when HA must bridge a logical target.
- For external numbers, confirm the optional trunk is configured and registered.

## Busy Or DND

DND and active-call contention should produce `486 Busy Here` or a terminal
reason of `busy`. Decline should produce `603 Decline` or a configured SIP
final response.

## No Audio

- Confirm RTP ports are reachable in both directions.
- Check selected TX/RX formats in the SIP snapshot.
- Check RTP packet/byte counters on both HA and ESP.
- For HA bridge calls, inspect relay logs for conversion/drop messages.

## Trunk Does Not Register

- Confirm `trunk_enabled` is on; when off, no trunk runtime is created.
- Check `sip_trunk.trunk_status_code`, `trunk_status_reason` and
  `trunk_last_sip_event` in the HA softphone snapshot.
- Confirm provider transport, server, port, username/auth username and password.
- If the provider requires an outbound proxy, set `trunk_outbound_proxy`.

## Inbound Trunk Call Routes To The Wrong Target

- Confirm the provider offers RFC2833/telephone-event DTMF in SDP.
- Check `trunk_dtmf_routes` entries use `digits=target`, one route per line.
- Keep `trunk_dtmf_timeout_ms` short, normally 1000 ms and never above 2000 ms.
- If no route matches, HA logs the received digits and uses
  `trunk_inbound_default_target`.
