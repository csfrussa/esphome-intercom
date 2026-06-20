#include "runtime_fsm.h"

#include "esphome/core/hal.h"
#include "esphome/core/log.h"

#ifdef USE_API
#include "esphome/components/api/api_server.h"
#endif
#ifdef USE_INTERCOM_API
#include "esphome/components/intercom_api/intercom_api.h"
#endif
#ifdef USE_WIFI
#include "esphome/components/wifi/wifi_component.h"
#endif

namespace esphome::runtime_fsm {

static const char *const TAG = "runtime_fsm";

void RuntimeFsm::setup() {
  this->boot_led_until_ = millis() + 1000;
  this->last_boot_initializing_ = this->boot_initializing_();
  this->last_wifi_connected_ = this->wifi_connected_();
  this->last_api_connected_ = this->api_connected_();
  this->state_ = RuntimeState{};
  recompute_outputs(this->state_, this->observe_());
  this->last_media_state_ = this->media_player_ != nullptr ? this->media_player_->state : media_player::MEDIA_PLAYER_STATE_NONE;
  this->publish_outputs_();
}

void RuntimeFsm::loop() {
  const bool boot_initializing = this->boot_initializing_();
  const bool wifi_connected = this->wifi_connected_();
  const bool api_connected = this->api_connected_();
  if (boot_initializing != this->last_boot_initializing_ || wifi_connected != this->last_wifi_connected_ ||
      api_connected != this->last_api_connected_) {
    this->last_boot_initializing_ = boot_initializing;
    this->last_wifi_connected_ = wifi_connected;
    this->last_api_connected_ = api_connected;
    this->dispatch_(Event{EventType::OBSERVED_STATE_CHANGED}, "connectivity");
  }

  if (this->media_player_ != nullptr && this->media_player_->state != this->last_media_state_) {
    auto old = this->last_media_state_;
    this->last_media_state_ = this->media_player_->state;
    if (this->media_player_->state == media_player::MEDIA_PLAYER_STATE_ANNOUNCING &&
        (this->state_.phase == VaPhase::TTS_SYNTHESIZING || this->state_.phase == VaPhase::THINKING)) {
      this->dispatch_(Event{EventType::TTS_URL_ACCEPTED});
      this->dispatch_(Event{EventType::TTS_SOURCE_PLAYING});
    } else if (old == media_player::MEDIA_PLAYER_STATE_ANNOUNCING &&
               this->media_player_->state != media_player::MEDIA_PLAYER_STATE_ANNOUNCING) {
      this->dispatch_(Event{EventType::TTS_SOURCE_TERMINAL});
    } else {
      this->dispatch_(Event{EventType::OBSERVED_STATE_CHANGED});
    }
  }

  if (this->state_.phase == VaPhase::TTS_DRAINING && !this->media_announcing_()) {
    this->dispatch_(Event{EventType::TTS_DRAINED});
  }

  if (this->state_.phase == VaPhase::CANCEL_REQUESTED && this->state_.barge_pending && !this->va_running_() &&
      !this->media_announcing_()) {
    this->dispatch_(Event{EventType::BARGE_READY});
  }

  if (this->state_.phase == VaPhase::WAITING_FOLLOWUP && !this->va_running_()) {
    this->dispatch_(Event{EventType::VA_RUN_ENDED}, "followup_done");
  }
}

void RuntimeFsm::dump_config() {
  ESP_LOGCONFIG(TAG, "Runtime FSM:");
  ESP_LOGCONFIG(TAG, "  Voice Assistant: %s", this->va_ != nullptr ? "configured" : "missing");
  ESP_LOGCONFIG(TAG, "  Media player: %s", this->media_player_ != nullptr ? "configured" : "missing");
  ESP_LOGCONFIG(TAG, "  Media mixer input: %s", this->media_mixer_input_ != nullptr ? "configured" : "missing");
}

void RuntimeFsm::on_va_started() { this->dispatch_(Event{EventType::VA_START_ACCEPTED}); }
void RuntimeFsm::on_va_listening() { this->dispatch_(Event{EventType::VA_LISTENING}); }
void RuntimeFsm::on_va_thinking() { this->dispatch_(Event{EventType::VA_THINKING}); }
void RuntimeFsm::on_tts_start(const std::string &text) { this->dispatch_(Event{EventType::TTS_SYNTHESIS_STARTED}, text.c_str()); }
void RuntimeFsm::on_tts_end(const std::string &url) { this->dispatch_(Event{EventType::OBSERVED_STATE_CHANGED}, "tts_end"); }
void RuntimeFsm::on_va_end() { this->dispatch_(Event{EventType::VA_RUN_ENDED}); }
void RuntimeFsm::on_va_error(const std::string &code, const std::string &message) {
  this->dispatch_(Event{EventType::VA_ERROR}, code.c_str());
}
void RuntimeFsm::on_wake_word(const std::string &wake_word) {
  Event event{EventType::WAKE_WORD};
  event.has_wake_word = !wake_word.empty();
  this->dispatch_(event, wake_word.c_str());
}
void RuntimeFsm::on_intercom_event() { this->dispatch_(Event{EventType::OBSERVED_STATE_CHANGED}, "intercom"); }
void RuntimeFsm::on_media_event() { this->dispatch_(Event{EventType::OBSERVED_STATE_CHANGED}, "media"); }
void RuntimeFsm::on_connectivity_event() {
  this->last_boot_initializing_ = this->boot_initializing_();
  this->last_wifi_connected_ = this->wifi_connected_();
  this->last_api_connected_ = this->api_connected_();
  this->dispatch_(Event{EventType::OBSERVED_STATE_CHANGED}, "connectivity");
}

void RuntimeFsm::dispatch_(Event event, const char *detail) {
  if (this->dispatching_) {
    if (this->pending_count_ < EVENT_QUEUE_SIZE) {
      size_t index = (this->pending_head_ + this->pending_count_) % EVENT_QUEUE_SIZE;
      this->pending_events_[index] = QueuedEvent{event, detail != nullptr ? detail : ""};
      this->pending_count_++;
    } else {
      ESP_LOGE(TAG, "Event queue full, dropping %s", event_name(event.type));
    }
    return;
  }

  this->dispatching_ = true;
  VaPhase old_phase = this->state_.phase;
  uint32_t old_mask = this->state_.activity_mask;
  this->sequence_++;
  ReduceResult result = reduce(this->state_, this->observe_(), event);
  this->apply_result_(result, event.type, detail);
  this->log_transition_(event.type, old_phase, old_mask, detail, result.effects);
  this->dispatching_ = false;
  this->drain_events_();
}

void RuntimeFsm::drain_events_() {
  while (this->pending_count_ > 0 && !this->dispatching_) {
    QueuedEvent queued = this->pending_events_[this->pending_head_];
    this->pending_head_ = (this->pending_head_ + 1) % EVENT_QUEUE_SIZE;
    this->pending_count_--;
    this->dispatch_(queued.event, queued.detail.empty() ? nullptr : queued.detail.c_str());
  }
}

void RuntimeFsm::apply_result_(const ReduceResult &result, EventType event_type, const char *detail) {
  this->state_ = result.state;
  if (!validate_invariants(this->state_)) {
    ESP_LOGE(TAG, "FSM invariant failed after %s", event_name(event_type));
    this->state_.phase = VaPhase::ERROR;
    recompute_outputs(this->state_, this->observe_());
  }
  this->execute_effects_(result.effects, event_type, detail);
  this->publish_outputs_();
}

ObservedState RuntimeFsm::observe_() const {
  ObservedState observed;
  observed.va_running = this->va_running_();
  observed.proxy_announcing = this->media_announcing_();
  observed.tts_source_playing = this->media_announcing_();
  observed.tts_source_terminal = !this->media_announcing_();
  observed.tts_audio_terminal = !this->media_announcing_();
  observed.media_playing = this->media_playing_();
  observed.announcement_playing = this->media_announcing_();
  observed.mic_muted = this->mic_mute_switch_ != nullptr && this->mic_mute_switch_->state;
  observed.speaker_muted = this->speaker_mute_switch_ != nullptr && this->speaker_mute_switch_->state;
  observed.initializing = this->boot_initializing_();
  observed.no_wifi = !this->wifi_connected_();
  observed.no_ha = !this->api_connected_();
  observed.intercom = this->intercom_phase_();
  return observed;
}

void RuntimeFsm::publish_outputs_() {
  bool want_ducking =
      (this->state_.activity_mask & (ACT_INTERCOM_RINGING | ACT_INTERCOM_OUTGOING | ACT_INTERCOM_STREAMING |
                                     ACT_VA_STARTING | ACT_VA_LISTENING | ACT_VA_THINKING |
                                     ACT_TTS_SYNTHESIZING | ACT_TTS_QUEUED | ACT_TTS_PLAYING |
                                     ACT_TTS_DRAINING | ACT_WAITING_FOLLOWUP | ACT_BARGE_PENDING |
                                     ACT_ANNOUNCEMENT | ACT_TIMER_RINGING)) != 0;
  if (this->media_mixer_input_ != nullptr && want_ducking != this->ducking_active_) {
    this->media_mixer_input_->apply_ducking(want_ducking ? 20 : 0, want_ducking ? 200 : 500);
    this->ducking_active_ = want_ducking;
  }
  if (this->output_script_ != nullptr) {
    this->output_script_->execute();
  }
}

void RuntimeFsm::execute_effects_(uint32_t effects, EventType event_type, const char *detail) {
  if (effects & EFFECT_STOP_TTS_PATH) {
    this->stop_tts_announcement_();
  }
  if (effects & EFFECT_REQUEST_VA_STOP) {
    this->request_va_stop_();
  }
  if (effects & EFFECT_REQUEST_VA_START) {
    this->request_va_start_(detail);
  }
}

void RuntimeFsm::request_va_start_(const char *wake_word) {
  if (this->va_ == nullptr)
    return;
  if (wake_word != nullptr && wake_word[0] != '\0') {
    this->va_->set_wake_word(wake_word);
  }
  this->va_->request_start(false, true);
}

void RuntimeFsm::request_va_stop_() {
  if (this->va_ != nullptr) {
    this->va_->request_stop();
  }
}

void RuntimeFsm::stop_tts_announcement_() {
  if (this->media_player_ == nullptr)
    return;
  auto call = this->media_player_->make_call();
  call.set_command(media_player::MEDIA_PLAYER_COMMAND_STOP);
  call.set_announcement(true);
  call.perform();
}

void RuntimeFsm::log_transition_(EventType event_type, VaPhase old_phase, uint32_t old_mask, const char *detail,
                                 uint32_t effects) {
  ESP_LOGI(TAG,
           "TRANSITION seq=%" PRIu32 " event=%s detail=%s %s->%s epoch=%" PRIu32 " proposed=%" PRIu32
           " tts=%" PRIu32 " cancelled=%" PRIu32 " mask=0x%08" PRIx32 "->0x%08" PRIx32
           " effects=0x%08" PRIx32 " media=%d va=%d ui=%d led=%d duck=%d",
           this->sequence_, event_name(event_type), detail != nullptr ? detail : "-", phase_name(old_phase),
           phase_name(this->state_.phase), this->state_.va_epoch, this->state_.proposed_epoch,
           this->state_.tts_epoch, this->state_.cancelled_epoch, old_mask, this->state_.activity_mask, effects,
           this->media_player_ != nullptr ? this->media_player_->state : -1, this->va_running_(),
           static_cast<int>(this->state_.ui_state), this->state_.led_state, this->ducking_active_);
}

bool RuntimeFsm::va_running_() const { return this->va_ != nullptr && this->va_->is_running(); }

bool RuntimeFsm::boot_initializing_() const { return millis() < this->boot_led_until_; }

bool RuntimeFsm::wifi_connected_() const {
#ifdef USE_WIFI
  return wifi::global_wifi_component != nullptr && wifi::global_wifi_component->is_connected();
#else
  return true;
#endif
}

bool RuntimeFsm::api_connected_() const {
#ifdef USE_API
  return api::global_api_server != nullptr && api::global_api_server->is_connected();
#else
  return true;
#endif
}

bool RuntimeFsm::media_announcing_() const {
  return this->media_player_ != nullptr && this->media_player_->state == media_player::MEDIA_PLAYER_STATE_ANNOUNCING;
}

bool RuntimeFsm::media_playing_() const {
  return this->media_player_ != nullptr && this->media_player_->state == media_player::MEDIA_PLAYER_STATE_PLAYING;
}

IntercomPhase RuntimeFsm::intercom_phase_() const {
#ifdef USE_INTERCOM_API
  if (this->intercom_ == nullptr)
    return IntercomPhase::IDLE;
  if (this->intercom_->is_ringing())
    return IntercomPhase::RINGING;
  if (this->intercom_->is_outgoing())
    return IntercomPhase::OUTGOING;
  if (this->intercom_->is_streaming())
    return IntercomPhase::STREAMING;
#endif
  return IntercomPhase::IDLE;
}

}  // namespace esphome::runtime_fsm
