const PCM_FORMATS = Object.freeze(["s16le", "s24le", "s24le_in_s32", "s32le"]);
const FRAME_MS = Object.freeze([10, 16, 20, 32]);
const TX_BUFFER_POOL = 4;

function normaliseFormat(value) {
  if (!value) throw new Error("recorder worklet requires negotiated PCM format");
  const sampleRate = Number(value.sampleRate);
  const frameMs = Number(value.frameMs);
  const channels = Number(value.channels);
  const pcmFormat = value.pcmFormat;
  if (!Number.isFinite(sampleRate) || !Number.isFinite(frameMs) || !Number.isFinite(channels)) {
    throw new Error("recorder worklet PCM format has invalid numeric fields");
  }
  if (!PCM_FORMATS.includes(pcmFormat)) throw new Error(`recorder worklet unsupported PCM format ${pcmFormat}`);
  if (![1, 2].includes(channels)) throw new Error(`recorder worklet unsupported channel count ${channels}`);
  if (!FRAME_MS.includes(frameMs)) throw new Error(`recorder worklet unsupported frame_ms ${frameMs}`);
  if ((sampleRate * frameMs) % 1000 !== 0) throw new Error("recorder worklet PCM format does not form whole frames");
  const bytesPerSample = pcmFormat === "s16le" ? 2 : pcmFormat === "s24le" ? 3 : 4;
  return {
    sampleRate,
    frameMs,
    channels,
    pcmFormat,
    bytesPerSample,
    frameSamples: Math.floor((sampleRate * frameMs) / 1000),
  };
}

class RecorderProcessor extends AudioWorkletProcessor {
  constructor(options) {
    super();
    this._format = normaliseFormat(options?.processorOptions?.format);
    this._frameBytes = this._format.frameSamples * this._format.channels * this._format.bytesPerSample;
    this._buffers = Array.from({ length: TX_BUFFER_POOL }, () => new ArrayBuffer(this._frameBytes));
    this._views = this._buffers.map((buffer) => new DataView(buffer));
    this._bufferIndex = 0;
    this._buffer = this._buffers[this._bufferIndex];
    this._view = this._views[this._bufferIndex];
    this._writeSample = 0;
    this._position = 0;
    this._lastSample = 0;
  }

  _encode(sample, sampleIndex) {
    const s = Math.max(-1, Math.min(1, sample));
    const offset = sampleIndex * this._format.bytesPerSample;
    if (this._format.pcmFormat === "s16le") {
      this._view.setInt16(offset, s < 0 ? s * 0x8000 : s * 0x7fff, true);
    } else if (this._format.pcmFormat === "s24le") {
      const v = Math.trunc(s < 0 ? s * 0x800000 : s * 0x7fffff);
      this._view.setUint8(offset, v & 0xff);
      this._view.setUint8(offset + 1, (v >> 8) & 0xff);
      this._view.setUint8(offset + 2, (v >> 16) & 0xff);
    } else if (this._format.pcmFormat === "s24le_in_s32") {
      this._view.setInt32(offset, Math.trunc(s < 0 ? s * 0x80000000 : s * 0x7fffff00), true);
    } else {
      this._view.setInt32(offset, Math.trunc(s < 0 ? s * 0x80000000 : s * 0x7fffffff), true);
    }
  }

  _writeMono(sample) {
    for (let ch = 0; ch < this._format.channels; ch++) {
      this._encode(sample, this._writeSample * this._format.channels + ch);
    }
    this._writeSample++;
    if (this._writeSample !== this._format.frameSamples) return;

    const frame = this._buffer;
    this.port.postMessage({ type: "audio", buffer: frame });
    this._bufferIndex = (this._bufferIndex + 1) % this._buffers.length;
    this._buffer = this._buffers[this._bufferIndex];
    this._view = this._views[this._bufferIndex];
    this._writeSample = 0;
  }

  process(inputList) {
    const input = inputList?.[0]?.[0];
    if (!input?.length) return true;

    const ratio = sampleRate / this._format.sampleRate;
    if (ratio === 1) {
      for (let i = 0; i < input.length; i++) this._writeMono(input[i]);
    } else {
      while (this._position < input.length) {
        const idx = Math.floor(this._position);
        const frac = this._position - idx;
        const a = idx > 0 ? input[idx - 1] : this._lastSample;
        const b = input[idx] ?? a;
        this._writeMono(a + (b - a) * frac);
        this._position += ratio;
      }
      this._position -= input.length;
      this._lastSample = input[input.length - 1] || this._lastSample;
    }
    return true;
  }
}

registerProcessor("voip-stack-processor", RecorderProcessor);
