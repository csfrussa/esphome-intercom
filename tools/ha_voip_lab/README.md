# Local VoIP Stack lab

This directory contains the reproducible Home Assistant Core laboratory used by
the VoIP Stack integration tests.  It deliberately runs in the existing Codex
host, on loopback-only HTTP and isolated SIP/RTP ports.  It is not an addon,
runtime dependency, or supported deployment topology for the integration.

Bootstrap the external working directory once:

```bash
tools/ha_voip_lab/bootstrap.sh
sudo systemctl start home-assistant-voip-lab.service
```

The default root is `$HOME/ha-voip-lab`. Set `HA_VOIP_LAB_ROOT`,
`HA_VOIP_LAB_USER` or `HA_VOIP_LAB_GROUP` before bootstrap when the service
must run elsewhere or under another local account. The bootstrap renders the
systemd unit from the checked-in template; no developer home path is embedded
in the repository.

The service listens on `127.0.0.1:18123`.  SIP uses port `15060`; the initial
RTP port is `44000`.  The custom component is linked directly from the current
working tree so a restart tests exactly the local code.

The laboratory must never be configured with production trunk credentials or
household ESP endpoints. Synthetic SIP/FFmpeg peers and isolated bareSIP
accounts are used by the qualification scripts instead.

Refresh the short-lived browser access token before a Playwright qualification
without repeating onboarding:

```bash
python tools/ha_voip_lab/refresh_playwright_auth.py
```

The helper reads the lab-only refresh token and updates only the local
Playwright storage-state file. It never prints either token.

Run `experimental_sip_video_browser_probe.py` against the lab dashboard and
`experimental_sip_video_peer.py` against SIP port `15060` for repeatable media
tests. The probe checks the rendered canvas, responsive card geometry and
post-call backend ownership. The peer can generate audio-only, H.263,
H.263-1998, H.264, H.265, JPEG and VP8 offers without a physical door station.
