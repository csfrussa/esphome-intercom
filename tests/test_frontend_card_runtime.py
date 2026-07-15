#!/usr/bin/env python3
"""Runtime anti-regressions for HA softphone card signaling state."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
CARD = (
    ROOT
    / "custom_components"
    / "voip_stack"
    / "frontend"
    / "voip-stack-card.js"
)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_card_runtime_follows_authoritative_sip_state_and_call_identity() -> None:
    """Exercise the real card class instead of matching source-code strings."""

    script = rf"""
import fs from "fs";
import vm from "vm";
import assert from "assert/strict";

let source = fs.readFileSync({json.dumps(str(CARD))}, "utf8");
source = source
  .replace(/await import\(`\.\/voip-phonebook-card\.js[^;]+;/, "")
  .replace(
    /const \{{ voipStackEngine \}} = await import\(`\.\/voip-stack-engine\.js[^;]+;/,
    "const {{ voipStackEngine }} = globalThis.__engine;",
  )
  .replace("class VoipStackCard extends HTMLElement", "export class VoipStackCard extends HTMLElement");

const serviceCalls = [];
const engineEvents = [];
const engine = {{
  active: false,
  callId: "",
  deviceId: "",
  softphoneCallId: "",
  videoVisible: false,
  videoCameraEnabled: false,
  addEventListener() {{}},
  removeEventListener() {{}},
  claimSoftphoneController(card) {{ return !!card?.isConnected; }},
  releaseSoftphoneController() {{}},
  claimVideoCanvas() {{ return false; }},
  releaseVideoCanvas() {{ return false; }},
  clearRingtoneRequest() {{}},
  setRingtoneRequest() {{}},
  statsText() {{ return ""; }},
  ownsSoftphoneSession(callId) {{ return this.softphoneCallId === callId; }},
  claimSoftphoneSession(callId) {{ this.softphoneCallId = String(callId || ""); }},
  releaseSoftphoneSession(callId = "") {{
    if (!callId || this.softphoneCallId === callId) this.softphoneCallId = "";
  }},
  async close(reason) {{
    engineEvents.push(["close", reason, this.callId]);
    this.active = false;
    this.callId = "";
  }},
  async reconcileSession() {{}},
  async resumeSession() {{ return true; }},
  async prepareVideoCameraPermission() {{ return false; }},
  async startHaSoftphone() {{ throw new Error("not configured"); }},
}};

class FakeClassList {{
  constructor() {{ this.values = new Set(); }}
  toggle(name, wanted) {{
    if (wanted) this.values.add(name);
    else this.values.delete(name);
    return !!wanted;
  }}
}}
class FakeElement {{
  constructor() {{
    this.textContent = "";
    this.hidden = false;
    this.disabled = false;
    this.checked = false;
    this.value = "";
    this.className = "";
    this.classList = new FakeClassList();
    this.style = {{ display: "", setProperty() {{}} }};
    this.attributes = new Map();
  }}
  setAttribute(name, value) {{ this.attributes.set(name, String(value)); }}
  addEventListener() {{}}
  focus() {{}}
}}
class FakeHTMLElement extends EventTarget {{
  constructor() {{
    super();
    this.isConnected = true;
    this.shadowRoot = null;
  }}
  attachShadow() {{
    this.shadowRoot = {{ querySelector() {{ return null; }} }};
    return this.shadowRoot;
  }}
}}
class FakeResizeObserver {{ observe() {{}} disconnect() {{}} }}

const storage = new Map();
const registry = new Map();
const context = vm.createContext({{
  __engine: {{ voipStackEngine: engine }},
  EventTarget,
  Event,
  CustomEvent: class CustomEvent extends Event {{
    constructor(type, init) {{ super(type); this.detail = init?.detail; }}
  }},
  HTMLElement: FakeHTMLElement,
  ResizeObserver: FakeResizeObserver,
  WheelEvent: {{ DOM_DELTA_LINE: 1, DOM_DELTA_PAGE: 2 }},
  customElements: {{
    get(name) {{ return registry.get(name); }},
    define(name, value) {{ registry.set(name, value); }},
  }},
  document: {{ createElement() {{ return new FakeElement(); }} }},
  localStorage: {{
    getItem(key) {{ return storage.get(key) || null; }},
    setItem(key, value) {{ storage.set(key, String(value)); }},
    removeItem(key) {{ storage.delete(key); }},
  }},
  sessionStorage: {{ getItem() {{ return null; }}, setItem() {{}}, removeItem() {{}} }},
  console,
  URL,
  encodeURIComponent,
  setTimeout,
  clearTimeout,
  setInterval,
  clearInterval,
  requestAnimationFrame(callback) {{ callback(); }},
  window: {{
    customCards: [],
    innerHeight: 800,
    location: {{ href: "https://ha.example/", protocol: "https:", host: "ha.example" }},
    scrollBy() {{}},
  }},
}});
const module = new vm.SourceTextModule(source, {{ context }});
await module.link(() => {{ throw new Error("unexpected import"); }});
await module.evaluate();
const Card = module.namespace.VoipStackCard;

function elements() {{
  const names = [
    "answerBtn", "autoAnswerCheckbox", "autoAnswerRow", "callBtn", "card",
    "declineBtn", "destRow", "destSelect", "destValue", "destValueWrap",
    "dndCheckbox", "dndRow", "err", "hangupBtn", "hangupDuration",
    "hangupPeer", "hangupState", "header", "headerName", "keypadBtn",
    "keypadInput", "keypadPanel", "nextBtn", "offlinePanel", "placeholderBtn",
    "prevBtn", "ringtoneCheckbox", "ringtoneRow", "runtimeControls",
    "settingsBtn", "settingsPanel", "softphoneGroupsPanel", "stats",
    "statusIndicator", "statusReason", "statusText", "videoCameraCheckbox",
    "videoCameraRow", "videoCanvas", "videoShade",
  ];
  const result = Object.fromEntries(names.map((name) => [name, new FakeElement()]));
  result.keypadKeys = {{ one: new FakeElement() }};
  return result;
}}

function makeCard() {{
  const card = new Card();
  card.config = {{ mode: "ha_softphone", name: "Office HA" }};
  card._hass = {{
    config: {{ location_name: "Office HA" }},
    states: {{}},
    callService: async (...args) => serviceCalls.push(args),
  }};
  card._skeletonMode = "main";
  card._els = elements();
  card._renderSoftphoneDestinationSelect = () => {{}};
  card._renderGroupControls = () => {{}};
  card._maybeAnswerFromUrl = () => {{}};
  card._captureEndReason = (kind, reason, origin, peer) => {{
    card._lastEndInfo = {{ kind, reason, origin, peer }};
  }};
  card._getDeviceInfo = async () => ({{
    device_id: "__voip_stack_ha_softphone__",
    name: "Office HA",
    softphone: true,
  }});
  return card;
}}

const card = makeCard();
const base = {{
  call_id: "call-A",
  direction: "outgoing",
  caller: "Office HA",
  callee: "Home HA",
  peer_name: "Home HA",
}};

// The local Call action may publish only the backend's provisional result.
// It must not optimistically promote the card to in_call.
const outbound = makeCard();
outbound._rosterEntries = [{{
  id: "home-ha",
  name: "Home HA",
  enabled: true,
  metadata: {{}},
}}];
let outboundStart;
engine.startHaSoftphone = async (target, session, options) => {{
  outboundStart = {{ target, session, options }};
  return {{ ...base, state: "calling", sequence: 1 }};
}};
await outbound._startCall();
assert.equal(outboundStart.target.name, "Home HA");
assert.equal(outboundStart.options.callee, "Home HA");
assert.equal(outbound._softphoneSnapshot.state, "calling");
outbound._render();
assert.equal(outbound._els.statusText.textContent, "Calling Home HA...");
// Lovelace recreating the element is not a user cancellation.  The
// page-level engine must keep the outbound SIP transaction alive so the
// replacement card can adopt it while it is calling or ringing.
assert.equal(outboundStart.options.shouldAbort(), false);
outbound.isConnected = false;
outbound.disconnectedCallback();
assert.equal(outboundStart.options.shouldAbort(), false);

// Provisional SIP responses never render as an answered call. Only the
// authoritative final state can switch the card to "In Call".
assert.equal(card._applySoftphoneSnapshot({{ ...base, state: "calling", sequence: 1 }}), true);
card._render();
assert.equal(card._els.statusText.textContent, "Calling Home HA...");
assert.equal(card._els.hangupBtn.hidden, false);
assert.equal(card._els.answerBtn.hidden, true);

assert.equal(card._applySoftphoneSnapshot({{ ...base, state: "remote_ringing", sequence: 2 }}), true);
card._render();
assert.equal(card._els.statusText.textContent, "Home HA is ringing...");
assert.equal(card._els.hangupState.textContent, "Ringing");
assert.equal(card._applySoftphoneSnapshot({{ ...base, state: "calling", sequence: 1 }}), false);
assert.equal(card._softphoneSnapshot.state, "remote_ringing");

assert.equal(card._applySoftphoneSnapshot({{ ...base, state: "in_call", sequence: 3 }}), true);
card._render();
assert.equal(card._els.statusText.textContent, "In Call: Home HA");
assert.equal(card._els.hangupState.textContent, "In call");
assert.equal(card._applySoftphoneSnapshot({{ ...base, state: "remote_ringing", sequence: 2 }}), false);
assert.equal(card._softphoneSnapshot.state, "in_call");

// Pressing Answer sends the exact call service but does not invent an
// in-call state before the backend publishes a final 200/answer snapshot.
const incoming = makeCard();
incoming._applySoftphoneSnapshot({{
  state: "ringing",
  direction: "incoming",
  call_id: "incoming-A",
  caller: "Door",
  peer_name: "Door",
  sequence: 1,
}});
await incoming._answer();
assert.equal(incoming._softphoneSnapshot.state, "ringing");
assert.equal(JSON.stringify(serviceCalls.at(-1)), JSON.stringify([
  "voip_stack",
  "answer",
  {{ call_id: "incoming-A", send_video: false }},
]));
incoming._applySoftphoneSnapshot({{
  state: "answering",
  direction: "incoming",
  call_id: "incoming-A",
  caller: "Door",
  peer_name: "Door",
  sequence: 2,
}});
incoming._render();
assert.equal(incoming._els.statusText.textContent, "Answering Door...");
incoming._applySoftphoneSnapshot({{
  state: "in_call",
  direction: "incoming",
  call_id: "incoming-A",
  caller: "Door",
  peer_name: "Door",
  sequence: 3,
}});
incoming._render();
assert.equal(incoming._els.statusText.textContent, "In Call: Door");

// If a different call replaces the ring while device lookup is pending, the
// stale Answer operation is discarded and cannot answer the new call.
const race = makeCard();
race._applySoftphoneSnapshot({{
  state: "ringing", direction: "incoming", call_id: "race-A", caller: "A", sequence: 1,
}});
let releaseLookup;
race._getDeviceInfo = () => new Promise((resolve) => {{ releaseLookup = resolve; }});
const pendingAnswer = race._answer();
await Promise.resolve();
race._applySoftphoneSnapshot({{
  state: "ringing", direction: "incoming", call_id: "race-B", caller: "B", sequence: 1,
}});
const serviceCount = serviceCalls.length;
releaseLookup({{ device_id: "__voip_stack_ha_softphone__" }});
await pendingAnswer;
assert.equal(serviceCalls.length, serviceCount);
assert.equal(race._softphoneSnapshot.call_id, "race-B");

// Home Assistant may recreate the Lovelace element while an Answer request is
// in flight.  The page-level engine owns the media session, so the detached
// element must not compensate with a late hangup: the replacement card will
// adopt the same authoritative backend call.
const replacement = makeCard();
replacement._applySoftphoneSnapshot({{
  state: "ringing", direction: "incoming", call_id: "replace-A", caller: "Door", sequence: 1,
}});
let releaseAnswer;
const replacementCalls = [];
replacement._hass.callService = async (...args) => {{
  replacementCalls.push(args);
  if (args[1] === "answer") await new Promise((resolve) => {{ releaseAnswer = resolve; }});
}};
const pendingReplacementAnswer = replacement._answer({{ videoPermission: false }});
for (let attempt = 0; attempt < 10 && !releaseAnswer; attempt++) {{
  await Promise.resolve();
}}
assert.equal(replacementCalls.length, 1);
replacement._lifecycleGeneration++;
replacement.isConnected = false;
releaseAnswer();
await pendingReplacementAnswer;
assert.equal(JSON.stringify(replacementCalls), JSON.stringify([[
  "voip_stack",
  "answer",
  {{ call_id: "replace-A", send_video: false }},
]]));

// Hangup is call-scoped and likewise waits for the backend terminal snapshot.
engine.active = true;
engine.callId = "incoming-A";
engine.softphoneCallId = "incoming-A";
incoming._loadSoftphoneState = async () => {{}};
await incoming._hangup();
assert.equal(JSON.stringify(serviceCalls.at(-1)), JSON.stringify([
  "voip_stack",
  "hangup",
  {{ call_id: "incoming-A" }},
]));
assert.equal(incoming._softphoneSnapshot.state, "in_call");
incoming._applySoftphoneSnapshot({{
  ...incoming._softphoneSnapshot,
  state: "idle",
  terminal_reason: "local_hangup",
  sequence: 4,
}});
incoming._render();
assert.equal(incoming._els.statusText.textContent, "Call with Door ended.");
assert.match(incoming._els.statusReason.textContent, /Local hangup/);
"""
    completed = subprocess.run(
        [
            "node",
            "--no-warnings",
            "--experimental-vm-modules",
            "--input-type=module",
            "-e",
            script,
        ],
        cwd=ROOT,
        check=False,
        capture_output=True,
        text=True,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
