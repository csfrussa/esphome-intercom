"""Browser audio WebSocket for the HA SIP softphone media leg."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
import json
import logging
from pathlib import Path
import secrets
from typing import Any
import wave

from aiohttp import WSMsgType, web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from . import rtp
from .audio_ws import decode_audio_frame, encode_audio_frame
from .call_registry import CallRegistry
from .const import CONF_DEBUG_MODE, DOMAIN, HA_SOFTPHONE_DEVICE_ID
from .debug_capture import (
    DEBUG_CAPTURE_DIR,
    ensure_debug_capture_dir,
    prune_debug_captures,
    safe_capture_name,
    wav_pcm_payload,
)
from .queue_utils import put_drop_oldest
from .sip_client import RtpPayloadDecoder, RtpPayloadEncoder, SipCallClient
from .websocket_api import _fire_call_event, _ha_softphone_store

_LOGGER = logging.getLogger(__name__)
_DEBUG_CAPTURE_SECONDS = 15
_DEBUG_TIMING_MAX_SAMPLES = 4096


@dataclass(frozen=True, slots=True)
class _SoftphoneMediaSession:
    call_id: str
    local_rtp_port: int
    remote_rtp_host: str
    remote_rtp_port: int
    send_format: Any
    recv_format: Any
    conference_room: str = ""
    conference_queue: asyncio.Queue[bytes] | None = None
    local_ssrc: int = 0


class _RtpAudioProtocol(asyncio.DatagramProtocol):
    def __init__(self, queue: asyncio.Queue[tuple[bytes, tuple[str, int]]]) -> None:
        self.queue = queue
        self.dropped_packets = 0

    def datagram_received(self, data: bytes, addr) -> None:
        if put_drop_oldest(self.queue, (data, addr)):
            self.dropped_packets += 1


class _DebugAudioCapture:
    def __init__(self, call_id: str, *, rx_format: Any, tx_format: Any) -> None:
        self.call_id = str(call_id or "call")
        self.rx_format = rx_format.audio_format
        self.tx_format = tx_format.audio_format
        self.rtp_to_ws = bytearray()
        self.ws_to_rtp = bytearray()
        self.rtp_to_ws_deltas_ms: list[float] = []
        self.ws_rx_deltas_ms: list[float] = []
        self.ws_send_deltas_ms: list[float] = []
        self.rtp_tx_deltas_ms: list[float] = []
        self._last_rtp_rx: float | None = None
        self._last_ws_rx: float | None = None
        self._last_ws_send: float | None = None
        self._last_rtp_tx: float | None = None

    def note_rtp_rx(self, now: float, pcm: bytes) -> None:
        self._append_delta(self.rtp_to_ws_deltas_ms, "_last_rtp_rx", now)
        self._append_pcm(self.rtp_to_ws, pcm, self.rx_format)

    def note_ws_rx(self, now: float, pcm: bytes) -> None:
        self._append_delta(self.ws_rx_deltas_ms, "_last_ws_rx", now)
        self._append_pcm(self.ws_to_rtp, pcm, self.tx_format)

    def note_ws_send(self, now: float) -> None:
        self._append_delta(self.ws_send_deltas_ms, "_last_ws_send", now)

    def note_rtp_tx(self, now: float) -> None:
        self._append_delta(self.rtp_tx_deltas_ms, "_last_rtp_tx", now)

    def _append_delta(self, target: list[float], attr: str, now: float) -> None:
        previous = getattr(self, attr)
        if previous is not None and len(target) < _DEBUG_TIMING_MAX_SAMPLES:
            target.append((now - previous) * 1000.0)
        setattr(self, attr, now)

    @staticmethod
    def _append_pcm(target: bytearray, pcm: bytes, fmt: Any) -> None:
        max_bytes = (
            int(fmt.sample_rate)
            * int(fmt.channels)
            * int(fmt.container_bytes_per_sample)
            * _DEBUG_CAPTURE_SECONDS
        )
        remaining = max(0, max_bytes - len(target))
        if remaining:
            target.extend(pcm[:remaining])

    def write(self, counters: dict[str, int]) -> None:
        ensure_debug_capture_dir()
        safe_name = safe_capture_name(self.call_id)
        rx_path = DEBUG_CAPTURE_DIR / f"{safe_name}_ha_ws_rtp_to_browser.wav"
        tx_path = DEBUG_CAPTURE_DIR / f"{safe_name}_ha_ws_browser_to_rtp.wav"
        meta_path = DEBUG_CAPTURE_DIR / f"{safe_name}_ha_ws_timing.json"
        self._write_wav(rx_path, self.rx_format, self.rtp_to_ws)
        self._write_wav(tx_path, self.tx_format, self.ws_to_rtp)
        meta = {
            "call_id": self.call_id,
            "rx_format": self.rx_format.wire_token(),
            "tx_format": self.tx_format.wire_token(),
            "rtp_to_browser_wav": str(rx_path),
            "browser_to_rtp_wav": str(tx_path),
            "counters": dict(counters),
            "rtp_to_ws_deltas_ms": self.rtp_to_ws_deltas_ms,
            "ws_rx_deltas_ms": self.ws_rx_deltas_ms,
            "ws_send_deltas_ms": self.ws_send_deltas_ms,
            "rtp_tx_deltas_ms": self.rtp_tx_deltas_ms,
        }
        meta_path.write_text(json.dumps(meta, indent=2, sort_keys=True), encoding="utf-8")
        prune_debug_captures()
        _LOGGER.info(
            "HA softphone audio debug capture wrote call_id=%s rtp_to_browser=%s bytes=%d "
            "browser_to_rtp=%s bytes=%d timing=%s",
            self.call_id,
            rx_path,
            len(self.rtp_to_ws),
            tx_path,
            len(self.ws_to_rtp),
            meta_path,
        )

    def _write_wav(self, path: Path, fmt: Any, payload: bytearray) -> None:
        sample_width, wav_payload = wav_pcm_payload(fmt, payload)
        with wave.open(str(path), "wb") as wav_file:
            wav_file.setnchannels(int(fmt.channels))
            wav_file.setsampwidth(int(sample_width))
            wav_file.setframerate(int(fmt.sample_rate))
            wav_file.writeframes(wav_payload)


class VoipAudioWebSocketView(HomeAssistantView):
    """Expose browser audio to the current HA softphone SIP/RTP dialog."""

    url = "/api/voip_stack/ws"
    name = "api:voip_stack:ws"
    requires_auth = True

    async def get(self, request: web.Request) -> web.WebSocketResponse:
        hass: HomeAssistant = request.app["hass"]
        device_id = str(request.query.get("device_id") or "")
        if device_id and device_id != HA_SOFTPHONE_DEVICE_ID:
            raise web.HTTPNotFound()

        session = _active_softphone_media_session(hass)
        if session is None:
            raise web.HTTPConflict(text="HA softphone has no active SIP/RTP dialog")

        token = object()
        owners = hass.data.setdefault(DOMAIN, {}).setdefault("audio_ws_owners", {})
        owner_lock = hass.data.setdefault(DOMAIN, {}).setdefault("audio_ws_owner_lock", asyncio.Lock())
        async with owner_lock:
            if session.call_id in owners:
                raise web.HTTPConflict(text="HA softphone media is already attached")
            owners[session.call_id] = token

        ws = web.WebSocketResponse()
        try:
            await ws.prepare(request)
            await _run_audio_session(hass, ws, session)
        finally:
            async with owner_lock:
                if owners.get(session.call_id) is token:
                    owners.pop(session.call_id, None)
        return ws


def async_register_audio_ws_view(hass: HomeAssistant) -> None:
    if hass.data.setdefault(DOMAIN, {}).get("audio_ws_view_registered"):
        return
    hass.http.register_view(VoipAudioWebSocketView)
    hass.data[DOMAIN]["audio_ws_view_registered"] = True
    _LOGGER.info("HA softphone browser audio websocket ready on %s", VoipAudioWebSocketView.url)


def _active_softphone_media_session(hass: HomeAssistant) -> _SoftphoneMediaSession | None:
    store = _ha_softphone_store(hass)
    call_id = str(store.get("call_id") or "").strip()
    state = str(store.get("state") or "").strip().lower()
    if state not in {"connecting", "in_call"}:
        return None
    registry = hass.data.get(DOMAIN, {}).get("call_registry")
    if not isinstance(registry, CallRegistry):
        return None
    inbound = registry.softphone_media
    if call_id and call_id in inbound:
        item = inbound[call_id]
        if item.get("rtp_loopback"):
            return _SoftphoneMediaSession(
                call_id=call_id,
                local_rtp_port=0,
                remote_rtp_host=str(item["remote_rtp_host"]),
                remote_rtp_port=int(item["remote_rtp_port"]),
                send_format=item["send_format"],
                recv_format=item["recv_format"],
                local_ssrc=int(item.get("local_ssrc") or 0),
            )
        conference_room = str(item.get("conference_room") or "")
        conference_queue = item.get("conference_queue")
        if conference_room and conference_queue is not None:
            from .conference import CONFERENCE_RTP_FORMAT

            return _SoftphoneMediaSession(
                call_id=call_id,
                local_rtp_port=0,
                remote_rtp_host="",
                remote_rtp_port=0,
                send_format=CONFERENCE_RTP_FORMAT,
                recv_format=CONFERENCE_RTP_FORMAT,
                conference_room=conference_room,
                conference_queue=conference_queue,
            )
        invite = item.get("invite")
        local_rtp_port = int(item.get("local_rtp_port") or 0)
        if invite is not None and local_rtp_port:
            return _SoftphoneMediaSession(
                call_id=invite.call_id,
                local_rtp_port=local_rtp_port,
                remote_rtp_host=invite.remote_rtp_host,
                remote_rtp_port=int(invite.remote_rtp_port),
                send_format=invite.send_format,
                recv_format=invite.recv_format,
            )

    clients: dict[str, SipCallClient] = registry.sip_clients
    if call_id and call_id in clients:
        client = clients[call_id]
        if client.dialog is not None:
            dialog = client.dialog
            return _SoftphoneMediaSession(
                call_id=dialog.call_id,
                local_rtp_port=int(dialog.local_rtp_port),
                remote_rtp_host=dialog.remote_rtp_host,
                remote_rtp_port=int(dialog.remote_rtp_port),
                send_format=dialog.send_format,
                recv_format=dialog.recv_format,
            )
    return None


async def _run_audio_session(
    hass: HomeAssistant,
    ws: web.WebSocketResponse,
    session: _SoftphoneMediaSession,
) -> None:
    if session.conference_queue is not None:
        await _run_conference_audio_session(hass, ws, session)
        return
    frame_ms = max(1, int(session.recv_format.audio_format.frame_ms))
    rx_queue_frames = max(4, min(16, (160 + frame_ms - 1) // frame_ms))
    queue: asyncio.Queue[tuple[bytes, tuple[str, int]]] = asyncio.Queue(maxsize=rx_queue_frames)
    protocol = _RtpAudioProtocol(queue)
    loop = asyncio.get_running_loop()
    try:
        transport, _ = await loop.create_datagram_endpoint(
            lambda: protocol,
            local_addr=("0.0.0.0", int(session.local_rtp_port)),
        )
    except OSError as err:
        _LOGGER.warning(
            "HA softphone audio websocket rejected call_id=%s local_rtp=%s: %s",
            session.call_id,
            session.local_rtp_port,
            err,
        )
        await ws.close(code=1013, message=b"RTP port already in use")
        return
    sequence = secrets.randbelow(0x10000)
    timestamp = secrets.randbelow(0x100000000)
    ssrc = int(session.local_ssrc) or secrets.randbelow(0x100000000)
    closed = asyncio.Event()
    counters = {
        "ws_rx": 0,
        "ws_tx": 0,
        "rtp_rx": 0,
        "rtp_tx": 0,
        "rtp_rx_bytes": 0,
        "rtp_tx_bytes": 0,
        "drop_addr": 0,
        "drop_payload_type": 0,
        "drop_error": 0,
        "drop_rx_queue": 0,
        "drop_tx_queue": 0,
        "tx_error": 0,
        "tx_silence_keepalive": 0,
    }
    logged_first_rtp = False
    latched_rtp_source: tuple[str, int] | None = None
    latched_rtp_ssrc: int | None = None
    remote_rtp_port = int(session.remote_rtp_port)
    last_counter_event = 0.0
    debug_capture = (
        _DebugAudioCapture(session.call_id, rx_format=session.recv_format, tx_format=session.send_format)
        if bool(hass.data.get(DOMAIN, {}).get(CONF_DEBUG_MODE, False))
        else None
    )

    def publish_counters(*, force: bool = False) -> None:
        nonlocal last_counter_event
        now = loop.time()
        if not force and now - last_counter_event < 0.5:
            return
        last_counter_event = now
        store = _ha_softphone_store(hass)
        if str(store.get("call_id") or "") != session.call_id:
            return
        counters["drop_rx_queue"] = protocol.dropped_packets
        update = {
            "rtp_tx_packets": counters["rtp_tx"],
            "rtp_rx_packets": counters["rtp_rx"],
            "rtp_tx_bytes": counters["rtp_tx_bytes"],
            "rtp_rx_bytes": counters["rtp_rx_bytes"],
            "last_sip_event": "rtp_media",
        }
        if bool(hass.data.get(DOMAIN, {}).get(CONF_DEBUG_MODE, False)):
            update["media_debug"] = {
                "call_id": session.call_id,
                "local_rtp_port": session.local_rtp_port,
                "remote_rtp_host": session.remote_rtp_host,
                "remote_rtp_port": remote_rtp_port,
                "tx_format": session.send_format.audio_format.wire_token(),
                "rx_format": session.recv_format.audio_format.wire_token(),
                "tx_rtp_format": session.send_format.wire_token(),
                "rx_rtp_format": session.recv_format.wire_token(),
                "expected_browser_tx_frame_bytes": session.send_format.audio_format.nominal_frame_bytes,
                "expected_browser_rx_frame_bytes": session.recv_format.audio_format.nominal_frame_bytes,
                **counters,
            }
        store.update(update)
        _fire_call_event(hass, dict(store, device_id=HA_SOFTPHONE_DEVICE_ID), "session")

    await ws.send_json(
        {
            "state": "in_call",
            "call_id": session.call_id,
            "tx_format": session.send_format.audio_format.wire_token(),
            "rx_format": session.recv_format.audio_format.wire_token(),
            "selected_tx_format": session.send_format.audio_format.wire_token(),
            "selected_rx_format": session.recv_format.audio_format.wire_token(),
            "selected_tx_rtp_format": session.send_format.wire_token(),
            "selected_rx_rtp_format": session.recv_format.wire_token(),
        }
    )
    _LOGGER.info(
        "HA softphone audio websocket attached call_id=%s local_rtp=%s remote=%s:%s tx=%s (%s) rx=%s (%s)",
        session.call_id,
        session.local_rtp_port,
        session.remote_rtp_host,
        session.remote_rtp_port,
        session.send_format.audio_format.wire_token(),
        session.send_format.wire_token(),
        session.recv_format.audio_format.wire_token(),
        session.recv_format.wire_token(),
    )
    rtp_decoder = RtpPayloadDecoder(session.recv_format)
    rtp_encoder = RtpPayloadEncoder(session.send_format)

    async def rtp_to_ws() -> None:
        nonlocal latched_rtp_source, latched_rtp_ssrc, logged_first_rtp, remote_rtp_port
        while not closed.is_set():
            data, addr = await queue.get()
            if addr[0] != session.remote_rtp_host:
                counters["drop_addr"] += 1
                continue
            try:
                packet = rtp.parse_packet(data)
                if not logged_first_rtp:
                    _LOGGER.info(
                        "HA softphone RTP RX first packet call_id=%s from=%s:%s payload_type=%s expected=%s bytes=%d",
                        session.call_id,
                        addr[0],
                        addr[1],
                        packet.payload_type,
                        session.recv_format.payload_type,
                        len(data),
                    )
                    logged_first_rtp = True
                if packet.payload_type != session.recv_format.payload_type:
                    counters["drop_payload_type"] += 1
                    continue
                if latched_rtp_ssrc is not None and packet.ssrc != latched_rtp_ssrc:
                    counters["drop_addr"] += 1
                    continue
                pcm = rtp_decoder.decode(packet.payload)
                if not pcm:
                    continue
                source = (str(addr[0]), int(addr[1]))
                if latched_rtp_source is None:
                    latched_rtp_source = source
                    latched_rtp_ssrc = packet.ssrc
                    remote_rtp_port = source[1]
                elif source[0] != latched_rtp_source[0]:
                    counters["drop_addr"] += 1
                    continue
                elif source[1] != latched_rtp_source[1]:
                    # Preserve the SSRC latch while allowing a NAT mapping to
                    # change its source port during a long-lived call.
                    latched_rtp_source = source
                    remote_rtp_port = source[1]
                counters["rtp_rx"] += 1
                counters["rtp_rx_bytes"] += len(data)
                if debug_capture is not None:
                    debug_capture.note_rtp_rx(loop.time(), pcm)
                await ws.send_bytes(encode_audio_frame(pcm))
                if debug_capture is not None:
                    debug_capture.note_ws_send(loop.time())
                counters["ws_tx"] += 1
                publish_counters()
            except Exception as err:  # noqa: BLE001 - media path must stay alive on bad packets.
                counters["drop_error"] += 1
                _LOGGER.debug("HA softphone RTP RX drop: %s", err)

    tx_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=4)
    async def ws_to_rtp() -> None:
        nonlocal sequence, timestamp
        frame_delay = max(0.001, session.send_format.audio_format.frame_ms / 1000.0)
        next_send = loop.time()
        silence_pcm = bytes(int(session.send_format.audio_format.nominal_frame_bytes))
        while not closed.is_set():
            sleep_for = next_send - loop.time()
            if sleep_for > 0:
                await asyncio.sleep(sleep_for)
                if closed.is_set():
                    break
            try:
                pcm = tx_queue.get_nowait()
                while not tx_queue.empty():
                    pcm = tx_queue.get_nowait()
                    counters["drop_tx_queue"] += 1
            except asyncio.QueueEmpty:
                pcm = silence_pcm
                counters["tx_silence_keepalive"] += 1
            try:
                payload = rtp_encoder.encode(pcm)
            except Exception as err:  # noqa: BLE001 - keep the media clock alive.
                counters["tx_error"] += 1
                _LOGGER.debug("HA softphone RTP encode drop: %s", err)
                timestamp = rtp.next_timestamp(
                    timestamp,
                    session.send_format.audio_format.nominal_frame_samples,
                )
                next_send = max(next_send + frame_delay, loop.time() + frame_delay)
                continue
            if payload:
                packet = rtp.build_packet(
                    rtp.RtpPacket(
                        payload_type=session.send_format.payload_type,
                        sequence=sequence,
                        timestamp=timestamp,
                        ssrc=ssrc,
                        payload=payload,
                    )
                )
                try:
                    transport.sendto(packet, (session.remote_rtp_host, remote_rtp_port))
                except (OSError, RuntimeError) as err:
                    counters["tx_error"] += 1
                    _LOGGER.debug("HA softphone RTP TX drop: %s", err)
                else:
                    if debug_capture is not None:
                        debug_capture.note_rtp_tx(loop.time())
                    counters["rtp_tx"] += 1
                    counters["rtp_tx_bytes"] += len(packet)
                sequence = rtp.next_sequence(sequence)
            timestamp = rtp.next_timestamp(timestamp, session.send_format.audio_format.nominal_frame_samples)
            publish_counters()
            next_send += frame_delay
            if next_send <= loop.time():
                next_send = loop.time() + frame_delay

    rx_task = asyncio.create_task(rtp_to_ws())
    tx_task = asyncio.create_task(ws_to_rtp())
    try:
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                try:
                    counters["ws_rx"] += 1
                    pcm = decode_audio_frame(bytes(msg.data))
                    expected = int(session.send_format.audio_format.nominal_frame_bytes)
                    if len(pcm) != expected:
                        raise ValueError(f"browser PCM frame has {len(pcm)} bytes, expected {expected}")
                    if debug_capture is not None:
                        debug_capture.note_ws_rx(loop.time(), pcm)
                    if tx_queue.full():
                        tx_queue.get_nowait()
                        counters["drop_tx_queue"] += 1
                    tx_queue.put_nowait(pcm)
                except Exception as err:  # noqa: BLE001 - report malformed browser frames without killing HA.
                    counters["tx_error"] += 1
                    _LOGGER.debug("HA softphone browser audio TX drop: %s", err)
            elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR):
                break
    finally:
        closed.set()
        rx_task.cancel()
        try:
            await rx_task
        except asyncio.CancelledError:
            pass
        tx_task.cancel()
        try:
            await tx_task
        except asyncio.CancelledError:
            pass
        transport.close()
        publish_counters(force=True)
        _LOGGER.info(
            "HA softphone audio websocket detached call_id=%s ws_rx=%d rtp_tx=%d rtp_rx=%d ws_tx=%d "
            "drop_addr=%d drop_pt=%d drop_error=%d drop_rx_queue=%d drop_tx_queue=%d tx_error=%d",
            session.call_id,
            counters["ws_rx"],
            counters["rtp_tx"],
            counters["rtp_rx"],
            counters["ws_tx"],
            counters["drop_addr"],
            counters["drop_payload_type"],
            counters["drop_error"],
            protocol.dropped_packets,
            counters["drop_tx_queue"],
            counters["tx_error"],
        )
        if debug_capture is not None:
            await hass.async_add_executor_job(debug_capture.write, counters)


async def _run_conference_audio_session(
    hass: HomeAssistant,
    ws: web.WebSocketResponse,
    session: _SoftphoneMediaSession,
) -> None:
    conference_queue = session.conference_queue
    if conference_queue is None:
        _LOGGER.error("HA softphone conference session has no media queue call_id=%s", session.call_id)
        await ws.close(code=1011, message=b"Conference media queue unavailable")
        return
    closed = asyncio.Event()
    counters = {
        "ws_rx": 0,
        "ws_tx": 0,
        "rtp_rx": 0,
        "rtp_tx": 0,
        "rtp_rx_bytes": 0,
        "rtp_tx_bytes": 0,
        "drop_addr": 0,
        "drop_payload_type": 0,
        "drop_error": 0,
        "drop_tx_queue": 0,
        "tx_error": 0,
        "tx_silence_keepalive": 0,
    }
    await ws.send_json(
        {
            "state": "in_call",
            "call_id": session.call_id,
            "tx_format": session.send_format.audio_format.wire_token(),
            "rx_format": session.recv_format.audio_format.wire_token(),
            "selected_tx_format": session.send_format.audio_format.wire_token(),
            "selected_rx_format": session.recv_format.audio_format.wire_token(),
            "selected_tx_rtp_format": session.send_format.wire_token(),
            "selected_rx_rtp_format": session.recv_format.wire_token(),
        }
    )
    _LOGGER.info("HA softphone conference websocket attached call_id=%s room=%s", session.call_id, session.conference_room)

    async def room_to_ws() -> None:
        while not closed.is_set():
            pcm = await conference_queue.get()
            await ws.send_bytes(encode_audio_frame(pcm))
            counters["ws_tx"] += 1

    rx_task = asyncio.create_task(room_to_ws())
    try:
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                try:
                    pcm = decode_audio_frame(bytes(msg.data))
                    manager = hass.data.setdefault(DOMAIN, {}).get("conference_manager")
                    if manager is not None:
                        manager.push_ha_audio(session.conference_room, pcm)
                    counters["ws_rx"] += 1
                except Exception as err:  # noqa: BLE001 - keep media path alive.
                    counters["tx_error"] += 1
                    _LOGGER.debug("HA softphone conference audio TX drop: %s", err)
            elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR):
                break
    finally:
        closed.set()
        rx_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await rx_task
        manager = hass.data.setdefault(DOMAIN, {}).get("conference_manager")
        if manager is not None:
            await manager.leave_ha_softphone(session.conference_room)
        registry = hass.data.setdefault(DOMAIN, {}).get("call_registry")
        if isinstance(registry, CallRegistry):
            media = registry.softphone_media.get(session.call_id)
            if media is not None and media.get("conference_queue") is session.conference_queue:
                registry.softphone_media.pop(session.call_id, None)
                registry.finish_and_pop(session.call_id, reason="local_hangup")
        store = _ha_softphone_store(hass)
        if str(store.get("call_id") or "") == session.call_id:
            store.update({"last_sip_event": "conference_media", **counters})
        _fire_call_event(hass, dict(store, device_id=HA_SOFTPHONE_DEVICE_ID), "session")
