#include "intercom_api.h"

#ifdef USE_ESP32

#include <algorithm>
#include <cmath>
#include <cstdint>
#include <cstring>

#include "esphome/core/hal.h"
#include "esphome/core/log.h"

#include "../audio_processor/audio_utils.h"
#include "../audio_processor/scoped_lock.h"

namespace esphome {
namespace intercom_api {

static const char *const TAG = "intercom_api.audio";

#ifdef USE_INTERCOM_STANDALONE_AUDIO
static inline void release_i16_buffer(RAMAllocator<int16_t> &alloc, int16_t **buffer,
                                      size_t samples) {
  if (*buffer != nullptr) {
    alloc.deallocate(*buffer, samples);
    *buffer = nullptr;
  }
}
#endif

void IntercomApi::debug_log_pcm_level_(const char *label, const uint8_t *pcm, size_t bytes,
                                       uint32_t &last_log_ms, uint32_t &frame_count) {
  frame_count++;
  const uint32_t now = millis();
  if (now - last_log_ms < 1000)
    return;
  last_log_ms = now;

  const size_t samples = bytes / sizeof(int16_t);
  if (pcm == nullptr || samples == 0) {
    ESP_LOGI(TAG, "AudioDebug[%s]: frames=%u bytes=%u empty state=%s",
             label, frame_count, (unsigned) bytes, this->get_call_state_str());
    return;
  }

  const int16_t *data = reinterpret_cast<const int16_t *>(pcm);
  uint64_t sum_sq = 0;
  uint32_t peak = 0;
  for (size_t i = 0; i < samples; i++) {
    const int32_t sample = data[i];
    const uint32_t abs_sample = sample == INT16_MIN ? 32768U : static_cast<uint32_t>(std::abs(sample));
    if (abs_sample > peak)
      peak = abs_sample;
    sum_sq += static_cast<int64_t>(sample) * static_cast<int64_t>(sample);
  }

  const float rms = std::sqrt(static_cast<float>(sum_sq) / static_cast<float>(samples));
  const float peak_db = peak > 0 ? 20.0f * std::log10(static_cast<float>(peak) / 32768.0f) : -120.0f;
  const float rms_db = rms > 0.0f ? 20.0f * std::log10(rms / 32768.0f) : -120.0f;
#ifdef USE_INTERCOM_STANDALONE_AUDIO
  const char *path = this->speaker_buffer_ != nullptr ? "buffered" : "direct";
#else
  const char *path = "direct";
#endif
  ESP_LOGI(TAG,
           "AudioDebug[%s]: frames=%u bytes=%u samples=%u peak=%u peak_dbfs=%.1f rms_dbfs=%.1f "
           "intercom_volume=%.3f state=%s path=%s",
           label, frame_count, (unsigned) bytes, (unsigned) samples, (unsigned) peak, peak_db, rms_db,
           this->volume_.load(std::memory_order_relaxed), this->get_call_state_str(), path);
}

#ifdef USE_INTERCOM_STANDALONE_AUDIO
void IntercomApi::reset_aec_buffers_() {
  if (!this->aec_enabled_.load(std::memory_order_relaxed) || this->spk_ref_buffer_ == nullptr) return;

  this->aec_mic_fill_ = 0;
  // 50 ms wait is generous; called off the audio hot path (only at call start).
  audio_processor::ScopedLock lock(this->spk_ref_mutex_, pdMS_TO_TICKS(50));
  if (lock) {
    this->spk_ref_buffer_->reset();
    // Pre-fill with silence to bake in the configured DMA + acoustic delay.
    const size_t delay_bytes = (SAMPLE_RATE * this->aec_ref_delay_ms_ / 1000) * sizeof(int16_t);
    uint8_t zeros[256] = {};
    size_t remaining = delay_bytes;
    while (remaining > 0) {
      size_t chunk = std::min(remaining, sizeof(zeros));
      this->spk_ref_buffer_->write(zeros, chunk);
      remaining -= chunk;
    }
    ESP_LOGD(TAG, "AEC buffers reset, pre-filled %ums silence", (unsigned) this->aec_ref_delay_ms_);
  }
}

void IntercomApi::set_aec_enabled(bool enabled) {
  if (enabled) {
    if (this->aec_ == nullptr || !this->aec_->is_initialized()) {
      ESP_LOGW(TAG, "Cannot enable AEC: not initialized");
      this->aec_enabled_.store(false, std::memory_order_relaxed);
      return;
    }
    // Lazy alloc on first enable (~13 KB).
    if (this->aec_mic_ == nullptr) {
      const size_t frame_samples = static_cast<size_t>(this->aec_frame_samples_);
      const size_t ref_delay_bytes = (SAMPLE_RATE * this->aec_ref_delay_ms_ / 1000) * sizeof(int16_t);
      this->spk_ref_buffer_ = audio_processor::create_prefer_psram(
          ref_delay_bytes + RX_BUFFER_SIZE, "intercom.spk_ref");
      RAMAllocator<int16_t> aec_alloc = this->buffers_in_psram_
          ? RAMAllocator<int16_t>()
          : RAMAllocator<int16_t>(RAMAllocator<int16_t>::ALLOC_INTERNAL);
      this->aec_mic_ = aec_alloc.allocate(frame_samples);
      this->aec_ref_ = aec_alloc.allocate(frame_samples);
      this->aec_out_ = aec_alloc.allocate(frame_samples);

      if (!this->spk_ref_buffer_ || !this->aec_mic_ || !this->aec_ref_ || !this->aec_out_) {
        ESP_LOGE(TAG, "AEC buffer allocation failed");
        this->spk_ref_buffer_.reset();
        release_i16_buffer(aec_alloc, &this->aec_mic_, frame_samples);
        release_i16_buffer(aec_alloc, &this->aec_ref_, frame_samples);
        release_i16_buffer(aec_alloc, &this->aec_out_, frame_samples);
        this->aec_enabled_.store(false, std::memory_order_relaxed);
        return;
      }
      ESP_LOGD(TAG, "AEC buffers allocated (frame=%d samples, ref_buf=%zu bytes, delay=%ums)",
               this->aec_frame_samples_, ref_delay_bytes + RX_BUFFER_SIZE,
               (unsigned) this->aec_ref_delay_ms_);
    }
  }
  this->aec_enabled_.store(enabled, std::memory_order_relaxed);
  if (enabled) {
    this->reset_aec_buffers_();
  } else {
    this->aec_mic_fill_ = 0;
  }
  ESP_LOGI(TAG, "AEC %s", enabled ? "enabled" : "disabled");
}
#endif

// === TX Task (Core 0) - Mic to Network ===

void IntercomApi::tx_task(void *param) {
  static_cast<IntercomApi *>(param)->tx_task_();
}

bool IntercomApi::is_tx_stream_ready_() const {
  return this->active_.load(std::memory_order_acquire) &&
         this->streaming_.load(std::memory_order_acquire) &&
         this->transport_ != nullptr && this->transport_->is_connected();
}

void IntercomApi::send_chunk_(const uint8_t *data, size_t length) {
  if (!this->is_tx_stream_ready_())
    return;
  if (this->audio_debug_) {
    this->debug_log_pcm_level_("tx_network", data, length,
                               this->audio_debug_last_tx_log_ms_, this->audio_debug_tx_frames_);
  }
  this->transport_->send_audio_frame(data, length);
}

#ifdef USE_INTERCOM_STANDALONE_AUDIO
void IntercomApi::process_aec_chunk_(const uint8_t *audio_chunk) {
  // Drain a 512-sample chunk into aec_mic_ in frame_size pieces, process
  // each complete frame, forward via send_chunk_. aec_mic_fill_ persists
  // across calls so partial frames resume on the next chunk.
  const int16_t *mic_samples = reinterpret_cast<const int16_t *>(audio_chunk);
  const size_t num_samples = AUDIO_CHUNK_SIZE / sizeof(int16_t);
  const size_t frame_size = static_cast<size_t>(this->aec_frame_samples_);
  const size_t ref_bytes_needed = frame_size * sizeof(int16_t);
  const size_t out_bytes = frame_size * sizeof(int16_t);
  size_t consumed = 0;

  while (consumed < num_samples) {
    size_t space = frame_size - this->aec_mic_fill_;
    size_t to_copy = std::min(num_samples - consumed, space);
    memcpy(this->aec_mic_ + this->aec_mic_fill_, mic_samples + consumed, to_copy * sizeof(int16_t));
    this->aec_mic_fill_ += to_copy;
    consumed += to_copy;

    if (this->aec_mic_fill_ < frame_size)
      continue;

    // 2 ms cap: zero-fill on lock miss is better than stalling the task;
    // AEC adapts through the occasional silent reference frame.
    {
      audio_processor::ScopedLock lock(this->spk_ref_mutex_, pdMS_TO_TICKS(2));
      if (lock && this->spk_ref_buffer_->available() >= ref_bytes_needed) {
        this->spk_ref_buffer_->read(this->aec_ref_, ref_bytes_needed, 0);
      } else {
        memset(this->aec_ref_, 0, ref_bytes_needed);
      }
    }

    this->aec_->process(this->aec_mic_, this->aec_ref_, this->aec_out_);
    this->aec_mic_fill_ = 0;

    this->send_chunk_(reinterpret_cast<const uint8_t *>(this->aec_out_), out_bytes);
  }
}
#endif

void IntercomApi::tx_task_() {
  ESP_LOGD(TAG, "TX task started");

  uint8_t *const audio_chunk = this->tx_audio_chunk_;

  while (true) {
    if (!this->is_tx_stream_ready_()) {
#ifdef USE_INTERCOM_STANDALONE_AUDIO
      this->aec_mic_fill_ = 0;
#endif
      ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
      continue;
    }

    if (this->mic_buffer_->available() < AUDIO_CHUNK_SIZE) {
      ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
      continue;
    }

    if (this->mic_buffer_->read(audio_chunk, AUDIO_CHUNK_SIZE, 0) != AUDIO_CHUNK_SIZE)
      continue;

#ifdef USE_INTERCOM_STANDALONE_AUDIO
    if (this->aec_enabled_.load(std::memory_order_relaxed) &&
        this->aec_ != nullptr && this->aec_mic_ != nullptr) {
      this->process_aec_chunk_(audio_chunk);
      delay(1);  // feed the IDLE watchdog on Core 0
      continue;
    }
#endif

    this->send_chunk_(audio_chunk, AUDIO_CHUNK_SIZE);
    delay(1);
  }
}

// === Speaker Task (Core 0) - Network to Speaker ===

#ifdef USE_INTERCOM_STANDALONE_AUDIO
void IntercomApi::speaker_task(void *param) {
  static_cast<IntercomApi *>(param)->speaker_task_();
}

void IntercomApi::speaker_task_() {
  ESP_LOGD(TAG, "Speaker task started");

#ifdef USE_SPEAKER
  uint8_t *const audio_chunk = this->spk_audio_chunk_;
  bool speaker_was_idle = true;

  while (true) {
    // Single-owner model: only this task stops the speaker hardware.
    if (this->speaker_stop_requested_.load(std::memory_order_acquire)) {
      if (this->speaker_ != nullptr) {
        ESP_LOGD(TAG, "Speaker task: stopping speaker");
        this->speaker_->stop();
      }
      if (this->speaker_stopped_sem_ != nullptr) {
        xSemaphoreGive(this->speaker_stopped_sem_);
      }
      while (this->speaker_stop_requested_.load(std::memory_order_acquire)) {
        ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
      }
      continue;
    }

    if (!this->active_.load(std::memory_order_acquire) || this->speaker_ == nullptr) {
      speaker_was_idle = true;
      ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
      continue;
    }

    // First-frame warm-up: give mixer + resampler 20-150 ms to init or
    // we'd starve their ring buffers reading from us.
    if (speaker_was_idle) {
      speaker_was_idle = false;
      for (int i = 0; i < 15; i++) {
        ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(10));
        if (!this->active_.load(std::memory_order_acquire) || this->speaker_ == nullptr) break;
        if (this->speaker_buffer_ != nullptr && this->speaker_buffer_->available() >= AUDIO_CHUNK_SIZE) break;
      }
      continue;
    }

    size_t avail = this->speaker_buffer_->available();
    if (avail < AUDIO_CHUNK_SIZE) {
      ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
      continue;
    }

    // Batch up to 4 chunks per iteration to amortise overhead.
    size_t to_read = avail;
    if (to_read > AUDIO_CHUNK_SIZE * 4) to_read = AUDIO_CHUNK_SIZE * 4;
    to_read = (to_read / AUDIO_CHUNK_SIZE) * AUDIO_CHUNK_SIZE;

    size_t read = this->speaker_buffer_->read(audio_chunk, to_read, 0);

    const float volume = this->volume_.load(std::memory_order_relaxed);
    if (read > 0 && volume > 0.001f) {
      if (this->audio_debug_) {
        this->debug_log_pcm_level_("rx_speaker_task", audio_chunk, read,
                                   this->audio_debug_last_spk_log_ms_, this->audio_debug_spk_frames_);
      }
      this->speaker_->play(audio_chunk, read, 0);

#ifdef USE_INTERCOM_STANDALONE_AUDIO
      // Feed AEC ref with the volume-scaled signal so the AEC sees what
      // hit the driver. 2 ms cap; drop on miss, next iter catches up.
      if (this->aec_enabled_.load(std::memory_order_relaxed) && this->spk_ref_buffer_ != nullptr) {
        audio_processor::ScopedLock lock(this->spk_ref_mutex_, pdMS_TO_TICKS(2));
        if (lock) {
          if (volume != 1.0f) {
            const int16_t *src = reinterpret_cast<const int16_t *>(audio_chunk);
            size_t num_samples = read / sizeof(int16_t);
            scale_block_i16(src, this->spk_ref_scaled_, num_samples, volume);
            this->spk_ref_buffer_->write(this->spk_ref_scaled_, read);
          } else {
            this->spk_ref_buffer_->write(audio_chunk, read);
          }
        }
      }
#endif
    }

    delay(1);  // feed the IDLE watchdog on Core 0
  }
#else
  while (true) {
    delay(1000);
  }
#endif
}
#endif

// === Microphone Callback ===

void IntercomApi::on_microphone_data_(const uint8_t *data, size_t len) {
  if (!this->is_tx_stream_ready_()) {
    return;
  }

  // MicrophoneSource delivers 16-bit regardless of mic hardware.
  const int16_t *src = reinterpret_cast<const int16_t *>(data);
  const size_t total_samples = len / sizeof(int16_t);
  // Skip our gain when esp_audio_stack owns the mic_gain entity (already applied upstream).
  int16_t *mic_converted = this->mic_converted_.load(std::memory_order_acquire);
  const float effective_gain = mic_converted != nullptr
      ? this->mic_gain_.load(std::memory_order_relaxed)
      : 1.0f;
  const bool needs_processing =
      mic_converted != nullptr && (effective_gain != 1.0f || this->dc_offset_removal_);

  if (needs_processing) {
    // Chunk by MIC_CONVERTED_SAMPLES so a long mic frame doesn't overflow
    // the staging buffer when gain/DC processing is on.
    size_t off = 0;
    while (off < total_samples) {
      const size_t chunk = std::min(total_samples - off, kMicConvertedSamples);
      if (this->dc_offset_removal_) {
        for (size_t i = 0; i < chunk; i++) {
          int32_t s = src[off + i];
          // IIR HPF ~2.5 Hz @ 16 kHz (matches esp_audio_stack).
          this->dc_offset_ += (s - this->dc_offset_) >> 10;
          s = s - this->dc_offset_;
          mic_converted[i] = scale_sample(static_cast<int16_t>(s), effective_gain);
        }
      } else {
        scale_block_i16(src + off, mic_converted, chunk, effective_gain);
      }
      this->mic_buffer_->write(mic_converted, chunk * sizeof(int16_t));
      if (this->tx_task_handle_ != nullptr) {
        xTaskNotifyGive(this->tx_task_handle_);
      }
      off += chunk;
    }
  } else {
    this->mic_buffer_->write(data, len);
    if (this->tx_task_handle_ != nullptr) {
      xTaskNotifyGive(this->tx_task_handle_);
    }
  }
}

}  // namespace intercom_api
}  // namespace esphome

#endif  // USE_ESP32
