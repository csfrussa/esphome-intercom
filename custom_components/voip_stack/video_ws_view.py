"""Experimental browser video WebSocket for the HA SIP softphone."""

from __future__ import annotations

import asyncio
import base64
import contextlib
from dataclasses import dataclass
import logging
import secrets
import socket
import struct

from aiohttp import WSMsgType, web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant, callback

from . import rtp, sdp
from .call_registry import CallRegistry
from .const import (
    CONF_DEBUG_MODE,
    CONF_VIDEO_CAMERA_SEND,
    CONF_VIDEO_TRANSCODING,
    DOMAIN,
)
from .queue_utils import put_drop_oldest
from .sip_client import SipCallClient
from .video_rtcp import build_fir, build_pli, build_receiver_compound
from .video_transcoder import FfmpegVideoTranscoder
from .video_rtp import (
    H264Depacketizer,
    H264RtpError,
    JpegDepacketizer,
    MAX_ACCESS_UNIT_BYTES,
    RtpReorderBuffer,
    VideoAccessUnit,
    Vp8Depacketizer,
    packetize_annex_b,
    packetize_vp8,
)
from .websocket_api import (
    CALL_EVENT,
    _ha_softphone_store,
    _publish_ha_softphone_state,
)


_LOGGER = logging.getLogger(__name__)
_VIDEO_ACCESS_UNIT = 1
_VIDEO_HEADER = struct.Struct("!BBI")
_VIDEO_OWNER_HANDOFF_TIMEOUT = 2.0
_VIDEO_ACCESS_UNIT_QUEUE = 12
_RTCP_REPORT_INTERVAL = 5.0
_KEYFRAME_FEEDBACK_INTERVAL = 1.0
_SYMMETRIC_RTP_KEEPALIVE_INTERVAL = 0.25
_SYMMETRIC_RTP_KEEPALIVE_ATTEMPTS = 8
_DYNAMIC_RTP_PAYLOAD_TYPES = tuple(range(127, 95, -1))


@dataclass(frozen=True, slots=True)
class _VideoMediaSession:
    call_id: str
    local_rtp_port: int
    remote_rtp_host: str
    remote_rtp_port: int
    remote_rtcp_port: int
    video_format: sdp.RtpVideoFormat
    local_direction: str
    remote_video_payload_types: tuple[int, ...] = ()
    camera_send_enabled: bool = False
    transcoding_enabled: bool = False
    debug_mode: bool = False
    local_ssrc: int = 0
    rtp_socket: socket.socket | None = None

    @property
    def requires_transcoding(self) -> bool:
        return not sdp.browser_video_receive_supported(self.video_format)

    @property
    def browser_format(self) -> sdp.RtpVideoFormat:
        if not self.requires_transcoding:
            return self.video_format
        return sdp.RtpVideoFormat(
            payload_type=103,
            encoding="VP8",
            clock_rate=90000,
            direction=self.video_format.direction,
            rtcp_feedback=("nack pli", "ccm fir"),
        )

    @property
    def can_send(self) -> bool:
        return bool(
            self.camera_send_enabled
            and not self.requires_transcoding
            and self.local_direction in {"sendonly", "sendrecv"}
            and sdp.browser_video_send_supported(self.video_format)
        )

    @property
    def can_receive(self) -> bool:
        return bool(
            self.local_direction in {"recvonly", "sendrecv"}
            and (not self.requires_transcoding or self.transcoding_enabled)
        )


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
            await _run_video_session(hass, ws, session, request.transport)
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
    config = hass.data.get(DOMAIN, {}).get("transport_config", {})
    camera_send = bool(config.get(CONF_VIDEO_CAMERA_SEND, False))
    transcode = bool(config.get(CONF_VIDEO_TRANSCODING, False))
    debug = bool(hass.data.get(DOMAIN, {}).get(CONF_DEBUG_MODE, False))

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
                remote_rtcp_port=int(
                    invite.remote_video_rtcp_port or int(invite.remote_video_rtp_port) + 1
                ),
                video_format=video_format,
                local_direction=str(
                    item.get("video_direction")
                    or store.get("video_direction")
                    or "inactive"
                ),
                remote_video_payload_types=tuple(invite.remote_video_payload_types),
                camera_send_enabled=camera_send,
                transcoding_enabled=transcode,
                debug_mode=debug,
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
        remote_rtcp_port=int(
            dialog.remote_video_rtcp_port or int(dialog.remote_video_rtp_port) + 1
        ),
        video_format=dialog.video_format,
        local_direction=str(dialog.local_video_direction or "inactive"),
        remote_video_payload_types=tuple(dialog.remote_video_payload_types),
        camera_send_enabled=camera_send,
        transcoding_enabled=transcode,
        debug_mode=debug,
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


def _sdp_parameter_sets(video_format: sdp.RtpVideoFormat) -> list[bytes]:
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
    websocket_transport: asyncio.BaseTransport | None = None,
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

    browser_format = session.browser_format
    media_queue = queue
    transcode_transport: asyncio.DatagramTransport | None = None
    transcode_protocol: _RtpVideoProtocol | None = None
    transcoder: FfmpegVideoTranscoder | None = None

    async def close_setup_resources() -> None:
        """Release media acquired before the long-lived task guard exists."""

        transport.close()
        if transcode_transport is not None:
            transcode_transport.close()
        if transcoder is not None:
            await transcoder.async_close()

    if session.requires_transcoding:
        try:
            transcode_queue: asyncio.Queue[tuple[bytes, tuple[str, int]]] = asyncio.Queue(maxsize=256)
            transcode_protocol = _RtpVideoProtocol(transcode_queue)
            transcode_transport, _ = await loop.create_datagram_endpoint(
                lambda: transcode_protocol,
                local_addr=("127.0.0.1", 0),
            )
            output_port = int(transcode_transport.get_extra_info("sockname")[1])
            transcoder = FfmpegVideoTranscoder(
                hass=hass,
                call_id=session.call_id,
                input_format=session.video_format,
                output_port=output_port,
            )
            await transcoder.async_start()
        except asyncio.CancelledError:
            await close_setup_resources()
            raise
        except Exception as err:  # noqa: BLE001 - video failure must leave audio/call control alive.
            await close_setup_resources()
            with contextlib.suppress(ConnectionError, RuntimeError):
                await ws.send_json({"error": str(err)})
            await ws.close(code=1013, message=b"video transcode unavailable")
            return
        media_queue = transcode_queue

    closed = asyncio.Event()
    sequence = secrets.randbelow(0x10000)
    ssrc = int(session.local_ssrc) or secrets.randbelow(0xFFFFFFFF) + 1
    remote_host = str(session.remote_rtp_host)
    remote_port = int(session.remote_rtp_port)
    latched_source: tuple[str, int] | None = None
    latched_ssrc: int | None = None
    registry = hass.data.get(DOMAIN, {}).get("call_registry")
    cached_parameter_sets: tuple[bytes, ...] = ()
    if isinstance(registry, CallRegistry):
        cached_parameter_sets = registry.video_parameter_sets.get(session.call_id, ())
    if browser_format.encoding == "H264":
        depacketizer = H264Depacketizer(
            [*_sdp_parameter_sets(browser_format), *cached_parameter_sets]
        )
    elif browser_format.encoding == "VP8":
        depacketizer = Vp8Depacketizer()
    elif browser_format.encoding == "JPEG":
        depacketizer = JpegDepacketizer()
    else:
        depacketizer = None
    reorder: RtpReorderBuffer[rtp.RtpPacket] = RtpReorderBuffer()
    input_reorder: RtpReorderBuffer[tuple[bytes, rtp.RtpPacket]] = RtpReorderBuffer()
    access_units: asyncio.Queue[VideoAccessUnit] = asyncio.Queue(maxsize=_VIDEO_ACCESS_UNIT_QUEUE)
    needs_key_frame = True
    highest_sequence = 0
    last_keyframe_feedback = 0.0
    rtcp_transport: asyncio.DatagramTransport | None = None
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
        "video_reordered_packets": 0,
        "video_lost_packets": 0,
        "video_duplicate_packets": 0,
        "video_keyframe_requests": 0,
        "video_symmetric_rtp_keepalives": 0,
        "video_symmetric_rtp_keepalive_payload_type": 0,
        "video_access_unit_queue_max": 0,
    }

    try:
        await ws.send_json(
            {
                "state": "in_call",
                "call_id": session.call_id,
                "codec": browser_format.browser_codec,
                "encoding": browser_format.encoding,
                "clock_rate": browser_format.clock_rate,
                "format": browser_format.wire_token(),
                "source_format": session.video_format.wire_token(),
                "direction": session.local_direction,
                "can_send": session.can_send,
                "can_receive": session.can_receive,
                "camera_send_enabled": session.camera_send_enabled,
                "transcoding_enabled": session.transcoding_enabled,
                "debug": session.debug_mode,
            }
        )
    except asyncio.CancelledError:
        await close_setup_resources()
        raise
    except (ConnectionError, RuntimeError):
        await close_setup_resources()
        return
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
        current_call_id = str(store.get("call_id") or "")
        if current_call_id:
            if current_call_id != session.call_id:
                return
        elif str(store.get("last_terminal_call_id") or "") != session.call_id:
            return
        store.update(counters)
        store["video_rtp_dropped_packets"] = protocol.dropped_packets + int(
            transcode_protocol.dropped_packets if transcode_protocol is not None else 0
        )
        if bool(hass.data.get(DOMAIN, {}).get(CONF_DEBUG_MODE, False)):
            media_debug = dict(store.get("media_debug") or {})
            media_debug["video"] = {
                "call_id": session.call_id,
                "local_rtp_port": session.local_rtp_port,
                "remote_rtp_host": remote_host,
                "remote_rtp_port": remote_port,
                "remote_rtcp_port": session.remote_rtcp_port,
                "format": session.video_format.wire_token(),
                "direction": session.local_direction,
                **counters,
                "video_rtp_dropped_packets": store["video_rtp_dropped_packets"],
            }
            store["media_debug"] = media_debug
            _publish_ha_softphone_state(hass)

    def queue_access_unit(access_unit: VideoAccessUnit) -> None:
        if access_units.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                access_units.get_nowait()
                counters["video_drop_error"] += 1
        access_units.put_nowait(access_unit)
        counters["video_access_unit_queue_max"] = max(
            counters["video_access_unit_queue_max"], access_units.qsize()
        )

    def request_key_frame(now: float) -> None:
        nonlocal last_keyframe_feedback
        if rtcp_transport is None or latched_ssrc is None or now - last_keyframe_feedback < _KEYFRAME_FEEDBACK_INTERVAL:
            return
        feedback = set(session.video_format.rtcp_feedback)
        feedback_packet = None
        if "nack pli" in feedback:
            feedback_packet = build_pli(ssrc, latched_ssrc)
        elif "ccm fir" in feedback:
            feedback_packet = build_fir(
                ssrc,
                latched_ssrc,
                counters["video_keyframe_requests"] + 1,
            )
        if feedback_packet is None:
            return
        raw = build_receiver_compound(
            ssrc,
            latched_ssrc,
            cumulative_lost=(
                input_reorder.lost if session.requires_transcoding else reorder.lost
            ),
            highest_sequence=highest_sequence,
            feedback=feedback_packet,
        )
        rtcp_transport.sendto(raw, (remote_host, int(session.remote_rtcp_port)))
        last_keyframe_feedback = now
        counters["video_keyframe_requests"] += 1

    def consume_ordered(packet: rtp.RtpPacket, now: float) -> None:
        nonlocal needs_key_frame, highest_sequence
        if not session.requires_transcoding:
            highest_sequence = packet.sequence
        if depacketizer is None:
            counters["video_drop_error"] += 1
            return
        before = int(getattr(depacketizer, "dropped_access_units", 0))
        result = depacketizer.push(packet)
        if int(getattr(depacketizer, "dropped_access_units", 0)) > before:
            needs_key_frame = True
            request_key_frame(now)
        if result is None:
            return
        access_unit = (
            VideoAccessUnit(result.data, result.timestamp, result.key_frame, "H264")
            if browser_format.encoding == "H264"
            else result
        )
        if browser_format.encoding == "H264" and isinstance(depacketizer, H264Depacketizer):
            parameter_sets = depacketizer.parameter_sets
            if len(parameter_sets) == 2 and isinstance(registry, CallRegistry):
                registry.video_parameter_sets[session.call_id] = parameter_sets
        if needs_key_frame and not access_unit.key_frame:
            counters["video_drop_error"] += 1
            request_key_frame(now)
            return
        if access_unit.key_frame:
            needs_key_frame = False
        queue_access_unit(access_unit)
        counters["video_access_units_rx"] += 1

    def forward_ordered_to_transcoder(item: tuple[bytes, rtp.RtpPacket]) -> None:
        nonlocal highest_sequence
        if transcoder is None:
            return
        raw, packet = item
        highest_sequence = packet.sequence
        transcoder.send_rtp(raw)

    async def rtp_to_transcoder() -> None:
        nonlocal latched_source, latched_ssrc, remote_host, remote_port, needs_key_frame
        assert transcoder is not None
        local_loop = asyncio.get_running_loop()
        while not closed.is_set():
            deadline = input_reorder.next_deadline
            timeout = None if deadline is None else max(0.0, deadline - local_loop.time())
            try:
                data, addr = await asyncio.wait_for(queue.get(), timeout=timeout)
            except TimeoutError:
                lost_before = input_reorder.lost
                for ordered in input_reorder.flush(local_loop.time()):
                    forward_ordered_to_transcoder(ordered)
                if input_reorder.lost > lost_before:
                    needs_key_frame = True
                    request_key_frame(local_loop.time())
                continue
            try:
                if str(addr[0]) != session.remote_rtp_host:
                    counters["video_drop_addr"] += 1
                    continue
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
                    remote_host, remote_port = source
                    request_key_frame(local_loop.time())
                elif source[0] != latched_source[0]:
                    counters["video_drop_addr"] += 1
                    continue
                elif source[1] != latched_source[1]:
                    latched_source = source
                    remote_port = source[1]
                counters["video_rtp_rx_packets"] += 1
                counters["video_rtp_rx_bytes"] += len(data)
                lost_before = input_reorder.lost
                for ordered in input_reorder.push(
                    packet.sequence, (data, packet), local_loop.time()
                ):
                    forward_ordered_to_transcoder(ordered)
                if input_reorder.lost > lost_before:
                    needs_key_frame = True
                    request_key_frame(local_loop.time())
                counters["video_reordered_packets"] = input_reorder.reordered
                counters["video_lost_packets"] = input_reorder.lost
                counters["video_duplicate_packets"] = input_reorder.duplicates
            except (OSError, RuntimeError, ValueError) as err:
                counters["video_drop_error"] += 1
                _LOGGER.debug("HA softphone video transcode input drop: %s", err)

    async def rtp_to_access_units() -> None:
        nonlocal latched_source, latched_ssrc, remote_host, remote_port, needs_key_frame
        loop = asyncio.get_running_loop()
        while not closed.is_set():
            deadline = reorder.next_deadline
            timeout = None if deadline is None else max(0.0, deadline - loop.time())
            try:
                data, addr = await asyncio.wait_for(media_queue.get(), timeout=timeout)
            except TimeoutError:
                lost_before = reorder.lost
                for ordered in reorder.flush(loop.time()):
                    consume_ordered(ordered, loop.time())
                if reorder.lost > lost_before:
                    needs_key_frame = True
                    request_key_frame(loop.time())
                continue
            if not session.can_receive:
                continue
            try:
                packet = rtp.parse_packet(data)
                if packet.payload_type != browser_format.payload_type:
                    counters["video_drop_payload_type"] += 1
                    continue
                if session.requires_transcoding:
                    lost_before = reorder.lost
                    for ordered in reorder.push(packet.sequence, packet, loop.time()):
                        consume_ordered(ordered, loop.time())
                    if reorder.lost > lost_before:
                        needs_key_frame = True
                    continue
                if str(addr[0]) != session.remote_rtp_host:
                    counters["video_drop_addr"] += 1
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
                    request_key_frame(loop.time())
                elif source[0] != latched_source[0]:
                    counters["video_drop_addr"] += 1
                    continue
                elif source[1] != latched_source[1]:
                    latched_source = source
                    remote_port = source[1]
                counters["video_rtp_rx_packets"] += 1
                counters["video_rtp_rx_bytes"] += len(data)
                lost_before = reorder.lost
                for ordered in reorder.push(packet.sequence, packet, loop.time()):
                    consume_ordered(ordered, loop.time())
                if reorder.lost > lost_before:
                    needs_key_frame = True
                    request_key_frame(loop.time())
                counters["video_reordered_packets"] = reorder.reordered
                counters["video_lost_packets"] = reorder.lost
                counters["video_duplicate_packets"] = reorder.duplicates
                if counters["video_access_units_rx"] % 30 == 0:
                    store_counters()
            except Exception as err:  # noqa: BLE001 - a bad frame must not stop audio/call control.
                counters["video_drop_error"] += 1
                _LOGGER.debug("HA softphone video RTP RX drop: %s", err)

    async def access_units_to_ws() -> None:
        while not closed.is_set():
            access_unit = await access_units.get()
            flags = 1 if access_unit.key_frame else 0
            await ws.send_bytes(
                _VIDEO_HEADER.pack(_VIDEO_ACCESS_UNIT, flags, access_unit.timestamp)
                + access_unit.data
            )

    async def rtcp_reports() -> None:
        while not closed.is_set():
            try:
                await asyncio.wait_for(closed.wait(), timeout=_RTCP_REPORT_INTERVAL)
            except TimeoutError:
                pass
            if closed.is_set() or rtcp_transport is None or latched_ssrc is None:
                continue
            report = build_receiver_compound(
                ssrc,
                latched_ssrc,
                cumulative_lost=(input_reorder.lost if session.requires_transcoding else reorder.lost),
                highest_sequence=highest_sequence,
            )
            rtcp_transport.sendto(report, (remote_host, int(session.remote_rtcp_port)))

    try:
        rtcp_transport, _ = await loop.create_datagram_endpoint(
            asyncio.DatagramProtocol,
            local_addr=("0.0.0.0", int(session.local_rtp_port) + 1),
        )
    except (OSError, RuntimeError) as err:
        _LOGGER.debug("SIP video RTCP disabled call_id=%s: %s", session.call_id, err)

    call_ended = asyncio.Event()

    @callback
    def on_call_event(event) -> None:
        payload = event.data
        if str(payload.get("call_id") or "") != session.call_id:
            return
        if str(payload.get("state") or "").lower() not in {"connecting", "in_call"}:
            call_ended.set()

    remove_call_listener = hass.bus.async_listen(CALL_EVENT, on_call_event)
    current_store = _ha_softphone_store(hass)
    if (
        str(current_store.get("call_id") or "") != session.call_id
        or str(current_store.get("state") or "").lower() not in {"connecting", "in_call"}
    ):
        call_ended.set()

    async def close_on_call_end() -> None:
        """Wake the media owner as soon as the authoritative call ends."""

        await call_ended.wait()

    async def browser_to_rtp() -> None:
        nonlocal sequence
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                if not session.can_send:
                    continue
                data = bytes(msg.data)
                if (
                    len(data) <= _VIDEO_HEADER.size
                    or len(data) > MAX_ACCESS_UNIT_BYTES + _VIDEO_HEADER.size
                ):
                    counters["video_drop_error"] += 1
                    continue
                frame_type, _flags, timestamp = _VIDEO_HEADER.unpack_from(data)
                if frame_type != _VIDEO_ACCESS_UNIT:
                    counters["video_drop_error"] += 1
                    continue
                try:
                    if session.video_format.encoding == "H264":
                        packets = packetize_annex_b(
                            data[_VIDEO_HEADER.size :],
                            payload_type=session.video_format.payload_type,
                            sequence=sequence,
                            timestamp=timestamp,
                            ssrc=ssrc,
                        )
                    elif session.video_format.encoding == "VP8":
                        packets = packetize_vp8(
                            data[_VIDEO_HEADER.size :],
                            payload_type=session.video_format.payload_type,
                            sequence=sequence,
                            timestamp=timestamp,
                            ssrc=ssrc,
                        )
                    else:
                        raise ValueError(
                            f"browser TX does not support {session.video_format.encoding}"
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

    async def symmetric_rtp_keepalive() -> None:
        """Open/latch the RTP mapping for a video receive path.

        SIP media relays commonly use symmetric RTP when an endpoint is behind
        NAT. A recvonly answer, delayed camera permission, or a browser with
        Send Camera disabled can otherwise leave the advertised private RTP
        port unreachable. RFC 6263 section 4.6 defines a zero-payload RTP
        packet on an unnegotiated dynamic payload type for this purpose; it
        carries no video media but lets the relay learn the symmetric RTP
        source tuple.
        """

        nonlocal sequence
        if not session.can_receive:
            return
        negotiated_payload_types = {
            *session.remote_video_payload_types,
            session.video_format.payload_type,
        }
        keepalive_payload_type = next(
            (
                payload_type
                for payload_type in _DYNAMIC_RTP_PAYLOAD_TYPES
                if payload_type not in negotiated_payload_types
            ),
            None,
        )
        if keepalive_payload_type is None:
            _LOGGER.warning(
                "SIP video symmetric RTP keepalive unavailable call_id=%s: all dynamic payload types negotiated",
                session.call_id,
            )
            return
        counters["video_symmetric_rtp_keepalive_payload_type"] = keepalive_payload_type
        for _attempt in range(_SYMMETRIC_RTP_KEEPALIVE_ATTEMPTS):
            if (
                closed.is_set()
                or counters["video_rtp_rx_packets"]
                or counters["video_rtp_tx_packets"]
            ):
                return
            keepalive = rtp.build_packet(
                rtp.RtpPacket(
                    payload_type=keepalive_payload_type,
                    sequence=sequence,
                    timestamp=int(loop.time() * session.video_format.clock_rate) & 0xFFFFFFFF,
                    ssrc=ssrc,
                    payload=b"",
                )
            )
            transport.sendto(keepalive, (remote_host, remote_port))
            sequence = rtp.next_sequence(sequence)
            counters["video_symmetric_rtp_keepalives"] += 1
            await asyncio.sleep(_SYMMETRIC_RTP_KEEPALIVE_INTERVAL)

    rx_task = asyncio.create_task(rtp_to_access_units())
    ws_task = asyncio.create_task(access_units_to_ws())
    rtcp_task = asyncio.create_task(rtcp_reports())
    lifetime_task = asyncio.create_task(close_on_call_end())
    browser_task = asyncio.create_task(browser_to_rtp())
    keepalive_task = asyncio.create_task(symmetric_rtp_keepalive())
    transcode_input_task = (
        asyncio.create_task(rtp_to_transcoder()) if transcoder is not None else None
    )
    try:
        done, _pending = await asyncio.wait(
            {browser_task, lifetime_task},
            return_when=asyncio.FIRST_COMPLETED,
        )
        if lifetime_task in done:
            # Do not let a browser that vanished without a close handshake
            # retain the SIP RTP/RTCP ports after the dialog is gone.
            # This is a dedicated media WebSocket, so closing its HTTP
            # transport is the deterministic way to wake aiohttp's pending
            # receive before releasing the RTP resources.
            ws.force_close()
            if websocket_transport is not None:
                websocket_transport.close()
            browser_task.cancel()
            await asyncio.gather(browser_task, return_exceptions=True)
        if browser_task in done and not browser_task.cancelled():
            browser_task.result()
    finally:
        closed.set()
        remove_call_listener()
        tasks = [
            rx_task,
            ws_task,
            rtcp_task,
            lifetime_task,
            browser_task,
            keepalive_task,
        ]
        if transcode_input_task is not None:
            tasks.append(transcode_input_task)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        transport.close()
        if transcode_transport is not None:
            transcode_transport.close()
        if transcoder is not None:
            await transcoder.async_close()
        if rtcp_transport is not None:
            rtcp_transport.close()
        counters["video_rtp_dropped_packets"] = protocol.dropped_packets + int(
            transcode_protocol.dropped_packets if transcode_protocol is not None else 0
        )
        store_counters()
        _LOGGER.info("HA softphone video websocket detached call_id=%s counters=%s", session.call_id, counters)
