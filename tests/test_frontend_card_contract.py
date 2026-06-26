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
        esp_branch = 'this._callMode = "mirror"' + esp_branch.split('this._callMode = "mirror"', 1)[1].split("catch (err)", 1)[0]
        self.assertIn('this._callMode = "mirror"', esp_branch)
        self.assertIn('this._pressEspButton(this._callButtonEntityId, "Call")', esp_branch)
        self.assertNotIn("answer_esp_call", esp_branch)
        self.assertNotIn("intercom_native/answer", esp_branch)

    def test_ha_softphone_mode_is_the_only_softphone_context(self) -> None:
        body = _method_body(self.source, "_isSoftphoneContext")
        self.assertIn("this._isHaSoftphoneMode()", body)
        self.assertNotIn("this._isHaName(this._getDestination())", body)
        self.assertNotIn('this._callMode === "mirror"', body)

    def test_card_default_mode_is_esp_mirror_not_hybrid(self) -> None:
        body = _method_body(self.source, "_isHaSoftphoneMode")
        self.assertIn('"esp_mirror"', body)
        self.assertNotIn('"hybrid"', body)

    def test_sip_events_are_rendered_through_session_mirror(self) -> None:
        call_event = _method_body(self.source, "_onCallEvent")
        self.assertIn('scope === "sip"', call_event)
        self.assertIn('scope === "sip_bridge"', call_event)
        self.assertIn("this._onSipStateEvent(event)", call_event)

        sip_event = _method_body(self.source, "_onSipStateEvent")
        self.assertIn('data.scope = "session"', sip_event)
        self.assertIn("this._onSessionStateEvent({ data })", sip_event)

    def test_ha_softphone_targets_come_from_shared_roster(self) -> None:
        body = _method_body(self.source, "_softphoneTargets")
        self.assertIn("this._rosterEntries", body)
        self.assertIn("_isCallableRosterEntry", body)
        self.assertNotIn("_availableDevices", body)

    def test_ha_softphone_actions_target_only_the_ha_softphone(self) -> None:
        answer = _method_body(self.source, "async _answer")
        ha_answer = answer.split("if (this._isHaSoftphoneMode())", 1)[1].split("return;", 1)[0]
        self.assertIn("intercomEngine.answerHaSoftphone(sessionInfo", ha_answer)
        self.assertIn("tx_format: this._sessionTxFormat", ha_answer)
        self.assertNotIn('type: "intercom_native/answer"', ha_answer)
        self.assertNotIn("intercomEngine.resumeSession(sessionInfo, HA_SOFTPHONE_DEVICE_ID", ha_answer)
        self.assertNotIn("this._sessionDeviceId()", ha_answer)

        decline = _method_body(self.source, "async _decline")
        ha_decline = decline.split("if (this._isHaSoftphoneMode())", 1)[1].split("} else {", 1)[0]
        self.assertIn('device_id: HA_SOFTPHONE_DEVICE_ID', ha_decline)
        self.assertNotIn("this._sessionDeviceId()", ha_decline)

        hangup = _method_body(self.source, "async _hangup")
        softphone_hangup = hangup.split("if (wasSoftphone)", 1)[1].split("} else {", 1)[0]
        self.assertIn("intercomEngine.stop(HA_SOFTPHONE_DEVICE_ID", softphone_hangup)
        self.assertNotIn("this._sessionDeviceId()", softphone_hangup)


if __name__ == "__main__":
    unittest.main()
