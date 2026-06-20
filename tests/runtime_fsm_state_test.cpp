#include "../esphome/components/runtime_fsm/runtime_fsm_state.h"

#include <cstdlib>
#include <iostream>

using namespace esphome::runtime_fsm;

static void check(bool condition, const char *message) {
  if (!condition) {
    std::cerr << "FAIL: " << message << "\n";
    std::exit(1);
  }
}

static RuntimeState step(RuntimeState state, ObservedState observed, EventType event, uint32_t expected_effects = 0) {
  auto result = reduce(state, observed, Event{event});
  check(validate_invariants(result.state), "invariant");
  if (expected_effects != 0) {
    check((result.effects & expected_effects) == expected_effects, "expected effects missing");
  }
  return result.state;
}

static void normal_tts_no_media() {
  RuntimeState s;
  ObservedState o;
  s = step(s, o, EventType::WAKE_WORD, EFFECT_REQUEST_VA_START);
  check(s.phase == VaPhase::START_REQUESTED, "wake starts requested");
  check(s.va_epoch == 0 && s.proposed_epoch == 1, "epoch proposed not committed");
  o.va_running = true;
  s = step(s, o, EventType::VA_START_ACCEPTED);
  check(s.va_epoch == 1, "epoch committed on VA start");
  s = step(s, o, EventType::VA_LISTENING);
  s = step(s, o, EventType::VA_THINKING);
  s = step(s, o, EventType::TTS_SYNTHESIS_STARTED);
  check(s.phase == VaPhase::TTS_SYNTHESIZING && s.tts_epoch == 1, "tts synth owns epoch");
  s = step(s, o, EventType::TTS_URL_ACCEPTED, EFFECT_PROXY_ANNOUNCING | EFFECT_START_TTS_CURRENT);
  check(s.phase == VaPhase::TTS_QUEUED, "tts queued");
  o.announcement_playing = true;
  s = step(s, o, EventType::TTS_SOURCE_PLAYING);
  check(s.phase == VaPhase::TTS_PLAYING, "tts playing");
  o.announcement_playing = false;
  s = step(s, o, EventType::TTS_SOURCE_TERMINAL);
  check(s.phase == VaPhase::TTS_DRAINING, "tts draining");
  s = step(s, o, EventType::TTS_DRAINED, EFFECT_PROXY_IDLE);
  check(s.phase == VaPhase::WAITING_FOLLOWUP, "waiting followup after tts");
  o.va_running = false;
  s = step(s, o, EventType::VA_RUN_ENDED);
  check(s.phase == VaPhase::IDLE, "idle after followup done");
}

static void media_ducking_persists_through_tts() {
  RuntimeState s;
  ObservedState o;
  o.media_playing = true;
  recompute_outputs(s, o);
  check((s.activity_mask & ACT_MEDIA) != 0, "media bit");
  s = step(s, o, EventType::WAKE_WORD, EFFECT_REQUEST_VA_START);
  o.va_running = true;
  s = step(s, o, EventType::VA_START_ACCEPTED);
  s = step(s, o, EventType::VA_LISTENING);
  s = step(s, o, EventType::VA_THINKING);
  s = step(s, o, EventType::TTS_SYNTHESIS_STARTED);
  s = step(s, o, EventType::TTS_URL_ACCEPTED);
  check((s.activity_mask & ACT_MEDIA) != 0, "media remains under tts");
  check((s.activity_mask & ACT_TTS_QUEUED) != 0, "tts queued bit");
  check(s.ui_state == UiState::RESPONDING, "tts ui priority over media");
}

static void duplicate_mww_is_ignored_while_listening() {
  RuntimeState s;
  ObservedState o;
  s = step(s, o, EventType::WAKE_WORD, EFFECT_REQUEST_VA_START);
  o.va_running = true;
  s = step(s, o, EventType::VA_START_ACCEPTED);
  s = step(s, o, EventType::VA_LISTENING);
  RuntimeState before = s;
  s = step(s, o, EventType::WAKE_WORD);
  check(s.phase == before.phase, "duplicate mww listening ignored");
  check(!s.barge_pending, "no barge while listening");
}

static void barge_during_tts_playing() {
  RuntimeState s;
  ObservedState o;
  s = step(s, o, EventType::WAKE_WORD, EFFECT_REQUEST_VA_START);
  o.va_running = true;
  s = step(s, o, EventType::VA_START_ACCEPTED);
  s = step(s, o, EventType::VA_THINKING);
  s = step(s, o, EventType::TTS_SYNTHESIS_STARTED);
  s = step(s, o, EventType::TTS_URL_ACCEPTED);
  o.announcement_playing = true;
  s = step(s, o, EventType::TTS_SOURCE_PLAYING);
  s = step(s, o, EventType::WAKE_WORD, EFFECT_REQUEST_VA_STOP | EFFECT_STOP_TTS_PATH);
  check(s.phase == VaPhase::CANCEL_REQUESTED && s.barge_pending, "barge cancel requested");
  check(s.cancelled_epoch == 1, "cancelled epoch set");
  o.va_running = false;
  o.announcement_playing = false;
  s = step(s, o, EventType::BARGE_READY, EFFECT_REQUEST_VA_START);
  check(s.phase == VaPhase::START_REQUESTED, "barge starts new request");
  check(s.proposed_epoch == 2, "new proposed epoch");
}

static void intercom_priority_over_media() {
  RuntimeState s;
  ObservedState o;
  o.media_playing = true;
  o.intercom = IntercomPhase::STREAMING;
  recompute_outputs(s, o);
  check((s.activity_mask & ACT_MEDIA) != 0, "media bit under intercom");
  check((s.activity_mask & ACT_INTERCOM_STREAMING) != 0, "intercom streaming bit");
  check(s.ui_state == UiState::INTERCOM_STREAMING, "intercom priority over media");
}

int main() {
  normal_tts_no_media();
  media_ducking_persists_through_tts();
  duplicate_mww_is_ignored_while_listening();
  barge_during_tts_playing();
  intercom_priority_over_media();
  std::cout << "runtime_fsm_state tests passed\n";
}
