#!/usr/bin/env python3
"""Static contract checks for the Lovelace intercom card.

These tests pin the phase-1 VoIP UI split:

* `ha_softphone` owns browser audio and HA-originated calls.
* ESP cards are pure mirrors and only press the ESP entities.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CARD = ROOT / "custom_components" / "intercom_native" / "frontend" / "intercom-card.js"


def _method_body(source: str, method_name: str) -> str:
    match = re.search(rf"\n\s+{re.escape(method_name)}\([^)]*\)\s*\{{", source)
    if not match:
        raise AssertionError(f"method {method_name} not found")
    start = match.end()
    depth = 1
    i = start
    while i < len(source) and depth:
        ch = source[i]
        if ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
        i += 1
    if depth:
        raise AssertionError(f"method {method_name} body not closed")
    return source[start : i - 1]


class FrontendCardContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = CARD.read_text()

    def test_esp_start_call_is_a_pure_button_press(self) -> None:
        body = _method_body(self.source, "async _startCall")
        esp_branch = body.split("if (this._isHaSoftphoneMode())", 1)[1]
        esp_branch = esp_branch.split("catch (err)", 1)[0]
        self.assertIn('this._callMode = "mirror"', esp_branch)
        self.assertIn('this._pressEspButton(this._callButtonEntityId, "Call")', esp_branch)
        self.assertNotIn("_startP2P", esp_branch)
        self.assertNotIn("destination === this._getHaName()", esp_branch)

    def test_esp_answer_call_is_a_pure_button_press(self) -> None:
        body = _method_body(self.source, "async _answer")
        esp_branch = body.split("if (this._isHaSoftphoneMode())", 1)[1]
        esp_branch = esp_branch.split("catch (err)", 1)[0]
        self.assertIn('this._callMode = "mirror"', esp_branch)
        self.assertIn('this._pressEspButton(this._callButtonEntityId, "Call")', esp_branch)
        self.assertNotIn("answer_esp_call", esp_branch)
        self.assertNotIn("intercom_native/answer", esp_branch)

    def test_ha_softphone_mode_is_the_only_softphone_context(self) -> None:
        body = _method_body(self.source, "_isSoftphoneContext")
        self.assertIn("this._isHaSoftphoneMode()", body)
        self.assertNotIn("this._isHaName(this._getDestination())", body)
        self.assertNotIn('this._callMode === "mirror"', body)

    def test_ha_softphone_targets_come_from_shared_roster(self) -> None:
        body = _method_body(self.source, "_softphoneTargets")
        self.assertIn("this._rosterEntries", body)
        self.assertIn("_isCallableRosterEntry", body)
        self.assertNotIn("_availableDevices", body)


if __name__ == "__main__":
    unittest.main()
