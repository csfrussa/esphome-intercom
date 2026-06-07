#!/usr/bin/env python3
"""Probe intercom_native or an ESP intercom endpoint with a synthetic caller.

Usage:
  HA_TOKEN=... tools/intercom_softphone_probe.py --ha 192.168.1.10
  tools/intercom_softphone_probe.py --target 192.168.1.47 --dest-name "WS3" --expect answer
  tools/intercom_softphone_probe.py --target 192.168.1.47 --send-tone 880 --record-wav /tmp/ws3.wav

When HA_TOKEN is available the script can also subscribe to Home Assistant's
intercom_native.call_event stream. Audio on the wire is the project protocol:
16 kHz mono signed 16-bit PCM, 512 samples / 1024 bytes per chunk.
"""

from __future__ import annotations

import argparse
import base64
import json
import math
import os
import socket
import struct
import sys
import threading
import time
import urllib.request
import wave


INTERCOM_PORT = 6054
HEADER_SIZE = 3
MAX_PAYLOAD_SIZE = 2048
MAX_CALL_ID_LEN = 64
MAX_ROUTE_ID_LEN = 64
MAX_NAME_LEN = 64
MAX_REASON_LEN = 160
MSG_START = 0x02
MSG_HANGUP = 0x03
MSG_PING = 0x04
MSG_PONG = 0x05
MSG_ERROR = 0x06
MSG_RING = 0x07
MSG_ANSWER = 0x08
MSG_DECLINE = 0x09
MSG_AUDIO = 0x01

SAMPLE_RATE = 16000
AUDIO_CHUNK_BYTES = 1024
AUDIO_CHUNK_SAMPLES = AUDIO_CHUNK_BYTES // 2
AUDIO_CHUNK_SECONDS = AUDIO_CHUNK_SAMPLES / SAMPLE_RATE


OP_TEXT = 0x1
OP_CLOSE = 0x8
OP_PING = 0x9
OP_PONG = 0xA


def build_header(msg_type: int, length: int) -> bytes:
    return struct.pack("<BH", msg_type, length)


def parse_header(data: bytes) -> tuple[int, int]:
    if len(data) < HEADER_SIZE:
        raise ValueError("short header")
    return struct.unpack("<BH", data[:HEADER_SIZE])


def build_frame(msg_type: int, body: bytes = b"") -> bytes:
    if len(body) > MAX_PAYLOAD_SIZE:
        raise ValueError("payload too large")
    return build_header(msg_type, len(body)) + body


def encode_lp_string(value: str, max_len: int) -> bytes:
    raw = value.encode("utf-8")
    if len(raw) > max_len:
        raise ValueError(f"string too long ({len(raw)} > {max_len})")
    return bytes([len(raw)]) + raw


def encode_call_id_prefix(call_id: str) -> bytes:
    raw = call_id.encode("utf-8")
    if len(raw) > MAX_CALL_ID_LEN:
        raise ValueError(f"call_id too long ({len(raw)} > {MAX_CALL_ID_LEN})")
    return bytes([len(raw)]) + raw


def build_start_body(
    call_id: str,
    caller_route: str,
    caller_name: str,
    dest_route: str,
    dest_name: str,
) -> bytes:
    return (
        encode_call_id_prefix(call_id)
        + encode_lp_string(caller_route, MAX_ROUTE_ID_LEN)
        + encode_lp_string(caller_name, MAX_NAME_LEN)
        + encode_lp_string(dest_route, MAX_ROUTE_ID_LEN)
        + encode_lp_string(dest_name, MAX_NAME_LEN)
    )


def build_call_id_only_body(call_id: str) -> bytes:
    return encode_call_id_prefix(call_id)


def decode_call_id_prefix(data: bytes) -> tuple[str, int]:
    if not data:
        raise ValueError("missing call_id length")
    n = data[0]
    if len(data) < 1 + n:
        raise ValueError("truncated call_id")
    return data[1 : 1 + n].decode("utf-8", errors="replace"), 1 + n


def decode_lp_string(data: bytes) -> tuple[str, int]:
    if not data:
        raise ValueError("missing string length")
    n = data[0]
    if len(data) < 1 + n:
        raise ValueError("truncated string")
    return data[1 : 1 + n].decode("utf-8", errors="replace"), 1 + n


def describe_payload(msg_type: int, payload: bytes) -> str:
    try:
        if msg_type in (MSG_RING, MSG_ANSWER, MSG_HANGUP):
            call_id, _ = decode_call_id_prefix(payload)
            return f" call_id={call_id!r}"
        if msg_type == MSG_DECLINE:
            call_id, off = decode_call_id_prefix(payload)
            reason, _ = decode_lp_string(payload[off:])
            return f" call_id={call_id!r} reason={reason!r}"
        if msg_type == MSG_ERROR:
            call_id, off = decode_call_id_prefix(payload)
            code = payload[off] if len(payload) > off else None
            detail, _ = decode_lp_string(payload[off + 1 :]) if code is not None else ("", 0)
            return f" call_id={call_id!r} code={code} detail={detail!r}"
    except ValueError as err:
        return f" malformed={err}"
    return ""


def _http_get_json(base_url: str, token: str, path: str) -> dict:
    req = urllib.request.Request(
        f"{base_url}{path}",
        headers={"Authorization": f"Bearer {token}"},
    )
    with urllib.request.urlopen(req, timeout=5) as resp:
        return json.loads(resp.read().decode("utf-8"))


class MinimalWebSocket:
    def __init__(self, host: str, port: int, path: str) -> None:
        self.sock = socket.create_connection((host, port), timeout=5)
        self.sock.settimeout(0.5)
        self._rx_buffer = b""
        key = base64.b64encode(os.urandom(16)).decode("ascii")
        request = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "\r\n"
        ).encode("ascii")
        self.sock.sendall(request)
        response = b""
        while b"\r\n\r\n" not in response:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise RuntimeError("WebSocket HTTP upgrade closed")
            response += chunk
        header_block, _, self._rx_buffer = response.partition(b"\r\n\r\n")
        if b" 101 " not in header_block.split(b"\r\n", 1)[0]:
            raise RuntimeError(response.decode("utf-8", errors="replace"))

    def send_json(self, payload: dict) -> None:
        raw = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        self._send_frame(OP_TEXT, raw)

    def close(self) -> None:
        try:
            self._send_frame(OP_CLOSE, b"")
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass

    def _send_frame(self, opcode: int, payload: bytes) -> None:
        header = bytearray([0x80 | opcode])
        length = len(payload)
        if length < 126:
            header.append(0x80 | length)
        elif length < 65536:
            header.append(0x80 | 126)
            header.extend(struct.pack("!H", length))
        else:
            header.append(0x80 | 127)
            header.extend(struct.pack("!Q", length))
        mask = os.urandom(4)
        header.extend(mask)
        masked = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        self.sock.sendall(bytes(header) + masked)

    def recv_json(self, timeout: float = 5.0) -> dict | None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            frame = self._recv_frame(deadline - time.monotonic())
            if frame is None:
                continue
            opcode, payload = frame
            if opcode == OP_TEXT:
                return json.loads(payload.decode("utf-8"))
            if opcode == OP_PING:
                self._send_frame(OP_PONG, payload)
                continue
            if opcode == OP_CLOSE:
                return None
        return None

    def _recv_exact(self, n: int, timeout: float) -> bytes | None:
        self.sock.settimeout(max(0.05, timeout))
        data = b""
        if self._rx_buffer:
            data = self._rx_buffer[:n]
            self._rx_buffer = self._rx_buffer[n:]
        while len(data) < n:
            try:
                chunk = self.sock.recv(n - len(data))
            except socket.timeout:
                return None
            if not chunk:
                return None
            data += chunk
        return data

    def _recv_frame(self, timeout: float) -> tuple[int, bytes] | None:
        head = self._recv_exact(2, timeout)
        if head is None:
            return None
        b0, b1 = head
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        length = b1 & 0x7F
        if length == 126:
            ext = self._recv_exact(2, timeout)
            if ext is None:
                return None
            length = struct.unpack("!H", ext)[0]
        elif length == 127:
            ext = self._recv_exact(8, timeout)
            if ext is None:
                return None
            length = struct.unpack("!Q", ext)[0]
        mask = self._recv_exact(4, timeout) if masked else b""
        payload = self._recv_exact(length, timeout) if length else b""
        if payload is None:
            return None
        if masked:
            payload = bytes(b ^ mask[i % 4] for i, b in enumerate(payload))
        return opcode, payload


class EventCollector:
    def __init__(self, base_url: str, token: str) -> None:
        parsed = base_url.removeprefix("http://").removeprefix("https://")
        host, _, port_s = parsed.partition(":")
        self.host = host
        self.port = int(port_s or "8123")
        self.token = token
        self.events: list[dict] = []
        self._stop = threading.Event()
        self._ready = threading.Event()
        self._error: Exception | None = None
        self._thread: threading.Thread | None = None
        self._ws: MinimalWebSocket | None = None

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=10):
            if self._error:
                raise self._error
            raise RuntimeError("HA event subscription did not become ready")
        if self._error:
            raise self._error

    def stop(self) -> None:
        self._stop.set()
        if self._ws:
            self._ws.close()
        if self._thread:
            self._thread.join(timeout=2)
        if self._error is not None:
            print(f"HA_WS_ERROR {self._error}", file=sys.stderr)

    def _run(self) -> None:
        try:
            self._ws = MinimalWebSocket(self.host, self.port, "/api/websocket")
            ws = self._ws
            first = ws.recv_json(timeout=5)
            if not first or first.get("type") != "auth_required":
                raise RuntimeError(f"unexpected HA WS greeting: {first}")
            ws.send_json({"type": "auth", "access_token": self.token})
            auth = ws.recv_json(timeout=5)
            if not auth or auth.get("type") != "auth_ok":
                raise RuntimeError(f"HA WS auth failed: {auth}")
            ws.send_json({"id": 1, "type": "subscribe_events", "event_type": "intercom_native.call_event"})
            while True:
                msg = ws.recv_json(timeout=5)
                if not msg:
                    raise RuntimeError("HA event subscription timed out")
                if msg.get("id") == 1:
                    if not msg.get("success", False):
                        raise RuntimeError(f"HA event subscription failed: {msg}")
                    break
            ws.send_json({"id": 2, "type": "intercom_native/ha_softphone_state"})
            self._ready.set()
            while not self._stop.is_set():
                msg = ws.recv_json(timeout=0.5)
                if not msg:
                    continue
                if msg.get("type") == "event":
                    self.events.append(msg.get("event", {}).get("data", {}))
                    print("HA_EVENT", json.dumps(self.events[-1], sort_keys=True))
                elif msg.get("id") == 2:
                    if msg.get("success", True):
                        print("HA_SOFTPHONE_STATE", json.dumps(msg.get("result", {}), sort_keys=True))
                    else:
                        raise RuntimeError(f"HA softphone state command failed: {msg}")
        except OSError as err:
            if not self._stop.is_set():
                self._error = err
                self._ready.set()
        except Exception as err:
            self._error = err
            self._ready.set()


def _read_control_frame(sock: socket.socket, timeout: float) -> tuple[int, bytes] | None:
    sock.settimeout(timeout)
    try:
        header = sock.recv(HEADER_SIZE)
        if len(header) < HEADER_SIZE:
            return None
        msg_type, length = parse_header(header)
        payload = b""
        while len(payload) < length:
            chunk = sock.recv(length - len(payload))
            if not chunk:
                break
            payload += chunk
        return msg_type, payload
    except socket.timeout:
        return None


def _message_name(msg_type: int) -> str:
    return {
        MSG_AUDIO: "AUDIO",
        MSG_START: "START",
        MSG_HANGUP: "HANGUP",
        MSG_PING: "PING",
        MSG_PONG: "PONG",
        MSG_RING: "RING",
        MSG_ANSWER: "ANSWER",
        MSG_DECLINE: "DECLINE",
        MSG_ERROR: "ERROR",
    }.get(msg_type, f"0x{msg_type:02X}")


def _load_wav_chunks(path: str) -> list[bytes]:
    with wave.open(path, "rb") as wav:
        channels = wav.getnchannels()
        sample_width = wav.getsampwidth()
        sample_rate = wav.getframerate()
        if channels != 1 or sample_width != 2 or sample_rate != SAMPLE_RATE:
            raise ValueError(
                f"{path}: expected mono s16le {SAMPLE_RATE}Hz WAV, "
                f"got channels={channels} width={sample_width} rate={sample_rate}"
            )
        raw = wav.readframes(wav.getnframes())
    if len(raw) % 2:
        raw = raw[:-1]
    chunks = []
    for off in range(0, len(raw), AUDIO_CHUNK_BYTES):
        chunk = raw[off : off + AUDIO_CHUNK_BYTES]
        if len(chunk) < AUDIO_CHUNK_BYTES:
            chunk += b"\x00" * (AUDIO_CHUNK_BYTES - len(chunk))
        chunks.append(chunk)
    return chunks


def _tone_chunks(freq_hz: float, seconds: float, amplitude: float) -> list[bytes]:
    if seconds <= 0:
        return []
    amplitude_i16 = max(0, min(32767, int(32767 * amplitude)))
    total_samples = int(seconds * SAMPLE_RATE)
    chunks = []
    for base in range(0, total_samples, AUDIO_CHUNK_SAMPLES):
        samples = bytearray()
        for i in range(AUDIO_CHUNK_SAMPLES):
            n = base + i
            value = 0
            if n < total_samples:
                value = int(amplitude_i16 * math.sin(2.0 * math.pi * freq_hz * n / SAMPLE_RATE))
            samples.extend(struct.pack("<h", value))
        chunks.append(bytes(samples))
    return chunks


def _audio_chunks_from_args(args: argparse.Namespace) -> list[bytes]:
    if args.send_wav:
        return _load_wav_chunks(args.send_wav)
    if args.send_tone:
        return _tone_chunks(args.send_tone, args.tone_seconds, args.tone_amplitude)
    return []


def _write_recording(path: str, chunks: list[bytes]) -> None:
    with wave.open(path, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(SAMPLE_RATE)
        wav.writeframes(b"".join(chunks))


class AudioSender:
    def __init__(self, sock: socket.socket, chunks: list[bytes], repeat: bool) -> None:
        self.sock = sock
        self.chunks = chunks
        self.repeat = repeat
        self.sent_chunks = 0
        self.sent_bytes = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()

    def start(self) -> None:
        if not self.chunks or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

    def _run(self) -> None:
        index = 0
        next_send = time.monotonic()
        while not self._stop.is_set() and self.chunks:
            chunk = self.chunks[index]
            try:
                with self._lock:
                    self.sock.sendall(build_frame(MSG_AUDIO, chunk))
            except OSError:
                return
            self.sent_chunks += 1
            self.sent_bytes += len(chunk)
            index += 1
            if index >= len(self.chunks):
                if not self.repeat:
                    return
                index = 0
            next_send += AUDIO_CHUNK_SECONDS
            delay = next_send - time.monotonic()
            if delay > 0:
                self._stop.wait(delay)


def run_probe(args: argparse.Namespace) -> int:
    token = os.environ.get("HA_TOKEN", "")
    target_host = args.target or args.ha
    ha_events = args.ha_events
    if ha_events == "auto":
        ha_events_enabled = bool(token)
    else:
        ha_events_enabled = ha_events == "on"
    if ha_events_enabled and not token:
        print("HA_TOKEN env var is required when --ha-events=on", file=sys.stderr)
        return 2

    base_url = args.ha_url or f"http://{args.ha}:8123"
    config = _http_get_json(base_url, token, "/api/config") if ha_events_enabled else {}
    dest_name = args.dest_name or config.get("location_name") or "Home Assistant"
    call_id = args.call_id or f"{args.caller_name}<->{dest_name}:{int(time.time())}"

    collector = None
    if ha_events_enabled:
        collector = EventCollector(base_url, token)
        collector.start()
        time.sleep(0.3)

    audio_chunks = _audio_chunks_from_args(args)
    rx_audio: list[bytes] = []
    sock = socket.create_connection((target_host, args.port), timeout=5)
    sender = AudioSender(sock, audio_chunks, args.repeat_audio)
    body = build_start_body(
        call_id=call_id,
        caller_route=args.caller_route,
        caller_name=args.caller_name,
        dest_route=args.dest_route,
        dest_name=dest_name,
    )
    sock.sendall(build_frame(MSG_START, body))
    print(
        f"SENT START target={target_host!r} caller={args.caller_name!r} "
        f"dest={dest_name!r} call_id={call_id!r}"
    )
    if audio_chunks:
        print(
            f"AUDIO_TX queued chunks={len(audio_chunks)} bytes={sum(len(c) for c in audio_chunks)} "
            f"repeat={args.repeat_audio}"
        )

    deadline = time.monotonic() + args.listen_seconds
    observed: list[str] = []
    answered = False
    try:
        while time.monotonic() < deadline:
            frame = _read_control_frame(sock, timeout=0.5)
            if frame is None:
                continue
            msg_type, payload = frame
            observed.append(_message_name(msg_type).lower())
            if msg_type == MSG_AUDIO:
                rx_audio.append(payload)
                if len(rx_audio) == 1 or len(rx_audio) % 50 == 0:
                    print(f"AUDIO_RX chunks={len(rx_audio)} bytes={sum(len(c) for c in rx_audio)}")
                continue
            print(f"TCP_RX {_message_name(msg_type)} len={len(payload)}{describe_payload(msg_type, payload)}")
            if msg_type == MSG_PING:
                sock.sendall(build_frame(MSG_PONG))
            if msg_type == MSG_ANSWER:
                answered = True
                sender.start()
                if not args.record_wav and not audio_chunks:
                    time.sleep(1.0)
                    break
            if msg_type in (MSG_DECLINE, MSG_ERROR):
                time.sleep(0.5)
                break
            if msg_type == MSG_RING and not args.keep_ringing:
                time.sleep(1.0)
                break
    finally:
        sender.stop()
        try:
            sock.sendall(build_frame(MSG_HANGUP, build_call_id_only_body(call_id)))
        except Exception:
            pass
        sock.close()
        time.sleep(0.5)
        if collector is not None:
            collector.stop()
        if args.record_wav:
            _write_recording(args.record_wav, rx_audio)
            print(f"AUDIO_RX wrote {args.record_wav} chunks={len(rx_audio)} bytes={sum(len(c) for c in rx_audio)}")
        if audio_chunks:
            print(f"AUDIO_TX sent chunks={sender.sent_chunks} bytes={sender.sent_bytes} answered={answered}")

    if args.expect:
        return 0 if args.expect.lower() in observed else 1

    if not ha_events_enabled:
        return 0 if observed else 1

    assert collector is not None
    ringing_events = [
        e for e in collector.events
        if e.get("device_id") == "__intercom_native_ha_softphone__"
        and e.get("state") == "ringing"
    ]
    return 0 if ringing_events else 1


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ha", default="192.168.1.10", help="HA host/IP for event subscription and default TCP target")
    parser.add_argument("--target", default="", help="TCP intercom target host/IP; defaults to --ha")
    parser.add_argument("--ha-url", default="", help="HA base URL, default http://HA:8123")
    parser.add_argument("--ha-events", choices=("auto", "on", "off"), default="auto",
                        help="Subscribe to HA intercom events. auto enables it when HA_TOKEN is set.")
    parser.add_argument("--port", type=int, default=INTERCOM_PORT)
    parser.add_argument("--caller-name", default="Codex Fake ESP 1")
    parser.add_argument("--caller-route", default="codex-fake-1")
    parser.add_argument("--dest-name", default="")
    parser.add_argument("--dest-route", default="")
    parser.add_argument("--call-id", default="")
    parser.add_argument("--listen-seconds", type=float, default=6.0)
    parser.add_argument("--expect", choices=("ring", "answer", "decline", "error"), default="")
    parser.add_argument("--send-tone", type=float, default=0.0, metavar="HZ",
                        help="Send a generated sine tone after ANSWER, e.g. 880.")
    parser.add_argument("--tone-seconds", type=float, default=3.0)
    parser.add_argument("--tone-amplitude", type=float, default=0.20)
    parser.add_argument("--send-wav", default="", help=f"Send mono s16le {SAMPLE_RATE}Hz WAV after ANSWER.")
    parser.add_argument("--repeat-audio", action="store_true", help="Loop --send-tone/--send-wav until listen timeout.")
    parser.add_argument("--record-wav", default="", help=f"Record received AUDIO frames as mono s16le {SAMPLE_RATE}Hz WAV.")
    parser.add_argument("--keep-ringing", action="store_true", help="Do not exit immediately after RING.")
    return run_probe(parser.parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
