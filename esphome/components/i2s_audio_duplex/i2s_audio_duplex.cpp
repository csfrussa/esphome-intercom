#include "i2s_audio_duplex.h"

#ifdef USE_ESP32

#include <esp_heap_caps.h>
#include <algorithm>
#include <cmath>
#include <cstring>

#include "esphome/core/defines.h"
#include "esphome/core/hal.h"
#include "esphome/core/log.h"

#ifdef USE_AUDIO_PROCESSOR
#include "../audio_processor/audio_processor.h"
#endif
#include "../audio_processor/ring_buffer_caps.h"
#include "../audio_processor/scoped_lock.h"
#include "../audio_processor/task_utils.h"

namespace esphome {
namespace i2s_audio_duplex {

static const char *const TAG = "i2s_duplex";

// Audio parameters
// IDF default I2S channel config uses 6 descriptors; upstream ESPHome I2S
// speaker keeps ~60 ms of DMA headroom (4 x 15 ms). Keep the duplex driver on
// the same budget: 6 descriptors x 10 ms, instead of the older 8 x 10 ms that
// consumed extra DMA-capable internal RAM on full AFE targets.
static const size_t DMA_BUFFER_COUNT = 6;
static const uint32_t DMA_BUFFER_DURATION_MS = 10;
// Minimum speaker buffer for very small configured durations. The default comes
// from the speaker platform schema (`buffer_duration: 500ms`) to match native
// ESPHome I2S speaker semantics.
static const size_t SPEAKER_BUFFER_MIN_BYTES = 2048;

namespace {
static const int16_t Q15_VOLUME_FACTORS[] = {
    0,     116,   122,   130,   137,   146,   154,   163,   173,   183,   194,   206,   218,   231,   244,
    259,   274,   291,   308,   326,   345,   366,   388,   411,   435,   461,   488,   517,   548,   580,
    615,   651,   690,   731,   774,   820,   868,   920,   974,   1032,  1094,  1158,  1227,  1300,  1377,
    1459,  1545,  1637,  1734,  1837,  1946,  2061,  2184,  2313,  2450,  2596,  2750,  2913,  3085,  3269,
    3462,  3668,  3885,  4116,  4360,  4619,  4893,  5183,  5490,  5816,  6161,  6527,  6914,  7324,  7758,
    8218,  8706,  9222,  9770,  10349, 10963, 11613, 12302, 13032, 13805, 14624, 15491, 16410, 17384, 18415,
    19508, 20665, 21891, 23189, 24565, 26022, 27566, 29201, 30933, 32767};
static constexpr size_t Q15_VOLUME_FACTORS_COUNT = sizeof(Q15_VOLUME_FACTORS) / sizeof(Q15_VOLUME_FACTORS[0]);

int16_t volume_factor_to_q15(float volume) {
  if (!(volume > 0.0f)) return 0;
  if (volume >= 1.0f) return 32767;
  size_t idx = static_cast<size_t>(volume * (Q15_VOLUME_FACTORS_COUNT - 1));
  if (idx >= Q15_VOLUME_FACTORS_COUNT) idx = Q15_VOLUME_FACTORS_COUNT - 1;
  return Q15_VOLUME_FACTORS[idx];
}

int16_t multiply_q15(int16_t a, int16_t b) {
  if (a <= 0 || b <= 0) return 0;
  const int32_t value = (static_cast<int32_t>(a) * static_cast<int32_t>(b) + 16384) >> 15;
  return value >= 32767 ? 32767 : static_cast<int16_t>(value);
}

float sanitize_gain_factor(float gain) {
  if (!std::isfinite(gain) || gain <= 0.0f) {
    return 0.0f;
  }
  // YAML exposes mic attenuation up to 32x and mic_gain_db +30 dB maps to
  // ~31.6x. Clamp direct C++ callers to the same practical envelope.
  return gain > 32.0f ? 32.0f : gain;
}
}  // namespace

void I2SAudioDuplex::set_mic_gain(float gain) {
  this->mic_gain_.store(sanitize_gain_factor(gain), std::memory_order_relaxed);
}

void I2SAudioDuplex::set_mic_attenuation(float atten) {
  this->mic_attenuation_.store(sanitize_gain_factor(atten), std::memory_order_relaxed);
}

void I2SAudioDuplex::set_speaker_volume(float volume) {
  if (!(volume > 0.0f)) {
    volume = 0.0f;
  } else if (volume > 1.0f) {
    volume = 1.0f;
  }
  this->set_speaker_volume_q15(volume_factor_to_q15(volume));
}

void I2SAudioDuplex::set_speaker_volume_q15(int16_t q15) {
  if (q15 < 0) q15 = 0;
  const int16_t previous = this->master_volume_q15_.exchange(q15, std::memory_order_relaxed);
  if (previous != q15) this->update_combined_speaker_volume_();
}

void I2SAudioDuplex::set_output_volume(float volume) {
  if (!(volume > 0.0f)) {
    volume = 0.0f;
  } else if (volume > 1.0f) {
    volume = 1.0f;
  }
  this->set_output_volume_q15(volume_factor_to_q15(volume));
}

void I2SAudioDuplex::set_output_volume_q15(int16_t q15) {
  if (q15 < 0) q15 = 0;
  const int16_t previous = this->output_volume_q15_.exchange(q15, std::memory_order_relaxed);
  if (previous != q15) this->update_combined_speaker_volume_();
}

void I2SAudioDuplex::update_combined_speaker_volume_() {
  const int16_t output_q15 = this->output_volume_q15_.load(std::memory_order_relaxed);
  const int16_t master_q15 = this->master_volume_q15_.load(std::memory_order_relaxed);
  const int16_t combined_q15 = multiply_q15(output_q15, master_q15);
  const float linear = static_cast<float>(combined_q15) / 32767.0f;
  this->speaker_volume_.store(linear, std::memory_order_relaxed);
  const int16_t previous = this->speaker_volume_q15_.exchange(combined_q15, std::memory_order_relaxed);
  if (previous != combined_q15) {
    ESP_LOGD(TAG, "Speaker software volume: output_q15=%d master_q15=%d combined_q15=%d linear=%.3f previous_q15=%d",
             output_q15, master_q15, combined_q15, linear, previous);
  }
}

const char *I2SAudioDuplex::runtime_state_to_string_(DuplexRuntimeState state) {
  switch (state) {
    case DuplexRuntimeState::IDLE:
      return "idle";
    case DuplexRuntimeState::MIC:
      return "mic";
    case DuplexRuntimeState::SPEAKER:
      return "speaker";
    case DuplexRuntimeState::DUPLEX:
      return "duplex";
  }
  return "unknown";
}

DuplexRuntimeState I2SAudioDuplex::compute_runtime_state_() const {
  const bool mic = this->has_mic_consumers_.load(std::memory_order_relaxed);
  const bool speaker = this->speaker_running_.load(std::memory_order_relaxed);
  if (mic && speaker)
    return DuplexRuntimeState::DUPLEX;
  if (mic)
    return DuplexRuntimeState::MIC;
  if (speaker)
    return DuplexRuntimeState::SPEAKER;
  return DuplexRuntimeState::IDLE;
}

void I2SAudioDuplex::update_runtime_state_() {
  const auto next = this->compute_runtime_state_();
  const auto next_raw = static_cast<uint8_t>(next);
  const auto prev_raw = this->runtime_state_.exchange(next_raw, std::memory_order_relaxed);
  if (prev_raw == next_raw)
    return;
  const char *state = runtime_state_to_string_(next);
  ESP_LOGD(TAG, "Runtime state: %s", state);
  this->state_trigger_.trigger(std::string(state));
}

const char *I2SAudioDuplex::i2s_hardware_state_to_string_(I2SHardwareState state) {
  switch (state) {
    case I2SHardwareState::UNPREPARED:
      return "unprepared";
    case I2SHardwareState::PREPARING:
      return "preparing";
    case I2SHardwareState::READY:
      return "ready";
    case I2SHardwareState::RUNNING:
      return "running";
    case I2SHardwareState::STOPPING:
      return "stopping";
    case I2SHardwareState::ERROR:
      return "error";
  }
  return "unknown";
}

void I2SAudioDuplex::set_i2s_hardware_state_(I2SHardwareState state) {
  const auto next_raw = static_cast<uint8_t>(state);
  const auto prev_raw = this->i2s_hardware_state_.exchange(next_raw, std::memory_order_relaxed);
  if (prev_raw != next_raw) {
    ESP_LOGD(TAG, "I2S hardware state: %s", i2s_hardware_state_to_string_(state));
  }
}

void I2SAudioDuplex::log_memory_snapshot_(const char *label) const {
  ESP_LOGI(TAG,
           "Memory[%s]: internal_free=%u largest_internal=%u dma_free=%u largest_dma=%u psram_free=%u",
           label,
           (unsigned) heap_caps_get_free_size(MALLOC_CAP_INTERNAL),
           (unsigned) heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL),
           (unsigned) heap_caps_get_free_size(MALLOC_CAP_DMA),
           (unsigned) heap_caps_get_largest_free_block(MALLOC_CAP_DMA),
           (unsigned) heap_caps_get_free_size(MALLOC_CAP_SPIRAM));
}

// Helper: get MCLK multiple enum from integer value
static i2s_mclk_multiple_t get_mclk_multiple(uint32_t mult) {
  switch (mult) {
    case 128: return I2S_MCLK_MULTIPLE_128;
    case 384: return I2S_MCLK_MULTIPLE_384;
    case 512: return I2S_MCLK_MULTIPLE_512;
    default: return I2S_MCLK_MULTIPLE_256;
  }
}

// Helper: get STD slot config for the configured comm format (0=philips, 1=msb, 2=pcm_short, 3=pcm_long)
// Note: PCM short/long are TDM-only in ESP-IDF; falls back to Philips in STD mode
static i2s_std_slot_config_t get_std_slot_config(uint8_t fmt, i2s_data_bit_width_t bw, i2s_slot_mode_t mode) {
  switch (fmt) {
    case 1: return I2S_STD_MSB_SLOT_DEFAULT_CONFIG(bw, mode);
    default: return I2S_STD_PHILIPS_SLOT_DEFAULT_CONFIG(bw, mode);
  }
}

#if SOC_I2S_SUPPORTS_TDM
// Helper: get TDM slot config for the configured comm format
static i2s_tdm_slot_config_t get_tdm_slot_config(uint8_t fmt, i2s_data_bit_width_t bw,
                                                   i2s_slot_mode_t mode, i2s_tdm_slot_mask_t mask) {
  switch (fmt) {
    case 1: return I2S_TDM_MSB_SLOT_DEFAULT_CONFIG(bw, mode, mask);
    case 2: return I2S_TDM_PCM_SHORT_SLOT_DEFAULT_CONFIG(bw, mode, mask);
    case 3: return I2S_TDM_PCM_LONG_SLOT_DEFAULT_CONFIG(bw, mode, mask);
    default: return I2S_TDM_PHILIPS_SLOT_DEFAULT_CONFIG(bw, mode, mask);
  }
}
#endif  // SOC_I2S_SUPPORTS_TDM

void I2SAudioDuplex::setup() {
  ESP_LOGCONFIG(TAG, "Setting up I2S Audio Duplex...");

  // Mutex for the mic consumer registry. Plain FreeRTOS mutex (used as lock,
  // not as a counting semaphore); replaces std::mutex to keep the public
  // header free of <mutex>.
  this->mic_consumers_mutex_ = xSemaphoreCreateMutex();
  if (this->mic_consumers_mutex_ == nullptr) {
    ESP_LOGE(TAG, "Failed to create mic_consumers_mutex_");
    this->mark_failed();
    return;
  }

  // Compute decimation ratio: only active when output_sample_rate is explicitly set
  // and differs from sample_rate. If not set, ratio stays 1 (no decimation, zero overhead).
  if (this->output_sample_rate_ > 0 && this->output_sample_rate_ != this->sample_rate_) {
    this->decimation_ratio_ = this->sample_rate_ / this->output_sample_rate_;
    if (this->decimation_ratio_ * this->output_sample_rate_ != this->sample_rate_) {
      ESP_LOGE(TAG, "sample_rate (%u) must be an exact multiple of output_sample_rate (%u)",
               (unsigned)this->sample_rate_, (unsigned)this->output_sample_rate_);
      this->mark_failed();
      return;
    }
    if (this->decimation_ratio_ > 6) {
      ESP_LOGE(TAG, "Decimation ratio %u exceeds maximum of 6", (unsigned)this->decimation_ratio_);
      this->mark_failed();
      return;
    }
    this->mic_decimator_.init(this->decimation_ratio_);
    this->play_ref_decimator_.init(this->decimation_ratio_);
    this->mic_decimator_.set_use_float_fir(this->fir_decimator_custom_);
    this->play_ref_decimator_.set_use_float_fir(this->fir_decimator_custom_);
    // rx_decimator_ is lazily initialized inside audio_session_ once the
    // processor has reported its frame_spec and we know how many channels
    // the RX stream carries (mono / stereo-AEC / TDM-with-or-without-second-mic).
    ESP_LOGI(TAG, "Multi-rate: bus=%uHz, output=%uHz, ratio=%u",
             (unsigned)this->sample_rate_, (unsigned)this->output_sample_rate_,
             (unsigned)this->decimation_ratio_);
  }

  // Speaker ring buffer: stores mono PCM at bus rate (e.g. 48kHz).
  // PREFER_PSRAM: staging buffer between API play() and the i2s write path, not
  // realtime-critical itself (the task drains it at priority 19), so PSRAM is fine.
  const size_t speaker_bytes_per_second = this->sample_rate_ * sizeof(int16_t);
  this->speaker_buffer_size_ = std::max<size_t>(
      SPEAKER_BUFFER_MIN_BYTES,
      (speaker_bytes_per_second * static_cast<size_t>(this->speaker_buffer_duration_ms_)) / 1000);
  this->speaker_buffer_ = audio_processor::create_prefer_psram(
      this->speaker_buffer_size_, "i2s_duplex.speaker");
  if (!this->speaker_buffer_) {
    ESP_LOGE(TAG, "Failed to create speaker ring buffer (%u bytes)", (unsigned)this->speaker_buffer_size_);
    this->mark_failed();
    return;
  }
  this->log_memory_snapshot_("after_speaker_ring");

  // AEC reference (mono mode only; stereo/TDM get ref from I2S RX).
  // direct_aec_ref_ is allocated lazily in allocate_audio_buffers_() once the
  // processor frame spec is known. Storage matches input_frame_bytes because
  // AudioProcessor::process consumes in_ref at input_samples length; the TX-side
  // decimator writes one input-side reference frame here at the processor rate,
  // not at the bus rate.

  // Create the permanent audio task during component setup, then park it
  // until start() flips duplex_running_. This reserves the stack/TCB before
  // Wi-Fi/API/VA/MWW churn can fragment internal RAM, and removes xTaskCreate
  // from the first wake-word/audio activation path.
  const BaseType_t core = this->task_core_ >= 0 ? this->task_core_ : tskNO_AFFINITY;
  const uint32_t stack_words = this->task_stack_size_ / sizeof(StackType_t);
  if (!audio_processor::start_pinned_task(
          audio_task, "i2s_duplex", stack_words, this, this->task_priority_,
          core, this->audio_stack_in_psram_, TAG,
          &this->audio_task_handle_, &this->audio_task_tcb_,
          &this->audio_task_stack_)) {
    ESP_LOGE(TAG, "Failed to create permanent audio task");
    this->has_i2s_error_.store(true, std::memory_order_relaxed);
    this->mark_failed();
    return;
  }

  if (!this->prepare_i2s_channels_()) {
    ESP_LOGE(TAG, "Failed to prepare I2S channels");
    this->mark_failed();
    return;
  }

  ESP_LOGI(TAG, "I2S Audio Duplex ready (speaker_buf=%u bytes, task precreated)",
           (unsigned)this->speaker_buffer_size_);
}

void I2SAudioDuplex::set_processor(AudioProcessor *processor) {
  this->processor_ = processor;
  this->processor_enabled_.store(processor != nullptr, std::memory_order_relaxed);
  // Note: direct_aec_ref_ is allocated later in allocate_audio_buffers_() once
  // the processor frame spec is known for the current audio session.
}

void I2SAudioDuplex::sync_processor_background_consumer_() {
#ifdef USE_AUDIO_PROCESSOR
  const bool want_background =
      this->processor_ != nullptr && this->processor_->wants_background_input();
  const bool registered =
      this->processor_background_consumer_registered_.load(std::memory_order_relaxed);

  if (want_background && !registered) {
    if (this->register_mic_consumer(this)) {
      this->processor_background_consumer_registered_.store(true, std::memory_order_relaxed);
      ESP_LOGI(TAG, "Processor background mic consumer registered");
    }
  } else if (!want_background && registered) {
    this->processor_background_consumer_registered_.store(false, std::memory_order_relaxed);
    this->unregister_mic_consumer(this);
    ESP_LOGI(TAG, "Processor background mic consumer unregistered");
  }
#endif
}

void I2SAudioDuplex::request_audio_preallocation_() {
  if (this->prealloc_attempted_.load(std::memory_order_acquire) ||
      this->prealloc_requested_.load(std::memory_order_acquire) ||
      this->audio_task_handle_ == nullptr) {
    return;
  }
  bool frame_shape_known = true;
#ifdef USE_AUDIO_PROCESSOR
  if (this->processor_ != nullptr) {
    auto spec = this->processor_->frame_spec();
    frame_shape_known = spec.input_samples > 0 && spec.output_samples > 0;
  }
#endif
  if (!frame_shape_known) {
    return;
  }
  this->prealloc_requested_.store(true, std::memory_order_release);
  xTaskNotifyGive(this->audio_task_handle_);
}

void I2SAudioDuplex::loop() {
  this->sync_processor_background_consumer_();

  if (!this->duplex_running_.load(std::memory_order_relaxed) &&
      this->audio_task_idle_.load(std::memory_order_relaxed)) {
    this->request_audio_preallocation_();
  }

  // Pick up the deferred I2S channel teardown queued by stop(). We can
  // only call i2s_channel_disable when the audio task has parked itself
  // in its outer wait loop; doing it earlier races the task's I2S read
  // and produces ESP_ERR_INVALID_STATE warnings. Polling here on the
  // main loop is bounded (one check per tick, no blocking).
  if (this->teardown_pending_.load(std::memory_order_relaxed) &&
      this->audio_task_idle_.load(std::memory_order_relaxed)) {
    this->disable_i2s_channels_();
    this->teardown_pending_.store(false, std::memory_order_relaxed);
    ESP_LOGI(TAG, "Duplex audio stopped");
  }
}

void I2SAudioDuplex::dump_config() {
  ESP_LOGCONFIG(TAG, "I2S Audio Duplex:");
  ESP_LOGCONFIG(TAG, "  LRCLK Pin: %d", this->lrclk_pin_);
  ESP_LOGCONFIG(TAG, "  BCLK Pin: %d", this->bclk_pin_);
  ESP_LOGCONFIG(TAG, "  MCLK Pin: %d", this->mclk_pin_);
  ESP_LOGCONFIG(TAG, "  DIN Pin: %d", this->din_pin_);
  ESP_LOGCONFIG(TAG, "  DOUT Pin: %d", this->dout_pin_);
  ESP_LOGCONFIG(TAG, "  I2S Port: %u", this->i2s_num_);
  ESP_LOGCONFIG(TAG, "  I2S Role: %s", this->i2s_mode_secondary_ ? "secondary (slave)" : "primary (master)");
  ESP_LOGCONFIG(TAG, "  I2S Bus Rate: %u Hz", (unsigned)this->sample_rate_);
  ESP_LOGCONFIG(TAG, "  I2S Bits Per Sample: %u", this->bits_per_sample_);
  if (this->slot_bit_width_ > 0) {
    ESP_LOGCONFIG(TAG, "  Slot Bit Width: %u", this->slot_bit_width_);
  }
  ESP_LOGCONFIG(TAG, "  TX Channels: %u (%s)", this->num_channels_,
                this->num_channels_ == 2 ? "stereo" : "mono");
  ESP_LOGCONFIG(TAG, "  RX Mic Channel: %s", this->mic_channel_right_ ? "RIGHT" : "LEFT");
  static const char *const fmt_names[] = {"Philips", "MSB", "PCM Short", "PCM Long"};
  ESP_LOGCONFIG(TAG, "  Comm Format: %s", fmt_names[this->i2s_comm_fmt_ & 3]);
  ESP_LOGCONFIG(TAG, "  MCLK Multiple: %u", (unsigned)this->mclk_multiple_);
  if (this->use_apll_) {
    ESP_LOGCONFIG(TAG, "  APLL: enabled");
  }
  if (this->correct_dc_offset_) {
    ESP_LOGCONFIG(TAG, "  DC Offset Correction: enabled");
  }
  if (this->decimation_ratio_ > 1) {
    ESP_LOGCONFIG(TAG, "  Output Rate: %u Hz (decimation x%u)",
                  (unsigned)this->get_output_sample_rate(), (unsigned)this->decimation_ratio_);
    ESP_LOGCONFIG(TAG, "  FIR Decimator: %s",
                  this->fir_decimator_custom_ ? "custom (float scalar, 32-tap Kaiser)"
                                              : "dsps_fird_s16 (esp-dsp SIMD)");
  }
  ESP_LOGCONFIG(TAG, "  Speaker Buffer: %u bytes (%u ms)", (unsigned)this->speaker_buffer_size_,
                (unsigned)this->speaker_buffer_duration_ms_);
  if (this->use_stereo_aec_ref_) {
    ESP_LOGCONFIG(TAG, "  Stereo AEC Reference: %s channel", this->ref_channel_right_ ? "RIGHT" : "LEFT");
  }
  if (this->use_tdm_bus_) {
    if (this->tdm_second_mic_slot_ >= 0) {
      ESP_LOGCONFIG(TAG, "  TDM Reference: %u slots, mic_slots=[%u,%d], ref_slot=%u",
                    this->tdm_total_slots_, this->tdm_mic_slot_,
                    this->tdm_second_mic_slot_, this->tdm_ref_slot_);
    } else {
      ESP_LOGCONFIG(TAG, "  TDM Reference: %u slots, mic_slot=%u, ref_slot=%u",
                    this->tdm_total_slots_, this->tdm_mic_slot_, this->tdm_ref_slot_);
    }
  }
  ESP_LOGCONFIG(TAG, "  AEC: %s", this->processor_ != nullptr ? "enabled" : "disabled");
  ESP_LOGCONFIG(TAG, "  Task: priority=%u, core=%d, stack=%u",
                this->task_priority_, this->task_core_, (unsigned)this->task_stack_size_);
  ESP_LOGCONFIG(TAG, "  I2S Preparation: setup prepares channels to READY");
  ESP_LOGCONFIG(TAG, "  Reset Processor On Speaker Start: %s",
                this->reset_processor_on_speaker_start_ ? "enabled" : "disabled");
  ESP_LOGCONFIG(TAG, "  I2S Hardware State: %s",
                i2s_hardware_state_to_string_(
                    static_cast<I2SHardwareState>(this->i2s_hardware_state_.load(std::memory_order_relaxed))));
#ifdef USE_DUPLEX_TELEMETRY
  ESP_LOGCONFIG(TAG, "  Telemetry Log Interval: %u frames", (unsigned) this->telemetry_log_interval_frames_);
#endif
}

bool I2SAudioDuplex::prepare_i2s_channels_() {
  if (this->tx_handle_ != nullptr || this->rx_handle_ != nullptr) {
    return true;
  }

  ESP_LOGCONFIG(TAG, "Preparing I2S channels in DUPLEX mode...");
  this->set_i2s_hardware_state_(I2SHardwareState::PREPARING);
  this->log_memory_snapshot_("before_i2s_prepare");

  // Map configured bit depth to I2S enum
  // Note: 24-bit data is stored in 32-bit DMA containers (MSB-aligned)
  i2s_data_bit_width_t bit_width;
  switch (this->bits_per_sample_) {
    case 32: bit_width = I2S_DATA_BIT_WIDTH_32BIT; break;
    case 24: bit_width = I2S_DATA_BIT_WIDTH_24BIT; break;
    default: bit_width = I2S_DATA_BIT_WIDTH_16BIT; break;
  }

  // Slot bit width: auto = match data bit width, or explicit override
  i2s_slot_bit_width_t slot_bw = I2S_SLOT_BIT_WIDTH_AUTO;
  if (this->slot_bit_width_ > 0) {
    switch (this->slot_bit_width_) {
      case 32: slot_bw = I2S_SLOT_BIT_WIDTH_32BIT; break;
      case 24: slot_bw = I2S_SLOT_BIT_WIDTH_24BIT; break;
      case 16: slot_bw = I2S_SLOT_BIT_WIDTH_16BIT; break;
      default: slot_bw = I2S_SLOT_BIT_WIDTH_AUTO; break;
    }
  }

  bool need_tx = (this->dout_pin_ >= 0);
  bool need_rx = (this->din_pin_ >= 0);

  if (!need_tx && !need_rx) {
    ESP_LOGE(TAG, "At least one of din_pin or dout_pin must be configured");
    this->set_i2s_hardware_state_(I2SHardwareState::ERROR);
    return false;
  }

  // Channel configuration
  // Clock source: APLL for accurate clocking (ESP32 original only)
  i2s_clock_src_t clk_src = I2S_CLK_SRC_DEFAULT;
#ifdef I2S_CLK_SRC_APLL
  if (this->use_apll_) clk_src = I2S_CLK_SRC_APLL;
#endif
  i2s_mclk_multiple_t mclk_mult = get_mclk_multiple(this->mclk_multiple_);

  // DMA descriptor limit is 4092 bytes. Compute max bytes per frame across
  // TX and RX configs, then clamp dma_frame_num to stay within the limit.
  // RX can be wider than TX (e.g., mono TX but stereo RX for AEC feedback).
  uint32_t bytes_per_sample = (this->bits_per_sample_ > 16) ? 4 : 2;  // 24/32-bit → 4-byte DMA container
  uint32_t tx_bytes_per_frame = this->num_channels_ * bytes_per_sample;
  uint32_t rx_bytes_per_frame = tx_bytes_per_frame;
  if (this->use_stereo_aec_ref_) {
    rx_bytes_per_frame = 2 * bytes_per_sample;  // stereo RX forced
  }
#if SOC_I2S_SUPPORTS_TDM
  if (this->use_tdm_bus_) {
    uint32_t tdm_frame = this->tdm_total_slots_ * bytes_per_sample;
    rx_bytes_per_frame = tdm_frame;
    tx_bytes_per_frame = tdm_frame;
  }
#endif
  uint32_t max_bytes_per_frame = std::max(tx_bytes_per_frame, rx_bytes_per_frame);
  // dma_frame_num scales with sample_rate so each DMA descriptor holds ~10 ms
  // of audio at any rate. A hard-coded sample count would yield 256 ms total
  // DMA latency at 16 kHz vs 85 ms at 48 kHz with the same constant, exceeding
  // the AEC filter tail at low sample rates. esp-skainet boards use the same
  // ~10 ms/descriptor pattern (e.g. 160 frames/desc at 16 kHz).
  uint32_t dma_frame_num = std::max<uint32_t>(
      64, (this->sample_rate_ * DMA_BUFFER_DURATION_MS) / 1000);
  if (max_bytes_per_frame > 0) {
    uint32_t max_frames = 4092 / max_bytes_per_frame;
    if (dma_frame_num > max_frames) {
      dma_frame_num = max_frames;
    }
  }
  i2s_chan_config_t chan_cfg = {
      .id = static_cast<i2s_port_t>(this->i2s_num_),
      .role = this->i2s_mode_secondary_ ? I2S_ROLE_SLAVE : I2S_ROLE_MASTER,
      .dma_desc_num = DMA_BUFFER_COUNT,
      .dma_frame_num = dma_frame_num,
      .auto_clear_after_cb = true,
#if ESP_IDF_VERSION >= ESP_IDF_VERSION_VAL(5, 2, 0)
      .auto_clear_before_cb = false,
      .intr_priority = 0,
#endif
  };

  i2s_chan_handle_t *tx_ptr = need_tx ? &this->tx_handle_ : nullptr;
  i2s_chan_handle_t *rx_ptr = need_rx ? &this->rx_handle_ : nullptr;

  esp_err_t err = i2s_new_channel(&chan_cfg, tx_ptr, rx_ptr);
  if (err != ESP_OK) {
    ESP_LOGE(TAG, "Failed to create I2S channel: %s", esp_err_to_name(err));
    this->set_i2s_hardware_state_(I2SHardwareState::ERROR);
    return false;
  }

  ESP_LOGD(TAG, "I2S channel created: TX=%s RX=%s",
           this->tx_handle_ ? "yes" : "no",
           this->rx_handle_ ? "yes" : "no");

  auto pin_or_nc = [](int pin) -> gpio_num_t {
    return pin >= 0 ? static_cast<gpio_num_t>(pin) : GPIO_NUM_NC;
  };

#if SOC_I2S_SUPPORTS_TDM
  if (this->use_tdm_bus_) {
    // ── TDM MODE: ES7210 multi-slot RX + ES8311 slot-0 TX ──
    // STEREO with 4 slots: DMA contains all 4 interleaved slots, BCLK/FS = 64.
    // ESP-IDF MONO only puts slot 0 in DMA; STEREO gives all active slots.
    // total_slot is derived from slot_mask (not slot_mode), so BCLK doesn't change.
    // ES8311 reads/writes slot 0 as standard I2S (first 16 bits after LRCLK edge).
    // DMA frame = tdm_total_slots × 2 bytes. At 4 slots, 256 frames = 2048 bytes/desc (< 4092 limit).
    i2s_tdm_slot_mask_t tdm_mask = I2S_TDM_SLOT0;
    for (int i = 1; i < this->tdm_total_slots_; i++)
      tdm_mask = static_cast<i2s_tdm_slot_mask_t>(tdm_mask | (I2S_TDM_SLOT0 << i));

    i2s_tdm_config_t tdm_cfg = {
        .clk_cfg = {
            .sample_rate_hz = this->sample_rate_,
            .clk_src = clk_src,
            .ext_clk_freq_hz = 0,
            .mclk_multiple = mclk_mult,
        },
        .slot_cfg = get_tdm_slot_config(this->i2s_comm_fmt_, bit_width, I2S_SLOT_MODE_STEREO, tdm_mask),
        .gpio_cfg = {
            .mclk = pin_or_nc(this->mclk_pin_),
            .bclk = pin_or_nc(this->bclk_pin_),
            .ws = pin_or_nc(this->lrclk_pin_),
            .dout = pin_or_nc(this->dout_pin_),
            .din = pin_or_nc(this->din_pin_),
            .invert_flags = {
                .mclk_inv = false,
                .bclk_inv = false,
                .ws_inv = false,
            },
        },
    };

    // Apply slot_bit_width override BEFORE init
    if (slot_bw != I2S_SLOT_BIT_WIDTH_AUTO) {
      tdm_cfg.slot_cfg.slot_bit_width = slot_bw;
    }

    if (this->tx_handle_) {
      err = i2s_channel_init_tdm_mode(this->tx_handle_, &tdm_cfg);
      if (err != ESP_OK) {
        ESP_LOGE(TAG, "Failed to init TDM TX: %s", esp_err_to_name(err));
        this->deinit_i2s_();
        this->set_i2s_hardware_state_(I2SHardwareState::ERROR);
        return false;
      }
    }
    if (this->rx_handle_) {
      err = i2s_channel_init_tdm_mode(this->rx_handle_, &tdm_cfg);
      if (err != ESP_OK) {
        ESP_LOGE(TAG, "Failed to init TDM RX: %s", esp_err_to_name(err));
        this->deinit_i2s_();
        this->set_i2s_hardware_state_(I2SHardwareState::ERROR);
        return false;
      }
    }

    if (this->tdm_second_mic_slot_ >= 0) {
      ESP_LOGD(TAG, "TDM mode: %d slots, mic_slots=[%d,%d], ref_slot=%d, mask=0x%x",
               this->tdm_total_slots_, this->tdm_mic_slot_, this->tdm_second_mic_slot_,
               this->tdm_ref_slot_, (unsigned) tdm_mask);
    } else {
      ESP_LOGD(TAG, "TDM mode: %d slots, mic_slot=%d, ref_slot=%d, mask=0x%x",
               this->tdm_total_slots_, this->tdm_mic_slot_, this->tdm_ref_slot_, (unsigned) tdm_mask);
    }
  } else
#endif  // SOC_I2S_SUPPORTS_TDM
  {
    // ── STANDARD MODE ──
    i2s_slot_mode_t tx_slot_mode = (this->num_channels_ == 2)
        ? I2S_SLOT_MODE_STEREO : I2S_SLOT_MODE_MONO;
    i2s_std_config_t tx_cfg = {
        .clk_cfg = {
            .sample_rate_hz = this->sample_rate_,
            .clk_src = clk_src,
            .mclk_multiple = mclk_mult,
        },
        .slot_cfg = get_std_slot_config(this->i2s_comm_fmt_, bit_width, tx_slot_mode),
        .gpio_cfg = {
            .mclk = pin_or_nc(this->mclk_pin_),
            .bclk = pin_or_nc(this->bclk_pin_),
            .ws = pin_or_nc(this->lrclk_pin_),
            .dout = pin_or_nc(this->dout_pin_),
            .din = pin_or_nc(this->din_pin_),
            .invert_flags = {
                .mclk_inv = false,
                .bclk_inv = false,
                .ws_inv = false,
            },
        },
    };
    tx_cfg.slot_cfg.slot_mask = (this->num_channels_ == 2)
        ? I2S_STD_SLOT_BOTH : (this->tx_slot_right_ ? I2S_STD_SLOT_RIGHT : I2S_STD_SLOT_LEFT);
    // Apply slot_bit_width override
    if (slot_bw != I2S_SLOT_BIT_WIDTH_AUTO) {
      tx_cfg.slot_cfg.slot_bit_width = slot_bw;
    }

    // RX configuration - always independent of TX num_channels
    i2s_std_config_t rx_cfg = tx_cfg;
    if (this->use_stereo_aec_ref_) {
      rx_cfg.slot_cfg = get_std_slot_config(this->i2s_comm_fmt_, bit_width, I2S_SLOT_MODE_STEREO);
      rx_cfg.slot_cfg.slot_mask = I2S_STD_SLOT_BOTH;
      ESP_LOGD(TAG, "RX configured as STEREO for ES8311 digital feedback AEC");
    } else {
      rx_cfg.slot_cfg = get_std_slot_config(this->i2s_comm_fmt_, bit_width, I2S_SLOT_MODE_MONO);
      rx_cfg.slot_cfg.slot_mask = this->mic_channel_right_ ? I2S_STD_SLOT_RIGHT : I2S_STD_SLOT_LEFT;
    }
    // Apply slot_bit_width override to RX
    if (slot_bw != I2S_SLOT_BIT_WIDTH_AUTO) {
      rx_cfg.slot_cfg.slot_bit_width = slot_bw;
    }

    if (this->tx_handle_) {
      err = i2s_channel_init_std_mode(this->tx_handle_, &tx_cfg);
      if (err != ESP_OK) {
        ESP_LOGE(TAG, "Failed to init TX channel: %s", esp_err_to_name(err));
        this->deinit_i2s_();
        this->set_i2s_hardware_state_(I2SHardwareState::ERROR);
        return false;
      }
      ESP_LOGD(TAG, "TX channel initialized");
    }

    if (this->rx_handle_) {
      err = i2s_channel_init_std_mode(this->rx_handle_, &rx_cfg);
      if (err != ESP_OK) {
        ESP_LOGE(TAG, "Failed to init RX channel: %s", esp_err_to_name(err));
        this->deinit_i2s_();
        this->set_i2s_hardware_state_(I2SHardwareState::ERROR);
        return false;
      }
      ESP_LOGD(TAG, "RX channel initialized (%s)", this->use_stereo_aec_ref_ ? "stereo" : "mono");
    }
  }

  this->set_i2s_hardware_state_(I2SHardwareState::READY);
  this->log_memory_snapshot_("after_i2s_prepare");
  ESP_LOGI(TAG, "I2S DUPLEX prepared (%s, READY)", this->use_tdm_bus_ ? "TDM" : "standard");
  return true;
}

bool I2SAudioDuplex::enable_i2s_channels_() {
  auto state = static_cast<I2SHardwareState>(
      this->i2s_hardware_state_.load(std::memory_order_relaxed));
  if (state == I2SHardwareState::RUNNING) {
    return true;
  }
  if (state == I2SHardwareState::ERROR) {
    ESP_LOGE(TAG, "Cannot enable I2S from error state");
    return false;
  }
  if (!this->prepare_i2s_channels_()) {
    return false;
  }

  bool tx_enabled = false;
  bool rx_enabled = false;
  esp_err_t err;
  if (this->tx_handle_) {
    err = i2s_channel_enable(this->tx_handle_);
    if (err != ESP_OK) {
      ESP_LOGE(TAG, "Failed to enable TX channel: %s", esp_err_to_name(err));
      this->set_i2s_hardware_state_(I2SHardwareState::ERROR);
      return false;
    }
    tx_enabled = true;
  }
  if (this->rx_handle_) {
    err = i2s_channel_enable(this->rx_handle_);
    if (err != ESP_OK) {
      ESP_LOGE(TAG, "Failed to enable RX channel: %s", esp_err_to_name(err));
      if (tx_enabled) {
        i2s_channel_disable(this->tx_handle_);
      }
      this->set_i2s_hardware_state_(I2SHardwareState::READY);
      return false;
    }
    rx_enabled = true;
  }

  (void) tx_enabled;
  (void) rx_enabled;
  this->set_i2s_hardware_state_(I2SHardwareState::RUNNING);
  this->log_memory_snapshot_("after_i2s_enable");
  ESP_LOGI(TAG, "I2S DUPLEX running (%s)", this->use_tdm_bus_ ? "TDM" : "standard");
  return true;
}

bool I2SAudioDuplex::init_i2s_duplex_() {
  return this->enable_i2s_channels_();
}

void I2SAudioDuplex::disable_i2s_channels_() {
  auto state = static_cast<I2SHardwareState>(
      this->i2s_hardware_state_.load(std::memory_order_relaxed));
  if (state != I2SHardwareState::RUNNING && state != I2SHardwareState::STOPPING) {
    return;
  }
  this->set_i2s_hardware_state_(I2SHardwareState::STOPPING);
  esp_err_t err;
  if (this->tx_handle_) {
    err = i2s_channel_disable(this->tx_handle_);
    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
      ESP_LOGW(TAG, "TX channel disable failed: %s", esp_err_to_name(err));
    }
  }
  if (this->rx_handle_) {
    err = i2s_channel_disable(this->rx_handle_);
    if (err != ESP_OK && err != ESP_ERR_INVALID_STATE) {
      ESP_LOGW(TAG, "RX channel disable failed: %s", esp_err_to_name(err));
    }
  }
  this->set_i2s_hardware_state_(I2SHardwareState::READY);
  this->log_memory_snapshot_("after_i2s_disable");
}

void I2SAudioDuplex::deinit_i2s_() {
  this->disable_i2s_channels_();
  if (this->tx_handle_) {
    i2s_del_channel(this->tx_handle_);
    this->tx_handle_ = nullptr;
  }
  if (this->rx_handle_) {
    i2s_del_channel(this->rx_handle_);
    this->rx_handle_ = nullptr;
  }
  this->set_i2s_hardware_state_(I2SHardwareState::UNPREPARED);
  ESP_LOGI(TAG, "I2S deinitialized");
}

void I2SAudioDuplex::start() {
  if (this->duplex_running_.load(std::memory_order_relaxed)) {
    return;
  }

  // Cancel any in-flight deferred teardown: a rapid stop()-then-start()
  // cycle (e.g. consumer toggle) should keep the I2S channels enabled.
  this->teardown_pending_.store(false, std::memory_order_relaxed);

  ESP_LOGI(TAG, "Starting duplex audio...");

  // setup() normally pre-creates the task. Keep this fallback so manually
  // constructed test instances still behave correctly.
  if (this->audio_task_handle_ == nullptr) {
    const BaseType_t core = this->task_core_ >= 0 ? this->task_core_ : tskNO_AFFINITY;
    const uint32_t stack_words = this->task_stack_size_ / sizeof(StackType_t);
    if (!audio_processor::start_pinned_task(
            audio_task, "i2s_duplex", stack_words, this, this->task_priority_,
            core, this->audio_stack_in_psram_, TAG,
            &this->audio_task_handle_, &this->audio_task_tcb_,
            &this->audio_task_stack_)) {
      this->has_i2s_error_.store(true, std::memory_order_relaxed);
      return;
    }
  }

  // setup() prepares channels to READY. start() owns only the transition to
  // RUNNING; prepare_i2s_channels_() remains idempotent as an invariant guard.
  this->request_audio_preallocation_();
  if (!this->enable_i2s_channels_()) {
    ESP_LOGE(TAG, "Failed to start I2S");
    return;
  }

  this->has_i2s_error_.store(false, std::memory_order_relaxed);
  // Cross-thread mailbox: ask the audio task to drain speaker_buffer_
  // from its own context before resuming TX. Calling reset() inline
  // would race the consumer's read indices on the same RingBuffer.
  this->request_speaker_reset_.store(true, std::memory_order_relaxed);
  // start_speaker()/stop_speaker() own speaker_running_. A mic-only start
  // still writes silence to TX to keep the duplex bus clocked, but it must not
  // make the component look like active playback; otherwise the last mic
  // consumer leaving would fail to park the pipeline.

  // Reset FIR decimators for clean state. rx_decimator_ is lazily initialised
  // inside audio_session_, so reset only when its consumer path is active.
  this->mic_decimator_.reset();
  if (this->use_tdm_bus_ || this->use_stereo_aec_ref_) {
    this->rx_decimator_.reset();
  }
  this->play_ref_decimator_.reset();

#ifdef USE_AUDIO_PROCESSOR
  if (this->use_stereo_aec_ref_) {
    ESP_LOGD(TAG, "ES8311 digital feedback - reference is sample-aligned");
  }
  if (this->use_tdm_ref_) {
    ESP_LOGD(TAG, "TDM hardware reference - slot %u is echo ref", this->tdm_ref_slot_);
  }
#endif

  // Wake the permanent audio task (created once in setup()).
  this->duplex_running_.store(true, std::memory_order_relaxed);
  if (this->audio_task_handle_ != nullptr) {
    xTaskNotifyGive(this->audio_task_handle_);
  }
  this->start_trigger_.trigger();
  ESP_LOGI(TAG, "Duplex audio started");
}

void I2SAudioDuplex::stop() {
  if (!this->duplex_running_.load(std::memory_order_relaxed)) {
    return;
  }

  ESP_LOGI(TAG, "Stopping duplex audio (deferred)");

  // Consumers stay registered across stop()/start() so the mic path is
  // reconnected automatically after an internal restart (frame_spec change).
  if (this->speaker_running_.exchange(false, std::memory_order_relaxed)) {
    this->speaker_idle_trigger_.trigger();
    this->update_runtime_state_();
  }
  this->duplex_running_.store(false, std::memory_order_relaxed);
  this->idle_trigger_.trigger();

  // Defer the I2S channel teardown to loop(): polling audio_task_idle_
  // here would block the main task for up to 600 ms (often >60 ms),
  // starving network/UI/LVGL. loop() picks this up on the next tick once
  // the audio task has parked itself in its outer wait loop.
  this->teardown_pending_.store(true, std::memory_order_relaxed);
}

bool I2SAudioDuplex::register_mic_consumer(void *token) {
  bool needs_start = false;
  size_t count_after = 0;
  bool first_consumer = false;
  bool full = false;
  if (this->mic_consumers_mutex_ == nullptr)
    return false;
  {
    audio_processor::ScopedLock lock(this->mic_consumers_mutex_);
    // Already registered?
    for (size_t i = 0; i < this->mic_consumer_count_; i++) {
      if (this->mic_consumers_[i] == token) return true;
    }
    if (this->mic_consumer_count_ >= MAX_LISTENERS) {
      full = true;
    } else {
      first_consumer = (this->mic_consumer_count_ == 0);
      this->mic_consumers_[this->mic_consumer_count_++] = token;
      count_after = this->mic_consumer_count_;
      this->has_mic_consumers_.store(true, std::memory_order_relaxed);
      needs_start = !this->duplex_running_.load(std::memory_order_relaxed);
    }
  }
  if (full) {
    ESP_LOGW(TAG, "Mic consumer registry full (max=%u), refusing token=%p",
             (unsigned) MAX_LISTENERS, token);
    return false;
  }
  if (first_consumer) {
    ESP_LOGI(TAG, "Mic consumer registered (token=%p), mic path active (consumers=%zu)",
             token, count_after);
    this->mic_start_trigger_.trigger();
    // Wake the audio processor (e.g. esp_afe feed/fetch tasks) before any
    // consumer expects processed frames; the processor must already be
    // pumping by the time audio_task starts pushing into it.
    if (this->processor_ != nullptr) {
      this->processor_->set_processing_active(true);
    }
  } else {
    ESP_LOGD(TAG, "Mic consumer registered (token=%p, consumers=%zu)", token, count_after);
  }
  if (needs_start) {
    this->start();
  }
  if (first_consumer) {
    this->update_runtime_state_();
  }
  return true;
}

void I2SAudioDuplex::unregister_mic_consumer(void *token) {
  size_t count_after = 0;
  bool removed = false;
  bool last_consumer_gone = false;
  if (this->mic_consumers_mutex_ == nullptr)
    return;
  {
    audio_processor::ScopedLock lock(this->mic_consumers_mutex_);
    for (size_t i = 0; i < this->mic_consumer_count_; i++) {
      if (this->mic_consumers_[i] == token) {
        // Swap-and-pop: order in the array is not meaningful, swap with last.
        this->mic_consumers_[i] = this->mic_consumers_[this->mic_consumer_count_ - 1];
        this->mic_consumers_[this->mic_consumer_count_ - 1] = nullptr;
        this->mic_consumer_count_--;
        removed = true;
        break;
      }
    }
    count_after = this->mic_consumer_count_;
    last_consumer_gone = removed && this->mic_consumer_count_ == 0;
    this->has_mic_consumers_.store(this->mic_consumer_count_ != 0, std::memory_order_relaxed);
  }
  if (!removed) {
    return;
  }
  if (last_consumer_gone) {
    ESP_LOGI(TAG, "Last mic consumer removed (token=%p), mic path idle", token);
    this->mic_idle_trigger_.trigger();
    // Tell the audio processor it can suspend background work until a new
    // consumer arrives. Without this hint esp_afe's feed/fetch tasks (and
    // the esp-sr internal worker on Core 1) keep cycling on every frame
    // even when nobody is listening, which on spotpear-ball-v2 was monopolising
    // CPU1 long enough to trip the loopTask 30s watchdog on HA restart.
    if (this->processor_ != nullptr) {
      this->processor_->set_processing_active(false);
    }
    // If no playback either, park the audio task and disable I2S channels.
    // Task and channels stay allocated for instant wake on the next consumer.
    if (!this->speaker_running_.load(std::memory_order_relaxed)) {
      ESP_LOGI(TAG, "Duplex going idle (no consumers, no playback)");
      this->stop();
    }
    this->update_runtime_state_();
  } else {
    ESP_LOGD(TAG, "Mic consumer unregistered (token=%p, consumers=%zu)", token, count_after);
  }
}

void I2SAudioDuplex::start_speaker() {
  if (!this->duplex_running_.load(std::memory_order_relaxed)) {
    this->start();
  }
  if (!this->duplex_running_.load(std::memory_order_relaxed)) {
    ESP_LOGW(TAG, "Speaker start refused: duplex did not enter RUNNING");
    return;
  }
  if (!this->speaker_running_.exchange(true, std::memory_order_relaxed)) {
    this->direct_aec_ref_valid_ = false;
    this->tdm_ref_silent_frames_.store(0, std::memory_order_relaxed);
    if (this->reset_processor_on_speaker_start_ && this->processor_ != nullptr &&
        this->processor_enabled_.load(std::memory_order_relaxed)) {
      bool ok = this->processor_->reset_buffers();
      ESP_LOGI(TAG, "Audio processor buffer reset on speaker start: %s",
               ok ? "ok" : "failed");
    }
    this->speaker_start_trigger_.trigger();
    this->update_runtime_state_();
  }

  this->play_ref_decimator_.reset();
}

void I2SAudioDuplex::stop_speaker() {
  if (this->speaker_running_.exchange(false, std::memory_order_relaxed)) {
    this->speaker_idle_trigger_.trigger();
    this->update_runtime_state_();
  }
  // Request audio task to reset ring buffers (avoids concurrent access).
  this->request_speaker_reset_.store(true, std::memory_order_relaxed);
  // If no mic consumers either, tear down the duplex pipeline. This signals
  // the audio processor (e.g. AFE) it can suspend its workers and parks the
  // audio task; channels stay configured for fast wake on the next start.
  if (!this->has_mic_consumers_.load(std::memory_order_relaxed)) {
    ESP_LOGI(TAG, "Duplex going idle (speaker stopped, no mic consumers)");
    if (this->processor_ != nullptr) {
      this->processor_->set_processing_active(false);
    }
    this->stop();
  }
}

size_t I2SAudioDuplex::play(const uint8_t *data, size_t len, TickType_t ticks_to_wait) {
  if (!this->speaker_buffer_) {
    return 0;
  }

  // Data arrives at bus rate (e.g. 48kHz from mixer/resampler). Write directly.
  size_t written = this->speaker_buffer_->write_without_replacement((void *) data, len, ticks_to_wait, true);

  if (written > 0) {
    this->last_speaker_audio_ms_.store(millis(), std::memory_order_relaxed);
  }
  return written;
}

size_t I2SAudioDuplex::get_speaker_buffer_available() const {
  if (!this->speaker_buffer_) return 0;
  return this->speaker_buffer_->available();
}

size_t I2SAudioDuplex::get_speaker_buffer_size() const {
  return this->speaker_buffer_size_;
}

}  // namespace i2s_audio_duplex
}  // namespace esphome

#endif  // USE_ESP32
