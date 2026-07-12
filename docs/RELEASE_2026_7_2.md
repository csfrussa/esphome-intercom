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
- If a successful `200 OK` crosses the cancellation, HA acknowledges the dialog
  and ends it with BYE, preventing ghost calls.
- Cancelling a ring-group or conference dialing task no longer destroys the SIP
  transaction owner. Losing legs finish their standard teardown in the
  background without delaying the winning call.

## 🧪 Qualification So Far

- Full backend and frontend test suite: 297 tests passing.
- Python/Ruff checks clean.
- Real outbound Wildix call: `407`, authenticated INVITE, `100 Trying`, local
  hangup, CANCEL, `487 Request Terminated`, ACK.
- Call state remained idle after cancellation and the remote leg stopped
  ringing.

## Known Follow-Up Areas

The release audit also identified non-blocking registrar improvements that are
being kept separate from the transaction fix: digest nonce-count replay
protection, NAT-aware registered Contact routing and optional multiple Contact
bindings per account. They are not claimed as completed until implemented and
qualified.
