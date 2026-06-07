"""Single shared UDP socket pair (audio + control), demuxed by source IP.

One bind per HA instance: a per-session pair would collide with EADDRINUSE
on a UDP<->UDP bridge, and ESP-initiated calls need the control socket
bound before any session exists. The manager is pure transport-level
demux + sendto; FSM lives in IntercomSession / IntercomUdpClient.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from typing import Optional

from homeassistant.core import HomeAssistant

from .const import (
    DOMAIN,
    HEADER_SIZE,
    INTERCOM_UDP_AUDIO_PORT,
    INTERCOM_UDP_CONTROL_PORT,
    MSG_START,
)
from . import protocol

_LOGGER = logging.getLogger(__name__)

# Type aliases for clarity.
AudioCallback = Callable[[bytes], None]
ControlCallback = Callable[[int, bytes], None]  # (msg_type, payload)
# Unsolicited START callback: PBX-lite body fields plus the UDP source endpoint.
UnsolicitedCallback = Callable[[str, str, str, str, str, str, int], Awaitable[None]]
# (caller_name, caller_route, dest_name, dest_route, call_id, host, port)


class _AudioProtocol(asyncio.DatagramProtocol):
    def __init__(self, manager: "IntercomUdpSocketManager") -> None:
        self._manager = manager

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self._manager._on_audio_datagram(data, addr)

    def error_received(self, exc: Exception) -> None:
        _LOGGER.debug("UDP audio socket error: %s", exc)


class _ControlProtocol(asyncio.DatagramProtocol):
    def __init__(self, manager: "IntercomUdpSocketManager") -> None:
        self._manager = manager

    def datagram_received(self, data: bytes, addr: tuple[str, int]) -> None:
        self._manager._on_control_datagram(data, addr)

    def error_received(self, exc: Exception) -> None:
        _LOGGER.debug("UDP control socket error: %s", exc)


class _Consumer:
    """Per-peer record. The instance is the token returned by
    register_consumer; matching it on unregister prevents a stale
    teardown from evicting the live consumer."""

    __slots__ = ("on_audio", "on_control")

    def __init__(self, on_audio: AudioCallback, on_control: ControlCallback) -> None:
        self.on_audio = on_audio
        self.on_control = on_control


class IntercomUdpSocketManager:
    """One pair of bound UDP sockets, shared by all UDP sessions."""

    def __init__(
        self,
        hass: HomeAssistant,
        audio_port: int = INTERCOM_UDP_AUDIO_PORT,
        control_port: int = INTERCOM_UDP_CONTROL_PORT,
    ) -> None:
        self.hass = hass
        self.audio_port = audio_port
        self.control_port = control_port
        self._audio_transport: Optional[asyncio.DatagramTransport] = None
        self._control_transport: Optional[asyncio.DatagramTransport] = None
        # remote_ip -> active consumer (token-protected).
        self._consumers: dict[str, _Consumer] = {}
        self._unsolicited_cb: Optional[UnsolicitedCallback] = None
        self._audio_recv = 0
        self._control_recv = 0
        # Endpoint-declared/live UDP peers; endpoint sensors are the source of
        # truth and packet source ports update the cache during calls.
        self._udp_peers: set[str] = set()
        self._udp_peer_ports_by_ip: dict[str, tuple[int, int]] = {}

    # === Lifecycle ===

    async def start(self) -> bool:
        """Bind both UDP sockets. Idempotent."""
        if self._audio_transport is not None and self._control_transport is not None:
            return True

        loop = asyncio.get_running_loop()
        try:
            self._audio_transport, _ = await loop.create_datagram_endpoint(
                lambda: _AudioProtocol(self),
                local_addr=("0.0.0.0", self.audio_port),
            )
            self._control_transport, _ = await loop.create_datagram_endpoint(
                lambda: _ControlProtocol(self),
                local_addr=("0.0.0.0", self.control_port),
            )
        except OSError as err:
            _LOGGER.error("UdpSocketManager bind error: %s", err)
            await self.stop()
            return False

        _LOGGER.info(
            "UdpSocketManager listening: audio=%d control=%d",
            self.audio_port,
            self.control_port,
        )

        return True

    async def stop(self) -> None:
        """Close both sockets and drop all consumers. Idempotent."""
        self._udp_peers.clear()
        self._udp_peer_ports_by_ip.clear()
        if self._audio_transport is not None:
            self._audio_transport.close()
            self._audio_transport = None
        if self._control_transport is not None:
            self._control_transport.close()
            self._control_transport = None
        self._consumers.clear()
        _LOGGER.info("UdpSocketManager stopped (audio_recv=%d control_recv=%d)",
                     self._audio_recv, self._control_recv)

    def peer_ports(self, host: str) -> tuple[int, int]:
        """Return peer UDP (audio, control) ports, falling back to HA defaults."""
        return self._udp_peer_ports_by_ip.get(host, (self.audio_port, self.control_port))

    def set_peer_ports(
        self,
        host: str,
        *,
        audio_port: int | None = None,
        control_port: int | None = None,
    ) -> None:
        """Apply endpoint-declared UDP ports from the HA phonebook source."""
        self._learn_peer_ports(host, audio_port=audio_port, control_port=control_port)

    def _learn_peer_ports(
        self,
        host: str,
        *,
        audio_port: int | None = None,
        control_port: int | None = None,
    ) -> None:
        """Learn live UDP ports from packet source ports.

        Endpoint rows are the normal source of truth, but the packet itself is
        the freshest source for live UDP source ports during boot or DHCP
        changes.
        """
        if not host:
            return
        cur_audio, cur_control = self.peer_ports(host)
        new_audio = audio_port or cur_audio
        new_control = control_port or cur_control
        if (cur_audio, cur_control) == (new_audio, new_control) and host in self._udp_peers:
            return
        self._udp_peers.add(host)
        self._udp_peer_ports_by_ip[host] = (new_audio, new_control)

    def register_consumer(
        self,
        host: str,
        on_audio: AudioCallback,
        on_control: ControlCallback,
    ) -> _Consumer:
        """Bind per-peer callbacks; the returned token gates unregister
        so a stale teardown can't evict a replacement consumer."""
        if host in self._consumers:
            _LOGGER.warning(
                "UdpSocketManager: replacing active consumer for %s "
                "(previous session leaked its registration)", host,
            )
        token = _Consumer(on_audio, on_control)
        self._consumers[host] = token
        return token

    def unregister_consumer(self, host: str, token: Optional[_Consumer] = None) -> None:
        """Remove only if `token` matches; None removes unconditionally."""
        current = self._consumers.get(host)
        if current is None:
            return
        if token is not None and token is not current:
            _LOGGER.debug(
                "UdpSocketManager: stale unregister_consumer(%s) ignored "
                "(consumer was replaced)", host,
            )
            return
        self._consumers.pop(host, None)

    def has_consumer(self, host: str) -> bool:
        return host in self._consumers

    def send_audio(self, host: str, data: bytes) -> bool:
        if self._audio_transport is None:
            return False
        try:
            audio_port, _ = self.peer_ports(host)
            self._audio_transport.sendto(data, (host, audio_port))
            return True
        except Exception as err:
            _LOGGER.debug("send_audio to %s failed: %s", host, err)
            return False

    def send_control(self, host: str, packet: bytes) -> bool:
        if self._control_transport is None:
            return False
        try:
            _, control_port = self.peer_ports(host)
            self._control_transport.sendto(packet, (host, control_port))
            return True
        except Exception as err:
            _LOGGER.debug("send_control to %s failed: %s", host, err)
            return False

    def set_unsolicited_callback(self, cb: Optional[UnsolicitedCallback]) -> None:
        """Set the coroutine fired when MSG_START arrives from an unknown peer."""
        self._unsolicited_cb = cb

    def _on_audio_datagram(self, data: bytes, addr: tuple[str, int]) -> None:
        self._audio_recv += 1
        self._learn_peer_ports(addr[0], audio_port=addr[1])
        c = self._consumers.get(addr[0])
        if c is None:
            return  # leftover from a previous session, drop silently
        try:
            c.on_audio(data)
        except Exception:
            _LOGGER.exception("on_audio callback raised for %s", addr[0])

    def _on_control_datagram(self, data: bytes, addr: tuple[str, int]) -> None:
        self._control_recv += 1
        if len(data) < HEADER_SIZE:
            _LOGGER.warning("UdpSocketManager: short control datagram (%d bytes) from %s",
                            len(data), addr[0])
            return
        try:
            msg_type, length = protocol.parse_header(data)
        except ValueError:
            return
        payload = data[HEADER_SIZE : HEADER_SIZE + length]
        if len(payload) != length:
            _LOGGER.warning("UdpSocketManager: truncated payload from %s "
                            "(header=%d actual=%d)", addr[0], length, len(payload))
            return
        self._learn_peer_ports(addr[0], control_port=addr[1])

        c = self._consumers.get(addr[0])
        if c is not None:
            try:
                c.on_control(msg_type, payload)
            except Exception:
                _LOGGER.exception("on_control callback raised for %s", addr[0])
            return

        # No consumer registered for this peer.
        if msg_type == MSG_START and self._unsolicited_cb is not None:
            try:
                parsed = protocol.parse_start_body(payload)
            except ValueError as err:
                _LOGGER.warning("UdpSocketManager: malformed MSG_START from %s: %s",
                                addr[0], err)
                return
            _LOGGER.info(
                "UdpSocketManager: unsolicited MSG_START from %s "
                "(caller=%s/%s dest=%s/%s call_id=%s)",
                addr[0],
                parsed["caller_name"] or "(unknown)", parsed["caller_route"] or "-",
                parsed["dest_name"] or "(self)", parsed["dest_route"] or "-",
                parsed["call_id"] or "-",
            )
            self.hass.async_create_task(self._unsolicited_cb(
                parsed["caller_name"], parsed["caller_route"],
                parsed["dest_name"], parsed["dest_route"], parsed["call_id"],
                addr[0], addr[1],
            ))
        else:
            _LOGGER.debug("UdpSocketManager: dropped 0x%02X from unregistered %s",
                          msg_type, addr[0])


def get_manager(hass: HomeAssistant) -> Optional[IntercomUdpSocketManager]:
    """Return the active manager from hass.data, or None if UDP is disabled."""
    return hass.data.get(DOMAIN, {}).get("udp_manager")
