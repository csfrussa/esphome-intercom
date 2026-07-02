#!/usr/bin/env python3
"""Static contract checks for the Lovelace voip card.

These tests pin the phase-1 VoIP UI split:

* `ha_softphone` owns browser audio and HA-originated calls.
* ESP cards are pure mirrors and only press the ESP entities.
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
CARD = ROOT / "custom_components" / "voip_stack" / "frontend" / "voip-stack-card.js"


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
        self.assertNotIn("voip_stack/answer", esp_branch)

    def test_ha_softphone_mode_is_the_only_softphone_context(self) -> None:
        body = _method_body(self.source, "_isSoftphoneContext")
        self.assertIn("this._isHaSoftphoneMode()", body)
        self.assertNotIn("this._isConfiguredSoftphone()", body)
        self.assertNotIn("this._isHaName(this._getDestination())", body)
        self.assertNotIn('this._callMode === "mirror"', body)

    def test_card_default_mode_is_esp_mirror_not_hybrid(self) -> None:
        body = _method_body(self.source, "_isHaSoftphoneMode")
        self.assertIn('"esp_mirror"', body)
        self.assertNotIn('"hybrid"', body)

    def test_call_events_are_rendered_through_softphone_session_mirror(self) -> None:
        call_event = _method_body(self.source, "_onCallEvent")
        self.assertIn('scope === "session"', call_event)
        self.assertIn("this._onSessionStateEvent(event)", call_event)
        self.assertNotIn("_onSipStateEvent", self.source)

    def test_ha_softphone_targets_come_from_shared_roster(self) -> None:
        body = _method_body(self.source, "_softphoneTargets")
        self.assertIn("this._rosterEntries", body)
        self.assertIn("_isCallableRosterEntry", body)
        self.assertNotIn("_availableDevices", body)

    def test_ha_softphone_actions_target_only_the_ha_softphone(self) -> None:
        answer = _method_body(self.source, "async _answer")
        ha_answer = answer.split("if (this._isHaSoftphoneMode())", 1)[1].split("return;", 1)[0]
        self.assertIn('"voip_stack", "answer"', ha_answer)
        self.assertIn("call_id: this._sessionCallId()", ha_answer)
        self.assertNotIn('type: "voip_stack/answer"', ha_answer)
        self.assertNotIn("voipStackEngine.resumeSession(sessionInfo, HA_SOFTPHONE_DEVICE_ID", ha_answer)
        self.assertNotIn("this._sessionDeviceId()", ha_answer)

        decline = _method_body(self.source, "async _decline")
        ha_decline = decline.split("if (this._isHaSoftphoneMode())", 1)[1].split("} else {", 1)[0]
        self.assertIn('"voip_stack", "decline"', ha_decline)
        self.assertIn("call_id: this._sessionCallId()", ha_decline)
        self.assertNotIn("this._sessionDeviceId()", ha_decline)

        hangup = _method_body(self.source, "async _hangup")
        softphone_hangup = hangup.split("if (wasSoftphone)", 1)[1].split("} else {", 1)[0]
        self.assertIn('"voip_stack", "hangup"', softphone_hangup)
        self.assertIn("call_id: this._sessionCallId()", softphone_hangup)
        self.assertNotIn("this._sessionDeviceId()", softphone_hangup)

    def test_esp_mirror_does_not_render_sip_rtp_counters(self) -> None:
        render = _method_body(self.source, "_render")
        stats_branch = render.split("// Stats line", 1)[1].split("// Error", 1)[0]
        self.assertIn("this._isHaSoftphoneMode()", stats_branch)
        self.assertIn("voipStackEngine.statsText()", stats_branch)
        self.assertNotIn("voip_sip_snapshot", self.source)
        self.assertNotIn("rtp_tx_packets", self.source)
        self.assertNotIn("rtp_rx_packets", self.source)

    def test_ha_softphone_in_call_state_attaches_browser_audio(self) -> None:
        body = _method_body(self.source, "_onSessionStateEvent")
        self.assertIn("this._applySoftphoneSnapshot(data)", body)
        self.assertIn("this._ensureHaSoftphoneAudioPath(data)", body)

    def test_deep_link_answer_handles_ha_softphone_session_ringing(self) -> None:
        apply_snapshot = _method_body(self.source, "_applySoftphoneSnapshot")
        self.assertIn("this._maybeAnswerFromUrl()", apply_snapshot)

        maybe_answer = _method_body(self.source, "_maybeAnswerFromUrl")
        self.assertNotIn("if (this._isHaSoftphoneMode() ||", maybe_answer)
        self.assertIn("if (!this._isHaSoftphoneMode()) return", maybe_answer)
        self.assertIn("snap.direction", maybe_answer)
        self.assertIn("snap.call_id", maybe_answer)
        self.assertIn("this._tryAutoAnswer({ requirePersistentPermission: false })", maybe_answer)

    def test_deep_link_answer_is_not_part_of_esp_mirror_state_updates(self) -> None:
        setter = _method_body(self.source, "set hass")
        self.assertNotIn("this._maybeAnswerFromUrl(newEspState)", setter)

    def test_frontend_has_no_esp_call_control_ws_commands(self) -> None:
        engine = (ROOT / "custom_components" / "voip_stack" / "frontend" / "voip-stack-engine.js").read_text()
        for token in (
            "ENGINE_TRANSITIONS",
            "startP2P",
            "answerEspCall",
            "answerHaSoftphone",
            'this._setState("CALLING")',
            'this._setState("RINGING")',
            'type: "start"',
            'type: "answer"',
            'type: "stop"',
            'type: "hangup"',
            "answer_esp_call",
        ):
            self.assertNotIn(token, engine)

    def test_ha_softphone_browser_audio_survives_hidden_tabs(self) -> None:
        engine = (ROOT / "custom_components" / "voip_stack" / "frontend" / "voip-stack-engine.js").read_text()
        self.assertNotIn("hidden_timeout", engine)
        self.assertNotIn('document.addEventListener("visibilitychange"', engine)


if __name__ == "__main__":
    unittest.main()
