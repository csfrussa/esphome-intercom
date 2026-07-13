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
PHONEBOOK_CARD = ROOT / "custom_components" / "voip_stack" / "frontend" / "voip-phonebook-card.js"


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

    def test_esp_contact_call_is_a_pure_button_press(self) -> None:
        body = _method_body(self.source, "async _startCall")
        esp_branch = body.split("if (this._isHaSoftphoneMode())", 1)[1]
        esp_branch = esp_branch.split("catch (err)", 1)[0]
        self.assertIn('this._pressEspButton(this._callButtonEntityId, "Call")', esp_branch)
        self.assertIn("this._mirrorKeypadOpen", esp_branch)
        self.assertIn('this._hass.callService(domain, service, { dest: manualTarget })', esp_branch)
        self.assertNotIn("_startP2P", esp_branch)
        self.assertNotIn("destination === this._getHaName()", esp_branch)

    def test_esp_keypad_has_separate_manual_buffer_and_never_writes_destination(self) -> None:
        self.assertIn("this._mirrorManualTarget", self.source)
        self.assertIn("this._mirrorKeypadOpen", self.source)
        self.assertIn('this._mirrorManualTarget = ""', self.source)
        self.assertIn("_destinationEntityId = e.destination || null", self.source)
        self.assertNotIn('this._hass.callService("text", "set_value", { entity_id: this._destinationEntityId', self.source)
        self.assertNotIn('this._setTextEntity(this._destinationEntityId', self.source)
        toggle = _method_body(self.source, "_toggleKeypad")
        self.assertIn("!this._isHaSoftphoneMode() && !this._startCallService", toggle)
        keypress = _method_body(self.source, "_pressKeypadKey")
        self.assertNotIn("this._isHaSoftphoneMode()", keypress)

    def test_esp_manual_terminal_destination_does_not_replace_contact_cycler(self) -> None:
        render = _method_body(self.source, "_render")
        cycler = _method_body(self.source, "_contactCyclerDestination")
        self.assertIn("this._contactCyclerDestination(destination)", render)
        self.assertIn("this._isHaSoftphoneMode()", cycler)
        self.assertIn("!this._lastEndInfo", cycler)
        self.assertIn("this._lastKnownMirrorDestination = destination", cycler)
        self.assertIn("this._lastEndInfo ? this._lastKnownMirrorDestination || destination : destination", cycler)

    def test_esp_answer_call_is_a_pure_button_press(self) -> None:
        body = _method_body(self.source, "async _answer")
        esp_branch = body.split("if (this._isHaSoftphoneMode())", 1)[1]
        esp_branch = esp_branch.split("catch (err)", 1)[0]
        self.assertIn('this._pressEspButton(this._callButtonEntityId, "Call")', esp_branch)
        self.assertNotIn("answer_esp_call", esp_branch)
        self.assertNotIn("voip_stack/answer", esp_branch)

    def test_ha_softphone_mode_is_the_only_softphone_context(self) -> None:
        body = _method_body(self.source, "_isSoftphoneContext")
        self.assertIn("this._isHaSoftphoneMode()", body)
        self.assertNotIn("this._isConfiguredSoftphone()", body)
        self.assertNotIn("this._isHaName(this._getDestination())", body)
        self.assertNotIn("_callMode", self.source)

    def test_card_default_mode_is_esp_mirror_not_hybrid(self) -> None:
        body = _method_body(self.source, "_isHaSoftphoneMode")
        self.assertIn('"esp_mirror"', body)
        self.assertNotIn('"hybrid"', body)

    def test_ha_softphone_uses_its_authoritative_state_stream(self) -> None:
        call_event = _method_body(self.source, "_onCallEvent")
        self.assertIn('scope === "sip_bridge"', call_event)
        self.assertIn("this._onMirroredBridgeStateEvent(event)", call_event)
        self.assertNotIn('scope === "session"', call_event)
        softphone = _method_body(self.source, "_onSoftphoneState")
        self.assertIn("this._applySoftphoneSnapshot(state)", softphone)
        self.assertNotIn("_eventConcernsThisCard", softphone)
        self.assertNotIn("_onSipStateEvent", self.source)

    def test_esp_mirror_terminal_bridge_event_uses_dialed_target_and_reason(self) -> None:
        body = _method_body(self.source, "_onMirroredBridgeStateEvent")
        self.assertIn("this._eventConcernsThisCard(data)", body)
        self.assertIn('"busy"', body)
        self.assertIn("data.terminal_reason || data.reason || state", body)
        self.assertIn("data.target || data.dialed_target || data.peer_name || data.callee", body)
        self.assertIn('this._captureEndReason("terminal", reason, data.origin || "remote", peer)', body)

    def test_ha_softphone_terminal_label_prefers_dialed_target(self) -> None:
        apply_snapshot = _method_body(self.source, "_applySoftphoneSnapshot")
        self.assertIn(
            "snapshot.dialed_target || snapshot.peer_name || snapshot.callee",
            apply_snapshot,
        )

    def test_ha_softphone_dnd_status_outweighs_terminal_history(self) -> None:
        render = _method_body(self.source, "_render")
        idle_branch = render.split('case "idle":', 1)[1].split('case "calling":', 1)[0]
        self.assertLess(
            idle_branch.index("this._softphoneDnd"),
            idle_branch.index("this._lastEndInfo"),
        )
        self.assertIn('statusText = "Do Not Disturb"', idle_branch)
        self.assertIn("Incoming calls to Home Assistant are declined.", idle_branch)

    def test_ha_softphone_targets_come_from_shared_roster(self) -> None:
        body = _method_body(self.source, "_softphoneTargets")
        self.assertIn("this._rosterEntries", body)
        self.assertIn("this._targetFromRosterEntry(entry)", body)
        self.assertIn("metadata.local_ha", body)
        self.assertNotIn("filter", body)
        self.assertNotIn("_availableDevices", body)

    def test_ha_softphone_targets_are_the_central_roster_with_only_self_exclusion(self) -> None:
        load = _method_body(self.source, "_loadSharedRoster")
        targets = _method_body(self.source, "_softphoneTargets")
        self.assertIn("roster_json", load)
        self.assertNotIn("softphone_targets_json", self.source)
        self.assertIn("metadata.local_ha", targets)
        self.assertNotIn("entry.address || entry.sip_uri", self.source)
        self.assertNotIn("_isCallableRosterEntry", self.source)

    def test_ha_softphone_group_controls_are_dynamic_backend_state(self) -> None:
        load = _method_body(self.source, "_loadSharedRoster")
        groups = _method_body(self.source, "_availableSoftphoneGroups")
        setter = _method_body(self.source, "async _setHaSoftphoneSettings")
        dnd = _method_body(self.source, "async _toggleDnd")
        self.assertIn("roster_json", load)
        self.assertIn("metadata?.group_type", groups)
        self.assertIn('"voip_stack", "set_ha_softphone_settings"', setter)
        self.assertIn('"voip_stack", "set_dnd"', dnd)
        self.assertNotIn('"voip_stack/set_ha_softphone_settings"', self.source)
        self.assertNotIn('"voip_stack/set_ha_softphone_dnd"', self.source)
        self.assertIn("extension: this._softphoneExtension", setter)
        self.assertIn('id = "ha-softphone-extension"', self.source)
        self.assertIn('type = "text"', self.source)
        self.assertIn('setAttribute("list", "ha-softphone-ring-group-options")', self.source)
        self.assertIn('setAttribute("list", "ha-softphone-conference-group-options")', self.source)
        self.assertNotIn("_populateGroupSelect", self.source)
        self.assertNotIn("conference_manager", self.source)
        self.assertNotIn("_ringConference", self.source)

    def test_esp_mirror_settings_write_exposed_esp_entities(self) -> None:
        finder = _method_body(self.source, "async _findEntityIds")
        self.assertIn("e.auto_answer", finder)
        self.assertIn("e.dnd", finder)
        self.assertIn("e.voip_ring_groups", finder)
        self.assertIn("e.voip_conference_groups", finder)
        self.assertIn("e.voip_conference_ring", finder)
        self.assertIn("e.voip_extension", finder)
        self.assertIn("e.start_call_service", finder)
        self.assertNotIn("deviceInfo.route_id", finder)
        self.assertNotIn("`esphome.${deviceInfo.route_id}_start_call`", self.source)
        set_text = _method_body(self.source, "async _setTextEntity")
        set_switch = _method_body(self.source, "async _setSwitchEntity")
        group_setter = _method_body(self.source, "async _setGroupSetting")
        auto_answer = _method_body(self.source, "async _toggleAutoAnswer")
        self.assertIn('"text", "set_value"', set_text)
        self.assertIn('"switch", enabled ? "turn_on" : "turn_off"', set_switch)
        self.assertIn("async _setExtensionSetting", self.source)
        self.assertIn("this._extensionTextEntityId", self.source)
        self.assertIn("this._ringGroupsTextEntityId", group_setter)
        self.assertIn("this._conferenceGroupsTextEntityId", group_setter)
        self.assertIn("this._conferenceRingSwitchEntityId", group_setter)
        self.assertIn("this._autoAnswerSwitchEntityId", auto_answer)

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

    def test_hangup_preempts_pending_outbound_start(self) -> None:
        render = _method_body(self.source, "_render")
        hangup = _method_body(self.source, "async _hangup")
        start = _method_body(self.source, "async _startHaSoftphoneCall")

        self.assertIn("els.hangupBtn.disabled = this._stopping", render)
        self.assertNotIn("els.hangupBtn.disabled = buttonDisabled", render)
        self.assertIn('case "connecting":', render)
        self.assertIn("showHangup = true", render.split("if (this._starting)", 1)[1])
        self.assertIn("++this._callOperationId", hangup)
        self.assertIn("this._starting = false", hangup)
        self.assertIn("const operationId = ++this._callOperationId", start)
        self.assertIn("operationId === this._callOperationId", start)

    def test_ha_terminal_reason_is_transient_and_deduplicated(self) -> None:
        apply_snapshot = _method_body(self.source, "_applySoftphoneSnapshot")
        render = _method_body(self.source, "_render")

        self.assertIn("this._lastSoftphoneTerminalKey", apply_snapshot)
        self.assertIn("this._captureEndReason(", apply_snapshot)
        self.assertIn("this._isHaSoftphoneMode() && this._lastEndInfo", render)
        self.assertNotIn("this._softphoneSnapshot?.terminal_reason", render)

    def test_ha_softphone_rejects_older_snapshots_for_the_same_call(self) -> None:
        normalise = _method_body(self.source, "_normaliseSoftphoneSnapshot")
        apply_snapshot = _method_body(self.source, "_applySoftphoneSnapshot")

        self.assertIn("revision: Number(payload.revision || 0)", normalise)
        self.assertIn("current?.call_id === snapshot.call_id", apply_snapshot)
        self.assertIn("Number(current.revision || 0) > snapshot.revision", apply_snapshot)
        self.assertIn("return false", apply_snapshot)

    def test_phonebook_is_an_internal_main_card_mode(self) -> None:
        source = PHONEBOOK_CARD.read_text()
        self.assertIn('customElements.define("voip-stack-phonebook-view"', source)
        self.assertNotIn('customElements.define("voip-phonebook-card"', source)
        self.assertNotIn('type: "voip-phonebook-card"', source)
        self.assertIn('phonebookOpt.value = "phonebook"', self.source)
        self.assertIn('this._isPhonebookMode()', self.source)
        self.assertIn('document.createElement("voip-stack-phonebook-view")', self.source)

    def test_phonebook_card_is_scrollable_safe_and_roster_driven(self) -> None:
        source = PHONEBOOK_CARD.read_text()
        self.assertIn('overflow-y: auto', source)
        self.assertIn('attributes?.roster_json', source)
        self.assertIn('localeCompare', source)
        self.assertIn('contact.enabled !== false', source)
        self.assertIn('link.href = `tel:', source)
        self.assertIn('name.textContent = this._name(contact)', source)
        self.assertNotIn("innerHTML", source)
        self.assertIn("background: transparent", source)
        self.assertNotIn("code-editor-background-color", source)

    def test_main_voip_module_loads_phonebook_card_with_same_cache_version(self) -> None:
        self.assertIn(
            'import(`./voip-phonebook-card.js?v=${encodeURIComponent(VOIP_STACK_MODULE_VERSION)}`)',
            self.source,
        )

    def test_phone_cards_support_native_sections_resizing(self) -> None:
        grid = _method_body(self.source, "getGridOptions")
        self.assertIn("columns: 12", grid)
        self.assertIn("rows: 7", grid)
        self.assertIn("min_columns: 6", grid)
        self.assertIn("min_rows: 4", grid)
        self.assertIn("min_columns: 4", grid)
        self.assertIn("min_rows: 3", grid)
        self.assertEqual(grid.count("max_rows: 8"), 2)
        self.assertIn("new ResizeObserver(() => this._measureLayout())", self.source)
        self.assertIn("const width = card.clientWidth", self.source)
        self.assertIn("const height = card.clientHeight", self.source)
        self.assertIn('--voip-button-size', self.source)
        self.assertGreaterEqual(self.source.count('document.createElement("ha-card")'), 2)
        self.assertNotIn('const card = document.createElement("div")', self.source)
        self.assertIn("overflow-y: auto", self.source)
        self.assertIn("height: 100%", self.source)

    def test_phone_card_masonry_size_matches_default_sections_height(self) -> None:
        size = _method_body(self.source, "getCardSize")
        self.assertIn("return 7", size)

    def test_esp_mirror_does_not_render_sip_rtp_counters(self) -> None:
        render = _method_body(self.source, "_render")
        stats_branch = render.split("// Stats line", 1)[1].split("// Error", 1)[0]
        self.assertIn("this._isHaSoftphoneMode()", stats_branch)
        self.assertIn("voipStackEngine.statsText()", stats_branch)
        self.assertNotIn("voip_sip_snapshot", self.source)
        self.assertNotIn("rtp_tx_packets", self.source)
        self.assertNotIn("rtp_rx_packets", self.source)

    def test_ha_softphone_in_call_state_attaches_browser_audio(self) -> None:
        body = _method_body(self.source, "_onSoftphoneState")
        self.assertIn("this._applySoftphoneSnapshot(state)", body)
        self.assertIn("this._ensureHaSoftphoneAudioPath(state)", body)

    def test_terminal_ha_softphone_event_always_closes_engine(self) -> None:
        cleanup = _method_body(self.source, "_cleanupAfterTerminalSession")
        self.assertIn("voipStackEngine.active", cleanup)
        self.assertIn('voipStackEngine.close("terminal")', cleanup)
        self.assertNotIn("this._hasBrowserAudioPath()", cleanup)

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

    def test_reconfigure_discards_stale_device_entity_bindings(self) -> None:
        config = _method_body(self.source, "setConfig")
        reset = _method_body(self.source, "_resetDeviceBindings")
        finder = _method_body(self.source, "async _findEntityIds")
        resolver = _method_body(self.source, "async _getDeviceInfo")

        self.assertIn("oldSelector !== newSelector || oldMode !== newMode", config)
        self.assertIn("this._resetDeviceBindings()", config)
        self.assertIn('this._startCallService = ""', reset)
        self.assertIn('"_voipStateEntityId"', reset)
        self.assertIn("expectedSelector !== this._getConfigSelector()", finder)
        self.assertIn("expectedSelector !== this._getConfigSelector()", resolver)
        self.assertGreaterEqual(
            finder.count("expectedSelector !== this._getConfigSelector()"),
            2,
        )

    def test_device_discovery_is_single_flight_with_bounded_startup_retry(self) -> None:
        finder = _method_body(self.source, "async _findEntityIds")
        scheduler = _method_body(self.source, "_scheduleDeviceBindingsLoad")
        resolver = _method_body(self.source, "async _getDeviceInfo")
        disconnect = _method_body(self.source, "disconnectedCallback")

        self.assertIn("this._deviceBindingsLoading || this._deviceBindingsRetryTimer", finder)
        self.assertIn("this._deviceBindingsLoading = true", finder)
        self.assertIn("this._deviceBindingsLoading = false", finder)
        self.assertIn("this._scheduleDeviceBindingsLoad()", finder)
        self.assertIn("this._deviceBindingsRetryTimer = setTimeout", scheduler)
        self.assertIn("this._isUnknownCommandError(err)", resolver)
        self.assertIn("clearTimeout(this._deviceBindingsRetryTimer)", disconnect)
        softphone_state = _method_body(self.source, "async _loadSoftphoneState")
        self.assertIn("const connection = this._hass.connection", softphone_state)
        self.assertIn("!this._isHaSoftphoneMode()", softphone_state)
        self.assertIn("this._hass?.connection !== connection", softphone_state)

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

    def test_browser_audio_websocket_is_bounded_and_stale_close_is_isolated(self) -> None:
        engine = (ROOT / "custom_components" / "voip_stack" / "frontend" / "voip-stack-engine.js").read_text()
        playback = (
            ROOT
            / "custom_components"
            / "voip_stack"
            / "frontend"
            / "voip-stack-playback-processor.js"
        ).read_text()
        capture = (
            ROOT
            / "custom_components"
            / "voip_stack"
            / "frontend"
            / "voip-stack-processor.js"
        ).read_text()

        self.assertIn("this._ws.bufferedAmount >= maxBufferedBytes", engine)
        self.assertIn("this._stats.tx_dropped++", engine)
        self.assertIn("if (this._ws !== ws) return", engine)
        self.assertIn("if (this._connectPromise === connectPromise)", engine)
        self.assertIn("this._deviceId !== deviceId || this._callId !== wantedCallId", engine)
        self.assertIn("Audio WebSocket superseded before connect", engine)
        self.assertIn('await this._connect(deviceId, reply?.call_id || "")', engine)
        self.assertNotIn("raw.slice(1)", engine)
        self.assertIn("byteOffset: 1", engine)
        self.assertIn("new DataView(buffer, byteOffset, frameBytes)", playback)
        self.assertIn("this._dropFrames = this._maxStartFrames + 1", playback)
        self.assertIn("if (underrunThisQuantum) this._started = false", playback)
        self.assertIn('pcmFormat === "s24le_in_s32") return view.getInt32(offset, true) / 8388608', playback)
        self.assertIn("s * 0x800000 : s * 0x7fffff", capture)
        self.assertNotIn("0x7fffff00", capture)
        self.assertIn("await this.resumeSession(mediaInfo, HA_SOFTPHONE_DEVICE_ID, reply)", engine)
        self.assertIn("const previousAttach = this._sessionAttachPromise", engine)
        self.assertIn("if (previousAttach) await previousAttach.catch", engine)
        self.assertIn("if (this._sessionAttachKey !== attachKey) return", engine)
        self.assertIn('await this.close("superseded", true)', engine)
        setup = _method_body(engine, "async _setupAudioOrAbort")
        self.assertIn("await this._setupAudio(deviceInfo, reply)", setup)
        after_setup = setup.split("await this._setupAudio(deviceInfo, reply)", 1)[1]
        self.assertIn("this._sessionAttachKey !== attachKey", after_setup)
        self.assertIn('await this.close("superseded", true)', after_setup)
        self.assertIn('await this.close("switch", true)', engine)
        self.assertIn('if (!preserveAttach) this._sessionAttachKey = ""', engine)
        self.assertIn("if (this._sessionAttachPromise !== trackedPromise) return", engine)

    def test_dynamic_call_controls_expose_accessible_state(self) -> None:
        source = CARD.read_text()
        self.assertIn('statusRow.setAttribute("aria-live", "polite")', source)
        self.assertIn('err.setAttribute("role", "alert")', source)
        self.assertIn('prevBtn.setAttribute("aria-label", "Previous destination")', source)
        self.assertIn('nextBtn.setAttribute("aria-label", "Next destination")', source)
        self.assertIn('els.keypadBtn.setAttribute("aria-expanded"', source)
        self.assertIn('els.settingsBtn.setAttribute("aria-expanded"', source)

    def test_ring_group_mirror_replaces_group_with_answering_endpoint(self) -> None:
        source = CARD.read_text()
        handler = _method_body(source, "_onMirroredBridgeStateEvent")
        self.assertIn('state === "in_call" || state === "answering"', handler)
        self.assertIn(
            "data.connected_party || data.answered_by || data.peer_name",
            handler,
        )
        self.assertIn('this._mirroredConnectedPeer = ""', handler)
        self.assertIn(
            "(!this._isHaSoftphoneMode() && this._mirroredConnectedPeer)",
            source,
        )

    def test_softphone_native_contact_popup_keeps_readable_system_contrast(self) -> None:
        source = CARD.read_text()
        self.assertIn(".destination-select option {", source)
        self.assertIn("color: CanvasText;", source)
        self.assertIn("background-color: Canvas;", source)

    def test_editor_only_lists_esps_and_cleans_retry_timer(self) -> None:
        editor = self.source[self.source.index("class VoipStackCardEditor") :]
        self.assertIn("const mirrorDevices = this._devices.filter(d => !d.softphone)", editor)
        self.assertIn("disconnectedCallback()", editor)
        self.assertIn("clearTimeout(this._devicesRetryTimer)", editor)
        self.assertIn(
            'if (!window.customCards.some(card => card.type === "voip-stack-card"))',
            editor,
        )


if __name__ == "__main__":
    unittest.main()
