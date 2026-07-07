# SIP Architecture

This document describes the active VoIP architecture. It intentionally does not
describe the retired proprietary intercom protocol except by omission: SIP,
SDP and RTP are the functional primitives.

## Product Model

Every ESP running `voip_stack` is a SIP user agent:

- it can originate and receive SIP calls;
- it does not register to a PBX;
- it does not require SIP authentication;
- it accepts compatible PCM SDP and rejects incompatible media with SIP status;
- it can run full-duplex, mic-only, speaker-only or control-only.

Home Assistant is more than a card backend:

- HA is its own SIP softphone endpoint;
- HA is the central phonebook/dial-plan publisher;
- HA is a SIP router/B2BUA for logical names, numbers, bridge requests and
  transport boundaries;
- HA can optionally register one provider/PBX trunk;
- HA can optionally act as a local registrar for standard SIP endpoints such as
  Zoiper, Linphone, baresip or pjsua.

The default install needs no user dialplan. Direct ESP calls happen when the
phonebook has complete direct SIP endpoint data. Logical names, numeric targets
and external numbers go to HA, which decides whether to answer locally, forward
to a local endpoint, bridge, group-call, use the trunk or reject.

## Components

```text
ESP voip_stack
  SIP UA + SDP offer/answer + RTP PCM + local phonebook + SipPhoneState

Home Assistant voip_stack
  HA softphone + SIP UDP/TCP endpoint + router/B2BUA + RTP relay/resampler
  + central phonebook + optional local registrar + optional trunk client

Lovelace card
  ESP mirror mode: ESPHome entities and ESP buttons
  HA softphone mode: HA softphone snapshot, commands and browser audio socket
```

Component ownership:

- `voip_stack` owns ESP SIP signaling, RTP sockets, selected call formats and
  the public ESP call state.
- `esp_audio_stack`, native ESPHome microphone/speaker components, `esp_aec`
  and `esp_afe` own physical audio capture/playback and processing.
- `voip_stack` owns HA-side SIP dialogs, route decisions, trunk
  registration, local SIP endpoint registrations and HA softphone media sessions.
- Cards never own the call FSM. They render state pushed by the owner and send
  user commands back to that owner.

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

ESP devices do not implement provider/PBX registration. HA trunk registration
and HA local SIP registration are separate features that live only in
`voip_stack`.

For outbound INVITE failures, HA sends the required ACK for non-2xx final
responses before surfacing the terminal reason. This keeps failed calls SIP
compliant rather than relying on retry side effects.

## Media

SDP offer/answer negotiates RTP. RTP media is always UDP, even when SIP
signaling uses TCP.

ESP devices are PCM-only endpoints. The supported ESP profile is linear PCM
with network byte order on RTP; incompatible SDP receives `488 Not Acceptable
Here` or the equivalent terminal reason.

HA can accept richer media on softphone/trunk legs when it has a converter for
the selected codec. The goal is best-quality-per-leg:

- a browser or softphone leg can negotiate OPUS or high-rate PCM;
- an ESP speaker leg should receive 48 kHz PCM when its speaker path supports
  it;
- an ESP AFE/AEC mic leg can still transmit 16 kHz PCM because that is the
  processor output surface;
- the HA bridge converts between leg formats instead of forcing the whole call
  to the lowest common endpoint where a per-leg bridge is possible.

Direct ESP-to-ESP standard SIP calls use one common packet duration for the
dialog. TX and RX sample rate may differ when the endpoints and SDP negotiation
support it, but `ptime` must be coherent for the selected dialog. If no
compatible media shape exists, the call fails explicitly.

HA bridge owns two SIP dialogs and relays RTP between them. When both legs
negotiate different supported media shapes, HA decodes/converts/resamples and
reframes between the formats. If conversion is not possible, the bridge fails
with `media_incompatible`.

Inbound provider trunk calls are also two-leg calls. HA answers the trunk leg to
receive DTMF routing digits when a standard digit channel exists, then
originates a normal SIP call to HA softphone or a local phonebook target and
bridges RTP with the same relay.

Current non-goals are RTCP, SRTP and SIP/TLS on ESP devices. The supported
trust boundary is a local LAN/VPN plus Home Assistant and ESPHome API security.
Codec-rich or encrypted external legs should terminate on Home Assistant, where
the bridge can convert and route them to lightweight ESP PCM endpoints.

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

The same state vocabulary is used on ESP and HA:

- `idle`
- `calling`
- `remote_ringing`
- `ringing`
- `connecting`
- `in_call`
- `terminating`
- terminal states such as `busy`, `declined`, `cancelled`,
  `media_incompatible`, `transport_unreachable`,
  `auth_required_unsupported`

Terminal reasons are backend-owned and may contain exact SIP/application
reasons. The frontend must display the supplied reason rather than mapping it
through a private parallel FSM.

HA runtime call ownership is centralized in `CallRegistry`. Pending routes,
pending INVITEs, pre-answered trunk legs, HA softphone media, SIP clients,
bridge clients, relays and client watcher tasks live behind that registry
instead of separate mutable maps. Service handlers, inbound SIP callbacks,
WebSocket audio and debug snapshots all derive active call information from the
same session/leg registry. This avoids HA softphone state being polluted by
router-only bridges and makes bridge teardown propagate BYE/cleanup through the
same call session.

## Routing

Direct SIP targets are dialed as SIP URIs. Logical names are resolved through
the local ESP phonebook or the HA roster.

ESP-origin routing:

- explicit `sip:name@host[:port]` or `name@host[:port]`: direct SIP;
- known ESP with complete host/port/transport and no `ha_bridge`: direct SIP;
- known target without direct route data: HA bridge;
- unknown name: HA bridge;
- numeric target: HA bridge.

HA-router routing:

- HA target: ring HA softphone;
- ESP target: forward/bridge to the ESP SIP endpoint;
- registered local SIP endpoint: forward to its REGISTER Contact;
- external/public number: trunk if registered, otherwise reject
  `trunk_unavailable`;
- disabled entry: reject;
- unresolved explicit route hint: reject `route_not_found`.

The optional SIP trunk is used only when configured and registered. It never
registers ESP devices to the provider. ESP devices remain local SIP user agents;
HA maps provider-side numbers or DTMF extension digits to local SIP targets.

Inbound trunk calls use deterministic policy:

- no explicit route hint: ring HA/default target;
- explicit DTMF/SIP route hint that resolves: bridge to that target;
- explicit DTMF/SIP route hint that does not resolve: terminate
  `route_not_found`.

DTMF is a route-hint source for provider/trunk callers only. Internal ESP
routing uses SIP request context and the phonebook; it does not encode ESP
routing as DTMF.

## Phonebook

The central phonebook is a SIP dial plan. `name` is the only mandatory contact
field. Optional fields include:

- `extension`: local/internal alias;
- `number`: external/public number used through the optional trunk;
- `address`, `sip_uri`, `sip_port`, `rtp_port`;
- `sip_transport`: `udp` or `tcp`;
- `ha_bridge`: force HA bridge routing.

User-facing contacts are data-driven. Name-only contacts route through HA,
`extension` resolves local/internal targets, `number` resolves external trunk
targets, endpoint contacts expose `address` or `sip_uri`, and local SIP
accounts are published by the registrar.

ESP phonebook storage is bounded. The runtime accepts up to 64 normalized
contacts per ESP phonebook and replaces existing names in place. Larger rosters
must be filtered by HA before push rather than relying on dynamic ESP heap
growth during call handling.

## SIP/TCP Backpressure

Every SIP/TCP connection has one governed writer task. Producers enqueue SIP
messages through `SipTcpWriter`; the writer owns `StreamWriter.write()` and
`drain()`. Queue pressure is explicit and logged instead of spawning ad-hoc
drain tasks or writing from multiple call paths. Closing or reconnecting a TCP
leg first closes the governed writer and only then replaces the stream.

This applies to outbound SIP clients, the TCP SIP listener and trunk
registration/call legs. UDP signaling still sends datagrams directly.

## Frontend Contract

Cards do not own call control state.

- The HA softphone card mirrors the HA softphone state pushed by
  `voip_stack`.
- ESP mirror cards use ESPHome entities and controls from the selected ESP.
- Frontend buttons issue commands such as call, answer, decline, hangup or
  contact navigation; they do not infer terminal SIP reasons or run a parallel
  call FSM.

ESP mirror cards are synchronized through ESPHome entities and buttons. Contact
left/right presses go to the ESP and the selected contact shown by the card is
the ESP selected contact. HA softphone cards are synchronized through
`voip_stack` snapshots and events. Browser audio belongs to the HA
softphone leg and is attached through `/api/voip_stack/ws`.

Browser capture runs inside an AudioWorklet. The worklet uses a small reusable
frame pool and posts fixed negotiated frames to the engine. The engine reuses a
single WebSocket send buffer for the negotiated frame size. Playback is paced by
the adaptive jitter buffer in the playback worklet, not by fixed UI-thread
timers. This keeps browser audio resilient across local LAN, HA app, SSL proxy
and remote WebSocket paths without tuning one set of magic delay constants.

## Observability

INFO logs describe user-level SIP progress: incoming call, ringing, answered,
bridged, hangup, trunk registered/unregistered and route decisions. DEBUG logs
carry SIP/SDP/RTP details useful for protocol investigation.

Snapshots expose listener readiness, active dialogs, pending transactions,
selected formats, RTP packet/byte counters, last SIP event/status and terminal
reason. Debug mode increases snapshot and log detail for live investigation.
