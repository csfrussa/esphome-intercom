"""RTP port allocation helpers."""

from __future__ import annotations

import socket

from homeassistant.core import HomeAssistant

from .config import transport_config
from .const import DOMAIN


def rtp_port_available(port: int) -> bool:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind(("0.0.0.0", int(port)))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def allocate_sip_rtp_port(hass: HomeAssistant, *, step: int = 2) -> int:
    cfg = transport_config(hass)
    bucket = hass.data.setdefault(DOMAIN, {})
    base_port = int(cfg["rtp_port"])
    if rtp_port_available(base_port):
        bucket["sip_rtp_next_port"] = base_port + int(step)
        return base_port
    candidate = int(bucket.get("sip_rtp_next_port", base_port + int(step)))
    for _ in range(64):
        if candidate == base_port:
            candidate += int(step)
            continue
        if rtp_port_available(candidate):
            bucket["sip_rtp_next_port"] = candidate + int(step)
            return candidate
        candidate += int(step)
    return base_port
