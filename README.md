# ESPHome Intercom SIP

This project turns ESPHome devices into standards-oriented SIP intercom phones.

The current functional contract is SIP/SDP/RTP only:

- ESP devices are SIP phones.
- Home Assistant is a SIP softphone, dial-plan authority, and optional SIP/RTP bridge.
- Call control is expressed as `INVITE`, `ACK`, `CANCEL`, `BYE`, `OPTIONS`, and SIP final/provisional status codes.
- Media is negotiated with SDP and carried as RTP PCM.
- The former proprietary intercom control protocol is not a public contract or fallback.

## ESP Configuration

`intercom_api.protocol` selects SIP signaling transport:

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

`name` is required. `ip`, `port`, `rtp_port`, and `protocol` are optional. A
name-only contact is a logical SIP target that HA can resolve or bridge later.

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

## Routing Model

- `sip:name@host[:port]` or `name@host[:port]` dials a direct SIP endpoint.
- `name` dials a phonebook target.
- Name-only targets can be resolved or bridged by HA.
- Explicit `ha_bridge` uses HA as a SIP bridge, not a proprietary PBX.

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
- `docs/troubleshooting.md`

Historical release notes may mention earlier proprietary intercom terminology.
Those notes are not the current public contract.
