# intercom-sip-pcm/1

`intercom-sip-pcm/1` is the ESPHome Intercom VoIP profile.

It is a SIP/SDP/RTP profile, not a proprietary intercom protocol. ESP devices
act as SIP user agents and exchange RTP PCM media. Home Assistant may provide
roster distribution, routing, bridging and future transcoding, but direct
ESP-to-ESP calls must work when a SIP URI is known.

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

## SIP Core

Required SIP methods:

- `INVITE`
- `ACK`
- `CANCEL`
- `BYE`
- `OPTIONS`

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

RTP media remains UDP.

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

- `sip:Cucina@192.168.1.51:5060;transport=tcp`
- `sip:Salotto@192.168.1.52:5060;transport=udp`

Phone numbers and unresolved names route through Home Assistant/PBX when
configured. Missing endpoint metadata must not invent a direct SIP route.

## State

SIP events are the source of truth for intercom state.

- outgoing `INVITE` -> calling
- incoming `INVITE` accepted -> ringing
- `180` -> remote ringing
- `200` + `ACK` -> connecting/streaming
- `CANCEL` before answer -> cancelled, never streaming
- `BYE` -> disconnected
- `486` -> busy
- `488` -> media incompatible
- `401`/`407` -> authentication unsupported

Cards render the state of their owner only:

- HA softphone card mirrors the HA softphone.
- ESP mirror card mirrors one ESP.
