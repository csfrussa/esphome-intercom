#include "i2s_audio_duplex.h"

#ifdef USE_ESP32

#include <esp_timer.h>
#include <esp_heap_caps.h>
#include <algorithm>
#include <cmath>
#include <cstring>

#include "esphome/core/hal.h"
#include "esphome/core/log.h"

#ifdef USE_AUDIO_PROCESSOR
#include "../audio_processor/audio_processor.h"
#include "../audio_processor/log_utils.h"
#endif
#include "../audio_processor/audio_utils.h"
#include "../audio_processor/ring_buffer_caps.h"

namespace esphome {
namespace i2s_audio_duplex {

static const char *const TAG = "i2s_duplex";

// I2S new driver uses milliseconds directly, NOT FreeRTOS ticks
static const uint32_t I2S_IO_TIMEOUT_MS = 50;
// Default frame size when no AudioProcessor is attached.
static const size_t DEFAULT_FRAME_SIZE = 256;
static constexpr size_t AUDIO_BUFFER_ALIGN = 16;

static inline bool alloc_i16_buffer(int16_t **buffer, size_t *buffer_bytes,
                                    size_t bytes, uint32_t caps, bool aligned) {
  if (bytes == 0) {
    return true;
  }
  *buffer = static_cast<int16_t *>(aligned ? heap_caps_aligned_alloc(AUDIO_BUFFER_ALIGN, bytes, caps)
                                           : heap_caps_malloc(bytes, caps));
  if (*buffer == nullptr) {
    return false;
  }
  if (buffer_bytes != nullptr) {
    *buffer_bytes = bytes;
  }
  return true;
}

static inline void free_i16_buffer(int16_t **buffer, size_t *buffer_bytes = nullptr) {
  if (*buffer != nullptr) {
    heap_caps_free(*buffer);
    *buffer = nullptr;
  }
  if (buffer_bytes != nullptr) {
    *buffer_bytes = 0;
  }
}

size_t I2SAudioDuplex::get_mic_callback_buffer_size() const {
  size_t samples = DEFAULT_FRAME_SIZE;
#ifdef USE_AUDIO_PROCESSOR
  const audio_processor::FrameSpec fallback_spec{};
  samples = std::max(samples, std::max(fallback_spec.input_samples, fallback_spec.output_samples));
  if (this->processor_ != nullptr) {
    const auto spec = this->processor_->frame_spec();
    if (spec.input_samples > 0) {
      samples = std::max(samples, spec.input_samples);
    }
    if (spec.output_samples > 0) {
      samples = std::max(samples, spec.output_samples);
    }
  }
#endif
  return samples * sizeof(int16_t);
}

#ifdef USE_DUPLEX_DEBUG_PROBE
static constexpr uint32_t DBG_FLAG_RX_BAD = 1u << 0;
static constexpr uint32_t DBG_FLAG_SPK_UNDERRUN = 1u << 1;
static constexpr uint32_t DBG_FLAG_TRIGGER = 1u << 2;

static uint16_t abs_delta_u16(int32_t a, int32_t b) {
  int32_t d = a - b;
  if (d < 0)
    d = -d;
  return d > UINT16_MAX ? UINT16_MAX : static_cast<uint16_t>(d);
}

static int16_t rms_db10_from_sumsq(uint64_t sumsq, size_t n) {
  if (n == 0 || sumsq == 0)
    return -1200;
  const float rms = std::sqrt(static_cast<float>(sumsq) / static_cast<float>(n));
  const float db = 20.0f * std::log10(rms / 32768.0f);
  int32_t db10 = static_cast<int32_t>(std::lround(db * 10.0f));
  if (db10 < -1200)
    db10 = -1200;
  if (db10 > 60)
    db10 = 60;
  return static_cast<int16_t>(db10);
}

bool I2SAudioDuplex::debug_probe_init_() {
  if (!this->debug_probe_enabled_)
    return true;
  if (this->debug_probe_ring_ != nullptr)
    return true;

  if (this->debug_probe_dump_frames_ > this->debug_probe_frames_) {
    this->debug_probe_dump_frames_ = this->debug_probe_frames_;
  }

  const size_t bytes = static_cast<size_t>(this->debug_probe_frames_) * sizeof(DebugProbeFrame);
  this->debug_probe_ring_ = static_cast<DebugProbeFrame *>(
      heap_caps_calloc(this->debug_probe_frames_, sizeof(DebugProbeFrame),
                       MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT));
  if (this->debug_probe_ring_ == nullptr) {
    this->debug_probe_ring_ = static_cast<DebugProbeFrame *>(
        heap_caps_calloc(this->debug_probe_frames_, sizeof(DebugProbeFrame),
                         MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT));
  }
  if (this->debug_probe_ring_ == nullptr) {
    ESP_LOGE(TAG, "Debug probe allocation failed (%u bytes)", (unsigned) bytes);
    return false;
  }

  this->debug_probe_capacity_ = this->debug_probe_frames_;
  this->debug_probe_write_seq_ = 0;
  this->debug_probe_last_trigger_seq_ = 0;
  memset(this->debug_probe_prev_valid_, 0, sizeof(this->debug_probe_prev_valid_));
  ESP_LOGI(TAG, "Debug probe armed (%u frames, %u bytes, trigger_delta=%u)",
           (unsigned) this->debug_probe_frames_, (unsigned) bytes,
           (unsigned) this->debug_probe_trigger_delta_);
  return true;
}

I2SAudioDuplex::DebugProbeMetric I2SAudioDuplex::debug_probe_metric_i16_(
    I2SAudioDuplex::DebugProbeStream stream, const int16_t *data, size_t n, size_t stride) {
  DebugProbeMetric m{};
  if (data == nullptr || n == 0 || stride == 0) {
    return m;
  }

  int16_t prev = data[0];
  m.first_sample = prev;
  uint64_t sumsq = 0;
  for (size_t i = 0; i < n; i++) {
    int16_t sample = data[i * stride];
    const int32_t s = sample;
    const uint16_t abs_s = static_cast<uint16_t>(s < 0 ? -s : s);
    if (abs_s > m.peak)
      m.peak = abs_s;
    if (i > 0) {
      uint16_t d = abs_delta_u16(sample, prev);
      if (d > m.max_delta)
        m.max_delta = d;
    }
    prev = sample;
    sumsq += static_cast<uint64_t>(s * s);
  }
  m.last_sample = prev;
  if (this->debug_probe_prev_valid_[stream]) {
    m.boundary_delta = abs_delta_u16(m.first_sample, this->debug_probe_prev_last_[stream]);
  }
  this->debug_probe_prev_last_[stream] = m.last_sample;
  this->debug_probe_prev_valid_[stream] = true;
  m.rms_db10 = rms_db10_from_sumsq(sumsq, n);
  return m;
}

I2SAudioDuplex::DebugProbeMetric I2SAudioDuplex::debug_probe_metric_i32_top16_(
    I2SAudioDuplex::DebugProbeStream stream, const int32_t *data, size_t n, size_t stride) {
  DebugProbeMetric m{};
  if (data == nullptr || n == 0 || stride == 0) {
    return m;
  }

  int16_t prev = static_cast<int16_t>(data[0] >> 16);
  m.first_sample = prev;
  uint64_t sumsq = 0;
  for (size_t i = 0; i < n; i++) {
    int16_t sample = static_cast<int16_t>(data[i * stride] >> 16);
    const int32_t s = sample;
    const uint16_t abs_s = static_cast<uint16_t>(s < 0 ? -s : s);
    if (abs_s > m.peak)
      m.peak = abs_s;
    if (i > 0) {
      uint16_t d = abs_delta_u16(sample, prev);
      if (d > m.max_delta)
        m.max_delta = d;
    }
    prev = sample;
    sumsq += static_cast<uint64_t>(s * s);
  }
  m.last_sample = prev;
  if (this->debug_probe_prev_valid_[stream]) {
    m.boundary_delta = abs_delta_u16(m.first_sample, this->debug_probe_prev_last_[stream]);
  }
  this->debug_probe_prev_last_[stream] = m.last_sample;
  this->debug_probe_prev_valid_[stream] = true;
  m.rms_db10 = rms_db10_from_sumsq(sumsq, n);
  return m;
}

void I2SAudioDuplex::debug_probe_record_(const AudioTaskCtx &ctx) {
  if (!this->debug_probe_enabled_ || this->debug_probe_ring_ == nullptr || this->debug_probe_capacity_ == 0)
    return;

  DebugProbeFrame frame{};
  frame.seq = ++this->debug_probe_write_seq_;
  frame.timestamp_us = esp_timer_get_time();
  frame.flags = ctx.debug_flags | (ctx.speaker_underrun ? DBG_FLAG_SPK_UNDERRUN : 0);
  frame.i2s_read_us = ctx.debug_i2s_read_us;
  frame.rx_us = ctx.debug_rx_us;
  frame.process_us = ctx.debug_process_us;
  frame.tx_us = ctx.debug_tx_us;
  frame.frame_us = ctx.debug_frame_us;

  if (ctx.rx_buffer != nullptr) {
    if (ctx.use_tdm_ref) {
      const size_t raw_slots = std::min<size_t>(ctx.tdm_total_slots, 4);
      for (size_t slot = 0; slot < raw_slots; slot++) {
        DebugProbeStream stream = static_cast<DebugProbeStream>(DBG_RAW0 + slot);
        if (ctx.i2s_bps == 4 && ctx.ratio > 1) {
          frame.metrics[stream] = this->debug_probe_metric_i32_top16_(
              stream, reinterpret_cast<const int32_t *>(ctx.rx_buffer) + slot,
              ctx.bus_frame_size, ctx.tdm_total_slots);
        } else {
          frame.metrics[stream] = this->debug_probe_metric_i16_(
              stream, ctx.rx_buffer + slot, ctx.bus_frame_size, ctx.tdm_total_slots);
        }
      }
    } else {
      if (ctx.i2s_bps == 4 && ctx.ratio > 1) {
        frame.metrics[DBG_RAW0] = this->debug_probe_metric_i32_top16_(
            DBG_RAW0, reinterpret_cast<const int32_t *>(ctx.rx_buffer), ctx.bus_frame_size, 1);
      } else {
        frame.metrics[DBG_RAW0] = this->debug_probe_metric_i16_(
            DBG_RAW0, ctx.rx_buffer, ctx.bus_frame_size, 1);
      }
    }
  }

  frame.metrics[DBG_MIC1] = this->debug_probe_metric_i16_(DBG_MIC1, ctx.mic_buffer, ctx.input_frame_size, 1);
  if (ctx.processor_mic_channels > 1 && ctx.processor_mic_buffer != nullptr) {
    frame.metrics[DBG_MIC2] = this->debug_probe_metric_i16_(DBG_MIC2, ctx.processor_mic_buffer + 1,
                                                            ctx.input_frame_size, 2);
  }
  frame.metrics[DBG_REF] = this->debug_probe_metric_i16_(DBG_REF, ctx.spk_ref_buffer, ctx.input_frame_size, 1);
  frame.metrics[DBG_OUT] = this->debug_probe_metric_i16_(DBG_OUT, ctx.output_buffer,
                                                         ctx.current_output_frame_size, 1);
  frame.metrics[DBG_SPK] = this->debug_probe_metric_i16_(DBG_SPK, ctx.spk_buffer, ctx.bus_frame_size, 1);

  bool trigger = (frame.flags & (DBG_FLAG_RX_BAD | DBG_FLAG_SPK_UNDERRUN)) != 0;
  for (uint8_t i = 0; i < DBG_STREAM_COUNT; i++) {
    const auto &m = frame.metrics[i];
    if (m.max_delta >= this->debug_probe_trigger_delta_ ||
        m.boundary_delta >= this->debug_probe_trigger_delta_) {
      trigger = true;
      break;
    }
  }

  if (trigger) {
    frame.flags |= DBG_FLAG_TRIGGER;
  }

  const uint16_t index = (frame.seq - 1) % this->debug_probe_capacity_;
  this->debug_probe_ring_[index] = frame;

  if (trigger &&
      (this->debug_probe_last_trigger_seq_ == 0 ||
       frame.seq - this->debug_probe_last_trigger_seq_ >= this->debug_probe_cooldown_frames_)) {
    this->debug_probe_last_trigger_seq_ = frame.seq;
    this->debug_probe_dump_("trigger", frame.seq);
  }
}

void I2SAudioDuplex::debug_probe_dump(const char *reason) {
  this->debug_probe_dump_(reason, this->debug_probe_write_seq_);
}

void I2SAudioDuplex::debug_probe_dump_(const char *reason, uint32_t trigger_seq) {
  if (!this->debug_probe_enabled_ || this->debug_probe_ring_ == nullptr || this->debug_probe_capacity_ == 0) {
    ESP_LOGW(TAG, "Debug probe dump requested but probe is not initialized");
    return;
  }

  const uint32_t latest = this->debug_probe_write_seq_;
  if (latest == 0) {
    ESP_LOGW(TAG, "Debug probe dump requested before any audio frame");
    return;
  }

  const uint32_t available = std::min<uint32_t>(latest, this->debug_probe_capacity_);
  const uint32_t dump_count = std::min<uint32_t>(available, this->debug_probe_dump_frames_);
  const uint32_t start = latest >= dump_count ? latest - dump_count + 1 : 1;

  ESP_LOGW(TAG, "DBG_PROBE dump reason=%s latest=%u trigger=%u frames=%u threshold=%u",
           reason, (unsigned) latest, (unsigned) trigger_seq, (unsigned) dump_count,
           (unsigned) this->debug_probe_trigger_delta_);
  ESP_LOGW(TAG, "DBG_PROBE format: stream=rms_db/peak/max_delta/boundary_delta");

  for (uint32_t seq = start; seq <= latest; seq++) {
    const auto &f = this->debug_probe_ring_[(seq - 1) % this->debug_probe_capacity_];
    if (f.seq != seq)
      continue;
    ESP_LOGW(TAG,
             "DBG[%u] flags=0x%02x us(frame=%u read=%u rx=%u proc=%u tx=%u) "
             "raw0=%.1f/%u/%u/%u raw1=%.1f/%u/%u/%u raw2=%.1f/%u/%u/%u raw3=%.1f/%u/%u/%u",
             (unsigned) f.seq, (unsigned) f.flags,
             (unsigned) f.frame_us, (unsigned) f.i2s_read_us, (unsigned) f.rx_us,
             (unsigned) f.process_us, (unsigned) f.tx_us,
             f.metrics[DBG_RAW0].rms_db10 / 10.0f, (unsigned) f.metrics[DBG_RAW0].peak,
             (unsigned) f.metrics[DBG_RAW0].max_delta, (unsigned) f.metrics[DBG_RAW0].boundary_delta,
             f.metrics[DBG_RAW1].rms_db10 / 10.0f, (unsigned) f.metrics[DBG_RAW1].peak,
             (unsigned) f.metrics[DBG_RAW1].max_delta, (unsigned) f.metrics[DBG_RAW1].boundary_delta,
             f.metrics[DBG_RAW2].rms_db10 / 10.0f, (unsigned) f.metrics[DBG_RAW2].peak,
             (unsigned) f.metrics[DBG_RAW2].max_delta, (unsigned) f.metrics[DBG_RAW2].boundary_delta,
             f.metrics[DBG_RAW3].rms_db10 / 10.0f, (unsigned) f.metrics[DBG_RAW3].peak,
             (unsigned) f.metrics[DBG_RAW3].max_delta, (unsigned) f.metrics[DBG_RAW3].boundary_delta);
    ESP_LOGW(TAG,
             "DBG[%u] mic1=%.1f/%u/%u/%u mic2=%.1f/%u/%u/%u ref=%.1f/%u/%u/%u "
             "out=%.1f/%u/%u/%u spk=%.1f/%u/%u/%u",
             (unsigned) f.seq,
             f.metrics[DBG_MIC1].rms_db10 / 10.0f, (unsigned) f.metrics[DBG_MIC1].peak,
             (unsigned) f.metrics[DBG_MIC1].max_delta, (unsigned) f.metrics[DBG_MIC1].boundary_delta,
             f.metrics[DBG_MIC2].rms_db10 / 10.0f, (unsigned) f.metrics[DBG_MIC2].peak,
             (unsigned) f.metrics[DBG_MIC2].max_delta, (unsigned) f.metrics[DBG_MIC2].boundary_delta,
             f.metrics[DBG_REF].rms_db10 / 10.0f, (unsigned) f.metrics[DBG_REF].peak,
             (unsigned) f.metrics[DBG_REF].max_delta, (unsigned) f.metrics[DBG_REF].boundary_delta,
             f.metrics[DBG_OUT].rms_db10 / 10.0f, (unsigned) f.metrics[DBG_OUT].peak,
             (unsigned) f.metrics[DBG_OUT].max_delta, (unsigned) f.metrics[DBG_OUT].boundary_delta,
             f.metrics[DBG_SPK].rms_db10 / 10.0f, (unsigned) f.metrics[DBG_SPK].peak,
             (unsigned) f.metrics[DBG_SPK].max_delta, (unsigned) f.metrics[DBG_SPK].boundary_delta);
  }
}
#endif  // USE_DUPLEX_DEBUG_PROBE

void I2SAudioDuplex::release_audio_buffers_() {
  free_i16_buffer(&this->prealloc_rx_buffer_, &this->prealloc_rx_buffer_bytes_);
  free_i16_buffer(&this->prealloc_mic_buffer_, &this->prealloc_mic_buffer_bytes_);
  free_i16_buffer(&this->prealloc_processor_mic_buffer_, &this->prealloc_processor_mic_buffer_bytes_);
  free_i16_buffer(&this->prealloc_spk_buffer_, &this->prealloc_spk_buffer_bytes_);
  free_i16_buffer(&this->prealloc_spk_ref_buffer_, &this->prealloc_spk_ref_buffer_bytes_);
  free_i16_buffer(&this->prealloc_aec_output_, &this->prealloc_aec_output_bytes_);
  free_i16_buffer(&this->prealloc_tdm_tx_buffer_, &this->prealloc_tdm_tx_buffer_bytes_);
  free_i16_buffer(&this->direct_aec_ref_);
  this->direct_aec_ref_valid_ = false;
  this->aec_ref_ring_buffer_.reset();

  this->audio_buffers_allocated_ = false;
}

bool I2SAudioDuplex::allocate_audio_buffers_(AudioTaskCtx &ctx) {
  const uint32_t buf_caps = this->buffers_in_psram_
      ? (MALLOC_CAP_SPIRAM | MALLOC_CAP_8BIT)
      : (MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT);

  // Worst-case processor mic channels: 2 if dual-mic TDM is available, else 1.
  // Allocating for 2ch unconditionally when dual-mic is possible lets the task
  // flip between MR (1 mic) and MMR (2 mic) without reallocating on reconfigure.
  const uint8_t worst_mic_ch = (this->tdm_second_mic_slot_ >= 0) ? 2 : 1;
  bool processor_buffers_needed = false;
#ifdef USE_AUDIO_PROCESSOR
  // Allocate for the processor once its frame shape is known, even if AFE
  // feed/fetch workers are still parked. This moves heap pressure out of the
  // first media/call activation without starting I2S or esp-sr work.
  processor_buffers_needed = this->processor_ != nullptr && ctx.processor_spec_loaded;
#endif
  const size_t rx_bytes = ctx.rx_frame_bytes;
  const size_t mic_bytes = ctx.mic_separate ? ctx.input_frame_bytes : 0;
  const size_t processor_mic_bytes =
      (processor_buffers_needed && worst_mic_ch > 1) ? ctx.input_frame_bytes * worst_mic_ch : 0;
  const size_t spk_bytes = ctx.bus_frame_size * ctx.num_ch * ctx.i2s_bps;
  const size_t spk_ref_bytes =
      (ctx.use_stereo_aec_ref || ctx.use_tdm_ref || processor_buffers_needed) ? ctx.input_frame_bytes : 0;
  const size_t aec_output_bytes = processor_buffers_needed ? ctx.output_frame_bytes : 0;
  const size_t tdm_tx_bytes =
      ctx.use_tdm_ref ? ctx.bus_frame_size * ctx.tdm_total_slots * ctx.i2s_bps : 0;

  if (this->audio_buffers_allocated_) {
    const bool fits =
        this->prealloc_rx_buffer_ != nullptr && this->prealloc_rx_buffer_bytes_ >= rx_bytes &&
        (!ctx.mic_separate ||
         (this->prealloc_mic_buffer_ != nullptr && this->prealloc_mic_buffer_bytes_ >= mic_bytes)) &&
        (processor_mic_bytes == 0 ||
         (this->prealloc_processor_mic_buffer_ != nullptr &&
          this->prealloc_processor_mic_buffer_bytes_ >= processor_mic_bytes)) &&
        this->prealloc_spk_buffer_ != nullptr && this->prealloc_spk_buffer_bytes_ >= spk_bytes &&
        (spk_ref_bytes == 0 ||
         (this->prealloc_spk_ref_buffer_ != nullptr &&
          this->prealloc_spk_ref_buffer_bytes_ >= spk_ref_bytes)) &&
        (aec_output_bytes == 0 ||
         (this->prealloc_aec_output_ != nullptr &&
          this->prealloc_aec_output_bytes_ >= aec_output_bytes)) &&
        (tdm_tx_bytes == 0 ||
         (this->prealloc_tdm_tx_buffer_ != nullptr &&
          this->prealloc_tdm_tx_buffer_bytes_ >= tdm_tx_bytes));
    if (fits) {
      return true;
    }
    ESP_LOGI(TAG, "Audio buffer shape changed; reallocating buffers");
    this->release_audio_buffers_();
  }

  alloc_i16_buffer(&this->prealloc_rx_buffer_, &this->prealloc_rx_buffer_bytes_,
                   rx_bytes, buf_caps, false);

  if (ctx.mic_separate) {
    alloc_i16_buffer(&this->prealloc_mic_buffer_, &this->prealloc_mic_buffer_bytes_,
                     mic_bytes, buf_caps, true);
  }

  if (processor_mic_bytes > 0) {
    alloc_i16_buffer(&this->prealloc_processor_mic_buffer_,
                     &this->prealloc_processor_mic_buffer_bytes_,
                     processor_mic_bytes, buf_caps, true);
  }

  alloc_i16_buffer(&this->prealloc_spk_buffer_, &this->prealloc_spk_buffer_bytes_,
                   spk_bytes, buf_caps, false);

  if (ctx.use_stereo_aec_ref || ctx.use_tdm_ref) {
    alloc_i16_buffer(&this->prealloc_spk_ref_buffer_, &this->prealloc_spk_ref_buffer_bytes_,
                     spk_ref_bytes, buf_caps, true);
  }

  if (ctx.use_tdm_ref) {
    alloc_i16_buffer(&this->prealloc_tdm_tx_buffer_, &this->prealloc_tdm_tx_buffer_bytes_,
                     tdm_tx_bytes, buf_caps, false);
  }

#ifdef USE_AUDIO_PROCESSOR
  if (processor_buffers_needed) {
    if (!this->prealloc_spk_ref_buffer_ && !ctx.use_tdm_ref) {
      alloc_i16_buffer(&this->prealloc_spk_ref_buffer_, &this->prealloc_spk_ref_buffer_bytes_,
                       spk_ref_bytes, buf_caps, true);
    }
    alloc_i16_buffer(&this->prealloc_aec_output_, &this->prealloc_aec_output_bytes_,
                     aec_output_bytes, buf_caps, true);

    // direct_aec_ref_ stores the decimated TX reference at the processor rate.
    // Sized to ctx.input_frame_bytes: the AEC reference is the signal that
    // enters the DSP alongside the mic, so it lives on the input side of the
    // processor (AudioProcessor::process expects in_ref of input_samples len).
    // The TX-side FIR writes input_frame_size samples here per frame.
    // Honours buffers_in_psram_ alongside the rest.
    if (!this->direct_aec_ref_ && !ctx.use_stereo_aec_ref && !ctx.use_tdm_ref) {
      alloc_i16_buffer(&this->direct_aec_ref_, nullptr, ctx.input_frame_bytes, buf_caps, true);
      if (this->direct_aec_ref_ != nullptr) {
        memset(this->direct_aec_ref_, 0, ctx.input_frame_bytes);
      }
    }

    // AEC reference ring buffer (TYPE2-style, no-codec setups). Also one-shot.
    if (this->aec_use_ring_buffer_ && !ctx.use_stereo_aec_ref && !ctx.use_tdm_ref &&
        !this->aec_ref_ring_buffer_) {
      // Sized at the processor rate (post-decimation), not the bus rate, since
      // we now decimate on the TX side before storing. Items pushed are
      // input_frame_bytes (one AEC reference frame) each.
      const uint32_t output_rate = this->sample_rate_ / ctx.ratio;
      size_t rb_bytes = (output_rate * this->aec_ref_buffer_ms_ / 1000) * sizeof(int16_t);
      if (rb_bytes < ctx.input_frame_bytes * 4) rb_bytes = ctx.input_frame_bytes * 4;
      // Placement YAML-controlled: internal saves ~13.6 us/frame on Core 0 (R+W ~1 KB each),
      // PSRAM saves ~3-5 KB internal RAM (set aec_ref_ring_in_psram).
      this->aec_ref_ring_buffer_ = this->aec_ref_ring_in_psram_
          ? audio_processor::create_prefer_psram(rb_bytes, "i2s_duplex.aec_ref")
          : audio_processor::create_internal(rb_bytes, "i2s_duplex.aec_ref");
      if (!this->aec_ref_ring_buffer_) {
        this->release_audio_buffers_();
        return false;
      }
      ESP_LOGI(TAG, "AEC reference: ring_buffer (%zu bytes, %ums capacity)",
               rb_bytes, (unsigned)this->aec_ref_buffer_ms_);
    } else if (!ctx.use_stereo_aec_ref && !ctx.use_tdm_ref && !this->aec_ref_ring_buffer_) {
      ESP_LOGI(TAG, "AEC reference: previous_frame");
    }
  }
#endif

  // Validate required allocations
  if (!this->prealloc_rx_buffer_ || !this->prealloc_spk_buffer_) {
    this->release_audio_buffers_();
    return false;
  }
  if (ctx.mic_separate && !this->prealloc_mic_buffer_) {
    this->release_audio_buffers_();
    return false;
  }
  if (processor_mic_bytes > 0 && !this->prealloc_processor_mic_buffer_) {
    this->release_audio_buffers_();
    return false;
  }
  if (ctx.use_tdm_ref && !this->prealloc_tdm_tx_buffer_) {
    this->release_audio_buffers_();
    return false;
  }
#ifdef USE_AUDIO_PROCESSOR
  if (processor_buffers_needed) {
    if (!this->prealloc_aec_output_) {
      this->release_audio_buffers_();
      return false;
    }
    if ((ctx.use_stereo_aec_ref || ctx.use_tdm_ref) && !this->prealloc_spk_ref_buffer_) {
      this->release_audio_buffers_();
      return false;
    }
    // Mono AEC depends on direct_aec_ref_ as both the TX-side decimation scratch
    // and the previous-frame store. If it failed to allocate, the AEC would
    // silently run with a zero reference (TX writer is null-gated, ring writer
    // too, fill_mono falls through to zero-fill) and stay degraded until reboot,
    // because audio_buffers_allocated_ latches true on success. Fail-closed.
    if (!ctx.use_stereo_aec_ref && !ctx.use_tdm_ref && !this->direct_aec_ref_) {
      ESP_LOGE(TAG, "Mono AEC reference buffer allocation failed (%zu bytes)",
               ctx.input_frame_bytes);
      this->release_audio_buffers_();
      return false;
    }
  }
#endif

  if (ctx.ratio > 1) {
    bool fir_ready = true;
    if (ctx.use_tdm_ref || ctx.use_stereo_aec_ref) {
      fir_ready = this->rx_decimator_.prepare(
          ctx.bus_frame_size, ctx.input_frame_size, ctx.rx_decimator_channels);
    } else if (ctx.i2s_bps == 4) {
      // 32-bit mono RX uses process_strided_32(), which deinterleaves into
      // internal FIR scratch. Prepare it here instead of allocating on the
      // first audio frame.
      fir_ready = this->mic_decimator_.prepare(ctx.bus_frame_size);
    }
    if (!fir_ready) {
      this->release_audio_buffers_();
      return false;
    }
  }

  this->audio_buffers_allocated_ = true;
  return true;
}

void I2SAudioDuplex::audio_task(void *param) {
  I2SAudioDuplex *self = static_cast<I2SAudioDuplex *>(param);
  self->audio_task_();
  vTaskDelete(nullptr);
}

void I2SAudioDuplex::audio_task_() {
  // Component lifetime is process-wide (no destructor in ESPHome), so
  // this task lives forever; stop()/start() just toggle duplex_running_
  // to park/wake the inner session loop.
  while (true) {
    this->audio_task_idle_.store(true, std::memory_order_relaxed);
    if (this->prealloc_requested_.exchange(false, std::memory_order_acq_rel)) {
      this->preallocate_audio_buffers_from_task_();
    }
    if (!this->duplex_running_.load(std::memory_order_relaxed)) {
      ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
      continue;
    }
    this->audio_task_idle_.store(false, std::memory_order_relaxed);

    // Run one audio session. Returns on stop() (duplex_running_ cleared) or on
    // processor frame_spec change (session restarts in the next outer iter).
    this->audio_session_();
  }
}

bool I2SAudioDuplex::prepare_audio_context_(AudioTaskCtx &ctx, bool require_processor_spec,
                                            bool log_context) {
  ctx.ratio = this->decimation_ratio_;
  ctx.i2s_bps = (this->bits_per_sample_ > 16) ? 4 : 2;
  ctx.num_ch = this->num_channels_;
  ctx.use_stereo_aec_ref = this->use_stereo_aec_ref_;
  ctx.use_tdm_ref = this->use_tdm_ref_;
  ctx.ref_channel_right = this->ref_channel_right_;
  ctx.correct_dc_offset = this->correct_dc_offset_;
  ctx.tdm_total_slots = this->tdm_total_slots_;
  ctx.tdm_mic_slot = this->tdm_mic_slot_;
  ctx.tdm_second_mic_slot = this->tdm_second_mic_slot_;
  ctx.tdm_ref_slot = this->tdm_ref_slot_;

  if (log_context) {
    ESP_LOGI(TAG, "Audio task started (stereo=%s, tdm=%s, decimation=%ux)",
             ctx.use_stereo_aec_ref ? "YES" : "no",
             ctx.use_tdm_ref ? "YES" : "no", (unsigned)ctx.ratio);
  }

  // Determine frame sizes: processors may consume and produce different frame lengths.
  ctx.input_frame_size = DEFAULT_FRAME_SIZE;
  ctx.output_frame_size = DEFAULT_FRAME_SIZE;
#ifdef USE_AUDIO_PROCESSOR
  if (this->processor_ != nullptr) {
    ctx.processor_spec_revision = this->processor_->frame_spec_revision();
    auto spec = this->processor_->frame_spec();
    if (spec.input_samples > 0 && spec.output_samples > 0) {
      ctx.input_frame_size = spec.input_samples;
      ctx.output_frame_size = spec.output_samples;
      ctx.processor_mic_channels = std::max<uint8_t>(1, spec.mic_channels);
      ctx.processor_spec_loaded = true;
      uint32_t out_rate = this->get_output_sample_rate();
      if (log_context) {
        ESP_LOGI(TAG, "Processor: input=%u, output=%u samples, mic_ch=%u (%ums @ %uHz), revision=%u",
                 (unsigned) ctx.input_frame_size, (unsigned) ctx.output_frame_size,
                 (unsigned) ctx.processor_mic_channels,
                 (unsigned) (ctx.input_frame_size * 1000 / out_rate), (unsigned) out_rate,
                 (unsigned) ctx.processor_spec_revision);
      }
    } else if (require_processor_spec) {
      return false;
    }
  }
#endif

  // Init multi-channel RX decimator now that we know channel count
  if (ctx.use_tdm_ref || ctx.use_stereo_aec_ref) {
    ctx.rx_decimator_channels = ctx.use_tdm_ref
        ? (ctx.processor_mic_channels > 1 ? 3 : 2)  // MMR or MR
        : 2;  // stereo: mic + ref
    this->rx_decimator_.init(ctx.ratio, ctx.rx_decimator_channels);
    this->rx_decimator_.set_use_float_fir(this->fir_decimator_custom_);
  }

  // ── Frame sizing ──
  ctx.bus_frame_size = ctx.input_frame_size * ctx.ratio;
  ctx.input_frame_bytes = ctx.input_frame_size * sizeof(int16_t);
  ctx.output_frame_bytes = ctx.output_frame_size * sizeof(int16_t);
  ctx.bus_frame_bytes = ctx.bus_frame_size * sizeof(int16_t);
  if (ctx.use_tdm_ref) {
    ctx.rx_frame_bytes = ctx.bus_frame_size * ctx.tdm_total_slots * ctx.i2s_bps;
  } else if (ctx.use_stereo_aec_ref) {
    ctx.rx_frame_bytes = ctx.bus_frame_size * 2 * ctx.i2s_bps;
  } else {
    ctx.rx_frame_bytes = ctx.bus_frame_size * ctx.i2s_bps;
  }
  ctx.mic_separate = (ctx.ratio > 1) || ctx.use_stereo_aec_ref || ctx.use_tdm_ref;
  return true;
}

void I2SAudioDuplex::preallocate_audio_buffers_from_task_() {
  if (this->audio_buffers_allocated_) {
    this->prealloc_attempted_.store(true, std::memory_order_release);
    return;
  }
  if (this->prealloc_attempted_.exchange(true, std::memory_order_acq_rel)) {
    return;
  }

  AudioTaskCtx ctx{};
  if (!this->prepare_audio_context_(ctx, true, false)) {
    this->prealloc_attempted_.store(false, std::memory_order_release);
    return;
  }
  if (this->allocate_audio_buffers_(ctx)) {
    ESP_LOGI(TAG, "Audio buffers preallocated (rx=%uB, spk=%uB, processor=%s)",
             (unsigned) this->prealloc_rx_buffer_bytes_,
             (unsigned) this->prealloc_spk_buffer_bytes_,
             ctx.processor_spec_loaded ? "yes" : "no");
    this->log_memory_snapshot_("after_audio_buffer_prealloc");
  } else {
    ESP_LOGE(TAG, "Audio buffer preallocation failed; refusing cold-path allocation");
    this->has_i2s_error_.store(true, std::memory_order_relaxed);
  }
}

void I2SAudioDuplex::audio_session_() {
  AudioTaskCtx ctx{};
  // Telemetry compute paths gated on log level: when ESPHOME_LOG_LEVEL is
  // below DEBUG the entire block is stripped, leaving zero runtime cost
  // (no per-frame counters, no processor_->telemetry() call, no heap probes).
#if defined(USE_DUPLEX_TELEMETRY) && ESPHOME_LOG_LEVEL >= ESPHOME_LOG_LEVEL_DEBUG
  uint32_t t_frame_count = 0;
  uint32_t t_spk_underruns = 0;
#ifdef USE_AUDIO_PROCESSOR
  ProcessorTelemetry prev_processor_telem{};
#endif
#endif

  // ── Populate invariants and frame sizing ──
  if (!this->prepare_audio_context_(ctx, false, true)) {
    this->has_i2s_error_.store(true, std::memory_order_relaxed);
    this->duplex_running_.store(false, std::memory_order_relaxed);
    this->speaker_running_.store(false, std::memory_order_relaxed);
    return;
  }

  // ── Buffer setup ──
  // Working buffers are owned by the component and pre-allocated worst-case
  // on first task entry (see allocate_audio_buffers_). Subsequent restarts
  // (frame_spec change, feature toggle) reuse the same pointers without any
  // heap_caps_alloc calls, eliminating SPIRAM fragmentation that previously
  // caused "Failed to allocate AEC output buffer" after a few reconfigures.

  auto alloc_fail = [this](const char *what) {
    ESP_LOGE(TAG, "Failed to allocate %s", what);
    this->has_i2s_error_.store(true, std::memory_order_relaxed);
    this->duplex_running_.store(false, std::memory_order_relaxed);
    this->speaker_running_.store(false, std::memory_order_relaxed);
  };

  if (ctx.processor_mic_channels > 2) {
    alloc_fail("unsupported processor mic channel count");
    goto cleanup;
  }
  if (ctx.processor_mic_channels > 1 && !ctx.use_tdm_ref) {
    alloc_fail("dual-mic processor requires TDM microphone slots");
    goto cleanup;
  }
  if (ctx.processor_mic_channels > 1 && ctx.tdm_second_mic_slot < 0) {
    alloc_fail("dual-mic processor requires tdm_mic_slots with two slots");
    goto cleanup;
  }

  if (!this->audio_buffers_allocated_) {
    alloc_fail("audio buffers (not preallocated)");
    goto cleanup;
  }
  if (!this->allocate_audio_buffers_(ctx)) {
    alloc_fail("audio buffers (prepared shape invalid)");
    goto cleanup;
  }

  ctx.rx_buffer = this->prealloc_rx_buffer_;
  ctx.mic_buffer = ctx.mic_separate ? this->prealloc_mic_buffer_ : ctx.rx_buffer;
  if (ctx.processor_mic_channels > 1) {
    ctx.processor_mic_buffer = this->prealloc_processor_mic_buffer_;
  }
  ctx.spk_buffer = this->prealloc_spk_buffer_;
  ctx.spk_ref_buffer = this->prealloc_spk_ref_buffer_;
  ctx.tdm_tx_buffer = this->prealloc_tdm_tx_buffer_;
  ctx.aec_output = this->prealloc_aec_output_;
  if (ctx.use_tdm_ref) {
    ctx.tdm_tx_frame_bytes = ctx.bus_frame_size * ctx.tdm_total_slots * ctx.i2s_bps;
  }

#if defined(USE_DUPLEX_TELEMETRY) && ESPHOME_LOG_LEVEL >= ESPHOME_LOG_LEVEL_DEBUG
  ESP_LOGD(TAG, "Heap after audio init: internal=%u, PSRAM=%u",
           (unsigned) heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
           (unsigned) heap_caps_get_free_size(MALLOC_CAP_SPIRAM));
#endif

  // ── Main loop ──
  while (this->duplex_running_.load(std::memory_order_relaxed)) {
#ifdef USE_DUPLEX_DEBUG_PROBE
    const int64_t debug_frame_start_us = esp_timer_get_time();
    ctx.frame_seq++;
    ctx.debug_flags = 0;
    ctx.debug_i2s_read_us = 0;
    ctx.debug_rx_us = 0;
    ctx.debug_process_us = 0;
    ctx.debug_tx_us = 0;
    ctx.debug_frame_us = 0;
#endif
    // Service ring buffer operations requested by main thread
    if (this->request_speaker_reset_.exchange(false, std::memory_order_relaxed)) {
      this->speaker_buffer_->reset();
      this->direct_aec_ref_valid_ = false;
      if (this->aec_ref_ring_buffer_) {
        this->aec_ref_ring_buffer_->reset();
      }
    }
    // Reset per-frame state
    ctx.output_buffer = nullptr;
    ctx.current_output_frame_size = ctx.input_frame_size;
    ctx.current_output_frame_bytes = ctx.input_frame_bytes;
    ctx.speaker_underrun = false;
    ctx.speaker_got = 0;

    // Snapshot atomic state for this frame (avoids repeated .load() in sample loops)
    ctx.mic_attenuation = this->mic_attenuation_.load(std::memory_order_relaxed);
    ctx.mic_gain = this->mic_gain_.load(std::memory_order_relaxed);
    ctx.speaker_volume_q15 = this->speaker_volume_q15_.load(std::memory_order_relaxed);
    ctx.speaker_running = this->speaker_running_.load(std::memory_order_relaxed);
    ctx.speaker_paused = this->speaker_paused_.load(std::memory_order_relaxed);
    ctx.mic_running = this->has_mic_consumers_.load(std::memory_order_relaxed);

#ifdef USE_DUPLEX_DEBUG_PROBE
    int64_t debug_stage_start_us = esp_timer_get_time();
#endif
    this->process_rx_path_(ctx);
#ifdef USE_DUPLEX_DEBUG_PROBE
    ctx.debug_rx_us = static_cast<uint32_t>(std::max<int64_t>(0, esp_timer_get_time() - debug_stage_start_us));
#endif

    ctx.processor_enabled = this->processor_enabled_.load(std::memory_order_relaxed);
#ifdef USE_AUDIO_PROCESSOR
    ctx.processor_ready = ctx.processor_enabled && this->processor_ != nullptr &&
                          this->processor_->is_initialized();
#endif
    ctx.now_ms = millis();

#ifdef USE_DUPLEX_DEBUG_PROBE
    debug_stage_start_us = esp_timer_get_time();
#endif
    this->process_aec_and_callbacks_(ctx);
#ifdef USE_DUPLEX_DEBUG_PROBE
    ctx.debug_process_us = static_cast<uint32_t>(std::max<int64_t>(0, esp_timer_get_time() - debug_stage_start_us));
    debug_stage_start_us = esp_timer_get_time();
#endif
    this->process_tx_path_(ctx);
#ifdef USE_DUPLEX_DEBUG_PROBE
    ctx.debug_tx_us = static_cast<uint32_t>(std::max<int64_t>(0, esp_timer_get_time() - debug_stage_start_us));
    ctx.debug_frame_us = static_cast<uint32_t>(std::max<int64_t>(0, esp_timer_get_time() - debug_frame_start_us));
    this->debug_probe_record_(ctx);
#endif

#ifdef USE_AUDIO_PROCESSOR
    // Frame_spec change (e.g. SE toggled, MR<->MMR switch): exit this session
    // so the outer audio_task_ wrapper can re-enter audio_session_ with a
    // fresh ctx. Preallocated buffers are already worst-case sized, so the
    // restart does not touch the heap.
    if (this->processor_ != nullptr) {
      uint32_t rev = this->processor_->frame_spec_revision();
      bool ready = this->processor_->is_initialized();
      if (ready && !ctx.processor_spec_loaded) {
        ESP_LOGI(TAG, "Processor frame_spec became available (rev %u), restarting session",
                 (unsigned) rev);
        break;
      }
      if (ready && rev != ctx.processor_spec_revision) {
        ESP_LOGI(TAG, "Processor frame_spec changed (rev %u -> %u), restarting session",
                 (unsigned) ctx.processor_spec_revision, (unsigned) rev);
        break;
      }
      if (!ready) {
        ctx.processor_spec_revision = rev;
      }
    }
#endif

#if defined(USE_DUPLEX_TELEMETRY) && ESPHOME_LOG_LEVEL >= ESPHOME_LOG_LEVEL_DEBUG
    {
      // Lightweight per-frame cycle snapshot (only when telemetry: true AND
      // log level >= DEBUG: otherwise this block is stripped at compile time
      // so the loop pays zero runtime cost in production builds).
      // t_frame_count and t_spk_underruns declared before the loop (reset on task restart)
      t_spk_underruns += ctx.speaker_underrun ? 1 : 0;
      t_frame_count++;
      if (t_frame_count >= this->telemetry_log_interval_frames_) {
        ESP_LOGD(TAG, "Perf[%u frames]: spk_underrun=%u, heap_int=%u, heap_ps=%u",
                 (unsigned) t_frame_count, (unsigned) t_spk_underruns,
                 (unsigned) heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
                 (unsigned) heap_caps_get_free_size(MALLOC_CAP_SPIRAM));
#ifdef USE_AUDIO_PROCESSOR
        if (this->processor_ != nullptr) {
          ProcessorTelemetry telem = this->processor_->telemetry();
          const uint32_t audio_stack_high_water =
              uxTaskGetStackHighWaterMark(nullptr) * sizeof(StackType_t);
          auto delta_u32 = [](uint32_t current, uint32_t previous) -> uint32_t {
            return current >= previous ? (current - previous) : current;
          };
          ESP_LOGD(TAG,
                   "AFE[%u]: +glitch=%u +in_drop=%u +feed_ok=%u +feed_rej=%u "
                   "+fetch_ok=%u +fetch_to=%u +out_drop=%u rb=%.0f%% "
                   "q(feed=%u pk=%u, fetch=%u pk=%u)",
                   (unsigned) telem.frame_count,
                   (unsigned) delta_u32(telem.glitch_count, prev_processor_telem.glitch_count),
                   (unsigned) delta_u32(telem.input_ring_drop, prev_processor_telem.input_ring_drop),
                   (unsigned) delta_u32(telem.feed_ok, prev_processor_telem.feed_ok),
                   (unsigned) delta_u32(telem.feed_rejected, prev_processor_telem.feed_rejected),
                   (unsigned) delta_u32(telem.fetch_ok, prev_processor_telem.fetch_ok),
                   (unsigned) delta_u32(telem.fetch_timeout, prev_processor_telem.fetch_timeout),
                   (unsigned) delta_u32(telem.output_ring_drop, prev_processor_telem.output_ring_drop),
                   telem.ringbuf_free_pct * 100.0f,
                   (unsigned) telem.feed_queue_frames,
                   (unsigned) telem.feed_queue_peak,
                   (unsigned) telem.fetch_queue_frames,
                   (unsigned) telem.fetch_queue_peak);
          ESP_LOGD(TAG,
                   "AFE timing: proc last/max=%u/%uus feed last/max=%u/%uus "
                   "fetch last/max=%u/%uus stackB audio/feed/fetch=%u/%u/%u",
                   (unsigned) telem.process_us_last,
                   (unsigned) telem.process_us_max,
                   (unsigned) telem.feed_us_last,
                   (unsigned) telem.feed_us_max,
                   (unsigned) telem.fetch_us_last,
                   (unsigned) telem.fetch_us_max,
                   (unsigned) audio_stack_high_water,
                   (unsigned) telem.feed_stack_high_water,
                   (unsigned) telem.fetch_stack_high_water);
          prev_processor_telem = telem;
        }
#endif
        t_frame_count = 0;
        t_spk_underruns = 0;
      }
    }
#endif

    // delay(1) (vTaskDelay) yields to lower-priority tasks too, so IDLE
    // gets to run and feed the task watchdog. taskYIELD() only cedes to
    // same- or higher-priority tasks; with this audio loop pinned at
    // prio 19 above lwIP=18, IDLE0 would never run on Core 0 and TWDT
    // would trip. The blocking i2s_channel_read above usually parks the
    // task long enough for IDLE to run, but the fast path (return with
    // data immediately available) needs an explicit cooperative point.
    delay(1);
  }

cleanup:
  // Buffers live on as component-owned preallocations; nothing to free here.
  // Returning from audio_session_ hands control back to the outer wrapper,
  // which either parks the task (stop) or re-enters with fresh ctx
  // (frame_spec change).
  ESP_LOGD(TAG, "Audio session ended");
}

// ════════════════════════════════════════════════════════════════════════════
// RX PATH: I2S read → deinterleave/decimate → mic_buffer + spk_ref_buffer
// ════════════════════════════════════════════════════════════════════════════
void I2SAudioDuplex::process_rx_path_(AudioTaskCtx &ctx) {
  if (!this->rx_handle_)
    return;

  size_t bytes_read;
#ifdef USE_DUPLEX_DEBUG_PROBE
  const int64_t debug_i2s_read_start_us = esp_timer_get_time();
#endif
  esp_err_t err = i2s_channel_read(this->rx_handle_, ctx.rx_buffer, ctx.rx_frame_bytes,
                                    &bytes_read, I2S_IO_TIMEOUT_MS);
#ifdef USE_DUPLEX_DEBUG_PROBE
  ctx.debug_i2s_read_us = static_cast<uint32_t>(
      std::max<int64_t>(0, esp_timer_get_time() - debug_i2s_read_start_us));
#endif
  if (err == ESP_ERR_INVALID_STATE) {
    // Brief INVALID_STATE around stop() teardown is expected (channel
    // disabled mid-flight). Escalate only if duplex_running_ is still
    // true and the condition persists, which means the RX channel got
    // corrupted independently of our own teardown.
    if (this->duplex_running_.load(std::memory_order_relaxed)) {
      if (++ctx.invalid_state_errors > 100) {
        ESP_LOGE(TAG, "Persistent I2S RX INVALID_STATE (%d) - channel corrupted",
                 ctx.invalid_state_errors);
        this->has_i2s_error_.store(true, std::memory_order_relaxed);
        this->duplex_running_.store(false, std::memory_order_relaxed);
      }
    }
    return;
  }
  if (err != ESP_OK && err != ESP_ERR_TIMEOUT) {
#ifdef USE_DUPLEX_DEBUG_PROBE
    ctx.debug_flags |= DBG_FLAG_RX_BAD;
#endif
    LOG_W_THROTTLED("i2s_channel_read failed: %s", esp_err_to_name(err));
    if (++ctx.consecutive_i2s_errors > 100) {
      ESP_LOGE(TAG, "Persistent I2S read errors (%d)", ctx.consecutive_i2s_errors);
      this->has_i2s_error_.store(true, std::memory_order_relaxed);
      this->duplex_running_.store(false, std::memory_order_relaxed);
    }
    return;
  }
  if (err != ESP_OK || bytes_read != ctx.rx_frame_bytes) {
#ifdef USE_DUPLEX_DEBUG_PROBE
    ctx.debug_flags |= DBG_FLAG_RX_BAD;
#endif
    return;
  }

  ctx.consecutive_i2s_errors = 0;
  ctx.invalid_state_errors = 0;

  // Convert 32-bit I2S samples to 16-bit only when FIR strided does NOT handle it.
  // When ratio > 1, the FIR decimator reads 32-bit directly via process_strided_32.
  if (ctx.i2s_bps == 4 && ctx.ratio <= 1) {
    auto *src32 = reinterpret_cast<int32_t *>(ctx.rx_buffer);
    size_t total_i2s_samples = bytes_read / sizeof(int32_t);
    for (size_t i = 0; i < total_i2s_samples; i++) {
      ctx.rx_buffer[i] = static_cast<int16_t>(src32[i] >> 16);
    }
  }

  if (ctx.use_tdm_ref) {
    this->update_tdm_slot_levels_(ctx);
  }

  ctx.output_buffer = ctx.mic_buffer;  // Default: no AEC processing
  ctx.processor_input = ctx.mic_buffer;

#if SOC_I2S_SUPPORTS_TDM
  if (ctx.use_tdm_ref) {
    const uint8_t ts = ctx.tdm_total_slots;
    const bool dual_mic = ctx.processor_mic_channels > 1 && ctx.tdm_second_mic_slot >= 0;
    uint8_t ch_offsets[MC_FIR_MAX_CH];
    uint8_t num_mic_ch;
    if (dual_mic) {
      ch_offsets[0] = ctx.tdm_mic_slot;
      ch_offsets[1] = static_cast<uint8_t>(ctx.tdm_second_mic_slot);
      ch_offsets[2] = ctx.tdm_ref_slot;
      num_mic_ch = 2;
    } else {
      ch_offsets[0] = ctx.tdm_mic_slot;
      ch_offsets[1] = ctx.tdm_ref_slot;
      num_mic_ch = 1;
    }
    if (ctx.i2s_bps == 4) {
      auto *src32 = reinterpret_cast<const int32_t *>(ctx.rx_buffer);
      this->rx_decimator_.process_multi_32(src32, ctx.input_frame_size, ts, ch_offsets,
          dual_mic ? ctx.processor_mic_buffer : nullptr, ctx.mic_buffer,
          ctx.spk_ref_buffer, num_mic_ch);
    } else {
      this->rx_decimator_.process_multi(ctx.rx_buffer, ctx.input_frame_size, ts, ch_offsets,
          dual_mic ? ctx.processor_mic_buffer : nullptr, ctx.mic_buffer,
          ctx.spk_ref_buffer, num_mic_ch);
    }
    if (dual_mic) {
      ctx.processor_input = ctx.processor_mic_buffer;
    }
  } else
#endif
  if (ctx.use_stereo_aec_ref) {
    // Stereo: mic + ref via multi-channel FIR
    const uint8_t mi = ctx.ref_channel_right ? 0 : 1;
    const uint8_t ri = ctx.ref_channel_right ? 1 : 0;
    uint8_t ch_offsets[2] = {mi, ri};
    if (ctx.i2s_bps == 4) {
      auto *src32 = reinterpret_cast<const int32_t *>(ctx.rx_buffer);
      this->rx_decimator_.process_multi_32(src32, ctx.input_frame_size, 2, ch_offsets,
          nullptr, ctx.mic_buffer, ctx.spk_ref_buffer, 1);
    } else {
      this->rx_decimator_.process_multi(ctx.rx_buffer, ctx.input_frame_size, 2, ch_offsets,
          nullptr, ctx.mic_buffer, ctx.spk_ref_buffer, 1);
    }
  } else if (ctx.ratio > 1) {
    // Mono with decimation
    if (ctx.i2s_bps == 4) {
      auto *src32 = reinterpret_cast<const int32_t *>(ctx.rx_buffer);
      this->mic_decimator_.process_strided_32(src32, ctx.mic_buffer, ctx.input_frame_size, 1, 0);
    } else {
      this->mic_decimator_.process(ctx.rx_buffer, ctx.mic_buffer, ctx.bus_frame_size);
    }
  }
  // else: Mono without decimation: mic_buffer == rx_buffer (aliased), nothing to do

  // Fused loop: DC offset + mic attenuation in one pass.
  // For dual-mic: mic1 is in mic_buffer, mic2 is in processor_mic_buffer[i*2+1]
  // (both filled by the multi-channel FIR). Apply DC+atten on both, update in-place.
  // When neither DC nor attenuation is needed, processor_mic_buffer (dual_mic case)
  // is left as-is: the multi-channel FIR has already produced correct values for both mics.
  const bool do_dc = ctx.correct_dc_offset;
  const bool do_atten = ctx.mic_attenuation != 1.0f;
  const bool dual_mic = ctx.processor_mic_channels > 1 && ctx.processor_mic_buffer != nullptr;

  if (do_dc || do_atten) {
    const float atten = ctx.mic_attenuation;
    for (size_t i = 0; i < ctx.input_frame_size; i++) {
      int16_t s1 = ctx.mic_buffer[i];

      if (do_dc) {
        int32_t inp = (int32_t) s1 << 16;
        int32_t out = inp - this->dc_prev_input_persistent_ + this->dc_prev_output_persistent_ -
                      (this->dc_prev_output_persistent_ >> 10);
        this->dc_prev_input_persistent_ = inp;
        this->dc_prev_output_persistent_ = out;
        s1 = static_cast<int16_t>(out >> 16);
      }
      if (do_atten) {
        s1 = scale_sample(s1, atten);
      }
      ctx.mic_buffer[i] = s1;

      if (dual_mic) {
        // mic2 already in processor_mic_buffer interleaved by multi-channel FIR
        int16_t s2 = ctx.processor_mic_buffer[i * 2 + 1];
        if (do_dc) {
          int32_t inp2 = (int32_t) s2 << 16;
          int32_t out2 = inp2 - this->dc_prev_input_secondary_persistent_ +
                         this->dc_prev_output_secondary_persistent_ -
                         (this->dc_prev_output_secondary_persistent_ >> 10);
          this->dc_prev_input_secondary_persistent_ = inp2;
          this->dc_prev_output_secondary_persistent_ = out2;
          s2 = static_cast<int16_t>(out2 >> 16);
        }
        if (do_atten) {
          s2 = scale_sample(s2, atten);
        }
        // Update both mic1 and mic2 in the interleaved buffer
        ctx.processor_mic_buffer[i * 2] = s1;
        ctx.processor_mic_buffer[i * 2 + 1] = s2;
      }
    }
  }
  // dual_mic with no DC/atten: processor_mic_buffer already correct from FIR (see top comment).
}

void I2SAudioDuplex::update_tdm_slot_levels_(const AudioTaskCtx &ctx) {
  uint8_t enabled_slots[8];
  size_t enabled_count = 0;
  const uint8_t slot_limit = std::min<uint8_t>(ctx.tdm_total_slots, 8);
  for (uint8_t slot = 0; slot < slot_limit; slot++) {
    if (this->tdm_slot_level_sensor_enabled_[slot]) {
      enabled_slots[enabled_count++] = slot;
    }
  }
  if (enabled_count == 0) {
    return;
  }

  // Probe only every ~256 ms at 16 kHz / 512-sample cadence to keep overhead low.
  this->tdm_slot_level_divider_++;
  if (this->tdm_slot_level_divider_ < 8) {
    return;
  }
  this->tdm_slot_level_divider_ = 0;

  const size_t frame_samples = ctx.bus_frame_size;
  const size_t slot_stride = ctx.tdm_total_slots;
  for (size_t i = 0; i < enabled_count; i++) {
    uint8_t slot = enabled_slots[i];
    float dbfs;
    if (ctx.i2s_bps == 4 && ctx.ratio > 1) {
      // 32-bit mode with decimation: rx_buffer has not been converted to 16-bit
      auto *src32 = reinterpret_cast<const int32_t *>(ctx.rx_buffer);
      dbfs = compute_rms_dbfs_i32_top16(src32 + slot, frame_samples, slot_stride);
    } else {
      dbfs = compute_rms_dbfs_i16(ctx.rx_buffer + slot, frame_samples, slot_stride);
    }
    this->tdm_slot_level_dbfs_[slot].store(dbfs, std::memory_order_relaxed);
  }
}

#ifdef USE_AUDIO_PROCESSOR
void I2SAudioDuplex::run_processor_(AudioTaskCtx &ctx) {
  this->processor_->process(ctx.processor_input, ctx.spk_ref_buffer, ctx.aec_output,
                            ctx.processor_mic_channels);
  ctx.output_buffer = ctx.aec_output;
  ctx.current_output_frame_size = ctx.output_frame_size;
  ctx.current_output_frame_bytes = ctx.output_frame_bytes;
}
#endif

// ════════════════════════════════════════════════════════════════════════════
// AEC + CALLBACKS: raw callbacks → AEC processing → gain → post callbacks
// ════════════════════════════════════════════════════════════════════════════
void I2SAudioDuplex::process_aec_and_callbacks_(AudioTaskCtx &ctx) {
  if (!this->rx_handle_ || ctx.output_buffer == nullptr)
    return;

  // Raw mic callbacks: pre-AEC audio for MWW
  if (ctx.mic_running && !this->raw_mic_callbacks_.empty()) {
    for (auto &callback : this->raw_mic_callbacks_) {
      callback((const uint8_t *) ctx.mic_buffer, ctx.input_frame_bytes);
    }
  }

#ifdef USE_AUDIO_PROCESSOR
#if SOC_I2S_SUPPORTS_TDM
  if (ctx.use_tdm_ref && ctx.processor_ready &&
      ctx.spk_ref_buffer != nullptr && ctx.aec_output != nullptr) {
    // TDM: hardware-synced reference, no speaker gating needed.
    // Health monitor: while the speaker is actively driving samples the
    // TDM ref slot should not be silent. Sustained silence here means
    // the hardware mapping is wrong (P4 ES7210 MIC3/slot drift, codec
    // wiring) and AEC will diverge; warn the user instead of silently
    // running with a dead reference.
    constexpr float kRefSilenceThresholdDbfs = -60.0f;
    constexpr uint32_t kRefSilenceWarnFrames = 100;  // ~3.2s @ 32ms frames
    const bool spk_active = ctx.speaker_running && !ctx.speaker_paused &&
        (ctx.now_ms - this->last_speaker_audio_ms_.load(std::memory_order_relaxed)
            <= AEC_ACTIVE_TIMEOUT_MS);
    if (spk_active) {
      const float ref_dbfs = compute_rms_dbfs_i16(
          ctx.spk_ref_buffer, ctx.input_frame_size, 1);
      if (ref_dbfs < kRefSilenceThresholdDbfs) {
        uint32_t n = this->tdm_ref_silent_frames_.fetch_add(1, std::memory_order_relaxed) + 1;
        if (n == kRefSilenceWarnFrames) {
          ESP_LOGW(TAG,
                   "TDM AEC reference silent for %u frames while speaker active "
                   "(ref %.1f dBFS); check tdm_ref_slot wiring or set "
                   "use_tdm_reference: false",
                   (unsigned) n, ref_dbfs);
        }
      } else {
        this->tdm_ref_silent_frames_.store(0, std::memory_order_relaxed);
      }
    }
    this->run_processor_(ctx);
  } else
#endif
  if (!ctx.use_tdm_ref && ctx.processor_ready &&
      ctx.spk_ref_buffer != nullptr && ctx.aec_output != nullptr &&
      // Stereo AEC: reference is embedded in I2S RX (L=DAC loopback), always available.
      // Mono AEC: reference comes from speaker ring buffer, only valid when speaker is active.
      (ctx.use_stereo_aec_ref ||
       (ctx.speaker_running && !ctx.speaker_paused &&
        (ctx.now_ms - this->last_speaker_audio_ms_.load(std::memory_order_relaxed) <= AEC_ACTIVE_TIMEOUT_MS)))) {

    // Mono mode: get AEC reference (direct from TX or ring buffer).
    // Reference is post-volume PCM, no additional scaling (Espressif TYPE2 pattern).
    if (!ctx.use_stereo_aec_ref) {
      this->fill_mono_aec_reference_(ctx);
    }
    // Stereo mode: spk_ref_buffer already filled from deinterleave. No extra scaling.
    // TDM mode: spk_ref_buffer filled from TDM deinterleave. No extra scaling.

    this->run_processor_(ctx);
  }
#endif

  // Apply mic gain (snapshot value). scale_block_i16 picks SIMD when gain
  // is in [0, 1] and falls back to scalar saturating loop above 1.0 (mic
  // gain can amplify up to ~10x via mic_gain_db: +20).
  scale_block_i16(ctx.output_buffer, ctx.output_buffer, ctx.current_output_frame_size, ctx.mic_gain);

  // Post-AEC callbacks (VA/STT)
  if (ctx.mic_running) {
    for (auto &callback : this->mic_callbacks_) {
      callback((const uint8_t *) ctx.output_buffer, ctx.current_output_frame_bytes);
    }
  }
}

void I2SAudioDuplex::fill_mono_aec_reference_(AudioTaskCtx &ctx) {
  // direct_aec_ref_ and the ring buffer hold already-decimated samples at the
  // processor rate (decimation happens once on the TX side in process_tx_path_).
  // The reference is the input side of the processor, so the unit is
  // input_frame_bytes (matches AudioProcessor::process expecting in_ref of
  // input_samples length).
  const size_t ref_bytes = ctx.input_frame_bytes;
  if (this->aec_ref_ring_buffer_) {
    if (this->aec_ref_ring_buffer_->available() >= ref_bytes) {
      this->aec_ref_ring_buffer_->read(ctx.spk_ref_buffer, ref_bytes, 0);
      return;
    }
  } else if (this->direct_aec_ref_ != nullptr && this->direct_aec_ref_valid_) {
    memcpy(ctx.spk_ref_buffer, this->direct_aec_ref_, ref_bytes);
    return;
  }

  // No reference available: zero-fill (AEC will pass-through without echo cancellation)
  memset(ctx.spk_ref_buffer, 0, ref_bytes);
}

// ════════════════════════════════════════════════════════════════════════════
// TX PATH: ring buffer read → volume → format expand → I2S write
// ════════════════════════════════════════════════════════════════════════════
void I2SAudioDuplex::process_tx_path_(AudioTaskCtx &ctx) {
  if (!this->tx_handle_)
    return;

  if (ctx.speaker_running && !ctx.speaker_paused) {
    ctx.speaker_got = this->speaker_buffer_->read((void *) ctx.spk_buffer, ctx.bus_frame_bytes, 0);
    size_t got = ctx.speaker_got;
    // Treat partial frames as underrun too (the frame is padded with zero below
    // and must not be used as AEC reference, otherwise the AEC adaptive filter
    // sees a half-real / half-silent signal and fails to correlate with the mic).
    ctx.speaker_underrun = got < ctx.bus_frame_bytes;

    if (got > 0) {
      // Speaker software volume is cached as Q15 when the volume changes, so
      // the audio task does not spend every frame converting float -> fixed.
      const size_t got_samples = got / sizeof(int16_t);
      scale_block_i16_q15(ctx.spk_buffer, ctx.spk_buffer, got_samples, ctx.speaker_volume_q15);
      if (got < ctx.bus_frame_bytes) {
        memset(((uint8_t *) ctx.spk_buffer) + got, 0, ctx.bus_frame_bytes - got);
      }
    } else {
      memset(ctx.spk_buffer, 0, ctx.bus_frame_bytes);
    }
  } else {
    // Paused speaker output must not drain speaker_buffer_: ESPHome speaker
    // pause means processing incoming audio is suspended, not consumed as
    // silent playback.
    memset(ctx.spk_buffer, 0, ctx.bus_frame_bytes);
    ctx.speaker_got = 0;
  }

  // Save post-volume TX data as AEC reference (skip if processor is off).
  // The reference is decimated to the processor rate HERE, on the TX side, so
  // downstream storage and consumer reads happen at the smaller output size.
  // Two safety properties of this gating:
  //   1) Decimation only runs on a complete frame (speaker_got == bus_frame_bytes),
  //      otherwise the FIR delay-line would absorb zero-padding and pollute the
  //      next valid frame's reference for ~32 samples.
  //   2) Skipping the save on a short read keeps the last good reference, same
  //      as the prior implementation.
#ifdef USE_AUDIO_PROCESSOR
  const bool full_frame =
      ctx.speaker_running && !ctx.speaker_paused && ctx.speaker_got == ctx.bus_frame_bytes;
  if (this->aec_ref_ring_buffer_ && ctx.processor_enabled) {
    if (full_frame && this->direct_aec_ref_ != nullptr) {
      // Decimate TX -> processor rate into direct_aec_ref_ scratch, then push
      // the decimated frame into the ring. direct_aec_ref_ is sized for
      // input_frame_bytes by allocate_audio_buffers_() (the FIR writes
      // bus_frame_size / ratio = input_frame_size samples).
      this->play_ref_decimator_.process(ctx.spk_buffer, this->direct_aec_ref_, ctx.bus_frame_size);
      const size_t ref_bytes = ctx.input_frame_bytes;
      // ESPHome RingBuffer::write() already drops oldest data on overflow
      // (discard_bytes_ + write_without_replacement), which is the right
      // backpressure here: keep the most recent reference window for AEC.
      this->aec_ref_ring_buffer_->write((void *) this->direct_aec_ref_, ref_bytes);
    }
  } else if (this->direct_aec_ref_ != nullptr && ctx.processor_enabled) {
    // Previous frame mode: decimate TX once and keep the result for the next
    // AEC iteration. Only on a full frame, otherwise we keep the last good
    // direct_aec_ref_ to avoid feeding a zero-padded reference.
    if (full_frame) {
      this->play_ref_decimator_.process(ctx.spk_buffer, this->direct_aec_ref_, ctx.bus_frame_size);
      this->direct_aec_ref_valid_ = true;
    }
  }
#endif

  // Prepare TX: format expansion + TDM interleave
  const void *tx_data;
  size_t tx_bytes;
#if SOC_I2S_SUPPORTS_TDM
  if (ctx.use_tdm_ref && ctx.tdm_tx_buffer != nullptr) {
    if (ctx.i2s_bps == 4) {
      auto *tdm32 = reinterpret_cast<int32_t *>(ctx.tdm_tx_buffer);
      memset(tdm32, 0, ctx.tdm_tx_frame_bytes);
      for (size_t i = 0; i < ctx.bus_frame_size; i++) {
        tdm32[i * ctx.tdm_total_slots] = static_cast<int32_t>(ctx.spk_buffer[i]) << 16;
      }
    } else {
      memset(ctx.tdm_tx_buffer, 0, ctx.tdm_tx_frame_bytes);
      for (size_t i = 0; i < ctx.bus_frame_size; i++) {
        ctx.tdm_tx_buffer[i * ctx.tdm_total_slots] = ctx.spk_buffer[i];
      }
    }
    tx_data = ctx.tdm_tx_buffer;
    tx_bytes = ctx.tdm_tx_frame_bytes;
  } else
#endif
  {
    size_t total_tx_samples = ctx.bus_frame_size * ctx.num_ch;
    if (ctx.num_ch == 2 && ctx.i2s_bps == 4) {
      // Fused mono->stereo + 16->32 in one backward pass
      auto *dst32 = reinterpret_cast<int32_t *>(ctx.spk_buffer);
      for (int i = static_cast<int>(ctx.bus_frame_size) - 1; i >= 0; i--) {
        int32_t s = static_cast<int32_t>(ctx.spk_buffer[i]) << 16;
        dst32[i * 2 + 1] = s;
        dst32[i * 2] = s;
      }
    } else if (ctx.num_ch == 2) {
      for (int i = static_cast<int>(ctx.bus_frame_size) - 1; i >= 0; i--) {
        ctx.spk_buffer[i * 2 + 1] = ctx.spk_buffer[i];
        ctx.spk_buffer[i * 2] = ctx.spk_buffer[i];
      }
    } else if (ctx.i2s_bps == 4) {
      auto *dst32 = reinterpret_cast<int32_t *>(ctx.spk_buffer);
      for (int i = static_cast<int>(total_tx_samples) - 1; i >= 0; i--) {
        dst32[i] = static_cast<int32_t>(ctx.spk_buffer[i]) << 16;
      }
    }
    tx_data = ctx.spk_buffer;
    tx_bytes = total_tx_samples * ctx.i2s_bps;
  }

  size_t bytes_written;
  esp_err_t err = i2s_channel_write(this->tx_handle_, tx_data, tx_bytes, &bytes_written, I2S_IO_TIMEOUT_MS);
  if (err == ESP_ERR_INVALID_STATE) {
    if (this->duplex_running_.load(std::memory_order_relaxed)) {
      if (++ctx.invalid_state_errors > 100) {
        ESP_LOGE(TAG, "Persistent I2S TX INVALID_STATE (%d) - channel corrupted",
                 ctx.invalid_state_errors);
        this->has_i2s_error_.store(true, std::memory_order_relaxed);
        this->duplex_running_.store(false, std::memory_order_relaxed);
      }
    }
  } else if (err != ESP_OK && err != ESP_ERR_TIMEOUT) {
    LOG_W_THROTTLED("i2s_channel_write failed: %s", esp_err_to_name(err));
    if (++ctx.consecutive_i2s_errors > 100) {
      ESP_LOGE(TAG, "Persistent I2S write errors (%d)", ctx.consecutive_i2s_errors);
      this->has_i2s_error_.store(true, std::memory_order_relaxed);
      this->duplex_running_.store(false, std::memory_order_relaxed);
    }
  } else if (err == ESP_OK) {
    ctx.consecutive_i2s_errors = 0;
    ctx.invalid_state_errors = 0;
  }

  // Report frames actually consumed from the ring buffer (not silence/pad frames).
  // Using got (ring buffer read) instead of bytes_written (I2S output) prevents
  // counting silence frames as "played" during underruns.
  if (err == ESP_OK && ctx.speaker_got > 0 && !this->speaker_output_callbacks_.empty()) {
    uint32_t frames_played = ctx.speaker_got / sizeof(int16_t);
    int64_t timestamp = esp_timer_get_time();
    for (auto &cb : this->speaker_output_callbacks_) {
      cb(frames_played, timestamp);
    }
  }
}

}  // namespace i2s_audio_duplex
}  // namespace esphome

#endif  // USE_ESP32
