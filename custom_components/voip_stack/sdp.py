"""SDP offer/answer helpers for RTP PCM used by the VoIP Stack profile."""

from __future__ import annotations

from dataclasses import dataclass

from .audio_format import AudioFormat, PcmFormat, UDP_SAFE_PAYLOAD_BYTES, choose_common_frame_ms


class SdpError(ValueError):
    """Malformed or unsupported SDP."""


MAX_RTP_OFFER_FORMATS = 11
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


@dataclass(frozen=True, slots=True)
class RtpVideoFormat:
    """One negotiated RTP video format.

    The H.264 fields remain first-class because RFC 6184 needs them during
    packetization.  Other codecs keep their complete normalized ``fmtp`` so
    an exact relay never invents or discards endpoint parameters.
    """

    payload_type: int = 103
    profile_level_id: str = "42800d"
    packetization_mode: int = 1
    direction: str = "sendrecv"
    sprop_parameter_sets: str = ""
    encoding: str = "H264"
    clock_rate: int = 90000
    transport_profile: str = "RTP/AVP"
    fmtp: str = ""
    rtcp_feedback: tuple[str, ...] = ()

    def wire_token(self) -> str:
        token = f"pt={self.payload_type}:{self.encoding}/{self.clock_rate}"
        if self.encoding == "H264":
            token += (
                f";profile-level-id={self.profile_level_id};"
                f"packetization-mode={self.packetization_mode}"
            )
        elif self.fmtp:
            token += f";fmtp={self.fmtp}"
        return (
            f"{token};rtp-profile={self.transport_profile};"
            f"direction={self.direction}"
        )

    @property
    def browser_codec(self) -> str:
        if self.encoding == "H264":
            return f"avc1.{self.profile_level_id.upper()}"
        return {
            "VP8": "vp8",
            "VP9": "vp09.00.10.08",
            "AV1": "av01.0.04M.08",
            "JPEG": "jpeg",
        }.get(self.encoding, "")


# Kept as a public compatibility alias for integrations and tests that used
# the experimental H.264-only type before the video format model was widened.
RtpH264Format = RtpVideoFormat


DEFAULT_H264_FORMAT = RtpH264Format()
DEFAULT_VIDEO_FORMATS = (
    DEFAULT_H264_FORMAT,
    RtpVideoFormat(payload_type=104, encoding="VP8"),
    RtpVideoFormat(payload_type=26, encoding="JPEG"),
)


def browser_video_send_supported(video_format: RtpVideoFormat | None) -> bool:
    """Return whether the browser bridge can packetize this negotiated codec."""

    if video_format is None:
        return False
    return video_format.encoding == "VP8" or (
        video_format.encoding == "H264" and video_format.packetization_mode == 1
    )


def browser_video_receive_supported(video_format: RtpVideoFormat | None) -> bool:
    """Return whether the direct browser bridge can render this codec."""

    return bool(video_format is not None and video_format.encoding in {"H264", "VP8", "JPEG"})


def video_formats_passthrough_compatible(
    source: RtpVideoFormat | None,
    destination: RtpVideoFormat | None,
) -> bool:
    """Return whether encoded RTP payloads may be relayed without transcoding.

    Payload type and direction are leg-local and therefore intentionally not
    compared.  H.264 packetization and profile bytes are bitstream contracts;
    forwarding a stream across a different contract can produce a call that
    negotiates successfully but cannot be decoded by the destination.
    """

    if source is None or destination is None:
        return False
    if source.encoding != destination.encoding or source.clock_rate != destination.clock_rate:
        return False
    if source.transport_profile != destination.transport_profile:
        return False
    if source.encoding == "H264":
        return bool(
            source.packetization_mode == destination.packetization_mode
            and source.profile_level_id.lower() == destination.profile_level_id.lower()
        )
    return _fmtp_parameters(source.fmtp) == _fmtp_parameters(destination.fmtp)


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
    video_port: int = 0,
    video_format: RtpH264Format | None = None,
    video_formats: tuple[RtpVideoFormat, ...] | list[RtpVideoFormat] | None = None,
    video_direction: str = "sendrecv",
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
    while next_payload in used_payloads:
        next_payload += 1
    dtmf_payload_type = next_payload
    payloads = " ".join([*(str(fmt.payload_type) for fmt in rtp_formats), str(dtmf_payload_type)])
    lines = [
        "v=0",
        f"o=- 0 0 IN IP4 {origin_ip}",
        "s=VoIP Stack",
        f"c=IN IP4 {media_ip}",
        "t=0 0",
        f"m=audio {int(media_port)} RTP/AVP {payloads}",
    ]
    for fmt in rtp_formats:
        lines.append(f"a=rtpmap:{fmt.payload_type} {fmt.encoding}/{fmt.sample_rate}/{fmt.channels}")
        if fmt.encoding == "OPUS":
            lines.append(f"a=fmtp:{fmt.payload_type} stereo=1;sprop-stereo=1;maxaveragebitrate=28000")
    lines.append(f"a=rtpmap:{dtmf_payload_type} telephone-event/8000")
    lines.append(f"a=fmtp:{dtmf_payload_type} 0-16")
    lines.append(f"a=ptime:{rtp_formats[0].frame_ms}")
    lines.append(f"a=maxptime:{rtp_formats[0].frame_ms}")
    lines.append("a=sendrecv")
    offered_video = tuple(video_formats or (() if video_format is None else (video_format,)))
    if offered_video and int(video_port) > 0:
        lines.extend(_video_media_lines_many(int(video_port), offered_video, direction=video_direction))
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
    media_conn = ""
    media_port = 0
    payload_order: list[int] = []
    rtpmap: dict[int, tuple[str, int, int]] = {}
    fmtp: dict[int, str] = {}
    ptime = 0
    minptime = 0
    maxptime = 0
    saw_media = False
    selected_audio = False
    in_selected_audio = False
    for raw in sdp.replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("m="):
            saw_media = True
            in_selected_audio = False
            parts = line.split()
            if selected_audio or len(parts) < 4 or parts[0] != "m=audio" or parts[2].upper() != "RTP/AVP":
                continue
            try:
                candidate_port = int(parts[1])
                candidate_payloads = [int(value) for value in parts[3:]]
            except ValueError as err:
                raise SdpError(f"bad audio media line: {line}") from err
            # Port zero rejects this media stream. Continue looking for the
            # first active RTP/AVP audio section that this single-stream bridge
            # can actually answer.
            if candidate_port == 0:
                continue
            if not 1 <= candidate_port <= 65535:
                raise SdpError(f"bad audio media port: {candidate_port}")
            if not candidate_payloads or any(not 0 <= pt <= 127 for pt in candidate_payloads):
                raise SdpError(f"bad audio payload list: {line}")
            media_port = candidate_port
            payload_order = candidate_payloads
            selected_audio = True
            in_selected_audio = True
            continue
        if line.startswith("c="):
            if not saw_media:
                if not line.startswith("c=IN IP4 "):
                    raise SdpError(f"unsupported SDP connection: {line}")
                session_conn = line.removeprefix("c=IN IP4 ").strip()
            elif in_selected_audio:
                if not line.startswith("c=IN IP4 "):
                    raise SdpError(f"unsupported SDP audio connection: {line}")
                media_conn = line.removeprefix("c=IN IP4 ").strip()
            continue
        if not in_selected_audio:
            continue
        try:
            if line.startswith("a=rtpmap:"):
                left, spec = line.removeprefix("a=rtpmap:").split(None, 1)
                pt = int(left)
                if not 0 <= pt <= 127:
                    raise SdpError(f"bad rtpmap payload type: {pt}")
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
            elif line.startswith("a=fmtp:"):
                left, spec = line.removeprefix("a=fmtp:").split(None, 1)
                fmtp[int(left)] = spec.strip()
            elif line.startswith("a=ptime:"):
                ptime = int(line.removeprefix("a=ptime:").strip())
            elif line.startswith("a=minptime:"):
                minptime = int(line.removeprefix("a=minptime:").strip())
            elif line.startswith("a=maxptime:"):
                maxptime = int(line.removeprefix("a=maxptime:").strip())
        except ValueError as err:
            raise SdpError(f"bad SDP audio attribute: {line}") from err
    connection_ip = media_conn or session_conn
    if not connection_ip or not media_port or not payload_order:
        raise SdpError("SDP missing c=, m=audio port, or payload list")
    return {
        "connection_ip": connection_ip,
        "media_port": media_port,
        "payload_order": payload_order,
        "rtpmap": rtpmap,
        "fmtp": fmtp,
        "ptime": ptime,
        "minptime": minptime,
        "maxptime": maxptime,
    }


def _parse_media_sections(sdp_body: str | bytes) -> tuple[str, str, list[dict]]:
    """Parse enough SDP structure to negotiate independent media sections."""

    if isinstance(sdp_body, bytes):
        sdp_body = sdp_body.decode("utf-8", errors="strict")
    session_connection = ""
    session_direction = "sendrecv"
    current: dict | None = None
    sections: list[dict] = []
    for raw in sdp_body.replace("\r\n", "\n").split("\n"):
        line = raw.strip()
        if not line:
            continue
        if line.startswith("m="):
            parts = line[2:].split()
            if len(parts) < 4:
                raise SdpError(f"bad media line: {line}")
            try:
                port = int(parts[1])
            except ValueError as err:
                raise SdpError(f"bad media port: {line}") from err
            if not 0 <= port <= 65535:
                raise SdpError(f"bad media port: {port}")
            current = {
                "media": parts[0].lower(),
                "port": port,
                "transport": parts[2].upper(),
                "formats": parts[3:],
                "connection_ip": "",
                "connection_seen": False,
                "connection_supported": True,
                "rtpmap": {},
                "fmtp": {},
                "rtcp_feedback": {},
                "rtcp_port": 0,
                "rtcp_mux": False,
                "direction": session_direction,
            }
            sections.append(current)
            continue
        if line.startswith("c="):
            if current is None:
                if not line.startswith("c=IN IP4 "):
                    raise SdpError(f"unsupported SDP connection: {line}")
                address = line.removeprefix("c=IN IP4 ").strip()
                session_connection = address
            else:
                current["connection_seen"] = True
                if line.startswith("c=IN IP4 "):
                    current["connection_ip"] = line.removeprefix("c=IN IP4 ").strip()
                else:
                    # A media-level connection applies only to that media
                    # section.  Preserve a usable IPv4 audio section even if
                    # an additional video/data section advertises IP6 or an
                    # address family this deliberately small SIP profile does
                    # not implement.  The unsupported section is rejected in
                    # the answer instead of poisoning the entire audio call.
                    current["connection_ip"] = ""
                    current["connection_supported"] = False
            continue
        if line in {"a=sendrecv", "a=sendonly", "a=recvonly", "a=inactive"}:
            direction = line[2:]
            if current is None:
                session_direction = direction
            else:
                current["direction"] = direction
            continue
        if current is None:
            continue
        try:
            if line.startswith("a=rtpmap:"):
                left, spec = line.removeprefix("a=rtpmap:").split(None, 1)
                current["rtpmap"][int(left)] = spec.strip()
            elif line.startswith("a=fmtp:"):
                left, spec = line.removeprefix("a=fmtp:").split(None, 1)
                current["fmtp"][int(left)] = spec.strip()
            elif line.startswith("a=rtcp-fb:"):
                left, spec = line.removeprefix("a=rtcp-fb:").split(None, 1)
                key: int | str = "*" if left == "*" else int(left)
                current["rtcp_feedback"].setdefault(key, []).append(spec.strip().lower())
            elif line.startswith("a=rtcp:"):
                current["rtcp_port"] = int(line.removeprefix("a=rtcp:").split(None, 1)[0])
            elif line == "a=rtcp-mux":
                current["rtcp_mux"] = True
        except ValueError as err:
            raise SdpError(f"bad SDP media attribute: {line}") from err
    for section in sections:
        if not section["connection_seen"]:
            section["connection_ip"] = session_connection
    return session_connection, session_direction, sections


def parse_video_sdp(sdp_body: str | bytes) -> dict | None:
    """Return the first video media section when it matches this profile."""

    _session_connection, _session_direction, sections = _parse_media_sections(sdp_body)
    for section in sections:
        if section["media"] != "video":
            continue
        # This deliberately small profile owns one video stream. Do not skip
        # an unsupported first m=video and accidentally answer a later format
        # in the first section's position.
        if (
            section["port"] == 0
            or section["transport"] not in {"RTP/AVP", "RTP/AVPF"}
            or not section["connection_supported"]
        ):
            return None
        if not section["connection_ip"]:
            raise SdpError("SDP video section has no connection address")
        payload_order: list[int] = []
        try:
            payload_order = [int(item) for item in section["formats"]]
        except ValueError as err:
            raise SdpError("bad video payload list") from err
        if not payload_order or any(not 0 <= item <= 127 for item in payload_order):
            raise SdpError("bad video payload list")
        return {
            "connection_ip": section["connection_ip"],
            "media_port": section["port"],
            "transport_profile": section["transport"],
            "payload_order": payload_order,
            "rtpmap": dict(section["rtpmap"]),
            "fmtp": dict(section["fmtp"]),
            "rtcp_feedback": dict(section["rtcp_feedback"]),
            "rtcp_port": int(section["rtcp_port"] or 0),
            "rtcp_mux": bool(section["rtcp_mux"]),
            "direction": section["direction"],
        }
    return None


def _fmtp_parameters(value: str) -> dict[str, str]:
    out: dict[str, str] = {}
    for item in str(value or "").split(";"):
        key, separator, raw_value = item.strip().partition("=")
        if key:
            out[key.lower()] = raw_value.strip() if separator else ""
    return out


_STATIC_VIDEO_RTPMAP = {
    26: "JPEG/90000",
    34: "H263/90000",
}
_VIDEO_ENCODING_ALIASES = {
    "H264": "H264",
    "H265": "H265",
    "HEVC": "H265",
    "VP8": "VP8",
    "VP9": "VP9",
    "AV1": "AV1",
    "JPEG": "JPEG",
    "MJPEG": "JPEG",
    "H263": "H263",
    "H263-1998": "H263P",
    "H263-2000": "H263P",
}


def offered_video_formats(sdp_body: str | bytes) -> list[RtpVideoFormat]:
    """List normalized formats from the first active AVP/AVPF video section."""

    parsed = parse_video_sdp(sdp_body)
    if parsed is None or parsed["rtcp_mux"]:
        return []
    out: list[RtpVideoFormat] = []
    for payload_type in parsed["payload_order"]:
        mapping = str(
            parsed["rtpmap"].get(payload_type)
            or _STATIC_VIDEO_RTPMAP.get(int(payload_type))
            or ""
        )
        if not mapping:
            continue
        bits = mapping.upper().split("/")
        if len(bits) < 2:
            continue
        encoding = _VIDEO_ENCODING_ALIASES.get(bits[0])
        if encoding is None:
            continue
        try:
            clock_rate = int(bits[1])
        except ValueError:
            continue
        if clock_rate <= 0 or (int(payload_type) < 96 and int(payload_type) not in _STATIC_VIDEO_RTPMAP):
            continue
        parameters = _fmtp_parameters(parsed["fmtp"].get(payload_type, ""))
        profile = parameters.get("profile-level-id", DEFAULT_H264_FORMAT.profile_level_id).lower()
        packetization_mode = 1
        sprop_parameter_sets = ""
        if encoding == "H264":
            try:
                packetization_mode = int(parameters.get("packetization-mode", "0") or 0)
            except ValueError:
                continue
            if packetization_mode not in {0, 1} or len(profile) != 6:
                continue
            try:
                profile_idc = bytes.fromhex(profile)[0]
            except (ValueError, IndexError):
                continue
            if profile_idc not in {0x42, 0x4D, 0x64}:
                continue
            sprop_parameter_sets = parameters.get("sprop-parameter-sets", "")
        feedback = tuple(
            dict.fromkeys(
                [
                    *parsed["rtcp_feedback"].get("*", []),
                    *parsed["rtcp_feedback"].get(payload_type, []),
                ]
            )
        )
        out.append(
            RtpVideoFormat(
                payload_type=payload_type,
                profile_level_id=profile,
                packetization_mode=packetization_mode,
                direction=str(parsed["direction"] or "sendrecv"),
                sprop_parameter_sets=sprop_parameter_sets,
                encoding=encoding,
                clock_rate=clock_rate,
                transport_profile=str(parsed["transport_profile"]),
                fmtp=str(parsed["fmtp"].get(payload_type, "")).strip(),
                rtcp_feedback=(
                    feedback
                    if str(parsed["transport_profile"]) == "RTP/AVPF"
                    else ()
                ),
            )
        )
    return out


def offered_h264_formats(sdp_body: str | bytes) -> list[RtpH264Format]:
    """Compatibility wrapper returning only RFC 6184 formats."""

    return [fmt for fmt in offered_video_formats(sdp_body) if fmt.encoding == "H264"]


def negotiate_video(
    remote_sdp: str | bytes,
    *,
    accepted_encodings: tuple[str, ...] = ("H264", "VP8", "JPEG"),
    prefer_browser_send: bool = False,
) -> RtpVideoFormat | None:
    """Select the best remote format accepted by the configured media path.

    Preserve the endpoint's order within each capability class, but prefer a
    codec the browser can consume directly over one that needs the optional
    FFmpeg bridge.  When camera transmission is requested, prefer the smaller
    subset that the browser can also packetize.
    """

    accepted = {item.upper() for item in accepted_encodings}
    compatible = [fmt for fmt in offered_video_formats(remote_sdp) if fmt.encoding in accepted]
    if prefer_browser_send:
        selected = next(
            (fmt for fmt in compatible if browser_video_send_supported(fmt)),
            None,
        )
        if selected is not None:
            return selected
    return next(
        (fmt for fmt in compatible if browser_video_receive_supported(fmt)),
        None,
    ) or next(iter(compatible), None)


def negotiate_h264(remote_sdp: str | bytes) -> RtpH264Format | None:
    """Select the first supported H.264 mode-1 format from the offer/answer."""

    offered = offered_h264_formats(remote_sdp)
    return offered[0] if offered else None


def negotiate_h264_answer(
    remote_sdp: str | bytes,
    offered: RtpH264Format,
) -> RtpH264Format | None:
    """Accept an H.264 answer only when it selects our offered payload type."""

    selected = negotiate_h264(remote_sdp)
    if (
        selected is None
        or selected.payload_type != offered.payload_type
        or selected.transport_profile != offered.transport_profile
        or selected.packetization_mode != offered.packetization_mode
        or selected.profile_level_id[:4].lower()
        != offered.profile_level_id[:4].lower()
    ):
        return None
    return selected


def negotiate_video_answer(
    remote_sdp: str | bytes,
    offered: RtpVideoFormat | tuple[RtpVideoFormat, ...],
) -> RtpVideoFormat | None:
    """Accept only a payload/codec pair present in our outbound offer."""

    offered_formats = (offered,) if isinstance(offered, RtpVideoFormat) else tuple(offered)
    by_payload = {int(item.payload_type): item for item in offered_formats}
    for selected in offered_video_formats(remote_sdp):
        candidate = by_payload.get(int(selected.payload_type))
        if (
            candidate is None
            or candidate.encoding != selected.encoding
            or candidate.transport_profile != selected.transport_profile
        ):
            continue
        if candidate.encoding == "H264":
            # RFC 6184 makes packetization-mode and the profile/constraint
            # bytes part of the media contract. A peer may answer with a
            # different level when level asymmetry is enabled, but silently
            # switching profile family or packetization mode would make the
            # browser/relay consume a bitstream it did not offer.
            if candidate.packetization_mode != selected.packetization_mode:
                continue
            if (
                candidate.profile_level_id[:4].lower()
                != selected.profile_level_id[:4].lower()
            ):
                continue
        return selected
    return None


def local_direction_for_remote(remote_direction: str) -> str:
    """Return the RFC 3264 answer/local direction for a remote direction."""

    return {
        "sendonly": "recvonly",
        "recvonly": "sendonly",
        "inactive": "inactive",
    }.get(str(remote_direction or "sendrecv").lower(), "sendrecv")


def constrained_video_direction(
    remote_direction: str,
    *,
    allow_send: bool,
    allow_receive: bool = True,
) -> str:
    """Intersect RFC 3264 direction with local camera/decoder capabilities."""

    remote = str(remote_direction or "sendrecv").lower()
    can_send = bool(allow_send and remote in {"sendrecv", "recvonly"})
    can_receive = bool(allow_receive and remote in {"sendrecv", "sendonly"})
    if can_send and can_receive:
        return "sendrecv"
    if can_send:
        return "sendonly"
    if can_receive:
        return "recvonly"
    return "inactive"


def _video_media_lines(
    media_port: int,
    selected: RtpVideoFormat,
    *,
    direction: str,
) -> list[str]:
    payload_type = int(selected.payload_type)
    rtp_encoding = {"H263P": "H263-1998"}.get(
        selected.encoding, selected.encoding
    )
    lines = [
        f"m=video {int(media_port)} {selected.transport_profile} {payload_type}",
        f"a=rtpmap:{payload_type} {rtp_encoding}/{selected.clock_rate}",
    ]
    if selected.encoding == "H264":
        lines.append(
            f"a=fmtp:{payload_type} profile-level-id={selected.profile_level_id};"
            f"packetization-mode={selected.packetization_mode};level-asymmetry-allowed=1"
        )
    elif selected.fmtp:
        lines.append(f"a=fmtp:{payload_type} {selected.fmtp}")
    if selected.transport_profile == "RTP/AVPF":
        for feedback in selected.rtcp_feedback:
            lines.append(f"a=rtcp-fb:{payload_type} {feedback}")
    lines.append(f"a=rtcp:{int(media_port) + 1}")
    lines.append(f"a={direction}")
    return lines


def _video_media_lines_many(
    media_port: int,
    selected: tuple[RtpVideoFormat, ...] | list[RtpVideoFormat],
    *,
    direction: str,
) -> list[str]:
    """Build one offer m-line containing all supported video payloads."""

    formats = tuple(selected)
    if not formats:
        raise SdpError("video offer requires at least one format")
    profiles = {item.transport_profile for item in formats}
    if len(profiles) != 1:
        raise SdpError("one video media section cannot mix RTP profiles")
    profile = profiles.pop()
    if profile not in {"RTP/AVP", "RTP/AVPF"}:
        raise SdpError("unsupported video RTP profile")
    payloads = " ".join(str(item.payload_type) for item in formats)
    lines = [f"m=video {int(media_port)} {profile} {payloads}"]
    for item in formats:
        payload_type = int(item.payload_type)
        rtp_encoding = {"H263P": "H263-1998"}.get(item.encoding, item.encoding)
        lines.append(f"a=rtpmap:{payload_type} {rtp_encoding}/{item.clock_rate}")
        if item.encoding == "H264":
            lines.append(
                f"a=fmtp:{payload_type} profile-level-id={item.profile_level_id};"
                f"packetization-mode={item.packetization_mode};level-asymmetry-allowed=1"
            )
        elif item.fmtp:
            lines.append(f"a=fmtp:{payload_type} {item.fmtp}")
        if profile == "RTP/AVPF":
            feedback = item.rtcp_feedback or (
                ("nack pli", "ccm fir")
                if item.encoding in {"H264", "VP8"}
                else ()
            )
            for value in feedback:
                lines.append(f"a=rtcp-fb:{payload_type} {value}")
    lines.append(f"a=rtcp:{int(media_port) + 1}")
    lines.append(f"a={direction}")
    return lines


def _rejected_media_line(section: dict) -> str:
    formats = " ".join(str(item) for item in section.get("formats") or ["0"])
    return f"m={section['media']} 0 {section['transport']} {formats}"


def _answer_with_offered_media_order(
    *,
    origin_ip: str,
    media_ip: str,
    audio_lines: list[str],
    remote_sdp: str | bytes,
    video_port: int,
    video_format: RtpH264Format | None,
    video_direction: str | None = None,
) -> str:
    _session_connection, _session_direction, sections = _parse_media_sections(remote_sdp)
    lines = [
        "v=0",
        f"o=- 0 0 IN IP4 {origin_ip}",
        "s=VoIP Stack",
        f"c=IN IP4 {media_ip}",
        "t=0 0",
    ]
    used_audio = False
    used_video = False
    for section in sections:
        if (
            section["media"] == "audio"
            and not used_audio
            and section["port"] > 0
            and section["transport"] == "RTP/AVP"
            and section["connection_supported"]
        ):
            lines.extend(audio_lines)
            used_audio = True
            continue
        if section["media"] == "video" and not used_video:
            offered_payloads = {str(item) for item in section.get("formats") or []}
            if (
                video_format is not None
                and int(video_port) > 0
                and section["port"] > 0
                and section["transport"] in {"RTP/AVP", "RTP/AVPF"}
                and section["connection_supported"]
                and str(video_format.payload_type) in offered_payloads
            ):
                lines.extend(
                    _video_media_lines(
                        int(video_port),
                        video_format,
                        direction=video_direction or local_direction_for_remote(video_format.direction),
                    )
                )
            else:
                lines.append(_rejected_media_line(section))
            used_video = True
            continue
        lines.append(_rejected_media_line(section))
    if not used_audio:
        lines.extend(audio_lines)
    return "\r\n".join(lines) + "\r\n"


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
    remote_sdp: str | bytes | None = None,
    video_port: int = 0,
    video_format: RtpH264Format | None = None,
    video_direction: str | None = None,
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
    audio_lines = [f"m=audio {int(media_port)} RTP/AVP {payloads}"]
    for fmt in selected:
        audio_lines.append(f"a=rtpmap:{fmt.payload_type} {fmt.encoding}/{fmt.sample_rate}/{fmt.channels}")
    if dtmf is not None:
        audio_lines.append(f"a=rtpmap:{dtmf.payload_type} telephone-event/{dtmf.sample_rate}")
        audio_lines.append(f"a=fmtp:{dtmf.payload_type} 0-16")
    audio_lines.extend([
        f"a=ptime:{selected[0].frame_ms}",
        f"a=maxptime:{selected[0].frame_ms}",
        "a=sendrecv",
    ])
    if remote_sdp is not None:
        _connection, _direction, sections = _parse_media_sections(remote_sdp)
        # RFC 3264 answers retain the offer's media-section count and order.
        # The legacy single-audio fast path is valid only for exactly one
        # audio section; every extra audio/video/data section must be answered
        # explicitly, with port zero when this profile does not select it.
        if len(sections) != 1 or sections[0]["media"] != "audio":
            return _answer_with_offered_media_order(
                origin_ip=origin_ip,
                media_ip=media_ip,
                audio_lines=audio_lines,
                remote_sdp=remote_sdp,
                video_port=video_port,
                video_format=video_format,
                video_direction=video_direction,
            )
    lines = [
        "v=0",
        f"o=- 0 0 IN IP4 {origin_ip}",
        "s=VoIP Stack",
        f"c=IN IP4 {media_ip}",
        "t=0 0",
        *audio_lines,
    ]
    return "\r\n".join(lines) + "\r\n"
