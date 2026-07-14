from __future__ import annotations

import asyncio
import importlib.util
import socket
import sys
import types
import unittest
from unittest import mock
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PKG_NAME = "custom_components.voip_stack"
PKG_DIR = ROOT / "custom_components" / "voip_stack"


def _load(name: str):
    if "custom_components" not in sys.modules:
        package = types.ModuleType("custom_components")
        package.__path__ = [str(ROOT / "custom_components")]
        sys.modules["custom_components"] = package
    if PKG_NAME not in sys.modules:
        package = types.ModuleType(PKG_NAME)
        package.__path__ = [str(PKG_DIR)]
        sys.modules[PKG_NAME] = package
    full_name = f"{PKG_NAME}.{name}"
    if full_name in sys.modules:
        return sys.modules[full_name]
    spec = importlib.util.spec_from_file_location(full_name, PKG_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module


rtp = _load("rtp")
sdp = _load("sdp")
_load("video_rtcp")
sip_video_relay = _load("sip_video_relay")
RtpVideoFormat = sdp.RtpVideoFormat
video_formats_passthrough_compatible = sdp.video_formats_passthrough_compatible
SipVideoRtpRelay = sip_video_relay.SipVideoRtpRelay
VideoRtpPeer = sip_video_relay.VideoRtpPeer
build_pli = sys.modules[f"{PKG_NAME}.video_rtcp"].build_pli


class _Transport:
    def __init__(self) -> None:
        self.sent: list[tuple[bytes, tuple[str, int]]] = []

    def sendto(self, data: bytes, addr) -> None:
        self.sent.append((data, addr))

    def close(self) -> None:
        return


def _format(payload_type: int, *, direction: str = "sendrecv", profile: str = "42e01f") -> RtpVideoFormat:
    return RtpVideoFormat(
        payload_type=payload_type,
        encoding="H264",
        profile_level_id=profile,
        packetization_mode=1,
        direction=direction,
    )


class SipVideoRelayTests(unittest.TestCase):
    def setUp(self) -> None:
        self.left = VideoRtpPeer("10.0.0.1", 10000, 10001, _format(102))
        self.right = VideoRtpPeer("10.0.0.2", 20000, 20001, _format(110))
        self.relay = SipVideoRtpRelay(
            left=self.left,
            right=self.right,
            left_port=30000,
            right_port=30002,
        )
        self.right_rtp = _Transport()
        self.left_rtp = _Transport()
        self.right_rtcp = _Transport()
        self.left_rtcp = _Transport()
        self.relay._transports = {  # noqa: SLF001 - deterministic unit boundary.
            ("left", False): self.left_rtp,
            ("right", False): self.right_rtp,
            ("left", True): self.left_rtcp,
            ("right", True): self.right_rtcp,
        }

    def test_rewrites_only_leg_local_payload_type(self) -> None:
        packet = rtp.build_packet(
            rtp.RtpPacket(
                payload_type=102,
                sequence=7,
                timestamp=12345,
                ssrc=0x11223344,
                marker=True,
                payload=b"encoded-frame",
            )
        )

        self.relay.handle_rtp("left", packet, ("10.0.0.1", 10008))

        self.assertEqual(len(self.right_rtp.sent), 1)
        outgoing, destination = self.right_rtp.sent[0]
        self.assertEqual(destination, ("10.0.0.2", 20000))
        self.assertEqual(outgoing[:1], packet[:1])
        self.assertEqual(outgoing[2:], packet[2:])
        parsed = rtp.parse_packet(outgoing)
        self.assertEqual(parsed.payload_type, 110)
        self.assertTrue(parsed.marker)
        self.assertEqual(parsed.sequence, 7)
        self.assertEqual(parsed.timestamp, 12345)
        self.assertEqual(parsed.ssrc, 0x11223344)
        self.assertEqual(self.left.port, 10008)

    def test_rejects_unnegotiated_direction_and_ssrc_change(self) -> None:
        self.left.video_format = _format(102, direction="recvonly")
        packet = rtp.build_packet(rtp.RtpPacket(102, 1, 1, 1, b"x"))
        self.relay.handle_rtp("left", packet, ("10.0.0.1", 10000))
        self.assertFalse(self.right_rtp.sent)

        self.left.video_format = _format(102)
        self.relay.handle_rtp("left", packet, ("10.0.0.1", 10000))
        changed = rtp.build_packet(rtp.RtpPacket(102, 2, 2, 2, b"x"))
        self.relay.handle_rtp("left", changed, ("10.0.0.1", 10000))
        self.assertEqual(len(self.right_rtp.sent), 1)
        self.assertEqual(self.relay.dropped, 2)

    def test_forwards_valid_rtcp_feedback(self) -> None:
        pli = build_pli(1, 2)
        self.relay.handle_rtcp("right", pli, ("10.0.0.2", 20009))
        self.assertEqual(self.left_rtcp.sent, [(pli, ("10.0.0.1", 10001))])
        self.assertEqual(self.right.rtcp_port, 20009)

        self.relay.handle_rtcp("right", b"bad", ("10.0.0.2", 20009))
        self.assertEqual(self.relay.dropped, 1)

        # An unexpected source is an ordinary media drop, not an exception
        # escaping asyncio.DatagramProtocol.datagram_received().
        self.relay.handle_rtcp("right", pli, ("10.0.0.99", 20009))
        self.assertEqual(self.relay.dropped, 2)

    def test_passthrough_compatibility_ignores_pt_and_direction(self) -> None:
        self.assertTrue(
            video_formats_passthrough_compatible(
                _format(102, direction="sendonly"),
                _format(110, direction="recvonly"),
            )
        )
        self.assertFalse(
            video_formats_passthrough_compatible(
                _format(102),
                _format(110, profile="64001f"),
            )
        )


class SipVideoRelayLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_failed_endpoint_creation_closes_all_prebound_sockets(self) -> None:
        sockets = [socket.socket(socket.AF_INET, socket.SOCK_DGRAM) for _ in range(4)]
        released: list[tuple[int, int]] = []
        relay = SipVideoRtpRelay(
            left=VideoRtpPeer("10.0.0.1", 10000, 10001, _format(102)),
            right=VideoRtpPeer("10.0.0.2", 20000, 20001, _format(110)),
            left_port=30000,
            right_port=30002,
            left_socket=sockets[0],
            right_socket=sockets[1],
            left_rtcp_socket=sockets[2],
            right_rtcp_socket=sockets[3],
            on_release=released.append,
        )
        loop = asyncio.get_running_loop()
        with mock.patch.object(
            loop,
            "create_datagram_endpoint",
            new=mock.AsyncMock(side_effect=OSError("bind failed")),
        ):
            with self.assertRaisesRegex(OSError, "bind failed"):
                await relay.start()

        self.assertTrue(all(item.fileno() == -1 for item in sockets))
        self.assertEqual(released, [(30000, 30002)])


if __name__ == "__main__":
    unittest.main()
