"""SDP offer/answer helpers for RTP PCM used by the VoIP intercom profile."""

from __future__ import annotations

from dataclasses import dataclass

from .audio_format import AudioFormat, PcmFormat, UDP_SAFE_PAYLOAD_BYTES


class SdpError(ValueError):
    """Malformed or unsupported SDP."""


MAX_RTP_OFFER_FORMATS = 12
_PREFERRED_RTP_AUDIO_KEYS = {
    (48000, PcmFormat.S16LE, 1, 10): 0,
    (32000, PcmFormat.S16LE, 1, 10): 1,
    (24000, PcmFormat.S16LE, 1, 20): 2,
    (16000, PcmFormat.S16LE, 1, 32): 3,
    (16000, PcmFormat.S16LE, 1, 20): 4,
    (16000, PcmFormat.S16LE, 1, 10): 5,
    (8000, PcmFormat.S16LE, 1, 20): 6,
}


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


@dataclass(frozen=True, slots=True)
class RtpPcmDirection:
    send: RtpPcmFormat
    recv: RtpPcmFormat

    @property
    def selected_format(self) -> RtpPcmFormat:
        return self.send


def audio_format_to_rtp(fmt: AudioFormat, payload_type: int) -> RtpPcmFormat:
    if not 96 <= int(payload_type) <= 127:
        raise SdpError("phase-1 PCM uses dynamic RTP payload types 96-127")
    if fmt.channels != 1:
        raise SdpError("phase-1 ESP RTP PCM is mono only")
    if not fmt.fits_udp_payload(UDP_SAFE_PAYLOAD_BYTES):
        raise SdpError(
            f"RTP PCM frame too large for intercom-sip-pcm/1: "
            f"{fmt.wire_token()} is {fmt.nominal_frame_bytes} bytes; "
            f"max is {UDP_SAFE_PAYLOAD_BYTES}"
        )
    if fmt.pcm_format == PcmFormat.S16LE:
        encoding = "L16"
    elif fmt.pcm_format in (PcmFormat.S24LE, PcmFormat.S24LE_IN_S32):
        encoding = "L24"
    else:
        raise SdpError(f"{fmt.pcm_format.value} has no phase-1 RTP PCM mapping")
    return RtpPcmFormat(int(payload_type), encoding, fmt.sample_rate, fmt.channels, fmt.frame_ms)


def is_rtp_pcm_mappable(fmt: AudioFormat) -> bool:
    if fmt.channels != 1:
        return False
    if not fmt.fits_udp_payload(UDP_SAFE_PAYLOAD_BYTES):
        return False
    return fmt.pcm_format in {PcmFormat.S16LE, PcmFormat.S24LE, PcmFormat.S24LE_IN_S32}


def rtp_mappable_formats(formats: list[AudioFormat]) -> list[AudioFormat]:
    return [fmt for fmt in formats if is_rtp_pcm_mappable(fmt)]


def _rtp_offer_rank(fmt: AudioFormat) -> tuple[int, int, int]:
    key = _format_key(fmt)
    if key in _PREFERRED_RTP_AUDIO_KEYS:
        return (0, _PREFERRED_RTP_AUDIO_KEYS[key], 0)
    pcm_rank = {
        PcmFormat.S16LE: 0,
        PcmFormat.S24LE: 1,
        PcmFormat.S24LE_IN_S32: 2,
        PcmFormat.S32LE: 3,
    }[fmt.pcm_format]
    frame_rank = {10: 0, 20: 1, 32: 2}.get(fmt.frame_ms, 9)
    return (1 + pcm_rank, -fmt.sample_rate, frame_rank)


def rtp_offer_formats(formats: list[AudioFormat]) -> list[AudioFormat]:
    ranked = sorted(_dedupe_formats(rtp_mappable_formats(formats)), key=_rtp_offer_rank)
    if not ranked:
        return []
    # a=ptime is media-level in the SIP/SDP profile. Keep one packetization
    # interval per m=audio instead of smuggling per-payload ptime in fmtp.
    frame_ms = ranked[0].frame_ms
    return [fmt for fmt in ranked if fmt.frame_ms == frame_ms][:MAX_RTP_OFFER_FORMATS]


def _format_key(fmt: AudioFormat) -> tuple[int, PcmFormat, int, int]:
    wire_pcm = PcmFormat.S24LE if fmt.pcm_format == PcmFormat.S24LE_IN_S32 else fmt.pcm_format
    return (fmt.sample_rate, wire_pcm, fmt.channels, fmt.frame_ms)


def _dedupe_formats(formats: list[AudioFormat]) -> list[AudioFormat]:
    seen: set[tuple[int, PcmFormat, int, int]] = set()
    out: list[AudioFormat] = []
    for fmt in formats:
        key = _format_key(fmt)
        if key in seen:
            continue
        seen.add(key)
        out.append(fmt)
    return out


def _rtp_compatible_audio(offered: RtpPcmFormat, local: AudioFormat) -> RtpPcmFormat | None:
    if not is_rtp_pcm_mappable(local):
        return None
    if offered.frame_ms not in (0, local.frame_ms):
        return None
    wanted = audio_format_to_rtp(local, offered.payload_type)
    if (
        offered.encoding == wanted.encoding
        and offered.sample_rate == wanted.sample_rate
        and offered.channels == wanted.channels
    ):
        return wanted
    return None


def _rtp_matches_audio(offered: RtpPcmFormat, local: AudioFormat) -> bool:
    return _rtp_compatible_audio(offered, local) is not None


def _best_offered_match(offered: list[RtpPcmFormat], local_preferred: list[AudioFormat]) -> RtpPcmFormat | None:
    for local in local_preferred:
        for offered_fmt in offered:
            selected = _rtp_compatible_audio(offered_fmt, local)
            if selected is not None:
                return selected
    return None


def _first_offered_match(
    offered: list[RtpPcmFormat],
    local_preferred: list[AudioFormat],
    *,
    skip_payload_type: int | None = None,
) -> RtpPcmFormat | None:
    for offered_fmt in offered:
        if skip_payload_type is not None and offered_fmt.payload_type == skip_payload_type:
            continue
        for local in local_preferred:
            selected = _rtp_compatible_audio(offered_fmt, local)
            if selected is not None:
                return selected
    return None


def build_offer(origin_ip: str, media_ip: str, media_port: int, formats: list[AudioFormat]) -> str:
    return build_offer_directional(origin_ip, media_ip, media_port, formats, formats)


def build_offer_directional(
    origin_ip: str,
    media_ip: str,
    media_port: int,
    send_formats: list[AudioFormat],
    recv_formats: list[AudioFormat],
) -> str:
    formats = rtp_offer_formats([*(send_formats or []), *(recv_formats or [])])
    if not formats:
        raise SdpError("SDP offer requires at least one RTP-mappable PCM format")
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
    lines.append(f"a=maxptime:{rtp_formats[0].frame_ms}")
    lines.append("a=sendrecv")
    return "\r\n".join(lines) + "\r\n"


def parse_sdp(sdp: str | bytes) -> dict:
    if isinstance(sdp, bytes):
        sdp = sdp.decode("utf-8", errors="strict")
    session_conn = ""
    media_port = 0
    payload_order: list[int] = []
    rtpmap: dict[int, tuple[str, int, int]] = {}
    ptime = 0
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
    return _best_offered_match(offered_pcm_formats(remote_sdp), local_preferred)


def negotiate_directional(
    remote_sdp: str | bytes,
    local_send_preferred: list[AudioFormat],
    local_recv_preferred: list[AudioFormat],
) -> RtpPcmDirection | None:
    send = negotiate(remote_sdp, local_send_preferred)
    recv = negotiate(remote_sdp, local_recv_preferred)
    if send is None or recv is None:
        return None
    return RtpPcmDirection(send=send, recv=recv)


def negotiate_answer_directional(
    remote_sdp: str | bytes,
    local_send_preferred: list[AudioFormat],
    local_recv_preferred: list[AudioFormat],
) -> RtpPcmDirection | None:
    """Negotiate an SDP answer for an outbound call.

    Our ESP SIP phones answer asymmetric PCM offers with the payload they
    receive first and the payload they transmit second. Prefer that answer
    order for RX so a 48k-to-ESP / 16k-from-ESP call does not drop the remote
    RTP stream by expecting the first payload in both directions.
    """
    offered = offered_pcm_formats(remote_sdp)
    send = _first_offered_match(offered, local_send_preferred)
    if send is None:
        return None

    recv = _first_offered_match(
        offered,
        local_recv_preferred,
        skip_payload_type=send.payload_type if len(offered) > 1 else None,
    )
    if recv is None:
        recv = _best_offered_match([send], local_recv_preferred)
    if recv is None:
        return None
    return RtpPcmDirection(send=send, recv=recv)


def build_answer(origin_ip: str, media_ip: str, media_port: int, selected: RtpPcmFormat) -> str:
    return build_answer_directional(origin_ip, media_ip, media_port, selected, selected)


def build_answer_directional(
    origin_ip: str,
    media_ip: str,
    media_port: int,
    send: RtpPcmFormat,
    recv: RtpPcmFormat,
) -> str:
    selected = []
    seen: set[int] = set()
    for fmt in (send, recv):
        if fmt.payload_type in seen:
            continue
        seen.add(fmt.payload_type)
        selected.append(fmt)
    payloads = " ".join(str(fmt.payload_type) for fmt in selected)
    lines = [
        "v=0",
        f"o=- 0 0 IN IP4 {origin_ip}",
        "s=Intercom Native",
        f"c=IN IP4 {media_ip}",
        "t=0 0",
        f"m=audio {int(media_port)} RTP/AVP {payloads}",
    ]
    for fmt in selected:
        lines.append(f"a=rtpmap:{fmt.payload_type} {fmt.encoding}/{fmt.sample_rate}/{fmt.channels}")
    lines.extend([
        f"a=ptime:{selected[0].frame_ms}",
        f"a=maxptime:{selected[0].frame_ms}",
        "a=sendrecv",
    ])
    return "\r\n".join(lines) + "\r\n"
