# Deployment Guide

## Network

SIP signaling listens on `sip_port` and may use UDP or TCP. RTP media always
uses UDP on `rtp_port`.

Use routable IP addresses in `phonebook` or let HA publish the central
`sensor.intercom_phonebook`. For HA Container/Docker, host networking remains
the simplest deployment because SIP/RTP use inbound UDP/TCP sockets.

## ESP Devices

Choose signaling transport per device:

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

## Media

ESP accepts compatible PCM SDP only. Unsupported codecs or oversized/unsupported
formats must receive a SIP failure such as `488 Not Acceptable Here`.

HA can bridge and resample between supported PCM formats. If a conversion cannot
be built, HA terminates the setup with `media_incompatible`.
