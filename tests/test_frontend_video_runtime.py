#!/usr/bin/env python3
"""Runtime anti-regressions for the browser SIP video media engine."""

from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess

import pytest


ROOT = Path(__file__).resolve().parents[1]
VIDEO_ENGINE = (
    ROOT
    / "custom_components"
    / "voip_stack"
    / "frontend"
    / "voip-stack-video.js"
)


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_video_engine_runtime_recovery_contracts() -> None:
    script = f"""
import fs from "fs";
import vm from "vm";
import assert from "assert/strict";

const source = fs.readFileSync({json.dumps(str(VIDEO_ENGINE))}, "utf8");
const cameraStorage = new Map();
const context = vm.createContext({{
  EventTarget,
  performance,
  console,
  Blob,
  CustomEvent: class CustomEvent extends Event {{
    constructor(type, init) {{ super(type); this.detail = init?.detail; }}
  }},
  localStorage: {{
    getItem(key) {{ return cameraStorage.has(key) ? cameraStorage.get(key) : null; }},
    setItem(key, value) {{ cameraStorage.set(key, String(value)); }},
  }},
  WebSocket: {{ OPEN: 1, CONNECTING: 0 }},
  EncodedVideoChunk: class EncodedVideoChunk {{ constructor(init) {{ Object.assign(this, init); }} }},
}});
const module = new vm.SourceTextModule(source, {{ context }});
await module.link(() => {{ throw new Error("unexpected import"); }});
await module.evaluate();
const Video = module.namespace.VoipStackVideo;

// Camera transmission is a browser preference per logical phone. The legacy
// global choice is inherited until that endpoint writes its own value.
cameraStorage.set("voip_stack_video_camera_enabled", "true");
const preferences = new Video();
assert.equal(preferences.cameraEnabledFor("default"), true);
assert.equal(preferences.cameraEnabledFor("kitchen"), true);
await preferences.setCameraEnabled(false, "kitchen");
assert.equal(preferences.cameraEnabledFor("kitchen"), false);
assert.equal(preferences.cameraEnabledFor("default"), true);
await preferences.setCameraEnabled(true, "test-phone");
assert.equal(cameraStorage.get("voip_stack_video_camera_enabled:test-phone"), "true");
const restoredPreferences = new Video();
assert.equal(restoredPreferences.cameraEnabledFor("kitchen"), false);
assert.equal(restoredPreferences.cameraEnabledFor("test-phone"), true);

// RFC 6184 Main/High streams can arrive in decode order with non-monotonic
// presentation timestamps (I00, R03, N01, N02). Preserve those timestamps.
const timestamps = new Video();
timestamps._clockRate = 90000;
assert.deepEqual(
  [0, 3000, 1000, 2000].map((value) => timestamps._unwrapRtpTimestamp(value)),
  [0, 33333, 11111, 22222],
);

// Decoder output without a currently-owned canvas is a dropped frame, not a
// rendered frame. This keeps debug counters truthful during card handoff.
const noCanvas = new Video();
noCanvas._active = true;
noCanvas._canReceive = true;
assert.equal(noCanvas.visible, false);
let noCanvasClosed = false;
noCanvas._queueDecodedFrame({{
  timestamp: 1,
  displayWidth: 1,
  displayHeight: 1,
  close() {{ noCanvasClosed = true; }},
}});
assert.equal(noCanvasClosed, true);
assert.equal(noCanvas._stats.rendered, 0);
assert.equal(noCanvas._stats.dropped, 1);
assert.equal(noCanvas._stats.dropped_no_canvas, 1);
noCanvas._stats.rendered = 1;
assert.equal(noCanvas.visible, true);
const wrapped = new Video();
wrapped._clockRate = 90000;
assert.deepEqual(
  [0xffffff00, 0x100].map((value) => wrapped._unwrapRtpTimestamp(value)),
  [0, 5689],
);

// Losing a keyframe to WebSocket backpressure invalidates its dependent GOP.
// Deltas stay blocked until a later keyframe is actually sent.
const sender = new Video();
const sent = [];
sender._clockRate = 90000;
sender._ws = {{ readyState: 1, bufferedAmount: 3 * 1024 * 1024, send(value) {{ sent.push(value); }} }};
const chunk = (type) => ({{
  type,
  byteLength: 2,
  timestamp: 0,
  copyTo(target) {{ target.set([1, 2]); }},
}});
sender._sendEncodedChunk(chunk("key"));
assert.equal(sender._sendDropUntilKeyFrame, true);
sender._ws.bufferedAmount = 0;
sender._sendEncodedChunk(chunk("delta"));
assert.equal(sent.length, 0);
sender._sendEncodedChunk(chunk("key"));
assert.equal(sent.length, 1);
assert.equal(sender._sendDropUntilKeyFrame, false);

// A synchronous decoder failure must re-enter keyframe acquisition and emit
// an RTCP feedback request through the media WebSocket.
const receiver = new Video();
const controls = [];
receiver._clockRate = 90000;
receiver._ws = {{ readyState: 1, send(value) {{ controls.push(value); }} }};
receiver._decoder = {{
  state: "configured",
  decodeQueueSize: 0,
  decode() {{ throw new Error("decoder rejected frame"); }},
}};
const frame = new ArrayBuffer(8);
const bytes = new Uint8Array(frame);
bytes[0] = 1;
bytes[1] = 1;
new DataView(frame).setUint32(2, 9000, false);
receiver._decodeMessage(frame);
assert.equal(receiver._stats.decode_errors, 1);
assert.equal(receiver._dropUntilKeyFrame, true);
assert.equal(JSON.parse(controls.at(-1)).type, "request_key_frame");

// Codec setup without a buffered key frame must retain the resync gate. A
// later delta cannot be treated as independently decodable.
const emptyFlush = new Video();
emptyFlush._dropUntilKeyFrame = true;
emptyFlush._flushPendingDecode();
assert.equal(emptyFlush._dropUntilKeyFrame, true);

// Remote RTCP PLI/FIR control must force the next camera encode to be a key
// frame, while unrelated JSON remains available to the negotiation handler.
const camera = new Video();
camera._forceCameraKeyFrame = false;
assert.equal(camera._handleEncoderControl({{ type: "force_key_frame", feedback: "pli" }}), true);
assert.equal(camera._forceCameraKeyFrame, true);
assert.equal(camera._handleEncoderControl({{ type: "media_update" }}), false);
const epochs = [];
camera._ws = {{ readyState: 1, send(value) {{ epochs.push(JSON.parse(value)); }} }};
camera._sendTxEpoch();
assert.equal(epochs.at(-1).type, "tx_epoch");

// A JPEG decode Promise from call A must not clear call B's in-flight decode
// or consume the frame that B coalesced behind it.
const jpegResolvers = [];
context.createImageBitmap = () => new Promise((resolve) => jpegResolvers.push(resolve));
const jpeg = new Video();
jpeg._encoding = "JPEG";
jpeg._active = true;
jpeg._generation = 1;
const jpegFrame = (timestamp) => {{
  const value = new ArrayBuffer(8);
  const view = new Uint8Array(value);
  view[0] = 1;
  view[1] = 1;
  new DataView(value).setUint32(2, timestamp, false);
  view[6] = 0xff;
  view[7] = 0xd8;
  return value;
}};
const frameA = jpegFrame(1);
const frameB = jpegFrame(2);
const frameBLatest = jpegFrame(3);
jpeg._decodeJpegMessage(frameA);
assert.equal(jpegResolvers.length, 1);
jpeg._generation = 2;
jpeg._jpegDecodePending = false;
jpeg._jpegQueuedBuffer = null;
jpeg._jpegDecodeToken = null;
jpeg._decodeJpegMessage(frameB);
jpeg._decodeJpegMessage(frameBLatest);
assert.equal(jpegResolvers.length, 2);
jpegResolvers[0]({{ width: 1, height: 1, close() {{}} }});
await new Promise((resolve) => setImmediate(resolve));
assert.equal(jpeg._jpegDecodePending, true);
assert.equal(jpeg._jpegQueuedBuffer, frameBLatest);
jpegResolvers[1]({{ width: 1, height: 1, close() {{}} }});
await new Promise((resolve) => setImmediate(resolve));
assert.equal(jpegResolvers.length, 3);
jpegResolvers[2]({{ width: 1, height: 1, close() {{}} }});

// A media update blocked in call A must not head-of-line block call B after
// close/start ownership changes.
const updates = new Video();
let releaseOldUpdate;
const oldUpdate = new Promise((resolve) => {{ releaseOldUpdate = resolve; }});
const applied = [];
updates._applyMediaUpdate = async (payload) => {{
  if (payload.id === "A") await oldUpdate;
  applied.push(payload.id);
}};
const wsA = {{ readyState: 3 }};
updates._ws = wsA;
updates._callId = "A";
updates._enqueueMediaUpdate({{ id: "A" }}, wsA, "A");
await Promise.resolve();
await updates.close();
const wsB = {{ readyState: 1 }};
updates._ws = wsB;
updates._callId = "B";
updates._enqueueMediaUpdate({{ id: "B" }}, wsB, "B");
await Promise.resolve();
await Promise.resolve();
assert.deepEqual(applied, ["B"]);
releaseOldUpdate();
await Promise.resolve();

// A same-call media update starts a fresh RTP timestamp epoch. Reset both the
// unwrap state and render/decode monotonic guards or the first new frame can
// be discarded forever as a timestamp regression.
const updateEpoch = new Video();
const updateWs = {{ readyState: 1 }};
updateEpoch._ws = updateWs;
updateEpoch._callId = "epoch";
updateEpoch._lastRenderedAt = 10;
updateEpoch._lastRenderedTimestamp = 999999;
updateEpoch._lastDecodedAt = 10;
updateEpoch._lastDecodedTimestamp = 999999;
updateEpoch._cleanupSender = async () => {{}};
updateEpoch._cleanupReceiver = () => {{}};
updateEpoch._setupCodecs = async () => {{}};
await updateEpoch._applyMediaUpdate({{
  encoding: "H264",
  clock_rate: 90000,
  can_receive: false,
  can_send: false,
}}, updateWs, "epoch");
assert.equal(updateEpoch._lastRenderedAt, 0);
assert.equal(updateEpoch._lastRenderedTimestamp, null);
assert.equal(updateEpoch._lastDecodedAt, 0);
assert.equal(updateEpoch._lastDecodedTimestamp, null);

// A direct/transcode topology change must release the old server-side owner
// and reconnect. Reconfiguring codecs on the same WebSocket would leave its
// FFmpeg process, loopback queue, and receive task bound to the old SDP.
const pipelineRestart = new Video();
const pipelineWs = {{ readyState: 1 }};
pipelineRestart._ws = pipelineWs;
pipelineRestart._callId = "pipeline";
const restartCalls = [];
pipelineRestart.close = async function() {{
  restartCalls.push("close");
  this._generation++;
  this._ws = null;
  this._callId = "";
}};
pipelineRestart.start = async function(payload) {{
  restartCalls.push(["start", payload]);
  return true;
}};
assert.equal(await pipelineRestart._applyMediaUpdate({{
  type: "media_update",
  restart_required: true,
  restart_reason: "video_pipeline_changed",
  call_id: "pipeline",
  encoding: "VP8",
}}, pipelineWs, "pipeline"), true);
assert.equal(restartCalls[0], "close");
assert.equal(restartCalls[1][0], "start");
assert.equal(restartCalls[1][1].call_id, "pipeline");
assert.equal(restartCalls[1][1].video_active, true);

// A newer owner that starts while cleanup is pending wins; a stale pipeline
// restart must never tear it down or resurrect the preceding call.
const staleRestart = new Video();
const staleRestartWs = {{ readyState: 1 }};
staleRestart._ws = staleRestartWs;
staleRestart._callId = "old";
let releaseRestartClose;
staleRestart.close = async function() {{
  this._generation++;
  this._ws = null;
  this._callId = "";
  await new Promise((resolve) => {{ releaseRestartClose = resolve; }});
}};
let staleRestartStarted = false;
staleRestart.start = async () => {{ staleRestartStarted = true; }};
const staleRestartPromise = staleRestart._applyMediaUpdate({{
  restart_required: true,
}}, staleRestartWs, "old");
while (!releaseRestartClose) await Promise.resolve();
staleRestart._generation++;
staleRestart._callId = "new";
releaseRestartClose();
await staleRestartPromise;
assert.equal(staleRestartStarted, false);
assert.equal(staleRestart._callId, "new");

// A closed call waiting for auth/sign_path must not create a WebSocket when
// the stale signing Promise eventually resolves.
const constructedSockets = [];
class TestWebSocket {{
  static CONNECTING = 0;
  static OPEN = 1;
  static CLOSED = 3;
  constructor(url) {{
    this.url = url;
    this.readyState = TestWebSocket.CONNECTING;
    this.bufferedAmount = 0;
    constructedSockets.push(this);
  }}
  close() {{ this.readyState = TestWebSocket.CLOSED; }}
  send() {{}}
}}
context.WebSocket = TestWebSocket;
context.window = {{
  isSecureContext: true,
  location: {{ protocol: "https:", host: "ha.example" }},
  setTimeout,
}};
const videoSignedPaths = [];
const signedVideo = new Video();
signedVideo.configure({{
  callWS: async (msg) => {{
    videoSignedPaths.push(msg.path);
    return {{ path: msg.path }};
  }},
}}, "tab-0123456789abcdef");
await signedVideo._wsUrl("signed-video-call");
assert.equal(
  new URL(`https://ha.example${{videoSignedPaths[0]}}`).searchParams.get("client_id"),
  "tab-0123456789abcdef",
);
let resolveSignedPath;
const staleStart = new Video();
staleStart.configure({{
  callWS() {{
    return new Promise((resolve) => {{ resolveSignedPath = resolve; }});
  }},
}});
const staleStartPromise = staleStart.start({{ call_id: "A", video_active: true }});
while (!resolveSignedPath) await Promise.resolve();
await staleStart.close();
resolveSignedPath({{ path: "/signed-A" }});
assert.equal(await staleStartPromise, false);
assert.equal(constructedSockets.length, 0);

// Closing a CONNECTING socket rejects the open wait; start() must settle
// immediately instead of hanging until its three-second hello timeout.
const connecting = new Video();
connecting.configure({{ callWS: async () => ({{ path: "/signed-B" }}) }});
const connectingStart = connecting.start({{ call_id: "B", video_active: true }});
while (!constructedSockets.length) await Promise.resolve();
const socketB = constructedSockets.at(-1);
socketB.readyState = TestWebSocket.CLOSED;
socketB.onclose();
assert.equal(await connectingStart, false);

// Two camera-enable actions share one setup. They must not prompt twice or
// publish two encoders for the same dialog generation.
const cameraRace = new Video();
cameraRace._generation = 7;
cameraRace._active = true;
cameraRace._cameraAllowed = true;
cameraRace._negotiated = {{ codec: "vp8" }};
let cameraSetups = 0;
let releaseCameraSetup;
const cameraSetupGate = new Promise((resolve) => {{ releaseCameraSetup = resolve; }});
cameraRace._setupEncoder = async () => {{
  cameraSetups++;
  await cameraSetupGate;
  cameraRace._encoder = {{ state: "configured" }};
}};
const enableOne = cameraRace.setCameraEnabled(true);
const enableTwo = cameraRace.setCameraEnabled(true);
await Promise.resolve();
assert.equal(cameraSetups, 1);
releaseCameraSetup();
await Promise.all([enableOne, enableTwo]);
assert.equal(cameraRace._canSend, true);

// Cleanup from call A detaches its resources synchronously. If cancellation
// finishes after call B has published a new pipeline, it must not null/close B.
const ownership = new Video();
let releaseReaderCancel;
const oldReader = {{
  cancel() {{ return new Promise((resolve) => {{ releaseReaderCancel = resolve; }}); }},
}};
const oldEncoder = {{ state: "configured", close() {{ this.state = "closed"; }} }};
const oldTrack = {{ stopped: false, stop() {{ this.stopped = true; }} }};
ownership._cameraReader = oldReader;
ownership._encoder = oldEncoder;
ownership._cameraStream = {{ getTracks() {{ return [oldTrack]; }} }};
ownership._encodeTask = Promise.resolve();
const oldCleanup = ownership._cleanupSender();
assert.equal(ownership._cameraReader, null);
const newReader = {{ cancel() {{ throw new Error("must remain owned by B"); }} }};
const newEncoder = {{ state: "configured", close() {{ throw new Error("must remain open"); }} }};
const newStream = {{ getTracks() {{ return []; }} }};
ownership._cameraReader = newReader;
ownership._encoder = newEncoder;
ownership._cameraStream = newStream;
releaseReaderCancel();
await oldCleanup;
assert.equal(ownership._cameraReader, newReader);
assert.equal(ownership._encoder, newEncoder);
assert.equal(ownership._cameraStream, newStream);
assert.equal(oldEncoder.state, "closed");
assert.equal(oldTrack.stopped, true);

// Unexpected camera EOF releases the published sender immediately instead
// of leaving the card in a false video-transmitting state.
const cameraEof = new Video();
cameraEof._generation = 3;
cameraEof._senderGeneration = 4;
cameraEof._cameraEnabled = true;
cameraEof._cameraAllowed = true;
cameraEof._canSend = true;
const eofReader = {{ read: async () => ({{ done: true }}) }};
const eofEncoder = {{ state: "configured", encodeQueueSize: 0, close() {{ this.state = "closed"; }} }};
let eofTrackStopped = 0;
cameraEof._cameraReader = eofReader;
cameraEof._encoder = eofEncoder;
cameraEof._cameraStream = {{ getTracks: () => [{{ stop() {{ eofTrackStopped++; }} }}] }};
const eofTask = cameraEof._encodeCamera(15, eofReader, eofEncoder, 3, 4);
cameraEof._encodeTask = eofTask;
await eofTask;
assert.equal(cameraEof._cameraReader, null);
assert.equal(cameraEof._encoder, null);
assert.equal(cameraEof._cameraStream, null);
assert.equal(cameraEof._canSend, false);
assert.equal(eofEncoder.state, "closed");
assert.equal(eofTrackStopped, 1);

// Camera acquisition must obey the receive envelope advertised by the peer.
// The former fixed 640x360 request violated H.264 Level 1.3 (MaxFS 396).
const h264Low = new Video();
h264Low._encoding = "H264";
h264Low._negotiated = {{
  codec: "avc1.42800D",
  profile_level_id: "42800d",
  fmtp: "profile-level-id=42800d;packetization-mode=1",
}};
const h264LowContract = h264Low._cameraCaptureContract();
assert.equal(h264LowContract.maxFs, 396);
assert.deepEqual(
  [h264LowContract.maxWidth, h264LowContract.maxHeight],
  [352, 288],
);
assert.equal(h264LowContract.maxFr, 20);

const h264OneB = new Video();
h264OneB._encoding = "H264";
h264OneB._negotiated = {{
  codec: "avc1.42B00B",
  profile_level_id: "42b00b",
}};
const h264OneBContract = h264OneB._cameraCaptureContract();
assert.equal(h264OneBContract.maxFs, 99);
assert.deepEqual(
  [h264OneBContract.maxWidth, h264OneBContract.maxHeight],
  [176, 144],
);
assert.equal(h264OneBContract.maxFr, 15);

const h264Default = new Video();
h264Default._encoding = "H264";
h264Default._negotiated = {{
  codec: "avc1.42801F",
  profile_level_id: "42801f",
}};
const h264DefaultContract = h264Default._cameraCaptureContract();
assert.deepEqual(
  [h264DefaultContract.idealWidth, h264DefaultContract.idealHeight],
  [640, 360],
);
assert.deepEqual(
  [h264DefaultContract.maxWidth, h264DefaultContract.maxHeight],
  [1280, 720],
);

const vp8Limited = new Video();
vp8Limited._encoding = "VP8";
vp8Limited._negotiated = {{ fmtp: "max-fr=12;max-fs=1200" }};
const vp8Contract = vp8Limited._cameraCaptureContract();
assert.deepEqual(
  [vp8Contract.maxWidth, vp8Contract.maxHeight, vp8Contract.maxFr],
  [640, 360, 12],
);

// Decoder output/error callbacks are owned by the generation that created
// them. A delayed callback from A closes its frame and cannot mutate B.
class TestDecoder {{
  static async isConfigSupported(config) {{ return {{ supported: true, config }}; }}
  constructor(init) {{ this.init = init; this.state = "unconfigured"; this.decodeQueueSize = 0; }}
  configure() {{ this.state = "configured"; }}
  close() {{ this.state = "closed"; }}
}}
context.VideoDecoder = TestDecoder;
const decoderOwner = new Video();
decoderOwner._generation = 11;
decoderOwner._encoding = "H264";
await decoderOwner._setupCodecs({{
  codec: "avc1.42E01F",
  can_receive: true,
  can_send: false,
}}, 11);
const decoderA = decoderOwner._decoder;
decoderOwner._generation = 12;
decoderOwner._decoder = {{ state: "configured" }};
let staleFrameClosed = false;
decoderA.init.output({{ close() {{ staleFrameClosed = true; }} }});
decoderA.init.error();
assert.equal(staleFrameClosed, true);
assert.equal(decoderOwner._stats.rendered, 0);
assert.equal(decoderOwner._stats.decode_errors, 0);
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
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout


@pytest.mark.skipif(shutil.which("node") is None, reason="Node.js is unavailable")
def test_video_engine_directional_media_contracts() -> None:
    script = f"""
import fs from "fs";
import vm from "vm";
import assert from "assert/strict";

const source = fs.readFileSync({json.dumps(str(VIDEO_ENGINE))}, "utf8");
const context = vm.createContext({{
  EventTarget,
  performance,
  console,
  Blob,
  CustomEvent: class CustomEvent extends Event {{
    constructor(type, init) {{ super(type); this.detail = init?.detail; }}
  }},
  localStorage: {{ getItem() {{ return null; }}, setItem() {{}} }},
  WebSocket: {{ OPEN: 1, CONNECTING: 0 }},
  EncodedVideoChunk: class EncodedVideoChunk {{ constructor(init) {{ Object.assign(this, init); }} }},
}});
const module = new vm.SourceTextModule(source, {{ context }});
await module.link(() => {{ throw new Error("unexpected import"); }});
await module.evaluate();
const Video = module.namespace.VoipStackVideo;

const asymmetric = {{
  can_send: true,
  can_receive: true,
  // Flat fields deliberately describe RX for compatibility with old cards.
  codec: "avc1.64001F",
  encoding: "H264",
  clock_rate: 90000,
  payload_type: 121,
  fmtp: "profile-level-id=64001f;packetization-mode=1",
  profile_level_id: "64001f",
  packetization_mode: 1,
  send: {{
    codec: "avc1.42800D",
    encoding: "H264",
    clock_rate: 45000,
    payload_type: 103,
    fmtp: "profile-level-id=42800d;packetization-mode=1;max-fs=396",
    profile_level_id: "42800d",
    packetization_mode: 1,
    format: "pt=103:H264/45000",
  }},
  receive: {{
    codec: "avc1.64001F",
    encoding: "H264",
    clock_rate: 90000,
    payload_type: 121,
    fmtp: "profile-level-id=64001f;packetization-mode=1",
    profile_level_id: "64001f",
    packetization_mode: 1,
    format: "pt=121:H264/90000",
  }},
}};

// The nested directional objects win over legacy flat RX aliases, including
// PT and clock. The browser itself does not packetize RTP, but exposing the
// negotiated PTs in state makes diagnostics verify the backend contract.
const media = new Video();
media._negotiated = asymmetric;
media._updateLegacyMediaAliases(asymmetric);
assert.deepEqual(
  [media._mediaContract("send").codec, media._mediaContract("send").clockRate,
    media._mediaContract("send").payloadType],
  ["avc1.42800D", 45000, 103],
);
assert.deepEqual(
  [media._mediaContract("receive").codec, media._mediaContract("receive").clockRate,
    media._mediaContract("receive").payloadType],
  ["avc1.64001F", 90000, 121],
);
let emitted;
media.addEventListener("state", (event) => {{ emitted = event.detail; }});
media._emit();
assert.deepEqual(
  [emitted.send_encoding, emitted.receive_encoding,
    emitted.send_clock_rate, emitted.receive_clock_rate,
    emitted.send_payload_type, emitted.receive_payload_type],
  ["H264", "H264", 45000, 90000, 103, 121],
);

// TX access-unit timestamps use the sender clock; RX timestamp unwrapping
// independently uses the decoder clock.
const wire = [];
media._ws = {{ readyState: 1, bufferedAmount: 0, send(value) {{ wire.push(value); }} }};
media._sendEncodedChunk({{
  type: "key",
  byteLength: 2,
  timestamp: 1000000,
  copyTo(target) {{ target.set([1, 2]); }},
}});
assert.equal(new DataView(wire.at(-1).buffer).getUint32(2, false), 45000);
assert.deepEqual(
  [0, 9000].map((value) => media._unwrapRtpTimestamp(value)),
  [0, 100000],
);

// H.264 level asymmetry is directional: camera constraints follow TX level
// 1.3 even though the decoder may accept High Profile Level 3.1.
const capture = media._cameraCaptureContract();
assert.equal(capture.maxFs, 396);
assert.deepEqual([capture.maxWidth, capture.maxHeight], [352, 288]);

// Codec setup probes/configures the decoder with RX and the encoder with TX.
const decoderConfigs = [];
class TestDecoder {{
  static async isConfigSupported(config) {{
    decoderConfigs.push(config);
    return {{ supported: true, config }};
  }}
  constructor(init) {{ this.init = init; this.state = "unconfigured"; this.decodeQueueSize = 0; }}
  configure(config) {{ this.config = config; this.state = "configured"; }}
  close() {{ this.state = "closed"; }}
}}
context.VideoDecoder = TestDecoder;
const codecs = new Video();
codecs._generation = 7;
codecs._cameraEnabled = true;
codecs._negotiated = asymmetric;
codecs._updateLegacyMediaAliases(asymmetric);
const senderCodecs = [];
codecs._ensureSender = async (codec) => {{
  senderCodecs.push(codec);
  codecs._encoder = {{ state: "configured" }};
}};
await codecs._setupCodecs(asymmetric, 7);
await Promise.resolve();
assert.equal(decoderConfigs.at(-1).codec, "avc1.64001F");
assert.deepEqual(senderCodecs, ["avc1.42800D"]);
assert.equal(codecs._canReceive, true);
assert.equal(codecs._canSend, true);

// Decoder dispatch follows RX encoding even when the camera sends a different
// codec. This also covers the direct JPEG browser path.
const splitEncoding = new Video();
splitEncoding._negotiated = {{
  can_send: true,
  can_receive: true,
  send: {{ codec: "vp8", encoding: "VP8", clock_rate: 90000, payload_type: 104 }},
  receive: {{ codec: "jpeg", encoding: "JPEG", clock_rate: 90000, payload_type: 26 }},
}};
let jpegCalls = 0;
splitEncoding._decodeJpegMessage = () => {{ jpegCalls++; }};
splitEncoding._decodeMessage(new ArrayBuffer(8));
assert.equal(jpegCalls, 1);
assert.equal(splitEncoding._mediaContract("send").encoding, "VP8");

// Hold/resume media_update retains the exact directional codec contract. A
// temporary can_send=false must not substitute RX codec/constraints into TX.
const updates = new Video();
const updateWs = {{ readyState: 1 }};
updates._ws = updateWs;
updates._callId = "hold-resume";
updates._cleanupSender = async function() {{ this._canSend = false; }};
updates._cleanupReceiver = function() {{ this._canReceive = false; }};
const applied = [];
updates._setupCodecs = async function(payload) {{
  applied.push({{
    send: this._mediaContract("send", payload),
    receive: this._mediaContract("receive", payload),
    canSend: payload.can_send,
    canReceive: payload.can_receive,
  }});
  this._cameraAllowed = Boolean(payload.can_send);
  this._canReceive = Boolean(payload.can_receive);
}};
await updates._applyMediaUpdate({{ ...asymmetric, can_send: false }}, updateWs, "hold-resume");
assert.equal(updates.canSend, false);
await updates._applyMediaUpdate({{ ...asymmetric, can_send: true }}, updateWs, "hold-resume");
assert.equal(updates.canSend, true);
assert.deepEqual(applied.map((item) => [
  item.send.codec,
  item.receive.codec,
  item.send.payloadType,
  item.receive.payloadType,
]), [
  ["avc1.42800D", "avc1.64001F", 103, 121],
  ["avc1.42800D", "avc1.64001F", 103, 121],
]);

// Legacy flat payloads remain a symmetric contract for both paths.
const legacy = new Video();
legacy._negotiated = {{
  codec: "vp8",
  encoding: "VP8",
  clock_rate: 90000,
  payload_type: 104,
  fmtp: "max-fr=12;max-fs=1200",
  can_send: true,
  can_receive: true,
}};
assert.deepEqual(legacy._mediaContract("send"), legacy._mediaContract("receive"));
assert.deepEqual(
  [legacy._mediaContract("send").codec, legacy._mediaContract("send").payloadType],
  ["vp8", 104],
);
assert.deepEqual(
  [legacy._cameraCaptureContract().maxWidth, legacy._cameraCaptureContract().maxFr],
  [640, 12],
);
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
        timeout=10,
    )
    assert completed.returncode == 0, completed.stderr or completed.stdout
