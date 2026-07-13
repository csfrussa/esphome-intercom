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
- A native `event.voip_stack_call` entity exposes incoming/outgoing calls,
  ringing, answer/connection, terminal results, explicit timeout requests and
  in-call DTMF in Home Assistant's entity and automation UI.
- Every HA-owned call carries a stable Call-ID, monotonic state sequence and
  bounded route history. Optional state/sequence guards reject stale automation
  runs instead of redirecting a call that has already changed.
- `voip_stack.forward` can move the same pending or ringing call to an ESP,
  registered SIP phone, ring group or Assist. Re-forwarding while the remote
  phone rings sends standards-based CANCEL before starting the replacement leg.
- `voip_stack.set_deadline` emits a calling/ringing timeout occurrence without
  hiding any route action. A second automation may forward an unanswered HA
  call to Assist, while an answered or otherwise changed call invalidates its
  old deadline automatically.
- A single automation can instead use Home Assistant's native
  `wait_for_trigger` with the stable Call-ID, then call `voip_stack.forward`
  only on timeout. The guide includes the exact 30-second fallback qualified
  against a real trunk call and Assist pipeline.
- Failed routes support `resume`, `terminate` and `busy`. Resume returns a
  pre-answered trunk caller to the normal HA ringing path using the same RTP
  reservation instead of leaving silent media behind.
- Direct ESP-to-ESP calls remain media-direct and observable-only; automation
  routing is offered only when HA actually owns the call.

See the [Automation Dial Plan guide](AUTOMATION_DIALPLAN.md) for copyable
conditional-forward and unanswered-call-to-Assist examples.

## 🧪 Qualification So Far

- Full backend and frontend test suite: 324 tests and 34 subtests passing.
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
  SIP caller and the real 30-second trunk-to-Assist automation.

## Known Follow-Up Areas

The release audit also identified non-blocking registrar improvements that are
being kept separate from the transaction fix: digest nonce-count replay
protection, NAT-aware registered Contact routing and optional multiple Contact
bindings per account. They are not claimed as completed until implemented and
qualified.
