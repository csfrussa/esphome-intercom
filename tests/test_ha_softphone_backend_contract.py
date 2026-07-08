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

    def test_esphome_roster_service_registration_refreshes_phonebook(self) -> None:
        self.assertIn("EVENT_SERVICE_REGISTERED", self.source)
        body = _function_body(self.source, "_register_phonebook_service_event_sync")
        self.assertIn('"phonebook_service_event_unsub"', body)
        self.assertIn('event.data.get("domain") != "esphome"', body)
        self.assertIn('service.endswith("_set_roster_json")', body)
        self.assertIn("_refresh_and_push_phonebook(hass)", body)
        self.assertNotIn("retry", body.lower())


if __name__ == "__main__":
    unittest.main()
