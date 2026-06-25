"""Shared session/transport plumbing helpers.

Deduplicates patterns that were copy-pasted across IntercomSession,
BridgeSession.start, BridgeSession.forward_to, and the cleanup paths:
  - TransportCallbacks dataclass + build_transport(...)
  - cancel_task() / stop_transport()

These helpers are deliberately neutral; FSM-level decisions (which event
to fire, which side hung up) stay in the session classes that own them.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Callable, Optional

from homeassistant.core import HomeAssistant

from .const import DOMAIN, INTERCOM_PORT
from .tcp_client import IntercomTcpClient
from .transport_base import IntercomTransport
from .udp_client import IntercomUdpClient

_LOGGER = logging.getLogger(__name__)


@dataclass
class TransportCallbacks:
    """Bundle of the seven callbacks every transport delivers upstream.

    Bundling them into one record means transport construction sites stop
    duplicating the seven-line `on_audio=..., on_disconnected=...` block.
    Any callback may be left None for legs that don't care (e.g. source
    leg has no on_ringing).
    """
    on_audio: Optional[Callable[[bytes], None]] = None
    on_disconnected: Optional[Callable[[], None]] = None
    on_ringing: Optional[Callable[[], None]] = None
    on_answered: Optional[Callable[[], None]] = None
    on_stop_received: Optional[Callable[[], None]] = None
    on_decline_received: Optional[Callable[[str], None]] = None
    on_error_received: Optional[Callable[[int, str], None]] = None


def configured_transport_type(hass: HomeAssistant, host: str | None = None) -> str:
    """Return the transport HA should use for a host.

    SIP/UDP need both the feature flag and an endpoint-declared device. Fall
    back to TCP so a TCP-only ESP is never addressed over another protocol.
    """
    config = hass.data.get(DOMAIN, {}).get(
        "transport_config",
        {"use_tcp": True, "use_udp": False, "use_sip": True},
    )
    if config.get("use_sip", True):
        if host is None:
            return "sip"
        from .device_resolver import get_resolver
        for device in get_resolver(hass)._devices or []:
            if device.get("host") == host and device.get("transport") == "sip":
                return "sip"
    if not config.get("use_udp", False):
        return "tcp"
    if host is None:
        return "udp"

    from .device_resolver import get_resolver
    for device in get_resolver(hass)._devices or []:
        if device.get("host") != host:
            continue
        transport = device.get("transport")
        if transport in ("udp", "tcp", "sip"):
            return transport
        break
    return "tcp"


def _endpoint_ports_for_host(hass: HomeAssistant, host: str) -> dict | None:
    from .device_resolver import get_resolver
    for device in get_resolver(hass)._devices or []:
        if device.get("host") == host:
            return device
    return None


def build_transport(
    hass: HomeAssistant,
    host: str,
    transport_type: str,
    callbacks: TransportCallbacks,
) -> IntercomTransport:
    """Instantiate the requested transport implementation.

    Replaces the previous _build_transport(**kwargs) shape with a single
    callbacks bundle so call sites are short and immune to drift between
    the parameter list and the IntercomTransport constructor.
    """
    if transport_type == "udp":
        endpoint = _endpoint_ports_for_host(hass, host)
        if endpoint and endpoint.get("transport") == "udp":
            manager = None
            try:
                from .udp_socket_manager import get_manager
                manager = get_manager(hass)
            except Exception as err:
                _LOGGER.debug("UDP endpoint port lookup failed for %s: %s", host, err)
            if manager is not None:
                manager.set_peer_ports(
                    host,
                    audio_port=endpoint.get("udp_audio_port"),
                    control_port=endpoint.get("udp_control_port"),
                )
        return IntercomUdpClient(
            hass=hass,
            host=host,
            on_audio=callbacks.on_audio,
            on_disconnected=callbacks.on_disconnected,
            on_ringing=callbacks.on_ringing,
            on_answered=callbacks.on_answered,
            on_stop_received=callbacks.on_stop_received,
            on_decline_received=callbacks.on_decline_received,
            on_error_received=callbacks.on_error_received,
        )

    if transport_type != "tcp":
        _LOGGER.warning("Unknown transport_type=%s, defaulting to tcp", transport_type)

    endpoint = _endpoint_ports_for_host(hass, host)
    port = (
        endpoint.get("tcp_port")
        if endpoint and endpoint.get("transport") == "tcp" and endpoint.get("tcp_port")
        else hass.data.get(DOMAIN, {}).get("tcp_port", INTERCOM_PORT)
    )

    return IntercomTcpClient(
        hass=hass,
        host=host,
        port=port,
        on_audio=callbacks.on_audio,
        on_disconnected=callbacks.on_disconnected,
        on_ringing=callbacks.on_ringing,
        on_answered=callbacks.on_answered,
        on_stop_received=callbacks.on_stop_received,
        on_decline_received=callbacks.on_decline_received,
        on_error_received=callbacks.on_error_received,
    )


async def cancel_task(task: Optional[asyncio.Task], timeout: float = 1.0) -> None:
    """Cancel `task` and wait up to `timeout` for it to actually finish.

    Both IntercomSession.stop() and BridgeSession.stop() / forward_to()
    used the same try/except CancelledError pattern; this is the single
    source of truth.
    """
    if task is None:
        return
    task.cancel()
    try:
        await asyncio.wait_for(task, timeout=timeout)
    except (asyncio.CancelledError, asyncio.TimeoutError):
        pass
    except Exception as err:
        _LOGGER.debug("cancel_task: ignored exception while joining task: %s", err)


async def stop_transport(
    transport: Optional[IntercomTransport],
    send_signaling: bool = True,
) -> None:
    """Send HANGUP (best-effort) and close the transport. Idempotent."""
    if transport is None:
        return
    if send_signaling:
        try:
            await transport.stop_stream()
        except Exception as err:
            _LOGGER.debug("stop_transport: stop_stream raised: %s", err)
    try:
        await transport.disconnect()
    except Exception as err:
        _LOGGER.debug("stop_transport: disconnect raised: %s", err)
