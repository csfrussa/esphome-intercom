# voip-pcm/1

`voip-pcm/1` is the VoIP Stack profile.

It is a SIP/SDP/RTP profile, not a proprietary intercom protocol. ESP devices
act as SIP user agents and exchange RTP PCM media. Home Assistant may provide
roster distribution, routing, bridging and optional provider trunk registration,
but direct ESP-to-ESP calls must work when a SIP URI is known.

## Security And Registration

Phase 1 intentionally does not implement SIP authentication or registration on
ESP devices.

- ESP does not require `Authorization`.
- ESP does not send `WWW-Authenticate`.
- ESP does not implement SIP `REGISTER`.
- If a remote endpoint returns `401` or `407`, the call fails with
  `auth_required_unsupported` or `proxy_auth_required_unsupported`.

ESP trust is provided by the existing ESPHome/Home Assistant management channel
for roster delivery. SIP signaling remains unauthenticated on the local network.

Home Assistant may register one optional provider/PBX trunk. That registration
belongs to HA only and does not create SIP accounts for ESP devices.

## SIP Core

Required SIP methods:

- `INVITE`
- `ACK`
- `CANCEL`
- `BYE`
- `OPTIONS`
- `REGISTER` on the HA trunk client only
- `INFO` is accepted by HA as a SIP method; DTMF routing is based on
  `telephone-event` RTP

Required responses:

- `100 Trying`
- `180 Ringing`
- `200 OK`
- `405 Method Not Allowed`
- `486 Busy Here`
- `487 Request Terminated`
- `488 Not Acceptable Here`
- `500 Server Internal Error`
- `501 Not Implemented`
- `603 Decline`

Unsupported methods are rejected explicitly. Unsupported media is rejected with
`488 Not Acceptable Here`.

SIP signaling transports:

- UDP on the configured SIP listen port
- TCP on the same configured SIP listen port

RTP audio remains UDP in the current profile, even when SIP signaling is TCP.

## SDP And RTP

ESP media is PCM-only.

- Mandatory: RTP `L16`
- Optional: RTP `L24`, only when packed as RTP L24
- Dynamic payload types: `96..127`
- SDP uses `a=rtpmap`, `a=ptime`, `a=maxptime`
- SDP must not use `a=fmtp` for packet time
- Default maximum RTP payload is 1200 bytes

ESP must convert at the RTP boundary:

- internal `S16LE` to RTP `L16` network byte order
- internal `S24LE_IN_S32` to packed RTP `L24` network byte order

Compressed codecs are not implemented on ESP. If a SIP softphone offers only
compressed codecs such as PCMU, PCMA, Opus, Speex, GSM or G.722, ESP returns
`488 Not Acceptable Here`.

## Roster

The public phonebook contract is canonical JSON. It resolves user-facing names,
extensions and phone numbers into either direct SIP URIs or Home Assistant
routes.

Direct calls use SIP URIs such as:

- `sip:Kitchen@192.168.1.51:5060;transport=tcp`
- `sip:Salotto@192.168.1.52:5060;transport=udp`

Phone numbers and unresolved names route through the Home Assistant SIP bridge when
configured. If an optional HA trunk is configured and registered, unresolved
external targets can route through that trunk. Missing endpoint metadata must
not invent a direct SIP route.

Inbound trunk calls can use RFC2833/telephone-event DTMF digits as local
extension selectors. Digits are mapped by HA to central phonebook `extension`
values; ESP devices do not need provider-side extensions or registrations.

## State

SIP events are the source of truth for VoIP phone state.

- outgoing `INVITE` -> calling
- incoming `INVITE` accepted -> ringing
- `180` -> remote ringing
- `200` + `ACK` -> connecting/in_call
- `CANCEL` before answer -> cancelled, never in_call
- `BYE` -> disconnected
- `486` -> busy
- `488` -> media incompatible
- `401`/`407` -> authentication unsupported

Cards render the state of their owner only:

- HA softphone card mirrors the HA softphone.
- ESP mirror card mirrors one ESP.

HA trunk registration status is observability data on the HA softphone snapshot;
it is not an ESP phone state.
