# ESPHome Intercom SIP

This project turns ESPHome devices into standards-oriented SIP intercom phones.

The current functional contract is SIP/SDP/RTP only:

- ESP devices are SIP phones.
- Home Assistant is a SIP softphone, dial-plan authority, SIP/RTP bridge, and optional SIP trunk endpoint.
- Call control is expressed as `INVITE`, `ACK`, `CANCEL`, `BYE`, `OPTIONS`, `REGISTER` where applicable, and SIP final/provisional status codes.
- Media is negotiated with SDP and carried as RTP PCM.
- The former proprietary intercom control protocol is not a public contract or compatibility layer.

## ESP Configuration

`intercom_api.protocol` selects SIP signaling transport. SIP itself is implicit;
there is no alternate intercom protocol behind this option:

```yaml
intercom_api:
  id: intercom
  protocol: udp   # SIP/UDP signaling; RTP remains UDP
```

or:

```yaml
intercom_api:
  id: intercom
  protocol: tcp   # SIP/TCP signaling; RTP remains UDP
```

Local dial-plan entries live in `phonebook`:

```yaml
intercom_api:
  id: intercom
  protocol: udp
  phonebook:
    - name: Kitchen
      ip: 192.168.1.42
      port: 5060
      rtp_port: 40000
    - name: Front Gate
```

`name` is required. `ip`, `port`, `rtp_port`, and `sip_transport` are optional.
A name-only contact is a logical SIP target that HA can resolve or bridge later.

Runtime automations can mutate the same phonebook:

```yaml
on_press:
  - intercom_api.add_contacts:
      name: Kitchen
      ip: 192.168.1.42
      port: 5060
      rtp_port: 40000
```

## Home Assistant

The `intercom_native` integration exposes SIP-first services:

- `intercom_native.sip_call`
- `intercom_native.sip_answer`
- `intercom_native.sip_decline`
- `intercom_native.sip_hangup`
- `intercom_native.sip_forward`
- `intercom_native.sip_set_dnd`
- `intercom_native.phonebook_add_contact`
- `intercom_native.phonebook_remove_contact`
- `intercom_native.phonebook_set_contacts`
- `intercom_native.phonebook_clear`
- `intercom_native.phonebook_push`

HA publishes the central `sensor.intercom_phonebook` roster and can bridge
logical targets. Use `ha_bridge: true` when a call should route through HA even
if a direct endpoint exists.

The HA setup flow also supports an optional SIP trunk. When disabled, trunk
registration, external routing, and DTMF collection are not started. When
enabled, unresolved outbound numbers can route through the trunk, and inbound
provider calls can select a local target by DTMF digits such as `100`.

## Routing Model

- `sip:name@host[:port]` or `name@host[:port]` dials a direct SIP endpoint.
- `name` dials a phonebook target.
- Name-only targets can be resolved or bridged by HA.
- Explicit `ha_bridge` uses HA as a SIP bridge, not a proprietary PBX.
- If an optional SIP trunk is configured and registered, unresolved external
  numbers can be sent to the provider trunk.
- Inbound trunk calls answer at HA, collect RFC2833/telephone-event DTMF for a
  short window, then route to HA or a local SIP phonebook target.

## Public State

Public call state is `SipPhoneState`:

- `idle`
- `calling`
- `remote_ringing`
- `ringing`
- `connecting`
- `in_call`
- `terminating`
- terminal states such as `busy`, `declined`, `cancelled`,
  `media_incompatible`, `transport_unreachable`, and
  `auth_required_unsupported`

Snapshots include Call-ID/dialog identity, selected SDP/RTP formats, SIP status,
terminal reason, RTP counters, and last SIP event.

## Documentation

Current operational docs:

- `docs/ARCHITECTURE.md`
- `docs/reference.md`
- `docs/PHONEBOOK_PROTOCOL.md`
- `docs/DEPLOYMENT_GUIDE.md`
- `docs/SIP_TRUNK.md`
- `docs/troubleshooting.md`
- `docs/MIGRATION_AUDIT.md`

Historical release notes may mention earlier proprietary intercom terminology.
Those notes are not the current public contract.
