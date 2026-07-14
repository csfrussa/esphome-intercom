# 2026.7.2-dev: Automation-Native Routing And Experimental SIP Video

<!-- Canonical source for the v2026.7.2-dev GitHub pre-release body. -->

> [!IMPORTANT]
> This is a GitHub development pre-release for manual testing. It is not
> offered through HACS; the normal HACS installation remains on stable
> `2026.7.1`.

`2026.7.2-dev` is where the slightly unreasonable ideas that arrived after
stable `2026.7.1` are becoming real: Home Assistant automations can now steer
live calls, and yes, we appear to be getting rather close to a real native
Home Assistant video phone. This is still a development build, so install it
when you actually want to test the new toys and tell us what breaks.

## 🧩 Native ESPHome Platform Boundaries

- The ESPHome VoIP core no longer auto-loads primary entity platforms. Button,
  number, switch, text and text-sensor support is compiled only when explicitly
  configured, following ESPHome's upstream component rules.
- Minimal VoIP YAML now compiles without empty `text:` or `text_sensor:` blocks.
  Maintained full-experience YAML keeps the same controls through explicit
  native `platform: voip_stack` entities.
- Both a platform-free ESP32-S3 fixture and a fixture containing every optional
  entity platform are compiled as regression tests. The full P4 landscape
  configuration is also compiled against the same component source.

## 📞 More Reliable Outbound Trunk Calls

- Home Assistant now uses the configured trunk username as the SIP `From` and
  `Contact` identity for outbound trunk calls, while the friendly softphone name
  remains separate in the card and HA state.
- Digest authentication still uses the independently configurable auth
  username. This supports providers where the address-of-record and digest
  identity differ.
- The authenticated retry keeps the correct Request-URI, Call-ID, incremented
  CSeq and fresh Via branch. The change was validated with a real `407 Proxy
  Authentication Required` trunk exchange.

## ☎️ Hang Up Means Hang Up

- The Lovelace card keeps Hang Up available throughout `calling`, `connecting`
  and remote ringing, including while the original start request is pending.
- The terminal call result remains available in HA state for diagnostics, but
  the card presents it for five seconds and then returns to Ready. Later option
  or roster updates do not resurrect an old hangup reason.
- Outbound INVITE transactions now have one signaling owner shared by HA
  softphone, bridges, ring groups and conference invitations.
- Cancellation follows the SIP transaction lifecycle: if no provisional
  response has arrived, CANCEL is deferred; after `100`, `180` or `183`, it is
  sent immediately with the original Call-ID, CSeq and Via branch.
- The remote endpoint receives a real CANCEL and terminates the INVITE with
  `487 Request Terminated`; HA acknowledges it and remains idle instead of
  returning to remote ringing because of a late provisional response.
- The terminal event now preserves the call's canonical direction. An outbound
  call no longer changes from `outgoing` to `incoming` when HA hangs up while
  its session is still present in the call registry.
- If a successful `200 OK` crosses the cancellation, HA acknowledges the dialog
  and ends it with BYE, preventing ghost calls.
- Cancelling a ring-group or conference dialing task no longer destroys the SIP
  transaction owner. Losing legs finish their standard teardown in the
  background without delaying the winning call.

## 🧭 Home Assistant Automations Can Override The Dial Plan

Automations can now cover some genuinely useful home-phone scenarios. Someone
rings the doorbell, or an external call arrives: Home Assistant rings first;
if nobody answers, the same call can be sent to Assist. With a sensible prompt,
your voice assistant can become a surprisingly capable domestic secretary.
Not mine, obviously: mine swears and insults saints.

Here is the complete no-answer automation. It waits while the HA softphone
rings, then forwards the still-open call to the Assist extension `1666`:

```yaml
alias: VoIP - HA unanswered to Assist
mode: parallel
max: 10
triggers:
  - trigger: state
    entity_id: sensor.voip_stack_call_state
    to: ringing
    for: "00:00:30"
actions:
  - action: voip_stack.forward
    data:
      destination: "1666"
      on_failure: resume
```

Change `1666` to any destination understood by the phonebook. No Call-ID,
helper timer or Jinja plumbing is required for the normal single-call case.

- The phonebook remains the complete default dial plan. With no matching
  automation, calls behave exactly as before.
- Trunk inbound routing now has explicit Direct and DTMF modes. Direct follows
  the configured default target without pre-answer; DTMF gives explicit
  phonebook extensions priority and uses the default target only when no digits
  arrive.
- Experimental automation routing is a separate, disabled-by-default option.
  It may override the Direct decision or the no-digits DTMF fallback, but never
  an explicit DTMF extension.
- A native `event.voip_stack_call` entity exposes incoming/outgoing calls,
  ringing, answer/connection, terminal results, explicit timeout requests and
  in-call DTMF in Home Assistant's entity and automation UI.
- `sensor.voip_stack_call_state` exposes the durable HA phone state. A native
  state trigger with `for:` can implement no-answer routing without templates,
  helper timers or a second automation.
- Every HA-owned call carries a stable Call-ID, one logical owner, monotonic
  control revision, state sequence and bounded route history. Ownership and
  destination changes advance the revision even when the visible state name
  does not change.
- `voip_stack.forward` can move the same pending or ringing call to an ESP,
  registered SIP phone, ring group or Assist. Re-forwarding while the remote
  phone rings sends standards-based CANCEL before starting the replacement leg.
- When exactly one call is forwardable, `voip_stack.forward` infers it and its
  concurrency guards. The normal 30-second HA-to-Assist fallback is now one
  state trigger and one action, with no user-facing Call-ID or Jinja plumbing.
- Explicit deadlines and Call-ID/state/sequence guards remain available for
  advanced multi-call and multi-stage policies.
- Failed routes support `resume`, `terminate` and `busy`. Resume returns a
  pre-answered trunk caller to the normal HA ringing path using the same RTP
  reservation instead of leaving silent media behind.
- Direct ESP-to-ESP calls remain media-direct and observable-only; automation
  routing is offered only when HA actually owns the call.

See the [Automation Dial Plan guide](AUTOMATION_DIALPLAN.md) for copyable
conditional-forward and unanswered-call-to-Assist examples.

## 🎥 Experimental SIP Video For The HA Softphone

Yes, apparently we are close to a real native Home Assistant video phone. Soon
you may be able to remain safely inside your sealed fortress of misanthropy and
despair while checking exactly how ugly the person ringing your doorbell is.

The results so far are encouraging. I do not own a SIP video door station yet
(I told you I am poor), so qualification currently uses standard SIP clients
sending webcam, generated video and real media streams. It is behaving well in
the lab; now I need real door-station tests from users, so please tell me what
works, what stutters and what catches fire.

<p align="center">
  <img
    src="https://raw.githubusercontent.com/n-IA-hane/esphome-intercom/dev/docs/images/ha-sip-video-call.gif"
    alt="Live SIP video call in the Home Assistant softphone"
    width="800"
  />
</p>

_A real SIP call feeding video into the HA softphone card. The video stays the
main character; identity, duration and hang-up move into the bottom bar._

- The disabled-by-default HA softphone video profile now negotiates H.264, VP8
  and JPEG directly with standard SIP phones and door stations. ESPHome
  endpoints remain audio-only.
- H.264 Baseline and Constrained Baseline, VP8 and RFC 2435 JPEG use bounded
  RTP reorder and depacketization before the authenticated card WebSocket.
  H.264 and VP8 can also carry the browser camera when both the global option
  and that browser's **Send Camera** choice are enabled.
- A second independent opt-in can use Home Assistant's existing FFmpeg binary
  to receive H.263, H.263-1998 or H.265 and stream VP8 to the browser. It is
  receive-only, limited to one process and one thread, and never saves an
  intermediate file. Direct H.264, VP8 and JPEG do not start FFmpeg.
- AVP remains the compatible default. When a peer offers AVPF, compound RTCP
  receiver reports and negotiated PLI/FIR requests help a newly attached or
  reloaded card recover at a key frame. HA-owned standard SIP bridges can
  relay matching-profile, exact-codec RTP and RTCP without decoding or
  re-encoding the stream.
- Audio, receive video and camera transmit have independent failure domains.
  Camera denial, an unavailable codec or a failed optional transcode leaves the
  compatible audio call and other usable media directions alive.
- Received video fills the card behind the call identity. The call state,
  duration and hang-up action become a responsive full-width bottom bar;
  codec diagnostics remain hidden unless debug mode is enabled.
- Video ownership survives dashboard reloads during ringing or an active call.
  H.264 parameter sets are retained for the replacement decoder, catch-up is
  bounded, and the old browser WebSocket is released before the new one owns
  the media.
- Live qualification covered direct H.264, VP8 and JPEG, FFmpeg receive for
  H.263, H.263-1998 and H.265, H.264 and VP8 bidirectional camera media,
  audio-only fallback, camera denial, local and remote hangup, caller CANCEL,
  repeated calls and compact through tall Home Assistant card sizes.
- Post-call diagnostics assert that sessions, dialogs, RTP sockets, browser
  owners, cleanup tasks and the optional transcode slot all return to zero.

This remains an experimental HA-softphone feature, not a general video PBX.
There is no ESP, Assist, ring-group or conference video, cross-codec endpoint
transcoding, SRTP, ICE/STUN/TURN, recording or established-dialog renegotiation.
Read the complete [Experimental SIP Video profile](EXPERIMENTAL_SIP_VIDEO.md)
before enabling it.

## 🧪 Qualification So Far

- Full backend and frontend test suite: 395 tests plus 39 subtests passing,
  including a real FFmpeg codec matrix.
- Python compilation, JavaScript syntax and repository diff checks clean.
- Real outbound Wildix call: `407`, authenticated INVITE, `100 Trying`, `183
  Session Progress`, local hangup, CANCEL, `487 Request Terminated`, ACK.
- Call state remained idle after cancellation and the remote leg stopped
  ringing; every state for the same call ID remained `outgoing` through
  teardown.
- Real Wildix `426` to HA trunk `427` calls covered: unchanged default HA
  ringing, immediate automation forward to Assist, four spaced SIP INFO digits
  selecting Assist, caller BYE during route selection, failed-route resume,
  explicit and stale deadlines, ring-group forwarding and a second forward
  while a registered bareSIP phone was ringing.
- The multi-hop test observed a real SIP CANCEL at the replaced bareSIP phone;
  the surviving call kept its source Call-ID and recorded both route-history
  entries. In-call SIP INFO toward Assist emitted one canonical `dtmf`
  occurrence while the initial extension digits remained isolated.
- The HA softphone card now consumes one complete authoritative state stream,
  without reinterpreting SIP scope or routing in the frontend. A live matrix
  covered ringing without refresh, refresh during ringing, answer, decline,
  auto-answer, failed-route resume, two simultaneous dashboards, a registered
  SIP caller, no-ID forward inference and the real 30-second trunk-to-Assist
  automation. The HA card releases to `idle/forwarded` while the same source
  call continues to Assist.
- A separate real-trunk inbound matrix covered nine routing contracts: Direct
  default with no decision delay, Direct timeout fallback, Direct native event
  override, DTMF no-digits default, DTMF no-digits override, explicit Assist
  extension, invalid extension rejection, native state `for:` forwarding and
  caller cancellation during digit collection. Every case captured the full
  WebSocket transition sequence and restored the original HA configuration.

## Known Follow-Up Areas

The release audit also identified non-blocking registrar improvements that are
being kept separate from the transaction fix: digest nonce-count replay
protection, NAT-aware registered Contact routing and optional multiple Contact
bindings per account. They are not claimed as completed until implemented and
qualified.
