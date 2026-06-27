"""Browser audio WebSocket for the HA SIP softphone media leg."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import secrets
from typing import Any

from aiohttp import WSMsgType, web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from . import rtp
from .audio_ws import decode_audio_frame, encode_audio_frame
from .const import DOMAIN, HA_SOFTPHONE_DEVICE_ID
from .sip_client import SipCallClient, pcm_to_rtp_payload, rtp_payload_to_pcm
from .websocket_api import _ha_softphone_store

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class _SoftphoneMediaSession:
    call_id: str
    local_rtp_port: int
    remote_rtp_host: str
    remote_rtp_port: int
    send_format: Any
    recv_format: Any


class _RtpAudioProtocol(asyncio.DatagramProtocol):
    def __init__(self, queue: asyncio.Queue[tuple[bytes, tuple[str, int]]]) -> None:
        self.queue = queue

    def datagram_received(self, data: bytes, addr) -> None:
        self.queue.put_nowait((data, addr))


class IntercomAudioWebSocketView(HomeAssistantView):
    """Expose browser audio to the current HA softphone SIP/RTP dialog."""

    url = "/api/intercom_native/ws"
    name = "api:intercom_native:ws"
    requires_auth = True

    async def get(self, request: web.Request) -> web.WebSocketResponse:
        hass: HomeAssistant = request.app["hass"]
        device_id = str(request.query.get("device_id") or "")
        if device_id and device_id != HA_SOFTPHONE_DEVICE_ID:
            raise web.HTTPNotFound()

        session = _active_softphone_media_session(hass)
        if session is None:
            raise web.HTTPConflict(text="HA softphone has no active SIP/RTP dialog")

        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await _run_audio_session(hass, ws, session)
        return ws


def async_register_audio_ws_view(hass: HomeAssistant) -> None:
    if hass.data.setdefault(DOMAIN, {}).get("audio_ws_view_registered"):
        return
    hass.http.register_view(IntercomAudioWebSocketView)
    hass.data[DOMAIN]["audio_ws_view_registered"] = True
    _LOGGER.info("HA softphone browser audio websocket ready on %s", IntercomAudioWebSocketView.url)


def _active_softphone_media_session(hass: HomeAssistant) -> _SoftphoneMediaSession | None:
    store = _ha_softphone_store(hass)
    call_id = str(store.get("call_id") or "").strip()
    inbound = hass.data.get(DOMAIN, {}).get("ha_softphone_media", {})
    if call_id and call_id in inbound:
        item = inbound[call_id]
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

    clients: dict[str, SipCallClient] = hass.data.get(DOMAIN, {}).get("sip_clients", {})
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
    if len(clients) == 1:
        client = next(iter(clients.values()))
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
    queue: asyncio.Queue[tuple[bytes, tuple[str, int]]] = asyncio.Queue()
    loop = asyncio.get_running_loop()
    try:
        transport, _ = await loop.create_datagram_endpoint(
            lambda: _RtpAudioProtocol(queue),
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
    ssrc = secrets.randbelow(0x100000000)
    closed = asyncio.Event()
    counters = {
        "ws_rx": 0,
        "ws_tx": 0,
        "rtp_rx": 0,
        "rtp_tx": 0,
        "drop_addr": 0,
        "drop_payload_type": 0,
        "drop_error": 0,
        "tx_error": 0,
    }
    logged_first_rtp = False

    await ws.send_json(
        {
            "state": "in_call",
            "call_id": session.call_id,
            "tx_format": session.send_format.audio_format.wire_token(),
            "rx_format": session.recv_format.audio_format.wire_token(),
            "selected_tx_format": session.send_format.audio_format.wire_token(),
            "selected_rx_format": session.recv_format.audio_format.wire_token(),
        }
    )
    _LOGGER.info(
        "HA softphone audio websocket attached call_id=%s local_rtp=%s remote=%s:%s tx=%s rx=%s",
        session.call_id,
        session.local_rtp_port,
        session.remote_rtp_host,
        session.remote_rtp_port,
        session.send_format.audio_format.wire_token(),
        session.recv_format.audio_format.wire_token(),
    )

    async def rtp_to_ws() -> None:
        nonlocal logged_first_rtp
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
                counters["rtp_rx"] += 1
                pcm = rtp_payload_to_pcm(packet.payload, session.recv_format.audio_format)
                await ws.send_bytes(encode_audio_frame(pcm))
                counters["ws_tx"] += 1
            except Exception as err:  # noqa: BLE001 - media path must stay alive on bad packets.
                counters["drop_error"] += 1
                _LOGGER.debug("HA softphone RTP RX drop: %s", err)

    rx_task = asyncio.create_task(rtp_to_ws())
    try:
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                try:
                    counters["ws_rx"] += 1
                    pcm = decode_audio_frame(bytes(msg.data))
                    payload = pcm_to_rtp_payload(pcm, session.send_format.audio_format)
                    packet = rtp.build_packet(
                        rtp.RtpPacket(
                            payload_type=session.send_format.payload_type,
                            sequence=sequence,
                            timestamp=timestamp,
                            ssrc=ssrc,
                            payload=payload,
                        )
                    )
                    transport.sendto(packet, (session.remote_rtp_host, int(session.remote_rtp_port)))
                    counters["rtp_tx"] += 1
                    sequence = rtp.next_sequence(sequence)
                    timestamp = rtp.next_timestamp(timestamp, session.send_format.audio_format.nominal_frame_samples)
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
        transport.close()
        _LOGGER.info(
            "HA softphone audio websocket detached call_id=%s ws_rx=%d rtp_tx=%d rtp_rx=%d ws_tx=%d "
            "drop_addr=%d drop_pt=%d drop_error=%d tx_error=%d",
            session.call_id,
            counters["ws_rx"],
            counters["rtp_tx"],
            counters["rtp_rx"],
            counters["ws_tx"],
            counters["drop_addr"],
            counters["drop_payload_type"],
            counters["drop_error"],
            counters["tx_error"],
        )
