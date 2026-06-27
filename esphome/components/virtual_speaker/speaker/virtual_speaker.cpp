#include "virtual_speaker.h"

#include "esphome/core/hal.h"
#include "esphome/core/log.h"

namespace esphome::virtual_speaker {

static const char *const TAG = "virtual_speaker";
static constexpr uint32_t DRAIN_IDLE_MS = 50;

void VirtualSpeaker::setup() { ESP_LOGCONFIG(TAG, "Virtual speaker prepared"); }

void VirtualSpeaker::dump_config() {
  ESP_LOGCONFIG(TAG, "Virtual Speaker:");
  ESP_LOGCONFIG(TAG, "  Output path: %s", this->output_path_.c_str());
  ESP_LOGCONFIG(TAG, "  Sample rate: %" PRIu32, this->audio_stream_info_.get_sample_rate());
  ESP_LOGCONFIG(TAG, "  Bits per sample: %u", this->audio_stream_info_.get_bits_per_sample());
  ESP_LOGCONFIG(TAG, "  Channels: %u", this->audio_stream_info_.get_channels());
}

void VirtualSpeaker::loop() {
  if (this->state_ == speaker::STATE_RUNNING && this->last_write_ms_ != 0 &&
      millis() - this->last_write_ms_ >= DRAIN_IDLE_MS) {
    this->mark_drained_();
  }
}

void VirtualSpeaker::start() {
  if (this->state_ == speaker::STATE_RUNNING || this->state_ == speaker::STATE_STARTING) {
    return;
  }
  this->state_ = speaker::STATE_STARTING;
  if (!this->output_.is_open()) {
    this->bytes_written_ = 0;
  }
  this->last_write_ms_ = 0;
  if (!this->open_output_()) {
    ESP_LOGW(TAG, "Unable to open virtual speaker output: %s", this->output_path_.c_str());
  }
  this->state_ = speaker::STATE_RUNNING;
  ESP_LOGD(TAG, "Virtual speaker started");
}

void VirtualSpeaker::stop() {
  if (this->state_ == speaker::STATE_STOPPING) {
    return;
  }
  this->state_ = speaker::STATE_STOPPING;
  if (this->output_.is_open()) {
    this->output_.flush();
    this->output_.close();
  }
  this->last_write_ms_ = 0;
  this->state_ = speaker::STATE_STOPPED;
  ESP_LOGD(TAG, "Virtual speaker stopped after writing %" PRIu64 " bytes", this->bytes_written_);
}

void VirtualSpeaker::finish() {
  if (this->output_.is_open()) {
    this->output_.flush();
  }
  this->stop();
}

size_t VirtualSpeaker::play(const uint8_t *data, size_t length) {
  if (data == nullptr || length == 0) {
    return 0;
  }
  if (this->state_ != speaker::STATE_RUNNING) {
    this->start();
  }
  if (!this->open_output_()) {
    return 0;
  }
  this->output_.write(reinterpret_cast<const char *>(data), static_cast<std::streamsize>(length));
  if (!this->output_.good()) {
    ESP_LOGW(TAG, "Write failed after %" PRIu64 " bytes", this->bytes_written_);
    return 0;
  }
  this->bytes_written_ += length;
  this->last_write_ms_ = millis();
  const uint32_t frames = this->audio_stream_info_.bytes_to_frames(length);
  this->audio_output_callback_.call(frames, static_cast<int64_t>(micros()));
  return length;
}

bool VirtualSpeaker::open_output_() {
  if (this->output_.is_open()) {
    return true;
  }
  if (this->output_path_.empty()) {
    return false;
  }
  this->output_.open(this->output_path_, std::ios::binary | std::ios::trunc);
  return this->output_.good();
}

void VirtualSpeaker::mark_drained_() {
  if (this->output_.is_open()) {
    this->output_.flush();
  }
  this->state_ = speaker::STATE_STOPPED;
  ESP_LOGVV(TAG, "Virtual speaker drained after %" PRIu64 " bytes", this->bytes_written_);
}

}  // namespace esphome::virtual_speaker
