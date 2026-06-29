#include "esphome_voip_stack.h"

#ifdef USE_ESP32

#include <algorithm>
#include <cstdint>
#include <cstring>

#include "esphome/core/hal.h"
#include "esphome/core/log.h"

#include "../audio_processor/audio_utils.h"

namespace esphome {
namespace esphome_voip_stack {

static const char *const TAG = "esphome_voip_stack.audio";

void ESPHomeVoipStack::debug_log_pcm_level_(const char *label, const uint8_t *pcm, size_t bytes,
                                       const AudioFormat &format,
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

  if (format.pcm_format != PcmFormat::S16LE) {
    ESP_LOGI(TAG,
             "AudioDebug[%s]: frames=%u bytes=%u format=%u:%u:%u:%u levels=skipped_non_s16 "
             "voip_volume=%.3f state=%s",
             label, frame_count, (unsigned) bytes,
             (unsigned) format.sample_rate,
             (unsigned) format.pcm_format,
             (unsigned) format.channels,
             (unsigned) format.frame_ms,
             this->volume_.load(std::memory_order_relaxed), this->get_call_state_str());
    return;
  }

  const auto levels = compute_levels_dbfs_i16(reinterpret_cast<const int16_t *>(pcm), samples);
  const char *path = "direct";
  ESP_LOGI(TAG,
           "AudioDebug[%s]: frames=%u bytes=%u samples=%u peak=%u peak_dbfs=%.1f rms_dbfs=%.1f "
           "voip_volume=%.3f state=%s path=%s",
           label, frame_count, (unsigned) bytes, (unsigned) samples, (unsigned) levels.peak,
           levels.peak_dbfs, levels.rms_dbfs,
           this->volume_.load(std::memory_order_relaxed), this->get_call_state_str(), path);
}

#ifdef USE_ESPHOME_VOIP_STACK_MIC
// === TX Task (Core 0) - Mic to Network ===

void ESPHomeVoipStack::tx_task(void *param) {
  static_cast<ESPHomeVoipStack *>(param)->tx_task_();
}

bool ESPHomeVoipStack::is_tx_stream_ready_() const {
  return this->active_.load(std::memory_order_acquire) &&
         this->in_call_.load(std::memory_order_acquire) &&
         this->transport_ != nullptr && this->transport_->is_connected();
}

void ESPHomeVoipStack::send_chunk_(const uint8_t *data, size_t length) {
  if (!this->is_tx_stream_ready_())
    return;
  if (this->audio_debug_) {
    this->debug_log_pcm_level_("tx_network", data, length,
                               this->current_tx_audio_format_,
                               this->audio_debug_last_tx_log_ms_, this->audio_debug_tx_frames_);
  }
  this->transport_->send_audio_frame(data, length);
}

void ESPHomeVoipStack::process_tx_chunk_(const uint8_t *audio_chunk) {
  this->send_chunk_(audio_chunk, this->tx_audio_chunk_bytes_());
}

bool ESPHomeVoipStack::read_tx_chunk_(uint8_t *audio_chunk) {
  const size_t frame_bytes = this->tx_audio_chunk_bytes_();
  return this->mic_buffer_->read(audio_chunk, frame_bytes, 0) == frame_bytes;
}

void ESPHomeVoipStack::tx_task_() {
  ESP_LOGD(TAG, "TX task started");

  uint8_t *const audio_chunk = this->tx_audio_chunk_;
  TickType_t last_wake = xTaskGetTickCount();

  while (true) {
    const size_t frame_bytes = this->tx_audio_chunk_bytes_();
    if (!this->is_tx_stream_ready_()) {
      ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
      last_wake = xTaskGetTickCount();
      continue;
    }

    if (this->mic_buffer_->available() < frame_bytes) {
      ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(1));
      last_wake = xTaskGetTickCount();
      continue;
    }

    if (!this->read_tx_chunk_(audio_chunk)) {
      ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(1));
      last_wake = xTaskGetTickCount();
      continue;
    }

    this->process_tx_chunk_(audio_chunk);

    const uint32_t frame_ms = this->current_tx_audio_frame_ms_.load(std::memory_order_acquire);
    const TickType_t frame_ticks = std::max<TickType_t>(1, pdMS_TO_TICKS(frame_ms));
    vTaskDelayUntil(&last_wake, frame_ticks);
  }
}

// === Microphone Callback ===

void ESPHomeVoipStack::on_microphone_data_(const uint8_t *data, size_t len) {
  if (!this->is_tx_stream_ready_()) {
    return;
  }
  if (this->mic_buffer_ == nullptr || data == nullptr || len == 0) {
    return;
  }
  if (this->audio_debug_) {
    const uint32_t now = millis();
    const uint32_t delta = this->audio_debug_last_mic_callback_ms_ == 0
                               ? 0
                               : now - this->audio_debug_last_mic_callback_ms_;
    this->audio_debug_last_mic_callback_ms_ = now;
    this->audio_debug_mic_callbacks_++;
    if (now - this->audio_debug_last_mic_log_ms_ >= 1000) {
      this->audio_debug_last_mic_log_ms_ = now;
      ESP_LOGI(TAG,
               "AudioDebug[mic_callback]: callbacks=%u len=%u delta_ms=%u tx_frame_bytes=%u "
               "buffer_available=%u tx_format=%u:%u:%u:%u state=%s",
               (unsigned) this->audio_debug_mic_callbacks_, (unsigned) len, (unsigned) delta,
               (unsigned) this->tx_audio_chunk_bytes_(),
               (unsigned) this->mic_buffer_->available(),
               (unsigned) this->current_tx_audio_format_.sample_rate,
               (unsigned) this->current_tx_audio_format_.pcm_format,
               (unsigned) this->current_tx_audio_format_.channels,
               (unsigned) this->current_tx_audio_format_.frame_ms,
               this->get_call_state_str());
    }
  }

  // Skip our gain when esp_audio_stack owns the mic_gain entity (already applied upstream).
  int16_t *mic_converted = this->mic_converted_.load(std::memory_order_acquire);
  const float effective_gain = mic_converted != nullptr
      ? this->mic_gain_.load(std::memory_order_relaxed)
      : 1.0f;
  const bool needs_processing =
      mic_converted != nullptr && (effective_gain != 1.0f || this->dc_offset_removal_);

  if (needs_processing) {
    const int16_t *src = reinterpret_cast<const int16_t *>(data);
    const size_t total_samples = len / sizeof(int16_t);
    // Chunk by MIC_CONVERTED_SAMPLES so a long mic frame doesn't overflow
    // the staging buffer when gain/DC processing is on.
    size_t off = 0;
    while (off < total_samples) {
      const size_t chunk = std::min(total_samples - off, this->mic_processing_samples_());
      if (this->dc_offset_removal_) {
        for (size_t i = 0; i < chunk; i++) {
          mic_converted[i] = scale_sample(this->dc_blocker_.process(src[off + i]), effective_gain);
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
#endif  // USE_ESPHOME_VOIP_STACK_MIC

}  // namespace esphome_voip_stack
}  // namespace esphome

#endif  // USE_ESP32
