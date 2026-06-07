# Intercom Wire Protocol

This document is the shared PBX-lite wire contract for the ESPHome
`intercom_api` component and the Home Assistant `intercom_native` integration.
Python and C++ implementations must treat this file and the test fixtures as
the source of truth.

## Transport Model

TCP and UDP use the same control message format.

- TCP: every frame is `MessageHeader + body` on `tcp_port` (default 6054).
- UDP control: every datagram is `MessageHeader + body` on
  `udp_control_port` (default 6055).
- UDP audio: raw L16 PCM datagrams on `udp_audio_port` (default 6054). No
  `MessageHeader` is present on the audio socket.

## Header

All framed control messages start with a 3-byte little-endian header:

```text
type:   u8
length: u16 little-endian body byte length
```

The header is intentionally serialized field-by-field. Do not `memcpy` a C
struct onto the wire.

## Message Types

| Code | Name | Body |
|---:|---|---|
| `0x01` | `AUDIO` | TCP-only raw L16 PCM bytes. UDP audio uses the audio socket without a header. |
| `0x02` | `START` | `call_id` prefix + caller/destination strings. |
| `0x03` | `HANGUP` | `call_id` only. Established-call BYE. |
| `0x04` | `PING` | Empty call id prefix (`00`). |
| `0x05` | `PONG` | Empty call id prefix (`00`). |
| `0x06` | `ERROR` | `call_id` prefix + `error_code:u8` + detail string. |
| `0x07` | `RING` | `call_id` only. Destination is ringing locally. |
| `0x08` | `ANSWER` | `call_id` only. Destination accepted. |
| `0x09` | `DECLINE` | `call_id` prefix + reason string. |

## Body Strings

The common prefix is:

```text
call_id_len: u8
call_id:     UTF-8 bytes
```

Every additional string is length-prefixed the same way:

```text
len:   u8
value: UTF-8 bytes
```

Maximum lengths:

| Field | Max bytes |
|---|---:|
| `call_id` | 64 |
| `route_id` | 64 |
| display name | 64 |
| reason/detail | 160 |

UTF-8 is passed through. Receivers should reject truncated fields and tolerate
unknown free-form text in reason/detail fields.

## START

```text
call_id_prefix
caller_route: lp-string
caller_name:  lp-string
dest_route:   lp-string
dest_name:    lp-string
```

`call_id` remains human-readable: `Caller<->Destination`.

Friendly names are the public endpoint identity. `route` fields may be the same
friendly name during the current protocol generation; implementations must not
replace the display destination with HA when HA is only bridging a cross-protocol
call.

## HANGUP vs DECLINE

- `HANGUP(call_id)` is the normal BYE for an established call.
- `DECLINE(call_id, "")` is setup-phase cancel/dismiss and renders as ordinary
  `remote_hangup` on the peer.
- `DECLINE(call_id, reason)` carries a user-visible reason verbatim.

Canonical reasons include:

```text
local_hangup
remote_hangup
remote_device_lost
declined
timeout
busy
unreachable
protocol_error
bridge_error
DND
```

Free-form reasons are valid. HA as PBX-lite must forward reason strings, not
consume or rewrite them. DND is implemented as `DECLINE("DND")`.

## ERROR

`ERROR` is for protocol/transport faults, not normal user decline.

```text
call_id_prefix
error_code: u8
detail:     lp-string
```

Current code defines `BUSY = 0x01`, but normal busy signaling should prefer
`DECLINE("busy")` when a call is being rejected cleanly.

## Audio

| Parameter | Value |
|---|---:|
| Sample rate | 16000 Hz |
| Format | signed 16-bit little-endian PCM |
| Channels | mono |
| Nominal frame | 512 samples / 1024 bytes / 32 ms |

TCP `AUDIO` frames carry raw PCM as the body. UDP audio datagrams carry only raw
PCM, without the 3-byte header.

## Keepalive

PING/PONG bodies are one byte: `00`. The current ESP constants use a 5 second
interval and a 15 second peer-lost deadline. When the deadline expires during a
call, callers should return to idle with `remote_device_lost`.

## Canonical Fixture Examples

The Python test suite under `tests/test_intercom_protocol.py` pins these binary
fixtures:

```text
PING frame:
04 01 00 00

START A<->B, A/"Panel A" to B/"Panel B":
02 1a 00 05 41 3c 2d 3e 42 01 41 07 50 61 6e 65 6c 20 41 01 42 07 50 61 6e 65 6c 20 42

DECLINE A<->B, reason DND:
09 0a 00 05 41 3c 2d 3e 42 03 44 4e 44

ERROR A<->B, code 1, detail busy:
06 0c 00 05 41 3c 2d 3e 42 01 04 62 75 73 79
```

When any implementation changes framing, message constants, or reason/error
semantics, these fixtures must be updated intentionally in the same change.
