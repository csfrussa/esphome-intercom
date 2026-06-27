# Troubleshooting

## ESP Does Not Ring

- Confirm the peer sends a SIP `INVITE` to the ESP `sip_port`.
- Check ESP `intercom_api` `protocol` matches the peer signaling transport.
- Verify SDP offers at least one compatible PCM format.
- Inspect `sensor.*_intercom_sip_snapshot` for `last_sip_event`,
  `sip_status_code`, and `terminal_reason`.

## Call Fails With `media_incompatible`

The SDP offer/answer did not produce a usable PCM RTP format, or HA could not
build the required bridge conversion. Use one of the supported PCM profiles such
as `16000:s16le:1:32`, `16000:s16le:1:20`, or `48000:s16le:1:10`.

## HA Cannot Route A Name

- Ensure `sensor.intercom_phonebook` contains the target.
- For local ESP-only routing, declare the contact in `intercom_api.phonebook`.
- Use a direct SIP URI (`sip:name@host:5060`) when bypassing HA.
- Use `ha_bridge: true` when HA must bridge a logical target.

## Busy Or DND

DND and active-call contention should produce `486 Busy Here` or a terminal
reason of `busy`. Decline should produce `603 Decline` or a configured SIP
final response.

## No Audio

- Confirm RTP ports are reachable in both directions.
- Check selected TX/RX formats in the SIP snapshot.
- Check RTP packet/byte counters on both HA and ESP.
- For HA bridge calls, inspect relay logs for conversion/drop messages.
