# Call Flows

This document explains what happens on the wire and in Home Assistant for the
main call paths. It is intentionally operational: when a call behaves oddly,
start here and compare the observed log path against the expected path.

## Common Pieces

Every call has these layers:

- SIP signaling: INVITE, 100 Trying, 180 Ringing, 200 OK, ACK, BYE, CANCEL.
- SDP negotiation: each leg negotiates its own RTP format.
- RTP media: direct endpoint media or HA-anchored relay/mixer media.
- Call registry: HA tracks source call ID, destination call ID, relay, client,
  pending invite and softphone state.
- Phonebook resolver: maps the dialed target to a route action.

## ESP To ESP Direct

1. ESP A selects `ESP B` from its synced phonebook.
2. The entry has a direct address/SIP URI and does not require `ha_bridge`.
3. ESP A sends SIP INVITE directly to ESP B.
4. ESP B rings or auto-answers depending on its own config.
5. RTP flows directly between ESP A and ESP B.
6. HA observes state through ESP sensors but does not route media.

Expected HA behavior: no `route_requested` event.

## ESP To HA Softphone

1. ESP dials the HA softphone name or extension, for example `Casa` or `666`.
2. ESP sends the call to HA.
3. HA resolves the target as `answer_ha`.
4. HA sends `100 Trying` then `180 Ringing`.
5. The HA softphone state becomes `ringing`.
6. The browser/card that owns the HA softphone may answer.
7. On answer, HA sends `200 OK` with SDP and the browser audio WebSocket binds
   to the call.
8. On decline/hangup, HA sends the appropriate SIP final response or BYE.

If HA softphone DND is enabled, HA returns `486 Busy Here` with terminal reason
`dnd`.

If HA softphone is already ringing, in call or has an active browser media
session, HA returns `486 Busy Here`.

## HA/Card To ESP

1. The card sends `voip_stack.call` with the selected target.
2. HA resolves the target from `sensor.voip_phonebook`.
3. If the target is an ESP, HA creates a SIP client leg to the ESP.
4. The HA softphone state moves to `calling`.
5. If the ESP rings, HA state becomes `remote_ringing`.
6. If the ESP answers, HA state becomes `in_call`.
7. If the call terminates, HA stops the client, relay/media reservation and
   updates the softphone state to a terminal state.

The card does not decide routing and does not filter the phonebook. It displays
the central roster and mirrors HA softphone state.

## ESP Mirror Card To Target

1. The ESP mirror card is bound to one ESPHome device.
2. Contact next/previous buttons press that ESP's own contact-cycler controls.
3. The normal Call button presses the ESP call control for the currently
   selected contact.
4. The keypad/manual target view sends the typed target to the same ESP
   `start_call` action.
5. The ESP first resolves the target through its local synced phonebook. Direct
   SIP targets can be called by the ESP itself.
6. If the target is not locally direct, the ESP sends the call to HA and HA
   applies the central dial plan: extension, group, registered SIP endpoint,
   trunk number or reject.

The keypad is not a second HA-only routing path and it must not overwrite the
ESP selected-contact sensor. Closing the keypad returns to the ESP contact
cycler.

## Registered SIP Endpoint To Registered SIP Endpoint

1. Endpoint A registers to HA with a local SIP account.
2. Endpoint B registers to HA with a local SIP account.
3. Endpoint A sends INVITE to HA, target `EndpointB` or B's `extension`.
4. HA recognizes A as a registered endpoint and uses the central dial plan
   immediately.
5. HA resolves B to B's current Contact URI.
6. HA sends INVITE to B and bridges the SIP/RTP legs.
7. B's final response is propagated back to A.

Expected log shape:

- `SIP RX INVITE sip:<target>@<ha>`
- `SIP TX INVITE <target>@<registered-contact-host>:<port>`
- `SIP bridge registered ... target=<target>`

Unexpected log shape:

- `SIP route requested ...` for ordinary registered endpoint calls. That means
  the call left the canonical dial plan and went into the automation fallback.

## Unknown Or Unregistered Caller

1. Any SIP peer with network reachability sends INVITE to the ESP or HA
   listener; no phonebook or registration lookup is used as a source allowlist.
2. The receiver validates the SIP transaction, Request-URI, current busy/DND
   state and SDP media.
3. A directly addressed idle ESP rings when media is compatible. HA resolves
   the destination using the same dial plan used for registered callers.
4. The call proceeds or receives the normal routing/media/status response.

The HA registrar authenticates REGISTER and publishes the endpoint's current
Contact. It does not make registered accounts the only callers HA or an ESP can
receive. Use network policy when an installation needs that restriction.

## Hold Or Media-Changing Re-INVITE

1. A call is already established with its original negotiated media.
2. One peer sends an in-dialog INVITE whose SDP signals hold or changes media.
3. The ESP or HA returns `488 Not Acceptable Here`.
4. The established dialog and RTP selection remain active; no second call
   state is created.
5. Either peer can later send BYE and both sides clean up the original call.

HA may answer an in-dialog session refresh with unchanged SDP using the
existing answer. It does not rerun the dial plan or create a second call.

## Registered SIP Endpoint To HA

1. Registered endpoint calls `Casa` or HA's extension.
2. HA resolves the target as `answer_ha`.
3. HA sends `180 Ringing` immediately and updates HA softphone state.
4. If the caller sends CANCEL before answer, HA returns `487 Request
   Terminated` and clears pending state.

This path is a useful regression test for TCP SIP clients. The SIP listener
must send the `180` response before any slow UI/event work can delay the
transaction.

## HA Forward To Registered SIP Endpoint

1. HA service `voip_stack.forward` is called without a pending `call_id`, for
   example destination `SvcPhone` or extension `761`.
2. HA resolves the registered endpoint to its current Contact URI.
3. HA originates a SIP call to that Contact.
4. HA must not rewrite the destination to `sip:<target>@<ha-ip>`.

If it does rewrite to HA itself, the call loops into the inbound router and
appears as `route_requested`. That is a bug.

## Ring Group

1. Caller dials a group such as `RG Casa`.
2. HA resolves a roster entry with `metadata.group_type = ring`.
3. HA sends `180 Ringing` to the caller.
4. HA forks INVITE to all callable members except the caller.
5. Busy/declined members drop out.
6. First member to answer wins.
7. HA cancels the remaining forks.
8. HA bridges caller to the winner and reports `answered_by`.
9. Hangup from either side propagates to the other side.

Caller cancellation before answer must cancel every fork and release every RTP
reservation.

## Conference Group

1. Caller dials a group such as `CG Casa`.
2. HA resolves a roster entry with `metadata.group_type = conference`.
3. Caller joins the HA conference room immediately.
4. If configured, HA invites members listed in `ring_members`.
5. Invited members that answer join the room. There is no winner.
6. Members without conference ringing can still join manually by calling the
   group.
7. HA softphone can be a participant like any other endpoint.
8. The room ends when the last participant leaves.

Conference media is HA-mixed. Each participant receives the N-1 mix: everyone
except itself.

## Trunk Outbound

1. Caller dials an external number or a contact with `number`.
2. HA checks that no internal `extension` owns that target.
3. HA requires a registered trunk.
4. HA creates a trunk SIP client leg and a relay.
5. RTP is relayed between local caller and trunk endpoint.

If trunk is not registered, HA rejects with `trunk_unavailable`.

## Route Requested Fallback

`route_requested` is the automation escape hatch, not the normal path for
known roster targets.

It is expected when:

- inbound trunk handling needs an automation decision;
- an unknown external SIP caller reaches HA and the route is not immediately
  known;
- tests intentionally exercise `voip_stack.route`.

It is not expected for:

- registered endpoint calling another roster target;
- HA/card calling a roster target;
- ESP calling a known roster target;
- `forward` to a registered account.
