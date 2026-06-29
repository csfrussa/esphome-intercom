"""SDP offer/answer helpers for RTP PCM used by the VoIP Stack profile."""

from __future__ import annotations

from dataclasses import dataclass

from .audio_format import AudioFormat, PcmFormat, UDP_SAFE_PAYLOAD_BYTES, choose_common_frame_ms


class SdpError(ValueError):
    """Malformed or unsupported SDP."""


MAX_RTP_OFFER_FORMATS = 12
_PREFERRED_RTP_AUDIO_KEYS = {
    (48000, PcmFormat.S16LE, 1, 10): 0,
    (32000, PcmFormat.S16LE, 1, 16): 1,
    (32000, PcmFormat.S16LE, 1, 10): 2,
    (24000, PcmFormat.S16LE, 1, 20): 3,
    (16000, PcmFormat.S16LE, 1, 16): 4,
    (16000, PcmFormat.S16LE, 1, 10): 5,
    (16000, PcmFormat.S16LE, 1, 20): 6,
    (16000, PcmFormat.S16LE, 1, 32): 7,
    (8000, PcmFormat.S16LE, 1, 20): 8,
}
_STATIC_RTPMAP = {
    0: ("PCMU", 8000, 1),
    8: ("PCMA", 8000, 1),
}


@dataclass(frozen=True, slots=True)
class RtpPcmFormat:
    payload_type: int
    encoding: str
    sample_rate: int
    channels: int
    frame_ms: int = 20
    min_frame_ms: int = 0
    max_frame_ms: int = 0

    @property
    def bits(self) -> int:
        if self.encoding == "L16":
            return 16
        if self.encoding == "L24":
            return 24
        if self.encoding in {"PCMA", "PCMU"}:
            return 8
        if self.encoding == "OPUS":
            return 0
        raise SdpError(f"unsupported RTP PCM encoding {self.encoding}")

    @property
    def audio_format(self) -> AudioFormat:
        if self.encoding in {"PCMA", "PCMU"}:
            return AudioFormat(self.sample_rate, PcmFormat.S16LE, self.channels, self.frame_ms or 20)
        if self.encoding == "OPUS":
            return AudioFormat(48000, PcmFormat.S16LE, self.channels, self.frame_ms or 20)
        pcm = PcmFormat.S16LE if self.encoding == "L16" else PcmFormat.S24LE
        return AudioFormat(self.sample_rate, pcm, self.channels, self.frame_ms)

    def wire_token(self) -> str:
        return f"pt={self.payload_type}:{self.encoding}/{self.sample_rate}/{self.channels}/{self.frame_ms}ms"


@dataclass(frozen=True, slots=True)
class RtpDtmfFormat:
    payload_type: int
    sample_rate: int


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
            f"RTP PCM frame too large for voip-pcm/1: "
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
    frame_rank = {16: 0, 10: 1, 20: 2, 32: 3}.get(fmt.frame_ms, 9)
    return (1 + pcm_rank, -fmt.sample_rate, frame_rank)


def rtp_offer_formats(formats: list[AudioFormat]) -> list[AudioFormat]:
    ranked = sorted(_dedupe_formats(rtp_mappable_formats(formats)), key=_rtp_offer_rank)
    if not ranked:
        return []
    # a=ptime is media-level in the SIP/SDP profile. Keep one packetization
    # interval per m=audio instead of smuggling per-payload ptime in fmtp.
    frame_ms = ranked[0].frame_ms
    return [fmt for fmt in ranked if fmt.frame_ms == frame_ms][:MAX_RTP_OFFER_FORMATS]


def _common_ptime_formats(*format_lists: list[AudioFormat]) -> list[AudioFormat]:
    frame_ms = choose_common_frame_ms(*format_lists)
    if frame_ms is None:
        return []
    return [fmt for formats in format_lists for fmt in formats if fmt.frame_ms == frame_ms]


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
    if offered.frame_ms not in (0, local.frame_ms):
        min_frame_ms = offered.min_frame_ms or 0
        max_frame_ms = offered.max_frame_ms or 0
        if min_frame_ms and local.frame_ms < min_frame_ms:
            return None
        if max_frame_ms and local.frame_ms > max_frame_ms:
            return None
        if not min_frame_ms and not max_frame_ms:
            return None
        if min_frame_ms and not max_frame_ms and local.frame_ms > offered.frame_ms:
            return None
        if max_frame_ms and not min_frame_ms and local.frame_ms < offered.frame_ms:
            return None
    if offered.frame_ms == 0 and offered.min_frame_ms and local.frame_ms < offered.min_frame_ms:
        return None
    if offered.frame_ms == 0 and offered.max_frame_ms and local.frame_ms > offered.max_frame_ms:
        return None
    if offered.encoding == "OPUS":
        if (
            local.pcm_format == PcmFormat.S16LE
            and local.sample_rate == 48000
            and local.channels == offered.channels
            and local.frame_ms == 20
        ):
            return RtpPcmFormat(offered.payload_type, "OPUS", 48000, offered.channels, local.frame_ms)
        return None
    if not is_rtp_pcm_mappable(local):
        return None
    if offered.encoding in {"PCMA", "PCMU"}:
        if local.pcm_format == PcmFormat.S16LE and local.sample_rate == offered.sample_rate and local.channels == offered.channels:
            return RtpPcmFormat(offered.payload_type, offered.encoding, offered.sample_rate, offered.channels, local.frame_ms)
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
    *,
    include_common_codecs: bool = False,
) -> str:
    common_formats = _common_ptime_formats(send_formats or [], recv_formats or [])
    formats = rtp_offer_formats(common_formats)
    if not formats:
        raise SdpError("SDP offer requires at least one RTP-mappable PCM format")
    rtp_formats = _common_codec_offer_formats(common_formats) if include_common_codecs else []
    next_payload = 96
    used_payloads = {fmt.payload_type for fmt in rtp_formats}
    for fmt in formats:
        while next_payload in used_payloads:
            next_payload += 1
        rtp_formats.append(audio_format_to_rtp(fmt, next_payload))
        used_payloads.add(next_payload)
        next_payload += 1
    payloads = " ".join(str(fmt.payload_type) for fmt in rtp_formats)
    lines = [
        "v=0",
        f"o=- 0 0 IN IP4 {origin_ip}",
        "s=Home Assistant VoIP Stack",
        f"c=IN IP4 {media_ip}",
        "t=0 0",
        f"m=audio {int(media_port)} RTP/AVP {payloads}",
    ]
    for fmt in rtp_formats:
        lines.append(f"a=rtpmap:{fmt.payload_type} {fmt.encoding}/{fmt.sample_rate}/{fmt.channels}")
        if fmt.encoding == "OPUS":
            lines.append(f"a=fmtp:{fmt.payload_type} stereo=1;sprop-stereo=1;maxaveragebitrate=28000")
    lines.append(f"a=ptime:{rtp_formats[0].frame_ms}")
    lines.append(f"a=maxptime:{rtp_formats[0].frame_ms}")
    lines.append("a=sendrecv")
    return "\r\n".join(lines) + "\r\n"


def _common_codec_offer_formats(formats: list[AudioFormat]) -> list[RtpPcmFormat]:
    format_set = set(formats)
    out: list[RtpPcmFormat] = []
    if AudioFormat(48000, PcmFormat.S16LE, 2, 20) in format_set:
        out.append(RtpPcmFormat(98, "OPUS", 48000, 2, 20))
    if AudioFormat(8000, PcmFormat.S16LE, 1, 20) in format_set:
        out.extend((
            RtpPcmFormat(8, "PCMA", 8000, 1, 20),
            RtpPcmFormat(0, "PCMU", 8000, 1, 20),
        ))
    return out


def parse_sdp(sdp: str | bytes) -> dict:
    if isinstance(sdp, bytes):
        sdp = sdp.decode("utf-8", errors="strict")
    session_conn = ""
    media_port = 0
    payload_order: list[int] = []
    rtpmap: dict[int, tuple[str, int, int]] = {}
    fmtp: dict[int, str] = {}
    ptime = 0
    minptime = 0
    maxptime = 0
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
        elif in_audio and line.startswith("a=fmtp:"):
            left, spec = line.removeprefix("a=fmtp:").split(None, 1)
            fmtp[int(left)] = spec.strip()
        elif in_audio and line.startswith("a=ptime:"):
            ptime = int(line.removeprefix("a=ptime:").strip())
        elif in_audio and line.startswith("a=minptime:"):
            minptime = int(line.removeprefix("a=minptime:").strip())
        elif in_audio and line.startswith("a=maxptime:"):
            maxptime = int(line.removeprefix("a=maxptime:").strip())
    if not session_conn or not media_port or not payload_order:
        raise SdpError("SDP missing c=, m=audio port, or payload list")
    return {
        "connection_ip": session_conn,
        "media_port": media_port,
        "payload_order": payload_order,
        "rtpmap": rtpmap,
        "fmtp": fmtp,
        "ptime": ptime,
        "minptime": minptime,
        "maxptime": maxptime,
    }


def offered_pcm_formats(sdp: str | bytes) -> list[RtpPcmFormat]:
    parsed = parse_sdp(sdp)
    out: list[RtpPcmFormat] = []
    for pt in parsed["payload_order"]:
        spec = parsed["rtpmap"].get(pt) or _STATIC_RTPMAP.get(pt)
        if spec is None:
            continue
        encoding, rate, channels = spec
        if encoding not in {"L16", "L24", "PCMA", "PCMU", "OPUS"}:
            continue
        out.append(RtpPcmFormat(pt, encoding, rate, channels, parsed["ptime"], parsed["minptime"], parsed["maxptime"]))
    return out


def offered_dtmf_formats(sdp: str | bytes) -> list[RtpDtmfFormat]:
    parsed = parse_sdp(sdp)
    out: list[RtpDtmfFormat] = []
    for pt in parsed["payload_order"]:
        spec = parsed["rtpmap"].get(pt) or _STATIC_RTPMAP.get(pt)
        if spec is None:
            continue
        encoding, rate, _channels = spec
        if encoding == "TELEPHONE-EVENT":
            out.append(RtpDtmfFormat(pt, rate))
    return out


def offered_media_descriptions(sdp: str | bytes) -> list[str]:
    parsed = parse_sdp(sdp)
    out: list[str] = []
    for pt in parsed["payload_order"]:
        spec = parsed["rtpmap"].get(pt) or _STATIC_RTPMAP.get(pt)
        if spec is None:
            out.append(f"pt={pt}")
            continue
        encoding, rate, channels = spec
        suffix = f"/{channels}" if channels != 1 else ""
        out.append(f"pt={pt}:{encoding}/{rate}{suffix}")
    if parsed["ptime"]:
        out.append(f"ptime={parsed['ptime']}")
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
    if send is None or recv is None or send.frame_ms != recv.frame_ms:
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

    asymmetric_profile = [_format_key(fmt) for fmt in local_send_preferred] != [
        _format_key(fmt) for fmt in local_recv_preferred
    ]
    recv = _first_offered_match(
        offered,
        local_recv_preferred,
        skip_payload_type=send.payload_type if asymmetric_profile and len(offered) > 1 else None,
    )
    if recv is None:
        recv = _best_offered_match([send], local_recv_preferred)
    if recv is None or send.frame_ms != recv.frame_ms:
        return None
    return RtpPcmDirection(send=send, recv=recv)


def build_answer(
    origin_ip: str,
    media_ip: str,
    media_port: int,
    selected: RtpPcmFormat,
    *,
    dtmf: RtpDtmfFormat | None = None,
) -> str:
    return build_answer_directional(origin_ip, media_ip, media_port, selected, selected, dtmf=dtmf)


def build_answer_directional(
    origin_ip: str,
    media_ip: str,
    media_port: int,
    send: RtpPcmFormat,
    recv: RtpPcmFormat,
    *,
    dtmf: RtpDtmfFormat | None = None,
) -> str:
    if send.frame_ms != recv.frame_ms:
        raise SdpError("SDP answer requires a common TX/RX RTP packet time")
    selected = []
    seen: set[int] = set()
    for fmt in (send, recv):
        if fmt.payload_type in seen:
            continue
        seen.add(fmt.payload_type)
        selected.append(fmt)
    payload_values = [str(fmt.payload_type) for fmt in selected]
    if dtmf is not None and str(dtmf.payload_type) not in payload_values:
        payload_values.append(str(dtmf.payload_type))
    payloads = " ".join(payload_values)
    lines = [
        "v=0",
        f"o=- 0 0 IN IP4 {origin_ip}",
        "s=Home Assistant VoIP Stack",
        f"c=IN IP4 {media_ip}",
        "t=0 0",
        f"m=audio {int(media_port)} RTP/AVP {payloads}",
    ]
    for fmt in selected:
        lines.append(f"a=rtpmap:{fmt.payload_type} {fmt.encoding}/{fmt.sample_rate}/{fmt.channels}")
    if dtmf is not None:
        lines.append(f"a=rtpmap:{dtmf.payload_type} telephone-event/{dtmf.sample_rate}")
        lines.append(f"a=fmtp:{dtmf.payload_type} 0-16")
    lines.extend([
        f"a=ptime:{selected[0].frame_ms}",
        f"a=maxptime:{selected[0].frame_ms}",
        "a=sendrecv",
    ])
    return "\r\n".join(lines) + "\r\n"
