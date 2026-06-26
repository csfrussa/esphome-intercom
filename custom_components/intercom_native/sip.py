"""Strict SIP/2.0 helpers for the intercom VoIP profile.

This module intentionally implements a small standards-compatible subset of
SIP rather than a proprietary replacement. Unsupported methods/features are
handled by policy at the call layer, but the messages built and parsed here are
ordinary SIP/2.0 messages.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from secrets import token_hex
from typing import Iterable


CRLF = "\r\n"
SIP_VERSION = "SIP/2.0"
MAX_SIP_MESSAGE_BYTES = 8192
MAX_SIP_BODY_BYTES = 4096
SUPPORTED_METHODS = frozenset({"INVITE", "ACK", "BYE", "CANCEL", "OPTIONS"})


class SipError(ValueError):
    """Malformed or unsupported SIP message."""


@dataclass(frozen=True, slots=True)
class SipUri:
    user: str
    host: str
    port: int | None = None
    params: tuple[tuple[str, str | None], ...] = ()

    def __str__(self) -> str:
        user = self.user.strip()
        host = self.host.strip()
        if not user or not host:
            raise SipError("SIP URI requires non-empty user and host")
        uri = f"sip:{user}@{host}"
        if self.port is not None:
            if not 1 <= int(self.port) <= 65535:
                raise SipError(f"SIP URI port out of range: {self.port}")
            uri += f":{int(self.port)}"
        for key, value in self.params:
            if not key:
                continue
            uri += f";{key}" if value is None else f";{key}={value}"
        return uri


@dataclass(frozen=True, slots=True)
class SipMessage:
    method: str | None = None
    uri: str = ""
    status_code: int | None = None
    reason: str = ""
    headers: tuple[tuple[str, str], ...] = ()
    body: bytes = b""

    @property
    def is_request(self) -> bool:
        return self.method is not None

    @property
    def is_response(self) -> bool:
        return self.status_code is not None

    def header_values(self, name: str) -> list[str]:
        wanted = name.lower()
        return [value for key, value in self.headers if key.lower() == wanted]

    def header(self, name: str, default: str = "") -> str:
        values = self.header_values(name)
        return values[-1] if values else default


@dataclass(slots=True)
class SipDialogIds:
    call_id: str
    local_tag: str
    remote_tag: str = ""
    cseq: int = 1
    branch: str = field(default_factory=lambda: make_branch())


def make_call_id(prefix: str = "intercom") -> str:
    return f"{prefix}-{token_hex(12)}"


def make_tag() -> str:
    return token_hex(8)


def make_branch() -> str:
    return "z9hG4bK" + token_hex(10)


def parse_sip_uri(value: str) -> SipUri:
    raw = value.strip()
    if raw.startswith("<") and raw.endswith(">"):
        raw = raw[1:-1].strip()
    if not raw.lower().startswith("sip:"):
        raise SipError(f"not a sip URI: {value!r}")
    rest = raw[4:]
    if "@" not in rest:
        raise SipError("SIP URI missing @host")
    user, host_params = rest.split("@", 1)
    params_raw: list[str] = []
    if ";" in host_params:
        host_part, params_part = host_params.split(";", 1)
        params_raw = [p for p in params_part.split(";") if p]
    else:
        host_part = host_params
    port: int | None = None
    host = host_part
    if host_part.count(":") == 1 and not host_part.startswith("["):
        host, port_raw = host_part.rsplit(":", 1)
        if port_raw:
            port = int(port_raw)
    params: list[tuple[str, str | None]] = []
    for param in params_raw:
        if "=" in param:
            key, val = param.split("=", 1)
            params.append((key.strip(), val.strip()))
        else:
            params.append((param.strip(), None))
    uri = SipUri(user=user.strip(), host=host.strip(), port=port, params=tuple(params))
    str(uri)
    return uri


def _split_header_body(data: bytes) -> tuple[str, bytes]:
    if len(data) > MAX_SIP_MESSAGE_BYTES:
        raise SipError("SIP message too large")
    marker = b"\r\n\r\n"
    split = data.find(marker)
    if split < 0:
        raise SipError("SIP message missing CRLF CRLF separator")
    try:
        head = data[:split].decode("utf-8", errors="strict")
    except UnicodeDecodeError as err:
        raise SipError("SIP header is not strict UTF-8") from err
    return head, data[split + len(marker):]


def parse_message(data: bytes) -> SipMessage:
    head, body_tail = _split_header_body(data)
    lines = head.split(CRLF)
    if not lines or not lines[0]:
        raise SipError("empty SIP start line")
    headers: list[tuple[str, str]] = []
    for line in lines[1:]:
        if not line:
            continue
        if line.startswith((" ", "\t")):
            raise SipError("folded SIP headers are not supported")
        if ":" not in line:
            raise SipError(f"malformed SIP header: {line!r}")
        key, value = line.split(":", 1)
        key = key.strip()
        if not key:
            raise SipError("empty SIP header name")
        headers.append((key, value.strip()))

    content_lengths = [v for k, v in headers if k.lower() == "content-length"]
    content_length = int(content_lengths[-1]) if content_lengths else 0
    if content_length < 0 or content_length > MAX_SIP_BODY_BYTES:
        raise SipError("invalid SIP Content-Length")
    if len(body_tail) < content_length:
        raise SipError("SIP body shorter than Content-Length")
    body = body_tail[:content_length]
    if body_tail[content_length:]:
        raise SipError("SIP message has trailing bytes after Content-Length")

    start = lines[0]
    parts = start.split(" ", 2)
    if start.startswith(SIP_VERSION + " "):
        if len(parts) < 3:
            raise SipError("malformed SIP status line")
        code = int(parts[1])
        if not 100 <= code <= 699:
            raise SipError(f"SIP status code out of range: {code}")
        return SipMessage(status_code=code, reason=parts[2], headers=tuple(headers), body=body)

    if len(parts) != 3 or parts[2] != SIP_VERSION:
        raise SipError("malformed SIP request line")
    method = parts[0].upper()
    if method not in SUPPORTED_METHODS:
        raise SipError(f"unsupported SIP method {method}")
    parse_sip_uri(parts[1])
    return SipMessage(method=method, uri=parts[1], headers=tuple(headers), body=body)


def _render_headers(headers: Iterable[tuple[str, str]], body: bytes) -> str:
    out: list[str] = []
    saw_content_length = False
    for key, value in headers:
        if key.lower() == "content-length":
            saw_content_length = True
            value = str(len(body))
        out.append(f"{key}: {value}")
    if not saw_content_length:
        out.append(f"Content-Length: {len(body)}")
    return CRLF.join(out)


def build_request(method: str, uri: str | SipUri, headers: Iterable[tuple[str, str]], body: bytes = b"") -> bytes:
    method = method.upper()
    if method not in SUPPORTED_METHODS:
        raise SipError(f"unsupported SIP method {method}")
    uri_text = str(uri)
    parse_sip_uri(uri_text)
    body = body or b""
    head = f"{method} {uri_text} {SIP_VERSION}{CRLF}{_render_headers(headers, body)}"
    return head.encode("utf-8") + b"\r\n\r\n" + body


def build_response(status_code: int, reason: str, headers: Iterable[tuple[str, str]], body: bytes = b"") -> bytes:
    if not 100 <= int(status_code) <= 699:
        raise SipError(f"SIP status code out of range: {status_code}")
    body = body or b""
    head = f"{SIP_VERSION} {int(status_code)} {reason}{CRLF}{_render_headers(headers, body)}"
    return head.encode("utf-8") + b"\r\n\r\n" + body


def dialog_headers(
    *,
    request_uri: str,
    local_uri: str,
    remote_uri: str,
    dialog: SipDialogIds,
    method: str,
    contact_uri: str,
    max_forwards: int = 70,
    content_type: str | None = None,
    transport: str = "UDP",
) -> list[tuple[str, str]]:
    """Build the common headers used by the ESP/HA phase-1 profile."""
    contact = parse_sip_uri(contact_uri)
    sent_by = contact.host
    if contact.port:
        sent_by = f"{sent_by}:{contact.port}"
    via_transport = (transport or "UDP").strip().upper()
    if via_transport not in {"UDP", "TCP"}:
        raise SipError(f"unsupported SIP transport {transport!r}")
    headers = [
        ("Via", f"SIP/2.0/{via_transport} {sent_by};branch={dialog.branch};rport"),
        ("Max-Forwards", str(max_forwards)),
        ("From", f"<{local_uri}>;tag={dialog.local_tag}"),
        ("To", f"<{remote_uri}>" + (f";tag={dialog.remote_tag}" if dialog.remote_tag else "")),
        ("Call-ID", dialog.call_id),
        ("CSeq", f"{dialog.cseq} {method.upper()}"),
        ("Contact", f"<{contact_uri}>"),
        ("Allow", ", ".join(sorted(SUPPORTED_METHODS))),
    ]
    if content_type:
        headers.append(("Content-Type", content_type))
    parse_sip_uri(request_uri)
    return headers
