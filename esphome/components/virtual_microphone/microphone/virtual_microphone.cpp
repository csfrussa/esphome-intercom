#include "virtual_microphone.h"

#include "esphome/core/hal.h"
#include "esphome/core/log.h"

namespace esphome::virtual_microphone {

static const char *const TAG = "virtual_microphone";

void VirtualMicrophone::setup() {
  const size_t frame_bytes = this->audio_stream_info_.ms_to_bytes(this->frame_ms_);
  this->frame_.assign(frame_bytes, 0);
  ESP_LOGCONFIG(TAG, "Virtual microphone prepared: %u ms frames, %zu bytes/frame", this->frame_ms_, frame_bytes);
}

void VirtualMicrophone::dump_config() {
  ESP_LOGCONFIG(TAG, "Virtual Microphone:");
  ESP_LOGCONFIG(TAG, "  Input path: %s", this->input_path_.c_str());
  ESP_LOGCONFIG(TAG, "  Sample rate: %" PRIu32, this->audio_stream_info_.get_sample_rate());
  ESP_LOGCONFIG(TAG, "  Bits per sample: %u", this->audio_stream_info_.get_bits_per_sample());
  ESP_LOGCONFIG(TAG, "  Channels: %u", this->audio_stream_info_.get_channels());
  ESP_LOGCONFIG(TAG, "  Frame: %" PRIu32 " ms", this->frame_ms_);
  ESP_LOGCONFIG(TAG, "  Repeat: %s", YESNO(this->repeat_));
}

void VirtualMicrophone::start() {
  if (this->state_ == microphone::STATE_RUNNING || this->state_ == microphone::STATE_STARTING) {
    return;
  }
  this->state_ = microphone::STATE_STARTING;
  this->exhausted_ = false;
  if (!this->open_input_()) {
    ESP_LOGW(TAG, "Input file unavailable, microphone will emit silence: %s", this->input_path_.c_str());
  }
  this->last_emit_ms_ = millis();
  this->state_ = microphone::STATE_RUNNING;
  ESP_LOGD(TAG, "Virtual microphone started");
}

void VirtualMicrophone::stop() {
  if (this->state_ == microphone::STATE_STOPPED || this->state_ == microphone::STATE_STOPPING) {
    return;
  }
  this->state_ = microphone::STATE_STOPPING;
  if (this->input_.is_open()) {
    this->input_.close();
  }
  this->state_ = microphone::STATE_STOPPED;
  ESP_LOGD(TAG, "Virtual microphone stopped");
}

void VirtualMicrophone::loop() {
  if (this->state_ != microphone::STATE_RUNNING) {
    return;
  }

  const uint32_t now = millis();
  if (now - this->last_emit_ms_ < this->frame_ms_) {
    return;
  }
  this->last_emit_ms_ = now;

  if (!this->read_next_frame_()) {
    this->emit_silence_();
  }
}

bool VirtualMicrophone::open_input_() {
  if (this->input_.is_open()) {
    return true;
  }
  if (this->input_path_.empty()) {
    return false;
  }
  this->input_.open(this->input_path_, std::ios::binary);
  return this->input_.good();
}

bool VirtualMicrophone::read_next_frame_() {
  if (this->exhausted_ || !this->open_input_()) {
    return false;
  }

  this->input_.read(reinterpret_cast<char *>(this->frame_.data()), static_cast<std::streamsize>(this->frame_.size()));
  const auto bytes_read = this->input_.gcount();
  if (bytes_read == static_cast<std::streamsize>(this->frame_.size())) {
    this->data_callbacks_.call(this->frame_);
    return true;
  }

  if (bytes_read > 0) {
    std::fill(this->frame_.begin() + bytes_read, this->frame_.end(), 0);
    this->data_callbacks_.call(this->frame_);
  }

  if (this->repeat_) {
    this->input_.clear();
    this->input_.seekg(0, std::ios::beg);
  } else {
    this->exhausted_ = true;
  }
  return bytes_read > 0;
}

void VirtualMicrophone::emit_silence_() {
  std::fill(this->frame_.begin(), this->frame_.end(), 0);
  this->data_callbacks_.call(this->frame_);
}

}  // namespace esphome::virtual_microphone
