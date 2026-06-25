"""SDP offer/answer helpers for RTP PCM used by the VoIP intercom profile."""

from __future__ import annotations

from dataclasses import dataclass

from .audio_format import AudioFormat, PcmFormat


class SdpError(ValueError):
    """Malformed or unsupported SDP."""


@dataclass(frozen=True, slots=True)
class RtpPcmFormat:
    payload_type: int
    encoding: str
    sample_rate: int
    channels: int
    frame_ms: int = 20

    @property
    def bits(self) -> int:
        if self.encoding == "L16":
            return 16
        if self.encoding == "L24":
            return 24
        raise SdpError(f"unsupported RTP PCM encoding {self.encoding}")

    @property
    def audio_format(self) -> AudioFormat:
        pcm = PcmFormat.S16LE if self.encoding == "L16" else PcmFormat.S24LE
        return AudioFormat(self.sample_rate, pcm, self.channels, self.frame_ms)


def audio_format_to_rtp(fmt: AudioFormat, payload_type: int) -> RtpPcmFormat:
    if not 96 <= int(payload_type) <= 127:
        raise SdpError("phase-1 PCM uses dynamic RTP payload types 96-127")
    if fmt.channels != 1:
        raise SdpError("phase-1 ESP RTP PCM is mono only")
    if fmt.pcm_format == PcmFormat.S16LE:
        encoding = "L16"
    elif fmt.pcm_format in (PcmFormat.S24LE, PcmFormat.S24LE_IN_S32):
        encoding = "L24"
    else:
        raise SdpError(f"{fmt.pcm_format.value} has no phase-1 RTP PCM mapping")
    return RtpPcmFormat(int(payload_type), encoding, fmt.sample_rate, fmt.channels, fmt.frame_ms)


def build_offer(origin_ip: str, media_ip: str, media_port: int, formats: list[AudioFormat]) -> str:
    if not formats:
        raise SdpError("SDP offer requires at least one PCM format")
    rtp_formats = [audio_format_to_rtp(fmt, 96 + i) for i, fmt in enumerate(formats)]
    payloads = " ".join(str(fmt.payload_type) for fmt in rtp_formats)
    lines = [
        "v=0",
        f"o=- 0 0 IN IP4 {origin_ip}",
        "s=Intercom Native",
        f"c=IN IP4 {media_ip}",
        "t=0 0",
        f"m=audio {int(media_port)} RTP/AVP {payloads}",
    ]
    for fmt in rtp_formats:
        lines.append(f"a=rtpmap:{fmt.payload_type} {fmt.encoding}/{fmt.sample_rate}/{fmt.channels}")
    lines.append(f"a=ptime:{rtp_formats[0].frame_ms}")
    lines.append("a=sendrecv")
    return "\r\n".join(lines) + "\r\n"


def parse_sdp(sdp: str | bytes) -> dict:
    if isinstance(sdp, bytes):
        sdp = sdp.decode("utf-8", errors="strict")
    session_conn = ""
    media_port = 0
    payload_order: list[int] = []
    rtpmap: dict[int, tuple[str, int, int]] = {}
    ptime = 20
    in_audio = False
    for raw in sdp.replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("c=IN IP4 "):
            session_conn = line.removeprefix("c=IN IP4 ").strip()
        elif line.startswith("m="):
            in_audio = False
            parts = line.split()
            if len(parts) >= 4 and parts[0] == "m=audio" and parts[2] == "RTP/AVP":
                media_port = int(parts[1])
                payload_order = [int(p) for p in parts[3:]]
                in_audio = True
        elif in_audio and line.startswith("a=rtpmap:"):
            left, spec = line.removeprefix("a=rtpmap:").split(None, 1)
            pt = int(left)
            bits = spec.split("/")
            if len(bits) == 2:
                encoding, rate = bits
                channels = 1
            elif len(bits) == 3:
                encoding, rate, channels_raw = bits
                channels = int(channels_raw)
            else:
                raise SdpError(f"bad rtpmap: {line}")
            rtpmap[pt] = (encoding.upper(), int(rate), channels)
        elif in_audio and line.startswith("a=ptime:"):
            ptime = int(line.removeprefix("a=ptime:").strip())
    if not session_conn or not media_port or not payload_order:
        raise SdpError("SDP missing c=, m=audio port, or payload list")
    return {
        "connection_ip": session_conn,
        "media_port": media_port,
        "payload_order": payload_order,
        "rtpmap": rtpmap,
        "ptime": ptime,
    }


def offered_pcm_formats(sdp: str | bytes) -> list[RtpPcmFormat]:
    parsed = parse_sdp(sdp)
    out: list[RtpPcmFormat] = []
    for pt in parsed["payload_order"]:
        spec = parsed["rtpmap"].get(pt)
        if spec is None:
            continue
        encoding, rate, channels = spec
        if encoding not in {"L16", "L24"}:
            continue
        out.append(RtpPcmFormat(pt, encoding, rate, channels, parsed["ptime"]))
    return out


def negotiate(remote_sdp: str | bytes, local_preferred: list[AudioFormat]) -> RtpPcmFormat | None:
    local = [audio_format_to_rtp(fmt, 96 + i) for i, fmt in enumerate(local_preferred)]
    for wanted in local:
        for offered in offered_pcm_formats(remote_sdp):
            if (
                offered.encoding == wanted.encoding
                and offered.sample_rate == wanted.sample_rate
                and offered.channels == wanted.channels
            ):
                return RtpPcmFormat(
                    offered.payload_type,
                    offered.encoding,
                    offered.sample_rate,
                    offered.channels,
                    offered.frame_ms,
                )
    return None


def build_answer(origin_ip: str, media_ip: str, media_port: int, selected: RtpPcmFormat) -> str:
    lines = [
        "v=0",
        f"o=- 0 0 IN IP4 {origin_ip}",
        "s=Intercom Native",
        f"c=IN IP4 {media_ip}",
        "t=0 0",
        f"m=audio {int(media_port)} RTP/AVP {selected.payload_type}",
        f"a=rtpmap:{selected.payload_type} {selected.encoding}/{selected.sample_rate}/{selected.channels}",
        f"a=ptime:{selected.frame_ms}",
        "a=sendrecv",
    ]
    return "\r\n".join(lines) + "\r\n"
