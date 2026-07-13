# 2026.7.2: Work In Progress

<!-- Canonical incremental source for the future v2026.7.2 release body. -->

`2026.7.2` collects changes made after stable `2026.7.1`. This document is
updated as work lands on `dev`; features listed here are not released until the
version is published from `main`.

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

## 🧪 Qualification So Far

- Full backend and frontend test suite: 340 tests and 35 subtests passing.
- Python/Ruff checks clean.
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
