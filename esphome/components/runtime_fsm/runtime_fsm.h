#pragma once

#include "runtime_fsm_state.h"

#include "esphome/core/component.h"
#include "esphome/components/media_player/media_player.h"
#include "esphome/components/micro_wake_word/micro_wake_word.h"
#include "esphome/components/mixer/speaker/mixer_speaker.h"
#include "esphome/components/script/script.h"
#include "esphome/components/switch/switch.h"
#include "esphome/components/voice_assistant/voice_assistant.h"

#include <array>
#include <cstdint>
#include <string>

namespace esphome {
namespace intercom_api {
class IntercomApi;
}
namespace runtime_fsm {

class RuntimeFsm : public Component {
 public:
  void setup() override;
  void loop() override;
  void dump_config() override;
  float get_setup_priority() const override { return setup_priority::PROCESSOR; }

  void set_voice_assistant(voice_assistant::VoiceAssistant *va) { this->va_ = va; }
#ifdef USE_MICRO_WAKE_WORD
  void set_micro_wake_word(micro_wake_word::MicroWakeWord *mww) { this->mww_ = mww; }
#endif
  void set_media_mixer_input(mixer_speaker::SourceSpeaker *speaker) { this->media_mixer_input_ = speaker; }
  void set_media_player(media_player::MediaPlayer *player) { this->media_player_ = player; }
  void set_output_script(script::Script<> *script) { this->output_script_ = script; }
  void set_intercom(intercom_api::IntercomApi *intercom) { this->intercom_ = intercom; }
  void set_mic_mute_switch(switch_::Switch *sw) { this->mic_mute_switch_ = sw; }
  void set_speaker_mute_switch(switch_::Switch *sw) { this->speaker_mute_switch_ = sw; }

  void on_va_started();
  void on_va_listening();
  void on_va_thinking();
  void on_tts_start(const std::string &text);
  void on_tts_end(const std::string &url);
  void on_va_end();
  void on_va_error(const std::string &code, const std::string &message);
  void on_wake_word(const std::string &wake_word);
  void on_intercom_event();
  void on_media_event();
  void on_connectivity_event();

  int get_ui_state() const { return static_cast<int>(this->state_.ui_state); }
  int get_led_state() const { return this->state_.led_state; }
  int get_compat_va_state() const { return this->state_.compat_va_state; }
  uint32_t get_activity_mask() const { return this->state_.activity_mask; }
  uint32_t get_sequence() const { return this->sequence_; }
  uint32_t get_va_epoch() const { return this->state_.va_epoch; }
  uint32_t get_tts_epoch() const { return this->state_.tts_epoch; }
  uint32_t get_cancelled_epoch() const { return this->state_.cancelled_epoch; }
  int get_va_phase() const { return static_cast<int>(this->state_.phase); }
  bool is_ducking_active() const { return this->ducking_active_; }

 protected:
  static constexpr size_t EVENT_QUEUE_SIZE = 8;

  struct QueuedEvent {
    Event event;
    std::string detail;
  };

  void dispatch_(Event event, const char *detail = nullptr);
  void drain_events_();
  void apply_result_(const ReduceResult &result, EventType event_type, const char *detail);
  ObservedState observe_() const;
  void publish_outputs_();
  void execute_effects_(uint32_t effects, EventType event_type, const char *detail);
  void request_va_start_(const char *wake_word);
  void request_va_stop_();
  void stop_tts_announcement_();
  void log_transition_(EventType event_type, VaPhase old_phase, uint32_t old_mask, const char *detail,
                       uint32_t effects);

  bool va_running_() const;
  bool boot_initializing_() const;
  bool wifi_connected_() const;
  bool api_connected_() const;
  bool media_announcing_() const;
  bool media_playing_() const;
  IntercomPhase intercom_phase_() const;

  voice_assistant::VoiceAssistant *va_{nullptr};
#ifdef USE_MICRO_WAKE_WORD
  micro_wake_word::MicroWakeWord *mww_{nullptr};
#endif
  mixer_speaker::SourceSpeaker *media_mixer_input_{nullptr};
  media_player::MediaPlayer *media_player_{nullptr};
  script::Script<> *output_script_{nullptr};
  intercom_api::IntercomApi *intercom_{nullptr};
  switch_::Switch *mic_mute_switch_{nullptr};
  switch_::Switch *speaker_mute_switch_{nullptr};

  RuntimeState state_{};
  bool dispatching_{false};
  std::array<QueuedEvent, EVENT_QUEUE_SIZE> pending_events_{};
  size_t pending_head_{0};
  size_t pending_count_{0};
  bool ducking_active_{false};
  uint32_t sequence_{0};
  uint32_t boot_led_until_{0};
  bool last_boot_initializing_{false};
  bool last_wifi_connected_{false};
  bool last_api_connected_{false};
  media_player::MediaPlayerState last_media_state_{media_player::MEDIA_PLAYER_STATE_NONE};
};

}  // namespace runtime_fsm
}  // namespace esphome
