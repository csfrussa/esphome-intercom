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
const MAX_AUDIO_WS_BUFFER_MS = 120;
const MIN_AUDIO_WS_BUFFER_FRAMES = 4;
const PCM_FORMATS = Object.freeze(["s16le", "s24le", "s24le_in_s32", "s32le"]);
const FRAME_MS = Object.freeze([10, 16, 20, 32]);

class VoipStackEngine extends EventTarget {
  constructor() {
    super();
    this._hass = null;
    this._ws = null;
    this._state = "IDLE";
    this._deviceId = "";
    this._callId = "";
    this._audioMode = "full_duplex";
    this._txFormat = null;
    this._rxFormat = null;
    this._lastSessionPayload = null;
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
    this._callSubscribers = new Set();
    this._softphoneSubscribers = new Set();
    this._lastEvents = new Map();
    this._lastSoftphoneState = null;
    this._controlWaiter = null;
    this._connectPromise = null;
    this._sessionAttachKey = "";
    this._sessionAttachPromise = null;
    this._ringtoneRequests = new Map();
    this._ringtoneContext = null;
    this._ringtoneTimer = null;
    this._audioFrameBuffer = null;

    window.addEventListener("pagehide", () => {
      this._ringtoneRequests.clear();
      this._stopRingtone();
      void this.close("pagehide");
    });
  }

  configure(hass) {
    this._hass = hass;
    const conn = hass?.connection || null;
    if (!conn || conn === this._busConnection) return;
    if (this._busUnsub) {
      this._busUnsub();
      this._busUnsub = null;
    }
    if (this._softphoneBusUnsub) {
      this._softphoneBusUnsub();
      this._softphoneBusUnsub = null;
    }
    this._busConnection = conn;
    conn.subscribeMessage((event) => this._onBusEvent(event), { type: WS_SUBSCRIBE_CALL_EVENTS })
      .then((unsub) => {
        if (this._busConnection === conn) this._busUnsub = unsub;
        else unsub();
      })
      .catch((err) => {
        if (this._busConnection === conn) this._busConnection = null;
        console.warn("voip-stack-engine: call_event subscription failed", err);
      });
    conn.subscribeMessage(
      (event) => this._onSoftphoneState(event),
      { type: WS_SUBSCRIBE_HA_SOFTPHONE },
    ).then((unsub) => {
      if (this._busConnection === conn) this._softphoneBusUnsub = unsub;
      else unsub();
    }).catch((err) => {
      console.warn("voip-stack-engine: HA softphone subscription failed", err);
    });
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

  get stats() {
    return { ...this._stats };
  }

  statsText() {
    if (!this.active) return "";
    return `Sent: ${this._stats.sent} | Recv: ${this._stats.received} | TxDrop: ${this._stats.tx_dropped || 0} | Buf: ${this._stats.buffered_frames} | Und: ${this._stats.underruns || 0}`;
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

  async _wsUrl(deviceId) {
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const path = `/api/voip_stack/ws?device_id=${encodeURIComponent(deviceId)}`;
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
    ) return;
    if (
      this._connectPromise &&
      this._deviceId === deviceId &&
      this._callId === wantedCallId &&
      this._ws &&
      this._ws.readyState === WebSocket.CONNECTING
    ) {
      return this._connectPromise;
    }
    await this.close("switch", true);
    this._deviceId = deviceId;
    this._callId = wantedCallId;
    this._lastSessionPayload = null;
    const wsUrl = await this._wsUrl(deviceId);
    if (this._deviceId !== deviceId || this._callId !== wantedCallId) {
      throw new Error("Audio WebSocket superseded before connect");
    }
    const ws = new WebSocket(wsUrl);
    this._ws = ws;
    ws.binaryType = "arraybuffer";
    ws.onmessage = (event) => {
      if (this._ws === ws) this._handleMessage(event);
    };
    let opened = false;
    const connectPromise = new Promise((resolve, reject) => {
      ws.onopen = () => {
        opened = true;
        if (this._ws === ws) resolve();
        else {
          try { ws.close(); } catch (_) {}
          reject(new Error("Audio WebSocket superseded"));
        }
      };
      ws.onerror = () => reject(new Error("Audio WebSocket failed"));
      ws.onclose = () => {
        if (!opened) reject(new Error("Audio WebSocket closed before opening"));
        if (this._ws !== ws) return;
        this._ws = null;
        void this._cleanupAudio("ws_close");
      };
    });
    this._connectPromise = connectPromise;
    try {
      await connectPromise;
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
        if (msg.tx_format || msg.rx_format) this._lastSessionPayload = msg;
        if (msg.state) this._setState(String(msg.state).toUpperCase());
        if (msg.error) this.dispatchEvent(new CustomEvent("error", { detail: msg.error }));
        this._resolveControlWaiter(msg);
      } catch (_) {}
      return;
    }
    const raw = new Uint8Array(event.data);
    if (raw[0] !== WS_AUDIO || raw.byteLength < 2 || !this._playbackNode) return;
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

  async _setupAudio(deviceInfo, negotiated = null) {
    this._audioMode = this._normaliseAudioMode(deviceInfo?.audio_mode);
    const formats = this._resolveSessionFormats(negotiated);
    this._txFormat = formats.tx;
    this._rxFormat = formats.rx;
    const sendToEsp = this._audioMode === "full_duplex" || this._audioMode === "speaker_only";
    const receiveFromEsp = this._audioMode === "full_duplex" || this._audioMode === "mic_only";
    if (!sendToEsp && !receiveFromEsp) return;

    this._audioContext = this._createAudioContext();
    if (this._audioContext.state === "suspended") await this._audioContext.resume();

    if (sendToEsp) {
      this._mediaStream = await navigator.mediaDevices.getUserMedia({
        audio: { echoCancellation: true, noiseSuppression: true, autoGainControl: true },
      });
      await this._audioContext.audioWorklet.addModule(`/voip-stack/voip-stack-processor.js?v=${encodeURIComponent(MODULE_VERSION)}`);
      this._source = this._audioContext.createMediaStreamSource(this._mediaStream);
      this._captureNode = new AudioWorkletNode(this._audioContext, "voip-stack-processor", {
        processorOptions: { format: this._txFormat },
      });
      this._captureNode.port.onmessage = (event) => {
        if (event.data?.type === "audio") this._sendAudio(event.data.buffer);
      };
      this._source.connect(this._captureNode);
      this._captureSink = this._audioContext.createGain();
      this._captureSink.gain.value = 0;
      this._captureNode.connect(this._captureSink).connect(this._audioContext.destination);
    }

    if (receiveFromEsp) {
      await this._audioContext.audioWorklet.addModule(`/voip-stack/voip-stack-playback-processor.js?v=${encodeURIComponent(MODULE_VERSION)}`);
      this._playbackNode = new AudioWorkletNode(this._audioContext, "voip-stack-playback-processor", {
        outputChannelCount: [this._rxFormat.channels],
        processorOptions: { format: this._rxFormat },
      });
      this._playbackNode.port.onmessage = (event) => {
        if (event.data?.type !== "stats") return;
        this._stats = { ...this._stats, ...event.data };
        this._emit();
      };
      this._playbackNode.connect(this._audioContext.destination);
    }
  }

  _normaliseAudioMode(value) {
    const v = String(value || "").trim().toLowerCase();
    return ["full_duplex", "mic_only", "speaker_only", "control_only"].includes(v) ? v : "full_duplex";
  }

  async _setupAudioOrAbort(deviceId, deviceInfo, reply, attachKey = "") {
    try {
      await this._connect(deviceId, reply?.call_id || "");
      await this._setupAudio(deviceInfo, reply);
      if (attachKey && this._sessionAttachKey !== attachKey) {
        await this.close("superseded", true);
        return false;
      }
      return true;
    } catch (err) {
      if (attachKey && this._sessionAttachKey !== attachKey) {
        await this.close("superseded", true);
        return false;
      }
      console.error("voip-stack-engine: audio setup failed", err);
      this.dispatchEvent(new CustomEvent("error", { detail: err?.message || String(err) }));
      if (deviceId === HA_SOFTPHONE_DEVICE_ID && this._hass) {
        await this._hass.callService("voip_stack", "hangup", {
          call_id: reply?.call_id || "",
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
    });
    if (!["calling", "connecting", "remote_ringing", "ringing", "in_call"].includes(String(reply?.state || "").toLowerCase())) {
      this._setState("IDLE");
      return reply;
    }
    const state = String(reply.state || "calling").toLowerCase();
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
    const previousAttach = this._sessionAttachPromise;
    this._sessionAttachKey = attachKey;
    const attachPromise = (async () => {
      if (previousAttach) await previousAttach.catch(() => {});
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

  async _resumeSessionLocked(deviceInfo, deviceId, statePayload, attachKey) {
    const callId = String(statePayload?.call_id || "");
    if (
      this._ws &&
      this._deviceId === deviceId &&
      this._callId === callId &&
      this._ws.readyState === WebSocket.OPEN
    ) {
      this._setState("IN_CALL");
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
  }

  _resetStats() {
    this._stats = { sent: 0, received: 0, tx_dropped: 0, buffered_frames: 0, frames_drop: 0, underruns: 0 };
  }

  async close(_reason = "", preserveAttach = false) {
    if (!preserveAttach) this._sessionAttachKey = "";
    const ws = this._ws;
    this._ws = null;
    if (ws && (ws.readyState === WebSocket.OPEN || ws.readyState === WebSocket.CONNECTING)) {
      try { ws.close(); } catch (_) {}
    }
    this._callId = "";
    await this._cleanupAudio("close");
  }

  async _cleanupAudio(_reason) {
    if (this._controlWaiter) {
      window.clearTimeout(this._controlWaiter.timer);
      this._controlWaiter.resolve(null);
      this._controlWaiter = null;
    }
    if (this._captureNode) { this._captureNode.disconnect(); this._captureNode = null; }
    if (this._captureSink) { this._captureSink.disconnect(); this._captureSink = null; }
    if (this._source) { this._source.disconnect(); this._source = null; }
    if (this._mediaStream) { this._mediaStream.getTracks().forEach((t) => t.stop()); this._mediaStream = null; }
    if (this._playbackNode) { this._playbackNode.disconnect(); this._playbackNode = null; }
    if (this._audioContext) { await this._audioContext.close().catch(() => {}); this._audioContext = null; }
    this._audioFrameBuffer = null;
    this._forceIdle();
  }
}

export const voipStackEngine = globalThis.__voipStackEngine ||
  (globalThis.__voipStackEngine = new VoipStackEngine());
