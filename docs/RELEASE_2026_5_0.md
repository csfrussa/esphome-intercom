# Release notes 2026.5.0

This release note is kept only as historical context for the pre-SIP
architecture. Current development has superseded it with the SIP/SDP/RTP
migration documented in `RELEASE_2026_7_0_DEV.md`, `PHONEBOOK_PROTOCOL.md`,
`voip_profile.md` and `codex_voip_test_matrix.md`.

The active architecture is intentionally breaking:

- ESP devices are SIP phones.
- Home Assistant is a SIP softphone and SIP router/bridge.
- SDP offer/answer selects PCM media formats.
- RTP/UDP carries media.
- Endpoint rows use `Name|host|sip_port|rtp_port|sip_udp` or the extended
  `Name|host|sip_port|rtp_port|audio_mode|tx_formats|rx_formats|sip_tcp`
  shape.
- `intercom_api.protocol` selects SIP signaling transport only: `udp` means
  SIP/UDP and `tcp` means SIP/TCP.

Older proprietary call-control descriptions from this release are obsolete and
are not a compatibility contract.
