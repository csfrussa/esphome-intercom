"""Browser audio WebSocket for the HA SIP softphone media leg."""

from __future__ import annotations

import asyncio
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

        client = _active_softphone_client(hass)
        if client is None or client.dialog is None:
            raise web.HTTPConflict(text="HA softphone has no active SIP/RTP dialog")

        ws = web.WebSocketResponse()
        await ws.prepare(request)
        await _run_audio_session(hass, ws, client)
        return ws


def async_register_audio_ws_view(hass: HomeAssistant) -> None:
    if hass.data.setdefault(DOMAIN, {}).get("audio_ws_view_registered"):
        return
    hass.http.register_view(IntercomAudioWebSocketView)
    hass.data[DOMAIN]["audio_ws_view_registered"] = True
    _LOGGER.info("HA softphone browser audio websocket ready on %s", IntercomAudioWebSocketView.url)


def _active_softphone_client(hass: HomeAssistant) -> SipCallClient | None:
    store = _ha_softphone_store(hass)
    call_id = str(store.get("call_id") or "").strip()
    clients: dict[str, SipCallClient] = hass.data.get(DOMAIN, {}).get("sip_clients", {})
    if call_id and call_id in clients:
        return clients[call_id]
    if len(clients) == 1:
        return next(iter(clients.values()))
    return None


async def _run_audio_session(
    hass: HomeAssistant,
    ws: web.WebSocketResponse,
    client: SipCallClient,
) -> None:
    dialog = client.dialog
    if dialog is None:
        await ws.close(message=b"no dialog")
        return

    queue: asyncio.Queue[tuple[bytes, tuple[str, int]]] = asyncio.Queue()
    loop = asyncio.get_running_loop()
    transport, _ = await loop.create_datagram_endpoint(
        lambda: _RtpAudioProtocol(queue),
        local_addr=("0.0.0.0", int(dialog.local_rtp_port)),
    )
    sequence = secrets.randbelow(0x10000)
    timestamp = secrets.randbelow(0x100000000)
    ssrc = secrets.randbelow(0x100000000)
    closed = asyncio.Event()

    await ws.send_json(
        {
            "state": "in_call",
            "call_id": dialog.call_id,
            "tx_format": dialog.send_format.audio_format.wire_token(),
            "rx_format": dialog.recv_format.audio_format.wire_token(),
            "selected_tx_format": dialog.send_format.audio_format.wire_token(),
            "selected_rx_format": dialog.recv_format.audio_format.wire_token(),
        }
    )
    _LOGGER.info(
        "HA softphone audio websocket attached call_id=%s local_rtp=%s remote=%s:%s tx=%s rx=%s",
        dialog.call_id,
        dialog.local_rtp_port,
        dialog.remote_rtp_host,
        dialog.remote_rtp_port,
        dialog.send_format.audio_format.wire_token(),
        dialog.recv_format.audio_format.wire_token(),
    )

    async def rtp_to_ws() -> None:
        while not closed.is_set():
            data, addr = await queue.get()
            if addr[0] != dialog.remote_rtp_host:
                continue
            try:
                packet = rtp.parse_packet(data)
                if packet.payload_type != dialog.recv_format.payload_type:
                    continue
                pcm = rtp_payload_to_pcm(packet.payload, dialog.recv_format.audio_format)
                await ws.send_bytes(encode_audio_frame(pcm))
            except Exception as err:  # noqa: BLE001 - media path must stay alive on bad packets.
                _LOGGER.debug("HA softphone RTP RX drop: %s", err)

    rx_task = asyncio.create_task(rtp_to_ws())
    try:
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                try:
                    pcm = decode_audio_frame(bytes(msg.data))
                    payload = pcm_to_rtp_payload(pcm, dialog.send_format.audio_format)
                    packet = rtp.build_packet(
                        rtp.RtpPacket(
                            payload_type=dialog.send_format.payload_type,
                            sequence=sequence,
                            timestamp=timestamp,
                            ssrc=ssrc,
                            payload=payload,
                        )
                    )
                    transport.sendto(packet, (dialog.remote_rtp_host, int(dialog.remote_rtp_port)))
                    sequence = rtp.next_sequence(sequence)
                    timestamp = rtp.next_timestamp(timestamp, dialog.send_format.audio_format.nominal_frame_samples)
                except Exception as err:  # noqa: BLE001 - report malformed browser frames without killing HA.
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
        _LOGGER.info("HA softphone audio websocket detached call_id=%s", dialog.call_id)
