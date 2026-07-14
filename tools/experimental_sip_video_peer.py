#!/usr/bin/env python3
"""Deterministic SIP audio/video caller for the local VoIP Stack lab.

This is a qualification peer, not a production softphone. It originates one
UDP SIP call, sends continuous PCMA silence plus an FFmpeg test pattern in the
requested RTP video codec, and records the real SIP/media outcome as JSON.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import importlib.util
import json
from pathlib import Path
import secrets
import shutil
import socket
import sys
import time
import types


ROOT = Path(__file__).resolve().parents[1]
PKG_NAME = "custom_components.voip_stack"
PKG_DIR = ROOT / "custom_components" / "voip_stack"


def _load(name: str):
    """Load protocol helpers without importing the Home Assistant integration."""

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
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {full_name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module


sip = _load("sip")
sdp = _load("sdp")
rtp = _load("rtp")
video_rtcp = _load("video_rtcp")


VIDEO_PROFILES = {
    "h264": {
        "payload_type": 102,
        "rtpmap": "H264/90000",
        "fmtp": "profile-level-id=42e01f;packetization-mode=1;level-asymmetry-allowed=1",
        "encoder": "libx264",
        "size": "320x180",
        "args": ("-preset", "ultrafast", "-tune", "zerolatency", "-profile:v", "baseline"),
    },
    "h265": {
        "payload_type": 104,
        "rtpmap": "H265/90000",
        "fmtp": "",
        "encoder": "libx265",
        "size": "320x180",
        "args": (
            "-preset",
            "ultrafast",
            "-x265-params",
            "log-level=error:keyint=15:min-keyint=15:bframes=0:no-scenecut=1:repeat-headers=1",
        ),
    },
    "vp8": {
        "payload_type": 103,
        "rtpmap": "VP8/90000",
        "fmtp": "",
        "encoder": "libvpx",
        "size": "320x180",
        "args": ("-deadline", "realtime", "-cpu-used", "8"),
    },
    "jpeg": {
        "payload_type": 26,
        "rtpmap": "JPEG/90000",
        "fmtp": "",
        "encoder": "mjpeg",
        "size": "320x180",
        # RFC 2435 carries the standard Huffman tables implicitly. FFmpeg's
        # default MJPEG encoder optimizes them per frame, which the RTP muxer
        # correctly refuses to packetize.
        "args": ("-huffman", "default", "-q:v", "5"),
    },
    "h263": {
        "payload_type": 34,
        "rtpmap": "H263/90000",
        "fmtp": "",
        "encoder": "h263",
        "size": "352x288",
        "args": (),
    },
    "h263p": {
        "payload_type": 105,
        "rtpmap": "H263-1998/90000",
        "fmtp": "",
        "encoder": "h263p",
        "size": "352x288",
        "args": (),
    },
}


def _local_ip(remote_host: str, remote_port: int) -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((remote_host, int(remote_port)))
        return str(sock.getsockname()[0])
    finally:
        sock.close()


def _reserve_udp_socket(local_ip: str) -> socket.socket:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setblocking(False)
    sock.bind((local_ip, 0))
    return sock


def _reserve_video_ports(
    local_ip: str,
) -> tuple[int, socket.socket, socket.socket]:
    """Reserve an even RTP port and bind its adjacent RTCP port."""

    for _ in range(128):
        probe = _reserve_udp_socket(local_ip)
        port = int(probe.getsockname()[1])
        probe.close()
        port &= ~1
        if port < 1024 or port >= 65534:
            continue
        rtp_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        rtp_sock.setblocking(False)
        rtcp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        rtcp.setblocking(False)
        try:
            rtp_sock.bind((local_ip, port))
            rtcp.bind((local_ip, port + 1))
        except OSError:
            rtp_sock.close()
            rtcp.close()
            continue
        return port, rtp_sock, rtcp
    raise RuntimeError("could not reserve a video RTP/RTCP pair")


def _offer(
    *,
    local_ip: str,
    audio_port: int,
    video_port: int,
    codec: str,
    direction: str,
    video_profile: str,
) -> bytes:
    lines = [
        "v=0",
        f"o=- 1 1 IN IP4 {local_ip}",
        "s=VoIP Stack video qualification peer",
        f"c=IN IP4 {local_ip}",
        "t=0 0",
        f"m=audio {audio_port} RTP/AVP 8 101",
        "a=rtpmap:8 PCMA/8000",
        "a=rtpmap:101 telephone-event/8000",
        "a=fmtp:101 0-16",
        "a=ptime:20",
        "a=sendrecv",
    ]
    if codec == "audio":
        return ("\r\n".join(lines) + "\r\n").encode()
    profile = VIDEO_PROFILES[codec]
    payload_type = int(profile["payload_type"])
    lines.extend(
        (
            f"m=video {video_port} {video_profile} {payload_type}",
            f"a=rtpmap:{payload_type} {profile['rtpmap']}",
        )
    )
    if profile["fmtp"]:
        lines.append(f"a=fmtp:{payload_type} {profile['fmtp']}")
    if video_profile == "RTP/AVPF" and codec in {"h264", "h265", "vp8"}:
        lines.extend(
            (
                f"a=rtcp-fb:{payload_type} nack pli",
                f"a=rtcp-fb:{payload_type} ccm fir",
            )
        )
    lines.extend((f"a=rtcp:{video_port + 1}", f"a={direction}"))
    return ("\r\n".join(lines) + "\r\n").encode()


def _request_headers(
    *,
    method: str,
    local_ip: str,
    local_port: int,
    local_user: str,
    remote_uri: str,
    call_id: str,
    local_tag: str,
    cseq: int,
    branch: str,
    remote_to: str | None = None,
    content_type: str | None = None,
) -> list[tuple[str, str]]:
    headers = [
        ("Via", f"SIP/2.0/UDP {local_ip}:{local_port};branch={branch};rport"),
        ("Max-Forwards", "70"),
        ("From", f"<sip:{local_user}@{local_ip}:{local_port}>;tag={local_tag}"),
        ("To", remote_to or f"<{remote_uri}>"),
        ("Call-ID", call_id),
        ("CSeq", f"{cseq} {method}"),
        ("Contact", f"<sip:{local_user}@{local_ip}:{local_port};transport=udp>"),
        ("Allow", "INVITE, ACK, BYE, CANCEL, INFO, OPTIONS"),
        ("User-Agent", "VoIP-Stack-Video-Lab/1"),
    ]
    if content_type:
        headers.append(("Content-Type", content_type))
    return headers


def _response_headers(request) -> list[tuple[str, str]]:
    return [
        *(('Via', value) for value in request.header_values("Via")),
        ("From", request.header("From")),
        ("To", request.header("To")),
        ("Call-ID", request.header("Call-ID")),
        ("CSeq", request.header("CSeq")),
    ]


async def _send_pcma(
    sock: socket.socket,
    destination: tuple[str, int],
    stopped: asyncio.Event,
    counters: dict[str, int],
) -> None:
    """Send an RTP clock with G.711 A-law silence without audible test tones."""

    loop = asyncio.get_running_loop()
    sequence = secrets.randbelow(65536)
    timestamp = secrets.randbelow(2**32)
    ssrc = secrets.randbelow(2**32 - 1) + 1
    next_send = loop.time()
    payload = bytes([0xD5]) * 160
    while not stopped.is_set():
        await asyncio.sleep(max(0.0, next_send - loop.time()))
        packet = rtp.build_packet(
            rtp.RtpPacket(8, sequence, timestamp, ssrc, payload)
        )
        await loop.sock_sendto(sock, packet, destination)
        counters["audio_tx_packets"] += 1
        sequence = (sequence + 1) & 0xFFFF
        timestamp = (timestamp + 160) & 0xFFFFFFFF
        next_send += 0.020
        if next_send < loop.time():
            next_send = loop.time() + 0.020


async def _receive_audio(
    sock: socket.socket,
    stopped: asyncio.Event,
    counters: dict[str, int],
) -> None:
    loop = asyncio.get_running_loop()
    while not stopped.is_set():
        try:
            raw, _addr = await asyncio.wait_for(loop.sock_recvfrom(sock, 4096), 0.5)
        except TimeoutError:
            continue
        try:
            rtp.parse_packet(raw)
        except Exception:
            continue
        counters["audio_rx_packets"] += 1


async def _receive_rtcp(
    sock: socket.socket,
    stopped: asyncio.Event,
    counters: dict,
) -> None:
    loop = asyncio.get_running_loop()
    while not stopped.is_set():
        try:
            raw, _addr = await asyncio.wait_for(loop.sock_recvfrom(sock, 4096), 0.5)
        except TimeoutError:
            continue
        counters["video_rtcp_rx_packets"] += 1
        try:
            parsed = video_rtcp.parse_compound(raw)
        except video_rtcp.RtcpError:
            counters["video_rtcp_invalid_packets"] += 1
            continue
        counters["video_rtcp_packet_types"].append(
            [[item.packet_type, item.fmt] for item in parsed]
        )


async def _relay_video(
    sock: socket.socket,
    destination: tuple[str, int],
    stopped: asyncio.Event,
    counters: dict[str, int],
    *,
    send_enabled: bool,
) -> None:
    """Forward generated RTP and count media returned by the HA browser.

    FFmpeg writes its generated stream to the local advertised RTP socket from
    an ephemeral source port. Packets from the negotiated HA media address are
    the opposite direction and must be counted, not reflected back into HA.
    """

    loop = asyncio.get_running_loop()
    while not stopped.is_set():
        try:
            raw, _addr = await asyncio.wait_for(loop.sock_recvfrom(sock, 65535), 0.5)
        except TimeoutError:
            continue
        packet = rtp.parse_packet(raw)
        # The address advertised in SDP can differ from the packet source
        # address when the peer is reached through loopback, NAT or a routed
        # interface. The negotiated RTP source port still identifies the HA
        # return leg in this single-call qualification peer. Comparing the
        # full tuple made legitimate browser video look like local FFmpeg
        # input and reflected it back to HA during loopback tests.
        if int(_addr[1]) == int(destination[1]):
            counters["video_rx_packets"] += 1
            counters["video_rx_bytes"] += len(raw)
            counters["video_rx_last_sequence"] = packet.sequence
            continue
        if not send_enabled:
            continue
        counters["video_tx_packets"] += 1
        counters["video_tx_bytes"] += len(raw)
        counters["video_last_sequence"] = packet.sequence
        await loop.sock_sendto(sock, raw, destination)


async def _drain_stderr(
    process: asyncio.subprocess.Process,
    tail: list[str],
) -> None:
    if process.stderr is None:
        return
    while line := await process.stderr.readline():
        tail.append(line.decode(errors="replace").rstrip())
        del tail[:-30]


async def _start_video_sender(
    *,
    codec: str,
    destination: tuple[str, int],
    duration: float,
):
    ffmpeg = shutil.which("ffmpeg")
    if not ffmpeg:
        raise RuntimeError("FFmpeg is required for the video qualification peer")
    profile = VIDEO_PROFILES[codec]
    command = [
        ffmpeg,
        "-hide_banner",
        "-loglevel",
        "warning",
        "-nostdin",
        "-re",
        "-f",
        "lavfi",
        "-i",
        f"testsrc2=size={profile['size']}:rate=15",
        "-t",
        str(max(2.0, duration + 2.0)),
        "-an",
        "-c:v",
        str(profile["encoder"]),
        *profile["args"],
        "-pix_fmt",
        "yuv420p",
        "-g",
        "15",
        "-f",
        "rtp",
        "-payload_type",
        str(profile["payload_type"]),
        f"rtp://{destination[0]}:{destination[1]}?pkt_size=1200",
    ]
    return await asyncio.create_subprocess_exec(
        *command,
        stdout=asyncio.subprocess.DEVNULL,
        stderr=asyncio.subprocess.PIPE,
    )


async def async_main(args: argparse.Namespace) -> int:
    local_ip = args.local_ip or _local_ip(args.host, args.port)
    sip_socket = _reserve_udp_socket(local_ip)
    audio_socket = _reserve_udp_socket(local_ip)
    if args.codec == "audio":
        video_port, video_socket, rtcp_socket = 0, None, None
    else:
        video_port, video_socket, rtcp_socket = _reserve_video_ports(local_ip)
    loop = asyncio.get_running_loop()
    sip_port = int(sip_socket.getsockname()[1])
    audio_port = int(audio_socket.getsockname()[1])
    call_id = f"video-lab-{secrets.token_hex(10)}@{local_ip}"
    local_tag = secrets.token_hex(8)
    remote_uri = f"sip:{args.target}@{args.host}:{args.port};transport=udp"
    invite_branch = f"z9hG4bK{secrets.token_hex(8)}"
    invite = sip.build_request(
        "INVITE",
        remote_uri,
        _request_headers(
            method="INVITE",
            local_ip=local_ip,
            local_port=sip_port,
            local_user=args.user,
            remote_uri=remote_uri,
            call_id=call_id,
            local_tag=local_tag,
            cseq=1,
            branch=invite_branch,
            content_type="application/sdp",
        ),
        _offer(
            local_ip=local_ip,
            audio_port=audio_port,
            video_port=video_port,
            codec=args.codec,
            direction=args.direction,
            video_profile=args.video_profile,
        ),
    )
    result: dict = {
        "ok": False,
        "codec": args.codec,
        "video_profile": args.video_profile,
        "call_id": call_id,
        "local_sip": f"{local_ip}:{sip_port}",
        "local_audio_rtp": audio_port,
        "local_video_rtp": video_port,
        "sip_statuses": [],
        "audio_tx_packets": 0,
        "audio_rx_packets": 0,
        "video_tx_packets": 0,
        "video_tx_bytes": 0,
        "video_rx_packets": 0,
        "video_rx_bytes": 0,
        "video_rtcp_rx_packets": 0,
        "video_rtcp_invalid_packets": 0,
        "video_rtcp_packet_types": [],
    }
    video_process = None
    tasks: list[asyncio.Task] = []
    stopped = asyncio.Event()
    answered = False
    remote_bye = False
    bye_sent = False
    remote_to = ""
    started = time.monotonic()
    try:
        await loop.sock_sendto(sip_socket, invite, (args.host, args.port))
        response = None
        try:
            async with asyncio.timeout(args.answer_timeout):
                while response is None:
                    # Keep one receive coroutine alive. Repeatedly cancelling
                    # sock_recvfrom on a timer boundary can race a final 200
                    # OK on the same event-loop turn.
                    raw, _addr = await loop.sock_recvfrom(sip_socket, 65535)
                    message = sip.parse_message(raw)
                    if message.status_code is None or message.header("Call-ID") != call_id:
                        continue
                    result["sip_statuses"].append(int(message.status_code))
                    print(f"SIP {message.status_code} {message.reason}", flush=True)
                    if message.status_code >= 300:
                        raise RuntimeError(
                            f"SIP call failed: {message.status_code} {message.reason}"
                        )
                    if 200 <= message.status_code < 300:
                        response = message
        except TimeoutError as err:
            raise TimeoutError("SIP call was not answered") from err
        if response is None:
            raise TimeoutError("SIP call was not answered")
        answered = True
        remote_to = response.header("To")
        result["answer_cseq"] = response.header("CSeq")
        result["answer_sdp"] = response.body.decode(errors="replace")

        answer_audio = sdp.parse_sdp(response.body)
        answer_video = sdp.offered_video_formats(response.body)
        if args.codec != "audio" and not answer_video and not args.allow_audio_only:
            raise RuntimeError("HA answered without an active video media section")
        parsed_video = sdp.parse_video_sdp(response.body) if answer_video else None
        result["remote_audio_rtp"] = int(answer_audio["media_port"])
        if answer_video and parsed_video is not None:
            selected_video = answer_video[0]
            result.update(
                {
                    "negotiated_video": selected_video.wire_token(),
                    "answer_video_direction": selected_video.direction,
                    "remote_video_rtp": int(parsed_video["media_port"]),
                }
            )
        else:
            result["negotiated_video"] = ""
            result["answer_video_direction"] = "inactive"
            result["remote_video_rtp"] = 0
        ack = sip.build_request(
            "ACK",
            remote_uri,
            _request_headers(
                method="ACK",
                local_ip=local_ip,
                local_port=sip_port,
                local_user=args.user,
                remote_uri=remote_uri,
                remote_to=remote_to,
                call_id=call_id,
                local_tag=local_tag,
                cseq=1,
                branch=f"z9hG4bK{secrets.token_hex(8)}",
            ),
        )
        await loop.sock_sendto(sip_socket, ack, (args.host, args.port))
        tasks = [
            asyncio.create_task(
                _send_pcma(
                    audio_socket,
                    (str(answer_audio["connection_ip"]), int(answer_audio["media_port"])),
                    stopped,
                    result,
                )
            ),
            asyncio.create_task(_receive_audio(audio_socket, stopped, result)),
        ]
        if parsed_video is not None and video_socket is not None and rtcp_socket is not None:
            tasks.append(asyncio.create_task(_receive_rtcp(rtcp_socket, stopped, result)))
            tasks.append(
                asyncio.create_task(
                    _relay_video(
                        video_socket,
                        (str(parsed_video["connection_ip"]), int(parsed_video["media_port"])),
                        stopped,
                        result,
                        send_enabled=args.direction in {"sendonly", "sendrecv"},
                    )
                )
            )
        video_stderr: list[str] = []
        if parsed_video is not None and args.direction in {"sendonly", "sendrecv"}:
            video_process = await _start_video_sender(
                codec=args.codec,
                destination=(local_ip, video_port),
                duration=args.duration,
            )
            tasks.append(asyncio.create_task(_drain_stderr(video_process, video_stderr)))

        call_deadline = loop.time() + args.duration
        while loop.time() < call_deadline:
            if (
                video_process is not None
                and video_process.returncode is not None
                and video_process.returncode != 0
            ):
                raise RuntimeError(
                    f"FFmpeg video sender failed ({video_process.returncode}): "
                    + "\n".join(video_stderr)
                )
            try:
                raw, addr = await asyncio.wait_for(
                    loop.sock_recvfrom(sip_socket, 65535),
                    min(0.5, max(0.05, call_deadline - loop.time())),
                )
            except TimeoutError:
                continue
            message = sip.parse_message(raw)
            if message.header("Call-ID") != call_id:
                continue
            if message.method == "BYE":
                await loop.sock_sendto(
                    sip_socket,
                    sip.build_response(200, "OK", _response_headers(message)),
                    addr,
                )
                remote_bye = True
                break
        result["remote_bye"] = remote_bye

        if not remote_bye:
            bye = sip.build_request(
                "BYE",
                remote_uri,
                _request_headers(
                    method="BYE",
                    local_ip=local_ip,
                    local_port=sip_port,
                    local_user=args.user,
                    remote_uri=remote_uri,
                    remote_to=remote_to,
                    call_id=call_id,
                    local_tag=local_tag,
                    cseq=2,
                    branch=f"z9hG4bK{secrets.token_hex(8)}",
                ),
            )
            await loop.sock_sendto(sip_socket, bye, (args.host, args.port))
            bye_sent = True
        result["ok"] = True
    except BaseException as err:
        result["error"] = f"{type(err).__name__}: {err}"
        raise
    finally:
        stopped.set()
        if not answered and not any(
            int(status) >= 200 for status in result.get("sip_statuses", [])
        ):
            cancel = sip.build_request(
                "CANCEL",
                remote_uri,
                _request_headers(
                    method="CANCEL",
                    local_ip=local_ip,
                    local_port=sip_port,
                    local_user=args.user,
                    remote_uri=remote_uri,
                    call_id=call_id,
                    local_tag=local_tag,
                    cseq=1,
                    branch=invite_branch,
                ),
            )
            with contextlib.suppress(OSError):
                await loop.sock_sendto(sip_socket, cancel, (args.host, args.port))
                await asyncio.sleep(0.05)
        elif answered and not remote_bye and not bye_sent and remote_to:
            bye = sip.build_request(
                "BYE",
                remote_uri,
                _request_headers(
                    method="BYE",
                    local_ip=local_ip,
                    local_port=sip_port,
                    local_user=args.user,
                    remote_uri=remote_uri,
                    remote_to=remote_to,
                    call_id=call_id,
                    local_tag=local_tag,
                    cseq=2,
                    branch=f"z9hG4bK{secrets.token_hex(8)}",
                ),
            )
            with contextlib.suppress(OSError):
                await loop.sock_sendto(sip_socket, bye, (args.host, args.port))
                await asyncio.sleep(0.05)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        if video_process is not None and video_process.returncode is None:
            video_process.terminate()
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(video_process.wait(), 2.0)
            if video_process.returncode is None:
                video_process.kill()
                await video_process.wait()
        if video_process is not None:
            result["video_sender_returncode"] = video_process.returncode
        if "video_stderr" in locals() and video_stderr:
            result["video_sender_stderr_tail"] = list(video_stderr)
        result["elapsed_s"] = round(time.monotonic() - started, 3)
        Path(args.out).write_text(json.dumps(result, indent=2, ensure_ascii=False))
        sip_socket.close()
        audio_socket.close()
        if video_socket is not None:
            video_socket.close()
        if rtcp_socket is not None:
            rtcp_socket.close()
        print(json.dumps(result, indent=2, ensure_ascii=False), flush=True)
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=15060)
    parser.add_argument("--target", default="VoIP%20Stack%20Lab")
    parser.add_argument("--user", default="video-lab-peer")
    parser.add_argument("--local-ip", default="")
    parser.add_argument("--codec", choices=("audio", *sorted(VIDEO_PROFILES)), required=True)
    parser.add_argument(
        "--direction",
        choices=("sendonly", "recvonly", "sendrecv"),
        default="sendonly",
        help="SDP direction advertised by the qualification caller",
    )
    parser.add_argument(
        "--video-profile",
        choices=("RTP/AVP", "RTP/AVPF"),
        default="RTP/AVP",
        help="video RTP profile; feedback attributes are emitted only for AVPF",
    )
    parser.add_argument("--answer-timeout", type=float, default=60.0)
    parser.add_argument(
        "--allow-audio-only",
        action="store_true",
        help="accept an audio-only answer when an offered video codec is rejected",
    )
    parser.add_argument("--duration", type=float, default=15.0)
    parser.add_argument("--out", default="/tmp/experimental_sip_video_peer.json")
    args = parser.parse_args()
    try:
        return asyncio.run(async_main(args))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
