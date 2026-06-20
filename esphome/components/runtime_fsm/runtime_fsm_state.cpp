#include "runtime_fsm_state.h"

namespace esphome::runtime_fsm {

static bool response_phase(VaPhase phase) {
  return phase == VaPhase::TTS_SYNTHESIZING || phase == VaPhase::TTS_QUEUED || phase == VaPhase::TTS_PLAYING ||
         phase == VaPhase::TTS_DRAINING || phase == VaPhase::WAITING_FOLLOWUP;
}

static bool barge_phase(VaPhase phase) {
  return phase == VaPhase::TTS_SYNTHESIZING || phase == VaPhase::TTS_QUEUED || phase == VaPhase::TTS_PLAYING ||
         phase == VaPhase::TTS_DRAINING;
}

ReduceResult reduce(const RuntimeState &old_state, const ObservedState &observed, const Event &event) {
  RuntimeState next = old_state;
  uint32_t effects = EFFECT_NONE;

  switch (event.type) {
    case EventType::WAKE_WORD:
      if (old_state.phase == VaPhase::IDLE || old_state.phase == VaPhase::WAITING_FOLLOWUP) {
        next.proposed_epoch = old_state.va_epoch + 1;
        next.phase = VaPhase::START_REQUESTED;
        next.run_end_seen = false;
        effects |= EFFECT_REQUEST_VA_START;
      } else if (barge_phase(old_state.phase) || observed.proxy_announcing) {
        next.phase = VaPhase::CANCEL_REQUESTED;
        next.barge_pending = true;
        next.cancelled_epoch = old_state.tts_epoch;
        effects |= EFFECT_REQUEST_VA_STOP | EFFECT_STOP_TTS_PATH | EFFECT_PROXY_IDLE;
      }
      break;

    case EventType::VA_START_ACCEPTED:
      if (old_state.proposed_epoch != 0) {
        next.va_epoch = old_state.proposed_epoch;
      } else if (old_state.phase == VaPhase::IDLE || old_state.phase == VaPhase::WAITING_FOLLOWUP) {
        next.va_epoch = old_state.va_epoch + 1;
      }
      next.proposed_epoch = 0;
      next.phase = VaPhase::START_REQUESTED;
      next.run_end_seen = false;
      break;

    case EventType::VA_LISTENING:
      if (old_state.phase != VaPhase::CANCEL_REQUESTED) {
        next.phase = VaPhase::LISTENING;
        next.run_end_seen = false;
      }
      break;

    case EventType::VA_THINKING:
      if (old_state.phase == VaPhase::LISTENING || old_state.phase == VaPhase::START_REQUESTED) {
        next.phase = VaPhase::THINKING;
      }
      break;

    case EventType::TTS_SYNTHESIS_STARTED:
      if (old_state.phase != VaPhase::CANCEL_REQUESTED) {
        next.tts_epoch = next.va_epoch;
        next.phase = VaPhase::TTS_SYNTHESIZING;
      }
      break;

    case EventType::TTS_URL_ACCEPTED:
      if (old_state.phase == VaPhase::TTS_SYNTHESIZING || old_state.phase == VaPhase::THINKING) {
        next.tts_epoch = next.va_epoch;
        next.phase = VaPhase::TTS_QUEUED;
        next.has_current_url = true;
        effects |= EFFECT_PROXY_ANNOUNCING | EFFECT_START_TTS_CURRENT;
      } else if (response_phase(old_state.phase) && event.enqueue) {
        next.has_queued_url = true;
      } else {
        effects |= EFFECT_ACK_REJECTED_URL;
      }
      break;

    case EventType::TTS_URL_REJECTED:
      effects |= EFFECT_ACK_REJECTED_URL;
      break;

    case EventType::TTS_SOURCE_PLAYING:
      if (old_state.phase == VaPhase::TTS_QUEUED || old_state.phase == VaPhase::TTS_SYNTHESIZING) {
        next.phase = VaPhase::TTS_PLAYING;
      } else if (old_state.phase == VaPhase::CANCEL_REQUESTED) {
        effects |= EFFECT_STOP_TTS_PATH;
      }
      break;

    case EventType::TTS_SOURCE_TERMINAL:
      if (old_state.phase == VaPhase::TTS_QUEUED || old_state.phase == VaPhase::TTS_PLAYING ||
          old_state.phase == VaPhase::TTS_SYNTHESIZING) {
        if (old_state.has_queued_url) {
          next.has_queued_url = false;
          next.phase = VaPhase::TTS_QUEUED;
          effects |= EFFECT_START_TTS_CURRENT;
        } else {
          next.phase = VaPhase::TTS_DRAINING;
          next.has_current_url = false;
        }
      }
      break;

    case EventType::TTS_DRAINED:
      if (old_state.phase == VaPhase::TTS_DRAINING) {
        next.tts_epoch = 0;
        next.has_current_url = false;
        next.has_queued_url = false;
        effects |= EFFECT_PROXY_IDLE;
        next.phase = old_state.barge_pending ? VaPhase::CANCEL_REQUESTED : VaPhase::WAITING_FOLLOWUP;
      }
      break;

    case EventType::VA_RUN_ENDED:
      next.run_end_seen = true;
      if (!response_phase(old_state.phase) && old_state.phase != VaPhase::CANCEL_REQUESTED && !observed.va_running) {
        next.phase = VaPhase::IDLE;
      }
      if (old_state.phase == VaPhase::WAITING_FOLLOWUP && !observed.va_running) {
        next.phase = VaPhase::IDLE;
      }
      break;

    case EventType::BARGE_READY:
      if (old_state.phase == VaPhase::CANCEL_REQUESTED && old_state.barge_pending) {
        next.proposed_epoch = old_state.va_epoch + 1;
        next.barge_pending = false;
        next.tts_epoch = 0;
        next.has_current_url = false;
        next.has_queued_url = false;
        next.phase = VaPhase::START_REQUESTED;
        effects |= EFFECT_REQUEST_VA_START;
      }
      break;

    case EventType::VA_ERROR:
      next.phase = VaPhase::ERROR;
      next.barge_pending = false;
      effects |= EFFECT_PROXY_IDLE | EFFECT_STOP_TTS_PATH;
      break;

    case EventType::OBSERVED_STATE_CHANGED:
      break;
  }

  recompute_outputs(next, observed);
  return ReduceResult{next, effects};
}

void recompute_outputs(RuntimeState &state, const ObservedState &observed) {
  uint32_t mask = 0;
  if (observed.media_playing)
    mask |= ACT_MEDIA;
  if (observed.announcement_playing)
    mask |= ACT_ANNOUNCEMENT;
  if (observed.timer_ringing)
    mask |= ACT_TIMER_RINGING;
  if (observed.intercom == IntercomPhase::RINGING)
    mask |= ACT_INTERCOM_RINGING;
  if (observed.intercom == IntercomPhase::OUTGOING)
    mask |= ACT_INTERCOM_OUTGOING;
  if (observed.intercom == IntercomPhase::STREAMING)
    mask |= ACT_INTERCOM_STREAMING;

  switch (state.phase) {
    case VaPhase::START_REQUESTED:
      mask |= ACT_VA_STARTING;
      break;
    case VaPhase::LISTENING:
      mask |= ACT_VA_LISTENING;
      break;
    case VaPhase::THINKING:
      mask |= ACT_VA_THINKING;
      break;
    case VaPhase::TTS_SYNTHESIZING:
      mask |= ACT_TTS_SYNTHESIZING;
      break;
    case VaPhase::TTS_QUEUED:
      mask |= ACT_TTS_QUEUED;
      break;
    case VaPhase::TTS_PLAYING:
      mask |= ACT_TTS_PLAYING;
      break;
    case VaPhase::TTS_DRAINING:
      mask |= ACT_TTS_DRAINING;
      break;
    case VaPhase::WAITING_FOLLOWUP:
      mask |= ACT_WAITING_FOLLOWUP;
      break;
    case VaPhase::CANCEL_REQUESTED:
      mask |= ACT_BARGE_PENDING;
      break;
    case VaPhase::ERROR:
      mask |= ACT_ERROR;
      break;
    default:
      break;
  }
  if (state.barge_pending)
    mask |= ACT_BARGE_PENDING;
  state.activity_mask = mask;

  UiState ui = UiState::IDLE;
  uint8_t led = 0;
  if (observed.initializing) {
    ui = UiState::INITIALIZING;
    led = 13;
  } else if (observed.no_wifi) {
    ui = UiState::NO_WIFI;
    led = 14;
  } else if (observed.no_ha) {
    ui = UiState::NO_HA;
    led = 15;
  } else if (observed.mic_muted && observed.speaker_muted) {
    ui = UiState::BOTH_MUTED;
    led = 1;
  } else if (observed.mic_muted) {
    ui = UiState::MIC_MUTED;
    led = 2;
  } else if (observed.speaker_muted) {
    ui = UiState::SPEAKER_MUTED;
    led = 3;
  } else if (mask & (ACT_BARGE_PENDING | ACT_VA_STARTING)) {
    ui = UiState::WAKE_DETECTED;
    led = 4;
  } else if (mask & ACT_VA_LISTENING) {
    ui = UiState::LISTENING;
    led = 5;
  } else if (mask & (ACT_VA_THINKING | ACT_TTS_SYNTHESIZING)) {
    ui = UiState::THINKING;
    led = 6;
  } else if (mask & (ACT_TTS_QUEUED | ACT_TTS_PLAYING | ACT_TTS_DRAINING | ACT_WAITING_FOLLOWUP)) {
    ui = UiState::RESPONDING;
    led = 7;
  } else if (mask & ACT_INTERCOM_RINGING) {
    ui = UiState::INTERCOM_RINGING;
    led = 8;
  } else if (mask & ACT_INTERCOM_OUTGOING) {
    ui = UiState::INTERCOM_OUTGOING;
    led = 9;
  } else if (mask & ACT_INTERCOM_STREAMING) {
    ui = UiState::INTERCOM_STREAMING;
    led = 10;
  } else if (mask & ACT_ERROR) {
    ui = UiState::ERROR;
    led = 11;
  } else if (mask & (ACT_ANNOUNCEMENT | ACT_MEDIA | ACT_TIMER_RINGING)) {
    ui = UiState::MEDIA_PLAYING;
    led = 12;
  }
  state.ui_state = ui;
  state.led_state = led;

  switch (state.phase) {
    case VaPhase::LISTENING:
      state.compat_va_state = 1;
      break;
    case VaPhase::THINKING:
      state.compat_va_state = 2;
      break;
    case VaPhase::TTS_SYNTHESIZING:
    case VaPhase::TTS_QUEUED:
    case VaPhase::TTS_PLAYING:
    case VaPhase::TTS_DRAINING:
    case VaPhase::WAITING_FOLLOWUP:
      state.compat_va_state = 3;
      break;
    case VaPhase::ERROR:
      state.compat_va_state = 4;
      break;
    default:
      state.compat_va_state = 0;
      break;
  }
}

bool validate_invariants(const RuntimeState &state) {
  if (state.phase == VaPhase::IDLE && (state.tts_epoch != 0 || state.barge_pending))
    return false;
  if (state.phase == VaPhase::CANCEL_REQUESTED && !state.barge_pending)
    return false;
  if (state.has_queued_url && !state.has_current_url)
    return false;
  return true;
}

const char *phase_name(VaPhase phase) {
  switch (phase) {
    case VaPhase::IDLE:
      return "IDLE";
    case VaPhase::START_REQUESTED:
      return "START_REQUESTED";
    case VaPhase::LISTENING:
      return "LISTENING";
    case VaPhase::THINKING:
      return "THINKING";
    case VaPhase::TTS_SYNTHESIZING:
      return "TTS_SYNTHESIZING";
    case VaPhase::TTS_QUEUED:
      return "TTS_QUEUED";
    case VaPhase::TTS_PLAYING:
      return "TTS_PLAYING";
    case VaPhase::TTS_DRAINING:
      return "TTS_DRAINING";
    case VaPhase::WAITING_FOLLOWUP:
      return "WAITING_FOLLOWUP";
    case VaPhase::CANCEL_REQUESTED:
      return "CANCEL_REQUESTED";
    case VaPhase::ERROR:
      return "ERROR";
  }
  return "?";
}

const char *event_name(EventType event) {
  switch (event) {
    case EventType::WAKE_WORD:
      return "WAKE_WORD";
    case EventType::VA_START_ACCEPTED:
      return "VA_START_ACCEPTED";
    case EventType::VA_LISTENING:
      return "VA_LISTENING";
    case EventType::VA_THINKING:
      return "VA_THINKING";
    case EventType::TTS_SYNTHESIS_STARTED:
      return "TTS_SYNTHESIS_STARTED";
    case EventType::TTS_URL_ACCEPTED:
      return "TTS_URL_ACCEPTED";
    case EventType::TTS_URL_REJECTED:
      return "TTS_URL_REJECTED";
    case EventType::VA_RUN_ENDED:
      return "VA_RUN_ENDED";
    case EventType::VA_ERROR:
      return "VA_ERROR";
    case EventType::TTS_SOURCE_PLAYING:
      return "TTS_SOURCE_PLAYING";
    case EventType::TTS_SOURCE_TERMINAL:
      return "TTS_SOURCE_TERMINAL";
    case EventType::TTS_DRAINED:
      return "TTS_DRAINED";
    case EventType::BARGE_READY:
      return "BARGE_READY";
    case EventType::OBSERVED_STATE_CHANGED:
      return "OBSERVED_STATE_CHANGED";
  }
  return "?";
}

}  // namespace esphome::runtime_fsm
