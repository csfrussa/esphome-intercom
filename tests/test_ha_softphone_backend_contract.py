#!/usr/bin/env python3
"""Backend contract checks for the HA SIP softphone."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INIT = ROOT / "custom_components" / "voip_stack" / "__init__.py"
ENDPOINT_RUNTIME = ROOT / "custom_components" / "voip_stack" / "endpoint_runtime.py"
SERVICES = ROOT / "custom_components" / "voip_stack" / "services.py"
ENDPOINT_ROUTING = ROOT / "custom_components" / "voip_stack" / "endpoint_routing.py"
SENSOR = ROOT / "custom_components" / "voip_stack" / "sensor.py"


def _function_body(source: str, function_name: str) -> str:
    match = re.search(rf"\n(?:async def|def) {re.escape(function_name)}\([^)]*\)(?: -> [^:]+)?:", source)
    if not match:
        raise AssertionError(f"function {function_name} not found")
    start = match.end()
    next_def = re.search(r"\n(?:async def|def) \w+\(", source[start:])
    end = start + next_def.start() if next_def else len(source)
    return source[start:end]


class HaSoftphoneBackendContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = INIT.read_text()

    def test_hangup_publishes_authoritative_idle_state(self) -> None:
        body = _function_body(self.source, "_handle_sip_hangup_service")
        self.assertIn("_ha_softphone_store(hass)", body)
        self.assertIn("_set_ha_softphone_call_state(", body)
        state_update = body.split("_set_ha_softphone_call_state(", 1)[1]
        self.assertIn("CallState.IDLE.value", state_update)
        self.assertIn("TerminalReason.LOCAL_HANGUP.value", state_update)
        self.assertIn("last_sip_event=", state_update)

    def test_hangup_does_not_depend_on_card_side_inference(self) -> None:
        body = _function_body(self.source, "_handle_sip_hangup_service")
        self.assertNotIn("card", body.lower())
        self.assertNotIn("frontend", body.lower())

    def test_bridge_hangup_does_not_publish_ha_softphone_session(self) -> None:
        body = _function_body(self.source, "_handle_sip_hangup_service")
        bridge_branch = body.split("if bridge_handled:", 1)[1].split("return", 1)[0]
        self.assertIn("_set_sip_bridge_call_state(", bridge_branch)
        self.assertNotIn("_set_ha_softphone_call_state(", bridge_branch)

    def test_bridge_teardown_clears_only_its_matching_softphone_session(self) -> None:
        body = _function_body(self.source, "_terminate_sip_bridge")
        self.assertIn('softphone_call_id = str(softphone.get("call_id") or "")', body)
        self.assertIn("if handled and source_call_id == softphone_call_id:", body)
        matching_branch = body.split(
            "if handled and source_call_id == softphone_call_id:", 1
        )[1]
        self.assertIn("_set_ha_softphone_call_state(", matching_branch)
        self.assertIn("CallState.IDLE.value", matching_branch)
        self.assertIn('last_sip_event="SIP_BYE"', matching_branch)

    def test_inbound_bridge_completion_does_not_mutate_ha_softphone(self) -> None:
        endpoint_runtime = ENDPOINT_RUNTIME.read_text()
        body = _function_body(endpoint_runtime, "async_start_sip_endpoint")
        bridge_path = body.split("routeable_sip_target =", 1)[1]
        bridge_path = bridge_path.split("ha_softphone_active = _ha_softphone_has_active_call", 1)[0]
        self.assertIn("_set_sip_bridge_call_state(", bridge_path)
        self.assertNotIn("_set_ha_softphone_call_state(", bridge_path)

    def test_ha_softphone_busy_excludes_bridge_runtime_maps(self) -> None:
        ws = (ROOT / "custom_components" / "voip_stack" / "websocket_api.py").read_text()
        state_body = _function_body(ws, "_ha_softphone_state")
        busy_expr = state_body.split('"busy":', 1)[1].split(",", 1)[0]
        self.assertIn("session_device_id", busy_expr)
        self.assertNotIn("pending_transactions", busy_expr)
        self.assertNotIn("active_dialogs", busy_expr)

    def test_missing_roster_formats_do_not_force_implicit_16k_default(self) -> None:
        endpoint_routing = ENDPOINT_ROUTING.read_text()
        body = _function_body(endpoint_routing, "roster_entry_formats")
        self.assertIn("if entry is None:", body)
        self.assertIn("return []", body)
        self.assertIn("if value in (None, \"\"):", body)
        self.assertIn("if not raw.strip():", body)

    def test_services_register_async_handlers_not_coroutine_returning_lambdas(self) -> None:
        services = SERVICES.read_text()
        self.assertIn("async def _handle(call: ServiceCall) -> None:", services)
        self.assertNotIn("lambda call:", services)
        self.assertNotIn("call_handler(", services)

    def test_phonebook_sensor_treats_unknown_as_unavailable(self) -> None:
        sensor = SENSOR.read_text()
        self.assertIn('UNAVAILABLE_STATES = {"", "unknown", "unavailable"}', sensor)
        self.assertIn("_state_is_available(old_state)", sensor)
        self.assertIn("_state_is_available(new_state)", sensor)

    def test_stale_call_id_cannot_replace_active_softphone_session(self) -> None:
        ws = (ROOT / "custom_components" / "voip_stack" / "websocket_api.py").read_text()
        body = _function_body(ws, "_set_ha_softphone_call_state")
        self.assertIn("next_call_id != previous_call_id", body)
        self.assertIn("previous_state", body)
        self.assertIn("Ignoring stale HA softphone", body)
        guard = body.split("next_call_id != previous_call_id", 1)[1].split("if terminal:", 1)[0]
        self.assertIn("CallState.IN_CALL.value", guard)
        self.assertIn("return", guard)

    def test_esphome_roster_service_registration_refreshes_phonebook(self) -> None:
        self.assertIn("EVENT_SERVICE_REGISTERED", self.source)
        body = _function_body(self.source, "_register_phonebook_service_event_sync")
        self.assertIn('"phonebook_service_event_unsub"', body)
        self.assertIn('event.data.get("domain") != "esphome"', body)
        self.assertIn('service.endswith("_set_roster_json")', body)
        self.assertIn("_refresh_and_push_phonebook(hass)", body)
        self.assertNotIn("retry", body.lower())

    def test_softphone_rtp_latches_source_port_and_ssrc(self) -> None:
        audio_ws = (ROOT / "custom_components" / "voip_stack" / "audio_ws_view.py").read_text()
        self.assertIn("latched_rtp_source", audio_ws)
        self.assertIn("latched_rtp_ssrc", audio_ws)
        self.assertIn("remote_rtp_port = source[1]", audio_ws)
        self.assertIn("packet.ssrc != latched_rtp_ssrc", audio_ws)
        self.assertIn("(session.remote_rtp_host, remote_rtp_port)", audio_ws)

    def test_softphone_tx_uses_one_deadline_per_frame_without_double_wait(self) -> None:
        audio_ws = (ROOT / "custom_components" / "voip_stack" / "audio_ws_view.py").read_text()
        self.assertIn("tx_queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=4)", audio_ws)
        self.assertIn("pcm = tx_queue.get_nowait()", audio_ws)
        self.assertNotIn("while not tx_queue.empty():", audio_ws)
        self.assertIn("payload = rtp_encoder.encode(pcm)", audio_ws)
        self.assertIn("tx_queue.put_nowait(pcm)", audio_ws)
        self.assertNotIn("await asyncio.wait_for(tx_queue.get(), timeout=frame_delay)", audio_ws)
        self.assertNotIn("asyncio.wait_for(closed.wait()", audio_ws)
        self.assertIn("await asyncio.sleep(sleep_for)", audio_ws)
        self.assertIn("next_send = loop.time() + frame_delay", audio_ws)

    def test_softphone_start_is_serialized_and_ring_group_claims_state_before_io(self) -> None:
        prepare = _function_body(self.source, "_async_prepare_ha_outbound_call")
        self.assertIn('setdefault("ha_softphone_start_lock", asyncio.Lock())', prepare)
        self.assertIn("async with start_lock:", prepare)

        endpoint_runtime = ENDPOINT_RUNTIME.read_text()
        start = endpoint_runtime.index("async def _start_ring_group_from_ha(")
        end = endpoint_runtime.index('\n    hass.data.setdefault(DOMAIN, {})["async_ring_conference_members"]', start)
        ring_start = endpoint_runtime[start:end]
        publish = ring_start.index("_set_ha_softphone_call_state(")
        snapshot = ring_start.index("peers = await _async_build_peer_snapshot(hass)")
        self.assertLess(publish, snapshot)
        self.assertIn("PEER_SNAPSHOT_FAILED", ring_start)

    def test_ha_originated_ring_group_exposes_browser_audio_through_existing_relay(self) -> None:
        endpoint_runtime = ENDPOINT_RUNTIME.read_text()
        audio_ws = (ROOT / "custom_components" / "voip_stack" / "audio_ws_view.py").read_text()
        bridge_manager = (ROOT / "custom_components" / "voip_stack" / "bridge_manager.py").read_text()

        ring_group = endpoint_runtime[
            endpoint_runtime.index("async def _run_ring_group_call(") : endpoint_runtime.index(
                "async def _ring_conference_members("
            )
        ]
        self.assertIn('"rtp_loopback": True', ring_group)
        self.assertIn('"remote_rtp_port": source_relay_port', ring_group)
        self.assertIn('"send_format": invite.recv_format', ring_group)
        self.assertIn('"recv_format": invite.send_format', ring_group)
        self.assertIn('item.get("rtp_loopback")', audio_ws)
        self.assertIn("local_rtp_port=0", audio_ws)
        self.assertIn("int(session.local_ssrc) or secrets.randbelow", audio_ws)
        self.assertIn("registry.softphone_media.pop(source_call_id, None)", bridge_manager)

    def test_conference_checks_softphone_busy_and_releases_browser_session(self) -> None:
        call_service = _function_body(self.source, "_handle_sip_call_target_service")
        conference_branch = call_service.split('if group_type == "conference":', 1)[1].split(
            "route_uri = route.sip_uri",
            1,
        )[0]
        self.assertIn("await _async_prepare_ha_outbound_call(hass)", conference_branch)

        audio_ws = (ROOT / "custom_components" / "voip_stack" / "audio_ws_view.py").read_text()
        conference_audio = _function_body(audio_ws, "_run_conference_audio_session")
        self.assertIn("media.get(\"conference_queue\") is session.conference_queue", conference_audio)
        self.assertIn("registry.softphone_media.pop(session.call_id, None)", conference_audio)
        self.assertIn("registry.finish_and_pop(session.call_id", conference_audio)


if __name__ == "__main__":
    unittest.main()
