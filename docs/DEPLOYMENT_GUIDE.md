# Deployment Guide

This guide maps the maintained YAMLs and the Home Assistant integration to the
current VoIP model. The old TCP/UDP intercom split is gone: every device is a
SIP phone, and `transport: udp` or `transport: tcp` only selects SIP signaling
transport.

## YAML Tree

```text
yamls/
├── voip-only/       SIP phone + audio stack, no wake word or VA
├── full-experience/     SIP phone + audio stack + MWW/Voice Assistant/media
├── experimental/        bring-up/reference profiles
└── host/                local-only ESPHome host test YAMLs, gitignored
```

Choose the maintained YAML closest to the hardware and edit identity, pins and
network settings. Do not start from historical `*-tcp`, `*-udp` or
`*-sip` filenames; transport is now inside the `voip_stack` declaration.

## Network

SIP signaling listens on `sip_port` and may use UDP or TCP. RTP media always
uses UDP on `rtp_port`.

Use routable IP addresses in `phonebook` or let HA publish the central
`sensor.voip_phonebook`. For HA Container/Docker/LXC, host networking or an
explicit advertised host remains the simplest deployment because SIP/RTP use
inbound UDP/TCP sockets.

## ESP Devices

Choose SIP signaling transport per device. SIP is implicit; `transport` selects
only whether signaling uses UDP or TCP:

```yaml
voip_stack:
  transport: udp
```

or:

```yaml
voip_stack:
  transport: tcp
```

Use `static_contacts` for a small fixed ESP-local dial plan. Use the HA
phonebook subscription package when HA should be the authority.

Supported audio shapes:

- full duplex: microphone plus speaker;
- mic only: sends audio but ignores remote playback;
- speaker only: plays remote audio but sends no mic RTP;
- control only: call signaling/phonebook without audio.

These are first-class SIP endpoint shapes. They are not compatibility modes.

## Home Assistant

Configure `voip_stack` with reachable SIP/RTP ports. HA is always
the local softphone and router/B2BUA. There is no separate "HA PBX" mode.

If HA is behind NAT, VPN, LXC, Docker, or multiple subnets, set the integration
advertise host so ESPs and softphones see a reachable SIP Contact/SDP address.

Use `ha_bridge` for routed or logical calls that should pass through HA.

Enable the local registrar if standard SIP endpoints should register to HA. Create
accounts with `voip_stack.create_account`; registered clients appear
in the central phonebook as softphone contacts.

## Optional SIP Trunk

The trunk is disabled by default. Leave it disabled for local-only VoIP
installs; no registration, external route or DTMF collector is started.

Enable it only when HA must register to a SIP provider or PBX. The trunk setup
asks for provider transport, server, credentials, optional outbound proxy,
default inbound target and optional DTMF digit collection.

Inbound provider calls are answered by HA so it can collect DTMF digits when
the provider exposes a standard digit channel. Normal mobile dialers can use
post-dial pauses, for example a contact that dials the provider number, waits,
and sends `100`. If no digits arrive, HA rings the configured default target
or HA softphone. If digits arrive, HA resolves them through central phonebook
`extension` values.
If digits arrive and do not resolve, HA terminates the answered leg with
`route_not_found`.

## Media

ESP accepts compatible PCM SDP only. Unsupported codecs or oversized/unsupported
formats must receive a SIP failure such as `488 Not Acceptable Here`.

HA can bridge and resample between supported formats. Trunk/softphone legs may
negotiate OPUS, PCMA or PCMU; ESP legs remain PCM-only. HA keeps the best
negotiated quality per leg when conversion is available. If a conversion cannot
be built, HA terminates the setup with `media_incompatible`.
