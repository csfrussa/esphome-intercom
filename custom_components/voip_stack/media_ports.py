"""RTP port allocation helpers."""

from __future__ import annotations

import socket

from homeassistant.core import HomeAssistant

from .config import transport_config
from .const import DOMAIN

RTP_RELAY_POOL_WIDTH = 400


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


def _relay_pool(hass: HomeAssistant, step: int) -> tuple[dict, int, int, int]:
    cfg = transport_config(hass)
    bucket = hass.data.setdefault(DOMAIN, {})
    base = int(cfg["rtp_port"]) + 2
    if base % 2:
        base += 1
    width = min(RTP_RELAY_POOL_WIDTH, max(0, 65534 - base + 1))
    width -= width % 2
    if width < 4:
        raise RuntimeError("SIP RTP relay port pool is too small")
    pool = bucket.setdefault("sip_rtp_port_pool", {})
    pool.setdefault("next", base)
    pool.setdefault("used", set())
    return pool, base, base + width, int(step)


def _wrap_pool_port(port: int, base: int, end: int, step: int) -> int:
    width = end - base
    if width <= 0:
        return base
    offset = (int(port) - base) % width
    if offset % step:
        offset += step - (offset % step)
    return base + (offset % width)


def allocate_sip_rtp_port_pair(hass: HomeAssistant, *, step: int = 2) -> tuple[int, int]:
    """Allocate two RTP relay ports from the bounded HA SIP pool."""
    pool, base, end, step = _relay_pool(hass, step)
    used: set[int] = pool["used"]
    candidate = _wrap_pool_port(int(pool.get("next", base)), base, end, step)
    attempts = max(1, (end - base) // step)
    for _ in range(attempts):
        first = candidate
        second = _wrap_pool_port(candidate + step, base, end, step)
        if first not in used and second not in used and rtp_port_available(first) and rtp_port_available(second):
            used.add(first)
            used.add(second)
            pool["next"] = _wrap_pool_port(second + step, base, end, step)
            return first, second
        candidate = _wrap_pool_port(candidate + step, base, end, step)
    raise RuntimeError("SIP RTP relay port pool exhausted")


def release_sip_rtp_port_pair(hass: HomeAssistant, ports: tuple[int, int] | list[int]) -> None:
    bucket = hass.data.setdefault(DOMAIN, {})
    pool = bucket.get("sip_rtp_port_pool")
    if not isinstance(pool, dict):
        return
    used = pool.get("used")
    if not isinstance(used, set):
        return
    for port in ports:
        used.discard(int(port))
