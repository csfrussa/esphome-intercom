"""Bounded FFmpeg bridge for optional SIP video transcoding.

The normal video path never imports a codec library or starts a process.  This
module is used only when the config-flow opt-in is enabled and the negotiated
SIP codec cannot be rendered directly by the browser bridge.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
import logging
import shutil
import socket
from typing import TYPE_CHECKING

from .const import DOMAIN
from .sdp import RtpVideoFormat

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant


_LOGGER = logging.getLogger(__name__)
_ACTIVE_TRANSCODER = "video_transcoder_active"
_TRANSCODER_LOCK = "video_transcoder_lock"
_MAX_FMTP_LENGTH = 1024


class VideoTranscoderError(RuntimeError):
    """The optional video transcoder could not be started or used."""


def _available_udp_port() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])
    finally:
        sock.close()


def _ffmpeg_binary(hass: HomeAssistant) -> str:
    manager = hass.data.get("ffmpeg")
    configured = str(getattr(manager, "binary", "") or "").strip()
    binary = configured or shutil.which("ffmpeg") or ""
    if not binary:
        raise VideoTranscoderError("FFmpeg is unavailable; continuing audio-only")
    return binary


def _input_sdp(video_format: RtpVideoFormat, port: int) -> str:
    encoding = str(video_format.encoding or "").upper()
    if encoding not in {"H263", "H263P", "H264", "H265", "JPEG", "VP8", "VP9", "AV1"}:
        raise VideoTranscoderError(f"unsupported FFmpeg RTP input codec {encoding}")
    rtp_encoding = {"H263P": "H263-1998"}.get(encoding, encoding)
    fmtp = str(video_format.fmtp or "").replace("\r", " ").replace("\n", " ").strip()
    if len(fmtp) > _MAX_FMTP_LENGTH:
        raise VideoTranscoderError("video fmtp exceeds safety limit")
    lines = [
        "v=0",
        "o=- 0 0 IN IP4 127.0.0.1",
        "s=VoIP Stack transcoder",
        "c=IN IP4 127.0.0.1",
        "t=0 0",
        f"m=video {int(port)} {video_format.transport_profile} {int(video_format.payload_type)}",
        f"a=rtpmap:{int(video_format.payload_type)} {rtp_encoding}/{int(video_format.clock_rate)}",
        "a=recvonly",
    ]
    if fmtp:
        lines.append(f"a=fmtp:{int(video_format.payload_type)} {fmtp}")
    return "\r\n".join(lines) + "\r\n"


@dataclass(slots=True)
class FfmpegVideoTranscoder:
    """One receive-only RTP codec conversion into browser-friendly VP8."""

    hass: HomeAssistant
    call_id: str
    input_format: RtpVideoFormat
    output_port: int
    input_port: int = 0
    process: asyncio.subprocess.Process | None = None
    _send_socket: socket.socket | None = None
    _stderr_task: asyncio.Task[None] | None = None
    stderr_tail: list[str] = field(default_factory=list, init=False)
    _released: bool = field(default=False, init=False)

    async def async_start(self) -> None:
        bucket = self.hass.data.setdefault(DOMAIN, {})
        lock = bucket.setdefault(_TRANSCODER_LOCK, asyncio.Lock())
        async with lock:
            active = bucket.get(_ACTIVE_TRANSCODER)
            if active is not None and active is not self:
                raise VideoTranscoderError("another SIP video transcode is active")
            bucket[_ACTIVE_TRANSCODER] = self
        try:
            self.input_port = _available_udp_port()
            command = [
                _ffmpeg_binary(self.hass),
                "-hide_banner",
                "-loglevel", "warning",
                "-nostdin",
                "-protocol_whitelist", "file,pipe,udp,rtp",
                "-fflags", "+nobuffer+discardcorrupt",
                "-flags", "low_delay",
                "-analyzeduration", "0",
                # SDP already declares codec and payload type. A small probe
                # prevents low-bitrate door cameras from adding seconds of
                # startup latency while FFmpeg waits for 32 KiB of RTP.
                "-probesize", "2048",
                "-f", "sdp",
                "-i", "pipe:0",
                "-map", "0:v:0",
                "-an",
                "-sn",
                "-dn",
                "-vf", "fps=15,scale='min(1280,iw)':-2:force_original_aspect_ratio=decrease",
                "-pix_fmt", "yuv420p",
                "-c:v", "libvpx",
                "-deadline", "realtime",
                "-cpu-used", "8",
                "-threads", "1",
                "-b:v", "700k",
                "-maxrate", "900k",
                "-bufsize", "1400k",
                "-g", "30",
                "-keyint_min", "30",
                "-f", "rtp",
                "-payload_type", "103",
                f"rtp://127.0.0.1:{int(self.output_port)}?pkt_size=1200",
            ]
            self.process = await asyncio.create_subprocess_exec(
                *command,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.DEVNULL,
                stderr=asyncio.subprocess.PIPE,
            )
            assert self.process.stdin is not None
            self.process.stdin.write(_input_sdp(self.input_format, self.input_port).encode())
            await self.process.stdin.drain()
            self.process.stdin.close()
            self._send_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._send_socket.setblocking(False)
            self._stderr_task = asyncio.create_task(self._drain_stderr())
            _LOGGER.info(
                "Started optional SIP video transcode call_id=%s input=%s loopback=%s output=VP8/%s",
                self.call_id,
                self.input_format.wire_token(),
                self.input_port,
                self.output_port,
            )
        except Exception:
            await self.async_close()
            raise

    def send_rtp(self, data: bytes) -> None:
        if self._send_socket is None or self.process is None or self.process.returncode is not None:
            raise VideoTranscoderError("FFmpeg video transcoder stopped")
        self._send_socket.sendto(data, ("127.0.0.1", int(self.input_port)))

    async def _drain_stderr(self) -> None:
        process = self.process
        if process is None or process.stderr is None:
            return
        while line := await process.stderr.readline():
            text = line.decode(errors="replace").rstrip()
            self.stderr_tail.append(text)
            del self.stderr_tail[:-20]
            _LOGGER.debug("FFmpeg SIP video: %s", text)

    async def async_close(self) -> None:
        if self._released:
            return
        self._released = True
        try:
            if self._send_socket is not None:
                self._send_socket.close()
                self._send_socket = None
            process = self.process
            self.process = None
            if process is not None:
                if process.returncode is None:
                    with contextlib.suppress(ProcessLookupError):
                        process.terminate()
                try:
                    await asyncio.wait_for(process.wait(), timeout=2.0)
                except TimeoutError:
                    with contextlib.suppress(ProcessLookupError):
                        process.kill()
                    await process.wait()
            if self._stderr_task is not None:
                self._stderr_task.cancel()
                await asyncio.gather(self._stderr_task, return_exceptions=True)
                self._stderr_task = None
        finally:
            # Never strand the single bounded transcode slot because FFmpeg
            # exited between the returncode check and process cleanup.
            bucket = self.hass.data.setdefault(DOMAIN, {})
            lock = bucket.setdefault(_TRANSCODER_LOCK, asyncio.Lock())
            async with lock:
                if bucket.get(_ACTIVE_TRANSCODER) is self:
                    bucket.pop(_ACTIVE_TRANSCODER, None)
        _LOGGER.info("Stopped optional SIP video transcode call_id=%s", self.call_id)
