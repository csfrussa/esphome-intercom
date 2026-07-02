"""Configuration normalization for VoIP Stack."""

from __future__ import annotations

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_REGISTRAR_ENABLED,
    CONF_TRUNK_AUTH_USERNAME,
    CONF_TRUNK_DOMAIN,
    CONF_TRUNK_DTMF_ENABLED,
    CONF_TRUNK_DTMF_ROUTES,
    CONF_TRUNK_DTMF_TERMINATOR,
    CONF_TRUNK_DTMF_TIMEOUT_MS,
    CONF_TRUNK_ENABLED,
    CONF_TRUNK_EXPIRES,
    CONF_TRUNK_INBOUND_DEFAULT_TARGET,
    CONF_TRUNK_OUTBOUND_PROXY,
    CONF_TRUNK_PASSWORD,
    CONF_TRUNK_PORT,
    CONF_TRUNK_SERVER,
    CONF_TRUNK_TRANSPORT,
    CONF_TRUNK_USERNAME,
    DOMAIN,
    VOIP_STACK_RTP_PORT,
    VOIP_STACK_SIP_PORT,
)


def entry_transport_config(entry: ConfigEntry | None = None) -> dict:
    data = entry.data if entry is not None else {}
    return {
        CONF_REGISTRAR_ENABLED: bool(data.get(CONF_REGISTRAR_ENABLED, False)),
        "sip_port": int(data.get("sip_port", VOIP_STACK_SIP_PORT)),
        "rtp_port": int(data.get("rtp_port", VOIP_STACK_RTP_PORT)),
        "advertise_host": (data.get("advertise_host") or "").strip(),
    }


def entry_trunk_config(entry: ConfigEntry | None = None) -> dict:
    data = entry.data if entry is not None else {}
    return {
        CONF_TRUNK_ENABLED: bool(data.get(CONF_TRUNK_ENABLED, False)),
        CONF_TRUNK_TRANSPORT: str(data.get(CONF_TRUNK_TRANSPORT) or "udp").strip().lower(),
        CONF_TRUNK_SERVER: str(data.get(CONF_TRUNK_SERVER) or "").strip(),
        CONF_TRUNK_PORT: int(data.get(CONF_TRUNK_PORT) or VOIP_STACK_SIP_PORT),
        CONF_TRUNK_DOMAIN: str(data.get(CONF_TRUNK_DOMAIN) or "").strip(),
        CONF_TRUNK_USERNAME: str(data.get(CONF_TRUNK_USERNAME) or "").strip(),
        CONF_TRUNK_AUTH_USERNAME: str(data.get(CONF_TRUNK_AUTH_USERNAME) or "").strip(),
        CONF_TRUNK_PASSWORD: str(data.get(CONF_TRUNK_PASSWORD) or ""),
        CONF_TRUNK_EXPIRES: int(data.get(CONF_TRUNK_EXPIRES) or 300),
        CONF_TRUNK_OUTBOUND_PROXY: str(data.get(CONF_TRUNK_OUTBOUND_PROXY) or "").strip(),
        CONF_TRUNK_INBOUND_DEFAULT_TARGET: str(data.get(CONF_TRUNK_INBOUND_DEFAULT_TARGET) or "HA").strip() or "HA",
        CONF_TRUNK_DTMF_ENABLED: bool(data.get(CONF_TRUNK_DTMF_ENABLED, False)),
        CONF_TRUNK_DTMF_TIMEOUT_MS: max(100, min(2000, int(data.get(CONF_TRUNK_DTMF_TIMEOUT_MS) or 1000))),
        CONF_TRUNK_DTMF_TERMINATOR: str(data.get(CONF_TRUNK_DTMF_TERMINATOR) or "").strip(),
        CONF_TRUNK_DTMF_ROUTES: str(data.get(CONF_TRUNK_DTMF_ROUTES) or "").strip(),
    }


def transport_config(hass: HomeAssistant) -> dict:
    return hass.data.get(DOMAIN, {}).get(
        "transport_config",
        {
            "sip_port": VOIP_STACK_SIP_PORT,
            "rtp_port": VOIP_STACK_RTP_PORT,
            "advertise_host": "",
        },
    )


def trunk_config(hass: HomeAssistant) -> dict:
    return hass.data.get(DOMAIN, {}).get("trunk_config", entry_trunk_config(None))


def debug_mode(hass: HomeAssistant) -> bool:
    from .const import CONF_DEBUG_MODE

    return bool(hass.data.get(DOMAIN, {}).get(CONF_DEBUG_MODE, False))


def trunk_enabled(cfg: dict) -> bool:
    return bool(
        cfg.get(CONF_TRUNK_ENABLED)
        and cfg.get(CONF_TRUNK_SERVER)
        and cfg.get(CONF_TRUNK_USERNAME)
        and cfg.get(CONF_TRUNK_PASSWORD)
    )
