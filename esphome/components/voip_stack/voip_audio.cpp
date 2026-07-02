#include "voip_stack.h"

#ifdef USE_ESP32

#include <algorithm>
#include <cstdint>
#include <cstring>

#include "esphome/core/hal.h"
#include "esphome/core/log.h"

#include "../audio_processor/audio_utils.h"

namespace esphome {
namespace voip_stack {

static const char *const TAG = "voip_stack.audio";

#ifdef USE_ESPHOME_VOIP_STACK_AUDIO_DEBUG
void VoipStack::debug_log_pcm_level_(const char *label, const uint8_t *pcm, size_t bytes,
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
#endif

#ifdef USE_ESPHOME_VOIP_STACK_MIC
namespace {
static uint32_t frames_for_bytes(size_t bytes, size_t frame_bytes) {
  if (bytes == 0 || frame_bytes == 0)
    return 0;
  return static_cast<uint32_t>((bytes + frame_bytes - 1) / frame_bytes);
}
}  // namespace

// === TX Task (Core 0) - Mic to Network ===

void VoipStack::tx_task(void *param) {
  static_cast<VoipStack *>(param)->tx_task_();
}

bool VoipStack::is_tx_stream_ready_() const {
  return this->active_.load(std::memory_order_acquire) &&
         this->in_call_.load(std::memory_order_acquire) &&
         this->transport_ != nullptr && this->transport_->is_connected();
}

void VoipStack::send_chunk_(const uint8_t *data, size_t length) {
  if (!this->is_tx_stream_ready_())
    return;
#ifdef USE_ESPHOME_VOIP_STACK_AUDIO_DEBUG
  if (this->audio_debug_) {
    this->debug_log_pcm_level_("tx_network", data, length,
                               this->current_tx_audio_format_,
                               this->audio_debug_last_tx_log_ms_, this->audio_debug_tx_frames_);
  }
#endif
  this->transport_->send_audio_frame(data, length);
}

void VoipStack::process_tx_chunk_(const uint8_t *audio_chunk) {
  this->send_chunk_(audio_chunk, this->tx_audio_chunk_bytes_());
}

bool VoipStack::read_tx_chunk_(uint8_t *audio_chunk) {
  const size_t frame_bytes = this->tx_audio_chunk_bytes_();
  return this->mic_buffer_ != nullptr &&
         this->mic_buffer_->read(audio_chunk, frame_bytes, 0) == frame_bytes;
}

void VoipStack::tx_task_() {
  ESP_LOGD(TAG, "TX task started");

  uint8_t *const audio_chunk = this->tx_audio_chunk_;

  while (true) {
    if (!this->is_tx_stream_ready_()) {
      ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
      continue;
    }

    if (this->mic_buffer_ == nullptr) {
      ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(20));
      continue;
    }

    // Capture-clocked TX: the microphone callback is the only pacing source.
    // Drain every complete frame immediately; RTP timestamps are sample-based,
    // so cadence jitter belongs in the receiver jitter buffer, not a TX timer.
    const size_t frame_bytes = this->tx_audio_chunk_bytes_();
    while (this->mic_buffer_->available() >= frame_bytes && this->read_tx_chunk_(audio_chunk)) {
      this->process_tx_chunk_(audio_chunk);
      if (!this->is_tx_stream_ready_())
        break;
    }
    this->media_tx_queue_depth_.store(
        static_cast<uint32_t>(this->mic_buffer_->available() / std::max<size_t>(1, frame_bytes)),
        std::memory_order_relaxed);
    ulTaskNotifyTake(pdTRUE, pdMS_TO_TICKS(20));
  }
}

// === Microphone Callback ===

void VoipStack::on_microphone_data_(const uint8_t *data, size_t len) {
  if (!this->is_tx_stream_ready_()) {
    return;
  }
  if (this->mic_buffer_ == nullptr || data == nullptr || len == 0) {
    return;
  }
#ifdef USE_ESPHOME_VOIP_STACK_AUDIO_DEBUG
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
               "accum_available=%u tx_queue_depth=%u tx_drops=%u tx_drop_bytes=%u tx_format=%u:%u:%u:%u state=%s",
               (unsigned) this->audio_debug_mic_callbacks_, (unsigned) len, (unsigned) delta,
               (unsigned) this->tx_audio_chunk_bytes_(),
               (unsigned) this->mic_buffer_->available(),
               (unsigned) this->media_tx_queue_depth_.load(std::memory_order_relaxed),
               (unsigned) this->media_tx_queue_drops_.load(std::memory_order_relaxed),
               (unsigned) this->media_tx_queue_drop_bytes_.load(std::memory_order_relaxed),
               (unsigned) this->current_tx_audio_format_.sample_rate,
               (unsigned) this->current_tx_audio_format_.pcm_format,
               (unsigned) this->current_tx_audio_format_.channels,
               (unsigned) this->current_tx_audio_format_.frame_ms,
               this->get_call_state_str());
    }
  }
#endif

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
      const size_t frame_bytes = this->tx_audio_chunk_bytes_();
      const size_t bytes = chunk * sizeof(int16_t);
      const size_t free_before = this->mic_buffer_->free();
      const size_t replaced = free_before < bytes ? bytes - free_before : 0;
      const size_t written = this->mic_buffer_->write(mic_converted, bytes);
      const size_t dropped = replaced + (written < bytes ? bytes - written : 0);
      if (dropped > 0) {
        this->media_tx_queue_drops_.fetch_add(frames_for_bytes(dropped, frame_bytes), std::memory_order_relaxed);
#ifdef USE_ESPHOME_VOIP_STACK_AUDIO_DEBUG
        this->media_tx_queue_drop_bytes_.fetch_add(static_cast<uint32_t>(dropped), std::memory_order_relaxed);
#endif
      }
      if (this->tx_task_handle_ != nullptr) {
        xTaskNotifyGive(this->tx_task_handle_);
      }
      off += chunk;
    }
  } else {
    const size_t frame_bytes = this->tx_audio_chunk_bytes_();
    const size_t free_before = this->mic_buffer_->free();
    const size_t replaced = free_before < len ? len - free_before : 0;
    const size_t written = this->mic_buffer_->write(data, len);
    const size_t dropped = replaced + (written < len ? len - written : 0);
    if (dropped > 0) {
      this->media_tx_queue_drops_.fetch_add(frames_for_bytes(dropped, frame_bytes), std::memory_order_relaxed);
#ifdef USE_ESPHOME_VOIP_STACK_AUDIO_DEBUG
      this->media_tx_queue_drop_bytes_.fetch_add(static_cast<uint32_t>(dropped), std::memory_order_relaxed);
#endif
    }
    if (this->tx_task_handle_ != nullptr) {
      xTaskNotifyGive(this->tx_task_handle_);
    }
  }
}
#endif  // USE_ESPHOME_VOIP_STACK_MIC

#ifdef USE_ESPHOME_VOIP_STACK_SPEAKER
namespace {
static int16_t rtp_sequence_delta(uint16_t sequence, uint16_t reference) {
  return static_cast<int16_t>(sequence - reference);
}
}  // namespace

void VoipStack::rx_task(void *param) {
  static_cast<VoipStack *>(param)->rx_task_();
}

void VoipStack::enqueue_rx_frame_(const TransportAudioFrame &frame) {
  if (this->rx_jitter_pcm_storage_ == nullptr || frame.pcm == nullptr || frame.bytes == 0)
    return;
  const size_t expected = this->current_rx_audio_format_.nominal_frame_bytes();
  if (frame.bytes != expected || frame.bytes > this->rx_audio_chunk_alloc_bytes_)
    return;

  LockGuard lock(this->rx_jitter_mutex_);
  uint16_t sequence = frame.sequence;
  if (!frame.has_rtp_metadata) {
    sequence = this->rx_jitter_next_sequence_valid_
                   ? static_cast<uint16_t>(this->rx_jitter_next_sequence_ + this->rx_jitter_valid_count_)
                   : 0;
  }

  if (!this->rx_jitter_next_sequence_valid_) {
    this->rx_jitter_next_sequence_ = sequence;
    this->rx_jitter_next_sequence_valid_ = true;
  }

  if (!this->rx_jitter_buffering_) {
    const int16_t delta = rtp_sequence_delta(sequence, this->rx_jitter_next_sequence_);
    if (delta < 0) {
#ifdef USE_ESPHOME_VOIP_STACK_AUDIO_DEBUG
      this->audio_debug_rx_late_frames_.fetch_add(1, std::memory_order_relaxed);
#endif
      return;
    }
    if (delta >= static_cast<int16_t>(VoipStack::kRxQueuedFrames)) {
      for (auto &slot : this->rx_jitter_slots_) {
        slot.valid = false;
        slot.bytes = 0;
      }
      this->rx_jitter_valid_count_ = 0;
      this->rx_jitter_buffering_ = true;
      this->rx_jitter_next_sequence_ = sequence;
      this->media_rx_queue_drops_.fetch_add(1, std::memory_order_relaxed);
    }
  }

  RxJitterSlot &slot = this->rx_jitter_slots_[sequence % VoipStack::kRxQueuedFrames];
  if (slot.valid) {
    if (slot.sequence == sequence) {
#ifdef USE_ESPHOME_VOIP_STACK_AUDIO_DEBUG
      this->audio_debug_rx_duplicate_frames_.fetch_add(1, std::memory_order_relaxed);
#endif
      return;
    }
    slot.valid = false;
    slot.bytes = 0;
    if (this->rx_jitter_valid_count_ > 0)
      this->rx_jitter_valid_count_--;
    this->media_rx_queue_drops_.fetch_add(1, std::memory_order_relaxed);
  }

  memcpy(slot.pcm, frame.pcm, frame.bytes);
  slot.valid = true;
  slot.sequence = sequence;
  slot.timestamp = frame.timestamp;
  slot.bytes = frame.bytes;
  this->rx_jitter_valid_count_++;

  if (this->rx_jitter_buffering_ && this->rx_jitter_valid_count_ >= VoipStack::kRxPrebufferFrames) {
    this->rx_jitter_buffering_ = false;
    this->rx_underrun_start_ms_ = 0;
  }
  this->media_rx_queue_depth_.store(this->rx_jitter_valid_count_, std::memory_order_relaxed);
  if (this->rx_task_handle_ != nullptr) {
    xTaskNotifyGive(this->rx_task_handle_);
  }
}

VoipStack::RxPlayoutReadResult VoipStack::read_rx_frame_(uint8_t *audio_chunk, uint16_t *sequence,
                                                         uint32_t *timestamp, bool *has_metadata) {
  if (this->rx_jitter_pcm_storage_ == nullptr || audio_chunk == nullptr)
    return RxPlayoutReadResult::BUFFERING;

  LockGuard lock(this->rx_jitter_mutex_);
  if (!this->rx_jitter_next_sequence_valid_ || this->rx_jitter_buffering_) {
    if (this->rx_jitter_next_sequence_valid_ &&
        this->rx_jitter_valid_count_ >= VoipStack::kRxPrebufferFrames) {
      this->rx_jitter_buffering_ = false;
    } else {
      return RxPlayoutReadResult::BUFFERING;
    }
  }

  RxJitterSlot &slot = this->rx_jitter_slots_[this->rx_jitter_next_sequence_ % VoipStack::kRxQueuedFrames];
  if (slot.valid && slot.sequence == this->rx_jitter_next_sequence_) {
    if (sequence != nullptr) *sequence = slot.sequence;
    if (timestamp != nullptr) *timestamp = slot.timestamp;
    if (has_metadata != nullptr) *has_metadata = true;
    memcpy(audio_chunk, slot.pcm, slot.bytes);
    slot.valid = false;
    slot.bytes = 0;
    if (this->rx_jitter_valid_count_ > 0)
      this->rx_jitter_valid_count_--;
    this->rx_jitter_next_sequence_ = static_cast<uint16_t>(this->rx_jitter_next_sequence_ + 1);
    this->media_rx_queue_depth_.store(this->rx_jitter_valid_count_, std::memory_order_relaxed);
    return RxPlayoutReadResult::FRAME;
  }

  if (this->rx_jitter_valid_count_ == 0) {
    this->rx_jitter_buffering_ = true;
    this->rx_jitter_next_sequence_valid_ = false;
    this->media_rx_queue_depth_.store(0, std::memory_order_relaxed);
    return RxPlayoutReadResult::BUFFERING;
  }

#ifdef USE_ESPHOME_VOIP_STACK_AUDIO_DEBUG
  this->audio_debug_rx_late_frames_.fetch_add(1, std::memory_order_relaxed);
#endif
  this->rx_jitter_next_sequence_ = static_cast<uint16_t>(this->rx_jitter_next_sequence_ + 1);
  this->media_rx_queue_depth_.store(this->rx_jitter_valid_count_, std::memory_order_relaxed);
  return RxPlayoutReadResult::MISSING;
}

void VoipStack::play_rx_frame_(const uint8_t *pcm, size_t bytes, bool synthetic_silence, TickType_t ticks_to_wait) {
  if (this->speaker_ == nullptr || pcm == nullptr || bytes == 0)
    return;
  if (!synthetic_silence && this->rx_silence_chunk_ != nullptr &&
      this->volume_.load(std::memory_order_relaxed) <= 0.001f) {
    pcm = this->rx_silence_chunk_;
  }

  size_t offset = 0;
  uint8_t stalls = 0;
  while (offset < bytes && this->in_call_.load(std::memory_order_acquire)) {
    const size_t written = this->speaker_->play(pcm + offset, bytes - offset, ticks_to_wait);
    if (written == 0) {
      if (++stalls >= 4) {
#ifdef USE_ESPHOME_VOIP_STACK_AUDIO_DEBUG
        this->audio_debug_speaker_short_writes_.fetch_add(1, std::memory_order_relaxed);
#endif
        break;
      }
      continue;
    }
    if (written < bytes - offset) {
#ifdef USE_ESPHOME_VOIP_STACK_AUDIO_DEBUG
      this->audio_debug_speaker_short_writes_.fetch_add(1, std::memory_order_relaxed);
#endif
    }
    stalls = 0;
    offset += written;
  }
  if (synthetic_silence) {
#ifdef USE_ESPHOME_VOIP_STACK_AUDIO_DEBUG
    this->audio_debug_rx_silence_frames_.fetch_add(1, std::memory_order_relaxed);
#endif
  }
}

void VoipStack::rx_task_() {
  ESP_LOGD(TAG, "RX playout task started");

  while (true) {
    if (!this->active_.load(std::memory_order_acquire) ||
        !this->in_call_.load(std::memory_order_acquire)) {
      ulTaskNotifyTake(pdTRUE, portMAX_DELAY);
      continue;
    }

    const TickType_t frame_ticks = std::max<TickType_t>(1, pdMS_TO_TICKS(this->current_rx_audio_format_.frame_ms));
    const RxPlayoutReadResult read_result =
        this->read_rx_frame_(this->rx_audio_chunk_, nullptr, nullptr, nullptr);
    if (read_result == RxPlayoutReadResult::FRAME) {
      this->rx_underrun_start_ms_ = 0;
      // The downstream speaker/mixer owns the hardware cadence. Blocking here
      // for up to one frame gives backpressure when that buffer is full; adding
      // an unconditional delay after a successful write double-paces playout and
      // lets the RTP queue grow until audible gaps appear.
      const size_t frame_bytes = this->current_rx_audio_format_.nominal_frame_bytes();
      this->play_rx_frame_(this->rx_audio_chunk_, frame_bytes, false, frame_ticks);
    } else {
      if (read_result == RxPlayoutReadResult::BUFFERING) {
        ulTaskNotifyTake(pdTRUE, frame_ticks);
        continue;
      }
      ulTaskNotifyTake(pdTRUE, frame_ticks);
      const uint32_t now = millis();
      if (this->rx_underrun_start_ms_ == 0) {
        this->rx_underrun_start_ms_ = read_result == RxPlayoutReadResult::MISSING
                                          ? now - VoipStack::kRxSilenceAfterMs
                                          : now;
      }
      if (now - this->rx_underrun_start_ms_ >= VoipStack::kRxSilenceAfterMs &&
          this->first_audio_received_.load(std::memory_order_acquire) &&
          this->active_.load(std::memory_order_acquire) &&
          this->in_call_.load(std::memory_order_acquire)) {
        const size_t silence_bytes = this->current_rx_audio_format_.nominal_frame_bytes();
        if (this->rx_silence_chunk_ != nullptr && silence_bytes <= this->rx_audio_chunk_alloc_bytes_) {
          this->play_rx_frame_(this->rx_silence_chunk_, silence_bytes, true, frame_ticks);
        }
      }
      continue;
    }
  }
}

void VoipStack::reset_rx_audio_() {
  {
    LockGuard lock(this->rx_jitter_mutex_);
    for (auto &slot : this->rx_jitter_slots_) {
      slot.valid = false;
      slot.bytes = 0;
    }
    this->rx_jitter_valid_count_ = 0;
    this->rx_jitter_buffering_ = true;
    this->rx_jitter_next_sequence_valid_ = false;
    this->rx_jitter_next_sequence_ = 0;
  }
  this->media_rx_queue_depth_.store(0, std::memory_order_relaxed);
  this->media_rx_queue_drops_.store(0, std::memory_order_relaxed);
#ifdef USE_ESPHOME_VOIP_STACK_AUDIO_DEBUG
  this->media_tx_queue_drop_bytes_.store(0, std::memory_order_relaxed);
  this->audio_debug_rx_late_frames_.store(0, std::memory_order_relaxed);
  this->audio_debug_rx_duplicate_frames_.store(0, std::memory_order_relaxed);
  this->audio_debug_rx_silence_frames_.store(0, std::memory_order_relaxed);
  this->audio_debug_speaker_short_writes_.store(0, std::memory_order_relaxed);
#endif
  this->rx_underrun_start_ms_ = 0;
}
#endif  // USE_ESPHOME_VOIP_STACK_SPEAKER

}  // namespace voip_stack
}  // namespace esphome

#endif  // USE_ESP32
