#!/usr/bin/env python3
"""Runtime anti-regressions for browser softphone ownership and permission gates."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
ENGINE = (
    ROOT
    / "custom_components"
    / "voip_stack"
    / "frontend"
    / "voip-stack-engine.js"
)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_softphone_engine_runtime_ownership_and_permission_contracts() -> None:
    script = rf"""
import fs from "fs";
import vm from "vm";
import assert from "assert/strict";

let source = fs.readFileSync({json.dumps(str(ENGINE))}, "utf8");
source = source.replace(
  /const \{{ RINGTONE_REPEAT_MS, playVoipRingtone \}} =\s*await import\([^;]+;/,
  "const RINGTONE_REPEAT_MS = 4000; const playVoipRingtone = () => {{}};",
).replace("class VoipStackEngine", "export class VoipStackEngine");

const storage = new Map();
const session = new Map();
const context = vm.createContext({{
  EventTarget,
  Event,
  CustomEvent: class CustomEvent extends Event {{
    constructor(type, init) {{ super(type); this.detail = init?.detail; }}
  }},
  console,
  URL,
  encodeURIComponent,
  localStorage: {{
    getItem(key) {{ return storage.get(key) || null; }},
    setItem(key, value) {{ storage.set(key, String(value)); }},
  }},
  sessionStorage: {{
    getItem(key) {{ return session.get(key) || null; }},
    setItem(key, value) {{ session.set(key, String(value)); }},
    removeItem(key) {{ session.delete(key); }},
  }},
  navigator: {{}},
  window: {{
    location: {{ protocol: "https:", host: "ha.example" }},
    addEventListener() {{}},
    setInterval,
    clearInterval,
    setTimeout,
    clearTimeout,
  }},
  WebSocket: {{ OPEN: 1, CONNECTING: 0 }},
  setTimeout,
  clearTimeout,
  setInterval,
  clearInterval,
}});
const module = new vm.SourceTextModule(source, {{ context }});
await module.link(() => {{ throw new Error("unexpected import"); }});
await module.evaluate();
const Engine = module.namespace.VoipStackEngine;

// Media ownership identity is stable for this browser tab and is part of the
// Home Assistant signed path, so it cannot be changed after signing.
const signedPaths = [];
const signingHass = {{
  callWS: async (msg) => {{ signedPaths.push(msg.path); return {{ path: msg.path }}; }},
}};
const signedA = new Engine();
signedA.configure(signingHass);
await signedA._wsUrl("device", "signed-call");
const signedB = new Engine();
signedB.configure(signingHass);
await signedB._wsUrl("device", "signed-call");
assert.ok(signedA._mediaClientId.length >= 16);
assert.equal(signedA._mediaClientId, signedB._mediaClientId);
assert.equal(
  new URL(`https://ha.example${{signedPaths[0]}}`).searchParams.get("client_id"),
  signedA._mediaClientId,
);

// Auto-answer permission probing is fail-closed and never opens a camera
// prompt when persistent camera access is absent.
storage.set("voip_stack_video_camera_enabled", "true");
let mediaRequests = 0;
context.navigator.permissions = {{ query: async () => ({{ state: "prompt" }}) }};
context.navigator.mediaDevices = {{
  getUserMedia: async () => {{ mediaRequests++; throw new Error("must not prompt"); }},
}};
const permission = new Engine();
assert.equal(
  await permission.prepareVideoCameraPermission({{ persistentOnly: true }}),
  false,
);
assert.equal(mediaRequests, 0);

// A granted preflight acquires and immediately releases the probe stream.
let stopped = 0;
context.navigator.permissions = {{ query: async () => ({{ state: "granted" }}) }};
context.navigator.mediaDevices = {{
  getUserMedia: async () => {{
    mediaRequests++;
    const track = {{ stop() {{ stopped++; }} }};
    return {{ getVideoTracks() {{ return [track]; }}, getTracks() {{ return [track]; }} }};
  }},
}};
assert.equal(
  await permission.prepareVideoCameraPermission({{ persistentOnly: true }}),
  true,
);
assert.equal(stopped, 1);

// A rejected WebSocket ownership claim did not attach media, so it must not
// send BYE for the dialog owned by the newer card.
const ownership = new Engine();
const services = [];
ownership._hass = {{ callService: async (...args) => services.push(args) }};
ownership._connect = async () => {{ throw new Error("HTTP 409 owner conflict"); }};
ownership.close = async () => {{}};
ownership._setState = () => {{}};
assert.equal(
  await ownership._setupAudioOrAbort(
    "__voip_stack_ha_softphone__",
    {{ device_id: "__voip_stack_ha_softphone__" }},
    {{ call_id: "call-A" }},
  ),
  false,
);
assert.deepEqual(services, []);

// Once the WebSocket is open, an actual local audio setup failure makes this
// browser leg unusable and intentionally terminates that exact call.
ownership._connect = async (deviceId, callId) => {{
  ownership._deviceId = deviceId;
  ownership._callId = callId;
}};
ownership._setupAudio = async () => {{ throw new Error("unsupported PCM"); }};
assert.equal(
  await ownership._setupAudioOrAbort(
    "__voip_stack_ha_softphone__",
    {{ device_id: "__voip_stack_ha_softphone__" }},
    {{ call_id: "call-B" }},
  ),
  false,
);
assert.equal(services.length, 1);
assert.equal(services[0][0], "voip_stack");
assert.equal(services[0][1], "hangup");
assert.equal(services[0][2].call_id, "call-B");

// Canvas ownership is explicit. A mirror/second card cannot silently redirect
// decoded frames away from the current HA softphone card.
const canvas = new Engine();
const ownerA = {{ id: "A" }};
const ownerB = {{ id: "B" }};
const canvasA = {{ id: "canvas-A" }};
const canvasB = {{ id: "canvas-B" }};
assert.equal(canvas.claimVideoCanvas(ownerA, canvasA), true);
assert.equal(canvas.claimVideoCanvas(ownerB, canvasB), false);
assert.equal(canvas._videoCanvas, canvasA);
assert.equal(canvas.releaseVideoCanvas(ownerB), false);
assert.equal(canvas.releaseVideoCanvas(ownerA), true);
assert.equal(canvas.claimVideoCanvas(ownerB, canvasB), true);
assert.equal(canvas._videoCanvas, canvasB);
assert.equal(canvas.claimSoftphoneController({{ isConnected: false }}), false);
assert.equal(canvas.claimVideoCanvas({{ isConnected: false }}, {{}}), false);

// A same-call audio-only -> video-active state update must reconcile the
// optional video channel even though the audio WebSocket is already open.
const reconcile = new Engine();
reconcile._callId = "call-video";
const reconciled = [];
reconcile._ensureVideo = async (payload) => reconciled.push(payload.video_active);
await reconcile.reconcileSession({{
  state: "in_call",
  call_id: "call-video",
  audio_direction: "recvonly",
  video_active: true,
}});
assert.deepEqual(reconciled, [true]);
assert.equal(reconcile._audioDirection, "recvonly");

// A stuck attach for A cannot head-of-line block B. Identity checks in the
// real attach path make A tear down only its local, unpublished pipeline.
const preempt = new Engine();
const entered = [];
let releaseAttachA;
const attachA = new Promise((resolve) => {{ releaseAttachA = resolve; }});
preempt._resumeSessionLocked = async (_info, _device, payload) => {{
  entered.push(payload.call_id);
  if (payload.call_id === "A") await attachA;
}};
const pendingA = preempt.resumeSession({{}}, "device", {{ state: "in_call", call_id: "A" }});
await Promise.resolve();
const pendingB = preempt.resumeSession({{}}, "device", {{ state: "in_call", call_id: "B" }});
await Promise.resolve();
assert.deepEqual(entered, ["A", "B"]);
releaseAttachA();
await Promise.all([pendingA, pendingB]);

// close(A) detaches its audio objects before awaiting slow video/context
// teardown, so a B pipeline installed meanwhile survives the continuation.
const closeRace = new Engine();
let releaseOldAudio;
let releaseOldVideo;
const oldAudioGate = new Promise((resolve) => {{ releaseOldAudio = resolve; }});
const oldVideoGate = new Promise((resolve) => {{ releaseOldVideo = resolve; }});
closeRace._audioContext = {{ close: () => oldAudioGate }};
closeRace._video = {{ close: () => oldVideoGate }};
closeRace._callId = "A";
const closingA = closeRace.close("test");
assert.equal(closeRace._audioContext, null);
let bClosed = 0;
const contextB = {{ close: async () => {{ bClosed++; }} }};
closeRace._audioContext = contextB;
closeRace._callId = "B";
closeRace._state = "IN_CALL";
releaseOldAudio();
releaseOldVideo();
await closingA;
assert.equal(bClosed, 0);
assert.equal(closeRace._audioContext, contextB);
assert.equal(closeRace._callId, "B");
assert.equal(closeRace._state, "IN_CALL");

// Concurrent connect switches are ordered by invocation, not by whichever
// asynchronous close/sign operation happens to resolve last.
const connectRace = new Engine();
let releaseCloseA;
let releaseCloseB;
const closeA = new Promise((resolve) => {{ releaseCloseA = resolve; }});
const closeB = new Promise((resolve) => {{ releaseCloseB = resolve; }});
let closeCount = 0;
connectRace.close = async () => (++closeCount === 1 ? closeA : closeB);
connectRace._wsUrl = async (_deviceId, callId) => `wss://ha.example/${{callId}}`;
const sockets = [];
class RuntimeWebSocket {{
  static CONNECTING = 0;
  static OPEN = 1;
  constructor(url) {{
    this.url = url;
    this.readyState = RuntimeWebSocket.CONNECTING;
    sockets.push(this);
    setTimeout(() => {{
      this.readyState = RuntimeWebSocket.OPEN;
      this.onopen?.();
      this.onmessage?.({{ data: JSON.stringify({{
        state: "in_call",
        call_id: url.split("/").at(-1),
        tx_format: "48000:s16le:1:20",
        rx_format: "48000:s16le:1:20",
        audio_direction: "sendrecv",
      }}) }});
    }}, 0);
  }}
  close() {{ this.readyState = 3; this.onclose?.(); }}
  send() {{}}
}}
context.WebSocket = RuntimeWebSocket;
const connectA = connectRace._connect("device", "A").then(
  () => "A:ok",
  (err) => `A:${{err.message}}`,
);
const connectB = connectRace._connect("device", "B").then(
  () => "B:ok",
  (err) => `B:${{err.message}}`,
);
releaseCloseB();
await new Promise((resolve) => setTimeout(resolve, 0));
releaseCloseA();
assert.deepEqual(await Promise.all([connectA, connectB]), [
  "A:Audio WebSocket superseded before connect",
  "B:ok",
]);
assert.equal(connectRace._callId, "B");
assert.equal(sockets.length, 1);
assert.equal(sockets[0].url, "wss://ha.example/B");

// A call created after the initiating UI operation was cancelled is
// compensated with an exact-call hangup and is never claimed by the engine.
const superseded = new Engine();
const supersededServices = [];
superseded._hass = {{
  callWS: async () => ({{ state: "calling", call_id: "orphan-B" }}),
  callService: async (...args) => supersededServices.push(args),
}};
const staleReply = await superseded.startHaSoftphone(
  {{ name: "peer" }},
  {{}},
  {{ shouldAbort: () => true }},
);
assert.equal(staleReply.superseded, true);
assert.equal(superseded.softphoneCallId, "");
assert.equal(supersededServices.length, 1);
assert.equal(supersededServices[0][2].call_id, "orphan-B");

// Browser capture/playback follows the negotiated local SDP direction. A
// recvonly answer must not ask for microphone permission; hold/resume and
// direction expansion rebuild atomically when a path is added or removed.
let microphoneRequests = 0;
let stoppedTracks = 0;
context.navigator.mediaDevices = {{
  getUserMedia: async () => {{
    microphoneRequests++;
    const track = {{ enabled: true, stop() {{ stoppedTracks++; }} }};
    return {{
      getAudioTracks() {{ return [track]; }},
      getTracks() {{ return [track]; }},
    }};
  }},
}};
class RuntimeAudioContext {{
  constructor() {{
    this.state = "running";
    this.destination = {{}};
    this.audioWorklet = {{ addModule: async () => {{}} }};
  }}
  async resume() {{ this.state = "running"; }}
  createMediaStreamSource() {{
    return {{ connect(target) {{ return target; }}, disconnect() {{}} }};
  }}
  createGain() {{
    return {{ gain: {{ value: 1 }}, connect(target) {{ return target; }}, disconnect() {{}} }};
  }}
  async close() {{ this.state = "closed"; }}
}}
class RuntimeWorkletNode {{
  constructor(_context, name) {{
    this.name = name;
    this.port = {{ onmessage: null, postMessage() {{}} }};
  }}
  connect(target) {{ return target; }}
  disconnect() {{}}
}}
context.window.AudioContext = RuntimeAudioContext;
context.AudioWorkletNode = RuntimeWorkletNode;
const audio = new Engine();
audio._callId = "directional";
const pcm = {{
  call_id: "directional",
  selected_tx_format: "48000:s16le:1:20",
  selected_rx_format: "48000:s16le:1:20",
  audio_direction: "recvonly",
}};
audio._lastSessionPayload = pcm;
await audio._setupAudio({{ audio_mode: "full_duplex" }}, pcm);
assert.equal(microphoneRequests, 0);
assert.equal(audio._captureNode, null);
assert.equal(audio._playbackNode?.name, "voip-stack-playback-processor");

await audio._reconcileAudioMedia({{ ...pcm, audio_direction: "sendrecv" }});
assert.equal(microphoneRequests, 1);
assert.equal(audio._captureNode?.name, "voip-stack-processor");
assert.equal(audio._playbackNode?.name, "voip-stack-playback-processor");

await audio._reconcileAudioMedia({{ ...pcm, audio_direction: "inactive" }});
assert.equal(audio._captureNode, null);
assert.equal(audio._playbackNode, null);
assert.equal(stoppedTracks, 1);

const sendOnly = new Engine();
sendOnly._callId = "send-only";
await sendOnly._setupAudio(
  {{ audio_mode: "full_duplex" }},
  {{ ...pcm, call_id: "send-only", audio_direction: "sendonly" }},
);
assert.equal(microphoneRequests, 2);
assert.equal(sendOnly._captureNode?.name, "voip-stack-processor");
assert.equal(sendOnly._playbackNode, null);

// Lazy video-module resolution from call A cannot attach or close media after
// call B has replaced the session intent.
const lazyVideo = new Engine();
lazyVideo._callId = "video-A";
let resolveVideo;
let videoStarts = 0;
let videoCloses = 0;
lazyVideo._loadVideo = () => new Promise((resolve) => {{ resolveVideo = resolve; }});
const staleVideoAttach = lazyVideo._ensureVideo({{
  call_id: "video-A",
  video_active: true,
}});
await Promise.resolve();
lazyVideo._callId = "video-B";
lazyVideo._videoAttachGeneration++;
resolveVideo({{
  active: false,
  callId: "",
  async start() {{ videoStarts++; }},
  async close() {{ videoCloses++; }},
}});
await staleVideoAttach;
assert.equal(videoStarts, 0);
assert.equal(videoCloses, 0);
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
