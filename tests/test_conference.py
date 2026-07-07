#!/usr/bin/env python3
"""Conference mixer contract tests."""

from __future__ import annotations

import asyncio
from array import array
import importlib.util
import socket
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PKG_NAME = "custom_components.voip_stack"
PKG_DIR = ROOT / "custom_components" / "voip_stack"


def _install_ha_fakes() -> None:
    ha = sys.modules.get("homeassistant")
    if ha is None:
        ha = types.ModuleType("homeassistant")
        sys.modules["homeassistant"] = ha
    if not hasattr(ha, "__path__"):
        ha.__path__ = []

    components = sys.modules.setdefault("homeassistant.components", types.ModuleType("homeassistant.components"))
    if not hasattr(components, "__path__"):
        components.__path__ = []
    helpers = sys.modules.setdefault("homeassistant.helpers", types.ModuleType("homeassistant.helpers"))
    if not hasattr(helpers, "__path__"):
        helpers.__path__ = []
    core = sys.modules.setdefault("homeassistant.core", types.ModuleType("homeassistant.core"))
    config_entries = sys.modules.setdefault("homeassistant.config_entries", types.ModuleType("homeassistant.config_entries"))
    device_registry = sys.modules.setdefault("homeassistant.helpers.device_registry", types.ModuleType("homeassistant.helpers.device_registry"))
    entity_registry = sys.modules.setdefault("homeassistant.helpers.entity_registry", types.ModuleType("homeassistant.helpers.entity_registry"))
    websocket_api = sys.modules.setdefault("homeassistant.components.websocket_api", types.ModuleType("homeassistant.components.websocket_api"))

    core.HomeAssistant = getattr(core, "HomeAssistant", type("HomeAssistant", (), {}))
    core.ServiceCall = getattr(core, "ServiceCall", type("ServiceCall", (), {}))
    core.callback = getattr(core, "callback", lambda fn: fn)
    config_entries.ConfigEntry = getattr(config_entries, "ConfigEntry", type("ConfigEntry", (), {}))
    device_registry.async_get = getattr(device_registry, "async_get", lambda _hass: None)
    entity_registry.async_get = getattr(entity_registry, "async_get", lambda _hass: None)
    websocket_api.async_register_command = getattr(websocket_api, "async_register_command", lambda *args, **kwargs: None)
    websocket_api.websocket_command = getattr(websocket_api, "websocket_command", lambda _schema: (lambda fn: fn))
    websocket_api.async_response = getattr(websocket_api, "async_response", lambda fn: fn)


def _load_module(name: str):
    _install_ha_fakes()
    if "custom_components" not in sys.modules:
        root_pkg = types.ModuleType("custom_components")
        root_pkg.__path__ = [str(ROOT / "custom_components")]
        sys.modules["custom_components"] = root_pkg
    if PKG_NAME not in sys.modules:
        pkg = types.ModuleType(PKG_NAME)
        pkg.__path__ = [str(PKG_DIR)]
        sys.modules[PKG_NAME] = pkg
    full_name = f"{PKG_NAME}.{name}"
    if full_name in sys.modules:
        return sys.modules[full_name]
    spec = importlib.util.spec_from_file_location(full_name, PKG_DIR / f"{name}.py")
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {full_name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module


conference = _load_module("conference")
sip_listener = _load_module("sip_listener")
sip_client = _load_module("sip_client")
sdp = _load_module("sdp")
rtp = _load_module("rtp")
voip_sip = _load_module("sip")
const = _load_module("const")


class _FakeBus:
    def __init__(self) -> None:
        self.events: list[tuple[str, dict]] = []

    def async_fire(self, event_type: str, event: dict) -> None:
        self.events.append((event_type, dict(event)))


class _FakeHass:
    def __init__(self) -> None:
        self.data: dict = {const.DOMAIN: {"transport_config": {"sip_port": 5060, "rtp_port": _free_rtp_base()}}}
        self.config = types.SimpleNamespace(location_name="HA")
        self.bus = _FakeBus()

    def async_create_task(self, coro):
        return asyncio.create_task(coro)


def _free_rtp_base() -> int:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.bind(("127.0.0.1", 0))
        port = sock.getsockname()[1]
    finally:
        sock.close()
    base = max(10000, port - 2)
    return base if base % 2 == 0 else base - 1


def _frame(value: int) -> bytes:
    return array("h", [value] * (conference.CONFERENCE_FRAME_BYTES // 2)).tobytes()


def _first_sample(frame: bytes) -> int:
    pcm = array("h")
    pcm.frombytes(frame[:2])
    return pcm[0]


class ConferenceMixerTest(unittest.TestCase):
    def test_mix_frames_is_n_minus_one(self) -> None:
        out = conference.mix_frames([_frame(1000), _frame(2000), _frame(-500)])
        self.assertEqual([_first_sample(frame) for frame in out], [750, 250, 1500])

    def test_mix_frames_keeps_headroom_for_three_participants(self) -> None:
        out = conference.mix_frames([_frame(30000), _frame(30000), _frame(30000)])
        self.assertEqual([_first_sample(frame) for frame in out], [30000, 30000, 30000])

    def test_mix_frames_clips_two_participant_sum(self) -> None:
        out = conference.mix_frames([_frame(32767), _frame(-32768)])
        self.assertEqual([_first_sample(frame) for frame in out], [-32768, 32767])

    def test_bad_length_is_silence(self) -> None:
        out = conference.mix_frames([_frame(1000), b""])
        self.assertEqual([_first_sample(frame) for frame in out], [0, 1000])


class ConferenceRuntimeTest(unittest.IsolatedAsyncioTestCase):
    async def test_remote_rtp_reaches_ha_softphone_participant(self) -> None:
        hass = _FakeHass()
        manager = conference.ConferenceManager(hass, local_ip="127.0.0.1")
        fmt = sdp.RtpPcmFormat(96, "L16", 16000, 1, 20)
        invite = sip_listener.SipInvite(
            source_host="127.0.0.1",
            source_port=5060,
            request_uri=voip_sip.parse_sip_uri("sip:Conference@127.0.0.1"),
            caller_uri=voip_sip.parse_sip_uri("sip:Kitchen@127.0.0.1"),
            target="Conference",
            caller="Kitchen",
            call_id="call-1",
            cseq="1 INVITE",
            remote_sdp=b"",
            send_format=fmt,
            recv_format=fmt,
            remote_rtp_host="127.0.0.1",
            remote_rtp_port=45678,
        )
        entry = types.SimpleNamespace(name="Conference", id="Conference")
        result = await manager.join(invite, entry, ring_ha=True)
        self.assertEqual(result.status, 200)
        self.assertIn("m=audio", result.answer_sdp)

        queue = manager.join_ha_softphone("Conference")
        self.assertIsNotNone(queue)
        assert queue is not None
        room = manager.rooms["Conference"]
        payload = _frame(1200)
        encoded = sip_client.RtpPayloadEncoder(fmt).encode(payload)
        room.handle_rtp(
            "call-1",
            rtp.build_packet(rtp.RtpPacket(payload_type=96, sequence=1, timestamp=0, ssrc=1, payload=encoded)),
            ("127.0.0.1", 45678),
        )
        heard = await asyncio.wait_for(queue.get(), timeout=1.0)
        self.assertEqual(_first_sample(heard), 1200)
        store = hass.data[const.DOMAIN]["ha_softphone"]
        self.assertEqual(store["state"], "ringing")
        self.assertEqual(store["call_id"], "conference:Conference")

        await manager.leave_ha_softphone("Conference")
        await manager.leave_call("call-1", reason="remote_hangup")
        self.assertNotIn("Conference", manager.rooms)
        pool = hass.data[const.DOMAIN]["sip_rtp_port_pool"]
        self.assertFalse(pool["used"])

    async def test_conference_join_does_not_ring_ha_without_ring_flag(self) -> None:
        hass = _FakeHass()
        manager = conference.ConferenceManager(hass, local_ip="127.0.0.1")
        fmt = sdp.RtpPcmFormat(96, "L16", 16000, 1, 20)
        invite = sip_listener.SipInvite(
            source_host="127.0.0.1",
            source_port=5060,
            request_uri=voip_sip.parse_sip_uri("sip:Conference@127.0.0.1"),
            caller_uri=voip_sip.parse_sip_uri("sip:Kitchen@127.0.0.1"),
            target="Conference",
            caller="Kitchen",
            call_id="call-2",
            cseq="1 INVITE",
            remote_sdp=b"",
            send_format=fmt,
            recv_format=fmt,
            remote_rtp_host="127.0.0.1",
            remote_rtp_port=45679,
        )
        entry = types.SimpleNamespace(name="Conference", id="Conference")
        result = await manager.join(invite, entry)
        self.assertEqual(result.status, 200)
        self.assertNotIn("ha_softphone", hass.data[const.DOMAIN])
        await manager.leave_call("call-2", reason="remote_hangup")

    async def test_ha_softphone_can_start_empty_conference_and_leave_without_closing_peers(self) -> None:
        hass = _FakeHass()
        manager = conference.ConferenceManager(hass, local_ip="127.0.0.1")
        queue = manager.start_ha_softphone("Conference")
        self.assertIsNotNone(queue)
        room = manager.rooms["Conference"]
        self.assertIn("conference:Conference", room.legs)
        self.assertEqual(room.legs["conference:Conference"].role, "ha")

        fmt = sdp.RtpPcmFormat(96, "L16", 16000, 1, 20)
        invite = sip_listener.SipInvite(
            source_host="127.0.0.1",
            source_port=5060,
            request_uri=voip_sip.parse_sip_uri("sip:Conference@127.0.0.1"),
            caller_uri=voip_sip.parse_sip_uri("sip:Kitchen@127.0.0.1"),
            target="Conference",
            caller="Kitchen",
            call_id="call-3",
            cseq="1 INVITE",
            remote_sdp=b"",
            send_format=fmt,
            recv_format=fmt,
            remote_rtp_host="127.0.0.1",
            remote_rtp_port=45680,
        )
        result = await manager.join(invite, types.SimpleNamespace(name="Conference", id="Conference"))
        self.assertEqual(result.status, 200)
        self.assertIn("call-3", room.legs)

        await manager.leave_ha_softphone("Conference")
        self.assertNotIn("conference:Conference", room.legs)
        self.assertIn("call-3", room.legs)

        await manager.leave_call("call-3", reason="remote_hangup")
        self.assertNotIn("Conference", manager.rooms)

    async def test_creator_leaving_does_not_close_room_with_remaining_participants(self) -> None:
        hass = _FakeHass()
        manager = conference.ConferenceManager(hass, local_ip="127.0.0.1")
        fmt = sdp.RtpPcmFormat(96, "L16", 16000, 1, 20)
        entry = types.SimpleNamespace(name="Conference", id="Conference")

        first = sip_listener.SipInvite(
            source_host="127.0.0.1",
            source_port=5060,
            request_uri=voip_sip.parse_sip_uri("sip:Conference@127.0.0.1"),
            caller_uri=voip_sip.parse_sip_uri("sip:Kitchen@127.0.0.1"),
            target="Conference",
            caller="Kitchen",
            call_id="owner-call",
            cseq="1 INVITE",
            remote_sdp=b"",
            send_format=fmt,
            recv_format=fmt,
            remote_rtp_host="127.0.0.1",
            remote_rtp_port=45681,
        )
        second = sip_listener.SipInvite(
            source_host="127.0.0.1",
            source_port=5060,
            request_uri=voip_sip.parse_sip_uri("sip:Conference@127.0.0.1"),
            caller_uri=voip_sip.parse_sip_uri("sip:Hall@127.0.0.1"),
            target="Conference",
            caller="Hall",
            call_id="second-call",
            cseq="1 INVITE",
            remote_sdp=b"",
            send_format=fmt,
            recv_format=fmt,
            remote_rtp_host="127.0.0.1",
            remote_rtp_port=45682,
        )

        self.assertEqual((await manager.join(first, entry)).status, 200)
        self.assertEqual((await manager.join(second, entry)).status, 200)
        room = manager.rooms["Conference"]
        self.assertEqual(set(room.legs), {"owner-call", "second-call"})

        await manager.leave_call("owner-call", reason="remote_hangup")
        self.assertIn("Conference", manager.rooms)
        self.assertEqual(set(room.legs), {"second-call"})
        self.assertFalse(room._closed)

        await manager.leave_call("second-call", reason="remote_hangup")
        self.assertNotIn("Conference", manager.rooms)
        pool = hass.data[const.DOMAIN]["sip_rtp_port_pool"]
        self.assertFalse(pool["used"])


if __name__ == "__main__":
    unittest.main()
