#!/usr/bin/env python3
"""Golden tests for the phase-1 SIP/SDP/RTP PCM profile."""

from __future__ import annotations

import importlib.util
import asyncio
import contextlib
import socket
import sys
import tempfile
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PKG_NAME = "custom_components.voip_stack"
PKG_DIR = ROOT / "custom_components" / "voip_stack"


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
    if full_name in sys.modules:
        return sys.modules[full_name]
    spec = importlib.util.spec_from_file_location(full_name, PKG_DIR / f"{name}.py")
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {full_name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module


audio_format = _load_intercom_module("audio_format")
audio_pcm = _load_intercom_module("audio_pcm")
sip = _load_intercom_module("sip")
sdp = _load_intercom_module("sdp")
rtp = _load_intercom_module("rtp")
roster = _load_intercom_module("roster")
router = _load_intercom_module("router")
debug_capture = _load_intercom_module("debug_capture")
sip_client = _load_intercom_module("sip_client")
sip_tcp_io = _load_intercom_module("sip_tcp_io")
sip_listener = _load_intercom_module("sip_listener")
sip_registrar = _load_intercom_module("sip_registrar")
sip_auth = _load_intercom_module("sip_auth")
sip_rtp_bridge = _load_intercom_module("sip_rtp_bridge")
sip_trunk = _load_intercom_module("sip_trunk")
dtmf = _load_intercom_module("dtmf")


class SipUriTest(unittest.TestCase):
    def test_uri_user_is_percent_encoded_and_line_breaks_are_rejected(self) -> None:
        self.assertEqual(
            str(sip.SipUri("Home Assistant", "192.168.1.10", 5060)),
            "sip:Home%20Assistant@192.168.1.10:5060",
        )
        with self.assertRaises(sip.SipError):
            sip.parse_sip_uri("sip:test@192.168.1.10\r\nX-Injected: yes")

    def test_endpoint_identity_includes_signaling_port(self) -> None:
        self.assertTrue(sip.sip_endpoints_equal("192.0.2.10", 5060, "192.0.2.10", 5060))
        self.assertFalse(sip.sip_endpoints_equal("192.0.2.10", 5062, "192.0.2.10", 5060))
        self.assertTrue(sip.sip_endpoints_equal("[2001:db8::1]", 5060, "2001:db8::1", 5060))

    def test_local_listener_match_requires_host_and_port(self) -> None:
        local = sip.parse_sip_uri("sip:HA@127.0.0.1:15060")
        sibling = sip.parse_sip_uri("sip:Phone@127.0.0.1:15102")
        kwargs = {
            "listener_hosts": ("localhost", "127.0.0.1", "::1"),
            "listener_port": 15060,
        }
        self.assertTrue(sip.sip_uri_targets_listener(local, **kwargs))
        self.assertFalse(sip.sip_uri_targets_listener(sibling, **kwargs))

    def test_message_builder_rejects_header_injection(self) -> None:
        with self.assertRaises(sip.SipError):
            sip.build_request(
                "OPTIONS",
                "sip:test@192.168.1.10",
                [("Call-ID", "safe\r\nX-Injected: yes")],
            )

    def test_debug_capture_names_are_path_safe_and_collision_resistant(self) -> None:
        hostile = debug_capture.safe_capture_name("../../etc/passwd")
        absolute = debug_capture.safe_capture_name("/tmp/escape")
        self.assertNotIn("/", hostile)
        self.assertNotIn("..", hostile)
        self.assertNotEqual(hostile, absolute)

    def test_debug_wav_packs_right_aligned_24_bit_containers(self) -> None:
        fmt = audio_format.AudioFormat(16000, "s24le_in_s32", 1, 20)
        width, payload = debug_capture.wav_pcm_payload(
            fmt,
            bytes((0x56, 0x34, 0x12, 0x00, 0xAA, 0xCB, 0xED, 0xFF)),
        )

        self.assertEqual(width, 3)
        self.assertEqual(payload, bytes((0x56, 0x34, 0x12, 0xAA, 0xCB, 0xED)))

    def test_debug_capture_retention_bounds_files_and_bytes(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            for index in range(5):
                (directory / f"capture-{index}.wav").write_bytes(b"x" * 10)
            untouched = directory / "notes.txt"
            untouched.write_text("keep", encoding="utf-8")
            debug_capture.prune_debug_captures(directory, max_files=3, max_bytes=25)
            self.assertLessEqual(len(list(directory.glob("*.wav"))), 2)
            self.assertTrue(untouched.exists())

    def test_debug_capture_directory_is_private(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir) / "capture"
            debug_capture.ensure_debug_capture_dir(directory)
            self.assertEqual(directory.stat().st_mode & 0o777, 0o700)

    def test_parse_host_only_uri_used_by_standard_register_routes(self) -> None:
        uri = sip.parse_sip_uri("sip:192.168.1.10;transport=tcp")
        self.assertEqual(uri.user, "")
        self.assertEqual(uri.host, "192.168.1.10")
        self.assertEqual(uri.params, (("transport", "tcp"),))
        self.assertEqual(str(uri), "sip:192.168.1.10;transport=tcp")

    def test_extract_tag_ignores_quoted_display_name_parameters(self) -> None:
        self.assertEqual(sip.extract_tag("<sip:a@b>;tag=abc;x=y"), "abc")
        self.assertEqual(sip.extract_tag("<sip:a@b>;TAG=ABC;x=y"), "ABC")
        self.assertEqual(sip.extract_tag(""), "")
        self.assertEqual(sip.extract_tag('"not;tag=quoted" <sip:a@b>;tag=real;x=y'), "real")


def _load_sip_transport_with_homeassistant_stubs():
    if "homeassistant" not in sys.modules:
        ha_pkg = types.ModuleType("homeassistant")
        ha_pkg.__path__ = []
        sys.modules["homeassistant"] = ha_pkg
    core = types.ModuleType("homeassistant.core")
    core.HomeAssistant = object
    sys.modules["homeassistant.core"] = core
    components = types.ModuleType("homeassistant.components")
    components.__path__ = []
    sys.modules["homeassistant.components"] = components
    network = types.ModuleType("homeassistant.components.network")

    async def async_get_announce_addresses(_hass):
        return ["127.0.0.1"]

    network.async_get_announce_addresses = async_get_announce_addresses
    sys.modules["homeassistant.components.network"] = network
    return _load_intercom_module("fsm")


@contextlib.contextmanager
def _reserved_udp_ports(count: int):
    sockets = []
    try:
        ports = []
        for _ in range(count):
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.bind(("127.0.0.1", 0))
            sockets.append(sock)
            ports.append(sock.getsockname()[1])
        yield ports
    finally:
        for sock in sockets:
            sock.close()


class SipProfileTest(unittest.TestCase):
    def test_build_and_parse_invite_with_l16_sdp(self) -> None:
        body = sdp.build_offer(
            "192.168.1.20",
            "192.168.1.20",
            40000,
            [audio_format.AudioFormat(48000, "s16le", 1, 10)],
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

    def test_standard_offer_does_not_include_trunk_codecs_by_default(self) -> None:
        body = sdp.build_offer_directional(
            "192.168.1.20",
            "192.168.1.20",
            40000,
            [
                audio_format.AudioFormat(48000, "s16le", 2, 20),
                audio_format.AudioFormat(8000, "s16le", 1, 20),
            ],
            [
                audio_format.AudioFormat(48000, "s16le", 2, 20),
                audio_format.AudioFormat(8000, "s16le", 1, 20),
            ],
        )
        self.assertNotIn("OPUS/48000/2", body)
        self.assertNotIn("PCMA/8000", body)
        self.assertNotIn("PCMU/8000", body)

    def test_trunk_offer_includes_opus_and_g711_fallbacks(self) -> None:
        body = sdp.build_offer_directional(
            "192.168.1.20",
            "192.168.1.20",
            40000,
            list(audio_format.HA_TRUNK_AUDIO_FORMATS),
            list(audio_format.HA_TRUNK_AUDIO_FORMATS),
            include_common_codecs=True,
        )
        self.assertIn("m=audio 40000 RTP/AVP 98 8 0", body)
        self.assertIn("a=rtpmap:98 OPUS/48000/2", body)
        self.assertIn("a=rtpmap:8 PCMA/8000/1", body)
        self.assertIn("a=rtpmap:0 PCMU/8000/1", body)
        self.assertIn("a=ptime:20", body)

    def test_trunk_opus_answer_negotiates_48k_stereo_20ms(self) -> None:
        answer = (
            "v=0\r\n"
            "o=- 0 0 IN IP4 192.168.1.30\r\n"
            "s=-\r\n"
            "c=IN IP4 192.168.1.30\r\n"
            "t=0 0\r\n"
            "m=audio 41000 RTP/AVP 98\r\n"
            "a=rtpmap:98 opus/48000/2\r\n"
            "a=ptime:20\r\n"
            "a=sendrecv\r\n"
        )
        selected = sdp.negotiate_answer_directional(
            answer,
            list(audio_format.HA_TRUNK_AUDIO_FORMATS),
            list(audio_format.HA_TRUNK_AUDIO_FORMATS),
        )
        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected.send.wire_token(), "pt=98:OPUS/48000/2/20ms")
        self.assertEqual(selected.recv.wire_token(), "pt=98:OPUS/48000/2/20ms")

    def test_sdp_parser_scopes_connection_and_payloads_to_selected_audio(self) -> None:
        offer = (
            "v=0\r\n"
            "o=- 0 0 IN IP4 192.168.1.20\r\n"
            "s=-\r\n"
            "c=IN IP4 192.168.1.21\r\n"
            "t=0 0\r\n"
            "m=audio 41000 RTP/AVP 96\r\n"
            "c=IN IP4 192.168.1.22\r\n"
            "a=rtpmap:96 L16/16000/1\r\n"
            "a=ptime:20\r\n"
            "m=video 42000 RTP/AVP 97\r\n"
            "c=IN IP4 192.168.1.99\r\n"
            "a=rtpmap:97 H264/90000\r\n"
            "m=audio 43000 RTP/AVP 98\r\n"
            "c=IN IP4 192.168.1.98\r\n"
            "a=rtpmap:98 L16/48000/1\r\n"
        )

        parsed = sdp.parse_sdp(offer)
        self.assertEqual(parsed["connection_ip"], "192.168.1.22")
        self.assertEqual(parsed["media_port"], 41000)
        self.assertEqual(parsed["payload_order"], [96])
        self.assertEqual(parsed["rtpmap"], {96: ("L16", 16000, 1)})

    def test_sdp_parser_skips_rejected_audio_and_validates_transport_ranges(self) -> None:
        offer = (
            "v=0\r\n"
            "o=- 0 0 IN IP4 192.168.1.20\r\n"
            "s=-\r\n"
            "c=IN IP4 192.168.1.20\r\n"
            "t=0 0\r\n"
            "m=audio 0 RTP/AVP 96\r\n"
            "a=rtpmap:96 L16/16000/1\r\n"
            "m=audio 41000 RTP/AVP 97\r\n"
            "a=rtpmap:97 L16/48000/1\r\n"
        )
        parsed = sdp.parse_sdp(offer)
        self.assertEqual(parsed["media_port"], 41000)
        self.assertEqual(parsed["payload_order"], [97])

        with self.assertRaises(sdp.SdpError):
            sdp.parse_sdp(offer.replace("41000", "70000"))
        with self.assertRaises(sdp.SdpError):
            sdp.parse_sdp(offer.replace("RTP/AVP 97", "RTP/AVP 128"))

    def test_parser_preserves_unsupported_method_for_sip_response(self) -> None:
        raw = (
            b"REGISTER sip:ha@192.168.1.10 SIP/2.0\r\n"
            b"Content-Length: 0\r\n\r\n"
        )
        parsed = sip.parse_message(raw)
        self.assertEqual(parsed.method, "REGISTER")

    def test_rejects_trailing_bytes_after_content_length(self) -> None:
        raw = (
            b"OPTIONS sip:ha@192.168.1.10 SIP/2.0\r\n"
            b"Content-Length: 0\r\n\r\nx"
        )
        with self.assertRaises(sip.SipError):
            sip.parse_message(raw)

    def test_compact_sip_headers_are_canonicalized(self) -> None:
        raw = (
            b"OPTIONS sip:ha@192.168.1.10 SIP/2.0\r\n"
            b"v: SIP/2.0/UDP 192.168.1.20:5060;branch=z9hG4bKcompact\r\n"
            b"f: <sip:test@192.168.1.20>;tag=remote\r\n"
            b"t: <sip:ha@192.168.1.10>\r\n"
            b"i: compact-call\r\n"
            b"CSeq: 1 OPTIONS\r\n"
            b"l: 0\r\n\r\n"
        )
        parsed = sip.parse_message(raw)
        self.assertEqual(parsed.header("Via"), "SIP/2.0/UDP 192.168.1.20:5060;branch=z9hG4bKcompact")
        self.assertEqual(parsed.header("Call-ID"), "compact-call")
        self.assertEqual(parsed.header("Content-Length"), "0")

    def test_sip_uri_parser_accepts_display_name_address(self) -> None:
        parsed = sip.parse_sip_uri('"Kitchen phone" <sip:kitchen@192.0.2.20:5090;transport=tcp>')
        self.assertEqual(str(parsed), "sip:kitchen@192.0.2.20:5090;transport=tcp")

    def test_parser_rejects_canonical_and_compact_content_length_together(self) -> None:
        raw = b"OPTIONS sip:ha@192.168.1.10 SIP/2.0\r\nContent-Length: 0\r\nl: 0\r\n\r\n"
        with self.assertRaises(sip.SipError):
            sip.parse_message(raw)

    def test_parser_rejects_duplicate_dialog_identity_headers(self) -> None:
        raw = (
            b"OPTIONS sip:ha@192.168.1.10 SIP/2.0\r\n"
            b"Call-ID: first\r\n"
            b"i: second\r\n"
            b"CSeq: 1 OPTIONS\r\n"
            b"Content-Length: 0\r\n\r\n"
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
        self.assertIn("SIP/2.0/UDP 192.168.1.10:5060", parsed.header("Via"))
        self.assertIn(";rport", parsed.header("Via"))

    def test_cancel_reuses_invite_transaction(self) -> None:
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
            call_id="call-cancel",
            local_tag="local",
            cseq=9,
            branch="z9hG4bKinvite",
        )
        client._invite_cseq = 9
        client._pending_request_uri = "sip:Cucina@192.168.1.30:5060"
        client._pending_local_uri = "sip:HA@192.168.1.10:5060"
        client._pending_remote_uri = "sip:Cucina@192.168.1.30:5060"
        client._pending_remote_host = "192.168.1.30"
        client._pending_remote_sip_port = 5060
        client._invite_transaction_active = True
        client._received_provisional = True

        client.cancel()

        raw, addr = client.transport.sent[0]  # type: ignore[union-attr]
        parsed = sip.parse_message(raw)
        self.assertEqual(addr, ("192.168.1.30", 5060))
        self.assertEqual(parsed.method, "CANCEL")
        self.assertEqual(parsed.header("CSeq"), "9 CANCEL")
        self.assertIn("z9hG4bKinvite", parsed.header("Via"))

    def test_tcp_ack_and_bye_use_stream_writer(self) -> None:
        sent = bytearray()
        client = sip_client.SipCallClient(
            local_ip="192.168.1.10",
            local_name="Casa",
            local_sip_port=43123,
            local_rtp_port=41000,
            signaling_transport="TCP",
        )
        client.use_reused_tcp_connection(send=sent.extend, responses=asyncio.Queue(), close=lambda: None)
        client.dialog_ids = sip.SipDialogIds(
            call_id="call-tcp-dialog",
            local_tag="local",
            remote_tag="remote",
            cseq=3,
            branch="z9hG4bKinvite",
        )
        client._invite_cseq = 3
        client.dialog = sip_client.SipDialog(
            target="ESP",
            remote_host="192.168.1.30",
            remote_sip_port=5060,
            remote_rtp_host="192.168.1.30",
            remote_rtp_port=40000,
            local_rtp_port=41000,
            call_id="call-tcp-dialog",
            local_uri="sip:Casa@192.168.1.10:43123",
            remote_uri="sip:ESP@192.168.1.30:5060",
            send_format=sdp.RtpPcmFormat(96, "L16", 16000, 1, 32),
            recv_format=sdp.RtpPcmFormat(96, "L16", 16000, 1, 32),
        )

        client._send_ack(
            "192.168.1.30",
            5060,
            "sip:ESP@192.168.1.30:5060",
            "sip:Casa@192.168.1.10:43123",
            "sip:ESP@192.168.1.30:5060",
        )
        ack = sip.parse_message(bytes(sent))
        self.assertEqual(ack.method, "ACK")
        self.assertIn("SIP/2.0/TCP 192.168.1.10:43123", ack.header("Via"))

        sent.clear()
        client.bye()
        bye = sip.parse_message(bytes(sent))
        self.assertEqual(bye.method, "BYE")
        self.assertIn("SIP/2.0/TCP 192.168.1.10:43123", bye.header("Via"))

    def test_invite_carries_intercom_display_identity_headers(self) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[bytes, tuple[str, int]]] = []

            def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
                self.sent.append((data, addr))

        client = sip_client.SipCallClient(
            local_ip="192.168.1.10",
            local_name="Casa",
            local_sip_port=5060,
            local_rtp_port=41000,
        )
        client.transport = FakeTransport()  # type: ignore[assignment]
        asyncio.run(client.invite(target="Cucina", remote_host="192.168.1.30", remote_sip_port=5060, timeout=0))

        raw, _ = client.transport.sent[0]  # type: ignore[union-attr]
        parsed = sip.parse_message(raw)
        self.assertEqual(parsed.header("X-Voip-Stack-Caller-Name"), "Casa")
        self.assertEqual(parsed.header("X-Voip-Stack-Caller-Route"), "Casa")
        self.assertEqual(parsed.header("X-Voip-Stack-Dest-Name"), "Cucina")

    def test_invite_preserves_registered_contact_request_uri_params(self) -> None:
        sent: list[bytes] = []
        contact_uri = "sip:Zoiper@192.168.1.50:5062;transport=tcp;ob;line=abc123"
        client = sip_client.SipCallClient(
            local_ip="192.168.1.10",
            local_name="Casa",
            local_sip_port=5060,
            local_rtp_port=41000,
            signaling_transport="TCP",
        )
        client.use_reused_tcp_connection(
            send=sent.append,
            responses=asyncio.Queue(),
            close=lambda: None,
        )

        asyncio.run(
            client.invite(
                target="Zoiper",
                remote_host="192.168.1.50",
                remote_sip_port=5062,
                request_uri=contact_uri,
                timeout=0,
            )
        )

        self.assertTrue(sent)
        raw = sent[0].decode()
        self.assertTrue(raw.startswith(f"INVITE {contact_uri} SIP/2.0\r\n"))
        parsed = sip.parse_message(sent[0])
        self.assertEqual(parsed.header("To"), f"<{contact_uri}>")

    def test_tcp_invite_connection_refused_returns_transport_unreachable(self) -> None:
        async def run() -> str:
            server = await asyncio.start_server(lambda _r, _w: None, "127.0.0.1", 0)
            port = server.sockets[0].getsockname()[1]
            server.close()
            await server.wait_closed()
            client = sip_client.SipCallClient(
                local_ip="127.0.0.1",
                local_name="Casa",
                local_sip_port=5060,
                local_rtp_port=41000,
                signaling_transport="TCP",
            )
            return await client.invite(
                target="TestBaresip",
                remote_host="127.0.0.1",
                remote_sip_port=port,
                timeout=0.1,
            )

        result = asyncio.run(run())

        self.assertEqual(result, "transport_unreachable")

    def test_dialog_headers_preserve_transport_port_and_rport(self) -> None:
        headers = sip.dialog_headers(
            request_uri="sip:Cucina@192.168.1.30:5070",
            local_uri="sip:Casa@192.168.1.10:43123",
            remote_uri="sip:Cucina@192.168.1.30:5070",
            dialog=sip.SipDialogIds(call_id="call-via", local_tag="local"),
            method="INVITE",
            contact_uri="sip:Casa@192.168.1.10:43123",
            transport="TCP",
        )
        via = dict(headers)["Via"]
        self.assertEqual(via.split(";", 1)[0], "SIP/2.0/TCP 192.168.1.10:43123")
        self.assertIn(";rport", via)

    def test_parse_via_and_cseq_for_transaction_matching(self) -> None:
        via = sip.parse_via("SIP/2.0/TCP 192.168.1.10:43123;branch=z9hG4bKabc;rport=43123;received=192.168.1.10")
        self.assertEqual(via.transport, "TCP")
        self.assertEqual(via.host, "192.168.1.10")
        self.assertEqual(via.port, 43123)
        self.assertEqual(via.branch, "z9hG4bKabc")
        self.assertEqual(via.rport, 43123)
        self.assertEqual(via.received, "192.168.1.10")

        cseq = sip.parse_cseq("42 INVITE")
        self.assertEqual(cseq.number, 42)
        self.assertEqual(cseq.method, "INVITE")

    def test_auth_challenge_failures_have_explicit_reasons(self) -> None:
        self.assertEqual(sip.sip_failure_reason(401), "auth_required_unsupported")
        self.assertEqual(sip.sip_failure_reason(407), "proxy_auth_required_unsupported")
        self.assertEqual(sip.sip_failure_reason(488), "media_incompatible")

    def test_sip_transport_classifies_terminal_response_reasons(self) -> None:
        sip_transport = _load_sip_transport_with_homeassistant_stubs()
        self.assertEqual(sip_transport.sip_terminal_status("busy"), ("decline", 0, "busy"))
        self.assertEqual(sip_transport.sip_terminal_status("declined"), ("decline", 0, "declined"))
        self.assertEqual(sip_transport.sip_terminal_status("cancelled"), ("decline", 0, "cancelled"))
        self.assertEqual(
            sip_transport.sip_terminal_status("media_incompatible"),
            ("error", 488, "media_incompatible"),
        )
        self.assertEqual(
            sip_transport.sip_terminal_status("auth_required_unsupported"),
            ("error", 401, "auth_required_unsupported"),
        )
        self.assertEqual(
            sip_transport.sip_terminal_status("proxy_auth_required_unsupported"),
            ("error", 407, "proxy_auth_required_unsupported"),
        )
        self.assertEqual(sip_transport.sip_terminal_status("timeout"), ("error", 408, "timeout"))
        self.assertEqual(sip_transport.sip_terminal_status("sip_500"), ("error", 500, "sip_500"))
        self.assertEqual(sip_transport.sip_public_state("sip_500"), "transport_unreachable")
        self.assertEqual(sip_transport.sip_terminal_reason("sip_500"), "sip_500")
        self.assertEqual(
            sip_transport.sip_failure_response("sip_500"),
            (480, "Temporarily Unavailable", "sip_500", "transport_unreachable"),
        )


class SipClientSocketTest(unittest.IsolatedAsyncioTestCase):
    async def test_invite_100_trying_stops_udp_retransmission_without_reporting_ringing(self) -> None:
        client = sip_client.SipCallClient(
            local_ip="127.0.0.1",
            local_name="HA",
            local_sip_port=5060,
            local_rtp_port=41000,
        )
        sends: list[bytes] = []
        read_timeouts: list[float] = []

        async def fake_start() -> None:
            return None

        async def fake_send(raw: bytes, _host: str, _port: int) -> None:
            sends.append(raw)

        async def fake_read(timeout: float):
            read_timeouts.append(timeout)
            if len(read_timeouts) > 1:
                return None
            response = sip.build_response(
                100,
                "Trying",
                [
                    ("Via", f"SIP/2.0/UDP 127.0.0.1:5060;branch={client.dialog_ids.branch}"),
                    ("From", f"<sip:HA@127.0.0.1>;tag={client.dialog_ids.local_tag}"),
                    ("To", "<sip:ESP@127.0.0.2>"),
                    ("Call-ID", client.dialog_ids.call_id),
                    ("CSeq", f"{client._invite_cseq} INVITE"),
                ],
            )
            return sip.parse_message(response), ("127.0.0.2", 5060)

        client.start = fake_start  # type: ignore[method-assign]
        client._send_raw = fake_send  # type: ignore[method-assign]
        client._read_response = fake_read  # type: ignore[method-assign]

        result = await client.invite(
            target="ESP",
            remote_host="127.0.0.2",
            remote_sip_port=5060,
            timeout=2.0,
        )

        self.assertEqual(result, "timeout")
        self.assertEqual(len(sends), 1)
        self.assertGreater(read_timeouts[-1], 1.0)

    async def test_outbound_client_advertises_bound_socket_port(self) -> None:
        client = sip_client.SipCallClient(
            local_ip="127.0.0.1",
            local_name="Casa",
            local_sip_port=5060,
            local_rtp_port=41000,
        )
        try:
            await client.start()
            self.assertNotEqual(client.local_sip_port, 5060)
            self.assertGreater(client.local_sip_port, 0)
        finally:
            await client.close()

    def test_sip_listener_prefers_intercom_display_identity_headers(self) -> None:
        body = sdp.build_offer(
            "192.168.1.47",
            "192.168.1.47",
            40000,
            [audio_format.AudioFormat(16000, "s16le", 1, 32)],
        ).encode()
        raw = sip.build_request(
            "INVITE",
            "sip:Spotpear_Ball_v2@192.168.1.10",
            [
                ("Via", "SIP/2.0/UDP 192.168.1.47:5060;branch=z9hG4bKdisplay"),
                ("From", "<sip:Waveshare_S3_Audio@192.168.1.47>;tag=src"),
                ("To", "<sip:Spotpear_Ball_v2@192.168.1.10>"),
                ("Call-ID", "call-display"),
                ("CSeq", "1 INVITE"),
                ("Contact", "<sip:Waveshare_S3_Audio@192.168.1.47:5060>"),
                ("Content-Type", "application/sdp"),
                ("X-Voip-Stack-Caller-Name", "Waveshare S3 Audio"),
                ("X-Voip-Stack-Dest-Name", "Spotpear Ball v2"),
            ],
            body,
        )
        endpoint = sip_listener.SipUdpEndpoint(
            local_ip="192.168.1.10",
            local_rtp_port=40002,
            supported_formats=[audio_format.AudioFormat(16000, "s16le", 1, 32)],
            on_invite=lambda _: None,  # type: ignore[arg-type]
        )
        invite = endpoint._parse_invite(sip.parse_message(raw), ("192.168.1.47", 5060))
        self.assertIsNotNone(invite)
        assert invite is not None
        self.assertEqual(invite.caller, "Waveshare S3 Audio")
        self.assertEqual(invite.target, "Spotpear Ball v2")

    async def test_listener_replies_405_and_501_for_unsupported_methods(self) -> None:
        sent: list[bytes] = []
        endpoint = sip_listener.SipUdpEndpoint(
            local_ip="192.168.1.10",
            local_rtp_port=40002,
            supported_formats=[audio_format.AudioFormat(16000, "s16le", 1, 32)],
            on_invite=lambda _: None,  # type: ignore[arg-type]
            send_override=lambda data, _addr: sent.append(data),
        )

        register = (
            b"REGISTER sip:Casa@192.168.1.10 SIP/2.0\r\n"
            b"Via: SIP/2.0/UDP 192.168.1.20:5060;branch=z9hG4bKreg;rport\r\n"
            b"From: <sip:ESP@192.168.1.20>;tag=src\r\n"
            b"To: <sip:Casa@192.168.1.10>\r\n"
            b"Call-ID: reg-1\r\n"
            b"CSeq: 1 REGISTER\r\n"
            b"Content-Length: 0\r\n\r\n"
        )
        await endpoint._handle_datagram(register, ("192.168.1.20", 5060))
        self.assertEqual(sip.parse_message(sent[-1]).status_code, 405)
        self.assertIn("INVITE", sip.parse_message(sent[-1]).header("Allow"))

        custom = register.replace(b"REGISTER", b"BREW")
        await endpoint._handle_datagram(custom, ("192.168.1.20", 5060))
        self.assertEqual(sip.parse_message(sent[-1]).status_code, 501)

    async def test_listener_200_ok_invite_includes_contact(self) -> None:
        sent: list[bytes] = []
        fmt = audio_format.AudioFormat(48000, "s16le", 1, 10)
        rtp_fmt = sdp.audio_format_to_rtp(fmt, 96)
        offer = sdp.build_offer("192.168.1.48", "192.168.1.48", 40900, [fmt]).encode()
        answer = sdp.build_answer_directional("192.168.1.10", "192.168.1.10", 40000, rtp_fmt, rtp_fmt)

        async def on_invite(_invite):
            return sip_listener.SipInviteResult(200, "OK", answer_sdp=answer)

        endpoint = sip_listener.SipUdpEndpoint(
            local_ip="192.168.1.10",
            local_rtp_port=40000,
            supported_formats=[fmt],
            on_invite=on_invite,
            send_override=lambda data, _addr: sent.append(data),
            signaling_transport="TCP",
        )
        invite = sip.build_request(
            "INVITE",
            "sip:Casa@192.168.1.10;transport=tcp",
            [
                ("Via", "SIP/2.0/TCP 192.168.1.48:38946;branch=z9hG4bKcontact;rport"),
                ("From", '"Test Baresip" <sip:test@192.168.1.48>;tag=src'),
                ("To", "<sip:Casa@192.168.1.10;transport=tcp>"),
                ("Call-ID", "contact-200-ok"),
                ("CSeq", "1 INVITE"),
                ("Contact", "<sip:test@192.168.1.48:38946;transport=tcp>"),
                ("Content-Type", "application/sdp"),
            ],
            offer,
        )

        await endpoint._handle_datagram(invite, ("192.168.1.48", 38946))

        response = sip.parse_message(sent[-1])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.header("Contact"), "<sip:Casa@192.168.1.10:5060;transport=tcp>")
        self.assertIn("L16/48000/1", response.body.decode())

    async def test_listener_coalesces_invite_retransmits_and_replays_final_response(self) -> None:
        sent: list[bytes] = []
        calls = 0
        started = asyncio.Event()
        release = asyncio.Event()
        fmt = audio_format.AudioFormat(16000, "s16le", 1, 20)
        rtp_fmt = sdp.audio_format_to_rtp(fmt, 96)
        offer = sdp.build_offer("192.168.1.48", "192.168.1.48", 40900, [fmt]).encode()
        answer = sdp.build_answer_directional("192.168.1.10", "192.168.1.10", 40000, rtp_fmt, rtp_fmt)

        async def on_invite(_invite):
            nonlocal calls
            calls += 1
            started.set()
            await release.wait()
            return sip_listener.SipInviteResult(180, "Ringing", defer_final=True)

        endpoint = sip_listener.SipUdpEndpoint(
            local_ip="192.168.1.10",
            local_rtp_port=40000,
            supported_formats=[fmt],
            on_invite=on_invite,
            send_override=lambda data, _addr: sent.append(data),
        )
        invite = sip.build_request(
            "INVITE",
            "sip:Casa@192.168.1.10:5060",
            [
                ("Via", "SIP/2.0/UDP 192.168.1.48:5060;branch=z9hG4bKdedupe"),
                ("From", "<sip:test@192.168.1.48>;tag=remote"),
                ("To", "<sip:Casa@192.168.1.10>"),
                ("Call-ID", "dedupe-call"),
                ("CSeq", "7 INVITE"),
                ("Content-Type", "application/sdp"),
            ],
            offer,
        )
        addr = ("192.168.1.48", 5060)

        first = asyncio.create_task(endpoint._handle_datagram(invite, addr))
        await started.wait()
        await endpoint._handle_datagram(invite, (addr[0], 5090))
        self.assertEqual(calls, 1)
        self.assertEqual([sip.parse_message(raw).status_code for raw in sent[-2:]], [100, 100])

        release.set()
        await first
        await endpoint._handle_datagram(invite, addr)
        self.assertEqual(calls, 1)
        self.assertEqual(sip.parse_message(sent[-1]).status_code, 180)

        self.assertTrue(endpoint.send_final_response("dedupe-call", 200, "OK", answer_sdp=answer))
        await endpoint._handle_datagram(invite, addr)
        replay = sip.parse_message(sent[-1])
        self.assertEqual(replay.status_code, 200)
        self.assertEqual(replay.body.decode(), answer)
        self.assertEqual(calls, 1)

    async def test_listener_response_preserves_complete_via_chain(self) -> None:
        sent: list[bytes] = []
        endpoint = sip_listener.SipUdpEndpoint(
            local_ip="192.168.1.10",
            local_rtp_port=40000,
            supported_formats=[audio_format.AudioFormat(16000, "s16le", 1, 20)],
            on_invite=lambda _: None,  # type: ignore[arg-type]
            send_override=lambda data, _addr: sent.append(data),
        )
        options = sip.build_request(
            "OPTIONS",
            "sip:Casa@192.168.1.10:5060",
            [
                ("Via", "SIP/2.0/UDP 192.168.1.1:5060;branch=z9hG4bKproxy;rport"),
                ("Via", "SIP/2.0/UDP 192.168.1.48:5060;branch=z9hG4bKclient"),
                ("From", "<sip:test@192.168.1.48>;tag=remote"),
                ("To", "<sip:Casa@192.168.1.10>"),
                ("Call-ID", "via-chain-call"),
                ("CSeq", "7 OPTIONS"),
            ],
        )

        await endpoint._handle_datagram(options, ("192.168.1.1", 5090))

        response = sip.parse_message(sent[-1])
        vias = response.header_values("Via")
        self.assertEqual(len(vias), 2)
        self.assertIn("branch=z9hG4bKproxy", vias[0])
        self.assertIn("received=192.168.1.1;rport=5090", vias[0])
        self.assertEqual(vias[1], "SIP/2.0/UDP 192.168.1.48:5060;branch=z9hG4bKclient")

    async def test_listener_replays_negative_invite_final_without_rerouting(self) -> None:
        sent: list[bytes] = []
        calls = 0
        fmt = audio_format.AudioFormat(16000, "s16le", 1, 20)
        offer = sdp.build_offer("192.168.1.48", "192.168.1.48", 40900, [fmt]).encode()

        async def on_invite(_invite):
            nonlocal calls
            calls += 1
            return sip_listener.SipInviteResult(486, "Busy Here", decline_reason="busy")

        endpoint = sip_listener.SipUdpEndpoint(
            local_ip="192.168.1.10",
            local_rtp_port=40000,
            supported_formats=[fmt],
            on_invite=on_invite,
            send_override=lambda data, _addr: sent.append(data),
        )
        invite = sip.build_request(
            "INVITE",
            "sip:Casa@192.168.1.10:5060",
            [
                ("Via", "SIP/2.0/UDP 192.168.1.48:5060;branch=z9hG4bKbusy"),
                ("From", "<sip:test@192.168.1.48>;tag=remote"),
                ("To", "<sip:Casa@192.168.1.10>"),
                ("Call-ID", "busy-replay-call"),
                ("CSeq", "7 INVITE"),
                ("Content-Type", "application/sdp"),
            ],
            offer,
        )

        await endpoint._handle_datagram(invite, ("192.168.1.48", 5060))
        await endpoint._handle_datagram(invite, ("192.168.1.48", 5090))

        self.assertEqual(calls, 1)
        self.assertEqual([sip.parse_message(raw).status_code for raw in sent[-2:]], [486, 486])
        self.assertIn("busy-replay-call", endpoint.completed_invites)
        self.assertNotIn("busy-replay-call", endpoint.pending_invites)

    async def test_listener_final_answer_wins_while_invite_policy_is_awaiting(self) -> None:
        sent: list[bytes] = []
        terminated: list[tuple[str, str]] = []
        started = asyncio.Event()
        release = asyncio.Event()
        fmt = audio_format.AudioFormat(16000, "s16le", 1, 20)
        rtp_fmt = sdp.audio_format_to_rtp(fmt, 96)
        offer = sdp.build_offer("192.168.1.48", "192.168.1.48", 40900, [fmt]).encode()
        answer = sdp.build_answer_directional("192.168.1.10", "192.168.1.10", 40000, rtp_fmt, rtp_fmt)

        async def on_invite(_invite):
            started.set()
            await release.wait()
            return sip_listener.SipInviteResult(180, "Ringing", defer_final=True)

        async def on_terminated(call_id: str, reason: str) -> None:
            terminated.append((call_id, reason))

        endpoint = sip_listener.SipUdpEndpoint(
            local_ip="192.168.1.10",
            local_rtp_port=40000,
            supported_formats=[fmt],
            on_invite=on_invite,
            on_terminated=on_terminated,
            send_override=lambda data, _addr: sent.append(data),
        )
        invite = sip.build_request(
            "INVITE",
            "sip:Casa@192.168.1.10:5060",
            [
                ("Via", "SIP/2.0/UDP 192.168.1.48:5060;branch=z9hG4bKfastanswer"),
                ("From", "<sip:test@192.168.1.48>;tag=remote"),
                ("To", "<sip:Casa@192.168.1.10>"),
                ("Call-ID", "fast-answer-call"),
                ("CSeq", "7 INVITE"),
                ("Content-Type", "application/sdp"),
            ],
            offer,
        )
        task = asyncio.create_task(endpoint._handle_datagram(invite, ("192.168.1.48", 5060)))
        await started.wait()
        self.assertTrue(endpoint.send_final_response("fast-answer-call", 200, "OK", answer_sdp=answer))
        release.set()
        await task

        self.assertFalse(terminated)
        self.assertIn("fast-answer-call", endpoint.active_dialogs)

    async def test_listener_cancel_wins_while_invite_policy_is_awaiting(self) -> None:
        sent: list[bytes] = []
        terminated: list[tuple[str, str]] = []
        started = asyncio.Event()
        release = asyncio.Event()
        fmt = audio_format.AudioFormat(16000, "s16le", 1, 20)
        offer = sdp.build_offer("192.168.1.48", "192.168.1.48", 40900, [fmt]).encode()

        async def on_invite(_invite):
            started.set()
            await release.wait()
            return sip_listener.SipInviteResult(180, "Ringing", defer_final=True)

        async def on_terminated(call_id: str, reason: str) -> None:
            terminated.append((call_id, reason))

        endpoint = sip_listener.SipUdpEndpoint(
            local_ip="192.168.1.10",
            local_rtp_port=40000,
            supported_formats=[fmt],
            on_invite=on_invite,
            on_terminated=on_terminated,
            send_override=lambda data, _addr: sent.append(data),
        )
        headers = [
            ("Via", "SIP/2.0/UDP 192.168.1.48:5060;branch=z9hG4bKcancelwait"),
            ("From", "<sip:test@192.168.1.48>;tag=remote"),
            ("To", "<sip:Casa@192.168.1.10>"),
            ("Call-ID", "cancel-wait-call"),
            ("CSeq", "9 INVITE"),
            ("Content-Type", "application/sdp"),
        ]
        invite = sip.build_request("INVITE", "sip:Casa@192.168.1.10:5060", headers, offer)
        addr = ("192.168.1.48", 5060)
        task = asyncio.create_task(endpoint._handle_datagram(invite, addr))
        await started.wait()
        cancel_headers = [(key, "9 CANCEL" if key == "CSeq" else value) for key, value in headers if key != "Content-Type"]
        cancel = sip.build_request("CANCEL", "sip:Casa@192.168.1.10:5060", cancel_headers, b"")
        await endpoint._handle_datagram(cancel, (addr[0], 5090))
        self.assertEqual([sip.parse_message(raw).status_code for raw in sent[-2:]], [200, 487])
        self.assertNotIn("cancel-wait-call", endpoint.pending_invites)

        release.set()
        await task
        self.assertGreaterEqual(terminated.count(("cancel-wait-call", "cancelled")), 1)
        self.assertNotIn("cancel-wait-call", endpoint.active_dialogs)

    async def test_listener_keeps_cancel_and_bye_transaction_scopes_separate(self) -> None:
        sent: list[bytes] = []
        terminated: list[tuple[str, str]] = []

        async def on_terminated(call_id: str, reason: str) -> None:
            terminated.append((call_id, reason))

        endpoint = sip_listener.SipUdpEndpoint(
            local_ip="192.168.1.10",
            local_rtp_port=40000,
            supported_formats=[audio_format.AudioFormat(16000, "s16le", 1, 20)],
            on_invite=lambda _: None,  # type: ignore[arg-type]
            on_terminated=on_terminated,
            send_override=lambda data, _addr: sent.append(data),
        )
        addr = ("192.168.1.48", 5060)

        def request(method: str, call_id: str, *, cseq: int = 2, branch: str = "z9hG4bKscope") -> bytes:
            return sip.build_request(
                method,
                "sip:Casa@192.168.1.10:5060",
                [
                    ("Via", f"SIP/2.0/UDP 192.168.1.48:5060;branch={branch}"),
                    ("From", "<sip:test@192.168.1.48>;tag=remote"),
                    ("To", "<sip:Casa@192.168.1.10>;tag=local"),
                    ("Call-ID", call_id),
                    ("CSeq", f"{cseq} {method}"),
                ],
                b"",
            )

        invite = sip.parse_message(request("INVITE", "pending-call"))
        active_invite = sip.parse_message(request("INVITE", "active-call"))
        endpoint.pending_invites["pending-call"] = sip_listener._PendingInvite(invite, addr, "local", "UDP")
        endpoint.active_dialogs["active-call"] = sip_listener._ActiveDialog(active_invite, addr, "local", 3, "UDP")

        await endpoint._handle_datagram(request("CANCEL", "active-call"), addr)
        self.assertEqual(sip.parse_message(sent[-1]).status_code, 481)
        self.assertIn("active-call", endpoint.active_dialogs)

        await endpoint._handle_datagram(request("BYE", "pending-call"), addr)
        self.assertEqual(sip.parse_message(sent[-1]).status_code, 481)
        self.assertIn("pending-call", endpoint.pending_invites)

        await endpoint._handle_datagram(request("CANCEL", "pending-call"), ("192.168.1.99", 5060))
        self.assertEqual(sip.parse_message(sent[-1]).status_code, 481)
        self.assertIn("pending-call", endpoint.pending_invites)

        await endpoint._handle_datagram(request("CANCEL", "pending-call", cseq=3), addr)
        self.assertEqual(sip.parse_message(sent[-1]).status_code, 481)
        self.assertIn("pending-call", endpoint.pending_invites)

        await endpoint._handle_datagram(request("CANCEL", "pending-call", branch="z9hG4bKother"), addr)
        self.assertEqual(sip.parse_message(sent[-1]).status_code, 481)
        self.assertIn("pending-call", endpoint.pending_invites)

        translated_addr = (addr[0], 5090)
        await endpoint._handle_datagram(request("CANCEL", "pending-call"), translated_addr)
        self.assertEqual([sip.parse_message(raw).status_code for raw in sent[-2:]], [200, 487])
        self.assertNotIn("pending-call", endpoint.pending_invites)
        await endpoint._handle_datagram(request("CANCEL", "pending-call"), translated_addr)
        self.assertEqual(sip.parse_message(sent[-1]).status_code, 200)

        await endpoint._handle_datagram(request("BYE", "active-call", cseq=3), translated_addr)
        self.assertEqual(sip.parse_message(sent[-1]).status_code, 200)
        self.assertNotIn("active-call", endpoint.active_dialogs)
        await endpoint._handle_datagram(request("BYE", "active-call", cseq=3), translated_addr)
        self.assertEqual(sip.parse_message(sent[-1]).status_code, 200)
        self.assertEqual(terminated, [("pending-call", "cancelled"), ("active-call", "remote_hangup")])

    async def test_listener_accepts_in_dialog_reinvite_without_restarting_route(self) -> None:
        sent: list[bytes] = []
        fmt = audio_format.AudioFormat(16000, "s16le", 1, 20)
        original_offer = sdp.build_offer(
            "192.168.1.48", "192.168.1.48", 40000, [fmt]
        ).encode()
        endpoint = sip_listener.SipUdpEndpoint(
            local_ip="192.168.1.10",
            local_rtp_port=40000,
            supported_formats=[fmt],
            on_invite=lambda _: None,  # type: ignore[arg-type]
            send_override=lambda data, _addr: sent.append(data),
        )
        addr = ("192.168.1.48", 5060)
        original = sip.parse_message(
            sip.build_request(
                "INVITE",
                "sip:Casa@192.168.1.10",
                [
                    ("Via", "SIP/2.0/UDP 192.168.1.48;branch=z9hG4bKinitial"),
                    ("From", "<sip:test@192.168.1.48>;tag=remote"),
                    ("To", "<sip:Casa@192.168.1.10>"),
                    ("Call-ID", "reinvite-call"),
                    ("CSeq", "1 INVITE"),
                    ("Content-Type", "application/sdp"),
                ],
                original_offer,
            )
        )
        endpoint.active_dialogs["reinvite-call"] = sip_listener._ActiveDialog(
            original, addr, "local", 2, "UDP", answer_sdp="v=0\r\n"
        )
        reinvite = sip.build_request(
            "INVITE",
            "sip:Casa@192.168.1.10",
            [
                ("Via", "SIP/2.0/UDP 192.168.1.48;branch=z9hG4bKrefresh"),
                ("From", "<sip:test@192.168.1.48>;tag=remote"),
                ("To", "<sip:Casa@192.168.1.10>;tag=local"),
                ("Call-ID", "reinvite-call"),
                ("CSeq", "2 INVITE"),
                ("Content-Type", "application/sdp"),
            ],
            original_offer,
        )
        await endpoint._handle_datagram(reinvite, addr)
        response = sip.parse_message(sent[-1])
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.body, b"v=0\r\n")
        self.assertIn("reinvite-call", endpoint.active_dialogs)

        compatible_media_change = sip.build_request(
            "INVITE",
            "sip:Casa@192.168.1.10",
            [
                ("Via", "SIP/2.0/UDP 192.168.1.48;branch=z9hG4bKhold"),
                ("From", "<sip:test@192.168.1.48>;tag=remote"),
                ("To", "<sip:Casa@192.168.1.10>;tag=local"),
                ("Call-ID", "reinvite-call"),
                ("CSeq", "3 INVITE"),
                ("Content-Type", "application/sdp"),
            ],
            sdp.build_offer(
                "192.168.1.48", "192.168.1.48", 45000, [fmt]
            ).encode(),
        )
        await endpoint._handle_datagram(compatible_media_change, addr)
        self.assertEqual(sip.parse_message(sent[-1]).status_code, 200)
        self.assertEqual(
            endpoint.active_dialogs["reinvite-call"].request.body,
            compatible_media_change.split(b"\r\n\r\n", 1)[1],
        )

        incompatible_media_change = sip.build_request(
            "INVITE",
            "sip:Casa@192.168.1.10",
            [
                ("Via", "SIP/2.0/UDP 192.168.1.48;branch=z9hG4bKincompatible"),
                ("From", "<sip:test@192.168.1.48>;tag=remote"),
                ("To", "<sip:Casa@192.168.1.10>;tag=local"),
                ("Call-ID", "reinvite-call"),
                ("CSeq", "4 INVITE"),
                ("Content-Type", "application/sdp"),
            ],
            sdp.build_offer(
                "192.168.1.48",
                "192.168.1.48",
                45002,
                [audio_format.AudioFormat(8000, "s16le", 1, 20)],
            ).encode(),
        )
        await endpoint._handle_datagram(incompatible_media_change, addr)
        self.assertEqual(sip.parse_message(sent[-1]).status_code, 488)
        self.assertIn("reinvite-call", endpoint.active_dialogs)

    async def test_listener_rejects_in_dialog_video_transport_change(self) -> None:
        sent: list[bytes] = []
        fmt = audio_format.AudioFormat(16000, "s16le", 1, 20)

        def offer(video_port: int) -> bytes:
            return sdp.build_offer_directional(
                "192.168.1.48",
                "192.168.1.48",
                40000,
                [fmt],
                [fmt],
                video_port=video_port,
                video_format=sdp.DEFAULT_H264_FORMAT,
            ).encode()

        def request(body: bytes, *, cseq: int, branch: str) -> bytes:
            return sip.build_request(
                "INVITE",
                "sip:Casa@192.168.1.10",
                [
                    ("Via", f"SIP/2.0/UDP 192.168.1.48;branch={branch}"),
                    ("From", "<sip:test@192.168.1.48>;tag=remote"),
                    ("To", "<sip:Casa@192.168.1.10>;tag=local"),
                    ("Call-ID", "video-reinvite-call"),
                    ("CSeq", f"{cseq} INVITE"),
                    ("Content-Type", "application/sdp"),
                ],
                body,
            )

        endpoint = sip_listener.SipUdpEndpoint(
            local_ip="192.168.1.10",
            local_rtp_port=40000,
            supported_formats=[fmt],
            on_invite=lambda _: None,  # type: ignore[arg-type]
            send_override=lambda data, _addr: sent.append(data),
            enable_video=True,
        )
        addr = ("192.168.1.48", 5060)
        original = sip.parse_message(
            request(offer(41002), cseq=1, branch="z9hG4bKvideo-initial")
        )
        endpoint.active_dialogs["video-reinvite-call"] = sip_listener._ActiveDialog(
            original,
            addr,
            "local",
            2,
            "UDP",
            answer_sdp="v=0\r\n",
        )

        changed = request(offer(41004), cseq=2, branch="z9hG4bKvideo-change")
        await endpoint._handle_datagram(changed, addr)

        self.assertEqual(sip.parse_message(sent[-1]).status_code, 488)
        self.assertEqual(
            endpoint.active_dialogs["video-reinvite-call"].request.body,
            original.body,
        )

    async def test_listener_delivers_in_dialog_sip_info_dtmf(self) -> None:
        sent: list[bytes] = []
        received: list[str] = []

        async def on_info(request, _addr, _transport) -> None:
            received.append(dtmf.parse_sip_info_digit(request.header("Content-Type"), request.body))

        endpoint = sip_listener.SipUdpEndpoint(
            local_ip="192.168.1.10",
            local_rtp_port=40000,
            supported_formats=[audio_format.AudioFormat(16000, "s16le", 1, 20)],
            on_invite=lambda _: None,  # type: ignore[arg-type]
            on_info=on_info,
            send_override=lambda data, _addr: sent.append(data),
        )
        addr = ("192.168.1.48", 5060)
        original = sip.parse_message(
            sip.build_request(
                "INVITE",
                "sip:Casa@192.168.1.10",
                [
                    ("Via", "SIP/2.0/UDP 192.168.1.48;branch=z9hG4bKinitial"),
                    ("From", "<sip:test@192.168.1.48>;tag=remote"),
                    ("To", "<sip:Casa@192.168.1.10>"),
                    ("Call-ID", "info-call"),
                    ("CSeq", "1 INVITE"),
                ],
            )
        )
        endpoint.active_dialogs["info-call"] = sip_listener._ActiveDialog(original, addr, "local", 2, "UDP")
        info = sip.build_request(
            "INFO",
            "sip:Casa@192.168.1.10",
            [
                ("Via", "SIP/2.0/UDP 192.168.1.48;branch=z9hG4bKinfo"),
                ("From", "<sip:test@192.168.1.48>;tag=remote"),
                ("To", "<sip:Casa@192.168.1.10>;tag=local"),
                ("Call-ID", "info-call"),
                ("CSeq", "2 INFO"),
                ("Content-Type", "application/dtmf-relay"),
            ],
            b"Signal=6\r\nDuration=160\r\n",
        )
        await endpoint._handle_datagram(info, addr)
        self.assertEqual(sip.parse_message(sent[-1]).status_code, 200)
        self.assertEqual(received, ["6"])
        await endpoint._handle_datagram(info, addr)
        self.assertEqual(sip.parse_message(sent[-1]).status_code, 200)
        self.assertEqual(received, ["6"])

    def test_listener_bye_uses_contact_as_target_and_from_as_identity(self) -> None:
        sent: list[bytes] = []
        endpoint = sip_listener.SipUdpEndpoint(
            local_ip="192.168.1.10",
            local_rtp_port=40000,
            supported_formats=[audio_format.AudioFormat(16000, "s16le", 1, 20)],
            on_invite=lambda _: None,  # type: ignore[arg-type]
            send_override=lambda data, _addr: sent.append(data),
        )
        request = sip.parse_message(
            sip.build_request(
                "INVITE",
                "sip:Casa@192.168.1.10:5060",
                [
                    ("Via", "SIP/2.0/UDP 192.168.1.48:5060;branch=z9hG4bKtarget"),
                    ("From", '"Desk" <sip:desk@192.168.1.48>;tag=remote'),
                    ("To", "<sip:Casa@192.168.1.10>"),
                    ("Contact", '"Desk phone" <sip:dialog@192.168.1.48:5090;transport=udp>'),
                    ("Call-ID", "remote-target-call"),
                    ("CSeq", "4 INVITE"),
                ],
                b"",
            )
        )
        endpoint.active_dialogs["remote-target-call"] = sip_listener._ActiveDialog(
            request,
            ("192.168.1.48", 5060),
            "local",
            5,
            "UDP",
        )

        self.assertTrue(endpoint.send_bye("remote-target-call"))
        bye = sip.parse_message(sent[0])
        self.assertEqual(bye.uri, "sip:dialog@192.168.1.48:5090;transport=udp")
        self.assertEqual(bye.header("To"), "<sip:desk@192.168.1.48>;tag=remote")

    def test_decline_reason_header_overrides_generic_status(self) -> None:
        msg = sip.SipMessage(
            status_code=486,
            reason="Busy Here",
            headers=(
                ("Reason", 'X-Voip-Stack;cause=486;text="DND"'),
                ("X-Voip-Stack-Decline-Reason", "DND"),
            ),
        )
        self.assertEqual(sip_client._sip_decline_reason(msg), "DND")

    def test_roster_target_matching_ignores_spaces_and_underscores(self) -> None:
        entries = [
            roster.RosterEntry(
                id="Spotpear Ball v2",
                name="Spotpear Ball v2",
                address="192.168.1.31",
                metadata={"sip_port": 5060},
            ),
            roster.RosterEntry(
                id="Casa",
                name="Casa",
                address="192.168.1.10",
                metadata={"sip_port": 5060},
            ),
        ]
        decision = router.resolve_esp_origin("Spotpear_Ball_v2", entries, "sip:Spotpear_Ball_v2@192.168.1.10:5060")
        self.assertEqual(decision.action, router.RouteAction.DIRECT)
        self.assertIsNotNone(decision.entry)
        assert decision.entry is not None
        self.assertEqual(decision.entry.address, "192.168.1.31")

    def test_esp_roster_entry_with_address_is_direct_even_without_transport_param(self) -> None:
        entries = [
            roster.RosterEntry(
                id="Casa",
                name="Casa",
                address="192.168.1.10",
                metadata={"sip_port": 5060, "sip_transport": "tcp"},
            ),
            roster.RosterEntry(
                id="Cucina",
                name="Cucina",
                address="192.168.1.31",
                metadata={"sip_port": 5060},
            ),
        ]
        decision = router.resolve_esp_origin("Cucina", entries, "sip:Cucina@192.168.1.10:5060;transport=tcp")
        self.assertEqual(decision.action, router.RouteAction.DIRECT)
        self.assertEqual(decision.sip_uri, "sip:Cucina@192.168.1.31")


class SdpPcmProfileTest(unittest.TestCase):
    def test_negotiate_l16_48k(self) -> None:
        offer = sdp.build_offer(
            "192.168.1.20",
            "192.168.1.20",
            40000,
            [
                audio_format.AudioFormat(48000, "s16le", 1, 10),
                audio_format.AudioFormat(16000, "s16le", 1, 20),
            ],
        )
        self.assertNotIn("a=fmtp:96", offer)
        self.assertIn("telephone-event/8000", offer)
        self.assertIn("a=fmtp:97 0-16", offer)
        self.assertIn("a=maxptime:10", offer)
        selected = sdp.negotiate(
            offer,
            [
                audio_format.AudioFormat(16000, "s16le", 1, 20),
                audio_format.AudioFormat(48000, "s16le", 1, 10),
            ],
        )
        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected.encoding, "L16")
        self.assertEqual(selected.sample_rate, 48000)
        self.assertEqual(selected.payload_type, 96)

    def test_negotiate_missing_ptime_prefers_best_local_pcm(self) -> None:
        offer = (
            "v=0\r\n"
            "o=- 0 0 IN IP4 192.168.1.48\r\n"
            "s=pjmedia\r\n"
            "c=IN IP4 192.168.1.48\r\n"
            "t=0 0\r\n"
            "m=audio 40760 RTP/AVP 96 97 98\r\n"
            "a=rtpmap:96 L16/16000\r\n"
            "a=rtpmap:97 L16/48000\r\n"
            "a=rtpmap:98 opus/48000/2\r\n"
            "a=sendrecv\r\n"
        )
        selected = sdp.negotiate(
            offer,
            [
                audio_format.AudioFormat(48000, "s16le", 1, 10),
                audio_format.AudioFormat(16000, "s16le", 1, 20),
            ],
        )
        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected.payload_type, 97)
        self.assertEqual(selected.audio_format, audio_format.AudioFormat(48000, "s16le", 1, 10))

        answer = sdp.build_answer("192.168.1.10", "192.168.1.10", 40000, selected)
        self.assertIn("m=audio 40000 RTP/AVP 97", answer)
        self.assertIn("a=rtpmap:97 L16/48000/1", answer)
        self.assertIn("a=ptime:10", answer)

    def test_negotiate_l24_from_s24(self) -> None:
        offer = sdp.build_offer(
            "192.168.1.20",
            "192.168.1.20",
            40000,
            [audio_format.AudioFormat(16000, "s24le", 1, 20)],
        )
        offered = sdp.offered_pcm_formats(offer)
        self.assertEqual(offered[0].encoding, "L24")
        self.assertEqual(offered[0].sample_rate, 16000)

    def test_directional_negotiation_allows_asymmetric_pcm_rates_per_direction(self) -> None:
        ha_to_esp = audio_format.AudioFormat(48000, "s16le", 1, 10)
        esp_to_ha = audio_format.AudioFormat(16000, "s16le", 1, 10)
        offer = sdp.build_offer_directional(
            "192.168.1.10",
            "192.168.1.10",
            40020,
            [ha_to_esp],
            [esp_to_ha],
        )
        selected_by_esp = sdp.negotiate_directional(
            offer,
            [esp_to_ha],
            [ha_to_esp],
        )
        self.assertIsNotNone(selected_by_esp)
        assert selected_by_esp is not None
        self.assertEqual(selected_by_esp.send.audio_format, esp_to_ha)
        self.assertEqual(selected_by_esp.recv.audio_format, ha_to_esp)
        self.assertNotEqual(selected_by_esp.send.payload_type, selected_by_esp.recv.payload_type)

        answer = sdp.build_answer_directional(
            "192.168.1.47",
            "192.168.1.47",
            40000,
            selected_by_esp.send,
            selected_by_esp.recv,
        )
        selected_by_ha = sdp.negotiate_answer_directional(
            answer,
            [ha_to_esp],
            [esp_to_ha],
        )
        self.assertIsNotNone(selected_by_ha)
        assert selected_by_ha is not None
        self.assertEqual(selected_by_ha.send.audio_format, ha_to_esp)
        self.assertEqual(selected_by_ha.recv.audio_format, esp_to_ha)
        self.assertIn("L16/48000/1", answer)
        self.assertIn("L16/16000/1", answer)
        self.assertNotIn("a=fmtp:", answer)
        self.assertIn("a=maxptime:10", answer)

    def test_directional_offer_requires_common_ptime(self) -> None:
        with self.assertRaises(sdp.SdpError):
            sdp.build_offer_directional(
                "192.168.1.10",
                "192.168.1.10",
                40020,
                [audio_format.AudioFormat(16000, "s16le", 1, 16)],
                [audio_format.AudioFormat(48000, "s16le", 1, 10)],
            )

    def test_answer_negotiation_preserves_asymmetric_payload_direction(self) -> None:
        ha_to_esp = audio_format.AudioFormat(48000, "s16le", 1, 10)
        esp_to_ha = audio_format.AudioFormat(16000, "s16le", 1, 10)
        answer = (
            "v=0\r\n"
            "o=- 0 0 IN IP4 192.168.1.47\r\n"
            "s=VoIP Stack\r\n"
            "c=IN IP4 192.168.1.47\r\n"
            "t=0 0\r\n"
            "m=audio 40000 RTP/AVP 96 97\r\n"
            "a=rtpmap:96 L16/48000/1\r\n"
            "a=rtpmap:97 L16/16000/1\r\n"
            "a=ptime:10\r\n"
            "a=maxptime:10\r\n"
            "a=sendrecv\r\n"
        )
        selected = sdp.negotiate_answer_directional(
            answer,
            [ha_to_esp],
            [esp_to_ha],
        )
        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected.send.payload_type, 96)
        self.assertEqual(selected.send.audio_format, ha_to_esp)
        self.assertEqual(selected.recv.payload_type, 97)
        self.assertEqual(selected.recv.audio_format, esp_to_ha)

    def test_standard_softphone_answer_uses_one_common_payload_when_profiles_are_symmetric(self) -> None:
        answer = (
            "v=0\r\n"
            "o=- 0 0 IN IP4 192.168.1.48\r\n"
            "s=baresip\r\n"
            "c=IN IP4 192.168.1.48\r\n"
            "t=0 0\r\n"
            "m=audio 45686 RTP/AVP 96 98\r\n"
            "a=rtpmap:96 L16/48000\r\n"
            "a=rtpmap:98 L16/16000\r\n"
            "a=minptime:10\r\n"
            "a=ptime:10\r\n"
            "a=sendrecv\r\n"
        )
        selected = sdp.negotiate_answer_directional(
            answer,
            list(audio_format.HA_SIP_PCM_FORMATS),
            list(audio_format.HA_SIP_PCM_FORMATS),
        )
        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected.send.payload_type, 96)
        self.assertEqual(selected.recv.payload_type, 96)
        self.assertEqual(selected.send.audio_format, audio_format.AudioFormat(48000, "s16le", 1, 10))
        self.assertEqual(selected.recv.audio_format, audio_format.AudioFormat(48000, "s16le", 1, 10))

    def test_rejects_oversized_pcm_rtp_frame(self) -> None:
        with self.assertRaises(sdp.SdpError):
            sdp.build_offer(
                "192.168.1.20",
                "192.168.1.20",
                40000,
                [audio_format.AudioFormat(48000, "s16le", 1, 20)],
            )

    def test_accepts_g711_trunk_offer_as_pcm_edge_codec(self) -> None:
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
            "a=ptime:20\r\n"
        )
        selected = sdp.negotiate(offer, [audio_format.AudioFormat(8000, "s16le", 1, 20)])
        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected.encoding, "PCMU")
        self.assertEqual(selected.payload_type, 0)
        self.assertEqual(selected.audio_format, audio_format.AudioFormat(8000, "s16le", 1, 20))

    def test_prefers_l16_48k_over_g711_when_both_are_offered(self) -> None:
        offer = (
            "v=0\r\n"
            "o=- 0 0 IN IP4 192.168.1.20\r\n"
            "s=Phone\r\n"
            "c=IN IP4 192.168.1.20\r\n"
            "t=0 0\r\n"
            "m=audio 40000 RTP/AVP 0 96 8\r\n"
            "a=rtpmap:96 L16/48000/1\r\n"
            "a=ptime:10\r\n"
        )
        selected = sdp.negotiate(
            offer,
            [
                audio_format.AudioFormat(48000, "s16le", 1, 10),
                audio_format.AudioFormat(8000, "s16le", 1, 20),
            ],
        )
        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected.encoding, "L16")
        self.assertEqual(selected.sample_rate, 48000)

    def test_negotiate_48k_10ms_when_softphone_offers_20ms_with_minptime_10(self) -> None:
        offer = (
            "v=0\r\n"
            "o=- 0 0 IN IP4 192.168.1.48\r\n"
            "s=baresip\r\n"
            "c=IN IP4 192.168.1.48\r\n"
            "t=0 0\r\n"
            "m=audio 12456 RTP/AVP 96 97 8 0 101\r\n"
            "a=rtpmap:96 L16/48000\r\n"
            "a=rtpmap:97 L16/16000\r\n"
            "a=rtpmap:8 PCMA/8000\r\n"
            "a=rtpmap:0 PCMU/8000\r\n"
            "a=rtpmap:101 telephone-event/8000\r\n"
            "a=fmtp:101 0-15\r\n"
            "a=sendrecv\r\n"
            "a=minptime:10\r\n"
            "a=ptime:20\r\n"
        )
        selected = sdp.negotiate_directional(
            offer,
            list(audio_format.HA_SIP_PCM_FORMATS),
            list(audio_format.HA_SIP_PCM_FORMATS),
        )
        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected.send.audio_format, audio_format.AudioFormat(48000, "s16le", 1, 10))
        self.assertEqual(selected.recv.audio_format, audio_format.AudioFormat(48000, "s16le", 1, 10))
        answer = sdp.build_answer_directional("192.168.1.10", "192.168.1.10", 40000, selected.send, selected.recv)
        self.assertIn("a=rtpmap:96 L16/48000/1", answer)
        self.assertIn("a=ptime:10", answer)

    def test_g711_rtp_payload_converts_to_internal_s16le(self) -> None:
        pcm = b"\x00\x00\x00\x10\x00\xf0"
        alaw = sip_client.pcm_to_rtp_payload(pcm, sdp.RtpPcmFormat(8, "PCMA", 8000, 1, 20))
        ulaw = sip_client.pcm_to_rtp_payload(pcm, sdp.RtpPcmFormat(0, "PCMU", 8000, 1, 20))
        self.assertEqual(len(alaw), 3)
        self.assertEqual(len(ulaw), 3)
        self.assertEqual(len(sip_client.rtp_payload_to_pcm(alaw, sdp.RtpPcmFormat(8, "PCMA", 8000, 1, 20))), len(pcm))
        self.assertEqual(len(sip_client.rtp_payload_to_pcm(ulaw, sdp.RtpPcmFormat(0, "PCMU", 8000, 1, 20))), len(pcm))

    def test_linear_pcm_rtp_endianness_round_trips_exactly(self) -> None:
        vectors = (
            (audio_format.AudioFormat(16000, "s16le", 1, 20), b"\x01\x02\xfe\xff", b"\x02\x01\xff\xfe"),
            (
                audio_format.AudioFormat(16000, "s24le", 1, 20),
                b"\x01\x02\x03\xfe\xfd\xfc",
                b"\x03\x02\x01\xfc\xfd\xfe",
            ),
            (
                audio_format.AudioFormat(16000, "s24le_in_s32", 1, 20),
                b"\x56\x34\x12\x00\x01\x00\x80\xff",
                b"\x12\x34\x56\x80\x00\x01",
            ),
        )
        for fmt, pcm, wire in vectors:
            with self.subTest(fmt=fmt.pcm_format):
                self.assertEqual(sip_client.pcm_to_rtp_payload(pcm, fmt), wire)
                self.assertEqual(sip_client.rtp_payload_to_pcm(wire, fmt), pcm)

    def test_s24_in_s32_bridge_conversion_is_right_aligned(self) -> None:
        s24 = audio_format.AudioFormat(16000, "s24le_in_s32", 1, 10)
        s16 = audio_format.AudioFormat(16000, "s16le", 1, 10)
        s24_pair = b"\x00\x00\x40\x00\x00\x00\xc0\xff"
        source = s24_pair * (s24.nominal_frame_samples // 2)

        converted = audio_pcm.PcmFrameConverter(s24, s16).convert(source)
        self.assertEqual(len(converted), 1)
        self.assertEqual(converted[0][:4], b"\x00\x40\x00\xc0")

        restored = audio_pcm.PcmFrameConverter(s16, s24).convert(converted[0])
        self.assertEqual(len(restored), 1)
        self.assertEqual(restored[0][:8], s24_pair)

    def test_bounded_sip_udp_queue_keeps_freshest_datagram(self) -> None:
        queue: asyncio.Queue[tuple[bytes, tuple[str, int]]] = asyncio.Queue(maxsize=2)
        protocol = sip_client._SipClientProtocol(queue)
        protocol.datagram_received(b"one", ("127.0.0.1", 1))
        protocol.datagram_received(b"two", ("127.0.0.1", 2))
        protocol.datagram_received(b"three", ("127.0.0.1", 3))

        self.assertEqual(protocol.dropped_packets, 1)
        self.assertEqual(queue.get_nowait()[0], b"two")
        self.assertEqual(queue.get_nowait()[0], b"three")

    def test_rejects_s32_wire_mapping(self) -> None:
        with self.assertRaises(sdp.SdpError):
            sdp.audio_format_to_rtp(audio_format.AudioFormat(48000, "s32le", 1, 20), 96)

    def test_offer_filters_non_rtp_mappable_pcm_formats(self) -> None:
        offer = sdp.build_offer_directional(
            "192.168.1.10",
            "192.168.1.10",
            40020,
            [
                audio_format.AudioFormat(48000, "s32le", 1, 10),
                audio_format.AudioFormat(48000, "s16le", 1, 10),
                audio_format.AudioFormat(16000, "s16le", 1, 10),
            ],
            [
                audio_format.AudioFormat(48000, "s16le", 1, 10),
                audio_format.AudioFormat(48000, "s16le", 2, 10),
                audio_format.AudioFormat(16000, "s16le", 1, 10),
            ],
        )
        offered = sdp.offered_pcm_formats(offer)
        self.assertEqual(
            [(fmt.encoding, fmt.sample_rate, fmt.channels, fmt.frame_ms) for fmt in offered],
            [("L16", 48000, 1, 10), ("L16", 16000, 1, 10)],
        )

    def test_offer_caps_payloads_to_compact_udp_safe_profile_without_losing_esp_baseline(self) -> None:
        browser_tx_formats = [
            audio_format.AudioFormat(rate, fmt, 1, frame_ms)
            for rate in sorted(audio_format.SUPPORTED_SAMPLE_RATES)
            for frame_ms in sorted(audio_format.SUPPORTED_FRAME_MS)
            if (rate * frame_ms) % 1000 == 0
            for fmt in audio_format.PcmFormat
        ]
        browser_rx_formats = [
            audio_format.AudioFormat(rate, fmt, channels, frame_ms)
            for rate in sorted(audio_format.SUPPORTED_SAMPLE_RATES)
            for frame_ms in sorted(audio_format.SUPPORTED_FRAME_MS)
            if (rate * frame_ms) % 1000 == 0
            for fmt in audio_format.PcmFormat
            for channels in (1, 2)
        ]
        offer = sdp.build_offer_directional(
            "192.168.1.10",
            "192.168.1.10",
            40020,
            browser_tx_formats,
            browser_rx_formats,
        )
        offered = sdp.offered_pcm_formats(offer)
        self.assertLessEqual(len(offered), 12)
        self.assertLess(len(offer.encode()), 900)
        self.assertEqual(offered[0].audio_format, audio_format.AudioFormat(48000, "s16le", 1, 10))
        self.assertIn(
            audio_format.AudioFormat(16000, "s16le", 1, 10),
            [fmt.audio_format for fmt in offered],
        )

    def test_ha_sip_profile_rejects_browser_only_sample_rates(self) -> None:
        offer = sdp.build_offer_directional(
            "192.168.1.48",
            "192.168.1.48",
            40020,
            [audio_format.AudioFormat(44100, "s16le", 1, 10)],
            [audio_format.AudioFormat(44100, "s16le", 1, 10)],
        )
        selected = sdp.negotiate_directional(
            offer,
            list(audio_format.HA_SIP_PCM_TX_FORMATS),
            list(audio_format.HA_SIP_PCM_RX_FORMATS),
        )
        self.assertIsNone(selected)

    def test_ha_sip_profile_keeps_esp_baseline_16k_16ms(self) -> None:
        baseline = audio_format.AudioFormat(16000, "s16le", 1, 16)
        offer = sdp.build_offer_directional(
            "192.168.1.48",
            "192.168.1.48",
            40020,
            [baseline],
            [baseline],
        )
        selected = sdp.negotiate_directional(
            offer,
            list(audio_format.HA_SIP_PCM_TX_FORMATS),
            list(audio_format.HA_SIP_PCM_RX_FORMATS),
        )
        self.assertIsNotNone(selected)
        self.assertEqual(selected.send.audio_format, baseline)


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

    def test_rfc4733_decoder_emits_one_event_per_press(self) -> None:
        decoder = dtmf.RtpDtmfDecoder(101)

        def event(sequence: int, timestamp: int, code: int, *, ssrc: int = 0x1234, ended: bool = False) -> bytes:
            return rtp.build_packet(
                rtp.RtpPacket(
                    payload_type=101,
                    sequence=sequence,
                    timestamp=timestamp,
                    ssrc=ssrc,
                    payload=bytes((code, 0x80 if ended else 0x00, 0, 160)),
                )
            )

        self.assertEqual(decoder.decode(event(1, 1000, 1)), "1")
        self.assertEqual(decoder.decode(event(2, 1000, 1, ended=True)), "")
        self.assertEqual(decoder.decode(event(3, 2000, 10)), "*")
        self.assertEqual(decoder.decode(event(4, 3000, 12)), "A")
        self.assertEqual(decoder.decode(event(5, 4000, 2, ssrc=0x9999)), "")

    def test_legacy_sip_info_accepts_digit_and_event_code_forms(self) -> None:
        self.assertEqual(dtmf.parse_sip_info_digit("application/dtmf-relay", b"Signal=1\r\nDuration=160"), "1")
        self.assertEqual(dtmf.parse_sip_info_digit("application/dtmf-relay", b"Signal=10\r\nDuration=160"), "*")
        self.assertEqual(dtmf.parse_sip_info_digit("application/dtmf", b"#"), "#")

    def test_relay_follows_same_ssrc_nat_port_rebind(self) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[bytes, tuple[str, int]]] = []

            def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
                self.sent.append((data, addr))

        fmt = audio_format.AudioFormat(16000, "s16le", 1, 16)
        left = sip_rtp_bridge.RtpPeer("192.0.2.10", 40000, 96, fmt)
        right = sip_rtp_bridge.RtpPeer("192.0.2.20", 41000, 96, fmt)
        relay = sip_rtp_bridge.SipRtpRelay(left=left, right=right, left_port=42000, right_port=42002)
        output = FakeTransport()
        relay.right_transport = output  # type: ignore[assignment]

        def packet(ssrc: int) -> bytes:
            return rtp.build_packet(
                rtp.RtpPacket(
                    payload_type=96,
                    sequence=1,
                    timestamp=1,
                    ssrc=ssrc,
                    payload=b"\0" * fmt.nominal_frame_bytes,
                )
            )

        relay.handle_packet("left", packet(0x1234), (left.host, 45000))
        self.assertEqual(left.port, 45000)
        relay.handle_packet("left", packet(0x1234), (left.host, 45002))
        self.assertEqual(left.port, 45002)
        relay.handle_packet("left", packet(0x9999), (left.host, 45004))
        self.assertEqual(left.port, 45002)
        self.assertEqual(len(output.sent), 2)


class RosterResolverTest(unittest.TestCase):
    def test_route_decisions(self) -> None:
        entries = roster.parse_roster_json(
            {
                "contacts": [
                    {"id": "HA", "address": "192.168.1.10"},
                    {"id": "Cucina", "address": "192.168.1.30"},
                    {
                        "id": "Studio",
                        "address": "192.168.1.31",
                        "metadata": {"sip_transport": "tcp"},
                    },
                    {"id": "Corridoio"},
                    {"id": "Nonna", "number": "0574863562"},
                ]
            }
        )
        ha_uri = "sip:Home@192.168.1.10;transport=tcp"
        cucina = router.resolve_esp_origin("Cucina", entries, ha_uri)
        self.assertEqual(cucina.action, router.RouteAction.DIRECT)
        self.assertEqual(cucina.sip_uri, "sip:Cucina@192.168.1.30")

        studio = router.resolve_esp_origin("Studio", entries, ha_uri)
        self.assertEqual(studio.action, router.RouteAction.DIRECT)
        self.assertEqual(studio.sip_uri, "sip:Studio@192.168.1.31;transport=tcp")

        corridoio = router.resolve_esp_origin("Corridoio", entries, ha_uri)
        self.assertEqual(corridoio.action, router.RouteAction.BRIDGE)
        self.assertEqual(corridoio.sip_uri, "sip:Corridoio@192.168.1.10;transport=tcp")

        phone_from_esp = router.resolve_esp_origin("Nonna", entries, ha_uri)
        self.assertEqual(phone_from_esp.action, router.RouteAction.BRIDGE)
        self.assertEqual(phone_from_esp.target, "Nonna")

        phone_from_ha = router.resolve_ha_router("Nonna", entries, trunk_ready=True)
        self.assertEqual(phone_from_ha.action, router.RouteAction.TRUNK)
        self.assertEqual(phone_from_ha.target, "0574863562")

    def test_explicit_sip_uri_and_name_at_ip(self) -> None:
        entries = roster.parse_roster_json([{"id": "HA", "address": "192.168.1.10"}])
        self.assertEqual(
            router.resolve_esp_origin("sip:Cucina@192.168.1.30", entries, "sip:Home@192.168.1.10").sip_uri,
            "sip:Cucina@192.168.1.30",
        )
        self.assertEqual(
            router.resolve_esp_origin("Cucina@192.168.1.30", entries, "sip:Home@192.168.1.10").sip_uri,
            "sip:Cucina@192.168.1.30",
        )

    def test_sip_transport_is_separate_from_endpoint_transport(self) -> None:
        entries = roster.parse_roster_json(
            {
                "contacts": [
                    {
                        "id": "Casa",
                        "address": "192.168.1.10",
                        "metadata": {"sip_transport": "tcp", "sip_port": 5060},
                    },
                    {
                        "id": "Cucina",
                        "address": "192.168.1.30",
                        "metadata": {"sip_transport": "tcp", "sip_port": 5060},
                    },
                    {
                        "id": "Salotto",
                        "address": "192.168.1.31",
                        "metadata": {"sip_transport": "udp", "sip_port": 5060},
                    },
                ]
            }
        )
        self.assertEqual(
            router.resolve_esp_origin("Cucina", entries, "sip:Casa@192.168.1.10;transport=tcp").sip_uri,
            "sip:Cucina@192.168.1.30;transport=tcp",
        )
        self.assertEqual(
            router.resolve_esp_origin("Salotto", entries, "sip:Casa@192.168.1.10;transport=tcp").sip_uri,
            "sip:Salotto@192.168.1.31;transport=udp",
        )
        bridged_entries = [
            entries[0],
            entries[1],
            roster.RosterEntry(
                id="Salotto",
                address="192.168.1.31",
                ha_bridge=True,
                metadata={"sip_transport": "udp", "sip_port": 5060},
            ),
        ]
        self.assertEqual(
            router.resolve_esp_origin(
                "Salotto",
                bridged_entries,
                "sip:Casa@192.168.1.10;transport=tcp",
            ).sip_uri,
            "sip:Salotto@192.168.1.10;transport=tcp",
        )


class RouterContractTest(unittest.TestCase):
    def _matrix_entries(self):
        return roster.parse_roster_json(
            [
                {
                    "id": "Casa",
                    "name": "Casa",
                    "address": "192.168.1.10",
                    "metadata": {"local_ha": True, "sip_transport": "tcp", "sip_port": 5060},
                },
                {
                    "id": "Spotpear",
                    "name": "Spotpear",
                    "address": "192.168.1.31",
                    "extension": "101",
                    "metadata": {"sip_transport": "udp", "sip_port": 5060},
                },
                {
                    "id": "WS3",
                    "name": "WS3",
                    "address": "192.168.1.47",
                    "extension": "102",
                    "metadata": {"sip_transport": "udp", "sip_port": 5060},
                },
                {
                    "id": "Zoiper",
                    "name": "Zoiper",
                    "sip_uri": "sip:Zoiper@192.168.1.17:57029;transport=tcp",
                    "extension": "201",
                    "metadata": {"registered": True, "sip_transport": "tcp"},
                },
                {"id": "Daniele", "name": "Daniele", "number": "3510000000"},
            ]
        )

    def test_dialplan_matrix_core_routes(self) -> None:
        entries = self._matrix_entries()
        ha_uri = "sip:Casa@192.168.1.10:5060;transport=tcp"

        cases = [
            (
                "ESP calls HA by name",
                router.resolve_esp_origin("Casa", entries, ha_uri),
                router.RouteAction.DIRECT,
                "sip:Casa@192.168.1.10;transport=tcp",
                "Casa",
            ),
            (
                "ESP calls external contact by name through HA",
                router.resolve_esp_origin("Daniele", entries, ha_uri),
                router.RouteAction.BRIDGE,
                "sip:Daniele@192.168.1.10;transport=tcp",
                "Daniele",
            ),
            (
                "ESP dials internal extension through HA",
                router.resolve_esp_origin("101", entries, ha_uri),
                router.RouteAction.BRIDGE,
                "sip:101@192.168.1.10;transport=tcp",
                "101",
            ),
            (
                "ESP calls another ESP by name direct",
                router.resolve_esp_origin("WS3", entries, ha_uri),
                router.RouteAction.DIRECT,
                "sip:WS3@192.168.1.47;transport=udp",
                "WS3",
            ),
            (
                "HA calls ESP by name",
                router.resolve_ha_router("Spotpear", entries, trunk_ready=True),
                router.RouteAction.FORWARD,
                "sip:Spotpear@192.168.1.31;transport=udp",
                "Spotpear",
            ),
            (
                "HA calls ESP by extension",
                router.resolve_ha_router("101", entries, trunk_ready=True),
                router.RouteAction.FORWARD,
                "sip:Spotpear@192.168.1.31;transport=udp",
                "Spotpear",
            ),
            (
                "HA calls registered softphone",
                router.resolve_ha_router("Zoiper", entries, trunk_ready=True),
                router.RouteAction.FORWARD,
                "sip:Zoiper@192.168.1.17:57029;transport=tcp",
                "Zoiper",
            ),
            (
                "HA calls external contact number via trunk",
                router.resolve_ha_router("Daniele", entries, trunk_ready=True),
                router.RouteAction.TRUNK,
                "",
                "3510000000",
            ),
            (
                "HA calls raw external number via trunk",
                router.resolve_ha_router("3510000000", entries, trunk_ready=True),
                router.RouteAction.TRUNK,
                "",
                "3510000000",
            ),
        ]
        for label, decision, action, sip_uri, target in cases:
            with self.subTest(label):
                self.assertEqual(decision.action, action)
                self.assertEqual(decision.target, target)
                if sip_uri:
                    self.assertEqual(decision.sip_uri, sip_uri)

    def test_dialplan_matrix_inbound_trunk_and_failures(self) -> None:
        entries = self._matrix_entries()
        no_hint = router.route_inbound_trunk(
            router.CallContext(call_id="in-1", direction="inbound", origin="trunk"),
            entries,
            trunk_ready=True,
        )
        self.assertEqual(no_hint.action, router.RouteAction.ANSWER_HA)

        extension_hint = router.route_inbound_trunk(
            router.CallContext(
                call_id="in-2",
                direction="inbound",
                origin="trunk",
                route_hint="101",
            ),
            entries,
            trunk_ready=True,
        )
        self.assertEqual(extension_hint.action, router.RouteAction.FORWARD)
        self.assertEqual(extension_hint.target, "Spotpear")

        external_number_hint = router.route_inbound_trunk(
            router.CallContext(
                call_id="in-3",
                direction="inbound",
                origin="trunk",
                route_hint="3510000000",
            ),
            entries,
            trunk_ready=True,
        )
        self.assertEqual(external_number_hint.action, router.RouteAction.REJECT)
        self.assertEqual(external_number_hint.reason, router.RouteReason.ROUTE_NOT_FOUND)

        missing_trunk = router.resolve_ha_router("Daniele", entries, trunk_ready=False)
        self.assertEqual(missing_trunk.action, router.RouteAction.REJECT)
        self.assertEqual(missing_trunk.reason, router.RouteReason.TRUNK_UNAVAILABLE)

    def test_esp_numeric_target_always_bridges_to_ha(self) -> None:
        entries = roster.parse_roster_json(
            [
                {"id": "HA", "address": "192.168.1.10"},
                {"id": "200", "address": "192.168.1.20", "metadata": {"sip_transport": "udp"}},
            ]
        )
        decision = router.resolve_esp_origin("200", entries, "sip:200@192.168.1.10;transport=tcp")
        self.assertEqual(decision.action, router.RouteAction.BRIDGE)
        self.assertEqual(decision.reason, router.RouteReason.NUMBER_VIA_HA)
        self.assertEqual(decision.sip_uri, "sip:200@192.168.1.10;transport=tcp")

    def test_ha_router_extension_forwards_to_esp(self) -> None:
        entries = roster.parse_roster_json(
            [{"id": "200", "name": "WS3", "address": "192.168.1.47", "metadata": {"sip_transport": "udp"}}]
        )
        decision = router.resolve_ha_router("200", entries, trunk_ready=False)
        self.assertEqual(decision.action, router.RouteAction.FORWARD)
        self.assertEqual(decision.target, "200")
        self.assertEqual(decision.sip_uri, "sip:200@192.168.1.47;transport=udp")

    def test_ha_router_extension_alias_forwards_to_esp(self) -> None:
        entries = roster.parse_roster_json(
            [
                {
                    "id": "Spotpear",
                    "name": "Spotpear Ball v2",
                    "address": "192.168.1.31",
                    "extension": "200",
                    "metadata": {"sip_transport": "udp"},
                }
            ]
        )
        decision = router.resolve_ha_router("200", entries, trunk_ready=False)
        self.assertEqual(decision.action, router.RouteAction.FORWARD)
        self.assertEqual(decision.target, "Spotpear")
        self.assertEqual(decision.sip_uri, "sip:Spotpear@192.168.1.31;transport=udp")

    def test_manual_phonebook_number_overrides_discovered_endpoint_without_duplicate(self) -> None:
        discovered = roster.parse_roster_json(
            [
                {
                    "id": "Spotpear",
                    "name": "Spotpear Ball v2",
                    "address": "192.168.1.31",
                    "metadata": {"sip_transport": "udp", "sip_port": 5060},
                }
            ]
        )
        manual = roster.parse_roster_json(
            [{"id": "Spotpear", "name": "Spotpear Ball v2", "number": "200"}]
        )
        merged = roster.merge_roster_overrides(discovered, manual)
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].address, "192.168.1.31")
        self.assertEqual(merged[0].number, "200")
        self.assertEqual(merged[0].metadata["sip_transport"], "udp")

    def test_ha_router_decodes_sip_uri_user_for_phonebook_lookup(self) -> None:
        entries = roster.parse_roster_json(
            [
                {
                    "id": "Waveshare S3 Audio",
                    "address": "192.168.1.47",
                    "metadata": {"sip_transport": "udp"},
                }
            ]
        )
        decision = router.resolve_ha_router("Waveshare%20S3%20Audio", entries, trunk_ready=False)
        self.assertEqual(decision.action, router.RouteAction.FORWARD)
        self.assertEqual(decision.target, "Waveshare S3 Audio")
        self.assertEqual(decision.sip_uri, "sip:Waveshare_S3_Audio@192.168.1.47;transport=udp")

    def test_ha_router_explicit_sip_uri_routes_without_phonebook_entry(self) -> None:
        decision = router.resolve_ha_router("sip:LabPhone@192.168.1.60:5062;transport=tcp", [], trunk_ready=False)
        self.assertEqual(decision.action, router.RouteAction.DIRECT)
        self.assertEqual(decision.target, "sip:LabPhone@192.168.1.60:5062;transport=tcp")
        self.assertEqual(decision.sip_uri, "sip:LabPhone@192.168.1.60:5062;transport=tcp")
        self.assertIsNone(decision.entry)

    def test_ha_router_address_port_transport_contract_builds_direct_uri(self) -> None:
        entries = roster.parse_roster_json(
            [
                {
                    "id": "Desk",
                    "name": "Desk",
                    "address": "192.168.1.55",
                    "port": 5070,
                    "metadata": {"transport": "tcp"},
                }
            ]
        )
        decision = router.resolve_ha_router("Desk", entries, trunk_ready=False)
        self.assertEqual(decision.action, router.RouteAction.FORWARD)
        self.assertEqual(decision.target, "Desk")
        self.assertEqual(decision.sip_uri, "sip:Desk@192.168.1.55:5070;transport=tcp")

    def test_ha_router_public_number_requires_ready_trunk(self) -> None:
        unavailable = router.resolve_ha_router("0551234567", [], trunk_ready=False)
        self.assertEqual(unavailable.action, router.RouteAction.REJECT)
        self.assertEqual(unavailable.status, 503)
        self.assertEqual(unavailable.reason, router.RouteReason.TRUNK_UNAVAILABLE)
        ready = router.resolve_ha_router("0551234567", [], trunk_ready=True)
        self.assertEqual(ready.action, router.RouteAction.TRUNK)

    def test_roster_contact_fields_are_data_driven(self) -> None:
        entries = roster.parse_roster_json(
            [
                {"name": "Daniele", "number": "3510000000"},
                {"name": "Spotpear", "address": "192.168.1.31", "extension": "101"},
                {"name": "Desk", "address": "192.168.1.55", "metadata": {"sip_transport": "udp"}},
                {"name": "Logical HA Target"},
            ]
        )
        self.assertEqual(entries[0].number, "3510000000")
        self.assertEqual(entries[1].extension, "101")
        self.assertEqual(entries[2].address, "192.168.1.55")
        self.assertEqual(entries[3].id, "Logical HA Target")

    def test_ha_router_name_with_number_and_no_endpoint_uses_trunk(self) -> None:
        entries = roster.parse_roster_json(
            [{"id": "Daniele", "name": "Daniele", "number": "3510000000"}]
        )
        unavailable = router.resolve_ha_router("Daniele", entries, trunk_ready=False)
        self.assertEqual(unavailable.action, router.RouteAction.REJECT)
        self.assertEqual(unavailable.target, "3510000000")
        self.assertEqual(unavailable.reason, router.RouteReason.TRUNK_UNAVAILABLE)
        ready = router.resolve_ha_router("Daniele", entries, trunk_ready=True)
        self.assertEqual(ready.action, router.RouteAction.TRUNK)
        self.assertEqual(ready.target, "3510000000")

    def test_ha_router_extension_resolves_internal_endpoint_not_trunk_number(self) -> None:
        entries = roster.parse_roster_json(
            [
                {
                    "id": "Spotpear",
                    "name": "Spotpear Ball v2",
                    "address": "192.168.1.31",
                    "extension": "101",
                    "metadata": {"sip_transport": "udp"},
                },
                {"id": "Daniele", "name": "Daniele", "number": "101"},
            ]
        )
        internal = router.resolve_ha_router("101", entries, trunk_ready=True)
        self.assertEqual(internal.action, router.RouteAction.FORWARD)
        self.assertEqual(internal.target, "Spotpear")
        self.assertIn("192.168.1.31", internal.sip_uri)

        external = router.resolve_ha_router("Daniele", entries, trunk_ready=True)
        self.assertEqual(external.action, router.RouteAction.TRUNK)
        self.assertEqual(external.target, "101")

    def test_ha_router_name_only_contact_answers_ha(self) -> None:
        entries = roster.parse_roster_json([{"id": "Casa Logica", "name": "Casa Logica"}])
        decision = router.resolve_ha_router("Casa Logica", entries, trunk_ready=True)
        self.assertEqual(decision.action, router.RouteAction.ANSWER_HA)
        self.assertEqual(decision.reason, router.RouteReason.NAME_VIA_HA)

    def test_trunk_inbound_no_hint_answers_ha(self) -> None:
        ctx = router.CallContext(call_id="trunk-1", direction="inbound", origin="trunk")
        decision = router.route_inbound_trunk(ctx, [], trunk_ready=False)
        self.assertEqual(decision.action, router.RouteAction.ANSWER_HA)

    def test_trunk_inbound_unknown_hint_rejects_route_not_found(self) -> None:
        ctx = router.CallContext(
            call_id="trunk-2",
            direction="inbound",
            origin="trunk",
            route_hint="999",
        )
        decision = router.route_inbound_trunk(ctx, [], trunk_ready=False)
        self.assertEqual(decision.action, router.RouteAction.REJECT)
        self.assertEqual(decision.reason, router.RouteReason.ROUTE_NOT_FOUND)
        self.assertEqual(decision.status, 404)

    def test_trunk_inbound_hint_resolves_phonebook_extension_alias(self) -> None:
        entries = roster.parse_roster_json(
            [
                {
                    "id": "Spotpear",
                    "name": "Spotpear Ball v2",
                    "address": "192.168.1.31",
                    "extension": "200",
                    "metadata": {"sip_transport": "udp"},
                }
            ]
        )
        ctx = router.CallContext(
            call_id="trunk-3",
            direction="inbound",
            origin="trunk",
            route_hint="200",
        )
        decision = router.route_inbound_trunk(ctx, entries, trunk_ready=False)
        self.assertEqual(decision.action, router.RouteAction.FORWARD)
        self.assertEqual(decision.target, "Spotpear")

    def test_disabled_entry_rejects(self) -> None:
        entries = roster.parse_roster_json(
            [{"id": "WS3", "address": "192.168.1.47", "enabled": False}]
        )
        decision = router.resolve_ha_router("WS3", entries, trunk_ready=False)
        self.assertEqual(decision.action, router.RouteAction.REJECT)
        self.assertEqual(decision.reason, router.RouteReason.TARGET_DISABLED)


class SipProtocolBugFixTest(unittest.TestCase):
    def test_dtmf_collector_emits_one_digit_per_event(self) -> None:
        digits: list[str] = []
        proto = dtmf._DtmfProtocol(101, digits.append, remote_host="127.0.0.1")

        def packet(*, sequence: int, timestamp: int, ended: bool, ssrc: bytes = b"ssrc") -> bytes:
            header = bytearray(12)
            header[0] = 0x80
            header[1] = 101
            header[2:4] = int(sequence).to_bytes(2, "big")
            header[4:8] = int(timestamp).to_bytes(4, "big")
            header[8:12] = ssrc
            payload = bytes([5, 0x80 if ended else 0x00, 0x00, 0xA0])
            return bytes(header) + payload

        proto.datagram_received(packet(sequence=0, timestamp=999, ended=False), ("127.0.0.2", 5000))
        proto.datagram_received(packet(sequence=1, timestamp=1234, ended=False), ("127.0.0.1", 5000))
        proto.datagram_received(packet(sequence=2, timestamp=1234, ended=True), ("127.0.0.1", 5000))
        proto.datagram_received(packet(sequence=3, timestamp=1234, ended=True), ("127.0.0.1", 5000))
        proto.datagram_received(
            packet(sequence=4, timestamp=5678, ended=False, ssrc=b"evil"),
            ("127.0.0.1", 5000),
        )
        self.assertEqual(digits, ["5"])

    def test_response_contact_uses_configured_local_sip_port(self) -> None:
        request = sip.parse_message(
            sip.build_request(
                "INVITE",
                "sip:HA@192.168.1.10:9999",
                [
                    ("Via", "SIP/2.0/UDP 192.168.1.30:5060;branch=z9hG4bKx;rport"),
                    ("From", "<sip:ESP@192.168.1.30>;tag=a"),
                    ("To", "<sip:HA@192.168.1.10>"),
                    ("Call-ID", "contact-port"),
                    ("CSeq", "1 INVITE"),
                    ("Content-Length", "0"),
                ],
                b"",
            )
        )
        uri = sip_listener._response_contact_uri(
            request,
            local_ip="192.168.1.10",
            local_sip_port=5060,
            transport="UDP",
        )
        self.assertEqual(uri, "sip:HA@192.168.1.10:5060;transport=udp")

    def test_invite_error_ack_uses_invite_transaction(self) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[bytes, tuple[str, int]]] = []

            def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
                self.sent.append((data, addr))

        client = sip_client.SipCallClient(local_ip="192.168.1.10", local_name="HA", local_sip_port=5060, local_rtp_port=41000)
        client.transport = FakeTransport()  # type: ignore[assignment]
        client.dialog_ids = sip.SipDialogIds(call_id="call-error", local_tag="ltag", cseq=3, branch="z9hG4bKorig")
        client._invite_cseq = 3
        client._pending_request_uri = "sip:ESP@192.168.1.30:5060"
        client._pending_local_uri = "sip:HA@192.168.1.10:5060"
        client._pending_remote_uri = "sip:ESP@192.168.1.30:5060"
        msg = sip.parse_message(
            sip.build_response(
                486,
                "Busy Here",
                [
                    ("Via", "SIP/2.0/UDP 192.168.1.10:5060;branch=z9hG4bKorig"),
                    ("From", "<sip:HA@192.168.1.10>;tag=ltag"),
                    ("To", "<sip:ESP@192.168.1.30>;tag=rtag"),
                    ("Call-ID", "call-error"),
                    ("CSeq", "3 INVITE"),
                ],
                b"",
            )
        )
        client._send_invite_error_ack(msg, "192.168.1.30", 5060)
        raw, addr = client.transport.sent[0]  # type: ignore[union-attr]
        parsed = sip.parse_message(raw)
        self.assertEqual(addr, ("192.168.1.30", 5060))
        self.assertEqual(parsed.method, "ACK")
        self.assertEqual(parsed.header("CSeq"), "3 ACK")
        self.assertIn("z9hG4bKorig", parsed.header("Via"))


class SipProtocolBugFixAsyncTest(unittest.IsolatedAsyncioTestCase):
    async def test_rtp_relay_partial_bind_failure_releases_first_socket(self) -> None:
        first = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        blocker = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        first.bind(("127.0.0.1", 0))
        blocker.bind(("127.0.0.1", 0))
        left_port = first.getsockname()[1]
        right_port = blocker.getsockname()[1]
        first.close()
        fmt = audio_format.AudioFormat(16000, "s16le", 1, 20)
        relay = sip_rtp_bridge.SipRtpRelay(
            left=sip_rtp_bridge.RtpPeer("127.0.0.2", 40000, 96, fmt),
            right=sip_rtp_bridge.RtpPeer("127.0.0.3", 41000, 96, fmt),
            left_port=left_port,
            right_port=right_port,
        )
        try:
            with self.assertRaises(OSError):
                await relay.start()
            probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                probe.bind(("0.0.0.0", left_port))
            finally:
                probe.close()
        finally:
            blocker.close()

    def test_incompatible_invite_200_is_acked_then_closed_with_bye(self) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[bytes, tuple[str, int]]] = []

            def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
                self.sent.append((data, addr))

        client = sip_client.SipCallClient(
            local_ip="127.0.0.1",
            local_name="HA",
            local_sip_port=5060,
            local_rtp_port=41000,
        )
        transport = FakeTransport()
        client.transport = transport  # type: ignore[assignment]
        client._invite_cseq = 7
        response = sip.parse_message(
            sip.build_response(
                200,
                "OK",
                [
                    ("Via", "SIP/2.0/UDP 127.0.0.1:5060;branch=z9hG4bKtest"),
                    ("From", "<sip:HA@127.0.0.1>;tag=local"),
                    ("To", "<sip:ESP@127.0.0.2>;tag=remote"),
                    ("Contact", '"ESP handset" <sip:dialog@127.0.0.2:5090;transport=udp>'),
                    ("Call-ID", client.dialog_ids.call_id),
                    ("CSeq", "7 INVITE"),
                ],
                b"",
            )
        )
        compatible = client._commit_200_ok(
            response,
            "ESP",
            "127.0.0.2",
            5060,
            "sip:ESP@127.0.0.2:5060",
            "sip:HA@127.0.0.1:5060",
            "sip:ESP@127.0.0.2:5060",
        )

        self.assertFalse(compatible)
        messages = [sip.parse_message(raw) for raw, _addr in transport.sent]
        methods = [message.method for message in messages]
        self.assertEqual(methods, ["ACK", "BYE"])
        self.assertEqual([message.uri for message in messages], ["sip:dialog@127.0.0.2:5090;transport=udp"] * 2)
        self.assertEqual(
            [message.header("To") for message in messages],
            ["<sip:ESP@127.0.0.2:5060>;tag=remote"] * 2,
        )

    async def test_sip_tcp_reader_rejects_oversized_header(self) -> None:
        reader = asyncio.StreamReader()
        reader.feed_data(b"OPTIONS sip:ha SIP/2.0\r\nX-Fill: " + b"x" * sip.MAX_SIP_MESSAGE_BYTES + b"\r\n\r\n")
        reader.feed_eof()
        self.assertIsNone(await sip_tcp_io.read_sip_stream_message(reader))

    async def test_sip_tcp_reader_rejects_oversized_combined_record_without_waiting_for_body(self) -> None:
        reader = asyncio.StreamReader()
        padding = b"x" * (sip.MAX_SIP_MESSAGE_BYTES - sip.MAX_SIP_BODY_BYTES)
        reader.feed_data(
            b"OPTIONS sip:ha SIP/2.0\r\nContent-Length: "
            + str(sip.MAX_SIP_BODY_BYTES).encode()
            + b"\r\nX-Fill: "
            + padding
            + b"\r\n\r\n"
        )
        self.assertIsNone(await asyncio.wait_for(sip_tcp_io.read_sip_stream_message(reader), timeout=0.1))

    async def test_sip_tcp_reader_rejects_ambiguous_content_length(self) -> None:
        reader = asyncio.StreamReader()
        reader.feed_data(
            b"OPTIONS sip:ha SIP/2.0\r\nContent-Length: 0\r\nContent-Length: 1\r\n\r\n"
        )
        reader.feed_eof()
        self.assertIsNone(await sip_tcp_io.read_sip_stream_message(reader))

    async def test_sip_tcp_reader_accepts_compact_content_length(self) -> None:
        reader = asyncio.StreamReader()
        reader.feed_data(b"OPTIONS sip:ha SIP/2.0\r\nl: 4\r\n\r\ntest")
        reader.feed_eof()
        self.assertEqual(
            await sip_tcp_io.read_sip_stream_message(reader),
            b"OPTIONS sip:ha SIP/2.0\r\nl: 4\r\n\r\ntest",
        )

    async def test_cancelled_tcp_send_cannot_enqueue_later(self) -> None:
        class BlockingWriter:
            def __init__(self) -> None:
                self.release = asyncio.Event()

            def is_closing(self) -> bool:
                return False

            def write(self, _data: bytes) -> None:
                pass

            async def drain(self) -> None:
                await self.release.wait()

        stream = BlockingWriter()
        writer = sip_tcp_io.SipTcpWriter(stream, label="test", max_queue=1)
        self.assertTrue(writer.send_nowait(b"in-flight"))
        await asyncio.sleep(0)
        self.assertTrue(writer.send_nowait(b"queued"))

        blocked_send = asyncio.create_task(writer.send(b"stale"))
        await asyncio.sleep(0)
        blocked_send.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await blocked_send

        self.assertEqual(writer.queue.get_nowait(), b"queued")
        await asyncio.sleep(0)
        self.assertTrue(writer.queue.empty())
        stream.release.set()
        await writer.close()

    async def test_tcp_send_unblocks_when_writer_task_dies_with_full_queue(self) -> None:
        class FailingWriter:
            def __init__(self) -> None:
                self.release = asyncio.Event()
                self.writes: list[bytes] = []

            def is_closing(self) -> bool:
                return False

            def write(self, data: bytes) -> None:
                self.writes.append(data)

            async def drain(self) -> None:
                await self.release.wait()
                raise OSError("connection lost")

        stream = FailingWriter()
        writer = sip_tcp_io.SipTcpWriter(stream, label="test", max_queue=1)
        self.assertTrue(writer.send_nowait(b"first"))
        await asyncio.sleep(0)
        self.assertTrue(writer.send_nowait(b"second"))
        blocked_send = asyncio.create_task(writer.send(b"third"))
        await asyncio.sleep(0)

        stream.release.set()
        self.assertFalse(await asyncio.wait_for(blocked_send, timeout=0.2))
        self.assertTrue(writer.task.done())

    async def test_client_ignores_response_for_another_call_id(self) -> None:
        client = sip_client.SipCallClient(
            local_ip="127.0.0.1",
            local_name="HA",
            local_sip_port=5060,
            local_rtp_port=41000,
        )
        right_call_id = client.dialog_ids.call_id

        def response(call_id: str) -> bytes:
            return sip.build_response(
                180,
                "Ringing",
                [
                    ("Via", "SIP/2.0/UDP 127.0.0.1:5060;branch=z9hG4bKtest"),
                    ("From", "<sip:HA@127.0.0.1>;tag=local"),
                    ("To", "<sip:ESP@127.0.0.2>;tag=remote"),
                    ("Call-ID", call_id),
                    ("CSeq", "1 INVITE"),
                ],
                b"",
            )

        client.queue.put_nowait((response("stale-call"), ("127.0.0.2", 5060)))
        client.queue.put_nowait((response(right_call_id), ("127.0.0.2", 5060)))
        received = await client._read_response(0.1)
        self.assertIsNotNone(received)
        assert received is not None
        self.assertEqual(received[0].header("Call-ID"), right_call_id)

    async def test_trunk_registration_filters_call_id_method_and_cseq(self) -> None:
        config = sip_trunk.SipTrunkConfig(
            enabled=True,
            transport="udp",
            server="127.0.0.1",
            port=5060,
            domain="127.0.0.1",
            username="ha",
            auth_username="ha",
            password="",
            expires=300,
        )
        trunk = sip_trunk.SipTrunkClient(config=config, local_ip="127.0.0.1", local_sip_port=5060)

        def response(call_id: str, cseq: str) -> sip.SipMessage:
            return sip.parse_message(
                sip.build_response(
                    200,
                    "OK",
                    [
                        ("Via", "SIP/2.0/UDP 127.0.0.1:5060;branch=z9hG4bKtest"),
                        ("From", "<sip:ha@127.0.0.1>;tag=local"),
                        ("To", "<sip:ha@127.0.0.1>;tag=remote"),
                        ("Call-ID", call_id),
                        ("CSeq", cseq),
                    ],
                    b"",
                )
            )

        trunk.responses.put_nowait(response("other", "2 REGISTER"))
        trunk.responses.put_nowait(response(trunk.call_id, "2 INVITE"))
        trunk.responses.put_nowait(response(trunk.call_id, "1 REGISTER"))
        provisional = sip.parse_message(
            sip.build_response(
                100,
                "Trying",
                [
                    ("Via", "SIP/2.0/UDP 127.0.0.1:5060;branch=z9hG4bKtest"),
                    ("From", "<sip:ha@127.0.0.1>;tag=local"),
                    ("To", "<sip:ha@127.0.0.1>;tag=remote"),
                    ("Call-ID", trunk.call_id),
                    ("CSeq", "2 REGISTER"),
                ],
                b"",
            )
        )
        trunk.responses.put_nowait(provisional)
        trunk.responses.put_nowait(response(trunk.call_id, "2 REGISTER"))
        received = await trunk._read_response(0.1, expected_cseq=2)
        self.assertEqual(received.header("CSeq"), "2 REGISTER")

    def test_trunk_outbound_proxy_uri_selects_host_and_port(self) -> None:
        config = sip_trunk.SipTrunkConfig(
            enabled=True,
            transport="tcp",
            server="pbx.example",
            port=5060,
            domain="pbx.example",
            username="ha",
            auth_username="ha",
            password="",
            expires=300,
            outbound_proxy="sip:proxy.example:5070;transport=tcp",
        )
        trunk = sip_trunk.SipTrunkClient(config=config, local_ip="127.0.0.1", local_sip_port=5060)

        self.assertEqual(trunk.registrar_target, ("proxy.example", 5070))

    def test_trunk_refresh_precedes_short_granted_registration_expiry(self) -> None:
        self.assertEqual(sip_trunk._registration_refresh_delay(300, 1020.0, 1000.0), 10.0)
        self.assertEqual(sip_trunk._registration_refresh_delay(300, 1005.0, 1000.0), 1.0)
        self.assertEqual(sip_trunk._registration_refresh_delay(300, 1300.0, 1000.0), 240.0)

    async def test_confirmed_dialog_rejects_cancel_without_ending_call(self) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[bytes, tuple[str, int]]] = []

            def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
                self.sent.append((data, addr))

        client = sip_client.SipCallClient(
            local_ip="127.0.0.1", local_name="HA", local_sip_port=5060, local_rtp_port=41000
        )
        transport = FakeTransport()
        client.transport = transport  # type: ignore[assignment]
        client.dialog = types.SimpleNamespace(remote_host="127.0.0.2")  # type: ignore[assignment]
        client.dialog_ids.remote_tag = "remote"
        call_id = client.dialog_ids.call_id

        def request(method: str, cseq: int) -> bytes:
            return sip.build_request(
                method,
                "sip:HA@127.0.0.1:5060",
                [
                    ("Via", "SIP/2.0/UDP 127.0.0.2:5060;branch=z9hG4bKdialog"),
                    ("From", "<sip:ESP@127.0.0.2>;tag=remote"),
                    ("To", f"<sip:HA@127.0.0.1>;tag={client.dialog_ids.local_tag}"),
                    ("Call-ID", call_id),
                    ("CSeq", f"{cseq} {method}"),
                ],
                b"",
            )

        client.queue.put_nowait((request("CANCEL", 1), ("127.0.0.2", 5060)))
        client.queue.put_nowait((request("BYE", 2), ("127.0.0.99", 5060)))
        client.queue.put_nowait((request("BYE", 2), ("127.0.0.2", 5060)))
        self.assertEqual(await client.wait_for_dialog_termination(timeout=0.1), "remote_hangup")
        self.assertEqual(
            [sip.parse_message(raw).status_code for raw, _addr in transport.sent],
            [481, 481, 200],
        )

    async def test_confirmed_dialog_rejects_reinvite_but_keeps_call_alive(self) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[bytes, tuple[str, int]]] = []

            def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
                self.sent.append((data, addr))

        client = sip_client.SipCallClient(
            local_ip="127.0.0.1", local_name="HA", local_sip_port=5060, local_rtp_port=41000
        )
        transport = FakeTransport()
        client.transport = transport  # type: ignore[assignment]
        client.dialog = types.SimpleNamespace(
            remote_host="127.0.0.2",
            remote_sip_port=5060,
            remote_target_uri="sip:ESP@127.0.0.2:5060",
        )  # type: ignore[assignment]
        client.dialog_ids.remote_tag = "remote"

        def request(method: str, cseq: int, *, remote_tag: str = "remote") -> bytes:
            return sip.build_request(
                method,
                "sip:HA@127.0.0.1:5060",
                [
                    ("Via", "SIP/2.0/UDP 127.0.0.2:5060;branch=z9hG4bKdialog"),
                    ("From", f"<sip:ESP@127.0.0.2>;tag={remote_tag}"),
                    ("To", f"<sip:HA@127.0.0.1>;tag={client.dialog_ids.local_tag}"),
                    ("Call-ID", client.dialog_ids.call_id),
                    ("CSeq", f"{cseq} {method}"),
                    ("Content-Type", "application/sdp"),
                ],
                b"v=0\r\na=sendonly\r\n",
            )

        client.queue.put_nowait((request("INVITE", 2, remote_tag="wrong"), ("127.0.0.2", 5060)))
        client.queue.put_nowait((request("INVITE", 3), ("127.0.0.2", 5060)))
        client.queue.put_nowait((request("ACK", 3), ("127.0.0.2", 5060)))
        client.queue.put_nowait((request("BYE", 4), ("127.0.0.2", 5060)))

        self.assertEqual(await client.wait_for_dialog_termination(timeout=0.1), "remote_hangup")
        responses = [sip.parse_message(raw) for raw, _addr in transport.sent]
        self.assertEqual([response.status_code for response in responses], [481, 488, 200])
        self.assertIn("Session renegotiation is not supported", responses[1].header("Warning"))

    async def test_confirmed_dialog_reacks_retransmitted_invite_2xx(self) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[bytes, tuple[str, int]]] = []

            def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
                self.sent.append((data, addr))

        client = sip_client.SipCallClient(
            local_ip="127.0.0.1",
            local_name="HA",
            local_sip_port=5060,
            local_rtp_port=41000,
        )
        transport = FakeTransport()
        client.transport = transport  # type: ignore[assignment]
        fmt = sdp.audio_format_to_rtp(audio_format.AudioFormat(16000, "s16le", 1, 20), 96)
        client.dialog_ids.remote_tag = "remote"
        client.dialog = sip_client.SipDialog(
            target="ESP",
            remote_host="127.0.0.2",
            remote_sip_port=5060,
            remote_rtp_host="127.0.0.2",
            remote_rtp_port=42000,
            local_rtp_port=41000,
            call_id=client.dialog_ids.call_id,
            local_uri="sip:HA@127.0.0.1:5060",
            remote_uri="sip:ESP@127.0.0.2:5060",
            send_format=fmt,
            recv_format=fmt,
            remote_target_uri="sip:ESP@127.0.0.2:5060",
        )
        duplicate_ok = sip.build_response(
            200,
            "OK",
            [
                ("Via", f"SIP/2.0/UDP 127.0.0.1:5060;branch={client.dialog_ids.branch}"),
                ("From", f"<sip:HA@127.0.0.1>;tag={client.dialog_ids.local_tag}"),
                ("To", "<sip:ESP@127.0.0.2>;tag=remote"),
                ("Call-ID", client.dialog_ids.call_id),
                ("CSeq", f"{client._invite_cseq} INVITE"),
            ],
        )
        bye = sip.build_request(
            "BYE",
            "sip:HA@127.0.0.1:5060",
            [
                ("Via", "SIP/2.0/UDP 127.0.0.2:5060;branch=z9hG4bKbye"),
                ("Via", "SIP/2.0/UDP 127.0.0.3:5060;branch=z9hG4bKproxy"),
                ("From", "<sip:ESP@127.0.0.2>;tag=remote"),
                ("To", f"<sip:HA@127.0.0.1>;tag={client.dialog_ids.local_tag}"),
                ("Call-ID", client.dialog_ids.call_id),
                ("CSeq", "2 BYE"),
            ],
        )
        client.queue.put_nowait((duplicate_ok, ("127.0.0.2", 5060)))
        client.queue.put_nowait((bye, ("127.0.0.2", 5060)))

        self.assertEqual(await client.wait_for_dialog_termination(timeout=0.1), "remote_hangup")
        self.assertEqual(sip.parse_message(transport.sent[0][0]).method, "ACK")
        bye_response = sip.parse_message(transport.sent[1][0])
        self.assertEqual(bye_response.status_code, 200)
        self.assertEqual(len(bye_response.header_values("Via")), 2)

    async def test_cancelled_invite_final_response_is_acked(self) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[bytes, tuple[str, int]]] = []

            def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
                self.sent.append((data, addr))

        client = sip_client.SipCallClient(
            local_ip="127.0.0.1", local_name="HA", local_sip_port=5060, local_rtp_port=41000
        )
        transport = FakeTransport()
        client.transport = transport  # type: ignore[assignment]
        client._pending_remote_host = "127.0.0.2"
        client._pending_remote_sip_port = 5060
        client._pending_request_uri = "sip:ESP@127.0.0.2:5060"
        client._pending_local_uri = "sip:HA@127.0.0.1:5060"
        client._pending_remote_uri = "sip:ESP@127.0.0.2:5060"
        client._invite_transaction_active = True
        client._received_provisional = True
        call_id = client.dialog_ids.call_id
        client.queue.put_nowait(
            (
                sip.build_response(
                    200,
                    "OK",
                    [
                        ("Via", f"SIP/2.0/UDP 127.0.0.1:5060;branch={client.dialog_ids.branch}"),
                        ("From", f"<sip:HA@127.0.0.1>;tag={client.dialog_ids.local_tag}"),
                        ("To", "<sip:ESP@127.0.0.2>;tag=remote"),
                        ("Call-ID", call_id),
                        ("CSeq", f"{client._invite_cseq} CANCEL"),
                    ],
                    b"",
                ),
                ("127.0.0.2", 5060),
            )
        )
        client.queue.put_nowait(
            (
                sip.build_response(
                    487,
                    "Request Terminated",
                    [
                        ("Via", f"SIP/2.0/UDP 127.0.0.1:5060;branch={client.dialog_ids.branch}"),
                        ("From", f"<sip:HA@127.0.0.1>;tag={client.dialog_ids.local_tag}"),
                        ("To", "<sip:ESP@127.0.0.2>;tag=remote"),
                        ("Call-ID", call_id),
                        ("CSeq", f"{client._invite_cseq} INVITE"),
                    ],
                    b"",
                ),
                ("127.0.0.2", 5060),
            )
        )

        self.assertEqual(await client.terminate(timeout=0.1), "cancelled")
        self.assertEqual([sip.parse_message(raw).method for raw, _addr in transport.sent], ["CANCEL", "ACK"])

    async def test_cancel_race_accepts_the_separate_bye_transaction(self) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[bytes, tuple[str, int]]] = []

            def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
                self.sent.append((data, addr))

        client = sip_client.SipCallClient(
            local_ip="127.0.0.1", local_name="HA", local_sip_port=5060, local_rtp_port=41000
        )
        transport = FakeTransport()
        client.transport = transport  # type: ignore[assignment]
        client._pending_target = "ESP"
        client._pending_remote_host = "127.0.0.2"
        client._pending_remote_sip_port = 5060
        client._pending_request_uri = "sip:ESP@127.0.0.2:5060"
        client._pending_local_uri = "sip:HA@127.0.0.1:5060"
        client._pending_remote_uri = "sip:ESP@127.0.0.2:5060"
        client._invite_transaction_active = True
        client._received_provisional = True
        call_id = client.dialog_ids.call_id
        rtp_fmt = sdp.audio_format_to_rtp(audio_format.AudioFormat(16000, "s16le", 1, 20), 96)
        answer = sdp.build_answer("127.0.0.2", "127.0.0.2", 42000, rtp_fmt).encode()

        def response(status: int, reason: str, method: str, cseq: int, branch: str, body: bytes = b"") -> bytes:
            headers = [
                ("Via", f"SIP/2.0/UDP 127.0.0.1:5060;branch={branch}"),
                ("From", f"<sip:HA@127.0.0.1>;tag={client.dialog_ids.local_tag}"),
                ("To", "<sip:ESP@127.0.0.2>;tag=remote"),
                ("Call-ID", call_id),
                ("CSeq", f"{cseq} {method}"),
            ]
            if body:
                headers.append(("Content-Type", "application/sdp"))
            return sip.build_response(status, reason, headers, body)

        client.queue.put_nowait((
            response(200, "OK", "INVITE", client._invite_cseq, client.dialog_ids.branch, answer),
            ("127.0.0.2", 5060),
        ))
        terminating = asyncio.create_task(client.terminate(timeout=0.5))
        while not any(sip.parse_message(raw).method == "BYE" for raw, _addr in transport.sent):
            await asyncio.sleep(0)
        client.queue.put_nowait((
            response(200, "OK", "BYE", client._bye_cseq, client._bye_branch),
            ("127.0.0.2", 5060),
        ))

        self.assertEqual(await terminating, "cancelled")
        self.assertEqual([sip.parse_message(raw).method for raw, _addr in transport.sent], ["CANCEL", "ACK", "BYE"])

    async def test_trunk_old_tcp_reader_cannot_clear_replacement(self) -> None:
        config = sip_trunk.SipTrunkConfig(
            enabled=True,
            transport="tcp",
            server="127.0.0.1",
            port=5060,
            domain="127.0.0.1",
            username="ha",
            auth_username="ha",
            password="",
            expires=300,
        )
        trunk = sip_trunk.SipTrunkClient(config=config, local_ip="127.0.0.1", local_sip_port=5060)
        old_reader = asyncio.StreamReader()
        new_reader = asyncio.StreamReader()

        class Writer:
            def is_closing(self) -> bool:
                return False

            def get_extra_info(self, _name: str):
                return ("127.0.0.1", 5060)

            def close(self) -> None:
                pass

        old_writer = Writer()
        new_writer = Writer()
        trunk.reader = old_reader
        trunk.writer = old_writer  # type: ignore[assignment]
        trunk._reader_ready.set()
        replacement_read = asyncio.Event()

        async def fake_read(reader):
            if reader is old_reader:
                trunk.reader = new_reader
                trunk.writer = new_writer  # type: ignore[assignment]
                return None
            replacement_read.set()
            await asyncio.Event().wait()

        original_read = sip_trunk._read_sip_stream_message
        sip_trunk._read_sip_stream_message = fake_read
        task = asyncio.create_task(trunk._receive_loop())
        try:
            await asyncio.wait_for(replacement_read.wait(), timeout=0.1)
            self.assertIs(trunk.reader, new_reader)
            self.assertIs(trunk.writer, new_writer)
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            sip_trunk._read_sip_stream_message = original_read

    async def test_udp_endpoint_caps_concurrent_handler_tasks(self) -> None:
        async def on_invite(_invite):
            raise AssertionError("not used")

        endpoint = sip_listener.SipUdpEndpoint(
            local_ip="127.0.0.1",
            local_sip_port=5060,
            local_rtp_port=41000,
            supported_formats=[audio_format.AudioFormat(16000, "s16le", 1, 20)],
            on_invite=on_invite,
        )
        release = asyncio.Event()

        async def blocked_handler(_data, _addr):
            await release.wait()

        endpoint._handle_datagram = blocked_handler  # type: ignore[method-assign]
        for index in range(100):
            endpoint.datagram_received(b"test", ("127.0.0.1", index))
        await asyncio.sleep(0)

        self.assertEqual(len(endpoint._request_tasks), sip_listener._MAX_SIP_INVITE_TASKS)
        self.assertEqual(endpoint.dropped_datagrams, 100 - sip_listener._MAX_SIP_INVITE_TASKS)
        release.set()
        await asyncio.gather(*tuple(endpoint._request_tasks))

    async def test_udp_endpoint_reserves_control_capacity_under_invite_load(self) -> None:
        async def on_invite(_invite):
            raise AssertionError("not used")

        endpoint = sip_listener.SipUdpEndpoint(
            local_ip="127.0.0.1",
            local_sip_port=5060,
            local_rtp_port=41000,
            supported_formats=[audio_format.AudioFormat(16000, "s16le", 1, 20)],
            on_invite=on_invite,
        )
        release = asyncio.Event()

        async def blocked_handler(_data, _addr):
            await release.wait()

        endpoint._handle_datagram = blocked_handler  # type: ignore[method-assign]
        for index in range(100):
            endpoint.datagram_received(b"INVITE sip:test SIP/2.0\r\n\r\n", ("127.0.0.1", index))
        for index in range(8):
            endpoint.datagram_received(b"CANCEL sip:test SIP/2.0\r\n\r\n", ("127.0.0.1", index))
        await asyncio.sleep(0)

        self.assertEqual(len(endpoint._invite_tasks), sip_listener._MAX_SIP_INVITE_TASKS)
        self.assertEqual(len(endpoint._request_tasks), sip_listener._MAX_SIP_UDP_TASKS)
        release.set()
        await asyncio.gather(*tuple(endpoint._request_tasks))

    async def test_trunk_reserves_control_capacity_under_invite_load(self) -> None:
        config = sip_trunk.SipTrunkConfig(
            enabled=True,
            transport="udp",
            server="127.0.0.1",
            port=5060,
            domain="127.0.0.1",
            username="ha",
            auth_username="ha",
            password="",
            expires=300,
        )
        trunk = sip_trunk.SipTrunkClient(config=config, local_ip="127.0.0.1", local_sip_port=5060)
        release = asyncio.Event()

        async def blocked_handler(_data, _addr):
            await release.wait()

        trunk.request_handler = blocked_handler
        for index in range(100):
            trunk._submit_request(b"invite", ("127.0.0.1", index), "INVITE")
        for index in range(8):
            trunk._submit_request(b"cancel", ("127.0.0.1", index), "CANCEL")
        await asyncio.sleep(0)

        self.assertEqual(len(trunk._invite_tasks), sip_trunk._MAX_TRUNK_INVITE_TASKS)
        self.assertEqual(len(trunk._request_tasks), sip_trunk._MAX_TRUNK_REQUEST_TASKS)
        release.set()
        await asyncio.gather(*tuple(trunk._request_tasks))

    async def test_udp_read_response_timeout_returns_none(self) -> None:
        client = sip_client.SipCallClient(
            local_ip="192.168.1.10",
            local_name="HA",
            local_sip_port=5060,
            local_rtp_port=41000,
            signaling_transport="UDP",
        )

        self.assertIsNone(await client._read_response(0.001))

    def test_invite_auth_retry_rebuilds_transaction_headers(self) -> None:
        source = (PKG_DIR / "sip_client.py").read_text()
        auth_branch = source[
            source.index("if msg.status_code in {401, 407}")
            : source.index("if msg.status_code and msg.status_code >= 300")
        ]

        self.assertIn("self.dialog_ids.branch = sip.make_branch()", auth_branch)
        self.assertIn("retry_headers = sip.dialog_headers(", auth_branch)
        self.assertIn("retry_headers.append((auth_header, auth_value))", auth_branch)
        self.assertIn("next_retransmit = loop.time() + retransmit_interval", auth_branch)
        self.assertNotIn("retry_headers = list(headers)", auth_branch)

    async def test_proxy_auth_retry_uses_trunk_identity(self) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[bytes, tuple[str, int]]] = []

            def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
                self.sent.append((data, addr))

        client = sip_client.SipCallClient(
            local_ip="192.168.1.10",
            local_name="17770000000",
            local_sip_port=5060,
            local_rtp_port=41000,
            username="17770000000",
            auth_username="17770000000",
            password="secret",
        )
        transport = FakeTransport()
        client.transport = transport  # type: ignore[assignment]
        response_count = 0

        async def read_response(_timeout: float):
            nonlocal response_count
            response_count += 1
            status, reason = (407, "Proxy Authentication Required") if response_count == 1 else (180, "Ringing")
            headers = [
                ("Via", f"SIP/2.0/UDP 192.168.1.10:5060;branch={client.dialog_ids.branch}"),
                ("From", f"<sip:17770000000@192.168.1.10:5060>;tag={client.dialog_ids.local_tag}"),
                ("To", "<sip:+15551234567@sip.example>;tag=provider"),
                ("Call-ID", client.dialog_ids.call_id),
                ("CSeq", f"{client._invite_cseq} INVITE"),
            ]
            if status == 407:
                headers.append(("Proxy-Authenticate", 'Digest realm="sip.example", nonce="nonce", qop="auth"'))
            return sip.parse_message(sip.build_response(status, reason, headers)), ("192.0.2.10", 5060)

        client._read_response = read_response  # type: ignore[method-assign]
        result = await client.invite(
            target="+15551234567",
            remote_host="192.0.2.10",
            remote_sip_port=5060,
            request_uri="sip:+15551234567@sip.example:5060;transport=udp",
        )

        self.assertEqual(result, "ringing")
        messages = [sip.parse_message(raw) for raw, _addr in transport.sent]
        invites = [message for message in messages if message.method == "INVITE"]
        self.assertEqual(len(invites), 2)
        self.assertEqual(invites[0].header("From"), invites[1].header("From"))
        self.assertTrue(invites[0].header("From").startswith("<sip:17770000000@192.168.1.10:5060"))
        self.assertTrue(invites[0].header("Contact").startswith("<sip:17770000000@192.168.1.10:5060"))
        self.assertEqual(invites[0].header("X-Voip-Stack-Caller-Name"), "17770000000")
        self.assertEqual(invites[1].header("X-Voip-Stack-Caller-Name"), "17770000000")
        self.assertFalse(invites[0].header("Proxy-Authorization"))
        self.assertIn('username="17770000000"', invites[1].header("Proxy-Authorization"))
        self.assertIn('uri="sip:+15551234567@sip.example:5060;transport=udp"', invites[1].header("Proxy-Authorization"))
        self.assertNotEqual(invites[0].header("Via"), invites[1].header("Via"))
        self.assertEqual(sip.parse_cseq(invites[1].header("CSeq")).number, sip.parse_cseq(invites[0].header("CSeq")).number + 1)

    async def test_pending_cancel_waits_for_provisional_then_terminates_invite(self) -> None:
        class FakeTransport:
            def __init__(self) -> None:
                self.sent: list[tuple[bytes, tuple[str, int]]] = []

            def sendto(self, data: bytes, addr: tuple[str, int]) -> None:
                self.sent.append((data, addr))

        client = sip_client.SipCallClient(
            local_ip="192.168.1.10",
            local_name="420",
            local_sip_port=5060,
            local_rtp_port=41000,
        )
        transport = FakeTransport()
        client.transport = transport  # type: ignore[assignment]
        response_count = 0

        async def read_response(_timeout: float):
            nonlocal response_count
            response_count += 1
            if response_count == 1:
                self.assertTrue(client.request_cancel())
                status, reason, method = 100, "Trying", "INVITE"
            elif response_count == 2:
                status, reason, method = 200, "OK", "CANCEL"
            else:
                status, reason, method = 487, "Request Terminated", "INVITE"
            headers = [
                ("Via", f"SIP/2.0/UDP 192.168.1.10:5060;branch={client.dialog_ids.branch}"),
                ("From", f"<sip:420@192.168.1.10:5060>;tag={client.dialog_ids.local_tag}"),
                ("To", "<sip:3519968203@sip.example>;tag=provider"),
                ("Call-ID", client.dialog_ids.call_id),
                ("CSeq", f"{client._invite_cseq} {method}"),
            ]
            return sip.parse_message(sip.build_response(status, reason, headers)), ("192.0.2.10", 5060)

        client._read_response = read_response  # type: ignore[method-assign]
        result = await client.invite(
            target="3519968203",
            remote_host="192.0.2.10",
            remote_sip_port=5060,
            request_uri="sip:3519968203@sip.example:5060;transport=udp",
        )

        self.assertEqual(result, "cancelled")
        messages = [sip.parse_message(raw) for raw, _addr in transport.sent]
        self.assertEqual([message.method for message in messages], ["INVITE", "CANCEL", "ACK"])
        invite, cancel, ack = messages
        self.assertEqual(sip.parse_cseq(cancel.header("CSeq")).number, sip.parse_cseq(invite.header("CSeq")).number)
        self.assertEqual(sip.parse_cseq(ack.header("CSeq")).number, sip.parse_cseq(invite.header("CSeq")).number)
        self.assertEqual(sip.parse_via(cancel.header("Via")).branch, sip.parse_via(invite.header("Via")).branch)
        self.assertEqual(sip.parse_via(ack.header("Via")).branch, sip.parse_via(invite.header("Via")).branch)

    async def test_invite_transaction_survives_owner_task_cancellation(self) -> None:
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
        transport = FakeTransport()
        client.transport = transport  # type: ignore[assignment]
        responses: asyncio.Queue[tuple[int, str, str]] = asyncio.Queue()

        async def read_response(_timeout: float):
            status, reason, method = await responses.get()
            headers = [
                ("Via", f"SIP/2.0/UDP 192.168.1.10:5060;branch={client.dialog_ids.branch}"),
                ("From", f"<sip:HA@192.168.1.10:5060>;tag={client.dialog_ids.local_tag}"),
                ("To", "<sip:ESP@192.0.2.10>;tag=remote"),
                ("Call-ID", client.dialog_ids.call_id),
                ("CSeq", f"{client._invite_cseq} {method}"),
            ]
            return sip.parse_message(sip.build_response(status, reason, headers)), ("192.0.2.10", 5060)

        client._read_response = read_response  # type: ignore[method-assign]
        owner = asyncio.create_task(
            client.invite(
                target="ESP",
                remote_host="192.0.2.10",
                remote_sip_port=5060,
                request_uri="sip:ESP@192.0.2.10:5060;transport=udp",
            )
        )
        while not transport.sent:
            await asyncio.sleep(0)
        owner.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await owner
        self.assertIsNotNone(client._invite_task)
        assert client._invite_task is not None
        self.assertFalse(client._invite_task.done())

        responses.put_nowait((100, "Trying", "INVITE"))
        responses.put_nowait((200, "OK", "CANCEL"))
        responses.put_nowait((487, "Request Terminated", "INVITE"))
        self.assertEqual(await client._invite_task, "cancelled")
        self.assertEqual(
            [sip.parse_message(raw).method for raw, _addr in transport.sent],
            ["INVITE", "CANCEL", "ACK"],
        )

    async def test_invite_treats_183_session_progress_as_ringing(self) -> None:
        sent: list[bytes] = []
        responses: asyncio.Queue[bytes] = asyncio.Queue()
        responses.put_nowait(
            sip.build_response(
                183,
                "Session Progress",
                [
                    ("Via", "SIP/2.0/TCP 192.168.1.10:5060;branch=z9hG4bKorig"),
                    ("From", "<sip:420@192.168.1.10>;tag=ltag"),
                    ("To", "<sip:3519968203@provider.example>;tag=rtag"),
                    ("Call-ID", "progress-call"),
                    ("CSeq", "1 INVITE"),
                ],
                b"",
            )
        )
        client = sip_client.SipCallClient(
            local_ip="192.168.1.10",
            local_name="420",
            local_sip_port=5060,
            local_rtp_port=41000,
            signaling_transport="TCP",
        )
        client.dialog_ids.call_id = "progress-call"
        client.dialog_ids.branch = "z9hG4bKorig"
        client.use_reused_tcp_connection(
            send=sent.append,
            responses=responses,
            close=lambda: None,
        )
        result = await client.invite(
            target="3519968203",
            remote_host="provider.example",
            remote_sip_port=5060,
            timeout=0.2,
        )
        self.assertEqual(result, "ringing")
        self.assertEqual(client.last_sip_status_code, 183)
        self.assertTrue(sent)


class SipRegistrarTest(unittest.IsolatedAsyncioTestCase):
    def test_digest_nonce_cache_is_bounded(self) -> None:
        registrar = sip_registrar.SipRegistrar(
            enabled=True,
            accounts=[],
            local_ip="192.168.1.10",
            local_sip_port=5060,
        )
        for _ in range(sip_registrar.MAX_ACTIVE_NONCES + 20):
            registrar._challenge()
        self.assertEqual(len(registrar.nonces), sip_registrar.MAX_ACTIVE_NONCES)

    def test_register_contacts_reject_non_sip_and_normalize_display_address(self) -> None:
        request = sip.SipMessage(
            method="REGISTER",
            uri="sip:ha@192.168.1.10",
            headers=(
                ("Contact", "https://example.invalid/phone"),
                ("Contact", '"Desk" <sip:desk@192.168.1.50:5090;transport=tcp>;expires=60'),
                ("Expires", "120"),
            ),
        )
        contacts = sip_registrar._register_contacts(request)
        self.assertEqual(
            contacts,
            [
                (
                    "sip:desk@192.168.1.50:5090;transport=tcp",
                    60,
                    '"Desk" <sip:desk@192.168.1.50:5090;transport=tcp>;expires=60',
                )
            ],
        )

    async def test_register_challenge_then_binding_roster_entry(self) -> None:
        registrar = sip_registrar.SipRegistrar(
            enabled=True,
            accounts=[sip_registrar.SipAccount("SmartphoneDany", "Smartphone Dany", "secret")],
            local_ip="192.168.1.10",
            local_sip_port=5060,
        )
        base_headers = [
            ("Via", "SIP/2.0/UDP 192.168.1.50:5062;branch=z9hG4bKreg;rport"),
            ("From", "<sip:SmartphoneDany@192.168.1.10>;tag=a"),
            ("To", "<sip:SmartphoneDany@192.168.1.10>"),
            ("Call-ID", "reg-1"),
            ("CSeq", "1 REGISTER"),
            ("Contact", "<sip:SmartphoneDany@192.168.1.50:5062;transport=udp>"),
            ("Expires", "120"),
        ]
        request_uri = "sip:SmartphoneDany@192.168.1.10"
        challenge_req = sip.parse_message(sip.build_request("REGISTER", request_uri, base_headers, b""))
        challenge = await registrar.handle_register(challenge_req, ("192.168.1.50", 5062), "UDP")
        self.assertEqual(challenge.status, 401)
        authenticate = dict(challenge.headers)["WWW-Authenticate"]
        authorization = sip_auth.build_digest_authorization(
            challenge_header=authenticate,
            username="SmartphoneDany",
            password="secret",
            method="REGISTER",
            uri=request_uri,
        )
        ok_req = sip.parse_message(
            sip.build_request("REGISTER", request_uri, base_headers + [("Authorization", authorization)], b"")
        )
        ok = await registrar.handle_register(ok_req, ("192.168.1.50", 5062), "UDP")
        self.assertEqual(ok.status, 200)
        entries = registrar.roster_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].id, "SmartphoneDany")
        self.assertEqual(entries[0].sip_uri, "sip:SmartphoneDany@192.168.1.50:5062;transport=udp")
        self.assertTrue(entries[0].metadata["registered"])

    async def test_stale_unregister_does_not_remove_active_binding(self) -> None:
        registrar = sip_registrar.SipRegistrar(
            enabled=True,
            accounts=[sip_registrar.SipAccount("Zoiper", "Zoiper", "secret")],
            local_ip="192.168.1.10",
            local_sip_port=5060,
        )
        registrar.registrations["Zoiper"] = sip_registrar.SipRegistration(
            username="Zoiper",
            contact_uri="sip:Zoiper@192.168.1.50:5062;transport=tcp",
            source_host="192.168.1.50",
            source_port=5062,
            transport="TCP",
            expires_at=9999999999,
        )
        challenge = registrar._challenge()[1]
        authorization = sip_auth.build_digest_authorization(
            challenge_header=challenge,
            username="Zoiper",
            password="secret",
            method="REGISTER",
            uri="sip:192.168.1.10;transport=tcp",
        )
        stale = sip.parse_message(
            sip.build_request(
                "REGISTER",
                "sip:192.168.1.10;transport=tcp",
                [
                    ("Via", "SIP/2.0/TCP 192.168.1.50:5062;branch=z9hG4bKreg;rport"),
                    ("From", "<sip:Zoiper@192.168.1.10>;tag=a"),
                    ("To", "<sip:Zoiper@192.168.1.10>"),
                    ("Call-ID", "reg-stale"),
                    ("CSeq", "2 REGISTER"),
                    ("Contact", "<sip:Zoiper@192.168.1.50:5060;transport=tcp>;expires=0"),
                    ("Authorization", authorization),
                ],
                b"",
            )
        )
        self.assertEqual((await registrar.handle_register(stale, ("192.168.1.50", 5062), "TCP")).status, 200)

        entries = registrar.registered_roster_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].sip_uri, "sip:Zoiper@192.168.1.50:5062;transport=tcp")

    async def test_registered_softphone_roster_entry_carries_group_membership(self) -> None:
        registrar = sip_registrar.SipRegistrar(
            enabled=True,
            accounts=[
                sip_registrar.SipAccount(
                    "Zoiper",
                    "Zoiper",
                    "secret",
                    conference_group="CG Casa",
                    conference_ring=True,
                    ring_group="RG Casa",
                )
            ],
            local_ip="192.168.1.10",
            local_sip_port=5060,
        )
        registrar.registrations["Zoiper"] = sip_registrar.SipRegistration(
            username="Zoiper",
            contact_uri="sip:Zoiper@192.168.1.50:5062;transport=udp",
            source_host="192.168.1.50",
            source_port=5062,
            transport="UDP",
            expires_at=9999999999,
        )

        entries = registrar.registered_roster_entries()

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].metadata["conference_group"], "CG Casa")
        self.assertTrue(entries[0].metadata["conference_ring"])
        self.assertEqual(entries[0].metadata["ring_group"], "RG Casa")

    async def test_register_with_active_and_expired_contacts_keeps_active_binding(self) -> None:
        registrar = sip_registrar.SipRegistrar(
            enabled=True,
            accounts=[sip_registrar.SipAccount("Zoiper", "Zoiper", "secret")],
            local_ip="192.168.1.10",
            local_sip_port=5060,
        )
        request_uri = "sip:192.168.1.10;transport=tcp"
        challenge = registrar._challenge()[1]
        authorization = sip_auth.build_digest_authorization(
            challenge_header=challenge,
            username="Zoiper",
            password="secret",
            method="REGISTER",
            uri=request_uri,
        )
        request = sip.parse_message(
            sip.build_request(
                "REGISTER",
                request_uri,
                [
                    ("Via", "SIP/2.0/TCP 192.168.1.50:5062;branch=z9hG4bKreg;rport"),
                    ("From", "<sip:Zoiper@192.168.1.10>;tag=a"),
                    ("To", "<sip:Zoiper@192.168.1.10>"),
                    ("Call-ID", "reg-multi"),
                    ("CSeq", "3 REGISTER"),
                    ("Contact", "<sip:Zoiper@192.168.1.50:5060;transport=tcp>;expires=0"),
                    ("Contact", "<sip:Zoiper@192.168.1.50:5062;transport=tcp>"),
                    ("Expires", "120"),
                    ("Authorization", authorization),
                ],
                b"",
            )
        )

        result = await registrar.handle_register(request, ("192.168.1.50", 5062), "TCP")

        self.assertEqual(result.status, 200)
        entries = registrar.registered_roster_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].sip_uri, "sip:Zoiper@192.168.1.50:5062;transport=tcp")

    def test_sip_account_does_not_publish_as_phonebook_contact_when_not_registered(self) -> None:
        registrar = sip_registrar.SipRegistrar(
            enabled=True,
            accounts=[sip_registrar.SipAccount("Zoiper", "Zoiper", "secret")],
            local_ip="192.168.1.10",
            local_sip_port=5060,
        )

        self.assertEqual(registrar.roster_entries(), [])
        self.assertEqual(registrar.registered_roster_entries(), [])

    def test_registered_softphone_entry_is_sip_uri_contact(self) -> None:
        registrar = sip_registrar.SipRegistrar(
            enabled=True,
            accounts=[sip_registrar.SipAccount("Zoiper", "Zoiper", "secret", extension="201")],
            local_ip="192.168.1.10",
            local_sip_port=5060,
        )
        registrar.registrations["Zoiper"] = sip_registrar.SipRegistration(
            username="Zoiper",
            contact_uri="sip:Zoiper@192.168.1.50:5062;transport=tcp",
            source_host="192.168.1.50",
            source_port=5062,
            transport="TCP",
            expires_at=9999999999,
        )

        entries = registrar.registered_roster_entries()

        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].id, "Zoiper")
        self.assertEqual(entries[0].sip_uri, "sip:Zoiper@192.168.1.50:5062;transport=tcp")
        self.assertEqual(entries[0].extension, "201")
        self.assertTrue(entries[0].metadata["registered"])
        self.assertNotIn("softphone", entries[0].metadata)
        by_name = router.resolve_ha_router("Zoiper", entries, trunk_ready=False)
        by_extension = router.resolve_ha_router("201", entries, trunk_ready=False)
        self.assertEqual(by_name.action, router.RouteAction.FORWARD)
        self.assertEqual(by_extension.action, router.RouteAction.FORWARD)
        self.assertEqual(by_extension.target, "Zoiper")
        self.assertEqual(by_extension.sip_uri, "sip:Zoiper@192.168.1.50:5062;transport=tcp")

    def test_account_without_registration_is_not_a_callable_roster_entry(self) -> None:
        registrar = sip_registrar.SipRegistrar(
            enabled=True,
            accounts=[sip_registrar.SipAccount("Zoiper", "Zoiper", "secret")],
            local_ip="192.168.1.10",
            local_sip_port=5060,
        )

        entries = registrar.registered_roster_entries()
        decision = router.resolve_ha_router("Zoiper", entries, trunk_ready=False)

        self.assertEqual(entries, [])
        self.assertEqual(decision.action, router.RouteAction.REJECT)
        self.assertEqual(decision.status, 404)
        self.assertEqual(decision.reason, router.RouteReason.ROUTE_NOT_FOUND)

    def test_sip_uri_parser_accepts_name_addr_with_header_params(self) -> None:
        parsed = sip.parse_sip_uri("<sip:Zoiper@192.168.1.10:41171;transport=udp>;tag=7bc04a5b")

        self.assertEqual(parsed.user, "Zoiper")
        self.assertEqual(parsed.host, "192.168.1.10")
        self.assertEqual(parsed.port, 41171)
        self.assertEqual(dict(parsed.params)["transport"], "udp")

    async def test_register_accepts_host_only_request_uri_from_baresip(self) -> None:
        registrar = sip_registrar.SipRegistrar(
            enabled=True,
            accounts=[sip_registrar.SipAccount("SmartphoneDany", "Smartphone Dany", "secret")],
            local_ip="192.168.1.10",
            local_sip_port=5060,
        )
        request_uri = "sip:192.168.1.10;transport=tcp"
        base_headers = [
            ("Via", "SIP/2.0/TCP 192.168.1.50:49258;branch=z9hG4bKreg;rport"),
            ("From", '"Smartphone Dany" <sip:SmartphoneDany@192.168.1.10>;tag=a'),
            ("To", "<sip:SmartphoneDany@192.168.1.10>"),
            ("Call-ID", "reg-host-only"),
            ("CSeq", "1 REGISTER"),
            ("Contact", "<sip:SmartphoneDany@192.168.1.50:49258;transport=tcp>"),
            ("Expires", "120"),
        ]
        challenge_req = sip.parse_message(sip.build_request("REGISTER", request_uri, base_headers, b""))
        challenge = await registrar.handle_register(challenge_req, ("192.168.1.50", 49258), "TCP")
        self.assertEqual(challenge.status, 401)
        authorization = sip_auth.build_digest_authorization(
            challenge_header=dict(challenge.headers)["WWW-Authenticate"],
            username="SmartphoneDany",
            password="secret",
            method="REGISTER",
            uri=request_uri,
        )
        ok_req = sip.parse_message(
            sip.build_request("REGISTER", request_uri, base_headers + [("Authorization", authorization)], b"")
        )
        ok = await registrar.handle_register(ok_req, ("192.168.1.50", 49258), "TCP")
        self.assertEqual(ok.status, 200)
        self.assertEqual(
            registrar.roster_entries()[0].sip_uri,
            "sip:SmartphoneDany@192.168.1.50:49258;transport=tcp",
        )


class SipBridgeTest(unittest.IsolatedAsyncioTestCase):
    async def test_local_browser_loopback_latches_ephemeral_rtp_port_bidirectionally(self) -> None:
        local = "127.0.0.1"
        with _reserved_udp_ports(3) as ports:
            relay_left_port, relay_right_port, destination_port = ports
        audio = audio_format.AudioFormat(16000, "s16le", 1, 32)

        class Capture(asyncio.DatagramProtocol):
            def __init__(self) -> None:
                self.queue: asyncio.Queue[tuple[bytes, tuple[str, int]]] = asyncio.Queue()

            def datagram_received(self, data: bytes, addr) -> None:
                self.queue.put_nowait((data, addr))

        loop = asyncio.get_running_loop()
        browser = Capture()
        destination = Capture()
        browser_transport, _ = await loop.create_datagram_endpoint(
            lambda: browser,
            local_addr=(local, 0),
        )
        destination_transport, _ = await loop.create_datagram_endpoint(
            lambda: destination,
            local_addr=(local, destination_port),
        )
        dtmf_events: list[tuple[str, str, str]] = []
        relay = sip_rtp_bridge.SipRtpRelay(
            left=sip_rtp_bridge.RtpPeer(local, 0, 96, audio, dtmf_payload_type=101),
            right=sip_rtp_bridge.RtpPeer(local, destination_port, 96, audio, dtmf_payload_type=101),
            left_port=relay_left_port,
            right_port=relay_right_port,
            on_dtmf=lambda side, digit, transport: dtmf_events.append((side, digit, transport)),
        )

        def frame(*, sequence: int, ssrc: int) -> bytes:
            return rtp.build_packet(
                rtp.RtpPacket(
                    payload_type=96,
                    sequence=sequence,
                    timestamp=sequence * audio.nominal_frame_samples,
                    ssrc=ssrc,
                    payload=bytes(audio.nominal_frame_bytes),
                )
            )

        try:
            await relay.start()
            browser_port = int(browser_transport.get_extra_info("sockname")[1])
            browser_transport.sendto(frame(sequence=1, ssrc=101), (local, relay_left_port))
            forwarded, _ = await asyncio.wait_for(destination.queue.get(), timeout=1.0)
            self.assertEqual(rtp.parse_packet(forwarded).payload_type, 96)
            self.assertEqual(relay.left.port, browser_port)

            dtmf_packet = rtp.build_packet(
                rtp.RtpPacket(
                    payload_type=101,
                    sequence=2,
                    timestamp=audio.nominal_frame_samples,
                    ssrc=101,
                    payload=bytes((1, 0x80, 0x01, 0x40)),
                )
            )
            browser_transport.sendto(dtmf_packet, (local, relay_left_port))
            browser_transport.sendto(dtmf_packet, (local, relay_left_port))
            await asyncio.sleep(0.05)
            self.assertEqual(dtmf_events, [("left", "1", "rtp_event")])
            self.assertTrue(destination.queue.empty())

            destination_transport.sendto(frame(sequence=3, ssrc=202), (local, relay_right_port))
            returned, _ = await asyncio.wait_for(browser.queue.get(), timeout=1.0)
            self.assertEqual(rtp.parse_packet(returned).payload_type, 96)
            self.assertGreaterEqual(relay.forwarded, 2)
        finally:
            await relay.stop()
            browser_transport.close()
            destination_transport.close()

    async def test_busy_bridge_target_returns_terminal_response_without_ringing(self) -> None:
        local = "127.0.0.1"
        with _reserved_udp_ports(4) as ports:
            ha_sip, caller_rtp, dest_sip, caller_sip = ports
        audio = audio_format.AudioFormat(16000, "s16le", 1, 32)
        stats = {"dest_invites": 0}

        async def dest_invite(invite):
            stats["dest_invites"] += 1
            return sip_listener.SipInviteResult(
                486,
                "Busy Here",
                decline_reason="busy",
            )

        async def ha_invite(invite):
            entries = [
                roster.RosterEntry(id="HA", address=local, metadata={"sip_port": ha_sip}),
                roster.RosterEntry(id="Cucina", address=local, metadata={"sip_port": dest_sip, "sip_transport": "udp"}),
            ]
            decision = router.resolve_ha_router(invite.target, entries, trunk_ready=False)
            self.assertIsNotNone(decision.entry)
            dest_client = sip_client.SipCallClient(
                local_ip=local,
                local_name=invite.caller or "HA",
                local_sip_port=ha_sip,
                local_rtp_port=caller_rtp + 2,
                supported_formats=[invite.selected_format.audio_format],
            )
            try:
                result = await dest_client.invite(
                    target=decision.entry.id,
                    remote_host=decision.entry.address,
                    remote_sip_port=decision.entry.metadata["sip_port"],
                )
            finally:
                await dest_client.close()
            self.assertNotEqual(result, "ringing")
            return sip_listener.SipInviteResult(
                486,
                "Busy Here",
                decline_reason=result if result != "sip_486" else "busy",
            )

        dest_server = sip_listener.SipUdpServer(
            host=local,
            port=dest_sip,
            local_ip=local,
            local_rtp_port=caller_rtp + 4,
            supported_formats=[audio],
            on_invite=dest_invite,
        )
        ha_server = sip_listener.SipUdpServer(
            host=local,
            port=ha_sip,
            local_ip=local,
            local_rtp_port=caller_rtp + 6,
            supported_formats=[audio],
            on_invite=ha_invite,
        )
        self.assertTrue(await dest_server.start())
        self.assertTrue(await ha_server.start())
        caller = sip_client.SipCallClient(
            local_ip=local,
            local_name="Spotpear",
            local_sip_port=caller_sip,
            local_rtp_port=caller_rtp,
            supported_formats=[audio],
        )
        try:
            self.assertEqual(
                await caller.invite(target="Cucina", remote_host=local, remote_sip_port=ha_sip),
                "busy",
            )
            self.assertIsNone(caller.dialog)
            self.assertEqual(stats["dest_invites"], 1)
        finally:
            await caller.close()
            await ha_server.stop()
            await dest_server.stop()

    async def test_symbolic_target_bridges_through_ha_with_rtp_relay(self) -> None:
        local = "127.0.0.1"
        with _reserved_udp_ports(7) as ports:
            ha_sip, caller_rtp, dest_sip, dest_rtp, ha_rtp_left, ha_rtp_right, caller_sip = ports
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
                roster.RosterEntry(id="HA", address=local, metadata={"sip_port": ha_sip}),
                roster.RosterEntry(id="Cucina", address=local, metadata={"sip_port": dest_sip, "sip_transport": "udp"}),
            ]
            decision = router.resolve_ha_router(invite.target, entries, trunk_ready=False)
            self.assertEqual(decision.sip_uri, f"sip:Cucina@{local}:{dest_sip};transport=udp")
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
            self.assertEqual(result, "in_call")
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
            local_sip_port=caller_sip,
            local_rtp_port=caller_rtp,
            supported_formats=[audio],
        )
        try:
            self.assertEqual(await caller.invite(target="Cucina", remote_host=local, remote_sip_port=ha_sip), "in_call")
            assert caller.dialog is not None
            caller_rtp_proto.remote = (
                caller.dialog.remote_rtp_host,
                caller.dialog.remote_rtp_port,
                caller.dialog.selected_format.payload_type,
            )
            deadline = asyncio.get_running_loop().time() + 3.0
            while (
                asyncio.get_running_loop().time() < deadline
                and (stats["caller_rtp_rx"] == 0 or stats["dest_rtp_rx"] == 0)
            ):
                await asyncio.sleep(0.05)
            self.assertEqual(stats["dest_invites"], 1)
            assert relay is not None
            relay_snapshot = relay.snapshot()
            self.assertGreater(stats["caller_rtp_rx"], 0, relay_snapshot)
            self.assertGreater(stats["dest_rtp_rx"], 0, relay_snapshot)
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


class SipTcpProfileTest(unittest.IsolatedAsyncioTestCase):
    async def test_tcp_listener_accepts_pcm_invite(self) -> None:
        local = "127.0.0.1"
        with _reserved_udp_ports(2) as ports:
            sip_port, rtp_port = ports
        audio = audio_format.AudioFormat(16000, "s16le", 1, 32)
        seen = {"invite": False}

        async def on_invite(invite):
            seen["invite"] = True
            answer = sdp.build_answer_directional(
                local,
                local,
                rtp_port,
                invite.send_format,
                invite.recv_format,
            )
            return sip_listener.SipInviteResult(200, "OK", answer_sdp=answer)

        server = sip_listener.SipTcpServer(
            host=local,
            port=sip_port,
            local_ip=local,
            local_rtp_port=rtp_port,
            supported_formats=[audio],
            on_invite=on_invite,
        )
        self.assertTrue(await server.start())
        reader, writer = await asyncio.open_connection(local, sip_port)
        try:
            body = sdp.build_offer(local, local, rtp_port + 2, [audio]).encode()
            raw = sip.build_request(
                "INVITE",
                f"sip:HA@{local}:{sip_port}",
                [
                    ("Via", f"SIP/2.0/TCP {local}:43210;branch=z9hG4bKtcp;rport"),
                    ("From", f"<sip:ESP@{local}:43210>;tag=src"),
                    ("To", f"<sip:HA@{local}:{sip_port}>"),
                    ("Call-ID", "tcp-call-1"),
                    ("CSeq", "1 INVITE"),
                    ("Contact", f"<sip:ESP@{local}:43210>"),
                    ("Content-Type", "application/sdp"),
                    ("X-Voip-Stack-Caller-Name", "ESP"),
                    ("X-Voip-Stack-Dest-Name", "HA"),
                ],
                body,
            )
            writer.write(raw)
            await writer.drain()
            first_raw = await sip_listener._read_sip_stream_message(reader)
            second_raw = await sip_listener._read_sip_stream_message(reader)
            assert first_raw is not None and second_raw is not None
            first = sip.parse_message(first_raw)
            second = sip.parse_message(second_raw)
            statuses = {first.status_code, second.status_code}
            self.assertEqual(statuses, {100, 200})
            final = first if first.status_code == 200 else second
            self.assertIn(b"m=audio", final.body)
            self.assertTrue(seen["invite"])
        finally:
            writer.close()
            await writer.wait_closed()
            await server.stop()

    async def test_tcp_client_establishes_pcm_dialog(self) -> None:
        local = "127.0.0.1"
        with _reserved_udp_ports(3) as ports:
            sip_port, server_rtp, client_rtp = ports
        audio = audio_format.AudioFormat(16000, "s16le", 1, 32)

        async def on_invite(invite):
            answer = sdp.build_answer_directional(
                local,
                local,
                server_rtp,
                invite.send_format,
                invite.recv_format,
            )
            return sip_listener.SipInviteResult(200, "OK", answer_sdp=answer)

        server = sip_listener.SipTcpServer(
            host=local,
            port=sip_port,
            local_ip=local,
            local_rtp_port=server_rtp,
            supported_formats=[audio],
            on_invite=on_invite,
        )
        self.assertTrue(await server.start())
        client = sip_client.SipCallClient(
            local_ip=local,
            local_name="Casa",
            local_sip_port=5060,
            local_rtp_port=client_rtp,
            supported_formats=[audio],
            signaling_transport="TCP",
        )
        try:
            self.assertEqual(
                await client.invite(target="ESP", remote_host=local, remote_sip_port=sip_port),
                "in_call",
            )
            self.assertIsNotNone(client.dialog)
            assert client.dialog is not None
            self.assertEqual(client.dialog.remote_rtp_port, server_rtp)
            self.assertNotEqual(client.local_sip_port, 5060)
        finally:
            client.bye()
            await client.close()
            await server.stop()


if __name__ == "__main__":
    unittest.main()
