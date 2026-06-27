#!/usr/bin/env python3
"""Small SIP interoperability probe for ESPHome Intercom endpoints.

This is intentionally a plain external SIP peer. It does not import project
SIP code, so it can catch interoperability mistakes in the firmware stack.
"""

from __future__ import annotations

import argparse
import random
import socket
import sys
import time
from dataclasses import dataclass


CRLF = "\r\n"


def local_ip_for(remote_host: str) -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect((remote_host, 9))
        return sock.getsockname()[0]
    finally:
        sock.close()


def read_sip_message(sock: socket.socket, timeout: float = 3.0) -> str:
    sock.settimeout(timeout)
    data = bytearray()
    header_end = -1
    content_length = 0
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            break
        data.extend(chunk)
        if header_end < 0:
            header_end = data.find(b"\r\n\r\n")
            if header_end >= 0:
                headers = data[:header_end].decode("utf-8", "replace")
                for line in headers.splitlines():
                    if line.lower().startswith("content-length:"):
                        content_length = int(line.split(":", 1)[1].strip() or "0")
                        break
        if header_end >= 0 and len(data) >= header_end + 4 + content_length:
            break
    return bytes(data).decode("utf-8", "replace")


def parse_status(message: str) -> tuple[int, str]:
    first = message.splitlines()[0] if message else ""
    parts = first.split(" ", 2)
    if len(parts) >= 2 and parts[0] == "SIP/2.0":
        return int(parts[1]), parts[2] if len(parts) > 2 else ""
    return 0, first


def header(message: str, name: str) -> str:
    prefix = name.lower() + ":"
    for line in message.splitlines():
        if line.lower().startswith(prefix):
            return line.split(":", 1)[1].strip()
    return ""


@dataclass
class Dialog:
    call_id: str
    from_tag: str
    cseq: int
    branch: str
    local_uri: str
    remote_uri: str
    contact_uri: str


def build_message(start: str, headers: list[tuple[str, str]], body: str = "") -> bytes:
    full_headers = list(headers)
    if body:
        full_headers.append(("Content-Type", "application/sdp"))
    full_headers.append(("Content-Length", str(len(body.encode()))))
    text = start + CRLF
    text += "".join(f"{k}: {v}{CRLF}" for k, v in full_headers)
    text += CRLF + body
    return text.encode()


def make_dialog(local_ip: str, local_port: int, target_uri: str, caller: str) -> Dialog:
    token = f"{random.getrandbits(64):016x}"
    return Dialog(
        call_id=token,
        from_tag=f"tag{random.getrandbits(48):012x}",
        cseq=random.randint(1000, 60000),
        branch=f"z9hG4bK{random.getrandbits(64):016x}",
        local_uri=f"<sip:{caller}@{local_ip}>",
        remote_uri=f"<{target_uri}>",
        contact_uri=f"<sip:{caller}@{local_ip}:{local_port};transport=tcp>",
    )


def common_headers(dialog: Dialog, local_ip: str, local_port: int, method: str) -> list[tuple[str, str]]:
    return [
        ("Via", f"SIP/2.0/TCP {local_ip}:{local_port};branch={dialog.branch};rport"),
        ("Max-Forwards", "70"),
        ("To", dialog.remote_uri),
        ("From", f'"CodexCLI" {dialog.local_uri};tag={dialog.from_tag}'),
        ("Call-ID", dialog.call_id),
        ("CSeq", f"{dialog.cseq} {method}"),
        ("Contact", dialog.contact_uri),
        ("User-Agent", "codex-sip-cli-probe/1"),
        ("Allow", "INVITE, ACK, BYE, CANCEL, OPTIONS"),
    ]


def options(sock: socket.socket, target_uri: str, target_host: str, caller: str) -> list[str]:
    local_ip, local_port = sock.getsockname()
    dialog = make_dialog(local_ip, local_port, target_uri, caller)
    msg = build_message(
        f"OPTIONS {target_uri} SIP/2.0",
        common_headers(dialog, local_ip, local_port, "OPTIONS"),
    )
    sock.sendall(msg)
    return [read_sip_message(sock)]


def sdp(profile: str, local_ip: str, rtp_port: int) -> str:
    if profile == "l16":
        media = [
            f"m=audio {rtp_port} RTP/AVP 96",
            "a=rtpmap:96 L16/16000/1",
            "a=ptime:32",
            "a=maxptime:32",
            "a=sendrecv",
        ]
    elif profile == "g711":
        media = [
            f"m=audio {rtp_port} RTP/AVP 0 8",
            "a=rtpmap:0 PCMU/8000/1",
            "a=rtpmap:8 PCMA/8000/1",
            "a=ptime:20",
            "a=maxptime:20",
            "a=sendrecv",
        ]
    else:
        raise ValueError(profile)
    lines = [
        "v=0",
        f"o=- {random.randint(1, 2**31)} 1 IN IP4 {local_ip}",
        "s=codex-sip-cli-probe",
        f"c=IN IP4 {local_ip}",
        "t=0 0",
        *media,
    ]
    return CRLF.join(lines) + CRLF


def invite(sock: socket.socket, target_uri: str, caller: str, profile: str, rtp_port: int) -> list[str]:
    local_ip, local_port = sock.getsockname()
    dialog = make_dialog(local_ip, local_port, target_uri, caller)
    body = sdp(profile, local_ip, rtp_port)
    msg = build_message(
        f"INVITE {target_uri} SIP/2.0",
        common_headers(dialog, local_ip, local_port, "INVITE"),
        body,
    )
    sock.sendall(msg)
    responses: list[str] = []
    to_header = dialog.remote_uri
    for _ in range(4):
        resp = read_sip_message(sock, timeout=5.0)
        if not resp:
            break
        responses.append(resp)
        status, _ = parse_status(resp)
        if status >= 200:
            to_header = header(resp, "To") or to_header
            break

    if responses:
        status, _ = parse_status(responses[-1])
        if 200 <= status < 300:
            ack_headers = common_headers(dialog, local_ip, local_port, "ACK")
            ack_headers = [(k, to_header if k == "To" else v) for k, v in ack_headers]
            sock.sendall(build_message(f"ACK {target_uri} SIP/2.0", ack_headers))
            time.sleep(0.2)
            dialog.cseq += 1
            bye_headers = common_headers(dialog, local_ip, local_port, "BYE")
            bye_headers = [(k, to_header if k == "To" else v) for k, v in bye_headers]
            sock.sendall(build_message(f"BYE {target_uri} SIP/2.0", bye_headers))
            try:
                responses.append(read_sip_message(sock, timeout=3.0))
            except socket.timeout:
                pass
    return responses


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=("options", "invite"))
    parser.add_argument("target_uri")
    parser.add_argument("--host", required=True)
    parser.add_argument("--port", type=int, default=5060)
    parser.add_argument("--caller", default="CodexCLI")
    parser.add_argument("--profile", choices=("l16", "g711"), default="l16")
    parser.add_argument("--rtp-port", type=int, default=39100)
    args = parser.parse_args()

    with socket.create_connection((args.host, args.port), timeout=5.0) as sock:
        if args.command == "options":
            responses = options(sock, args.target_uri, args.host, args.caller)
        else:
            responses = invite(sock, args.target_uri, args.caller, args.profile, args.rtp_port)
    for idx, response in enumerate(responses, 1):
        status, reason = parse_status(response)
        print(f"--- response {idx}: {status} {reason}")
        print(response)
    return 0


if __name__ == "__main__":
    sys.exit(main())
