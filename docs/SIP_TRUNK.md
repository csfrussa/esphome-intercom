# Optional SIP Trunk

Home Assistant can optionally register one SIP trunk account with a provider or
PBX. This does not change the local contract: ESP devices remain direct SIP
phones and do not register to the provider.

When the trunk is disabled, no trunk registration, external outbound routing or
inbound DTMF collector is started.

## Setup Flow

The first Intercom Native setup step configures HA's local SIP endpoint:

- SIP/TCP listener
- SIP/UDP listener
- SIP port
- RTP base port
- advertised host/IP
- Assist intents
- optional trunk enable switch

Only when the trunk switch is enabled does the second setup step ask for trunk
details:

- transport: `udp` or `tcp`
- server, port and optional domain
- username, optional auth username and password
- REGISTER expiration
- optional outbound proxy
- inbound default target
- optional DTMF routing

## Outbound Routing

Local targets still resolve through the phonebook first.

- `sip:name@host:port` routes direct.
- A known phonebook name routes direct or via HA according to the roster.
- A logical name can be bridged by HA.
- A number or unresolved target can route through the registered trunk.

If the trunk is configured but not registered, outbound unresolved targets fail
as routing errors. There is no legacy intercom compatibility route.

## Inbound Routing

Provider inbound calls arrive at HA's SIP endpoint. HA answers the trunk leg
with SDP so it can receive RFC2833/telephone-event DTMF digits from normal
mobile dialers and SIP softphones.

Example DTMF route map:

```text
100=Cucina
101=Camera
9=HA
```

Example user flow:

1. A caller dials the public provider number.
2. HA answers the trunk leg.
3. The caller sends post-dial digits such as `100`.
4. HA routes the call to the matching local phonebook target.
5. If no route matches before timeout, HA uses `trunk_inbound_default_target`.

No final `#` is required. `trunk_dtmf_terminator` can be set if a deployment
wants one, but the normal path is a short timeout. Ambiguous prefixes are not
rejected during setup: HA collects within the timeout, tries the final digit
buffer, logs unresolved buffers and then uses the default target.

## Media

The provider leg and local leg are separate SIP dialogs. HA bridges RTP between
them with the same relay/resampler used for local HA bridge calls. PCM remains
the project media contract; ESP devices still reject unsupported media with
standard SIP errors.

## Observability

The HA softphone snapshot exposes:

- `sip_trunk.trunk_enabled`
- `sip_trunk.trunk_registered`
- `sip_trunk.trunk_status_code`
- `sip_trunk.trunk_status_reason`
- `sip_trunk.trunk_last_sip_event`
- `sip_trunk.trunk_transport`
- `sip_trunk.trunk_server`

INFO logs describe normal call progress. DEBUG logs should be used when tracing
REGISTER, INVITE, DTMF routing and RTP relay behavior.
