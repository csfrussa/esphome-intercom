# 2026.7.1-dev: Qualified SIP/VoIP, Real-Time Audio And PBX Hardening

> [!WARNING]
> This is a GitHub development pre-release for manual source/device testing,
> not for installation through HACS. Keep HACS pre-release tracking disabled;
> the normal HACS path remains on stable `2026.7.0`.

This refresh keeps the HA-anchored PBX primitives introduced by the first
`2026.7.1-dev` snapshot and adds the complete SIP, real-time, frontend and
hardware qualification pass.

## Manual Test Installation

Use the `v2026.7.1-dev` GitHub tag or a fresh `dev` checkout. For the Home
Assistant integration, manually copy `custom_components/voip_stack` into the
Home Assistant configuration's `custom_components` directory, restart Home
Assistant and refresh the browser frontend cache.

For firmware testing from a checkout, route every maintained project source to
the four `dev` branches before compiling:

```bash
./scripts/yaml_paths.sh remote \
  --intercom dev \
  --voip dev \
  --audio dev \
  --runtime dev
./scripts/yaml_paths.sh check
```

Do not enable HACS pre-release tracking for this test. The HACS installation
path remains the stable `2026.7.0` release.

The installed integration and Lovelace card footer must both report
`v2026.7.1-dev` after restart/cache refresh.

## Home Assistant PBX And Dial Plan

- Home Assistant publishes its own SIP endpoint identity and remains the
  softphone, router, bridge, registrar, conference focus and optional trunk
  client.
- ESP devices, HA, registered SIP endpoints and manual contacts use the same
  central phonebook and routing model.
- Ring groups implement winner-takes-all forking: first answer wins, early
  losers receive CANCEL and confirmed non-winners are cleaned up.
- Conference groups use an HA-hosted media focus. Multiple participants can
  join, optional members can be invited and owner/room teardown is bounded.
- Extension, ring-group and conference-group membership stays data-driven and
  propagates through the endpoint/roster surface.
- Roster capability metadata supports directional audio negotiation instead of
  forcing one global format.

## SIP And RTP Hardening

- UDP and TCP transactions validate Call-ID, CSeq, method, Via branch, dialog
  tags and peer ownership.
- INVITE retransmission, provisional/final timers and required ACK behavior are
  deterministic.
- CANCEL versus `200 OK`, repeated `200 OK`, BYE and connection teardown races
  now preserve SIP dialog rules.
- Reused SIP/TCP connections have serialized writers and bounded pending work.
- RTP port reservations, group legs, conference members, queues, transaction
  caches, nonces and registrations have explicit capacity/ownership limits.
- Different TX and RX PCM formats can be negotiated and bridged with explicit
  conversion where supported.
- Unknown or unregistered callers are accepted when they can reach the listener
  and pass ordinary destination, busy/DND and SDP checks. The phonebook is not
  an inbound allowlist.
- Session-changing re-INVITE, including hold, receives `488 Not Acceptable
  Here`; the original dialog/media remains usable and can later receive BYE.
- Trunk digit routing consumes RTP `telephone-event`. SIP INFO is acknowledged
  but its body is not a digit source.

## HA Softphone, Card And Browser Audio

- The Lovelace card is the UI/audio surface for the HA softphone; it does not
  duplicate backend routing or call state.
- Endpoint discovery is single-flight and startup retry is bounded.
- DND, terminal state, ringtone, cleanup and device-local preferences no longer
  race stale frontend state.
- Option rows consistently align labels on the left and fields, selectors and
  checkboxes on the right in both ESP mirror and HA softphone modes.
- Caller/destination strings are rendered through safe text nodes.
- Browser audio has one owner per Call-ID, stateful codecs, absolute pacing and
  bounded queues/counters.
- Debug audio capture is opt-in, path-safe, stored in a private directory and
  retention-limited to 24 files / 64 MiB.

The current ESP mirror surface can keep the manual keypad and the selected
endpoint's options visible together. The keypad still calls through that ESP's
own phonebook/`start_call` path; Auto Answer, DND, extension and group controls
remain ESP-owned entities rather than card-local routing state.

![ESP mirror keypad and options](https://raw.githubusercontent.com/n-IA-hane/esphome-intercom/v2026.7.1-dev/docs/images/esp-mirror-card-keypad-options.png)

_Real `v2026.7.1-dev` ESP mirror card with keypad and endpoint options
expanded._

## ESP VoIP And Real-Time Audio

- SIP/SDP parsing and UTF-8 fields are size-bounded.
- UDP payload validation prevents advertised frames that exceed the configured
  datagram budget.
- The VoIP hot path uses blocking sockets, notifications and real deadlines;
  it does not add polling sleeps to mask contention.
- The AFE worker is persistent and event-driven. It parks while idle, drains
  before detach/reconfigure and avoids per-frame task create/delete.
- Converter state and working buffers are prepared outside the per-frame path.
- Single-mic AFE uses the direct ESP-SR feed/fetch path; dual-mic AFE uses the
  GMF manager/pipeline bridge.
- With a processor enabled, unavailable output fails closed to silence. The
  parent AEC switch is an explicit raw-mic bypass on the same microphone
  surface.
- Runtime-controller tables and reentrant queues are bounded; derived activity
  evaluation is order-independent and cyclic definitions are rejected.

## Validation

Automated gates:

- Home Assistant/integration/frontend/tooling: 281 tests plus 25 subtests;
- ESP VoIP stack: 55 tests;
- audio/AFE: 19 tests;
- runtime controller: 6 tests;
- virtual VoIP scenarios: 27 passed;
- qualification matrix: 2,162 valid combinations;
- terminal-state regression: 1,000 seeded repetitions passed;
- maintained YAML path check: 17/17 passed.

Real endpoints and media:

- HA to/from WS3 and Spotpear, ESP-to-ESP, registered endpoints and HA browser
  softphone;
- ring group, conference, DND, auto-answer, trunk cancel and immediate
  post-hangup reuse;
- caller identities absent from both registration and phonebook;
- re-INVITE/hold rejection while preserving the established call;
- PCMA 8 kHz, L16 48 kHz and Opus 48 kHz where supported by each leg;
- real incoming/outgoing browser AudioWorklet + WebSocket + RTP calls with zero
  observed drop/underrun in the final runs;
- concurrent music, TTS and bidirectional VoIP with heap, PSRAM and main-loop
  monitoring;
- clean ESPHome 2026.6.5 builds and concurrent OTA return-to-online for both S3
  devices.

## Breaking And Security Boundaries

Read [`BREAKING_CHANGES.md`](BREAKING_CHANGES.md) before upgrading custom dev
YAML or automations. In particular:

- structured ESP contacts use `ip`/`transport`; the HA phonebook owns richer
  endpoint and dial-plan fields;
- caller admission is a network/SBC policy, not a hidden phonebook allowlist;
- full SDP hold/resume renegotiation is not implemented;
- ESP SIP/RTP is plaintext and belongs on a trusted LAN/VPN or behind an SBC;
- SIP TLS, SRTP, ICE/TURN, REFER/transfer and advanced session timers remain
  outside this pre-release profile.

When reporting a regression, include the exact YAML, ESPHome and Home Assistant
versions, caller/destination path, negotiated TX/RX formats and the relevant HA
and ESP logs.
