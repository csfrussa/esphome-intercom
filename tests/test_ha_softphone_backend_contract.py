#!/usr/bin/env python3
"""Backend contract checks for the HA SIP softphone."""

from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
INIT = ROOT / "custom_components" / "intercom_native" / "__init__.py"


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

    def test_missing_roster_formats_do_not_force_legacy_16k_default(self) -> None:
        body = _function_body(self.source, "_roster_entry_formats")
        self.assertIn("if entry is None:", body)
        self.assertIn("return []", body)
        self.assertIn("if value in (None, \"\"):", body)
        self.assertIn("if not raw.strip():", body)


if __name__ == "__main__":
    unittest.main()
