const HA_SOFTPHONE_DEVICE_ID = "__voip_stack_ha_softphone__";
const WS_AUDIO = 1;
const WS_SUBSCRIBE_CALL_EVENTS = "voip_stack/subscribe_call_events";
const WS_SUBSCRIBE_HA_SOFTPHONE = "voip_stack/subscribe_ha_softphone_state";
const MODULE_VERSION = (() => {
  try {
    return new URL(import.meta.url).searchParams.get("v") || "dev";
  } catch (_) {
    return "dev";
  }
})();
const { RINGTONE_REPEAT_MS, playVoipRingtone } =
  await import(`./ringtone.js?v=${encodeURIComponent(MODULE_VERSION)}`);
const CONTROL_ACK_TIMEOUT_MS = 3000;
const AUDIO_NEGOTIATION_TIMEOUT_MS = 3000;
const BUS_SUBSCRIBE_RETRY_MS = 2000;
const SOFTPHONE_MEDIA_SESSION_KEY = "voip_stack_owned_softphone_call";
const MEDIA_CLIENT_SESSION_KEY = "voip_stack_media_client_id";
const VIDEO_CAMERA_STORAGE_KEY = "voip_stack_video_camera_enabled";
const MAX_AUDIO_WS_BUFFER_MS = 120;
const MIN_AUDIO_WS_BUFFER_FRAMES = 4;
const PCM_FORMATS = Object.freeze(["s16le", "s24le", "s24le_in_s32", "s32le"]);
const FRAME_MS = Object.freeze([10, 16, 20, 32]);

function mediaClientInstanceId() {
  try {
    const existing = sessionStorage.getItem(MEDIA_CLIENT_SESSION_KEY) || "";
    if (/^[A-Za-z0-9][A-Za-z0-9._~-]{15,127}$/.test(existing)) return existing;
  } catch (_) {}
  let generated = "";
  try {
    generated = globalThis.crypto?.randomUUID?.() || "";
  } catch (_) {}
  if (!generated) {
    // This token is collision resistance, not authentication: Home
    // Assistant signs the complete path including it before WebSocket use.
    generated = `tab-${Date.now().toString(36)}-${Math.random().toString(36).slice(2)}-${Math.random().toString(36).slice(2)}`;
  }
  try { sessionStorage.setItem(MEDIA_CLIENT_SESSION_KEY, generated); } catch (_) {}
  return generated;
}

class VoipStackEngine extends EventTarget {
  constructor() {
    super();
    this._hass = null;
    this._ws = null;
    this._state = "IDLE";
    this._deviceId = "";
    this._callId = "";
    this._audioMode = "full_duplex";
    this._audioDirection = "sendrecv";
    this._txFormat = null;
    this._rxFormat = null;
    this._lastSessionPayload = null;
    this._audioReady = false;
    this._audioSetupGeneration = 0;
    this._mediaStream = null;
    this._audioContext = null;
    this._captureNode = null;
    this._captureSink = null;
    this._source = null;
    this._playbackNode = null;
    this._stats = { sent: 0, received: 0, tx_dropped: 0, buffered_frames: 0, frames_drop: 0, underruns: 0 };
    this._busConnection = null;
    this._busUnsub = null;
    this._softphoneBusUnsub = null;
    this._busSubscribePending = false;
    this._softphoneBusSubscribePending = false;
    this._busSubscribeRetryTimer = null;
    this._callSubscribers = new Set();
    this._softphoneSubscribers = new Set();
    this._lastEvents = new Map();
    this._lastSoftphoneState = null;
    this._controlWaiter = null;
    this._connectPromise = null;
    this._connectGeneration = 0;
    this._sessionAttachKey = "";
    this._sessionAttachPromise = null;
    this._mediaClientId = mediaClientInstanceId();
    // Media ownership belongs to the page-level engine, not to one Lovelace
    // element. Home Assistant may recreate a card while an outbound call is
    // ringing; the replacement must still be able to attach that call's media.
    try {
      this._ownedSoftphoneCallId = sessionStorage.getItem(SOFTPHONE_MEDIA_SESSION_KEY) || "";
    } catch (_) {
      this._ownedSoftphoneCallId = "";
    }
    this._ringtoneRequests = new Map();
    this._ringtoneContext = null;
    this._ringtoneTimer = null;
    this._audioFrameBuffer = null;
    this._video = null;
    this._videoLoadPromise = null;
    this._videoCanvas = null;
    this._videoCanvasOwner = null;
    this._softphoneController = null;
    this._videoAttachGeneration = 0;
    this._videoAttachPromise = null;
    this._videoAttachCallId = "";

    window.addEventListener("pagehide", () => {
      this._ringtoneRequests.clear();
      this._stopRingtone();
      void this.close("pagehide");
    });
  }

  configure(hass) {
    this._hass = hass;
    if (this._video) this._video.configure(hass, this._mediaClientId);
    const conn = hass?.connection || null;
    if (!conn) return;
    if (conn !== this._busConnection) {
      if (this._busUnsub) this._busUnsub();
      if (this._softphoneBusUnsub) this._softphoneBusUnsub();
      this._busUnsub = null;
      this._softphoneBusUnsub = null;
      this._busSubscribePending = false;
      this._softphoneBusSubscribePending = false;
      if (this._busSubscribeRetryTimer) clearTimeout(this._busSubscribeRetryTimer);
      this._busSubscribeRetryTimer = null;
      this._busConnection = conn;
    }
    this._ensureBusSubscriptions(conn);
  }

  _scheduleBusSubscriptionRetry(conn) {
    if (this._busConnection !== conn || this._busSubscribeRetryTimer) return;
    this._busSubscribeRetryTimer = setTimeout(() => {
      this._busSubscribeRetryTimer = null;
      if (this._busConnection === conn) this._ensureBusSubscriptions(conn);
    }, BUS_SUBSCRIBE_RETRY_MS);
  }

  _ensureBusSubscriptions(conn) {
    if (this._busConnection !== conn) return;
    if (!this._busUnsub && !this._busSubscribePending) {
      this._busSubscribePending = true;
      conn.subscribeMessage((event) => this._onBusEvent(event), { type: WS_SUBSCRIBE_CALL_EVENTS })
      .then((unsub) => {
        if (this._busConnection === conn) this._busUnsub = unsub;
        else unsub();
      })
      .catch((err) => {
        console.warn("voip-stack-engine: call_event subscription failed", err);
        this._scheduleBusSubscriptionRetry(conn);
      })
      .finally(() => {
        if (this._busConnection === conn) this._busSubscribePending = false;
      });
    }
    if (!this._softphoneBusUnsub && !this._softphoneBusSubscribePending) {
      this._softphoneBusSubscribePending = true;
      conn.subscribeMessage(
        (event) => this._onSoftphoneState(event),
        { type: WS_SUBSCRIBE_HA_SOFTPHONE },
      ).then((unsub) => {
        if (this._busConnection === conn) this._softphoneBusUnsub = unsub;
        else unsub();
      }).catch((err) => {
        console.warn("voip-stack-engine: HA softphone subscription failed", err);
        this._scheduleBusSubscriptionRetry(conn);
      }).finally(() => {
        if (this._busConnection === conn) this._softphoneBusSubscribePending = false;
      });
    }
  }

  get active() {
    return this._state !== "IDLE";
  }

  get deviceId() {
    return this._deviceId;
  }

  get callId() {
    return this._callId;
  }

  claimSoftphoneSession(callId) {
    this._ownedSoftphoneCallId = String(callId || "");
    try {
      if (this._ownedSoftphoneCallId) {
        sessionStorage.setItem(SOFTPHONE_MEDIA_SESSION_KEY, this._ownedSoftphoneCallId);
      } else {
        sessionStorage.removeItem(SOFTPHONE_MEDIA_SESSION_KEY);
      }
    } catch (_) {}
  }

  ownsSoftphoneSession(callId) {
    const wanted = String(callId || "");
    return !!wanted && wanted === this._ownedSoftphoneCallId;
  }

  get softphoneCallId() {
    return this._ownedSoftphoneCallId;
  }

  releaseSoftphoneSession(callId = "") {
    const wanted = String(callId || "");
    if (!wanted || wanted === this._ownedSoftphoneCallId) {
      this._ownedSoftphoneCallId = "";
      try { sessionStorage.removeItem(SOFTPHONE_MEDIA_SESSION_KEY); } catch (_) {}
    }
  }

  claimSoftphoneController(owner) {
    if (!owner || owner.isConnected === false) return false;
    if (this._softphoneController && this._softphoneController !== owner) return false;
    if (!this._softphoneController) this._softphoneController = owner;
    return true;
  }

  releaseSoftphoneController(owner) {
    if (!owner || this._softphoneController !== owner) return false;
    this._softphoneController = null;
    this._emit();
    return true;
  }

  get stats() {
    return { ...this._stats, video: this._video?.stats || {} };
  }

  get videoActive() {
    return Boolean(this._video?.active);
  }

  get videoVisible() {
    return Boolean(this._video?.visible);
  }

  setVideoCanvas(canvas) {
    this._videoCanvas = canvas || null;
    if (this._video) this._video.setCanvas(this._videoCanvas);
  }

  claimVideoCanvas(owner, canvas) {
    if (!owner || owner.isConnected === false || !canvas) return false;
    if (this._videoCanvasOwner && this._videoCanvasOwner !== owner) return false;
    this._videoCanvasOwner = owner;
    this.setVideoCanvas(canvas);
    return true;
  }

  releaseVideoCanvas(owner) {
    if (!owner || this._videoCanvasOwner !== owner) return false;
    this._videoCanvasOwner = null;
    this.setVideoCanvas(null);
    return true;
  }

  get videoCanSend() {
    return Boolean(this._video?.canSend);
  }

  get videoCameraEnabled() {
    if (this._video) return Boolean(this._video.cameraEnabled);
    try { return localStorage.getItem(VIDEO_CAMERA_STORAGE_KEY) === "true"; }
    catch (_) { return false; }
  }

  async setVideoCameraEnabled(enabled) {
    const video = await this._loadVideo();
    await video.setCameraEnabled(enabled);
  }

  async prepareVideoCameraPermission({ persistentOnly = false } = {}) {
    if (!this.videoCameraEnabled || !navigator.mediaDevices?.getUserMedia) return false;
    if (persistentOnly) {
      if (!navigator.permissions?.query) return false;
      try {
        const permission = await navigator.permissions.query({ name: "camera" });
        if (permission.state !== "granted") return false;
      } catch (_) {
        return false;
      }
    }
    let stream = null;
    try {
      stream = await navigator.mediaDevices.getUserMedia({
        video: {
          width: { ideal: 640, max: 1280 },
          height: { ideal: 360, max: 720 },
          frameRate: { ideal: 15, max: 20 },
        },
        audio: false,
      });
      return Boolean(stream?.getVideoTracks?.()[0]);
    } catch (err) {
      console.warn("voip-stack-engine: camera permission unavailable; continuing receive-only", err);
      return false;
    } finally {
      for (const track of stream?.getTracks?.() || []) track.stop();
    }
  }

  async _loadVideo() {
    if (this._video) return this._video;
    if (!this._videoLoadPromise) {
      this._videoLoadPromise = import(`./voip-stack-video.js?v=${encodeURIComponent(MODULE_VERSION)}`)
        .then(({ VoipStackVideo }) => {
          const video = new VoipStackVideo();
          if (this._hass) video.configure(this._hass, this._mediaClientId);
          video.setCanvas(this._videoCanvas);
          video.addEventListener("state", () => this._emit());
          this._video = video;
          return video;
        })
        .finally(() => { this._videoLoadPromise = null; });
    }
    return this._videoLoadPromise;
  }

  statsText() {
    if (!this.active) return "";
    const video = this._video?.active
      ? ` | Video TX: ${this._video.stats.sent} RX: ${this._video.stats.received} Render: ${this._video.stats.rendered || 0} Drop: ${this._video.stats.dropped} Gap: ${Math.round(this._video.stats.max_frame_gap_ms || 0)}ms Src: ${Math.round(this._video.stats.max_source_gap_ms || 0)}ms Arr: ${Math.round(this._video.stats.max_arrival_gap_ms || 0)}ms Playout: ${Math.round(this._video.stats.playout_ms || 0)}ms`
      : "";
    return `Sent: ${this._stats.sent} | Recv: ${this._stats.received} | TxDrop: ${this._stats.tx_dropped || 0} | Buf: ${this._stats.buffered_frames} | Und: ${this._stats.underruns || 0}${video}`;
  }

  _emit() {
    this.dispatchEvent(new CustomEvent("state", {
      detail: {
        state: this._state,
        device_id: this._deviceId,
        stats: { ...this._stats },
      },
    }));
  }

  _setState(state) {
    const target = String(state || "").toUpperCase();
    if (target === this._state) return;
    this._state = target;
    this._emit();
  }

  _forceIdle() {
    this._state = "IDLE";
    this._emit();
  }

  _onBusEvent(event) {
    const data = event?.data;
    if (!data) return;
    const scope = (data.scope || "").toLowerCase();
    for (const id of [data.device_id, data.source_device_id, data.dest_device_id, data.session_device_id]) {
      if (!id) continue;
      const key = `${id}|${scope}`;
      if (!this._lastEvents.has(key) && this._lastEvents.size >= 256) {
        this._lastEvents.delete(this._lastEvents.keys().next().value);
      }
      this._lastEvents.set(key, event);
    }
    for (const cb of this._callSubscribers) {
      try { cb(event); } catch (err) { console.error("voip-stack-engine subscriber", err); }
    }
  }

  subscribeCallEvents(cb) {
    this._callSubscribers.add(cb);
    for (const event of this._lastEvents.values()) {
      try { cb(event); } catch (err) { console.error("voip-stack-engine replay", err); }
    }
    return () => this._callSubscribers.delete(cb);
  }

  _onSoftphoneState(state) {
    if (!state) return;
    this._lastSoftphoneState = state;
    for (const cb of this._softphoneSubscribers) {
      try { cb(state); } catch (err) { console.error("voip-stack-engine softphone subscriber", err); }
    }
  }

  subscribeSoftphoneState(cb) {
    this._softphoneSubscribers.add(cb);
    if (this._lastSoftphoneState) {
      try { cb(this._lastSoftphoneState); } catch (err) { console.error("voip-stack-engine softphone replay", err); }
    }
    return () => this._softphoneSubscribers.delete(cb);
  }

  setRingtoneRequest(key, active, enabled) {
    if (!key) return;
    const shouldRing = !!active && !!enabled;
    if (shouldRing) this._ringtoneRequests.set(key, true);
    else this._ringtoneRequests.delete(key);
    this._syncRingtone();
  }

  clearRingtoneRequest(key) {
    if (!key) return;
    this._ringtoneRequests.delete(key);
    this._syncRingtone();
  }

  unlockRingtone() {
    this._ensureRingtoneContext()
      ?.resume()
      .catch((err) => console.warn("voip-stack-engine: ringtone unlock failed", err));
  }

  _syncRingtone() {
    if (this._ringtoneRequests.size > 0) this._startRingtone();
    else this._stopRingtone();
  }

  _ensureRingtoneContext() {
    if (this._ringtoneContext) return this._ringtoneContext;
    const Ctor = window.AudioContext || window.webkitAudioContext;
    if (!Ctor) return null;
    this._ringtoneContext = new Ctor();
    return this._ringtoneContext;
  }

  _startRingtone() {
    if (this._ringtoneTimer !== null) return;
    const ctx = this._ensureRingtoneContext();
    if (!ctx) return;
    ctx.resume().catch((err) => console.warn("voip-stack-engine: ringtone resume failed", err));
    const tick = () => playVoipRingtone(ctx);
    tick();
    this._ringtoneTimer = window.setInterval(tick, RINGTONE_REPEAT_MS);
  }

  _stopRingtone() {
    if (this._ringtoneTimer !== null) {
      window.clearInterval(this._ringtoneTimer);
      this._ringtoneTimer = null;
    }
  }

  async _wsUrl(deviceId, callId) {
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const path = `/api/voip_stack/ws?device_id=${encodeURIComponent(deviceId)}&call_id=${encodeURIComponent(callId)}&client_id=${encodeURIComponent(this._mediaClientId)}`;
    const signed = await this._hass.callWS({ type: "auth/sign_path", path });
    return `${proto}//${window.location.host}${signed.path || path}`;
  }

  async _connect(deviceId, callId = "") {
    const wantedCallId = String(callId || "");
    if (
      this._ws &&
      this._deviceId === deviceId &&
      this._callId === wantedCallId &&
      this._ws.readyState === WebSocket.OPEN
    ) return this._lastSessionPayload;
    if (
      this._connectPromise &&
      this._deviceId === deviceId &&
      this._callId === wantedCallId &&
      this._ws &&
      this._ws.readyState === WebSocket.CONNECTING
    ) {
      return this._connectPromise;
    }
    const connectGeneration = ++this._connectGeneration;
    await this.close("switch", true, true);
    if (connectGeneration !== this._connectGeneration) {
      throw new Error("Audio WebSocket superseded before connect");
    }
    this._deviceId = deviceId;
    this._callId = wantedCallId;
    this._lastSessionPayload = null;
    const wsUrl = await this._wsUrl(deviceId, wantedCallId);
    if (
      connectGeneration !== this._connectGeneration ||
      this._deviceId !== deviceId ||
      this._callId !== wantedCallId
    ) {
      throw new Error("Audio WebSocket superseded before connect");
    }
    const ws = new WebSocket(wsUrl);
    this._ws = ws;
    ws.binaryType = "arraybuffer";
    let helloResolve;
    let helloReject;
    let helloSettled = false;
    const settleHello = (method, value) => {
      if (helloSettled) return;
      helloSettled = true;
      method(value);
    };
    const hello = new Promise((resolve, reject) => {
      helloResolve = resolve;
      helloReject = reject;
    });
    // onclose can reject this before OPEN has completed. Observe it now so a
    // losing connect generation never creates an unhandled rejection.
    void hello.catch(() => {});
    ws.onmessage = (event) => {
      if (this._ws !== ws) return;
      if (typeof event.data === "string") {
        try {
          const payload = JSON.parse(event.data);
          if (payload?.error) {
            settleHello(helloReject, new Error(payload.error));
          } else if (payload?.tx_format || payload?.selected_tx_format) {
            settleHello(helloResolve, payload);
          }
        } catch (_) {}
      }
      this._handleMessage(event);
    };
    let opened = false;
    const openedPromise = new Promise((resolve, reject) => {
      ws.onopen = () => {
        opened = true;
        if (this._ws === ws) resolve();
        else {
          try { ws.close(); } catch (_) {}
          reject(new Error("Audio WebSocket superseded"));
        }
      };
      ws.onerror = () => {
        const error = new Error("Audio WebSocket failed");
        settleHello(helloReject, error);
        reject(error);
      };
      ws.onclose = () => {
        const error = new Error(
          opened
            ? "Audio WebSocket closed before negotiation"
            : "Audio WebSocket closed before opening",
        );
        settleHello(helloReject, error);
        if (!opened) reject(error);
        if (this._ws !== ws) return;
        this._ws = null;
        void this._cleanupAudio("ws_close");
      };
    });
    const connectPromise = (async () => {
      await openedPromise;
      const negotiated = await Promise.race([
        hello,
        new Promise((_, reject) => window.setTimeout(
          () => reject(new Error("Audio WebSocket negotiation timed out")),
          AUDIO_NEGOTIATION_TIMEOUT_MS,
        )),
      ]);
      if (
        connectGeneration !== this._connectGeneration ||
        this._ws !== ws ||
        this._deviceId !== deviceId ||
        this._callId !== wantedCallId
      ) {
        throw new Error("Audio WebSocket superseded during negotiation");
      }
      return negotiated;
    })();
    this._connectPromise = connectPromise;
    try {
      return await connectPromise;
    } catch (err) {
      if (this._ws === ws) {
        this._ws = null;
        this._callId = "";
        try { ws.close(); } catch (_) {}
      }
      throw err;
    } finally {
      if (this._connectPromise === connectPromise) this._connectPromise = null;
    }
  }

  _sendControl(payload, waitForReply = false, acceptReply = null) {
    if (!this._ws || this._ws.readyState !== WebSocket.OPEN) {
      return waitForReply ? Promise.resolve(null) : null;
    }
    if (!waitForReply) {
      this._ws.send(JSON.stringify(payload));
      return null;
    }
    if (this._controlWaiter) {
      this._controlWaiter.resolve(null);
      this._controlWaiter = null;
    }
    const promise = new Promise((resolve) => {
      const timer = window.setTimeout(() => {
        if (this._controlWaiter?.resolve === resolve) this._controlWaiter = null;
        resolve(null);
      }, CONTROL_ACK_TIMEOUT_MS);
      this._controlWaiter = { resolve, timer, acceptReply };
    });
    this._ws.send(JSON.stringify(payload));
    return promise;
  }

  _resolveControlWaiter(msg) {
    if (!this._controlWaiter) return;
    const waiter = this._controlWaiter;
    if (waiter.acceptReply && !waiter.acceptReply(msg)) return;
    this._controlWaiter = null;
    window.clearTimeout(waiter.timer);
    waiter.resolve(msg);
  }

  _isTerminalControlReply(msg) {
    return !!msg?.error || ["in_call", "idle", "error"].includes(String(msg?.state || "").toLowerCase());
  }

  _sendAudio(buffer) {
    if (!this._ws || this._ws.readyState !== WebSocket.OPEN) return;
    if (!this._canSendAudio()) return;
    const bytes = new Uint8Array(buffer);
    if (!bytes.byteLength) return;
    const bytesPerSample = this._txFormat?.pcmFormat === "s16le" ? 2 :
      this._txFormat?.pcmFormat === "s24le" ? 3 : 4;
    const bytesPerSecond = Number(this._txFormat?.sampleRate || 0) *
      Number(this._txFormat?.channels || 0) * bytesPerSample;
    const maxBufferedBytes = Math.max(
      bytes.byteLength * MIN_AUDIO_WS_BUFFER_FRAMES,
      Math.ceil(bytesPerSecond * MAX_AUDIO_WS_BUFFER_MS / 1000),
    );
    if (this._ws.bufferedAmount >= maxBufferedBytes) {
      this._stats.tx_dropped++;
      if ((this._stats.tx_dropped & 31) === 1) this._emit();
      return;
    }
    if (!this._audioFrameBuffer || this._audioFrameBuffer.byteLength !== bytes.byteLength + 1) {
      this._audioFrameBuffer = new Uint8Array(bytes.byteLength + 1);
    }
    const frame = this._audioFrameBuffer;
    frame[0] = WS_AUDIO;
    frame.set(bytes, 1);
    this._ws.send(frame);
    this._stats.sent++;
    if ((this._stats.sent & 31) === 0) this._emit();
  }

  _handleMessage(event) {
    if (typeof event.data === "string") {
      try {
        const msg = JSON.parse(event.data);
        if (msg.tx_format || msg.rx_format || msg.audio_direction) {
          this._lastSessionPayload = { ...(this._lastSessionPayload || {}), ...msg };
        }
        if (msg.audio_direction) {
          if (this._audioReady) void this._reconcileAudioMedia(msg);
          else this._audioDirection = this._normaliseAudioDirection(msg.audio_direction);
        } else if (this._audioReady && (msg.tx_format || msg.rx_format)) {
          void this._reconcileAudioMedia(msg);
        }
        if (msg.state) this._setState(String(msg.state).toUpperCase());
        if (msg.error) this.dispatchEvent(new CustomEvent("error", { detail: msg.error }));
        this._resolveControlWaiter(msg);
      } catch (_) {}
      return;
    }
    const raw = new Uint8Array(event.data);
    if (
      raw[0] !== WS_AUDIO ||
      raw.byteLength < 2 ||
      !this._playbackNode ||
      !this._canReceiveAudio()
    ) return;
    this._playbackNode.port.postMessage({ type: "audio", buffer: event.data, byteOffset: 1 }, [event.data]);
    this._stats.received++;
    if ((this._stats.received & 31) === 0) this._emit();
  }

  _createAudioContext() {
    const Ctor = window.AudioContext || window.webkitAudioContext;
    return new Ctor();
  }

  _parseFormat(token, label = "audio format") {
    const parts = String(token || "").split(":");
    if (parts.length !== 4) throw new Error(`${label} missing negotiated PCM token`);
    const sampleRate = Number(parts[0]);
    const pcmFormat = parts[1];
    const channels = Number(parts[2]);
    const frameMs = Number(parts[3]);
    if (!Number.isFinite(sampleRate) || !Number.isFinite(channels) || !Number.isFinite(frameMs)) {
      throw new Error(`${label} has invalid numeric fields`);
    }
    if (!PCM_FORMATS.includes(pcmFormat)) throw new Error(`${label} has unsupported PCM format ${pcmFormat}`);
    if (![1, 2].includes(channels)) throw new Error(`${label} has unsupported channel count ${channels}`);
    if (!FRAME_MS.includes(frameMs)) throw new Error(`${label} has unsupported frame_ms ${frameMs}`);
    if ((sampleRate * frameMs) % 1000 !== 0) throw new Error(`${label} does not form whole PCM frames`);
    return { sampleRate, pcmFormat, channels, frameMs };
  }

  _resolveSessionFormats(negotiated = null) {
    const txFormat = negotiated?.selected_tx_format || negotiated?.tx_format;
    const rxFormat = negotiated?.selected_rx_format || negotiated?.rx_format;
    if (!txFormat || !rxFormat) {
      throw new Error("SIP session missing selected_tx_format/selected_rx_format");
    }
    return {
      tx: this._parseFormat(txFormat, "selected_tx_format"),
      rx: this._parseFormat(rxFormat, "selected_rx_format"),
    };
  }

  async _setupAudio(deviceInfo, negotiated = null, attachKey = "") {
    const audioMode = this._normaliseAudioMode(deviceInfo?.audio_mode);
    const audioDirection = this._normaliseAudioDirection(negotiated?.audio_direction);
    const formats = this._resolveSessionFormats(negotiated);
    const { capture, playback } = this._desiredAudioPaths(audioMode, audioDirection);
    const setupGeneration = ++this._audioSetupGeneration;
    const expectedCallId = this._callId;
    const resources = {
      audioContext: null,
      mediaStream: null,
      captureNode: null,
      captureSink: null,
      source: null,
      playbackNode: null,
    };
    const assertCurrent = () => {
      if (
        setupGeneration !== this._audioSetupGeneration ||
        this._callId !== expectedCallId
      ) {
        throw new Error("Audio setup superseded");
      }
      if (attachKey && this._sessionAttachKey !== attachKey) {
        throw new Error("Audio setup superseded");
      }
    };

    try {
      assertCurrent();
      if (capture || playback) {
        resources.audioContext = this._createAudioContext();
        if (resources.audioContext.state === "suspended") {
          await resources.audioContext.resume();
          assertCurrent();
        }
      }

      if (capture) {
        resources.mediaStream = await navigator.mediaDevices.getUserMedia({
          audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
        });
        assertCurrent();
        await resources.audioContext.audioWorklet.addModule(
          `/voip-stack/voip-stack-processor.js?v=${encodeURIComponent(MODULE_VERSION)}`,
        );
        assertCurrent();
        resources.source = resources.audioContext.createMediaStreamSource(resources.mediaStream);
        resources.captureNode = new AudioWorkletNode(resources.audioContext, "voip-stack-processor", {
          processorOptions: { format: formats.tx },
        });
        resources.captureNode.port.onmessage = (event) => {
          if (
            this._captureNode === resources.captureNode &&
            event.data?.type === "audio"
          ) this._sendAudio(event.data.buffer);
        };
        resources.source.connect(resources.captureNode);
        resources.captureSink = resources.audioContext.createGain();
        resources.captureSink.gain.value = 0;
        resources.captureNode
          .connect(resources.captureSink)
          .connect(resources.audioContext.destination);
      }

      if (playback) {
        await resources.audioContext.audioWorklet.addModule(
          `/voip-stack/voip-stack-playback-processor.js?v=${encodeURIComponent(MODULE_VERSION)}`,
        );
        assertCurrent();
        resources.playbackNode = new AudioWorkletNode(
          resources.audioContext,
          "voip-stack-playback-processor",
          {
            outputChannelCount: [formats.rx.channels],
            processorOptions: { format: formats.rx },
          },
        );
        resources.playbackNode.port.onmessage = (event) => {
          if (
            this._playbackNode !== resources.playbackNode ||
            event.data?.type !== "stats"
          ) return;
          this._stats = { ...this._stats, ...event.data };
          this._emit();
        };
        resources.playbackNode.connect(resources.audioContext.destination);
      }
      assertCurrent();
      const previous = this._takeAudioResources();
      this._audioMode = audioMode;
      this._audioDirection = audioDirection;
      this._txFormat = formats.tx;
      this._rxFormat = formats.rx;
      this._audioContext = resources.audioContext;
      this._mediaStream = resources.mediaStream;
      this._captureNode = resources.captureNode;
      this._captureSink = resources.captureSink;
      this._source = resources.source;
      this._playbackNode = resources.playbackNode;
      this._audioReady = true;
      this._applyAudioDirection(audioDirection);
      await this._disposeAudioResources(previous);
    } catch (err) {
      await this._disposeAudioResources(resources);
      throw err;
    }
  }

  _normaliseAudioMode(value) {
    const v = String(value || "").trim().toLowerCase();
    return ["full_duplex", "mic_only", "speaker_only", "control_only"].includes(v) ? v : "full_duplex";
  }

  _normaliseAudioDirection(value) {
    const direction = String(value || "sendrecv").trim().toLowerCase();
    return ["sendrecv", "sendonly", "recvonly", "inactive"].includes(direction)
      ? direction
      : "sendrecv";
  }

  _desiredAudioPaths(audioMode, audioDirection) {
    const mode = this._normaliseAudioMode(audioMode);
    const direction = this._normaliseAudioDirection(audioDirection);
    const modeCanCapture = mode === "full_duplex" || mode === "speaker_only";
    const modeCanPlayback = mode === "full_duplex" || mode === "mic_only";
    return {
      capture: modeCanCapture && ["sendrecv", "sendonly"].includes(direction),
      playback: modeCanPlayback && ["sendrecv", "recvonly"].includes(direction),
    };
  }

  _sameAudioFormat(left, right) {
    return Boolean(left && right) &&
      left.sampleRate === right.sampleRate &&
      left.pcmFormat === right.pcmFormat &&
      left.channels === right.channels &&
      left.frameMs === right.frameMs;
  }

  async _reconcileAudioMedia(update = {}) {
    if (!this._audioReady || !this._callId) return;
    const negotiated = { ...(this._lastSessionPayload || {}), ...(update || {}) };
    if (update?.tx_format || update?.rx_format) this._lastSessionPayload = negotiated;
    const direction = this._normaliseAudioDirection(
      negotiated.audio_direction || this._audioDirection,
    );
    const formats = this._resolveSessionFormats(negotiated);
    const desired = this._desiredAudioPaths(this._audioMode, direction);
    if (
      this._sameAudioFormat(formats.tx, this._txFormat) &&
      this._sameAudioFormat(formats.rx, this._rxFormat) &&
      desired.capture === Boolean(this._captureNode) &&
      desired.playback === Boolean(this._playbackNode)
    ) {
      this._applyAudioDirection(direction);
      return;
    }
    try {
      await this._setupAudio(
        { audio_mode: this._audioMode },
        { ...negotiated, audio_direction: direction },
      );
    } catch (err) {
      if (String(err?.message || err).includes("superseded")) return;
      console.warn("voip-stack-engine: audio media update failed", err);
      this.dispatchEvent(new CustomEvent("error", {
        detail: `Audio media update failed: ${err?.message || String(err)}`,
      }));
    }
  }

  _canSendAudio() {
    return ["sendrecv", "sendonly"].includes(this._audioDirection);
  }

  _canReceiveAudio() {
    return ["sendrecv", "recvonly"].includes(this._audioDirection);
  }

  _applyAudioDirection(value) {
    this._audioDirection = this._normaliseAudioDirection(value);
    const enabled = this._canSendAudio();
    for (const track of this._mediaStream?.getAudioTracks?.() || []) track.enabled = enabled;
    this._emit();
  }

  async _setupAudioOrAbort(deviceId, deviceInfo, reply, attachKey = "") {
    let connected = false;
    const callId = String(reply?.call_id || "");
    try {
      const negotiated = await this._connect(deviceId, callId);
      connected = true;
      await this._setupAudio(
        deviceInfo,
        { ...(reply || {}), ...(negotiated || {}) },
        attachKey,
      );
      if (attachKey && this._sessionAttachKey !== attachKey) {
        return false;
      }
      // A re-INVITE may land while getUserMedia or the worklet module is
      // pending. The WebSocket handler records it while the pipeline is not
      // yet publishable; reconcile once after the atomic initial commit.
      await this._reconcileAudioMedia(this._lastSessionPayload || {});
      if (attachKey && this._sessionAttachKey !== attachKey) return false;
      return true;
    } catch (err) {
      if (attachKey && this._sessionAttachKey !== attachKey) {
        return false;
      }
      console.error("voip-stack-engine: audio setup failed", err);
      this.dispatchEvent(new CustomEvent("error", { detail: err?.message || String(err) }));
      // A failed HTTP/WebSocket ownership claim (for example, a 409 while a
      // newer card takes over) never established a media path and therefore
      // must not terminate the live SIP dialog.  Once the socket opened, a
      // real browser media-format/setup failure is still fatal for this leg.
      if (
        connected &&
        deviceId === HA_SOFTPHONE_DEVICE_ID &&
        this._deviceId === deviceId &&
        this._callId === callId &&
        this._hass
      ) {
        await this._hass.callService("voip_stack", "hangup", {
          call_id: callId,
          reason: "media_incompatible",
        }).catch(() => {});
      }
      await this.close("audio_setup_failed");
      this._setState("ERROR");
      return false;
    }
  }

  async startHaSoftphone(target, softphoneInfo, context = {}) {
    this._resetStats();
    const reply = await this._hass.callWS({
      type: "voip_stack/ha_softphone_start",
      target_name: context.callee || target.name || "",
      callee: context.callee || target.name || "",
      call_id: context.call_id || "",
      send_video: Boolean(context.sendVideo),
    });
    const state = String(reply?.state || "").toLowerCase();
    const callId = String(reply?.call_id || "");
    if (typeof context.shouldAbort === "function" && context.shouldAbort()) {
      if (
        callId &&
        ["calling", "connecting", "remote_ringing", "ringing", "in_call"].includes(state)
      ) {
        await this._hass.callService("voip_stack", "hangup", {
          call_id: callId,
          reason: "superseded",
        }).catch(() => {});
      }
      return { ...(reply || {}), superseded: true };
    }
    if (!["calling", "connecting", "remote_ringing", "ringing", "in_call"].includes(state)) {
      this._setState("IDLE");
      return reply;
    }
    this.claimSoftphoneSession(callId);
    if (state === "in_call") {
      const mediaInfo = {
        ...(softphoneInfo || {}),
        ...(target || {}),
        device_id: HA_SOFTPHONE_DEVICE_ID,
        audio_mode: target?.audio_mode || softphoneInfo?.audio_mode || "full_duplex",
      };
      await this.resumeSession(mediaInfo, HA_SOFTPHONE_DEVICE_ID, reply);
    }
    return reply;
  }

  async resumeSession(deviceInfo, sessionDeviceId, statePayload) {
    const state = String(statePayload?.state || "").toLowerCase();
    if (state !== "in_call") return;
    const deviceId = sessionDeviceId || statePayload?.session_device_id || statePayload?.device_id || this._deviceId;
    if (!deviceId) return;
    const attachKey = `${deviceId}|${statePayload?.call_id || ""}`;
    if (this._sessionAttachPromise && this._sessionAttachKey === attachKey) {
      return this._sessionAttachPromise;
    }
    this._sessionAttachKey = attachKey;
    const attachPromise = (async () => {
      if (this._sessionAttachKey !== attachKey) return;
      return this._resumeSessionLocked(deviceInfo, deviceId, statePayload, attachKey);
    })();
    const trackedPromise = attachPromise.finally(() => {
      if (this._sessionAttachPromise !== trackedPromise) return;
      this._sessionAttachPromise = null;
      if (this._sessionAttachKey === attachKey) this._sessionAttachKey = "";
    });
    this._sessionAttachPromise = trackedPromise;
    return this._sessionAttachPromise;
  }

  async reconcileSession(statePayload) {
    const callId = String(statePayload?.call_id || "");
    if (
      String(statePayload?.state || "").toLowerCase() !== "in_call" ||
      !callId ||
      this._callId !== callId
    ) return;
    if (statePayload.audio_direction) {
      this._lastSessionPayload = { ...(this._lastSessionPayload || {}), ...statePayload };
      if (this._audioReady) await this._reconcileAudioMedia(statePayload);
      else this._audioDirection = this._normaliseAudioDirection(statePayload.audio_direction);
    }
    await this._ensureVideo(statePayload);
  }

  async _resumeSessionLocked(deviceInfo, deviceId, statePayload, attachKey) {
    const callId = String(statePayload?.call_id || "");
    if (
      this._ws &&
      this._deviceId === deviceId &&
      this._callId === callId &&
      this._ws.readyState === WebSocket.OPEN &&
      this._audioReady
    ) {
      this._setState("IN_CALL");
      void this._ensureVideo(statePayload);
      return;
    }
    this._resetStats();
    if (!await this._setupAudioOrAbort(
      deviceId,
      { ...(deviceInfo || {}), device_id: deviceId },
      statePayload,
      attachKey,
    )) return;
    if (this._sessionAttachKey !== attachKey) return;
    this._setState("IN_CALL");
    // Video is optional and directionally independent from the audio call.
    // In particular, a real browser may leave getUserMedia pending while it
    // asks for camera permission. Never make audio attachment or call control
    // wait for that prompt.
    void this._ensureVideo(statePayload);
  }

  async _ensureVideo(statePayload) {
    const wantedCallId = String(statePayload?.call_id || "");
    if (!statePayload?.video_active) {
      if (wantedCallId && this._callId !== wantedCallId) return;
      this._videoAttachGeneration++;
      this._videoAttachPromise = null;
      this._videoAttachCallId = "";
      if (
        this._video &&
        (!wantedCallId || !this._video.callId || this._video.callId === wantedCallId)
      ) await this._video.close();
      return;
    }

    if (!wantedCallId || this._callId !== wantedCallId) return;
    const intentGeneration = this._videoAttachGeneration;

    const video = await this._loadVideo();
    if (
      intentGeneration !== this._videoAttachGeneration ||
      !wantedCallId ||
      this._callId !== wantedCallId
    ) return;

    if (video.active && video.callId === wantedCallId) return;
    if (this._videoAttachPromise && this._videoAttachCallId === wantedCallId) {
      await this._videoAttachPromise;
      return;
    }

    const generation = ++this._videoAttachGeneration;
    this._videoAttachCallId = wantedCallId;
    const attach = (async () => {
      try {
        await video.start(statePayload);
        if (
          generation !== this._videoAttachGeneration ||
          !wantedCallId ||
          this._callId !== wantedCallId
        ) {
          if (video.callId === wantedCallId) await video.close();
        }
      } catch (err) {
        if (generation !== this._videoAttachGeneration) return;
        console.warn("voip-stack-engine: optional SIP video setup failed", err);
        this.dispatchEvent(new CustomEvent("video-error", { detail: err?.message || String(err) }));
      }
    })();
    this._videoAttachPromise = attach;
    try {
      await attach;
    } finally {
      if (this._videoAttachPromise === attach) {
        this._videoAttachPromise = null;
        this._videoAttachCallId = "";
      }
    }
  }

  _resetStats() {
    this._stats = { sent: 0, received: 0, tx_dropped: 0, buffered_frames: 0, frames_drop: 0, underruns: 0 };
  }

  async close(_reason = "", preserveAttach = false, preserveConnect = false) {
    if (!preserveConnect) this._connectGeneration++;
    this._videoAttachGeneration++;
    this._videoAttachPromise = null;
    this._videoAttachCallId = "";
    if (!preserveAttach) this._sessionAttachKey = "";
    const ws = this._ws;
    this._ws = null;
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
      try { ws.close(); } catch (_) {}
    }
    this._callId = "";
    // Detach audio resources before the first await. A new call may start
    // while video/AudioContext teardown for the old call is still pending;
    // that old continuation must never close or null the new pipeline.
    const audioCleanup = this._cleanupAudio("close");
    const videoCleanup = this._video ? this._video.close() : Promise.resolve();
    await Promise.allSettled([audioCleanup, videoCleanup]);
  }

  _detachAudioResources() {
    this._audioSetupGeneration++;
    this._audioReady = false;
    if (this._controlWaiter) {
      window.clearTimeout(this._controlWaiter.timer);
      this._controlWaiter.resolve(null);
      this._controlWaiter = null;
    }
    return this._takeAudioResources();
  }

  _takeAudioResources() {
    const resources = {
      captureNode: this._captureNode,
      captureSink: this._captureSink,
      source: this._source,
      mediaStream: this._mediaStream,
      playbackNode: this._playbackNode,
      audioContext: this._audioContext,
    };
    this._captureNode = null;
    this._captureSink = null;
    this._source = null;
    this._mediaStream = null;
    this._playbackNode = null;
    this._audioContext = null;
    this._audioFrameBuffer = null;
    return resources;
  }

  async _disposeAudioResources(resources = {}) {
    try { resources.captureNode?.disconnect(); } catch (_) {}
    try { resources.captureSink?.disconnect(); } catch (_) {}
    try { resources.source?.disconnect(); } catch (_) {}
    try { resources.mediaStream?.getTracks?.().forEach((track) => track.stop()); } catch (_) {}
    try { resources.playbackNode?.disconnect(); } catch (_) {}
    if (resources.audioContext) await resources.audioContext.close().catch(() => {});
  }

  async _cleanupAudio(_reason) {
    const resources = this._detachAudioResources();
    this._forceIdle();
    await this._disposeAudioResources(resources);
  }
}

export const voipStackEngine = globalThis.__voipStackEngine ||
  (globalThis.__voipStackEngine = new VoipStackEngine());
