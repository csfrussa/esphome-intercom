#pragma once

#include "esphome/components/speaker/speaker.h"
#include "esphome/core/component.h"

#include <cstdint>
#include <fstream>
#include <string>

namespace esphome::virtual_speaker {

class VirtualSpeaker : public Component, public speaker::Speaker {
 public:
  void setup() override;
  void dump_config() override;
  void loop() override;

  size_t play(const uint8_t *data, size_t length) override;
  void start() override;
  void stop() override;
  void finish() override;
  bool has_buffered_data() const override { return false; }

  void set_output_path(const std::string &path) { this->output_path_ = path; }
  void set_audio_stream_info(uint8_t bits_per_sample, uint8_t channels, uint32_t sample_rate) {
    this->audio_stream_info_ = audio::AudioStreamInfo(bits_per_sample, channels, sample_rate);
  }

 protected:
  bool open_output_();
  void mark_drained_();

  std::string output_path_;
  std::ofstream output_;
  uint64_t bytes_written_{0};
  uint32_t last_write_ms_{0};
};

}  // namespace esphome::virtual_speaker
