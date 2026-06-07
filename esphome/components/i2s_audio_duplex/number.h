#pragma once

#ifdef USE_ESP32

#include "esphome/components/number/number.h"
#include "esphome/components/speaker/speaker.h"
#include "esphome/core/component.h"
#include "esphome/core/preferences.h"
#include "i2s_audio_duplex.h"
#include <cmath>

namespace esphome {
namespace i2s_audio_duplex {

class MicGainNumber : public number::Number, public Component {
 public:
  void set_parent(I2SAudioDuplex *parent) { this->parent_ = parent; }
  void set_pre_aec(bool pre_aec) { this->pre_aec_ = pre_aec; }

  void setup() override {
    float value;
    this->pref_ = global_preferences->make_preference<float>(this->get_object_id_hash());
    if (this->pref_.load(&value)) {
      value = clamp_db_(value);
      this->apply_(value);
      this->publish_state(value);
    } else {
      this->publish_state(0.0f);  // 0 dB = unity gain
    }
  }

  void dump_config() override {
    ESP_LOGCONFIG("i2s_duplex.mic_gain", "Mic Gain Number (dB, %s)", this->pre_aec_ ? "pre-AEC" : "post-AEC");
  }

 protected:
  static float clamp_db_(float value) {
    if (!std::isfinite(value)) return 0.0f;
    if (value < -20.0f) return -20.0f;
    if (value > 30.0f) return 30.0f;
    return value;
  }

  void apply_(float value) {
    if (this->parent_ != nullptr) {
      float linear = std::pow(10.0f, value / 20.0f);
      if (this->pre_aec_) {
        this->parent_->set_mic_attenuation(linear);
      } else {
        this->parent_->set_mic_gain(linear);
      }
    }
  }

  void control(float value) override {
    if (this->parent_ != nullptr) {
      value = clamp_db_(value);
      this->apply_(value);
      this->publish_state(value);
      this->pref_.save(&value);
    }
  }

  I2SAudioDuplex *parent_{nullptr};
  ESPPreferenceObject pref_;
  bool pre_aec_{false};
};

class SpeakerVolumeNumber : public number::Number, public Component {
 public:
  void set_parent(I2SAudioDuplex *parent) { this->parent_ = parent; }
  void set_speaker(speaker::Speaker *speaker) { this->speaker_ = speaker; }

  void setup() override {
    float value;
    this->pref_ = global_preferences->make_preference<float>(this->get_object_id_hash());
    if (this->pref_.load(&value)) {
      value = clamp_percent_(value);
      this->apply_(value);
      this->publish_state(value);
    } else if (this->parent_ != nullptr) {
      this->publish_state(this->parent_->get_speaker_volume() * 100.0f);
    } else if (this->speaker_ != nullptr) {
      this->publish_state(this->speaker_->get_volume() * 100.0f);
    }
  }

  void dump_config() override {
    ESP_LOGCONFIG("i2s_duplex.speaker_volume", "Master Volume Number%s",
                  this->speaker_ != nullptr ? " (speaker-backed)" : "");
  }

 protected:
  static float clamp_percent_(float value) {
    if (!std::isfinite(value)) return 0.0f;
    if (value < 0.0f) return 0.0f;
    if (value > 100.0f) return 100.0f;
    return value;
  }

  void apply_(float value) {
    float volume = value / 100.0f;
    if (this->parent_ != nullptr) {
      this->parent_->set_speaker_volume(volume);
    } else if (this->speaker_ != nullptr) {
      this->speaker_->set_volume(volume);
    }
  }

  void control(float value) override {
    if (this->speaker_ != nullptr || this->parent_ != nullptr) {
      value = clamp_percent_(value);
      this->apply_(value);
      this->publish_state(value);
      this->pref_.save(&value);
    }
  }

  I2SAudioDuplex *parent_{nullptr};
  speaker::Speaker *speaker_{nullptr};
  ESPPreferenceObject pref_;
};

}  // namespace i2s_audio_duplex
}  // namespace esphome

#endif  // USE_ESP32
