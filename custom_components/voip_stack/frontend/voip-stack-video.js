import {
  cameraCaptureContract,
  cameraStorageKey,
  directionalVideoContract,
  emptyVideoStats,
  legacyVideoAliases,
} from "./voip-stack-video-model.js";

const VIDEO_ACCESS_UNIT = 1;
const VIDEO_HEADER_BYTES = 6;
const MAX_VIDEO_WS_BUFFER = 2 * 1024 * 1024;
const MAX_PENDING_DECODE_BYTES = 8 * 1024 * 1024;
const MAX_PENDING_DECODE_FRAMES = 60;
const MAX_DECODE_QUEUE_FRAMES = 8;
const CAMERA_STORAGE_KEY = "voip_stack_video_camera_enabled";

export class VoipStackVideo extends EventTarget {
  constructor() {
    super();
    this._hass = null;
    this._clientId = "";
    this._ws = null;
    this._callId = "";
    this._endpointId = "default";
    this._active = false;
    this._canReceive = false;
    this._canSend = false;
    this._canvas = null;
    this._decoder = null;
    this._encoder = null;
    this._cameraStream = null;
    this._cameraReader = null;
    this._encodeTask = null;
    this._forceCameraKeyFrame = true;
    this._sendDropUntilKeyFrame = false;
    this._encoding = "H264";
    this._clockRate = 90000;
    this._negotiated = null;
    this._cameraAllowed = false;
    this._cameraEnabled = this.cameraEnabledFor(this._endpointId);
    this._rtpTimestampBase = null;
    this._rtpTimestampLast = null;
    this._rtpTimestampTicks = 0;
    this._encodedFrames = 0;
    this._pendingDecode = [];
    this._pendingDecodeBytes = 0;
    this._dropUntilKeyFrame = false;
    this._jpegDecodePending = false;
    this._jpegQueuedBuffer = null;
    this._jpegDecodeToken = null;
    this._mediaUpdatePromise = Promise.resolve();
    this._generation = 0;
    this._senderGeneration = 0;
    this._senderSetupPromise = null;
    this._senderSetupToken = 0;
    this._lastRenderedAt = 0;
    this._lastRenderedTimestamp = null;
    this._lastDecodedAt = 0;
    this._lastDecodedTimestamp = null;
    this._stats = this._emptyStats();
  }

  _emptyStats() {
    return emptyVideoStats();
  }

  configure(hass, clientId = "") {
    this._hass = hass;
    if (clientId) this._clientId = String(clientId);
  }

  get active() {
    return this._active;
  }

  get callId() {
    return this._callId;
  }

  get visible() {
    // SDP only tells us that the peer may send video. Some PBXs advertise a
    // recvonly/sendrecv video line even for an audio-first call and never send
    // a frame. Keep the audio UI compact until an actual frame is rendered.
    return this._active && this._canReceive && this._stats.rendered > 0;
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

  cameraEnabledFor(endpointId = "default") {
    try {
      const scoped = localStorage.getItem(cameraStorageKey(endpointId));
      if (scoped !== null) return scoped === "true";
      // Migrate the original browser-wide preference lazily. Existing users
      // keep their choice, while the first change on each logical phone writes
      // an independent endpoint-scoped value.
      return localStorage.getItem(CAMERA_STORAGE_KEY) === "true";
    } catch (_) {
      return false;
    }
  }

  async setCameraEnabled(enabled, endpointId = this._endpointId) {
    const endpoint = String(endpointId || "default").trim() || "default";
    const selected = Boolean(enabled);
    try { localStorage.setItem(cameraStorageKey(endpoint), String(selected)); } catch (_) {}
    // An idle engine resets its media endpoint to default. A settings toggle
    // for another phone must still persist without mutating an unrelated live
    // sender.
    if (this._active && endpoint !== this._endpointId) return;
    this._cameraEnabled = selected;
    if (!this._cameraEnabled) {
      await this._cleanupSender();
      this._emit();
      return;
    }
    if (!this._active || !this._cameraAllowed || !this._negotiated || this._encoder) return;
    const generation = this._generation;
    await this._ensureSender(this._mediaContract("send").codec, generation);
    if (
      generation === this._generation &&
      this._cameraEnabled &&
      this._encoder?.state === "configured"
    ) {
      this._canSend = true;
      this._emit();
    }
  }

  setCanvas(canvas) {
    this._canvas = canvas || null;
  }

  _emit() {
    const send = this._mediaContract("send");
    const receive = this._mediaContract("receive");
    this.dispatchEvent(new CustomEvent("state", {
      detail: {
        active: this._active,
        visible: this.visible,
        can_receive: this._canReceive,
        can_send: this._canSend,
        camera_available: this._cameraAllowed,
        camera_enabled: this._cameraEnabled,
        encoding: this._encoding,
        send_encoding: send.encoding,
        receive_encoding: receive.encoding,
        send_clock_rate: send.clockRate,
        receive_clock_rate: receive.clockRate,
        send_payload_type: send.payloadType,
        receive_payload_type: receive.payloadType,
        call_id: this._callId,
        endpoint_id: this._endpointId,
        stats: this.stats,
      },
    }));
  }

  async _wsUrl(callId, endpointId = "default") {
    const proto = window.location.protocol === "https:" ? "wss:" : "ws:";
    const path = `/api/voip_stack/video_ws?endpoint_id=${encodeURIComponent(endpointId || "default")}&call_id=${encodeURIComponent(callId)}&client_id=${encodeURIComponent(this._clientId)}`;
    const signed = await this._hass.callWS({ type: "auth/sign_path", path });
    return `${proto}//${window.location.host}${signed.path || path}`;
  }

  async start(statePayload) {
    const callId = String(statePayload?.call_id || "");
    const endpointId = String(statePayload?.endpoint_id || "default");
    if (!statePayload?.video_active || !callId) {
      await this.close();
      return false;
    }
    if (
      this._ws?.readyState === WebSocket.OPEN &&
      this._callId === callId &&
      this._endpointId === endpointId
    ) return true;
    await this.close();
    if (!window.isSecureContext) {
      throw new Error("Experimental SIP video requires a secure browser context");
    }
    const generation = ++this._generation;
    this._callId = callId;
    this._endpointId = endpointId;
    this._cameraEnabled = this.cameraEnabledFor(endpointId);
    this._stats = this._emptyStats();
    const url = await this._wsUrl(callId, endpointId);
    if (
      generation !== this._generation ||
      this._callId !== callId ||
      this._endpointId !== endpointId
    ) return false;
    const ws = new WebSocket(url);
    ws.binaryType = "arraybuffer";
    this._ws = ws;
    let helloResolve;
    let helloReject;
    const hello = new Promise((resolve, reject) => {
      helloResolve = resolve;
      helloReject = reject;
    });
    // The socket can close while start() is still awaiting OPEN. Mark the
    // parallel hello Promise as observed now; Promise.race below still sees
    // its rejection once OPEN has succeeded.
    void hello.catch(() => {});
    ws.onmessage = (event) => {
      if (this._ws !== ws) return;
      if (typeof event.data === "string") {
        try {
          const payload = JSON.parse(event.data);
          if (payload.error) {
            helloReject(new Error(payload.error));
          } else if (this._handleEncoderControl(payload)) {
          } else if (payload.type === "media_update") {
            this._enqueueMediaUpdate(payload, ws, callId);
          } else {
            helloResolve(payload);
          }
        } catch (err) {
          helloReject(err);
        }
        return;
      }
      if (this._mediaContract("receive").encoding === "JPEG" && this._canReceive) {
        this._decodeMessage(event.data);
      } else if (!this._decoder || this._decoder.state !== "configured") {
        this._bufferDecodeMessage(event.data);
      } else {
        this._decodeMessage(event.data);
      }
    };
    let openedReject;
    const opened = new Promise((resolve, reject) => {
      openedReject = reject;
      ws.onopen = resolve;
      ws.onerror = () => {
        const error = new Error("SIP video WebSocket failed");
        reject(error);
        helloReject(error);
      };
    });
    ws.onclose = () => {
      const error = new Error("SIP video WebSocket closed before negotiation");
      openedReject(error);
      helloReject(error);
      if (this._ws !== ws) return;
      this._ws = null;
      const cleanupGeneration = ++this._generation;
      void this._cleanupMedia(cleanupGeneration);
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
      this._updateLegacyMediaAliases(negotiated);
      await this._setupCodecs(negotiated, generation);
      if (!this._isCurrent(generation, ws, callId)) return false;
      this._active = true;
      this._emit();
      return true;
    } catch (err) {
      if (!this._isCurrent(generation, ws, callId)) return false;
      await this.close();
      throw err;
    }
  }

  _isCurrent(generation, ws, callId) {
    return generation === this._generation && this._ws === ws && this._callId === callId;
  }

  _mediaContract(direction, negotiated = this._negotiated) {
    return directionalVideoContract(
      negotiated,
      direction,
      this._encoding,
      this._clockRate,
    );
  }

  _updateLegacyMediaAliases(negotiated) {
    const aliases = legacyVideoAliases(
      negotiated,
      this._encoding,
      this._clockRate,
    );
    this._encoding = aliases.encoding;
    this._clockRate = aliases.clockRate;
  }

  _handleEncoderControl(payload) {
    if (payload?.type !== "force_key_frame") return false;
    this._forceCameraKeyFrame = true;
    this._emit();
    return true;
  }

  _sendTxEpoch() {
    const ws = this._ws;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    ws.send(JSON.stringify({ type: "tx_epoch" }));
  }

  _enqueueMediaUpdate(payload, ws, callId) {
    this._mediaUpdatePromise = this._mediaUpdatePromise
      .then(() => this._applyMediaUpdate(payload, ws, callId))
      .catch((err) => {
        if (this._ws === ws) {
          console.warn(`voip-stack-video: media update failed (${err?.message || String(err)})`);
        }
      });
  }

  async _applyMediaUpdate(negotiated, ws, callId) {
    if (this._ws !== ws || this._callId !== callId) return;
    if (negotiated?.restart_required) {
      // The server cannot replace an FFmpeg/direct RTP topology underneath a
      // live owner. Release the old media socket first, then reconnect using
      // the normal ownership handoff and the newly committed SDP generation.
      const expectedClosedGeneration = this._generation + 1;
      const endpointId = this._endpointId;
      await this.close();
      if (
        this._generation !== expectedClosedGeneration ||
        this._ws !== null ||
        this._callId
      ) return false;
      return this.start({
        ...negotiated,
        call_id: callId,
        endpoint_id: endpointId,
        video_active: true,
      });
    }
    const generation = ++this._generation;
    const senderCleanup = this._cleanupSender();
    this._cleanupReceiver();
    this._active = false;
    await senderCleanup;
    if (this._ws !== ws || this._callId !== callId || generation !== this._generation) return;
    this._negotiated = negotiated;
    this._updateLegacyMediaAliases(negotiated);
    this._rtpTimestampBase = null;
    this._rtpTimestampLast = null;
    this._rtpTimestampTicks = 0;
    this._lastRenderedAt = 0;
    this._lastRenderedTimestamp = null;
    this._lastDecodedAt = 0;
    this._lastDecodedTimestamp = null;
    this._encodedFrames = 0;
    this._forceCameraKeyFrame = true;
    this._sendDropUntilKeyFrame = false;
    this._pendingDecode = [];
    this._pendingDecodeBytes = 0;
    this._dropUntilKeyFrame = true;
    this._jpegDecodePending = false;
    this._jpegQueuedBuffer = null;
    this._jpegDecodeToken = null;
    await this._setupCodecs(negotiated, generation);
    if (this._ws !== ws || this._callId !== callId || generation !== this._generation) return;
    this._active = Boolean(this._canReceive || this._canSend || this._cameraAllowed);
    this._emit();
  }

  async _setupCodecs(negotiated, generation) {
    if (generation !== this._generation) {
      throw new Error("SIP video session was superseded");
    }
    const receive = this._mediaContract("receive", negotiated);
    const send = this._mediaContract("send", negotiated);
    const failures = [];
    let usablePaths = 0;
    if (negotiated?.can_receive) {
      try {
        if (receive.encoding === "JPEG") {
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
            codec: receive.codec,
            optimizeForLatency: true,
          });
          if (generation !== this._generation) throw new Error("SIP video session was superseded");
          const support = await VideoDecoder.isConfigSupported(decoderConfig);
          if (generation !== this._generation) throw new Error("SIP video session was superseded");
          if (!support?.supported) throw new Error(`browser cannot decode ${receive.codec}`);
          let decoder;
          decoder = new VideoDecoder({
            output: (frame) => {
              if (generation !== this._generation || this._decoder !== decoder) {
                frame.close();
                return;
              }
              this._queueDecodedFrame(frame);
            },
            error: () => {
              if (generation !== this._generation || this._decoder !== decoder) return;
              this._stats.decode_errors++;
              this._dropUntilKeyFrame = true;
              this._requestKeyFrame();
              this._emit();
            },
          });
          decoder.configure(support.config || decoderConfig);
          if (generation !== this._generation) {
            decoder.close();
            throw new Error("SIP video session was superseded");
          }
          this._decoder = decoder;
          this._canReceive = true;
          this._active = true;
          usablePaths++;
          this._flushPendingDecode();
          this._emit();
        }
      } catch (err) {
        if (generation !== this._generation) throw err;
        this._cleanupReceiver();
        failures.push(`receive: ${err?.message || String(err)}`);
      }
    }
    if (generation !== this._generation) {
      throw new Error("SIP video session was superseded");
    }
    this._cameraAllowed = Boolean(negotiated?.can_send);
    if (this._cameraAllowed && this._cameraEnabled) {
      const setupSender = async () => {
        try {
          await this._ensureSender(send.codec, generation);
          if (
            generation !== this._generation ||
            !this._cameraEnabled ||
            this._encoder?.state !== "configured"
          ) return "superseded";
          this._canSend = true;
          this._active = true;
          this._emit();
          return "";
        } catch (err) {
          if (generation !== this._generation) return "superseded";
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

  async _ensureSender(codec, generation) {
    if (this._encoder?.state === "configured") return;
    if (
      this._senderSetupPromise &&
      this._senderSetupToken === this._senderGeneration
    ) {
      return this._senderSetupPromise;
    }
    const senderGeneration = ++this._senderGeneration;
    const setup = this._setupEncoder(codec, generation, senderGeneration);
    this._senderSetupPromise = setup;
    this._senderSetupToken = senderGeneration;
    try {
      await setup;
    } finally {
      if (this._senderSetupPromise === setup) {
        this._senderSetupPromise = null;
        this._senderSetupToken = 0;
      }
    }
  }

  _senderIsCurrent(generation, senderGeneration) {
    return (
      generation === this._generation &&
      senderGeneration === this._senderGeneration &&
      this._cameraEnabled &&
      this._cameraAllowed
    );
  }

  _cameraCaptureContract() {
    return cameraCaptureContract(this._mediaContract("send"));
  }

  async _setupEncoder(codec, generation, senderGeneration) {
    if (
      typeof VideoEncoder === "undefined" ||
      typeof MediaStreamTrackProcessor === "undefined" ||
      !navigator.mediaDevices?.getUserMedia
    ) {
      throw new Error("Browser cannot send SIP video with WebCodecs");
    }
    let stream = null;
    let encoder = null;
    let reader = null;
    let published = false;
    try {
      const captureContract = this._cameraCaptureContract();
      stream = await navigator.mediaDevices.getUserMedia({
        video: captureContract.constraints,
        audio: false,
      });
      if (!this._senderIsCurrent(generation, senderGeneration)) {
        throw new Error("SIP video session was superseded or camera transmission was disabled");
      }
      const track = stream.getVideoTracks()[0];
      if (!track) throw new Error("No browser camera track available");
      const settings = track.getSettings();
      const width = Math.max(
        16,
        Number(settings.width || captureContract.idealWidth) & ~1,
      );
      const height = Math.max(
        16,
        Number(settings.height || captureContract.idealHeight) & ~1,
      );
      const macroblocks = Math.ceil(width / 16) * Math.ceil(height / 16);
      const framerate = Math.max(
        1,
        Math.min(
          captureContract.maxFr,
          Math.floor(captureContract.maxMbps / Math.max(1, macroblocks)),
          Number(settings.frameRate || captureContract.maxFr),
        ),
      );
      if (macroblocks > captureContract.maxFs) {
        throw new Error("Browser camera exceeds the negotiated SIP video frame size");
      }
      const send = this._mediaContract("send");
      const encoderConfig = await this._supportedConfig(VideoEncoder, {
        codec,
        width,
        height,
        framerate,
        bitrate: 600000,
        latencyMode: "realtime",
        ...(send.encoding === "H264" ? { avc: { format: "annexb" } } : {}),
      });
      if (!this._senderIsCurrent(generation, senderGeneration)) {
        throw new Error("SIP video session was superseded or camera transmission was disabled");
      }
      const support = await VideoEncoder.isConfigSupported(encoderConfig);
      if (!this._senderIsCurrent(generation, senderGeneration)) {
        throw new Error("SIP video session was superseded or camera transmission was disabled");
      }
      if (!support?.supported) {
        throw new Error(`Browser cannot encode negotiated SIP video ${codec}`);
      }
      encoder = new VideoEncoder({
        output: (chunk) => this._sendEncodedChunk(
          chunk,
          generation,
          senderGeneration,
          encoder,
        ),
        error: () => {
          if (
            generation !== this._generation ||
            senderGeneration !== this._senderGeneration ||
            this._encoder !== encoder
          ) return;
          this._stats.dropped++;
          this._emit();
        },
      });
      encoder.configure(support.config || encoderConfig);
      const processor = new MediaStreamTrackProcessor({ track });
      reader = processor.readable.getReader();
      if (!this._senderIsCurrent(generation, senderGeneration)) {
        throw new Error("SIP video session was superseded or camera transmission was disabled");
      }
      // Publish the complete pipeline atomically. Until this point all media
      // resources are locally owned and a superseding call can only make this
      // setup stop and dispose them; it cannot inherit a half-built sender.
      this._cameraStream = stream;
      this._encoder = encoder;
      this._cameraReader = reader;
      this._encodeTask = this._encodeCamera(
        framerate,
        reader,
        encoder,
        generation,
        senderGeneration,
      );
      published = true;
      // A newly created WebCodecs encoder may restart its timestamp epoch.
      // WebSocket ordering applies the reset before its first access unit.
      this._sendTxEpoch();
    } finally {
      if (!published) {
        if (reader) await reader.cancel().catch(() => {});
        if (encoder && encoder.state !== "closed") encoder.close();
        if (stream) stream.getTracks().forEach((track) => track.stop());
      }
    }
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

  async _encodeCamera(framerate, reader, encoder, generation, senderGeneration) {
    const keyInterval = Math.max(1, Math.round(framerate * 2));
    try {
      while (
        this._senderIsCurrent(generation, senderGeneration) &&
        this._cameraReader === reader &&
        this._encoder === encoder &&
        encoder.state === "configured"
      ) {
        const { value: frame, done } = await reader.read();
        if (done || !frame) break;
        try {
          if (
            !this._senderIsCurrent(generation, senderGeneration) ||
            this._cameraReader !== reader ||
            this._encoder !== encoder
          ) {
            break;
          }
          if (encoder.encodeQueueSize > 2) {
            this._stats.dropped++;
          } else {
            const keyFrame = this._forceCameraKeyFrame || this._encodedFrames % keyInterval === 0;
            encoder.encode(frame, { keyFrame });
            this._forceCameraKeyFrame = false;
            this._encodedFrames++;
          }
        } finally {
          frame.close();
        }
      }
    } catch (_) {
      if (
        generation === this._generation &&
        senderGeneration === this._senderGeneration &&
        this._active
      ) this._stats.dropped++;
    } finally {
      // A camera track can end without an explicit card action (USB removal,
      // browser privacy revocation, laptop sleep). Do not keep advertising a
      // live browser sender or retain its stream/encoder after EOF.
      if (
        generation === this._generation &&
        senderGeneration === this._senderGeneration &&
        this._cameraReader === reader &&
        this._encoder === encoder
      ) {
        this._cameraReader = null;
        this._encoder = null;
        this._cameraStream?.getTracks?.().forEach((track) => track.stop());
        this._cameraStream = null;
        this._encodeTask = null;
        this._canSend = false;
        if (encoder.state !== "closed") {
          try { encoder.close(); } catch (_) {}
        }
        this._emit();
      }
    }
  }

  _sendEncodedChunk(
    chunk,
    generation = this._generation,
    senderGeneration = this._senderGeneration,
    encoder = this._encoder,
  ) {
    if (
      generation !== this._generation ||
      senderGeneration !== this._senderGeneration ||
      (encoder && this._encoder !== encoder)
    ) return;
    const ws = this._ws;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    if (ws.bufferedAmount > MAX_VIDEO_WS_BUFFER) {
      this._stats.dropped++;
      this._sendDropUntilKeyFrame = true;
      this._forceCameraKeyFrame = true;
      return;
    }
    if (this._sendDropUntilKeyFrame && chunk.type !== "key") {
      this._stats.dropped++;
      this._forceCameraKeyFrame = true;
      return;
    }
    if (chunk.type === "key") this._sendDropUntilKeyFrame = false;
    const payload = new Uint8Array(chunk.byteLength);
    chunk.copyTo(payload);
    const frame = new Uint8Array(VIDEO_HEADER_BYTES + payload.byteLength);
    const view = new DataView(frame.buffer);
    frame[0] = VIDEO_ACCESS_UNIT;
    frame[1] = chunk.type === "key" ? 1 : 0;
    const rtpTimestamp = Math.round(
      Number(chunk.timestamp || 0) * this._mediaContract("send").clockRate / 1000000,
    ) >>> 0;
    view.setUint32(2, rtpTimestamp, false);
    frame.set(payload, VIDEO_HEADER_BYTES);
    ws.send(frame);
    this._stats.sent++;
    if ((this._stats.sent & 31) === 0) this._emit();
  }

  _decodeMessage(buffer) {
    if (this._mediaContract("receive").encoding === "JPEG") {
      this._decodeJpegMessage(buffer);
      return;
    }
    if (!this._decoder || this._decoder.state !== "configured") return;
    const bytes = new Uint8Array(buffer);
    if (bytes.byteLength <= VIDEO_HEADER_BYTES || bytes[0] !== VIDEO_ACCESS_UNIT) return;
    const keyFrame = Boolean(bytes[1] & 1);
    if (this._dropUntilKeyFrame && !keyFrame) {
      this._stats.dropped++;
      this._stats.dropped_decode_backpressure++;
      return;
    }
    if (!keyFrame && this._decoder.decodeQueueSize >= MAX_DECODE_QUEUE_FRAMES) {
      // Encoded delta frames are interdependent. Once latency pressure makes
      // us discard one, discard the rest of that GOP and ask the SIP sender
      // for a fresh key frame instead of growing WebCodecs' queue without a
      // bound or rendering a corrupted dependency chain.
      this._dropUntilKeyFrame = true;
      this._stats.dropped++;
      this._stats.dropped_decode_backpressure++;
      this._requestKeyFrame();
      return;
    }
    if (keyFrame) this._dropUntilKeyFrame = false;
    const rtpTimestamp = new DataView(buffer).getUint32(2, false);
    const timestamp = this._unwrapRtpTimestamp(rtpTimestamp);
    try {
      this._decoder.decode(new EncodedVideoChunk({
        type: keyFrame ? "key" : "delta",
        timestamp,
        data: bytes.subarray(VIDEO_HEADER_BYTES),
      }));
      this._stats.received++;
      if ((this._stats.received & 31) === 0) this._emit();
    } catch (_) {
      this._stats.decode_errors++;
      this._dropUntilKeyFrame = true;
      this._requestKeyFrame();
    }
  }

  _requestKeyFrame() {
    const ws = this._ws;
    if (!ws || ws.readyState !== WebSocket.OPEN) return;
    try { ws.send(JSON.stringify({ type: "request_key_frame" })); } catch (_) {}
  }

  _decodeJpegMessage(buffer) {
    if (this._jpegDecodePending) {
      this._jpegQueuedBuffer = buffer;
      this._stats.dropped++;
      this._stats.dropped_render_coalesce++;
      return;
    }
    const bytes = new Uint8Array(buffer);
    if (bytes.byteLength <= VIDEO_HEADER_BYTES || bytes[0] !== VIDEO_ACCESS_UNIT) return;
    const rtpTimestamp = new DataView(buffer).getUint32(2, false);
    const timestamp = this._unwrapRtpTimestamp(rtpTimestamp);
    const generation = this._generation;
    const token = {};
    const payload = bytes.slice(VIDEO_HEADER_BYTES);
    this._jpegDecodePending = true;
    this._jpegDecodeToken = token;
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
    }).finally(() => {
      // A decoder Promise from a superseded call can settle after the next
      // call has already started decoding. It must not clear that call's
      // pending flag or consume its coalesced frame.
      if (generation !== this._generation || this._jpegDecodeToken !== token) return;
      this._jpegDecodePending = false;
      this._jpegDecodeToken = null;
      const latest = this._jpegQueuedBuffer;
      this._jpegQueuedBuffer = null;
      if (latest) this._decodeJpegMessage(latest);
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
    // An empty buffer does not prove decoder synchronisation. Keep dropping
    // deltas until a real key frame is observed after codec setup/update.
    if (!pending.length) return;
    this._dropUntilKeyFrame = false;
    for (const buffer of pending) this._decodeMessage(buffer);
  }

  _unwrapRtpTimestamp(value) {
    if (this._rtpTimestampBase === null) {
      this._rtpTimestampBase = value;
      this._rtpTimestampLast = value;
      this._rtpTimestampTicks = 0;
      return 0;
    }
    let delta = (value - this._rtpTimestampLast) >>> 0;
    if (delta >= 0x80000000) delta -= 0x100000000;
    this._rtpTimestampTicks += delta;
    this._rtpTimestampLast = value;
    return Math.round(
      this._rtpTimestampTicks * 1000000 / this._mediaContract("receive").clockRate,
    );
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
    if (!this._drawFrame(frame)) {
      this._stats.dropped++;
      this._stats.dropped_no_canvas++;
      return;
    }
    if (this._lastRenderedAt) {
      const gap = Math.round(now - this._lastRenderedAt);
      this._stats.max_frame_gap_ms = Math.max(this._stats.max_frame_gap_ms, gap);
      if (gap > 100) this._stats.render_gaps_over_100_ms++;
      if (gap > 250) this._stats.render_gaps_over_250_ms++;
    }
    this._lastRenderedAt = now;
    this._lastRenderedTimestamp = timestamp;
    const firstRenderedFrame = this._stats.rendered === 0;
    this._stats.rendered++;
    if (firstRenderedFrame) this._emit();
  }

  _drawFrame(frame) {
    try {
      const canvas = this._canvas;
      if (!canvas) return false;
      const width = frame.displayWidth || frame.codedWidth;
      const height = frame.displayHeight || frame.codedHeight;
      if (canvas.width !== width || canvas.height !== height) {
        canvas.width = width;
        canvas.height = height;
      }
      const context = canvas.getContext("2d", { alpha: false });
      if (!context) return false;
      context.drawImage(frame.bitmap || frame, 0, 0, width, height);
      return true;
    } finally {
      frame.close();
    }
  }

  async close() {
    const generation = ++this._generation;
    // Do not let a camera prompt or codec probe from an old dialog serialize
    // media updates belonging to the next WebSocket/call.
    this._mediaUpdatePromise = Promise.resolve();
    const ws = this._ws;
    this._ws = null;
    if (ws && [WebSocket.OPEN, WebSocket.CONNECTING].includes(ws.readyState)) {
      try { ws.close(); } catch (_) {}
    }
    await this._cleanupMedia(generation);
  }

  async _cleanupMedia(generation = this._generation) {
    const senderCleanup = this._cleanupSender();
    this._cleanupReceiver();
    if (generation === this._generation) {
      this._callId = "";
      this._endpointId = "default";
      this._active = false;
      this._canReceive = false;
      this._canSend = false;
      this._rtpTimestampBase = null;
      this._rtpTimestampLast = null;
      this._rtpTimestampTicks = 0;
      this._encodedFrames = 0;
      this._forceCameraKeyFrame = true;
      this._sendDropUntilKeyFrame = false;
      this._pendingDecode = [];
      this._pendingDecodeBytes = 0;
      this._dropUntilKeyFrame = false;
      this._jpegDecodePending = false;
      this._jpegQueuedBuffer = null;
      this._jpegDecodeToken = null;
      this._negotiated = null;
      this._cameraAllowed = false;
      this._lastRenderedAt = 0;
      this._lastRenderedTimestamp = null;
      this._lastDecodedAt = 0;
      this._lastDecodedTimestamp = null;
      this._emit();
    }
    await senderCleanup;
  }

  async _cleanupSender() {
    // Invalidate in-flight permission/config probes and detach all published
    // resources before the first await. A later call can then create its own
    // sender while this call finishes cancelling its old reader/task.
    this._senderGeneration++;
    this._senderSetupPromise = null;
    this._senderSetupToken = 0;
    const reader = this._cameraReader;
    const encodeTask = this._encodeTask;
    const encoder = this._encoder;
    const stream = this._cameraStream;
    this._cameraReader = null;
    this._encodeTask = null;
    this._encoder = null;
    this._cameraStream = null;
    this._canSend = false;
    let readerCancel = Promise.resolve();
    if (reader) {
      try {
        readerCancel = Promise.resolve(reader.cancel()).catch(() => {});
      } catch (_) {}
    }
    if (encoder && encoder.state !== "closed") {
      try { encoder.close(); } catch (_) {}
    }
    if (stream) stream.getTracks().forEach((track) => track.stop());
    await Promise.all([
      readerCancel,
      encodeTask ? Promise.resolve(encodeTask).catch(() => {}) : Promise.resolve(),
    ]);
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
