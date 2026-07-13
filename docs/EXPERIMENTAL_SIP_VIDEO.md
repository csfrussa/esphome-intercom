# Experimental SIP Video

VoIP Stack can optionally turn the Home Assistant softphone card into a native
SIP video phone. This is an experimental `2026.7.2` feature for standard SIP
phones and door stations. ESPHome endpoints remain audio-only.

The feature is disabled by default. Open the VoIP Stack integration, choose
**Reconfigure**, enable **Experimental SIP video for the Home Assistant
softphone**, and restart Home Assistant when prompted. An ordinary audio call
continues to work when the peer does not offer a compatible video stream.

## Supported Path

The experimental profile currently supports:

- incoming and outgoing calls owned by the Home Assistant softphone;
- standard SIP endpoints reached by a registered account, phonebook contact or
  direct SIP URI;
- H.264 Constrained Baseline or Baseline over RTP/AVP at 90 kHz;
- RFC 6184 packetization mode 1, including single NAL units, STAP-A and FU-A;
- one negotiated video stream, using the first `m=video` section in the offer;
- `sendrecv`, `sendonly`, `recvonly` and `inactive` SDP directions;
- independent audio negotiation using the existing PCMA, PCMU, Opus and PCM
  capabilities of the HA softphone;
- symmetric RTP source latching for ordinary SIP devices behind NAT;
- one authenticated browser video owner for the active HA call.

The video is rendered behind the active-call controls. The call identity and
Answer, Decline and Hang Up controls remain visible, and the controls move
toward the lower edge to preserve the useful picture area.

Audio and video deliberately fail independently. A denied camera permission or
an unavailable H.264 encoder does not hide a valid incoming door-station
stream. A decoder limitation does not stop a valid outgoing camera stream. If
no negotiated video direction can run, the SIP dialog and browser audio remain
usable as an audio-only call.

## Media Architecture

VoIP Stack does not save a video file and does not run a server-side video
transcoder. Incoming H.264 RTP is validated and reassembled into bounded Annex
B access units, then delivered through an authenticated Home Assistant
WebSocket. The browser decodes those access units with WebCodecs. In the other
direction, WebCodecs encodes the browser camera as H.264 Annex B and the backend
packetizes it into RFC 6184 RTP.

This keeps audio call control independent from video startup and browser camera
prompts. It also avoids adding a heavyweight codec pipeline to Home Assistant.
RTP queues, NAL units and complete access units have explicit size limits, and
sequence gaps discard the damaged access unit instead of feeding partial video
to the decoder.

## Browser And Network Requirements

- Use Home Assistant through HTTPS. WebCodecs and camera capture require a
  secure browser context.
- Use a current browser with H.264 WebCodecs support. Codec availability can
  still depend on the operating system and browser build.
- Permit camera access only when the HA side must transmit video. A remote
  `sendonly` door station needs no HA camera permission.
- Keep SIP and RTP on a trusted LAN or VPN. This experimental profile uses
  unencrypted SIP/RTP and does not add [SRTP](https://www.rfc-editor.org/rfc/rfc3711),
  ICE, STUN or TURN.
- Allow the configured HA RTP range through local firewalls. Video reserves a
  second UDP media port next to the audio port for that call.

## Deliberate Limits

The first experimental profile does not claim support for:

- video on ESPHome endpoints;
- H.265, VP8, VP9, MJPEG or proprietary camera formats such as MxPEG;
- SRTP, DTLS, ICE, STUN or TURN;
- RTCP feedback, PLI, FIR, NACK or bandwidth adaptation;
- server-side transcoding, recording or snapshots;
- video relay through ring groups, conference rooms or Assist;
- established-dialog video renegotiation, hold or transfer;
- more than one browser consuming the active HA video stream;
- IPv6 RTP media.

Unsupported video media is rejected in the SDP answer with a zero media port,
while a compatible audio section remains active. Unsupported extra media
sections cannot invalidate an otherwise valid IPv4 audio call.

## Qualification

The development probe can exercise a real card instead of checking only source
contracts. It requires an authenticated Playwright storage-state file:

```bash
export HA_URL="https://home-assistant.example/dashboard/voip"
export PLAYWRIGHT_STORAGE_STATE="$HOME/.cache/ha-playwright-state.json"
python tools/experimental_sip_video_browser_probe.py \
  --out /tmp/incoming-video.json
```

Place an H.264 SIP call while the probe waits. For an outgoing test, specify a
registered phonebook target and optionally force a page reload while it rings:

```bash
python tools/experimental_sip_video_browser_probe.py \
  --outbound "Video phone" \
  --reload-during-ring \
  --reload-in-call \
  --out /tmp/outgoing-video.json
```

The JSON result records the complete card state, call ownership, negotiated
direction, browser access-unit counters, WebCodecs errors and non-black canvas
evidence. Backend RTP counters are written to the normal teardown log without
being exposed as call-lifecycle events. A compatible test peer can be built with
[bareSIP](https://github.com/baresip/baresip) using its `avcodec` and
`fakevideo` modules.

The implementation follows [RFC 3264 offer/answer](https://www.rfc-editor.org/rfc/rfc3264),
[RFC 6184 H.264 RTP](https://www.rfc-editor.org/rfc/rfc6184) and the
[RFC 4961 symmetric RTP recommendation](https://www.rfc-editor.org/rfc/rfc4961).
Browser codec access uses the standard
[W3C WebCodecs API](https://www.w3.org/TR/webcodecs/) and its
[AVC Annex B registration](https://www.w3.org/TR/webcodecs-avc-codec-registration/),
so H.264 availability is checked at runtime rather than assumed from the
browser name. Physical door-station qualification remains device-specific; a
model advertising H.264 SIP video is not automatically proof that its exact
SDP and RTP behavior has been tested.
