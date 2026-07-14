const VIDEO_ACCESS_UNIT = 1;
const VIDEO_HEADER_BYTES = 6;
const MAX_VIDEO_WS_BUFFER = 2 * 1024 * 1024;
const MAX_PENDING_DECODE_BYTES = 8 * 1024 * 1024;
const MAX_PENDING_DECODE_FRAMES = 60;
const CAMERA_STORAGE_KEY = "voip_stack_video_camera_enabled";

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
    this._encoding = "H264";
    this._clockRate = 90000;
    this._negotiated = null;
    this._cameraAllowed = false;
    try {
      this._cameraEnabled = localStorage.getItem(CAMERA_STORAGE_KEY) === "true";
    } catch (_) {
      this._cameraEnabled = false;
    }
    this._rtpTimestampBase = null;
    this._rtpTimestampLast = null;
    this._rtpTimestampTicks = 0;
    this._encodedFrames = 0;
    this._pendingDecode = [];
    this._pendingDecodeBytes = 0;
    this._generation = 0;
    this._lastRenderedAt = 0;
    this._lastRenderedTimestamp = null;
    this._lastDecodedAt = 0;
    this._lastDecodedTimestamp = null;
    this._stats = this._emptyStats();
  }

  _emptyStats() {
    return {
      received: 0,
      sent: 0,
      rendered: 0,
      dropped: 0,
      dropped_decode_backpressure: 0,
      dropped_timestamp_regression: 0,
      dropped_frame_queue: 0,
      dropped_render_coalesce: 0,
      dropped_pending_decode: 0,
      decode_errors: 0,
      max_frame_gap_ms: 0,
      max_arrival_gap_ms: 0,
      max_source_gap_ms: 0,
      render_gaps_over_100_ms: 0,
      render_gaps_over_250_ms: 0,
      playout_ms: 0,
    };
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

  get canSend() {
    return this._cameraAllowed;
  }

  get cameraEnabled() {
    return this._cameraEnabled;
  }

  async setCameraEnabled(enabled) {
    this._cameraEnabled = Boolean(enabled);
    try { localStorage.setItem(CAMERA_STORAGE_KEY, String(this._cameraEnabled)); } catch (_) {}
    if (!this._cameraEnabled) {
      await this._cleanupSender();
      this._emit();
      return;
    }
    if (!this._active || !this._cameraAllowed || !this._negotiated || this._encoder) return;
    const generation = this._generation;
    await this._setupEncoder(String(this._negotiated.codec || "avc1.42E01F"), generation);
    if (generation === this._generation) {
      this._canSend = true;
      this._emit();
    }
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
        camera_available: this._cameraAllowed,
        camera_enabled: this._cameraEnabled,
        encoding: this._encoding,
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
    this._stats = this._emptyStats();
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
      if (this._encoding === "JPEG" && this._canReceive) {
        this._decodeMessage(event.data);
      } else if (!this._decoder || this._decoder.state !== "configured") {
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
      this._negotiated = negotiated;
      this._encoding = String(negotiated?.encoding || "H264").toUpperCase();
      this._clockRate = Math.max(1, Number(negotiated?.clock_rate || 90000));
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
        if (this._encoding === "JPEG") {
          if (typeof createImageBitmap !== "function") {
            throw new Error("browser cannot decode JPEG video frames");
          }
          this._canReceive = true;
          this._active = true;
          usablePaths++;
          this._flushPendingDecode();
          this._emit();
        } else {
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
            output: (frame) => this._queueDecodedFrame(frame),
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
        }
      } catch (err) {
        if (generation === this._generation) this._cleanupReceiver();
        failures.push(`receive: ${err?.message || String(err)}`);
      }
    }
    this._cameraAllowed = Boolean(negotiated?.can_send);
    if (this._cameraAllowed && this._cameraEnabled) {
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
    } else if (this._cameraAllowed) {
      // A send-only dialog may wait for an explicit user camera choice. Keep
      // the authenticated media attachment alive without prompting on load.
      usablePaths++;
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
    if (generation !== this._generation || !this._cameraEnabled) {
      stream.getTracks().forEach((item) => item.stop());
      throw new Error(
        generation !== this._generation
          ? "SIP video session was superseded"
          : "Browser camera transmission was disabled"
      );
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
      ...(this._encoding === "H264" ? { avc: { format: "annexb" } } : {}),
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
    const rtpTimestamp = Math.round(Number(chunk.timestamp || 0) * this._clockRate / 1000000) >>> 0;
    view.setUint32(2, rtpTimestamp, false);
    frame.set(payload, VIDEO_HEADER_BYTES);
    ws.send(frame);
    this._stats.sent++;
    if ((this._stats.sent & 31) === 0) this._emit();
  }

  _decodeMessage(buffer) {
    if (this._encoding === "JPEG") {
      this._decodeJpegMessage(buffer);
      return;
    }
    if (!this._decoder || this._decoder.state !== "configured") return;
    const bytes = new Uint8Array(buffer);
    if (bytes.byteLength <= VIDEO_HEADER_BYTES || bytes[0] !== VIDEO_ACCESS_UNIT) return;
    // Never discard an encoded inter-frame to relieve decoder backpressure.
    // H.264 and VP8 delta frames are references for later pictures; dropping
    // one creates exactly the frozen frames and motion trails that a live SIP
    // stream must avoid. WebCodecs already owns the bounded decode queue.
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

  _decodeJpegMessage(buffer) {
    const bytes = new Uint8Array(buffer);
    if (bytes.byteLength <= VIDEO_HEADER_BYTES || bytes[0] !== VIDEO_ACCESS_UNIT) return;
    const rtpTimestamp = new DataView(buffer).getUint32(2, false);
    const timestamp = this._unwrapRtpTimestamp(rtpTimestamp);
    const generation = this._generation;
    const payload = bytes.slice(VIDEO_HEADER_BYTES);
    void createImageBitmap(new Blob([payload], { type: "image/jpeg" })).then((bitmap) => {
      if (generation !== this._generation || !this._active) {
        bitmap.close();
        return;
      }
      this._stats.received++;
      this._queueDecodedFrame({
        bitmap,
        timestamp,
        displayWidth: bitmap.width,
        displayHeight: bitmap.height,
        close: () => bitmap.close(),
      });
      if ((this._stats.received & 31) === 0) this._emit();
    }).catch(() => {
      if (generation === this._generation) this._stats.decode_errors++;
    });
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
      this._stats.dropped_pending_decode++;
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
    return Math.round(this._rtpTimestampTicks * 1000000 / this._clockRate);
  }

  _queueDecodedFrame(frame) {
    if (!frame) return;
    const now = performance.now();
    const timestamp = Number(frame.timestamp || 0);
    if (this._lastRenderedTimestamp !== null && timestamp < this._lastRenderedTimestamp) {
      frame.close();
      this._stats.dropped++;
      this._stats.dropped_timestamp_regression++;
      return;
    }
    if (this._lastDecodedAt) {
      this._stats.max_arrival_gap_ms = Math.max(
        this._stats.max_arrival_gap_ms,
        Math.round(now - this._lastDecodedAt),
      );
    }
    if (this._lastDecodedTimestamp !== null && timestamp >= this._lastDecodedTimestamp) {
      this._stats.max_source_gap_ms = Math.max(
        this._stats.max_source_gap_ms,
        Math.round((timestamp - this._lastDecodedTimestamp) / 1000),
      );
    }
    this._lastDecodedAt = now;
    this._lastDecodedTimestamp = timestamp;
    this._drawFrame(frame);
    if (this._lastRenderedAt) {
      const gap = Math.round(now - this._lastRenderedAt);
      this._stats.max_frame_gap_ms = Math.max(this._stats.max_frame_gap_ms, gap);
      if (gap > 100) this._stats.render_gaps_over_100_ms++;
      if (gap > 250) this._stats.render_gaps_over_250_ms++;
    }
    this._lastRenderedAt = now;
    this._lastRenderedTimestamp = timestamp;
    this._stats.rendered++;
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
      const context = canvas.getContext("2d", { alpha: false });
      context.drawImage(frame.bitmap || frame, 0, 0, width, height);
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
    this._negotiated = null;
    this._cameraAllowed = false;
    this._lastRenderedAt = 0;
    this._lastRenderedTimestamp = null;
    this._lastDecodedAt = 0;
    this._lastDecodedTimestamp = null;
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
