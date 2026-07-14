# Experimental SIP Video

VoIP Stack can optionally turn the Home Assistant softphone card into a SIP
video phone for standard SIP phones, softphones and door stations. This is an
experimental `2026.7.2` feature. ESPHome endpoints remain audio-only.

Video is disabled by default and does not alter an audio-only installation.
Open the VoIP Stack integration, choose **Reconfigure**, then enable
**Experimental SIP video for the Home Assistant softphone**. A second step
offers two independent capabilities:

- **Enable video transcoding** lets the HA softphone receive H.263,
  H.263-1998 or H.265 through the FFmpeg binary already available to Home
  Assistant.
- **Allow browser camera transmission** exposes a **Send Camera** control in
  the card. Each browser stores its own choice and asks for its own camera
  permission. Receiving video never requires camera access.

If video setup, decoding, camera permission or transcoding fails, the SIP
dialog and browser audio remain active whenever audio negotiation succeeded.

## Capability Matrix

The direct browser path does not decode and re-encode video on the HA server:

| Negotiated SIP codec | Receive in card | Send browser camera | Server transcode |
| --- | --- | --- | --- |
| H.264 Baseline, Main or High | Direct when supported by the browser | Direct for packetization mode 1 | No |
| VP8 | Direct | Direct | No |
| JPEG over RTP | Direct | No | No |
| H.263 | Optional | No | Receive-only to VP8 |
| H.263-1998 / H.263-2000 | Optional | No | Receive-only to VP8 |
| H.265 / HEVC | Optional | No | Receive-only to VP8 |
| Audio-only or unsupported video | Audio continues | No | No |

The profile supports:

- incoming and outgoing calls owned by the Home Assistant softphone;
- `sendrecv`, `sendonly`, `recvonly` and `inactive` SDP directions;
- one negotiated video stream, using the first accepted `m=video` section;
- H.264 single NAL units, STAP-A and FU-A;
- VP8 RTP payloads and RFC 2435 JPEG reassembly;
- sequence reordering with bounded queues and damaged-access-unit rejection;
- RTP/AVP by default and RTP/AVPF when offered by the remote endpoint;
- compound RTCP receiver reports, plus negotiated AVPF PLI or FIR key-frame
  requests;
- symmetric RTP source latching for ordinary SIP devices behind NAT;
- one authenticated browser media owner for the active HA call;
- exact-codec RTP and RTCP relay between two HA-owned standard SIP legs.

The exact-codec bridge changes only the leg-local RTP payload type. Encoded
payload, timestamp, sequence, marker, SSRC and RTP extensions remain intact.
H.264 packetization mode, profile and RTP transport profile must match, and
other codecs must have matching normalized format parameters. Different
codecs are not transcoded between two SIP endpoints.

## Card Behavior And Privacy

Received video becomes the background of the in-call card. The caller,
duration and call state move into a full-width hang-up bar at the bottom so the
picture remains the primary content. The layout follows Home Assistant
Sections sizing from compact 6-column cards through wide and tall cards.
Long caller names are truncated inside the bar rather than widening the page.

Detailed codec, packet and frame counters appear only when VoIP Stack debug
mode is enabled. The normal card does not cover the picture with diagnostics.

Camera transmission has two gates. The integration-level option must first be
enabled, then the user must turn on **Send Camera** in that browser. Browser
permission denial stops only the outgoing camera track. Incoming video, audio
and call controls continue independently. Reloading the dashboard during
ringing or an established call transfers media ownership to the new card and
releases the old WebSocket deterministically.

## Direct And Transcoded Media Paths

Direct receive path:

```text
SIP peer RTP -> bounded RTP reorder/depacketizer -> authenticated HA WebSocket
             -> browser WebCodecs or JPEG decoder -> card canvas
```

Direct camera path:

```text
browser camera -> WebCodecs H.264/VP8 encoder -> authenticated HA WebSocket
               -> RTP packetizer -> SIP peer
```

Optional legacy-codec receive path:

```text
SIP H.263/H.263-1998/H.265 RTP -> local FFmpeg subprocess -> VP8 RTP
                                -> authenticated HA WebSocket -> browser
```

There is no intermediate recording or complete video file. FFmpeg receives
and emits RTP continuously on loopback. The transcode path is intentionally
bounded to one active call, one FFmpeg thread, at most 1280 pixels of width,
15 frames per second, 700 kbit/s target and 900 kbit/s maximum output. A second
simultaneous transcode remains audio-only instead of starting unbounded codec
workers.

VoIP Stack first uses the FFmpeg binary configured by Home Assistant and then
falls back to `ffmpeg` on the host path. Home Assistant OS and Home Assistant
Container already include FFmpeg. Home Assistant Core installations must make
it available separately, as described by the
[Home Assistant FFmpeg integration](https://www.home-assistant.io/integrations/ffmpeg/).

VoIP Stack does not bundle, modify or redistribute FFmpeg or codec libraries.
It starts the installation's existing executable only when transcoding was
explicitly enabled and is required by a call. FFmpeg builds can be LGPL or GPL
depending on their enabled components; distributors of a custom binary remain
responsible for that binary under the
[FFmpeg licensing guidance](https://ffmpeg.org/legal.html). This paragraph is
informational, not legal advice.

## Browser And Network Requirements

- Use Home Assistant through HTTPS or a browser-recognized secure local
  context. WebCodecs and camera capture are secure-context features.
- Use a current browser. Codec availability is checked at runtime because it
  can depend on the browser, operating system and installed media support.
- Permit camera access only when HA must transmit video. A remote `sendonly`
  door station needs no HA camera permission.
- Keep SIP and RTP on a trusted LAN or VPN. This profile uses unencrypted
  SIP/RTP and does not add SRTP, ICE, STUN or TURN.
- Allow the configured HA RTP range through local firewalls. A video call
  reserves an additional even RTP/odd RTCP pair alongside audio media.

## Deliberate Limits

This experimental profile does not claim support for:

- video on ESPHome endpoints;
- video through Assist, ring groups or conference rooms;
- cross-codec transcoding between two standard SIP endpoints;
- VP9, AV1, MxPEG or other proprietary payloads;
- more than one simultaneous server-side transcode;
- more than one browser owning an active HA video stream;
- SRTP, DTLS, ICE, STUN or TURN;
- RTCP multiplexing, generic NACK retransmission or bandwidth adaptation;
- recording, snapshots or a camera entity;
- established-dialog video renegotiation, hold or transfer;
- IPv6 RTP media.

Unsupported video is rejected in the SDP answer with a zero media port while
a compatible audio section remains active. The DTMF pre-answer trunk path also
remains audio-only because it answers before the final phonebook destination
is known.

## Qualification

The repository includes a deterministic SIP peer and an authenticated
Playwright probe. They exercise the real integration, card, WebSocket and RTP
paths rather than checking only source strings:

```bash
export HA_URL="https://home-assistant.example/dashboard/voip"
export PLAYWRIGHT_STORAGE_STATE="$HOME/.cache/ha-playwright-state.json"
python tools/experimental_sip_video_browser_probe.py \
  --reload-in-call \
  --out /tmp/incoming-video.json
```

Start the probe, then call HA with the peer:

```bash
python tools/experimental_sip_video_peer.py \
  --host home-assistant.example \
  --port 5060 \
  --target HA \
  --codec h264 \
  --direction sendrecv \
  --out /tmp/video-peer.json
```

Add `--video-profile RTP/AVPF` to qualify standards-aligned PLI/FIR feedback.
The default remains `RTP/AVP`, and feedback attributes are never advertised on
that profile.

The peer also supports `audio`, `h263`, `h263p`, `h265`, `jpeg` and `vp8`.
The probe records card and backend state, negotiated direction, audio and video
counters, WebCodecs errors, canvas pixels, responsive geometry and post-call
resource ownership. It fails if the card overflows, media does not arrive or
the call leaves sessions, dialogs, RTP owners, transcoders or cleanup tasks
behind.

The local disposable HA Core harness under `tools/ha_voip_lab/` is a test
environment, not an addon or runtime dependency. It keeps synthetic calls away
from household ESP devices and production trunks.

Qualification for this implementation covered:

- direct H.264, VP8 and JPEG receive;
- H.263, H.263-1998 and H.265 receive through FFmpeg;
- H.264 and VP8 bidirectional camera calls;
- local hangup, remote BYE and caller CANCEL while ringing;
- audio-only calls with all video options enabled;
- camera permission denial without losing incoming media;
- reload while ringing and while media is active;
- exact-codec RTP/RTCP relay contracts;
- RTP/AVP compatibility and RTP/AVPF compound RR/SDES/PLI feedback;
- repeated mixed-codec calls with zero post-call owners or RTP sockets;
- compact, default, wide and tall Home Assistant card sizes.

The protocol work follows
[RFC 3264 offer/answer](https://www.rfc-editor.org/rfc/rfc3264),
[RFC 3550 RTP/RTCP](https://www.rfc-editor.org/rfc/rfc3550),
[RFC 4585 RTP/AVPF](https://www.rfc-editor.org/rfc/rfc4585),
[RFC 6184 H.264 RTP](https://www.rfc-editor.org/rfc/rfc6184),
[RFC 7741 VP8 RTP](https://www.rfc-editor.org/rfc/rfc7741),
[RFC 2435 JPEG RTP](https://www.rfc-editor.org/rfc/rfc2435),
[RFC 4629 H.263 RTP](https://www.rfc-editor.org/rfc/rfc4629),
[RFC 7798 HEVC RTP](https://www.rfc-editor.org/rfc/rfc7798) and
[RFC 4961 symmetric RTP](https://www.rfc-editor.org/rfc/rfc4961).
Browser media uses the
[W3C WebCodecs API](https://www.w3.org/TR/webcodecs/) and the
[AVC Annex B registration](https://www.w3.org/TR/webcodecs-avc-codec-registration/).
Physical door-station qualification remains device-specific. Advertising a
codec in SDP does not prove that every device's exact RTP behavior has been
tested.
