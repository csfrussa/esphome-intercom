#!/usr/bin/env python3
"""Golden tests for the phase-1 SIP/SDP/RTP PCM profile."""

from __future__ import annotations

import importlib.util
import asyncio
import contextlib
import socket
import sys
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
sip = _load_intercom_module("sip")
sdp = _load_intercom_module("sdp")
rtp = _load_intercom_module("rtp")
roster = _load_intercom_module("roster")
router = _load_intercom_module("router")
sip_client = _load_intercom_module("sip_client")
sip_listener = _load_intercom_module("sip_listener")
sip_registrar = _load_intercom_module("sip_registrar")
sip_auth = _load_intercom_module("sip_auth")
sip_rtp_bridge = _load_intercom_module("sip_rtp_bridge")
dtmf = _load_intercom_module("dtmf")


class SipUriTest(unittest.TestCase):
    def test_parse_host_only_uri_used_by_standard_register_routes(self) -> None:
        uri = sip.parse_sip_uri("sip:192.168.1.10;transport=tcp")
        self.assertEqual(uri.user, "")
        self.assertEqual(uri.host, "192.168.1.10")
        self.assertEqual(uri.params, (("transport", "tcp"),))
        self.assertEqual(str(uri), "sip:192.168.1.10;transport=tcp")


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


class SipClientSocketTest(unittest.IsolatedAsyncioTestCase):
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
        self.assertNotIn("a=fmtp:", offer)
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
        offer = sdp.build_offer_directional(
            "192.168.1.10",
            "192.168.1.10",
            40020,
            list(audio_format.HA_BROWSER_TX_FORMATS),
            list(audio_format.HA_BROWSER_RX_FORMATS),
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
                route_hint_source=router.RouteHintSource.DTMF,
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
                route_hint_source=router.RouteHintSource.DTMF,
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
            route_hint_source=router.RouteHintSource.DTMF,
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
            route_hint_source=router.RouteHintSource.DTMF,
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
        proto = dtmf._DtmfProtocol(101, digits.append)

        def packet(*, sequence: int, timestamp: int, ended: bool) -> bytes:
            header = bytearray(12)
            header[0] = 0x80
            header[1] = 101
            header[2:4] = int(sequence).to_bytes(2, "big")
            header[4:8] = int(timestamp).to_bytes(4, "big")
            header[8:12] = b"ssrc"
            payload = bytes([5, 0x80 if ended else 0x00, 0x00, 0xA0])
            return bytes(header) + payload

        proto.datagram_received(packet(sequence=1, timestamp=1234, ended=False), ("127.0.0.1", 5000))
        proto.datagram_received(packet(sequence=2, timestamp=1234, ended=True), ("127.0.0.1", 5000))
        proto.datagram_received(packet(sequence=3, timestamp=1234, ended=True), ("127.0.0.1", 5000))
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
