# Release notes 2026.5.0

This release note is kept only as historical context for the pre-SIP
architecture. Current development has superseded it with the SIP/SDP/RTP
migration documented in `RELEASE_2026_7_0_DEV.md`, `PHONEBOOK_PROTOCOL.md`,
`voip_profile.md` and `voip_test_matrix.md`.

The active architecture is intentionally breaking:

- ESP devices are SIP phones.
- Home Assistant is a SIP softphone and SIP router/bridge.
- SDP offer/answer selects PCM media formats.
- RTP/UDP carries media.
- Endpoint rows are SIP phonebook entries with a required display name and
  optional address, ports, transport, extension and audio capabilities.
- `voip_stack.transport` selects SIP signaling transport only: `udp` means
  SIP/UDP and `tcp` means SIP/TCP.

Older proprietary call-control descriptions from this release are obsolete and
are not a compatibility contract.
