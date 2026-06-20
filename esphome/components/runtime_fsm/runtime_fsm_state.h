#pragma once

#include <cstdint>

namespace esphome::runtime_fsm {

enum class VaPhase : uint8_t {
  IDLE = 0,
  START_REQUESTED,
  LISTENING,
  THINKING,
  TTS_SYNTHESIZING,
  TTS_QUEUED,
  TTS_PLAYING,
  TTS_DRAINING,
  WAITING_FOLLOWUP,
  CANCEL_REQUESTED,
  ERROR,
};

enum class EventType : uint8_t {
  WAKE_WORD,
  VA_START_ACCEPTED,
  VA_LISTENING,
  VA_THINKING,
  TTS_SYNTHESIS_STARTED,
  TTS_URL_ACCEPTED,
  TTS_URL_REJECTED,
  VA_RUN_ENDED,
  VA_ERROR,
  TTS_SOURCE_PLAYING,
  TTS_SOURCE_TERMINAL,
  TTS_DRAINED,
  BARGE_READY,
  OBSERVED_STATE_CHANGED,
};

enum ActivityBits : uint32_t {
  ACT_MEDIA = 1u << 0,
  ACT_ANNOUNCEMENT = 1u << 1,
  ACT_INTERCOM_RINGING = 1u << 2,
  ACT_INTERCOM_OUTGOING = 1u << 3,
  ACT_INTERCOM_STREAMING = 1u << 4,
  ACT_VA_STARTING = 1u << 5,
  ACT_VA_LISTENING = 1u << 6,
  ACT_VA_THINKING = 1u << 7,
  ACT_TTS_SYNTHESIZING = 1u << 8,
  ACT_TTS_QUEUED = 1u << 9,
  ACT_TTS_PLAYING = 1u << 10,
  ACT_TTS_DRAINING = 1u << 11,
  ACT_WAITING_FOLLOWUP = 1u << 12,
  ACT_BARGE_PENDING = 1u << 13,
  ACT_TIMER_RINGING = 1u << 14,
  ACT_ERROR = 1u << 15,
};

enum class IntercomPhase : uint8_t {
  IDLE = 0,
  RINGING,
  OUTGOING,
  STREAMING,
};

enum class UiState : uint8_t {
  IDLE = 0,
  INITIALIZING = 1,
  NO_WIFI = 2,
  NO_HA = 3,
  BOTH_MUTED = 4,
  MIC_MUTED = 5,
  SPEAKER_MUTED = 6,
  WAKE_DETECTED = 7,
  LISTENING = 8,
  THINKING = 9,
  RESPONDING = 10,
  INTERCOM_RINGING = 11,
  INTERCOM_OUTGOING = 12,
  INTERCOM_STREAMING = 13,
  ERROR = 14,
  MEDIA_PLAYING = 15,
};

enum EffectBits : uint32_t {
  EFFECT_NONE = 0,
  EFFECT_REQUEST_VA_START = 1u << 0,
  EFFECT_REQUEST_VA_STOP = 1u << 1,
  EFFECT_START_TTS_CURRENT = 1u << 2,
  EFFECT_STOP_TTS_PATH = 1u << 3,
  EFFECT_PROXY_ANNOUNCING = 1u << 4,
  EFFECT_PROXY_IDLE = 1u << 5,
  EFFECT_ACK_REJECTED_URL = 1u << 6,
};

struct ObservedState {
  bool va_running{false};
  bool proxy_announcing{false};
  bool tts_source_playing{false};
  bool tts_source_terminal{true};
  bool tts_audio_terminal{true};
  bool media_playing{false};
  bool announcement_playing{false};
  bool timer_ringing{false};
  bool mic_muted{false};
  bool speaker_muted{false};
  bool initializing{false};
  bool no_wifi{false};
  bool no_ha{false};
  IntercomPhase intercom{IntercomPhase::IDLE};
};

struct RuntimeState {
  VaPhase phase{VaPhase::IDLE};
  uint32_t activity_mask{0};
  uint32_t va_epoch{0};
  uint32_t proposed_epoch{0};
  uint32_t tts_epoch{0};
  uint32_t cancelled_epoch{0};
  bool barge_pending{false};
  bool has_current_url{false};
  bool has_queued_url{false};
  bool run_end_seen{false};
  UiState ui_state{UiState::IDLE};
  uint8_t led_state{0};
  uint8_t compat_va_state{0};
};

struct Event {
  EventType type{EventType::OBSERVED_STATE_CHANGED};
  bool has_wake_word{false};
  bool enqueue{false};
};

struct ReduceResult {
  RuntimeState state;
  uint32_t effects{EFFECT_NONE};
};

ReduceResult reduce(const RuntimeState &old_state, const ObservedState &observed, const Event &event);
void recompute_outputs(RuntimeState &state, const ObservedState &observed);
bool validate_invariants(const RuntimeState &state);
const char *phase_name(VaPhase phase);
const char *event_name(EventType event);

}  // namespace esphome::runtime_fsm
