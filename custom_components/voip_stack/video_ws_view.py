"""Experimental browser H.264 WebSocket for the HA SIP softphone."""

from __future__ import annotations

import asyncio
import base64
import contextlib
from dataclasses import dataclass
import logging
import secrets
import socket
import struct
from typing import Any

from aiohttp import WSMsgType, web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant

from . import rtp, sdp
from .call_registry import CallRegistry
from .const import DOMAIN, HA_SOFTPHONE_DEVICE_ID
from .queue_utils import put_drop_oldest
from .sip_client import SipCallClient
from .video_rtp import H264Depacketizer, H264RtpError, MAX_ACCESS_UNIT_BYTES, packetize_annex_b
from .websocket_api import _ha_softphone_store


_LOGGER = logging.getLogger(__name__)
_VIDEO_ACCESS_UNIT = 1
_VIDEO_HEADER = struct.Struct("!BBI")
_VIDEO_OWNER_HANDOFF_TIMEOUT = 2.0


@dataclass(frozen=True, slots=True)
class _VideoMediaSession:
    call_id: str
    local_rtp_port: int
    remote_rtp_host: str
    remote_rtp_port: int
    video_format: sdp.RtpH264Format
    local_direction: str
    local_ssrc: int = 0
    rtp_socket: socket.socket | None = None

    @property
    def can_send(self) -> bool:
        return self.local_direction in {"sendonly", "sendrecv"}

    @property
    def can_receive(self) -> bool:
        return self.local_direction in {"recvonly", "sendrecv"}


@dataclass(slots=True)
class _VideoWsOwner:
    """One browser owner and its deterministic release notification."""

    token: object
    released: asyncio.Event


class _RtpVideoProtocol(asyncio.DatagramProtocol):
    def __init__(self, queue: asyncio.Queue[tuple[bytes, tuple[str, int]]]) -> None:
        self.queue = queue
        self.dropped_packets = 0

    def datagram_received(self, data: bytes, addr) -> None:
        if put_drop_oldest(self.queue, (data, addr)):
            self.dropped_packets += 1


class VoipVideoWebSocketView(HomeAssistantView):
    """Expose the negotiated SIP video media to one authenticated HA card."""

    url = "/api/voip_stack/video_ws"
    name = "api:voip_stack:video_ws"
    requires_auth = True

    async def get(self, request: web.Request) -> web.WebSocketResponse:
        hass: HomeAssistant = request.app["hass"]
        session = _active_video_session(hass)
        requested_call_id = str(request.query.get("call_id") or "")
        if session is None or (requested_call_id and requested_call_id != session.call_id):
            raise web.HTTPConflict(text="HA softphone has no matching video dialog")

        owner = _VideoWsOwner(token=object(), released=asyncio.Event())
        bucket = hass.data.setdefault(DOMAIN, {})
        owners = bucket.setdefault("video_ws_owners", {})
        owner_lock = bucket.setdefault("video_ws_owner_lock", asyncio.Lock())
        previous_owner = None
        async with owner_lock:
            previous_owner = owners.get(session.call_id)
        if isinstance(previous_owner, _VideoWsOwner):
            # Home Assistant can recreate the card or reload the dashboard
            # while the dialog remains active. Wait for the old WebSocket's
            # supported close path instead of racing it or polling a timer.
            try:
                await asyncio.wait_for(
                    previous_owner.released.wait(),
                    timeout=_VIDEO_OWNER_HANDOFF_TIMEOUT,
                )
            except TimeoutError as err:
                raise web.HTTPConflict(text="HA softphone video is already attached") from err
        elif previous_owner is not None:
            raise web.HTTPConflict(text="HA softphone video is already attached")
        async with owner_lock:
            if session.call_id in owners:
                raise web.HTTPConflict(text="HA softphone video is already attached")
            owners[session.call_id] = owner

        ws = web.WebSocketResponse(max_msg_size=MAX_ACCESS_UNIT_BYTES + _VIDEO_HEADER.size)
        try:
            await ws.prepare(request)
            _detach_video_socket(hass, session)
            await _run_video_session(hass, ws, session)
        finally:
            async with owner_lock:
                if owners.get(session.call_id) is owner:
                    owners.pop(session.call_id, None)
                    owner.released.set()
        return ws


def async_register_video_ws_view(hass: HomeAssistant) -> None:
    bucket = hass.data.setdefault(DOMAIN, {})
    if bucket.get("video_ws_view_registered"):
        return
    hass.http.register_view(VoipVideoWebSocketView)
    bucket["video_ws_view_registered"] = True
    _LOGGER.info("Experimental SIP video websocket ready on %s", VoipVideoWebSocketView.url)


def _active_video_session(hass: HomeAssistant) -> _VideoMediaSession | None:
    store = _ha_softphone_store(hass)
    call_id = str(store.get("call_id") or "").strip()
    if str(store.get("state") or "").lower() not in {"connecting", "in_call"} or not call_id:
        return None
    registry = hass.data.get(DOMAIN, {}).get("call_registry")
    if not isinstance(registry, CallRegistry):
        return None
    item = registry.softphone_media.get(call_id)
    if item is not None:
        invite = item.get("invite")
        video_format = getattr(invite, "video_format", None)
        local_port = int(item.get("local_video_rtp_port") or 0)
        if invite is not None and video_format is not None and local_port:
            return _VideoMediaSession(
                call_id=call_id,
                local_rtp_port=local_port,
                remote_rtp_host=str(invite.remote_video_rtp_host),
                remote_rtp_port=int(invite.remote_video_rtp_port),
                video_format=video_format,
                local_direction=sdp.local_direction_for_remote(video_format.direction),
                local_ssrc=int(item.get("video_local_ssrc") or 0),
                rtp_socket=item.get("video_rtp_socket"),
            )
    clients: dict[str, SipCallClient] = registry.sip_clients
    client = clients.get(call_id)
    dialog = client.dialog if client is not None else None
    if dialog is None or dialog.video_format is None or not dialog.local_video_rtp_port:
        return None
    return _VideoMediaSession(
        call_id=call_id,
        local_rtp_port=int(dialog.local_video_rtp_port),
        remote_rtp_host=str(dialog.remote_video_rtp_host),
        remote_rtp_port=int(dialog.remote_video_rtp_port),
        video_format=dialog.video_format,
        local_direction=str(dialog.local_video_direction or "inactive"),
        rtp_socket=client.video_rtp_socket,
    )


def _detach_video_socket(hass: HomeAssistant, session: _VideoMediaSession) -> None:
    """Transfer a pre-bound socket from call lifetime to media lifetime."""

    if session.rtp_socket is None:
        return
    registry = hass.data.get(DOMAIN, {}).get("call_registry")
    if not isinstance(registry, CallRegistry):
        return
    item = registry.softphone_media.get(session.call_id)
    if isinstance(item, dict) and item.get("video_rtp_socket") is session.rtp_socket:
        item.pop("video_rtp_socket", None)
        return
    client = registry.sip_clients.get(session.call_id)
    if client is not None and client.video_rtp_socket is session.rtp_socket:
        client.video_rtp_socket = None


def _sdp_parameter_sets(video_format: sdp.RtpH264Format) -> list[bytes]:
    out: list[bytes] = []
    for value in str(video_format.sprop_parameter_sets or "").split(","):
        value = value.strip()
        if not value:
            continue
        try:
            decoded = base64.b64decode(value, validate=True)
        except (ValueError, TypeError):
            continue
        if decoded and len(decoded) <= 65536:
            out.append(decoded)
    return out


async def _run_video_session(
    hass: HomeAssistant,
    ws: web.WebSocketResponse,
    session: _VideoMediaSession,
) -> None:
    queue: asyncio.Queue[tuple[bytes, tuple[str, int]]] = asyncio.Queue(maxsize=256)
    protocol = _RtpVideoProtocol(queue)
    loop = asyncio.get_running_loop()
    try:
        if session.rtp_socket is not None:
            transport, _ = await loop.create_datagram_endpoint(
                lambda: protocol,
                sock=session.rtp_socket,
            )
        else:
            transport, _ = await loop.create_datagram_endpoint(
                lambda: protocol,
                local_addr=("0.0.0.0", int(session.local_rtp_port)),
            )
    except OSError as err:
        if session.rtp_socket is not None:
            session.rtp_socket.close()
        _LOGGER.warning(
            "HA softphone video websocket rejected call_id=%s local_rtp=%s: %s",
            session.call_id,
            session.local_rtp_port,
            err,
        )
        await ws.close(code=1013, message=b"video RTP port already in use")
        return

    closed = asyncio.Event()
    sequence = secrets.randbelow(0x10000)
    ssrc = int(session.local_ssrc) or secrets.randbelow(0xFFFFFFFF) + 1
    remote_host = str(session.remote_rtp_host)
    remote_port = int(session.remote_rtp_port)
    latched_source: tuple[str, int] | None = None
    latched_ssrc: int | None = None
    depacketizer = H264Depacketizer(_sdp_parameter_sets(session.video_format))
    counters = {
        "video_rtp_rx_packets": 0,
        "video_rtp_tx_packets": 0,
        "video_rtp_rx_bytes": 0,
        "video_rtp_tx_bytes": 0,
        "video_access_units_rx": 0,
        "video_access_units_tx": 0,
        "video_drop_addr": 0,
        "video_drop_payload_type": 0,
        "video_drop_error": 0,
    }

    await ws.send_json(
        {
            "state": "in_call",
            "call_id": session.call_id,
            "codec": f"avc1.{session.video_format.profile_level_id.upper()}",
            "format": session.video_format.wire_token(),
            "direction": session.local_direction,
            "can_send": session.can_send,
            "can_receive": session.can_receive,
        }
    )
    _LOGGER.info(
        "HA softphone video websocket attached call_id=%s local_rtp=%s remote=%s:%s format=%s direction=%s",
        session.call_id,
        session.local_rtp_port,
        session.remote_rtp_host,
        session.remote_rtp_port,
        session.video_format.wire_token(),
        session.local_direction,
    )

    def store_counters() -> None:
        """Persist diagnostics without emitting a call lifecycle occurrence."""

        store = _ha_softphone_store(hass)
        if str(store.get("call_id") or "") != session.call_id:
            return
        store.update(counters)
        store["video_rtp_dropped_packets"] = protocol.dropped_packets

    async def rtp_to_ws() -> None:
        nonlocal latched_source, latched_ssrc, remote_host, remote_port
        while not closed.is_set():
            data, addr = await queue.get()
            if not session.can_receive:
                continue
            try:
                packet = rtp.parse_packet(data)
                if packet.payload_type != session.video_format.payload_type:
                    counters["video_drop_payload_type"] += 1
                    continue
                source = (str(addr[0]), int(addr[1]))
                if latched_ssrc is not None and packet.ssrc != latched_ssrc:
                    counters["video_drop_addr"] += 1
                    continue
                if latched_source is None:
                    latched_source = source
                    latched_ssrc = packet.ssrc
                    # RFC 3264 does not require an RTP packet's source tuple
                    # to equal the c=/m= destination advertised in SDP.  Latch
                    # the first valid negotiated payload/SSRC and send media
                    # back to that symmetric RTP tuple, which is essential for
                    # ordinary SIP phones behind NAT.
                    remote_host = source[0]
                    remote_port = source[1]
                elif source[0] != latched_source[0]:
                    counters["video_drop_addr"] += 1
                    continue
                elif source[1] != latched_source[1]:
                    latched_source = source
                    remote_port = source[1]
                counters["video_rtp_rx_packets"] += 1
                counters["video_rtp_rx_bytes"] += len(data)
                access_unit = depacketizer.push(packet)
                if access_unit is None:
                    continue
                flags = 1 if access_unit.key_frame else 0
                # ``send_bytes`` is an awaited aiohttp operation and therefore
                # supplies the supported flow-control boundary.  The bounded
                # RTP queue above absorbs short scheduling bursts and drops
                # oldest packets if the browser cannot keep up.  Do not reach
                # into aiohttp's private websocket writer/transport internals:
                # they are not a stable Home Assistant integration contract.
                await ws.send_bytes(
                    _VIDEO_HEADER.pack(_VIDEO_ACCESS_UNIT, flags, access_unit.timestamp)
                    + access_unit.data
                )
                counters["video_access_units_rx"] += 1
                if counters["video_access_units_rx"] % 30 == 0:
                    store_counters()
            except Exception as err:  # noqa: BLE001 - a bad frame must not stop audio/call control.
                counters["video_drop_error"] += 1
                _LOGGER.debug("HA softphone video RTP RX drop: %s", err)

    rx_task = asyncio.create_task(rtp_to_ws())
    try:
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                if not session.can_send:
                    continue
                data = bytes(msg.data)
                if len(data) <= _VIDEO_HEADER.size or len(data) > MAX_ACCESS_UNIT_BYTES + _VIDEO_HEADER.size:
                    counters["video_drop_error"] += 1
                    continue
                frame_type, _flags, timestamp = _VIDEO_HEADER.unpack_from(data)
                if frame_type != _VIDEO_ACCESS_UNIT:
                    counters["video_drop_error"] += 1
                    continue
                try:
                    packets = packetize_annex_b(
                        data[_VIDEO_HEADER.size :],
                        payload_type=session.video_format.payload_type,
                        sequence=sequence,
                        timestamp=timestamp,
                        ssrc=ssrc,
                    )
                    for packet in packets:
                        raw = rtp.build_packet(packet)
                        transport.sendto(raw, (remote_host, remote_port))
                        counters["video_rtp_tx_packets"] += 1
                        counters["video_rtp_tx_bytes"] += len(raw)
                    sequence = rtp.next_sequence(packets[-1].sequence)
                    counters["video_access_units_tx"] += 1
                    if counters["video_access_units_tx"] % 30 == 0:
                        store_counters()
                except (H264RtpError, OSError, RuntimeError, ValueError) as err:
                    counters["video_drop_error"] += 1
                    _LOGGER.debug("HA softphone browser video TX drop: %s", err)
            elif msg.type in {WSMsgType.CLOSE, WSMsgType.CLOSED, WSMsgType.ERROR}:
                break
    finally:
        closed.set()
        rx_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await rx_task
        transport.close()
        counters["video_rtp_dropped_packets"] = protocol.dropped_packets
        store_counters()
        _LOGGER.info("HA softphone video websocket detached call_id=%s counters=%s", session.call_id, counters)
