#!/usr/bin/env python3
"""Golden tests for the phase-1 SIP/SDP/RTP PCM profile."""

from __future__ import annotations

import importlib.util
import asyncio
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PKG_NAME = "custom_components.intercom_native"
PKG_DIR = ROOT / "custom_components" / "intercom_native"


def _load_intercom_module(name: str):
    if "custom_components" not in sys.modules:
        root_pkg = types.ModuleType("custom_components")
        root_pkg.__path__ = [str(ROOT / "custom_components")]
        sys.modules["custom_components"] = root_pkg
    if PKG_NAME not in sys.modules:
        pkg = types.ModuleType(PKG_NAME)
        pkg.__path__ = [str(PKG_DIR)]
        sys.modules[PKG_NAME] = pkg

    full_name = f"{PKG_NAME}.{name}"
    spec = importlib.util.spec_from_file_location(full_name, PKG_DIR / f"{name}.py")
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {full_name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module


audio_format = _load_intercom_module("audio_format")
sip = _load_intercom_module("sip")
sdp = _load_intercom_module("sdp")
rtp = _load_intercom_module("rtp")
roster = _load_intercom_module("roster")
sip_client = _load_intercom_module("sip_client")
sip_listener = _load_intercom_module("sip_listener")
sip_rtp_bridge = _load_intercom_module("sip_rtp_bridge")


class SipProfileTest(unittest.TestCase):
    def test_build_and_parse_invite_with_l16_sdp(self) -> None:
        body = sdp.build_offer(
            "192.168.1.20",
            "192.168.1.20",
            40000,
            [audio_format.AudioFormat(48000, "s16le", 1, 20)],
        ).encode()
        msg = sip.build_request(
            "INVITE",
            "sip:Cucina@192.168.1.30",
            [
                ("Via", "SIP/2.0/UDP 192.168.1.20:5060;branch=z9hG4bKtest"),
                ("Max-Forwards", "70"),
                ("From", "<sip:Spotpear@192.168.1.20>;tag=abc"),
                ("To", "<sip:Cucina@192.168.1.30>"),
                ("Call-ID", "call-1"),
                ("CSeq", "1 INVITE"),
                ("Contact", "<sip:Spotpear@192.168.1.20:5060>"),
                ("Content-Type", "application/sdp"),
            ],
            body,
        )
        parsed = sip.parse_message(msg)
        self.assertEqual(parsed.method, "INVITE")
        self.assertEqual(parsed.uri, "sip:Cucina@192.168.1.30")
        self.assertEqual(parsed.header("Call-ID"), "call-1")
        self.assertEqual(parsed.body, body)

    def test_rejects_unknown_method(self) -> None:
        raw = (
            b"REGISTER sip:ha@192.168.1.10 SIP/2.0\r\n"
            b"Content-Length: 0\r\n\r\n"
        )
        with self.assertRaises(sip.SipError):
            sip.parse_message(raw)

    def test_rejects_trailing_bytes_after_content_length(self) -> None:
        raw = (
            b"OPTIONS sip:ha@192.168.1.10 SIP/2.0\r\n"
            b"Content-Length: 0\r\n\r\nx"
        )
        with self.assertRaises(sip.SipError):
            sip.parse_message(raw)

    def test_ack_reuses_invite_cseq_with_fresh_branch(self) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[bytes, tuple[str, int]]] = []

            def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
                self.sent.append((data, addr))

        client = sip_client.SipCallClient(
            local_ip="192.168.1.10",
            local_name="HA",
            local_sip_port=5060,
            local_rtp_port=41000,
        )
        client.transport = FakeTransport()  # type: ignore[assignment]
        client.dialog_ids = sip.SipDialogIds(
            call_id="call-ack",
            local_tag="local",
            remote_tag="remote",
            cseq=7,
            branch="z9hG4bKinvite",
        )
        client._invite_cseq = 7
        client._send_ack(
            "192.168.1.30",
            5060,
            "sip:Cucina@192.168.1.30",
            "sip:HA@192.168.1.10:5060",
            "sip:Cucina@192.168.1.30:5060",
        )
        raw, addr = client.transport.sent[0]  # type: ignore[union-attr]
        parsed = sip.parse_message(raw)
        self.assertEqual(addr, ("192.168.1.30", 5060))
        self.assertEqual(parsed.method, "ACK")
        self.assertEqual(parsed.header("CSeq"), "7 ACK")
        self.assertNotIn("z9hG4bKinvite", parsed.header("Via"))


class SdpPcmProfileTest(unittest.TestCase):
    def test_negotiate_l16_48k(self) -> None:
        offer = sdp.build_offer(
            "192.168.1.20",
            "192.168.1.20",
            40000,
            [
                audio_format.AudioFormat(48000, "s16le", 1, 20),
                audio_format.AudioFormat(16000, "s16le", 1, 20),
            ],
        )
        selected = sdp.negotiate(
            offer,
            [
                audio_format.AudioFormat(16000, "s16le", 1, 20),
                audio_format.AudioFormat(48000, "s16le", 1, 20),
            ],
        )
        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected.encoding, "L16")
        self.assertEqual(selected.sample_rate, 16000)
        self.assertEqual(selected.payload_type, 97)

    def test_negotiate_l24_from_s24(self) -> None:
        offer = sdp.build_offer(
            "192.168.1.20",
            "192.168.1.20",
            40000,
            [audio_format.AudioFormat(48000, "s24le", 1, 20)],
        )
        offered = sdp.offered_pcm_formats(offer)
        self.assertEqual(offered[0].encoding, "L24")
        self.assertEqual(offered[0].sample_rate, 48000)

    def test_rejects_compressed_only_offer(self) -> None:
        offer = (
            "v=0\r\n"
            "o=- 0 0 IN IP4 192.168.1.20\r\n"
            "s=Phone\r\n"
            "c=IN IP4 192.168.1.20\r\n"
            "t=0 0\r\n"
            "m=audio 40000 RTP/AVP 0 8 96\r\n"
            "a=rtpmap:0 PCMU/8000\r\n"
            "a=rtpmap:8 PCMA/8000\r\n"
            "a=rtpmap:96 opus/48000/2\r\n"
        )
        selected = sdp.negotiate(offer, [audio_format.AudioFormat(16000, "s16le", 1, 20)])
        self.assertIsNone(selected)

    def test_rejects_s32_wire_mapping(self) -> None:
        with self.assertRaises(sdp.SdpError):
            sdp.audio_format_to_rtp(audio_format.AudioFormat(48000, "s32le", 1, 20), 96)


class RtpProfileTest(unittest.TestCase):
    def test_rtp_packet_round_trip(self) -> None:
        packet = rtp.RtpPacket(
            payload_type=96,
            marker=True,
            sequence=65535,
            timestamp=0xFFFFFFF0,
            ssrc=0x12345678,
            payload=b"\x00\x01\x02\x03",
        )
        raw = rtp.build_packet(packet)
        parsed = rtp.parse_packet(raw)
        self.assertEqual(parsed, packet)
        self.assertEqual(rtp.next_sequence(packet.sequence), 0)
        self.assertEqual(rtp.next_timestamp(packet.timestamp, 32), 16)

    def test_rejects_bad_rtp_version(self) -> None:
        raw = bytearray(rtp.build_packet(rtp.RtpPacket(96, 1, 2, 3, b"x")))
        raw[0] = 0
        with self.assertRaises(rtp.RtpError):
            rtp.parse_packet(bytes(raw))


class RosterResolverTest(unittest.TestCase):
    def test_route_decisions(self) -> None:
        entries = roster.parse_roster_json(
            {
                "contacts": [
                    {"id": "HA", "kind": "ha", "address": "192.168.1.10"},
                    {"id": "Cucina", "kind": "esp", "address": "192.168.1.30"},
                    {"id": "Corridoio", "kind": "esp"},
                    {"id": "Nonna", "kind": "phone", "number": "0574863562"},
                ]
            }
        )
        self.assertEqual(
            roster.resolve_target("Cucina", entries).sip_uri,
            "sip:Cucina@192.168.1.30",
        )
        self.assertEqual(
            roster.resolve_target("Cucina", entries, route_via_ha=True).sip_uri,
            "sip:Cucina@192.168.1.10",
        )
        self.assertEqual(
            roster.resolve_target("Corridoio", entries).sip_uri,
            "sip:Corridoio@192.168.1.10",
        )
        phone = roster.resolve_target("Nonna", entries)
        self.assertEqual(phone.kind, "requires_pbx")
        self.assertEqual(phone.sip_uri, "sip:0574863562@192.168.1.10")

    def test_explicit_sip_uri_and_name_at_ip(self) -> None:
        entries = roster.parse_roster_json([{"id": "HA", "kind": "ha", "address": "192.168.1.10"}])
        self.assertEqual(
            roster.resolve_target("sip:Cucina@192.168.1.30", entries).sip_uri,
            "sip:Cucina@192.168.1.30",
        )
        self.assertEqual(
            roster.resolve_target("Cucina@192.168.1.30", entries).sip_uri,
            "sip:Cucina@192.168.1.30",
        )


class PbxLiteBridgeTest(unittest.IsolatedAsyncioTestCase):
    async def test_symbolic_target_bridges_through_ha_with_rtp_relay(self) -> None:
        local = "127.0.0.1"
        ha_sip = 5070
        caller_rtp = 41700
        dest_sip = 5072
        dest_rtp = 41720
        ha_rtp_left = 41740
        ha_rtp_right = 41742
        audio = audio_format.AudioFormat(16000, "s16le", 1, 32)
        stats = {"dest_invites": 0, "caller_rtp_rx": 0, "dest_rtp_rx": 0}

        class DestRtp(asyncio.DatagramProtocol):
            def __init__(self) -> None:
                self.transport = None
                self.remote: tuple[str, int, int] | None = None
                self.sequence = 10
                self.timestamp = 0

            def connection_made(self, transport) -> None:
                self.transport = transport

            def datagram_received(self, data: bytes, addr) -> None:
                rtp.parse_packet(data)
                stats["dest_rtp_rx"] += 1

            async def send_loop(self) -> None:
                while True:
                    await asyncio.sleep(0.032)
                    if self.transport is None or self.remote is None:
                        continue
                    host, port, pt = self.remote
                    packet = rtp.build_packet(
                        rtp.RtpPacket(
                            payload_type=pt,
                            sequence=self.sequence,
                            timestamp=self.timestamp,
                            ssrc=0x2222,
                            payload=b"\0" * 1024,
                        )
                    )
                    self.transport.sendto(packet, (host, port))
                    self.sequence = rtp.next_sequence(self.sequence)
                    self.timestamp = rtp.next_timestamp(self.timestamp, 512)

        class CallerRtp(asyncio.DatagramProtocol):
            def __init__(self) -> None:
                self.transport = None
                self.remote: tuple[str, int, int] | None = None
                self.sequence = 500
                self.timestamp = 0

            def connection_made(self, transport) -> None:
                self.transport = transport

            def datagram_received(self, data: bytes, addr) -> None:
                rtp.parse_packet(data)
                stats["caller_rtp_rx"] += 1

            async def send_loop(self) -> None:
                while True:
                    await asyncio.sleep(0.032)
                    if self.transport is None or self.remote is None:
                        continue
                    host, port, pt = self.remote
                    packet = rtp.build_packet(
                        rtp.RtpPacket(
                            payload_type=pt,
                            sequence=self.sequence,
                            timestamp=self.timestamp,
                            ssrc=0x1111,
                            payload=b"\0" * 1024,
                        )
                    )
                    self.transport.sendto(packet, (host, port))
                    self.sequence = rtp.next_sequence(self.sequence)
                    self.timestamp = rtp.next_timestamp(self.timestamp, 512)

        dest_rtp_proto = DestRtp()
        caller_rtp_proto = CallerRtp()
        relay: sip_rtp_bridge.SipRtpRelay | None = None
        dest_client: sip_client.SipCallClient | None = None
        tasks: list[asyncio.Task] = []

        async def dest_invite(invite):
            stats["dest_invites"] += 1
            dest_rtp_proto.remote = (
                invite.remote_rtp_host,
                invite.remote_rtp_port,
                invite.selected_format.payload_type,
            )
            return sip_listener.SipInviteResult(
                200,
                "OK",
                answer_sdp=sdp.build_answer(local, local, dest_rtp, invite.selected_format),
            )

        async def ha_invite(invite):
            nonlocal relay, dest_client
            entries = [
                roster.RosterEntry(id="HA", kind="ha", address=local, metadata={"sip_port": ha_sip}),
                roster.RosterEntry(id="Cucina", kind="esp", address=local, metadata={"sip_port": dest_sip}),
            ]
            decision = roster.resolve_target(invite.target, entries, route_via_ha=True)
            self.assertEqual(decision.sip_uri, f"sip:Cucina@{local}:{ha_sip}")
            self.assertIsNotNone(decision.entry)
            dest_client = sip_client.SipCallClient(
                local_ip=local,
                local_name="HA",
                local_sip_port=ha_sip,
                local_rtp_port=ha_rtp_right,
                supported_formats=[invite.selected_format.audio_format],
            )
            result = await dest_client.invite(
                target=decision.entry.id,
                remote_host=decision.entry.address,
                remote_sip_port=decision.entry.metadata["sip_port"],
            )
            self.assertEqual(result, "streaming")
            assert dest_client.dialog is not None
            relay = sip_rtp_bridge.SipRtpRelay(
                left=sip_rtp_bridge.RtpPeer(
                    invite.remote_rtp_host,
                    invite.remote_rtp_port,
                    invite.selected_format.payload_type,
                    invite.selected_format.audio_format,
                ),
                right=sip_rtp_bridge.RtpPeer(
                    dest_client.dialog.remote_rtp_host,
                    dest_client.dialog.remote_rtp_port,
                    dest_client.dialog.selected_format.payload_type,
                    dest_client.dialog.selected_format.audio_format,
                ),
                left_port=ha_rtp_left,
                right_port=ha_rtp_right,
            )
            await relay.start()
            return sip_listener.SipInviteResult(
                200,
                "OK",
                answer_sdp=sdp.build_answer(local, local, ha_rtp_left, invite.selected_format),
            )

        dest_server = sip_listener.SipUdpServer(
            host=local,
            port=dest_sip,
            local_ip=local,
            local_rtp_port=dest_rtp,
            supported_formats=[audio],
            on_invite=dest_invite,
        )
        ha_server = sip_listener.SipUdpServer(
            host=local,
            port=ha_sip,
            local_ip=local,
            local_rtp_port=ha_rtp_left,
            supported_formats=[audio],
            on_invite=ha_invite,
        )
        self.assertTrue(await dest_server.start())
        self.assertTrue(await ha_server.start())
        loop = asyncio.get_running_loop()
        dest_transport, _ = await loop.create_datagram_endpoint(lambda: dest_rtp_proto, local_addr=(local, dest_rtp))
        caller_transport, _ = await loop.create_datagram_endpoint(
            lambda: caller_rtp_proto,
            local_addr=(local, caller_rtp),
        )
        tasks.append(asyncio.create_task(dest_rtp_proto.send_loop()))
        tasks.append(asyncio.create_task(caller_rtp_proto.send_loop()))
        caller = sip_client.SipCallClient(
            local_ip=local,
            local_name="Spotpear",
            local_sip_port=5066,
            local_rtp_port=caller_rtp,
            supported_formats=[audio],
        )
        try:
            self.assertEqual(await caller.invite(target="Cucina", remote_host=local, remote_sip_port=ha_sip), "streaming")
            assert caller.dialog is not None
            caller_rtp_proto.remote = (
                caller.dialog.remote_rtp_host,
                caller.dialog.remote_rtp_port,
                caller.dialog.selected_format.payload_type,
            )
            await asyncio.sleep(0.4)
            self.assertEqual(stats["dest_invites"], 1)
            self.assertGreater(stats["caller_rtp_rx"], 0)
            self.assertGreater(stats["dest_rtp_rx"], 0)
            assert relay is not None
            self.assertGreater(relay.forwarded, 0)
            self.assertEqual(relay.dropped, 0)
        finally:
            caller.bye()
            await caller.close()
            if dest_client is not None:
                dest_client.bye()
                await dest_client.close()
            if relay is not None:
                await relay.stop()
            await ha_server.stop()
            await dest_server.stop()
            dest_transport.close()
            caller_transport.close()
            for task in tasks:
                task.cancel()


if __name__ == "__main__":
    unittest.main()
