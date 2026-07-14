from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import shutil
import socket
import sys
import types
import unittest
from unittest import mock


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


_load("const")
sdp = _load("sdp")
rtp = _load("rtp")
video_rtp = _load("video_rtp")
video_transcoder = _load("video_transcoder")


class _Hass:
    def __init__(self) -> None:
        self.data: dict = {}


class VideoTranscoderPolicyTests(unittest.IsolatedAsyncioTestCase):
    def test_ffmpeg_binary_prefers_home_assistant_manager(self) -> None:
        hass = _Hass()
        hass.data["ffmpeg"] = types.SimpleNamespace(binary=" /opt/ha/ffmpeg ")
        with mock.patch.object(video_transcoder.shutil, "which", return_value="/usr/bin/ffmpeg"):
            self.assertEqual(video_transcoder._ffmpeg_binary(hass), "/opt/ha/ffmpeg")

    def test_ffmpeg_binary_falls_back_to_path_and_fails_cleanly(self) -> None:
        hass = _Hass()
        with mock.patch.object(video_transcoder.shutil, "which", return_value="/usr/bin/ffmpeg"):
            self.assertEqual(video_transcoder._ffmpeg_binary(hass), "/usr/bin/ffmpeg")
        with mock.patch.object(video_transcoder.shutil, "which", return_value=None):
            with self.assertRaisesRegex(video_transcoder.VideoTranscoderError, "unavailable"):
                video_transcoder._ffmpeg_binary(hass)

    def test_input_sdp_normalizes_h263p_and_sanitizes_fmtp(self) -> None:
        video_format = sdp.RtpVideoFormat(
            payload_type=102,
            encoding="H263P",
            fmtp="CIF=1\r\na=sendrecv",
        )
        description = video_transcoder._input_sdp(video_format, 45678)
        self.assertIn("m=video 45678 RTP/AVP 102\r\n", description)
        self.assertIn("a=rtpmap:102 H263-1998/90000\r\n", description)
        self.assertIn("a=fmtp:102 CIF=1  a=sendrecv\r\n", description)
        self.assertEqual(description.count("a=sendrecv"), 1)

    def test_input_sdp_rejects_unknown_codec_and_oversized_fmtp(self) -> None:
        with self.assertRaisesRegex(video_transcoder.VideoTranscoderError, "unsupported"):
            video_transcoder._input_sdp(
                sdp.RtpVideoFormat(payload_type=102, encoding="THEORA"),
                45678,
            )
        with self.assertRaisesRegex(video_transcoder.VideoTranscoderError, "safety limit"):
            video_transcoder._input_sdp(
                sdp.RtpVideoFormat(
                    payload_type=102,
                    encoding="H264",
                    fmtp="x" * (video_transcoder._MAX_FMTP_LENGTH + 1),
                ),
                45678,
            )

    async def test_only_one_transcoder_can_own_the_bounded_slot(self) -> None:
        hass = _Hass()
        active = object()
        hass.data[video_transcoder.DOMAIN] = {
            video_transcoder._ACTIVE_TRANSCODER: active,
        }
        contender = video_transcoder.FfmpegVideoTranscoder(
            hass=hass,
            call_id="second",
            input_format=sdp.RtpVideoFormat(payload_type=34, encoding="H263"),
            output_port=45678,
        )
        with self.assertRaisesRegex(video_transcoder.VideoTranscoderError, "another"):
            await contender.async_start()
        self.assertIs(
            hass.data[video_transcoder.DOMAIN][video_transcoder._ACTIVE_TRANSCODER],
            active,
        )

    async def test_failed_start_releases_the_transcoder_slot(self) -> None:
        hass = _Hass()
        transcoder = video_transcoder.FfmpegVideoTranscoder(
            hass=hass,
            call_id="failed",
            input_format=sdp.RtpVideoFormat(payload_type=34, encoding="H263"),
            output_port=45678,
        )
        with mock.patch.object(
            video_transcoder,
            "_ffmpeg_binary",
            side_effect=video_transcoder.VideoTranscoderError("missing"),
        ):
            with self.assertRaisesRegex(video_transcoder.VideoTranscoderError, "missing"):
                await transcoder.async_start()
        self.assertNotIn(
            video_transcoder._ACTIVE_TRANSCODER,
            hass.data[video_transcoder.DOMAIN],
        )

    async def test_cleanup_race_still_releases_the_transcoder_slot(self) -> None:
        hass = _Hass()
        transcoder = video_transcoder.FfmpegVideoTranscoder(
            hass=hass,
            call_id="cleanup-race",
            input_format=sdp.RtpVideoFormat(payload_type=34, encoding="H263"),
            output_port=45678,
        )
        process = mock.Mock()
        process.returncode = None
        process.terminate.side_effect = ProcessLookupError
        process.wait = mock.AsyncMock(return_value=0)
        transcoder.process = process
        hass.data[video_transcoder.DOMAIN] = {
            video_transcoder._ACTIVE_TRANSCODER: transcoder,
        }

        await transcoder.async_close()

        process.wait.assert_awaited_once()
        self.assertNotIn(
            video_transcoder._ACTIVE_TRANSCODER,
            hass.data[video_transcoder.DOMAIN],
        )


@unittest.skipUnless(shutil.which("ffmpeg"), "FFmpeg is required for the transcode qualification")
class VideoTranscoderTests(unittest.IsolatedAsyncioTestCase):
    async def _qualify_codec(
        self,
        *,
        video_format,
        encoder: str,
        size: str = "320x180",
        encoder_args: tuple[str, ...] = (),
    ) -> None:
        output = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        output.setblocking(False)
        output.bind(("127.0.0.1", 0))
        output_port = int(output.getsockname()[1])
        source = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        source.setblocking(False)
        source.bind(("127.0.0.1", 0))
        source_port = int(source.getsockname()[1])
        transcoder = video_transcoder.FfmpegVideoTranscoder(
            hass=_Hass(),
            call_id=f"qualification-{video_format.encoding.lower()}",
            input_format=video_format,
            output_port=output_port,
        )
        sender = None
        input_packets = 0

        async def forward_input() -> None:
            nonlocal input_packets
            loop = asyncio.get_running_loop()
            while True:
                raw, _addr = await loop.sock_recvfrom(source, 2048)
                input_packets += 1
                transcoder.send_rtp(raw)

        forward_task = asyncio.create_task(forward_input())
        try:
            await transcoder.async_start()
            await asyncio.sleep(0.2)
            sender = await asyncio.create_subprocess_exec(
                shutil.which("ffmpeg") or "ffmpeg",
                "-hide_banner",
                "-loglevel", "error",
                "-re",
                "-f", "lavfi",
                "-i", f"testsrc2=size={size}:rate=10",
                "-t", "2.8",
                "-an",
                "-c:v", encoder,
                *encoder_args,
                "-pix_fmt", "yuv420p",
                "-g", "10",
                "-f", "rtp",
                "-payload_type", str(video_format.payload_type),
                f"rtp://127.0.0.1:{source_port}?pkt_size=1200",
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            loop = asyncio.get_running_loop()
            depacketizer = video_rtp.Vp8Depacketizer()
            packets = 0
            access_units = []
            deadline = loop.time() + 5.0
            while loop.time() < deadline and len(access_units) < 8:
                try:
                    raw, _addr = await asyncio.wait_for(loop.sock_recvfrom(output, 2048), 0.5)
                except TimeoutError:
                    continue
                packet = rtp.parse_packet(raw)
                self.assertEqual(packet.payload_type, 103)
                packets += 1
                access_unit = depacketizer.push(packet)
                if access_unit is not None:
                    access_units.append(access_unit)
            stderr = b""
            if sender.returncode is None:
                await asyncio.wait_for(sender.wait(), 5.0)
            if sender.stderr is not None:
                stderr = await sender.stderr.read()
            self.assertEqual(sender.returncode, 0, stderr.decode(errors="replace"))
            diagnostic = "\n".join(transcoder.stderr_tail)
            self.assertGreater(input_packets, 20, diagnostic)
            self.assertGreater(packets, 8, diagnostic)
            self.assertGreaterEqual(len(access_units), 3, diagnostic)
            self.assertTrue(any(item.key_frame for item in access_units))
            timestamps = [item.timestamp for item in access_units]
            self.assertEqual(timestamps, sorted(timestamps))
        finally:
            forward_task.cancel()
            await asyncio.gather(forward_task, return_exceptions=True)
            if sender is not None and sender.returncode is None:
                sender.kill()
                await sender.wait()
            await transcoder.async_close()
            output.close()
            source.close()

    async def test_supported_sip_codec_matrix_transcodes_to_vp8(self) -> None:
        formats = (
            (
                sdp.RtpVideoFormat(payload_type=34, encoding="H263"),
                "h263",
                "352x288",
                (),
            ),
            (
                sdp.RtpVideoFormat(payload_type=102, encoding="H263P"),
                "h263p",
                "352x288",
                (),
            ),
            (
                sdp.RtpVideoFormat(
                    payload_type=102,
                    encoding="H264",
                    profile_level_id="42e01f",
                    packetization_mode=1,
                ),
                "libx264",
                "320x180",
                ("-preset", "ultrafast", "-tune", "zerolatency", "-profile:v", "baseline"),
            ),
            (
                sdp.RtpVideoFormat(payload_type=102, encoding="H265"),
                "libx265",
                "320x180",
                (
                    "-preset",
                    "ultrafast",
                    "-x265-params",
                    "log-level=error:keyint=10:min-keyint=10:bframes=0:no-scenecut=1:repeat-headers=1",
                ),
            ),
        )
        for video_format, encoder, size, encoder_args in formats:
            with self.subTest(codec=video_format.encoding):
                await self._qualify_codec(
                    video_format=video_format,
                    encoder=encoder,
                    size=size,
                    encoder_args=encoder_args,
                )


if __name__ == "__main__":
    unittest.main()
