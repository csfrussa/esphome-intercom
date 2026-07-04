#pragma once

#include "esphome/components/microphone/microphone.h"
#include "esphome/core/component.h"

#include <cstdint>
#include <fstream>
#include <string>
#include <vector>

namespace esphome::virtual_microphone {

class VirtualMicrophone : public Component, public microphone::Microphone {
 public:
  void setup() override;
  void loop() override;
  void dump_config() override;

  void start() override;
  void stop() override;

  void set_input_path(const std::string &path) { this->input_path_ = path; }
  void set_frame_ms(uint32_t frame_ms) { this->frame_ms_ = frame_ms; }
  void set_repeat(bool repeat) { this->repeat_ = repeat; }
  void set_audio_stream_info(uint8_t bits_per_sample, uint8_t channels, uint32_t sample_rate) {
    this->audio_stream_info_ = audio::AudioStreamInfo(bits_per_sample, channels, sample_rate);
  }

 protected:
  bool open_input_();
  bool read_next_frame_();
  void emit_silence_();

  std::string input_path_;
  std::ifstream input_;
  std::vector<uint8_t> frame_;
  uint32_t frame_ms_{20};
  uint32_t last_emit_ms_{0};
  bool repeat_{false};
  bool exhausted_{false};
};

}  // namespace esphome::virtual_microphone
