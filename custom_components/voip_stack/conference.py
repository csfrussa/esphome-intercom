"""HA-anchored SIP conference rooms for VoIP Stack."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass, field
import logging
import random
import time
from typing import Any

import numpy as np

from homeassistant.core import HomeAssistant

from .audio_format import AudioFormat, PcmFormat
from .audio_pcm import PcmFrameConverter
from .const import DOMAIN, HA_SOFTPHONE_DEVICE_ID
from .fsm import CallState, TerminalReason
from .groups import GROUP_TYPE_CONFERENCE
from .media_ports import RtpPortReservation
from .rtp import RtpPacket, build_packet, next_sequence, next_timestamp, parse_packet
from .sdp import build_answer_directional
from . import sdp
from .sip_client import RtpPayloadDecoder, RtpPayloadEncoder
from .sip_listener import SipInvite, SipInviteResult
from .websocket_api import _fire_call_event, _set_ha_softphone_call_state

_LOGGER = logging.getLogger(__name__)

CONFERENCE_FORMAT = AudioFormat(16000, PcmFormat.S16LE, 1, 20)
CONFERENCE_FRAME_BYTES = CONFERENCE_FORMAT.nominal_frame_bytes
CONFERENCE_TICK_S = CONFERENCE_FORMAT.frame_ms / 1000.0
CONFERENCE_INACTIVITY_S = 10.0
MAX_CONFERENCE_LEGS = 8
CONFERENCE_RTP_FORMAT = sdp.RtpPcmFormat(96, "L16", 16000, 1, 20)
_CONFERENCE_SILENCE = b"\x00" * CONFERENCE_FRAME_BYTES


def _silence() -> bytes:
    return _CONFERENCE_SILENCE


def mix_frames(frames: list[bytes]) -> list[bytes]:
    """Return an N-1 mix for each input 16 kHz s16le mono frame."""
    if not frames:
        return []
    normalized = [frame if len(frame) == CONFERENCE_FRAME_BYTES else _silence() for frame in frames]
    decoded = np.stack([np.frombuffer(frame, dtype="<i2") for frame in normalized]).astype(np.int32)
    mixed = decoded.sum(axis=0, dtype=np.int32)[None, :] - decoded
    peaks = np.maximum(mixed.max(axis=1), -mixed.min(axis=1) - 1)
    outputs: list[bytes] = []
    for row, peak in zip(mixed, peaks, strict=True):
        if peak > 32767:
            row = (row.astype(np.int64) * 32767) // int(peak)
        outputs.append(np.clip(row, -32768, 32767).astype("<i2").tobytes())
    return outputs


@dataclass(slots=True)
class _ConferenceLeg:
    call_id: str
    caller: str
    role: str
    remote_host: str
    remote_port: int
    in_converter: PcmFrameConverter
    out_converter: PcmFrameConverter
    local_ports: tuple[int, int] = (0, 0)
    transport: asyncio.DatagramTransport | None = None
    decoder: RtpPayloadDecoder | None = None
    encoder: RtpPayloadEncoder | None = None
    local_out: asyncio.Queue[bytes] | None = None
    client: Any | None = None
    port_reservation: RtpPortReservation | None = None
    in_fifo: list[bytes] = field(default_factory=list)
    last_rx: float = field(default_factory=time.monotonic)
    sequence: int = field(default_factory=lambda: random.randrange(0, 0xFFFF))
    timestamp: int = field(default_factory=lambda: random.randrange(0, 0xFFFFFFFF))
    ssrc: int = field(default_factory=lambda: random.randrange(1, 0xFFFFFFFF))
    rx_ssrc: int | None = None
    rx_packets: int = 0
    tx_packets: int = 0
    dropped_frames: int = 0


class _ConferenceRtpProtocol(asyncio.DatagramProtocol):
    def __init__(self, room: "ConferenceRoom", leg_id: str) -> None:
        self.room = room
        self.leg_id = leg_id

    def datagram_received(self, data: bytes, addr) -> None:
        self.room.handle_rtp(self.leg_id, data, addr)


class ConferenceRoom:
    """One active conference focus. Endpoints call the room as a normal SIP URI."""

    def __init__(
        self,
        hass: HomeAssistant,
        *,
        name: str,
        local_ip: str,
    ) -> None:
        self.hass = hass
        self.name = name
        self.local_ip = local_ip
        self.legs: dict[str, _ConferenceLeg] = {}
        self._task: asyncio.Task | None = None
        self._closed = False
        self._ha_softphone_announced = False

    async def join(self, invite: SipInvite, *, ring_ha: bool = False) -> SipInviteResult:
        if len(self.legs) >= MAX_CONFERENCE_LEGS:
            return SipInviteResult(486, "Busy Here", to_tag="", decline_reason=TerminalReason.BUSY.value)
        port_reservation = RtpPortReservation.allocate(self.hass)
        local_ports = port_reservation.ports
        transport: asyncio.DatagramTransport | None = None
        leg: _ConferenceLeg | None = None
        try:
            loop = asyncio.get_running_loop()
            transport, _ = await loop.create_datagram_endpoint(
                lambda: _ConferenceRtpProtocol(self, invite.call_id),
                local_addr=("0.0.0.0", local_ports[0]),
            )
            # Capacity can change while the socket bind yields to the event
            # loop. Recheck before publishing the leg so concurrent joins
            # cannot exceed the fixed mixer bound.
            if self._closed or (invite.call_id not in self.legs and len(self.legs) >= MAX_CONFERENCE_LEGS):
                transport.close()
                transport = None
                port_reservation.release()
                return SipInviteResult(486, "Busy Here", to_tag="", decline_reason=TerminalReason.BUSY.value)
            was_empty = not self.legs
            leg = _ConferenceLeg(
                call_id=invite.call_id,
                caller=invite.caller,
                role="owner" if was_empty else "manual",
                remote_host=invite.remote_rtp_host,
                remote_port=int(invite.remote_rtp_port),
                local_ports=local_ports,
                transport=transport,
                port_reservation=port_reservation,
                decoder=RtpPayloadDecoder(invite.recv_format),
                encoder=RtpPayloadEncoder(invite.send_format),
                in_converter=PcmFrameConverter(invite.recv_format.audio_format, CONFERENCE_FORMAT),
                out_converter=PcmFrameConverter(CONFERENCE_FORMAT, invite.send_format.audio_format),
            )
            self.legs[invite.call_id] = leg
            if self._task is None or self._task.done():
                self._task = self.hass.async_create_task(self._mix_loop())
            if was_empty and ring_ha:
                self._set_softphone_ringing(invite)
            if was_empty:
                self._fire("conference_started", invite.call_id, count=1)
            self._fire("conference_participant_joined", invite.call_id, caller=invite.caller, count=len(self.legs))
            answer = build_answer_directional(
                self.local_ip,
                self.local_ip,
                local_ports[0],
                invite.send_format,
                invite.recv_format,
            )
            return SipInviteResult(200, "OK", answer_sdp=answer, to_tag="")
        except BaseException:
            if leg is not None and self.legs.get(invite.call_id) is leg:
                self.legs.pop(invite.call_id, None)
            if transport is not None:
                transport.close()
            port_reservation.release()
            raise

    async def add_client_leg(
        self,
        *,
        call_id: str,
        caller: str,
        client: Any,
        port_reservation: RtpPortReservation,
        role: str = "manual",
    ) -> bool:
        if self._closed:
            port_reservation.release()
            return False
        if call_id not in self.legs and len(self.legs) >= MAX_CONFERENCE_LEGS:
            port_reservation.release()
            return False
        dialog = getattr(client, "dialog", None)
        if dialog is None:
            port_reservation.release()
            return False
        local_ports = port_reservation.ports
        transport: asyncio.DatagramTransport | None = None
        leg: _ConferenceLeg | None = None
        try:
            loop = asyncio.get_running_loop()
            transport, _ = await loop.create_datagram_endpoint(
                lambda: _ConferenceRtpProtocol(self, call_id),
                local_addr=("0.0.0.0", int(dialog.local_rtp_port)),
            )
            if self._closed or (call_id not in self.legs and len(self.legs) >= MAX_CONFERENCE_LEGS):
                transport.close()
                transport = None
                port_reservation.release()
                return False
            was_empty = not self.legs
            leg = _ConferenceLeg(
                call_id=call_id,
                caller=caller,
                role=role,
                remote_host=dialog.remote_rtp_host,
                remote_port=int(dialog.remote_rtp_port),
                local_ports=local_ports,
                transport=transport,
                port_reservation=port_reservation,
                decoder=RtpPayloadDecoder(dialog.recv_format),
                encoder=RtpPayloadEncoder(dialog.send_format),
                in_converter=PcmFrameConverter(dialog.recv_format.audio_format, CONFERENCE_FORMAT),
                out_converter=PcmFrameConverter(CONFERENCE_FORMAT, dialog.send_format.audio_format),
                client=client,
            )
            self.legs[call_id] = leg
            if self._task is None or self._task.done():
                self._task = self.hass.async_create_task(self._mix_loop())
            if was_empty:
                self._fire("conference_started", call_id, count=1)
            self._fire("conference_participant_joined", call_id, caller=caller, count=len(self.legs))
            return True
        except BaseException:
            if leg is not None and self.legs.get(call_id) is leg:
                self.legs.pop(call_id, None)
            if transport is not None:
                transport.close()
            port_reservation.release()
            raise

    def handle_rtp(self, call_id: str, data: bytes, addr) -> None:
        leg = self.legs.get(call_id)
        if leg is None:
            return
        if leg.decoder is None:
            return
        if addr[0] != leg.remote_host:
            return
        try:
            packet = parse_packet(data)
            if packet.payload_type != leg.decoder.fmt.payload_type:
                raise ValueError(
                    f"payload type {packet.payload_type} != expected {leg.decoder.fmt.payload_type}"
                )
            if leg.rx_ssrc is not None and packet.ssrc != leg.rx_ssrc:
                raise ValueError(f"SSRC {packet.ssrc} != latched {leg.rx_ssrc}")
            pcm = leg.decoder.decode(packet.payload)
            if not pcm:
                return
            if leg.rx_ssrc is None:
                leg.rx_ssrc = packet.ssrc
                leg.remote_port = int(addr[1])
            elif int(addr[1]) != leg.remote_port:
                leg.remote_port = int(addr[1])
            frames = leg.in_converter.convert(pcm)
        except Exception as err:
            _LOGGER.debug("Conference RTP frame ignored room=%s call_id=%s: %s", self.name, call_id, err)
            return
        leg.last_rx = time.monotonic()
        leg.rx_packets += 1
        for frame in frames:
            if len(leg.in_fifo) >= 3:
                leg.in_fifo.pop(0)
                leg.dropped_frames += 1
            leg.in_fifo.append(frame)

    async def leave(self, call_id: str, reason: str = "remote_hangup") -> bool:
        leg = self.legs.pop(call_id, None)
        if leg is None:
            return False
        await self._dispose_leg(leg, reason=reason)
        self._fire("conference_participant_left", call_id, caller=leg.caller, count=len(self.legs), reason=reason)
        if not self.legs:
            await self.close(reason=reason)
        return True

    async def close(self, reason: str = "idle") -> None:
        if self._closed:
            return
        self._closed = True
        legs = list(self.legs.values())
        self.legs.clear()
        if legs:
            await asyncio.gather(*(self._dispose_leg(leg, reason=reason) for leg in legs))
        task = self._task
        self._task = None
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self._set_softphone_idle(reason)
        self._fire("conference_ended", "", count=0, reason=reason)
        manager = self.hass.data.get(DOMAIN, {}).get("conference_manager")
        if isinstance(manager, ConferenceManager) and manager.rooms.get(self.name) is self:
            manager.rooms.pop(self.name, None)

    async def _dispose_leg(self, leg: _ConferenceLeg, *, reason: str) -> None:
        if leg.transport is not None:
            leg.transport.close()
        if leg.client is not None:
            if reason != "remote_hangup":
                with contextlib.suppress(Exception):
                    await leg.client.terminate()
            with contextlib.suppress(Exception):
                await leg.client.close()
        if leg.port_reservation is not None:
            leg.port_reservation.release()

    async def _mix_loop(self) -> None:
        next_deadline = time.monotonic()
        try:
            while self.legs:
                now = time.monotonic()
                if now < next_deadline:
                    await asyncio.sleep(next_deadline - now)
                else:
                    await asyncio.sleep(0)
                    if now - next_deadline > CONFERENCE_TICK_S:
                        # Never replay missed mixer ticks as a burst. Late audio
                        # is less useful than current audio in a live room.
                        next_deadline = now
                next_deadline += CONFERENCE_TICK_S
                now = time.monotonic()
                for call_id, leg in list(self.legs.items()):
                    if now - leg.last_rx > CONFERENCE_INACTIVITY_S:
                        await self.leave(call_id, reason="media_timeout")
                if not self.legs:
                    break
                leg_items = list(self.legs.items())
                input_frames = [leg.in_fifo.pop(0) if leg.in_fifo else _silence() for _call_id, leg in leg_items]
                for (_call_id, leg), mixed in zip(leg_items, mix_frames(input_frames), strict=True):
                    for out_frame in leg.out_converter.convert(mixed):
                        if leg.local_out is not None:
                            if leg.local_out.full():
                                with contextlib.suppress(asyncio.QueueEmpty):
                                    leg.local_out.get_nowait()
                                leg.dropped_frames += 1
                            leg.local_out.put_nowait(out_frame)
                            leg.tx_packets += 1
                            continue
                        if leg.transport is None or leg.encoder is None:
                            continue
                        try:
                            payload = leg.encoder.encode(out_frame)
                            packet = RtpPacket(
                                payload_type=leg.encoder.fmt.payload_type,
                                sequence=leg.sequence,
                                timestamp=leg.timestamp,
                                ssrc=leg.ssrc,
                                payload=payload,
                            )
                            leg.transport.sendto(build_packet(packet), (leg.remote_host, leg.remote_port))
                            leg.sequence = next_sequence(leg.sequence)
                            leg.tx_packets += 1
                        except Exception as err:
                            _LOGGER.debug("Conference RTP send failed room=%s call_id=%s: %s", self.name, leg.call_id, err)
                        finally:
                            # RTP timestamps describe the media clock, not the
                            # number of successfully emitted packets. Preserve
                            # wall-clock audio time across an encode/send drop.
                            leg.timestamp = next_timestamp(
                                leg.timestamp,
                                leg.encoder.fmt.audio_format.nominal_frame_samples,
                            )
        except asyncio.CancelledError:
            raise
        finally:
            if not self._closed and not self.legs:
                await self.close(reason="idle")

    def snapshot(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "legs": len(self.legs),
            "leg_stats": {
                call_id: {
                    "caller": leg.caller,
                    "role": leg.role,
                    "rx_packets": leg.rx_packets,
                    "tx_packets": leg.tx_packets,
                    "dropped_frames": leg.dropped_frames,
                }
                for call_id, leg in self.legs.items()
            },
        }

    def add_ha_softphone_leg(self) -> asyncio.Queue[bytes] | None:
        call_id = f"conference:{self.name}"
        existing = self.legs.get(call_id)
        if existing is not None and existing.local_out is not None:
            return existing.local_out
        if len(self.legs) >= MAX_CONFERENCE_LEGS:
            return None
        was_empty = not self.legs
        queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=8)
        self.legs[call_id] = _ConferenceLeg(
            call_id=call_id,
            caller="HA",
            role="ha",
            remote_host="",
            remote_port=0,
            in_converter=PcmFrameConverter(CONFERENCE_FORMAT, CONFERENCE_FORMAT),
            out_converter=PcmFrameConverter(CONFERENCE_FORMAT, CONFERENCE_FORMAT),
            local_out=queue,
        )
        if self._task is None or self._task.done():
            self._task = self.hass.async_create_task(self._mix_loop())
        if was_empty:
            self._fire("conference_started", call_id, count=1)
        self._fire("conference_participant_joined", call_id, caller="HA", count=len(self.legs))
        self._ha_softphone_announced = True
        return queue

    async def remove_ha_softphone_leg(self) -> None:
        removed = await self.leave(f"conference:{self.name}", reason="local_hangup")
        if removed and not self._closed:
            self._set_softphone_idle("local_hangup")
            self._ha_softphone_announced = False

    def push_ha_audio(self, pcm: bytes) -> None:
        leg = self.legs.get(f"conference:{self.name}")
        if leg is None:
            return
        leg.last_rx = time.monotonic()
        leg.rx_packets += 1
        for frame in leg.in_converter.convert(pcm):
            if len(leg.in_fifo) >= 3:
                leg.in_fifo.pop(0)
                leg.dropped_frames += 1
            leg.in_fifo.append(frame)

    def _set_softphone_ringing(self, invite: SipInvite) -> None:
        self._ha_softphone_announced = True
        _set_ha_softphone_call_state(
            self.hass,
            CallState.RINGING.value,
            session_device_id=HA_SOFTPHONE_DEVICE_ID,
            caller=self.name,
            callee=invite.target,
            peer_name=self.name,
            direction="incoming",
            call_id=f"conference:{self.name}",
            route_kind=GROUP_TYPE_CONFERENCE,
            sip_status_code=180,
            last_sip_event="INVITE",
        )

    def _set_softphone_idle(self, reason: str) -> None:
        if not self._ha_softphone_announced:
            return
        _set_ha_softphone_call_state(
            self.hass,
            CallState.IDLE.value,
            session_device_id=HA_SOFTPHONE_DEVICE_ID,
            caller=self.name,
            callee=self.name,
            peer_name=self.name,
            direction="incoming",
            call_id=f"conference:{self.name}",
            reason=reason,
            route_kind=GROUP_TYPE_CONFERENCE,
            last_sip_event="BYE",
        )

    def _fire(self, event: str, call_id: str, **extra: Any) -> None:
        _fire_call_event(
            self.hass,
            {
                "event": event,
                "state": event,
                "scope": "conference",
                "room": self.name,
                "call_id": call_id,
                **extra,
            },
            "sip",
        )


class ConferenceManager:
    def __init__(self, hass: HomeAssistant, *, local_ip: str) -> None:
        self.hass = hass
        self.local_ip = local_ip
        self.rooms: dict[str, ConferenceRoom] = {}

    async def join(self, invite: SipInvite, entry: Any, *, ring_ha: bool = False) -> SipInviteResult:
        room_name = str(getattr(entry, "name", "") or getattr(entry, "id", "") or invite.target)
        room = self.rooms.get(room_name)
        if room is None or room._closed:
            room = ConferenceRoom(self.hass, name=room_name, local_ip=self.local_ip)
            self.rooms[room_name] = room
        return await room.join(invite, ring_ha=ring_ha)

    async def add_client_leg(
        self,
        room_name: str,
        *,
        call_id: str,
        caller: str,
        client: Any,
        port_reservation: RtpPortReservation,
        role: str = "manual",
    ) -> bool:
        room_key = str(room_name or "").strip()
        room = self.rooms.get(room_key)
        if room is None or room._closed:
            if role == "auto_invited":
                port_reservation.release()
                return False
            room = ConferenceRoom(self.hass, name=room_key, local_ip=self.local_ip)
            self.rooms[room_key] = room
        return await room.add_client_leg(
            call_id=call_id,
            caller=caller,
            client=client,
            port_reservation=port_reservation,
            role=role,
        )

    async def leave_call(self, call_id: str, reason: str = "remote_hangup") -> bool:
        for name, room in list(self.rooms.items()):
            if await room.leave(call_id, reason=reason):
                if room._closed:
                    self.rooms.pop(name, None)
                return True
        return False

    def join_ha_softphone(self, room_name: str) -> asyncio.Queue[bytes] | None:
        room = self.rooms.get(str(room_name or "").strip())
        if room is None or room._closed:
            return None
        return room.add_ha_softphone_leg()

    def start_ha_softphone(self, room_name: str) -> asyncio.Queue[bytes] | None:
        room_key = str(room_name or "").strip()
        room = self.rooms.get(room_key)
        if room is None or room._closed:
            room = ConferenceRoom(self.hass, name=room_key, local_ip=self.local_ip)
            self.rooms[room_key] = room
        return room.add_ha_softphone_leg()

    def push_ha_audio(self, room_name: str, pcm: bytes) -> None:
        room = self.rooms.get(str(room_name or "").strip())
        if room is not None and not room._closed:
            room.push_ha_audio(pcm)

    async def leave_ha_softphone(self, room_name: str) -> None:
        room_key = str(room_name or "").strip()
        room = self.rooms.get(room_key)
        if room is not None:
            await room.remove_ha_softphone_leg()
            if room._closed and self.rooms.get(room_key) is room:
                self.rooms.pop(room_key, None)

    async def close(self, reason: str = "local_hangup") -> None:
        rooms = list(self.rooms.values())
        self.rooms.clear()
        if rooms:
            await asyncio.gather(*(room.close(reason=reason) for room in rooms))

    def snapshot(self) -> dict[str, Any]:
        return {name: room.snapshot() for name, room in self.rooms.items()}


def conference_manager(hass: HomeAssistant, *, local_ip: str) -> ConferenceManager:
    bucket = hass.data.setdefault(DOMAIN, {})
    manager = bucket.get("conference_manager")
    if not isinstance(manager, ConferenceManager) or manager.local_ip != local_ip:
        manager = ConferenceManager(hass, local_ip=local_ip)
        bucket["conference_manager"] = manager
    return manager
