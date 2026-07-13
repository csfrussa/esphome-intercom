#!/usr/bin/env python3
"""Behavioral tests for the experimental RFC 6184 media path."""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
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
video_rtp = _load("video_rtp")
audio_format = _load("audio_format")
sdp = _load("sdp")


class H264RtpTest(unittest.TestCase):
    def _round_trip(self, access_unit: bytes, *, max_payload: int = 1200):
        packets = video_rtp.packetize_annex_b(
            access_unit,
            payload_type=102,
            sequence=65534,
            timestamp=90000,
            ssrc=0x12345678,
            max_payload=max_payload,
        )
        depacketizer = video_rtp.H264Depacketizer()
        result = None
        for packet in packets:
            result = depacketizer.push(rtp.parse_packet(rtp.build_packet(packet))) or result
        return packets, result

    def test_single_nal_and_fu_a_round_trip(self) -> None:
        sps = b"\x67\x42\xe0\x1f\x11"
        pps = b"\x68\xce\x06\xe2"
        idr = b"\x65" + bytes(range(256)) * 8
        access_unit = b"".join(video_rtp.ANNEX_B_START_CODE + item for item in (sps, pps, idr))
        packets, result = self._round_trip(access_unit, max_payload=220)
        self.assertGreater(len(packets), 3)
        self.assertTrue(packets[-1].marker)
        self.assertFalse(any(packet.marker for packet in packets[:-1]))
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(video_rtp.split_annex_b(result.data), [sps, pps, idr])
        self.assertTrue(result.key_frame)
        self.assertEqual(result.timestamp, 90000)

    def test_stap_a_is_depacketized(self) -> None:
        sps = b"\x67\x42\xe0\x1f"
        pps = b"\x68\xce\x06\xe2"
        stap = b"\x78" + len(sps).to_bytes(2, "big") + sps + len(pps).to_bytes(2, "big") + pps
        depacketizer = video_rtp.H264Depacketizer()
        first = depacketizer.push(rtp.RtpPacket(102, 1, 90, 5, stap))
        self.assertIsNone(first)
        result = depacketizer.push(rtp.RtpPacket(102, 2, 90, 5, b"\x65\x99", marker=True))
        self.assertIsNotNone(result)
        assert result is not None
        self.assertEqual(video_rtp.split_annex_b(result.data), [sps, pps, b"\x65\x99"])

    def test_cached_parameter_sets_prefix_later_idr(self) -> None:
        depacketizer = video_rtp.H264Depacketizer()
        sps = b"\x67\x42\xe0\x1f"
        pps = b"\x68\xce\x06\xe2"
        depacketizer.push(rtp.RtpPacket(102, 1, 1, 5, sps))
        depacketizer.push(rtp.RtpPacket(102, 2, 1, 5, pps, marker=True))
        result = depacketizer.push(rtp.RtpPacket(102, 3, 2, 5, b"\x65\xaa", marker=True))
        assert result is not None
        self.assertEqual(video_rtp.split_annex_b(result.data), [sps, pps, b"\x65\xaa"])

    def test_sequence_gap_discards_entire_access_unit(self) -> None:
        access_unit = video_rtp.ANNEX_B_START_CODE + b"\x65" + b"x" * 1000
        packets = video_rtp.packetize_annex_b(
            access_unit,
            payload_type=102,
            sequence=10,
            timestamp=123,
            ssrc=7,
            max_payload=200,
        )
        depacketizer = video_rtp.H264Depacketizer()
        result = None
        for packet in [packets[0], *packets[2:]]:
            result = depacketizer.push(packet) or result
        self.assertIsNone(result)
        self.assertEqual(depacketizer.sequence_gaps, 1)
        self.assertEqual(depacketizer.dropped_access_units, 1)

    def test_timestamp_change_discards_unmarked_access_unit(self) -> None:
        depacketizer = video_rtp.H264Depacketizer()
        depacketizer.push(rtp.RtpPacket(102, 1, 100, 1, b"\x61\x01"))
        result = depacketizer.push(rtp.RtpPacket(102, 2, 200, 1, b"\x61\x02", marker=True))
        self.assertIsNotNone(result)
        self.assertEqual(depacketizer.dropped_access_units, 1)

    def test_invalid_or_unbounded_payloads_are_rejected(self) -> None:
        with self.assertRaises(video_rtp.H264RtpError):
            video_rtp.split_annex_b(b"not annex b")
        with self.assertRaises(video_rtp.H264RtpError):
            video_rtp.packetize_annex_b(
                video_rtp.ANNEX_B_START_CODE + b"\x65\x00",
                payload_type=102,
                sequence=0,
                timestamp=0,
                ssrc=0,
                max_payload=2,
            )


class H264SdpTest(unittest.TestCase):
    AUDIO_FORMATS = [audio_format.AudioFormat(16000, "s16le", 1, 20)]

    def test_offer_and_answer_negotiate_h264_mode_one(self) -> None:
        offer = sdp.build_offer_directional(
            "192.168.1.10",
            "192.168.1.10",
            40000,
            self.AUDIO_FORMATS,
            self.AUDIO_FORMATS,
            video_port=40002,
            video_format=sdp.DEFAULT_H264_FORMAT,
        )
        self.assertIn("m=video 40002 RTP/AVP 102", offer)
        selected = sdp.negotiate_h264(offer)
        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected.profile_level_id, "42e01f")
        self.assertEqual(selected.packetization_mode, 1)
        parsed = sdp.parse_video_sdp(offer)
        assert parsed is not None
        self.assertEqual(parsed["connection_ip"], "192.168.1.10")
        self.assertEqual(parsed["media_port"], 40002)

    def test_answer_rejects_video_with_port_zero_when_disabled(self) -> None:
        offer = sdp.build_offer_directional(
            "192.168.1.20",
            "192.168.1.20",
            41000,
            self.AUDIO_FORMATS,
            self.AUDIO_FORMATS,
            video_port=41002,
            video_format=sdp.DEFAULT_H264_FORMAT,
        )
        audio = sdp.negotiate_directional(offer, self.AUDIO_FORMATS, self.AUDIO_FORMATS)
        assert audio is not None
        answer = sdp.build_answer_directional(
            "192.168.1.10",
            "192.168.1.10",
            40000,
            audio.send,
            audio.recv,
            remote_sdp=offer,
        )
        self.assertIn("m=audio 40000", answer)
        self.assertIn("m=video 0 RTP/AVP 102", answer)

    def test_answer_preserves_video_first_media_order_and_direction(self) -> None:
        offer = (
            "v=0\r\n"
            "o=- 0 0 IN IP4 192.168.1.20\r\n"
            "s=-\r\n"
            "c=IN IP4 192.168.1.20\r\n"
            "t=0 0\r\n"
            "m=video 41002 RTP/AVP 103\r\n"
            "a=rtpmap:103 H264/90000\r\n"
            "a=fmtp:103 profile-level-id=42e01f;packetization-mode=1\r\n"
            "a=sendonly\r\n"
            "m=audio 41000 RTP/AVP 96\r\n"
            "a=rtpmap:96 L16/16000/1\r\n"
            "a=ptime:20\r\n"
            "a=sendrecv\r\n"
        )
        video = sdp.negotiate_h264(offer)
        audio = sdp.negotiate_directional(offer, self.AUDIO_FORMATS, self.AUDIO_FORMATS)
        assert video is not None and audio is not None
        answer = sdp.build_answer_directional(
            "192.168.1.10",
            "192.168.1.10",
            40000,
            audio.send,
            audio.recv,
            remote_sdp=offer,
            video_port=40002,
            video_format=video,
        )
        self.assertLess(answer.index("m=video"), answer.index("m=audio"))
        self.assertIn("m=video 40002 RTP/AVP 103", answer)
        self.assertIn("a=recvonly", answer)

    def test_rejects_packetization_zero_and_non_baseline_profile(self) -> None:
        offer = (
            "v=0\r\nc=IN IP4 192.168.1.20\r\nt=0 0\r\n"
            "m=video 41002 RTP/AVP 102 103\r\n"
            "a=rtpmap:102 H264/90000\r\n"
            "a=fmtp:102 profile-level-id=42e01f;packetization-mode=0\r\n"
            "a=rtpmap:103 H264/90000\r\n"
            "a=fmtp:103 profile-level-id=64001f;packetization-mode=1\r\n"
        )
        self.assertEqual(sdp.offered_h264_formats(offer), [])

    def test_rejects_static_h264_payload_type(self) -> None:
        offer = (
            "v=0\r\nc=IN IP4 192.168.1.20\r\nt=0 0\r\n"
            "m=audio 41000 RTP/AVP 96\r\n"
            "a=rtpmap:96 L16/16000/1\r\na=ptime:20\r\n"
            "m=video 41002 RTP/AVP 35\r\n"
            "a=rtpmap:35 H264/90000\r\n"
            "a=fmtp:35 profile-level-id=42e01f;packetization-mode=1\r\n"
        )
        self.assertEqual(sdp.offered_h264_formats(offer), [])

    def test_does_not_select_later_video_after_unsupported_first_section(self) -> None:
        offer = (
            "v=0\r\nc=IN IP4 192.168.1.20\r\nt=0 0\r\n"
            "m=audio 41000 RTP/AVP 96\r\n"
            "a=rtpmap:96 L16/16000/1\r\na=ptime:20\r\n"
            "m=video 41002 RTP/SAVP 102\r\n"
            "a=rtpmap:102 H264/90000\r\n"
            "a=fmtp:102 profile-level-id=42e01f;packetization-mode=1\r\n"
            "m=video 41004 RTP/AVP 103\r\n"
            "a=rtpmap:103 H264/90000\r\n"
            "a=fmtp:103 profile-level-id=42e01f;packetization-mode=1\r\n"
        )
        self.assertEqual(sdp.offered_h264_formats(offer), [])
        audio = sdp.negotiate_directional(offer, self.AUDIO_FORMATS, self.AUDIO_FORMATS)
        assert audio is not None
        answer = sdp.build_answer_directional(
            "192.168.1.10",
            "192.168.1.10",
            40000,
            audio.send,
            audio.recv,
            remote_sdp=offer,
            video_port=40002,
            video_format=sdp.DEFAULT_H264_FORMAT,
        )
        self.assertIn("m=video 0 RTP/SAVP 102", answer)
        self.assertIn("m=video 0 RTP/AVP 103", answer)

    def test_baresip_answer_is_accepted(self) -> None:
        answer = (
            "v=0\r\no=- 1 2 IN IP4 192.168.1.48\r\ns=-\r\n"
            "c=IN IP4 192.168.1.48\r\nt=0 0\r\n"
            "m=audio 44652 RTP/AVP 8 0 99\r\n"
            "a=rtpmap:8 PCMA/8000\r\na=sendrecv\r\n"
            "m=video 19728 RTP/AVP 102\r\n"
            "a=rtpmap:102 H264/90000\r\n"
            "a=fmtp:102 packetization-mode=1;profile-level-id=42e01f\r\n"
            "a=sendrecv\r\na=framerate:15.00\r\na=rtcp-fb:* nack pli\r\n"
        )
        selected = sdp.negotiate_h264(answer)
        self.assertIsNotNone(selected)
        assert selected is not None
        self.assertEqual(selected.payload_type, 102)
        self.assertEqual(selected.direction, "sendrecv")

    def test_answer_cannot_select_an_unoffered_payload_type(self) -> None:
        answer = (
            "v=0\r\nc=IN IP4 192.168.1.48\r\nt=0 0\r\n"
            "m=audio 44652 RTP/AVP 8\r\n"
            "m=video 19728 RTP/AVP 103\r\n"
            "a=rtpmap:103 H264/90000\r\n"
            "a=fmtp:103 packetization-mode=1;profile-level-id=42e01f\r\n"
        )
        self.assertIsNone(sdp.negotiate_h264_answer(answer, sdp.DEFAULT_H264_FORMAT))

    def test_unsupported_media_level_connection_does_not_break_audio(self) -> None:
        offer = (
            "v=0\r\no=- 1 2 IN IP4 192.168.1.48\r\ns=-\r\n"
            "c=IN IP4 192.168.1.48\r\nt=0 0\r\n"
            "m=audio 44652 RTP/AVP 96\r\n"
            "a=rtpmap:96 L16/16000/1\r\na=ptime:20\r\na=sendrecv\r\n"
            "m=video 19728 RTP/AVP 102\r\n"
            "c=IN IP6 2001:db8::1\r\n"
            "a=rtpmap:102 H264/90000\r\n"
            "a=fmtp:102 packetization-mode=1;profile-level-id=42e01f\r\n"
        )
        audio = sdp.negotiate_directional(offer, self.AUDIO_FORMATS, self.AUDIO_FORMATS)
        self.assertIsNotNone(audio)
        self.assertEqual(sdp.negotiate_h264(offer), None)
        assert audio is not None
        answer = sdp.build_answer_directional(
            "192.168.1.10",
            "192.168.1.10",
            40000,
            audio.send,
            audio.recv,
            remote_sdp=offer,
        )
        self.assertIn("m=audio 40000 RTP/AVP 96", answer)
        self.assertIn("m=video 0 RTP/AVP 102", answer)

    def test_unsupported_session_connection_still_rejects_call(self) -> None:
        offer = (
            "v=0\r\nc=IN IP6 2001:db8::1\r\nt=0 0\r\n"
            "m=audio 44652 RTP/AVP 96\r\n"
            "a=rtpmap:96 L16/16000/1\r\na=ptime:20\r\n"
        )
        with self.assertRaises(sdp.SdpError):
            sdp.build_answer_directional(
                "192.168.1.10",
                "192.168.1.10",
                40000,
                sdp.RtpPcmFormat(96, "L16", 16000, 1, 20),
                sdp.RtpPcmFormat(96, "L16", 16000, 1, 20),
                remote_sdp=offer,
            )

    def test_answer_rejects_additional_audio_section_in_offered_order(self) -> None:
        offer = (
            "v=0\r\nc=IN IP4 192.168.1.20\r\nt=0 0\r\n"
            "m=audio 41000 RTP/AVP 96\r\n"
            "a=rtpmap:96 L16/16000/1\r\na=ptime:20\r\n"
            "m=audio 41002 RTP/AVP 97\r\n"
            "a=rtpmap:97 L16/16000/1\r\na=ptime:20\r\n"
        )
        audio = sdp.negotiate_directional(offer, self.AUDIO_FORMATS, self.AUDIO_FORMATS)
        assert audio is not None
        answer = sdp.build_answer_directional(
            "192.168.1.10",
            "192.168.1.10",
            40000,
            audio.send,
            audio.recv,
            remote_sdp=offer,
        )
        self.assertLess(answer.index("m=audio 40000"), answer.index("m=audio 0 RTP/AVP 97"))

    def test_answer_rejects_unsupported_audio_before_selected_rtp_avp(self) -> None:
        offer = (
            "v=0\r\nc=IN IP4 192.168.1.20\r\nt=0 0\r\n"
            "m=audio 40998 RTP/SAVP 96\r\n"
            "a=rtpmap:96 L16/16000/1\r\na=ptime:20\r\n"
            "m=audio 41000 RTP/AVP 97\r\n"
            "a=rtpmap:97 L16/16000/1\r\na=ptime:20\r\n"
        )
        audio = sdp.negotiate_directional(offer, self.AUDIO_FORMATS, self.AUDIO_FORMATS)
        assert audio is not None
        answer = sdp.build_answer_directional(
            "192.168.1.10",
            "192.168.1.10",
            40000,
            audio.send,
            audio.recv,
            remote_sdp=offer,
        )
        self.assertLess(answer.index("m=audio 0 RTP/SAVP 96"), answer.index("m=audio 40000 RTP/AVP 97"))


if __name__ == "__main__":
    unittest.main()
