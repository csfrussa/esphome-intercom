"""Exact-codec RTP/RTCP video relay for HA-owned SIP bridges."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import socket
from typing import Any, Callable

from . import rtp
from .sdp import RtpVideoFormat
from .video_rtcp import RtcpError, parse_compound


_LOGGER = logging.getLogger(__name__)
_RTP_IP_TOS = 0x88


def remote_can_send(video_format: RtpVideoFormat | None) -> bool:
    """Return whether a remote endpoint may send media in its SDP direction."""

    return bool(video_format is not None and video_format.direction in {"sendonly", "sendrecv"})


def remote_can_receive(video_format: RtpVideoFormat | None) -> bool:
    """Return whether a remote endpoint may receive media in its SDP direction."""

    return bool(video_format is not None and video_format.direction in {"recvonly", "sendrecv"})


@dataclass(slots=True)
class VideoRtpPeer:
    """One negotiated remote video leg."""

    host: str
    port: int
    rtcp_port: int
    video_format: RtpVideoFormat
    rx_ssrc: int | None = None


class _VideoRelayProtocol(asyncio.DatagramProtocol):
    def __init__(self, relay: "SipVideoRtpRelay", side: str, *, rtcp: bool = False) -> None:
        self.relay = relay
        self.side = side
        self.rtcp = rtcp

    def datagram_received(self, data: bytes, addr) -> None:
        if self.rtcp:
            self.relay.handle_rtcp(self.side, data, addr)
        else:
            self.relay.handle_rtp(self.side, data, addr)


class SipVideoRtpRelay:
    """Relay encoded video without decoding or changing timestamps/SSRC.

    The payload type is the only RTP header field rewritten because it is
    negotiated independently on each SIP leg. RTP extensions, CSRCs, marker,
    sequence, timestamp and encoded payload remain byte-for-byte intact.
    """

    def __init__(
        self,
        *,
        left: VideoRtpPeer,
        right: VideoRtpPeer,
        left_port: int,
        right_port: int,
        left_socket: socket.socket | None = None,
        right_socket: socket.socket | None = None,
        left_rtcp_socket: socket.socket | None = None,
        right_rtcp_socket: socket.socket | None = None,
        on_release: Callable[[tuple[int, int]], None] | None = None,
    ) -> None:
        self.left = left
        self.right = right
        self.left_port = int(left_port)
        self.right_port = int(right_port)
        self._sockets = {
            ("left", False): left_socket,
            ("right", False): right_socket,
            ("left", True): left_rtcp_socket,
            ("right", True): right_rtcp_socket,
        }
        self._transports: dict[tuple[str, bool], asyncio.DatagramTransport] = {}
        self._on_release = on_release
        self._released = False
        self.started = False
        self.forwarded = 0
        self.rtcp_forwarded = 0
        self.dropped = 0
        self.left_rx_packets = 0
        self.left_rx_bytes = 0
        self.left_tx_packets = 0
        self.left_tx_bytes = 0
        self.right_rx_packets = 0
        self.right_rx_bytes = 0
        self.right_tx_packets = 0
        self.right_tx_bytes = 0

    @staticmethod
    def _socket(port: int) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            sock.setblocking(False)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 1024 * 1024)
            sock.setsockopt(socket.IPPROTO_IP, socket.IP_TOS, _RTP_IP_TOS)
            sock.bind(("0.0.0.0", int(port)))
            return sock
        except BaseException:
            sock.close()
            raise

    async def start(self) -> None:
        if self.started:
            return
        loop = asyncio.get_running_loop()
        try:
            for side, port in (("left", self.left_port), ("right", self.right_port)):
                for rtcp in (False, True):
                    key = (side, rtcp)
                    sock = self._sockets.get(key)
                    if sock is None:
                        sock = self._socket(port + (1 if rtcp else 0))
                    transport, _ = await loop.create_datagram_endpoint(
                        lambda side=side, rtcp=rtcp: _VideoRelayProtocol(self, side, rtcp=rtcp),
                        sock=sock,
                    )
                    # Ownership moves to the transport only after endpoint
                    # creation succeeds. On failure stop() must still see and
                    # close the pre-bound socket supplied by the port owner.
                    self._sockets[key] = None
                    self._transports[key] = transport
            self.started = True
        except BaseException:
            await self.stop()
            raise
        _LOGGER.info(
            "SIP video relay ready left=%s/%s right=%s/%s codec=%s",
            self.left_port,
            self.left_port + 1,
            self.right_port,
            self.right_port + 1,
            self.left.video_format.encoding,
        )

    async def stop(self) -> None:
        for transport in self._transports.values():
            transport.close()
        self._transports.clear()
        for sock in self._sockets.values():
            if sock is not None:
                sock.close()
        self._sockets.clear()
        was_started = self.started
        self.started = False
        if self._on_release is not None and not self._released:
            self._released = True
            self._on_release((self.left_port, self.right_port))
        if was_started:
            _LOGGER.info(
                "SIP video relay stopped forwarded=%d rtcp=%d dropped=%d",
                self.forwarded,
                self.rtcp_forwarded,
                self.dropped,
            )

    def _peers(self, side: str) -> tuple[VideoRtpPeer, VideoRtpPeer]:
        return (self.left, self.right) if side == "left" else (self.right, self.left)

    def handle_rtp(self, side: str, data: bytes, addr) -> None:
        source, destination = self._peers(side)
        output = self._transports.get(("right" if side == "left" else "left", False))
        try:
            if not remote_can_send(source.video_format) or not remote_can_receive(destination.video_format):
                raise ValueError("RTP direction is not negotiated")
            if str(addr[0]) != source.host:
                raise ValueError("unexpected RTP source host")
            packet = rtp.parse_packet(data)
            if packet.payload_type != int(source.video_format.payload_type):
                raise ValueError("unexpected RTP payload type")
            if source.rx_ssrc is not None and packet.ssrc != source.rx_ssrc:
                raise ValueError("unexpected RTP SSRC")
            if source.rx_ssrc is None:
                source.rx_ssrc = packet.ssrc
            source.port = int(addr[1])
            if output is None or destination.port <= 0:
                raise ValueError("destination RTP leg is not ready")
            payload_type = int(destination.video_format.payload_type)
            if not 0 <= payload_type <= 127:
                raise ValueError("invalid destination RTP payload type")
            outgoing = data if payload_type == packet.payload_type else bytes(
                (data[0], (data[1] & 0x80) | payload_type)
            ) + data[2:]
            output.sendto(outgoing, (destination.host, int(destination.port)))
        except (OSError, RuntimeError, ValueError) as err:
            self.dropped += 1
            _LOGGER.debug("SIP video RTP relay drop side=%s: %s", side, err)
            return
        self._account(side, len(data), len(outgoing))
        self.forwarded += 1

    def handle_rtcp(self, side: str, data: bytes, addr) -> None:
        source, destination = self._peers(side)
        output = self._transports.get(("right" if side == "left" else "left", True))
        try:
            if str(addr[0]) != source.host:
                raise ValueError("unexpected RTCP source host")
            parse_compound(data)
            source.rtcp_port = int(addr[1])
            if output is None or destination.rtcp_port <= 0:
                raise ValueError("destination RTCP leg is not ready")
            output.sendto(data, (destination.host, int(destination.rtcp_port)))
        except (OSError, RuntimeError, RtcpError, ValueError) as err:
            self.dropped += 1
            _LOGGER.debug("SIP video RTCP relay drop side=%s: %s", side, err)
            return
        self.rtcp_forwarded += 1

    def _account(self, side: str, received: int, sent: int) -> None:
        if side == "left":
            self.left_rx_packets += 1
            self.left_rx_bytes += int(received)
            self.right_tx_packets += 1
            self.right_tx_bytes += int(sent)
        else:
            self.right_rx_packets += 1
            self.right_rx_bytes += int(received)
            self.left_tx_packets += 1
            self.left_tx_bytes += int(sent)

    def snapshot(self) -> dict[str, Any]:
        return {
            "codec": self.left.video_format.encoding,
            "left_port": self.left_port,
            "right_port": self.right_port,
            "left_peer": f"{self.left.host}:{self.left.port}",
            "right_peer": f"{self.right.host}:{self.right.port}",
            "forwarded_packets": self.forwarded,
            "forwarded_rtcp_packets": self.rtcp_forwarded,
            "dropped_packets": self.dropped,
            "left_rx_packets": self.left_rx_packets,
            "left_rx_bytes": self.left_rx_bytes,
            "left_tx_packets": self.left_tx_packets,
            "left_tx_bytes": self.left_tx_bytes,
            "right_rx_packets": self.right_rx_packets,
            "right_rx_bytes": self.right_rx_bytes,
            "right_tx_packets": self.right_tx_packets,
            "right_tx_bytes": self.right_tx_bytes,
        }
