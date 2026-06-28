# SIP Architecture

## Components

- ESP `intercom_api` is a SIP phone.
- HA `intercom_native` is a SIP softphone, phonebook authority, SIP/RTP bridge,
  and optional SIP trunk endpoint.
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
- optional HA trunk registration: `REGISTER` with digest auth toward the
  provider/PBX only

## Media

SDP offer/answer negotiates RTP PCM. ESP supports PCM only; compressed codecs
are not implemented. RTP is always UDP, even when SIP signaling uses TCP.

HA bridge owns two SIP dialogs and relays RTP between them. When both legs
negotiate different supported PCM shapes, HA resamples/reframes between the
formats. If conversion is not possible, the bridge fails with
`media_incompatible`.

Inbound provider trunk calls are also two-leg calls. HA answers the trunk leg to
receive DTMF routing digits, then originates a normal SIP call to HA softphone
or a local phonebook target and bridges RTP with the same relay.

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

The optional SIP trunk is used only when configured and registered. It never
registers ESP devices to the provider. ESP devices remain local SIP user agents;
HA maps provider-side numbers or DTMF route digits to local SIP targets.

## Frontend Contract

Cards do not own call control state.

- The HA softphone card mirrors the HA softphone state pushed by
  `intercom_native`.
- ESP mirror cards use ESPHome entities and controls from the selected ESP.
- Frontend buttons issue commands such as call, answer, decline, hangup or
  contact navigation; they do not infer terminal SIP reasons or run a parallel
  call FSM.
