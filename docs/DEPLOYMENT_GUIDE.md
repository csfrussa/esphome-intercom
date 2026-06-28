# Deployment Guide

## Network

SIP signaling listens on `sip_port` and may use UDP or TCP. RTP media always
uses UDP on `rtp_port`.

Use routable IP addresses in `phonebook` or let HA publish the central
`sensor.intercom_phonebook`. For HA Container/Docker, host networking remains
the simplest deployment because SIP/RTP use inbound UDP/TCP sockets.

## ESP Devices

Choose SIP signaling transport per device. SIP is implicit; `protocol` selects
only whether signaling uses UDP or TCP:

```yaml
intercom_api:
  protocol: udp
```

or:

```yaml
intercom_api:
  protocol: tcp
```

Use `phonebook` for manual peers. Use the HA phonebook subscription package when
HA should be the authority.

## Home Assistant

Configure `intercom_native` with reachable SIP/RTP ports. If HA is behind NAT,
VPN, LXC, or multiple subnets, set the integration advertise host so ESPs see a
reachable SIP Contact/SDP address.

Use `ha_bridge` for routed or logical calls that should pass through HA.

## Optional SIP Trunk

The trunk is disabled by default. Leave it disabled for local-only intercom
installs; no registration, external route or DTMF collector is started.

Enable it only when HA must register to a SIP provider or PBX. The trunk setup
asks for provider transport, server, credentials, optional outbound proxy,
default inbound target and optional DTMF route map.

Inbound provider calls are answered by HA so it can collect DTMF digits. Normal
mobile dialers can use post-dial pauses, for example a contact that dials the
provider number, waits, and sends `100`. HA maps the final digit buffer to a
local phonebook target or falls back to the configured default target.

## Media

ESP accepts compatible PCM SDP only. Unsupported codecs or oversized/unsupported
formats must receive a SIP failure such as `488 Not Acceptable Here`.

HA can bridge and resample between supported PCM formats. If a conversion cannot
be built, HA terminates the setup with `media_incompatible`.
