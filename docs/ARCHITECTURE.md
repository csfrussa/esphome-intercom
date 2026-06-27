# SIP Architecture

## Components

- ESP `intercom_api` is a SIP phone.
- HA `intercom_native` is a SIP softphone, phonebook authority, and optional
  SIP/RTP bridge.
- The Lovelace card mirrors either the HA softphone state or the ESP state
  published through ESPHome entities.

## Call Control

All call control is SIP:

- outbound call: `INVITE`
- provisional ringing: `180 Ringing`
- answer: `200 OK` plus `ACK`
- caller cancellation before answer: `CANCEL` and `487 Request Terminated`
- established hangup: `BYE`
- busy/DND: `486 Busy Here`
- declined: `603 Decline`
- incompatible media: `488 Not Acceptable Here`
- auth challenges unsupported by ESP: `auth_required_unsupported`

## Media

SDP offer/answer negotiates RTP PCM. ESP supports PCM only; compressed codecs
are not implemented. RTP is always UDP, even when SIP signaling uses TCP.

HA bridge owns two SIP dialogs and relays RTP between them. When both legs
negotiate different supported PCM shapes, HA resamples/reframes between the
formats. If conversion is not possible, the bridge fails with
`media_incompatible`.

## State

The public contract is `SipPhoneState`, including:

- state
- call_id
- direction
- caller/callee
- local_uri/remote_uri/contact
- sip_transport
- sip_status_code
- terminal_reason
- selected_tx_format/selected_rx_format
- RTP packet and byte counters
- last_sip_event

## Routing

Direct SIP targets are dialed as SIP URIs. Logical names are resolved through
the local ESP phonebook or the HA roster. HA can be forced as a bridge with
`ha_bridge`.
