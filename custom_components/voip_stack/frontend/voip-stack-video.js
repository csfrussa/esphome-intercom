const VIDEO_ACCESS_UNIT = 1;
const VIDEO_HEADER_BYTES = 6;
const MAX_VIDEO_WS_BUFFER = 2 * 1024 * 1024;
const MAX_PENDING_DECODE_BYTES = 8 * 1024 * 1024;
const MAX_PENDING_DECODE_FRAMES = 60;

export class VoipStackVideo extends EventTarget {
  constructor() {
    super();
    this._hass = null;
    this._ws = null;
    this._callId = "";
    this._active = false;
    this._canReceive = false;
    this._canSend = false;
    this._canvas = null;
    this._decoder = null;
    this._encoder = null;
    this._cameraStream = null;
    this._cameraReader = null;
    this._encodeTask = null;
    this._rtpTimestampBase = null;
    this._rtpTimestampLast = null;
    this._rtpTimestampTicks = 0;
    this._encodedFrames = 0;
    this._pendingDecode = [];
    this._pendingDecodeBytes = 0;
    this._generation = 0;
    this._stats = { received: 0, sent: 0, dropped: 0, decode_errors: 0 };
  }

  configure(hass) {
    this._hass = hass;
  }

  get active() {
    return this._active;
  }

  get callId() {
    return this._callId;
  }

  get visible() {
    return this._active && this._canReceive;
  }

  get stats() {
    return { ...this._stats };
  }

  setCanvas(canvas) {
    this._canvas = canvas || null;
  }

  _emit() {
    this.dispatchEvent(new CustomEvent("state", {
      detail: {
        active: this._active,
        visible: this.visible,
        can_receive: this._canReceive,
        can_send: this._canSend,
        call_id: this._callId,
        stats: this.stats,
      },
    }));
  }

  async _wsUrl(callId) {
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const path = `/api/voip_stack/video_ws?call_id=${encodeURIComponent(callId)}`;
    const signed = await this._hass.callWS({ type: "auth/sign_path", path });
    return `${proto}//${window.location.host}${signed.path || path}`;
  }

  async start(statePayload) {
    const callId = String(statePayload?.call_id || "");
    if (!statePayload?.video_active || !callId) {
      await this.close();
      return false;
    }
    if (this._ws?.readyState === WebSocket.OPEN && this._callId === callId) return true;
    await this.close();
    if (!window.isSecureContext) {
      throw new Error("Experimental SIP video requires a secure browser context");
    }
    const generation = ++this._generation;
    this._callId = callId;
    this._stats = { received: 0, sent: 0, dropped: 0, decode_errors: 0 };
    const ws = new WebSocket(await this._wsUrl(callId));
    ws.binaryType = "arraybuffer";
    this._ws = ws;
    let helloResolve;
    let helloReject;
    const hello = new Promise((resolve, reject) => {
      helloResolve = resolve;
      helloReject = reject;
    });
    ws.onmessage = (event) => {
      if (this._ws !== ws) return;
      if (typeof event.data === "string") {
        try {
          const payload = JSON.parse(event.data);
          if (payload.error) helloReject(new Error(payload.error));
          else helloResolve(payload);
        } catch (err) {
          helloReject(err);
        }
        return;
      }
      if (!this._decoder || this._decoder.state !== "configured") {
        this._bufferDecodeMessage(event.data);
      } else {
        this._decodeMessage(event.data);
      }
    };
    const opened = new Promise((resolve, reject) => {
      ws.onopen = resolve;
      ws.onerror = () => {
        const error = new Error("SIP video WebSocket failed");
        reject(error);
        helloReject(error);
      };
    });
    ws.onclose = () => {
      helloReject(new Error("SIP video WebSocket closed before negotiation"));
      if (this._ws !== ws) return;
      this._ws = null;
      this._generation++;
      void this._cleanupMedia();
    };
    try {
      await opened;
      const negotiated = await Promise.race([
        hello,
        new Promise((_, reject) => window.setTimeout(
          () => reject(new Error("SIP video negotiation timed out")),
          3000,
        )),
      ]);
      if (!this._isCurrent(generation, ws, callId)) return false;
      await this._setupCodecs(negotiated, generation);
      if (!this._isCurrent(generation, ws, callId)) return false;
      this._active = true;
      this._emit();
      return true;
    } catch (err) {
      if (this._isCurrent(generation, ws, callId)) await this.close();
      throw err;
    }
  }

  _isCurrent(generation, ws, callId) {
    return generation === this._generation && this._ws === ws && this._callId === callId;
  }

  async _setupCodecs(negotiated, generation) {
    const codec = String(negotiated?.codec || "avc1.42E01F");
    const failures = [];
    let usablePaths = 0;
    if (negotiated?.can_receive) {
      try {
        if (typeof VideoDecoder === "undefined") {
          throw new Error("WebCodecs VideoDecoder is unavailable");
        }
        const decoderConfig = await this._supportedConfig(VideoDecoder, {
          codec,
          optimizeForLatency: true,
        });
        if (generation !== this._generation) throw new Error("SIP video session was superseded");
        const support = await VideoDecoder.isConfigSupported(decoderConfig);
        if (generation !== this._generation) throw new Error("SIP video session was superseded");
        if (!support?.supported) throw new Error(`browser cannot decode ${codec}`);
        this._decoder = new VideoDecoder({
          output: (frame) => this._drawFrame(frame),
          error: () => {
            this._stats.decode_errors++;
            this._emit();
          },
        });
        this._decoder.configure(support.config || decoderConfig);
        this._canReceive = true;
        this._active = true;
        usablePaths++;
        this._flushPendingDecode();
        this._emit();
      } catch (err) {
        if (generation === this._generation) this._cleanupReceiver();
        failures.push(`receive: ${err?.message || String(err)}`);
      }
    }
    if (negotiated?.can_send) {
      const setupSender = async () => {
        try {
          await this._setupEncoder(codec, generation);
          if (generation !== this._generation) return "superseded";
          this._canSend = true;
          this._active = true;
          this._emit();
          return "";
        } catch (err) {
          if (generation === this._generation) await this._cleanupSender();
          return `send: ${err?.message || String(err)}`;
        }
      };
      if (usablePaths) {
        // A camera permission prompt may remain open indefinitely. Incoming
        // video is already usable, so expose it immediately and let the
        // independent send direction finish in the background.
        void setupSender().then((failure) => {
          if (failure && failure !== "superseded" && generation === this._generation) {
            console.warn(`voip-stack-video: partial media support (${failure})`);
          }
        });
      } else {
        const failure = await setupSender();
        if (failure) failures.push(failure);
        else usablePaths++;
      }
    }
    if (!usablePaths) {
      throw new Error(failures.join("; ") || "No negotiated SIP video direction is usable");
    }
    if (failures.length) {
      // Video directions are independent in RFC 3264. Camera permission or
      // encoder failure must not hide a valid incoming door-station stream,
      // and a decoder limitation must not tear down a valid outgoing stream.
      console.warn(`voip-stack-video: partial media support (${failures.join("; ")})`);
    }
  }

  async _setupEncoder(codec, generation) {
    if (
      typeof VideoEncoder === "undefined" ||
      typeof MediaStreamTrackProcessor === "undefined" ||
      !navigator.mediaDevices?.getUserMedia
    ) {
      throw new Error("Browser cannot send SIP video with WebCodecs");
    }
    const stream = await navigator.mediaDevices.getUserMedia({
      video: {
        width: { ideal: 640, max: 1280 },
        height: { ideal: 360, max: 720 },
        frameRate: { ideal: 15, max: 20 },
      },
      audio: false,
    });
    if (generation !== this._generation) {
      stream.getTracks().forEach((item) => item.stop());
      throw new Error("SIP video session was superseded");
    }
    this._cameraStream = stream;
    const track = stream.getVideoTracks()[0];
    if (!track) throw new Error("No browser camera track available");
    const settings = track.getSettings();
    const width = Math.max(16, Number(settings.width || 640) & ~1);
    const height = Math.max(16, Number(settings.height || 360) & ~1);
    const framerate = Math.max(1, Math.min(20, Number(settings.frameRate || 15)));
    const encoderConfig = await this._supportedConfig(VideoEncoder, {
      codec,
      width,
      height,
      framerate,
      bitrate: 600000,
      latencyMode: "realtime",
      avc: { format: "annexb" },
    });
    if (generation !== this._generation) throw new Error("SIP video session was superseded");
    const support = await VideoEncoder.isConfigSupported(encoderConfig);
    if (generation !== this._generation) throw new Error("SIP video session was superseded");
    if (!support?.supported) throw new Error(`Browser cannot encode negotiated SIP video ${codec}`);
    this._encoder = new VideoEncoder({
      output: (chunk) => this._sendEncodedChunk(chunk),
      error: () => {
        this._stats.dropped++;
        this._emit();
      },
    });
    this._encoder.configure(support.config || encoderConfig);
    const processor = new MediaStreamTrackProcessor({ track });
    this._cameraReader = processor.readable.getReader();
    this._encodeTask = this._encodeCamera(framerate);
  }

  async _supportedConfig(codecClass, base) {
    // Hardware acceleration is a preference, not a media requirement. Some
    // Chromium builds expose H.264 only through software WebCodecs; rejecting
    // those browsers would needlessly turn a valid audio/video call into audio
    // only. Keep the negotiated codec fixed and relax only the accelerator.
    for (const hardwareAcceleration of ["prefer-hardware", "prefer-software", null]) {
      const candidate = hardwareAcceleration
        ? { ...base, hardwareAcceleration }
        : { ...base };
      try {
        const support = await codecClass.isConfigSupported(candidate);
        if (support?.supported) return support.config || candidate;
      } catch (_) {}
    }
    return base;
  }

  async _encodeCamera(framerate) {
    const keyInterval = Math.max(1, Math.round(framerate * 2));
    try {
      while (this._cameraReader && this._encoder?.state === "configured") {
        const { value: frame, done } = await this._cameraReader.read();
        if (done || !frame) break;
        try {
          if (this._encoder.encodeQueueSize > 2) {
            this._stats.dropped++;
          } else {
            const keyFrame = this._encodedFrames % keyInterval === 0;
            this._encoder.encode(frame, { keyFrame });
            this._encodedFrames++;
          }
        } finally {
          frame.close();
        }
      }
    } catch (_) {
      if (this._active) this._stats.dropped++;
    }
  }

  _sendEncodedChunk(chunk) {
    const ws = this._ws;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    if (ws.bufferedAmount > MAX_VIDEO_WS_BUFFER) {
      this._stats.dropped++;
      return;
    }
    const payload = new Uint8Array(chunk.byteLength);
    chunk.copyTo(payload);
    const frame = new Uint8Array(VIDEO_HEADER_BYTES + payload.byteLength);
    const view = new DataView(frame.buffer);
    frame[0] = VIDEO_ACCESS_UNIT;
    frame[1] = chunk.type === "key" ? 1 : 0;
    const rtpTimestamp = Math.round(Number(chunk.timestamp || 0) * 90000 / 1000000) >>> 0;
    view.setUint32(2, rtpTimestamp, false);
    frame.set(payload, VIDEO_HEADER_BYTES);
    ws.send(frame);
    this._stats.sent++;
    if ((this._stats.sent & 31) === 0) this._emit();
  }

  _decodeMessage(buffer) {
    if (!this._decoder || this._decoder.state !== "configured") return;
    const bytes = new Uint8Array(buffer);
    if (bytes.byteLength <= VIDEO_HEADER_BYTES || bytes[0] !== VIDEO_ACCESS_UNIT) return;
    if (this._decoder.decodeQueueSize > 3 && !(bytes[1] & 1)) {
      this._stats.dropped++;
      return;
    }
    const rtpTimestamp = new DataView(buffer).getUint32(2, false);
    const timestamp = this._unwrapRtpTimestamp(rtpTimestamp);
    try {
      this._decoder.decode(new EncodedVideoChunk({
        type: bytes[1] & 1 ? "key" : "delta",
        timestamp,
        data: bytes.subarray(VIDEO_HEADER_BYTES),
      }));
      this._stats.received++;
      if ((this._stats.received & 31) === 0) this._emit();
    } catch (_) {
      this._stats.decode_errors++;
    }
  }

  _bufferDecodeMessage(buffer) {
    const bytes = new Uint8Array(buffer);
    if (bytes.byteLength <= VIDEO_HEADER_BYTES || bytes[0] !== VIDEO_ACCESS_UNIT) return;
    const keyFrame = Boolean(bytes[1] & 1);
    // A decoder can only join the stream from an IDR. Retain a bounded GOP
    // while WebCodecs and camera permission are being prepared, replacing it
    // whenever a newer key frame arrives.
    if (keyFrame) {
      this._pendingDecode = [];
      this._pendingDecodeBytes = 0;
    } else if (!this._pendingDecode.length) {
      return;
    }
    if (
      this._pendingDecode.length >= MAX_PENDING_DECODE_FRAMES ||
      this._pendingDecodeBytes + bytes.byteLength > MAX_PENDING_DECODE_BYTES
    ) {
      this._stats.dropped++;
      return;
    }
    this._pendingDecode.push(buffer);
    this._pendingDecodeBytes += bytes.byteLength;
  }

  _flushPendingDecode() {
    const pending = this._pendingDecode;
    this._pendingDecode = [];
    this._pendingDecodeBytes = 0;
    for (const buffer of pending) this._decodeMessage(buffer);
  }

  _unwrapRtpTimestamp(value) {
    if (this._rtpTimestampBase === null) {
      this._rtpTimestampBase = value;
      this._rtpTimestampLast = value;
      this._rtpTimestampTicks = 0;
      return 0;
    }
    const delta = (value - this._rtpTimestampLast) >>> 0;
    if (delta < 0x80000000) this._rtpTimestampTicks += delta;
    this._rtpTimestampLast = value;
    return Math.round(this._rtpTimestampTicks * 1000000 / 90000);
  }

  _drawFrame(frame) {
    try {
      const canvas = this._canvas;
      if (!canvas) return;
      const width = frame.displayWidth || frame.codedWidth;
      const height = frame.displayHeight || frame.codedHeight;
      if (canvas.width !== width || canvas.height !== height) {
        canvas.width = width;
        canvas.height = height;
      }
      const context = canvas.getContext("2d", { alpha: false, desynchronized: true });
      context.drawImage(frame, 0, 0, width, height);
    } finally {
      frame.close();
    }
  }

  async close() {
    this._generation++;
    const ws = this._ws;
    this._ws = null;
    if (ws && [WebSocket.OPEN, WebSocket.CONNECTING].includes(ws.readyState)) {
      try { ws.close(); } catch (_) {}
    }
    await this._cleanupMedia();
  }

  async _cleanupMedia() {
    await this._cleanupSender();
    this._cleanupReceiver();
    this._callId = "";
    this._active = false;
    this._canReceive = false;
    this._canSend = false;
    this._rtpTimestampBase = null;
    this._rtpTimestampLast = null;
    this._rtpTimestampTicks = 0;
    this._encodedFrames = 0;
    this._pendingDecode = [];
    this._pendingDecodeBytes = 0;
    this._emit();
  }

  async _cleanupSender() {
    if (this._cameraReader) {
      await this._cameraReader.cancel().catch(() => {});
      this._cameraReader = null;
    }
    if (this._encodeTask) {
      await this._encodeTask.catch(() => {});
      this._encodeTask = null;
    }
    if (this._encoder) {
      if (this._encoder.state !== "closed") this._encoder.close();
      this._encoder = null;
    }
    if (this._cameraStream) {
      this._cameraStream.getTracks().forEach((track) => track.stop());
      this._cameraStream = null;
    }
    this._canSend = false;
  }

  _cleanupReceiver() {
    if (this._decoder) {
      if (this._decoder.state !== "closed") this._decoder.close();
      this._decoder = null;
    }
    this._pendingDecode = [];
    this._pendingDecodeBytes = 0;
    this._canReceive = false;
  }
}
