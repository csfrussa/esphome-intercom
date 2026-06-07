"""PBX-lite wire protocol helpers shared by HA transports.

This is the HA-side protocol library. Keep it aligned with
docs/INTERCOM_PROTOCOL.md and
esphome/components/intercom_api/intercom_protocol.h.
"""

from __future__ import annotations

import struct

from .const import (
    HEADER_SIZE,
    MAX_CALL_ID_LEN,
    MAX_NAME_LEN,
    MAX_REASON_LEN,
    MAX_ROUTE_ID_LEN,
)


MAX_PAYLOAD_SIZE = 2048
_HEADER_STRUCT = struct.Struct("<BH")


def build_header(msg_type: int, length: int) -> bytes:
    """Encode a PBX-lite header: msg_type:u8 + length:u16 little-endian."""
    if not 0 <= msg_type <= 0xFF:
        raise ValueError(f"msg_type out of range: {msg_type}")
    if not 0 <= length <= 0xFFFF:
        raise ValueError(f"length out of range: {length}")
    return _HEADER_STRUCT.pack(msg_type, length)


def parse_header(data: bytes) -> tuple[int, int]:
    """Decode a 3-byte PBX-lite header."""
    if len(data) < HEADER_SIZE:
        raise ValueError(f"header truncated ({len(data)} < {HEADER_SIZE})")
    return _HEADER_STRUCT.unpack(data[:HEADER_SIZE])


def build_frame(msg_type: int, body: bytes = b"") -> bytes:
    """Return header + body for a framed TCP/UDP-control message."""
    if len(body) > MAX_PAYLOAD_SIZE:
        raise ValueError(f"payload too large ({len(body)} > {MAX_PAYLOAD_SIZE})")
    return build_header(msg_type, len(body)) + body


def encode_call_id_prefix(call_id: str) -> bytes:
    """Encode the common prefix: call_id_len + call_id_utf8."""
    cid = call_id.encode("utf-8")
    if len(cid) > MAX_CALL_ID_LEN:
        raise ValueError(f"call_id too long ({len(cid)} > {MAX_CALL_ID_LEN})")
    return bytes([len(cid)]) + cid


def decode_call_id_prefix(data: bytes) -> tuple[str, int]:
    """Return (call_id, bytes_consumed). Raises on malformed input."""
    if len(data) < 1:
        raise ValueError("call_id prefix truncated (need >=1 byte)")
    cid_len = data[0]
    if len(data) < 1 + cid_len:
        raise ValueError(
            f"call_id truncated (header says {cid_len}, have {len(data) - 1})"
        )
    call_id = data[1 : 1 + cid_len].decode("utf-8", errors="replace")
    return call_id, 1 + cid_len


def encode_lp_string(s: str, max_len: int) -> bytes:
    """Length-prefixed UTF-8 string: u8 len + bytes."""
    raw = s.encode("utf-8")
    if len(raw) > max_len:
        raise ValueError(f"string too long ({len(raw)} > {max_len})")
    if len(raw) > 0xFF:
        raise ValueError(f"string exceeds u8 length prefix ({len(raw)} > 255)")
    return bytes([len(raw)]) + raw


def decode_lp_string(data: bytes) -> tuple[str, int]:
    """Decode a length-prefixed UTF-8 string. Returns (value, bytes_consumed)."""
    if len(data) < 1:
        raise ValueError("lp_string truncated (need >=1 byte)")
    n = data[0]
    if len(data) < 1 + n:
        raise ValueError(f"lp_string truncated (header says {n}, have {len(data) - 1})")
    return data[1 : 1 + n].decode("utf-8", errors="replace"), 1 + n


def build_start_body(
    call_id: str,
    caller_route: str,
    caller_name: str,
    dest_route: str,
    dest_name: str,
) -> bytes:
    """MSG_START body: prefix + caller_route + caller_name + dest_route + dest_name."""
    return (
        encode_call_id_prefix(call_id)
        + encode_lp_string(caller_route, MAX_ROUTE_ID_LEN)
        + encode_lp_string(caller_name, MAX_NAME_LEN)
        + encode_lp_string(dest_route, MAX_ROUTE_ID_LEN)
        + encode_lp_string(dest_name, MAX_NAME_LEN)
    )


def parse_start_body(body: bytes) -> dict:
    """Parse MSG_START body."""
    call_id, off = decode_call_id_prefix(body)
    caller_route, n = decode_lp_string(body[off:])
    off += n
    caller_name, n = decode_lp_string(body[off:])
    off += n
    dest_route, n = decode_lp_string(body[off:])
    off += n
    dest_name, n = decode_lp_string(body[off:])
    off += n
    return {
        "call_id": call_id,
        "caller_route": caller_route,
        "caller_name": caller_name,
        "dest_route": dest_route,
        "dest_name": dest_name,
    }


def build_call_id_only_body(call_id: str) -> bytes:
    """Body for MSG_RING / MSG_ANSWER / MSG_HANGUP (just the prefix)."""
    return encode_call_id_prefix(call_id)


def build_decline_body(call_id: str, reason: str = "") -> bytes:
    """MSG_DECLINE body: prefix + reason (lp_string, possibly empty)."""
    return encode_call_id_prefix(call_id) + encode_lp_string(reason, MAX_REASON_LEN)


def parse_decline_body(body: bytes) -> dict:
    """Parse MSG_DECLINE body. Returns call_id, reason."""
    call_id, off = decode_call_id_prefix(body)
    reason, _ = decode_lp_string(body[off:])
    return {"call_id": call_id, "reason": reason}


def build_error_body(call_id: str, error_code: int, detail: str = "") -> bytes:
    """MSG_ERROR body: prefix + error_code(u8) + detail(lp_string)."""
    if not 0 <= error_code <= 0xFF:
        raise ValueError(f"error_code out of range: {error_code}")
    return (
        encode_call_id_prefix(call_id)
        + bytes([error_code])
        + encode_lp_string(detail, MAX_REASON_LEN)
    )


def parse_error_body(body: bytes) -> dict:
    """Parse MSG_ERROR body. Returns call_id, error_code, detail."""
    call_id, off = decode_call_id_prefix(body)
    if len(body) < off + 1:
        raise ValueError("MSG_ERROR body missing error_code")
    error_code = body[off]
    off += 1
    detail, _ = decode_lp_string(body[off:])
    return {
        "call_id": call_id,
        "error_code": error_code,
        "detail": detail,
    }
