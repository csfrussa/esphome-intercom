#include "intercom_api.h"

#ifdef USE_ESP32

#include <algorithm>
#include <cstring>

#include "esphome/core/application.h"
#include "esphome/components/network/util.h"
#include "esphome/core/hal.h"
#include "esphome/core/helpers.h"
#include "esphome/core/log.h"
#include "../audio_processor/ring_buffer_caps.h"
#include "../audio_processor/task_utils.h"
#ifdef USE_INTERCOM_SIP_TRANSPORT
#include "sip_transport.h"
#endif

#include "esp_event.h"
#include "esp_netif.h"

namespace esphome {
namespace intercom_api {

static const char *const TAG = "intercom_api";

void IntercomApi::append_audio_format_(AudioFormatList *list, const AudioFormat &format) {
  if (list == nullptr || !format.is_valid()) return;
  for (uint8_t i = 0; i < list->count; i++) {
    if (list->formats[i] == format) return;
  }
  if (list->count >= INTERCOM_MAX_AUDIO_FORMATS) {
    ESP_LOGW(TAG, "Ignoring extra intercom audio format: max supported format count is %u",
             (unsigned) INTERCOM_MAX_AUDIO_FORMATS);
    return;
  }
  list->formats[list->count++] = format;
}

bool IntercomApi::ensure_mic_processing_buffer_() {
#ifdef USE_INTERCOM_API_MIC
  if (this->tx_audio_format_.pcm_format != PcmFormat::S16LE) {
    ESP_LOGE(TAG, "mic_gain and dc_offset_removal require intercom_api.audio.tx.pcm_format: s16le");
    return false;
  }
  if (this->mic_converted_.load(std::memory_order_acquire) != nullptr)
    return true;

  RAMAllocator<int16_t> alloc = this->buffers_in_psram_
      ? RAMAllocator<int16_t>()
      : RAMAllocator<int16_t>(RAMAllocator<int16_t>::ALLOC_INTERNAL);
  int16_t *buf = alloc.allocate(this->mic_processing_samples_());
  if (buf == nullptr) {
    ESP_LOGE(TAG, "Failed to allocate mic processing buffer");
    return false;
  }
  int16_t *expected = nullptr;
  if (!this->mic_converted_.compare_exchange_strong(
          expected, buf, std::memory_order_release, std::memory_order_acquire)) {
    alloc.deallocate(buf, this->mic_processing_samples_());
  }
  return true;
#else
  ESP_LOGW(TAG, "Ignoring mic processing request: this intercom endpoint has no microphone");
  return false;
#endif
}

void IntercomApi::cleanup_partial_setup_() {
  // Transactional setup cleanup. force_delete is safe here only because tasks
  // were just spawned and have not entered a blocking upstream call yet.
#ifdef USE_INTERCOM_API_MIC
  audio_processor::force_delete_pinned_task(&this->tx_task_handle_, &this->tx_task_stack_,
                                             IntercomApi::kTxTaskStackBytes);

  RAMAllocator<int16_t> i16_alloc;
  if (int16_t *mic_converted = this->mic_converted_.exchange(nullptr, std::memory_order_acq_rel)) {
    i16_alloc.deallocate(mic_converted, this->mic_processing_samples_());
  }

  RAMAllocator<uint8_t> u8_alloc;
  if (this->tx_audio_chunk_ != nullptr) {
    u8_alloc.deallocate(this->tx_audio_chunk_, this->tx_audio_chunk_bytes_());
    this->tx_audio_chunk_ = nullptr;
  }

  this->mic_buffer_.reset();
#endif
  this->transport_.reset();
}

bool IntercomApi::allocate_setup_buffers_() {
#ifdef USE_INTERCOM_API_MIC
  if (this->has_microphone_()) {
    const size_t tx_frame_bytes = this->tx_audio_chunk_bytes_();
    const size_t tx_buffer_bytes = std::max<size_t>(tx_frame_bytes * 4, tx_frame_bytes + 1024);
    this->mic_buffer_ = this->buffers_in_psram_
        ? audio_processor::create_prefer_psram(tx_buffer_bytes, "intercom.mic")
        : audio_processor::create_internal(tx_buffer_bytes, "intercom.mic");
    if (!this->mic_buffer_) {
      ESP_LOGE(TAG, "Failed to allocate mic ring buffer");
      return false;
    }
  }

  if (this->has_microphone_() && this->dc_offset_removal_) {
    if (this->tx_audio_format_.pcm_format != PcmFormat::S16LE) {
      ESP_LOGE(TAG, "dc_offset_removal requires intercom_api.audio.tx.pcm_format: s16le");
      return false;
    }
    if (!this->ensure_mic_processing_buffer_()) {
      return false;
    }
  }

  // Per-iteration drain buffers; same placement policy as above.
  RAMAllocator<uint8_t> psram_u8 = this->buffers_in_psram_
      ? RAMAllocator<uint8_t>()
      : RAMAllocator<uint8_t>(RAMAllocator<uint8_t>::ALLOC_INTERNAL);
  if (this->has_microphone_()) {
    this->tx_audio_chunk_ = psram_u8.allocate(this->tx_audio_chunk_bytes_());
    if (!this->tx_audio_chunk_) {
      ESP_LOGE(TAG, "Failed to allocate tx audio chunk buffer");
      return false;
    }
  }
#endif

  return true;
}

bool IntercomApi::setup_audio_processor_() {
#ifdef USE_INTERCOM_API_MIC
  if (this->microphone_ != nullptr) {
    this->microphone_->add_data_callback([this](const std::vector<uint8_t> &data) {
      this->on_microphone_data_(data.data(), data.size());
    });
  }
  if (this->microphone_source_ != nullptr) {
    this->microphone_source_->add_data_callback([this](const std::vector<uint8_t> &data) {
      this->on_microphone_data_(data.data(), data.size());
    });
  }
#endif
  return true;
}

bool IntercomApi::setup_transport_() {
#ifdef USE_INTERCOM_SIP_TRANSPORT
  this->transport_ = std::make_unique<SipTransport>(
      this->sip_port_, this->rtp_port_, "",
      this->task_stacks_in_psram_);
  this->transport_->set_sip_signaling_transport(this->protocol_ == TransportType::TCP);
#else
  ESP_LOGE(TAG, "SIP transport was not compiled into this firmware");
  return false;
#endif
  if (!this->transport_) {
    ESP_LOGE(TAG, "Failed to allocate transport");
    return false;
  }

  this->transport_->set_audio_formats(this->tx_audio_formats_, this->rx_audio_formats_);

  // Wire callbacks before start() so the transport task never fires into null.
  this->transport_->set_audio_callback(IntercomApi::transport_audio_callback_, this);
  this->transport_->set_sip_signal_callback(IntercomApi::transport_sip_signal_callback_, this);
  this->transport_->set_connection_callback(IntercomApi::transport_connection_callback_, this);
  this->transport_->set_accept_callback(IntercomApi::transport_accept_callback_, this);

  if (!this->transport_->start()) {
    ESP_LOGE(TAG, "Transport failed to start");
    return false;
  }
  return true;
}

void IntercomApi::transport_audio_callback_(void *ctx, const uint8_t *pcm, size_t bytes) {
  static_cast<IntercomApi *>(ctx)->on_audio_received_(pcm, bytes);
}

void IntercomApi::transport_sip_signal_callback_(void *ctx, const SipSignal &signal) {
  static_cast<IntercomApi *>(ctx)->on_sip_signal_received_(signal);
}

void IntercomApi::transport_connection_callback_(void *ctx, bool connected) {
  static_cast<IntercomApi *>(ctx)->on_connection_change_(connected);
}

bool IntercomApi::transport_accept_callback_(void *ctx) {
  return static_cast<IntercomApi *>(ctx)->can_accept_session_();
}

bool IntercomApi::start_runtime_tasks_() {
#ifdef USE_INTERCOM_API_MIC
  // TX task exists only when a microphone is configured. Speaker-only peers
  // still accept calls and play incoming audio through the transport recv task.
  if (this->has_microphone_()) {
    if (!audio_processor::start_pinned_task(IntercomApi::tx_task, "intercom_tx",
                                             IntercomApi::kTxTaskStackBytes, this, 5, 0,
                                             this->task_stacks_in_psram_, TAG,
                                             &this->tx_task_handle_, &this->tx_task_tcb_,
                                             &this->tx_task_stack_)) {
      return false;
    }
  }
#endif
  return true;
}

void IntercomApi::publish_initial_state_later_() {
  // Deferred so sensors are fully wired before the first publish.
  this->set_timeout(SCHED_PUBLISH_INITIAL_STATE, 250, [this]() {
    this->publish_state_();
    this->publish_destination_();
    this->publish_transport_();
    this->publish_endpoint_();
    this->publish_sip_snapshot_();
  });
}

void IntercomApi::fail_setup_() {
  this->cleanup_partial_setup_();
  this->mark_failed();
}

void IntercomApi::setup() {
  ESP_LOGI(TAG, "Setting up Intercom API...");

  ESP_LOGI(TAG, "Audio capability: %s (SIP/%s, tasks: %s)",
           this->audio_capability_(),
           this->protocol_ == TransportType::TCP ? "TCP" : "UDP",
           this->has_microphone_() ? "tx+rx/control" : "rx/control");

  if (!this->allocate_setup_buffers_()) {
    this->fail_setup_();
    return;
  }
  if (!this->setup_audio_processor_()) {
    this->fail_setup_();
    return;
  }
  if (!this->setup_transport_()) {
    this->fail_setup_();
    return;
  }
  if (!this->start_runtime_tasks_()) {
    this->fail_setup_();
    return;
  }

  this->load_settings_();
  esp_event_handler_instance_register(IP_EVENT, ESP_EVENT_ANY_ID,
                                      &IntercomApi::ip_event_handler_,
                                      this, nullptr);
  this->publish_initial_state_later_();

  ESP_LOGI(TAG, "Intercom API ready as SIP phone on %s/%u RTP UDP/%u",
           this->protocol_ == TransportType::TCP ? "TCP" : "UDP",
           (unsigned) this->sip_port_, (unsigned) this->rtp_port_);
}

void IntercomApi::handle_call_timeouts_(uint32_t now_ms, uint32_t calling_timeout_ms) {
  const CallState state = this->call_state_.load(std::memory_order_acquire);
  if (this->ringing_timeout_ms_ > 0 && state == CallState::RINGING &&
      now_ms - this->ringing_start_time_ >= this->ringing_timeout_ms_) {
    const std::string cid = this->get_current_call_id_();
    ESP_LOGI(TAG, "Ringing timeout after %u ms - declining caller (call_id=%s)",
             this->ringing_timeout_ms_, cid.c_str());
    this->fire_timeout_decline_();
    return;
  }

  if (state == CallState::CALLING &&
      now_ms - this->calling_start_time_ >= INVITE_NO_RESPONSE_TIMEOUT_MS) {
    bool saw_sip_response = false;
    if (this->transport_ != nullptr) {
      saw_sip_response = this->transport_->snapshot().last_sip_status_code != 0;
    }
    if (!saw_sip_response) {
      const std::string cid = this->get_current_call_id_();
      ESP_LOGI(TAG, "SIP INVITE timeout after %u ms without response - ending call (call_id=%s)",
               (unsigned) INVITE_NO_RESPONSE_TIMEOUT_MS, cid.c_str());
      this->fire_timeout_decline_();
      return;
    }
  }

  if (calling_timeout_ms > 0 && (state == CallState::CALLING || state == CallState::REMOTE_RINGING) &&
      now_ms - this->calling_start_time_ >= calling_timeout_ms) {
    const std::string cid = this->get_current_call_id_();
    ESP_LOGI(TAG, "Calling timeout after %u ms - sending CANCEL (call_id=%s)",
             calling_timeout_ms, cid.c_str());
    this->fire_timeout_decline_();
  }
}

void IntercomApi::loop() {
  if (this->endpoint_publish_requested_.exchange(false, std::memory_order_acq_rel)) {
    this->publish_endpoint_();
  }

  // Phonebook cycle timeout safeguard: a stuck on_update_contacts chain (e.g.
  // an external update source never completes) would otherwise leave the cycle open forever
  // and block subsequent counter advances. CYCLE_TIMEOUT_MS commits forcibly.
  if (this->cycle_active_ &&
      (millis() - this->cycle_started_at_) > CYCLE_TIMEOUT_MS) {
    ESP_LOGD("intercom_api", "Phonebook update cycle auto-commit after %u ms",
             (unsigned) CYCLE_TIMEOUT_MS);
    this->commit_cycle_();
  }

  // Auto-decline timeouts (0 = disabled). CALLING falls back to
  // ringing_timeout when calling_timeout is unset.
  uint32_t now = millis();
  const uint32_t calling_to = this->calling_timeout_ms_ > 0
                                ? this->calling_timeout_ms_
                                : this->ringing_timeout_ms_;

  this->handle_call_timeouts_(now, calling_to);
  bool keep_loop = this->cycle_active_ ||
                   this->call_state_.load(std::memory_order_acquire) != CallState::IDLE;
  keep_loop = keep_loop || this->endpoint_publish_requested_.load(std::memory_order_acquire);
  if (!keep_loop) {
    this->disable_loop();
  }
}

void IntercomApi::fire_timeout_decline_() {
  // Timeout sends CANCEL for pending outbound INVITE or a SIP final response for inbound ringing.
  const std::string call_id = this->get_current_call_id_();
  if (this->transport_ && this->transport_->is_connected() && !call_id.empty()) {
    this->send_sip_final_response_(call_id, kReasonTimeout);
  }
  this->set_terminal_response_(call_id, kReasonTimeout);
  this->set_active_(false);
  this->in_call_.store(false, std::memory_order_release);
  this->end_call_(CallEndReason::TIMEOUT, kReasonTimeout);
  if (this->transport_) this->transport_->disconnect();
}

void IntercomApi::dump_config() {
  ESP_LOGCONFIG(TAG, "Intercom API:");
  if (this->transport_) {
    ESP_LOGCONFIG(TAG, "  Transport: %s", this->transport_->transport_name());
  } else {
    ESP_LOGCONFIG(TAG, "  Transport: (not initialised)");
  }
  ESP_LOGCONFIG(TAG, "  SIP listen port: %u", (unsigned) this->sip_port_);
  ESP_LOGCONFIG(TAG, "  RTP port: %u", (unsigned) this->rtp_port_);
  ESP_LOGCONFIG(TAG, "  Routing: SIP dial plan");
  ESP_LOGCONFIG(TAG, "  HA peer name: %s", this->ha_peer_name_.c_str());
  ESP_LOGCONFIG(TAG, "  Audio capability: %s", this->audio_capability_());
  ESP_LOGCONFIG(TAG, "  HA as first contact: %s", YESNO(this->use_ha_as_first_contact_));
  ESP_LOGCONFIG(TAG, "  Phonebook source: HA SIP phonebook");
#ifdef USE_INTERCOM_API_MIC
  ESP_LOGCONFIG(TAG, "  Microphone: %s", this->microphone_ ? "direct" : (this->microphone_source_ ? "source" : "none"));
#endif
#ifdef USE_INTERCOM_API_SPEAKER
  ESP_LOGCONFIG(TAG, "  Speaker: %s", this->speaker_ ? "configured" : "none");
#endif
  ESP_LOGCONFIG(TAG, "  Tasks: %s", this->has_microphone_() ? "tx+rx/control" : "rx/control only");
  ESP_LOGCONFIG(TAG, "  Device Name: %s",
                this->device_name_.empty() ? "(unset)" : this->device_name_.c_str());
  if (this->ringing_timeout_ms_ > 0) {
    ESP_LOGCONFIG(TAG, "  Ringing Timeout: %u ms", this->ringing_timeout_ms_);
  } else {
    ESP_LOGCONFIG(TAG, "  Ringing Timeout: disabled");
  }
  if (this->calling_timeout_ms_ > 0) {
    ESP_LOGCONFIG(TAG, "  Calling Timeout: %u ms", this->calling_timeout_ms_);
  } else {
    ESP_LOGCONFIG(TAG, "  Calling Timeout: disabled");
  }
  ESP_LOGCONFIG(TAG, "  Contacts: %zu configured", this->phonebook_.size());
}

void IntercomApi::set_remote_endpoint(const std::string &ip, uint16_t port, uint16_t rtp_port) {
  if (this->transport_ != nullptr) {
    this->transport_->set_remote(ip, port, rtp_port);
  }
  ESP_LOGI(TAG, "Remote endpoint updated to SIP %s:%u RTP %u", ip.c_str(), (unsigned) port,
           (unsigned) (rtp_port != 0 ? rtp_port : this->rtp_port_));
}

void IntercomApi::set_remote_sip_transport_tcp(bool tcp) {
  if (this->transport_ != nullptr) {
    this->transport_->set_sip_signaling_transport(tcp);
  }
  ESP_LOGI(TAG, "Remote SIP signaling transport set to %s", tcp ? "TCP" : "UDP");
}

void IntercomApi::publish_transport_() {
  if (this->transport_sensor_ != nullptr) {
    this->transport_sensor_->publish_state(this->protocol_ == TransportType::TCP ? "tcp" : "udp");
  }
}

std::string IntercomApi::audio_format_token_(const AudioFormat &fmt) {
  const char *pcm = "s16le";
  switch (fmt.pcm_format) {
    case PcmFormat::S16LE:
      pcm = "s16le";
      break;
    case PcmFormat::S24LE:
      pcm = "s24le";
      break;
    case PcmFormat::S24LE_IN_S32:
      pcm = "s24le_in_s32";
      break;
    case PcmFormat::S32LE:
      pcm = "s32le";
      break;
    default:
      pcm = "s16le";
      break;
  }
  char token[48];
  snprintf(token, sizeof(token), "%u:%s:%u:%u",
           (unsigned) fmt.sample_rate, pcm,
           (unsigned) fmt.channels, (unsigned) fmt.frame_ms);
  return token;
}

std::string IntercomApi::local_ip_string_() const {
  char ip[network::IP_ADDRESS_BUFFER_SIZE];
  for (auto &address : network::get_ip_addresses()) {
    if (!address.is_ip4()) continue;
    address.str_to(ip);
    if (strcmp(ip, "0.0.0.0") != 0) {
      return ip;
    }
  }
  return "";
}

std::string IntercomApi::build_endpoint_string_() const {
  const std::string name = !this->device_name_.empty()
                               ? this->device_name_
                               : App.get_friendly_name().str();
  const std::string ip = this->local_ip_string_();
  if (name.empty() || ip.empty()) {
    return "";
  }

  auto format_list_token = [&](const AudioFormatList &list) -> std::string {
    std::string out;
    for (uint8_t i = 0; i < list.count; i++) {
      if (!out.empty()) out += ";";
      out += IntercomApi::audio_format_token_(list.formats[i]);
    }
    return out;
  };
  const std::string tx = format_list_token(this->tx_audio_formats_);
  const std::string rx = format_list_token(this->rx_audio_formats_);
  char buf[768];
  snprintf(buf, sizeof(buf), "%s|%s|%u|%u|%s|%s|%s|%s", name.c_str(), ip.c_str(),
           (unsigned) this->sip_port_, (unsigned) this->rtp_port_,
           this->audio_capability_(), tx.c_str(), rx.c_str(),
           this->protocol_ == TransportType::TCP ? "sip_tcp" : "sip_udp");
  return buf;
}

std::string IntercomApi::build_sip_snapshot_string_() const {
  auto field_escape = [](const std::string &in, size_t max_len = 32) -> std::string {
    std::string out;
    for (char ch : in) {
      if (ch == '\r' || ch == '\n' || ch == ';' || ch == '|') {
        out.push_back(' ');
      } else {
        out.push_back(ch);
      }
      if (out.size() >= max_len) break;
    }
    return out;
  };
  CallSnapshot call = this->snapshot_call_identity_();
  const std::string state = this->get_call_state_str();
  std::string direction;
  if (!call.caller_name.empty() && call.caller_name == this->device_name_) {
    direction = "outgoing";
  } else if (!call.dest_name.empty() && call.dest_name == this->device_name_) {
    direction = "incoming";
  } else if (this->call_state_.load(std::memory_order_acquire) == CallState::CALLING) {
    direction = "outgoing";
  } else if (this->call_state_.load(std::memory_order_acquire) == CallState::RINGING) {
    direction = "incoming";
  }
  if (call.call_id.empty() && !this->last_terminal_call_id_.empty() && !this->last_reason_.empty()) {
    call.call_id = this->last_terminal_call_id_;
    call.caller_name = this->last_terminal_caller_name_;
    call.dest_name = this->last_terminal_dest_name_;
    direction = this->last_terminal_direction_;
  }
  std::string contact = this->phonebook_.current_name();
  uint32_t rtp_tx_packets = 0;
  uint32_t rtp_rx_packets = 0;
  uint32_t rtp_tx_bytes = 0;
  uint32_t rtp_rx_bytes = 0;
  uint16_t sip_status = 0;
  const char *last_event = "";
  std::string selected_tx = IntercomApi::audio_format_token_(this->current_tx_audio_format_);
  std::string selected_rx = IntercomApi::audio_format_token_(this->current_rx_audio_format_);
  if (this->transport_ != nullptr) {
    const SipTransportSnapshot snap = this->transport_->snapshot();
    rtp_tx_packets = snap.rtp_tx_packets;
    rtp_rx_packets = snap.rtp_rx_packets;
    rtp_tx_bytes = snap.rtp_tx_bytes;
    rtp_rx_bytes = snap.rtp_rx_bytes;
    sip_status = snap.last_sip_status_code;
    last_event = snap.last_sip_event;
    selected_tx = IntercomApi::audio_format_token_(snap.selected_tx_format);
    selected_rx = IntercomApi::audio_format_token_(snap.selected_rx_format);
  }
  char out[256];
  snprintf(out, sizeof(out),
           "st=%s;id=%s;dir=%s;from=%s;to=%s;ct=%s;tr=%s;sc=%u;"
           "tx=%s;rx=%s;pt=%u;pr=%u;bt=%u;br=%u;rs=%s;ev=%s",
           field_escape(state, 18).c_str(),
           field_escape(call.call_id, 24).c_str(),
           field_escape(direction, 8).c_str(),
           field_escape(call.caller_name, 20).c_str(),
           field_escape(call.dest_name, 20).c_str(),
           field_escape(contact, 20).c_str(),
           this->protocol_ == TransportType::TCP ? "tcp" : "udp",
           (unsigned) sip_status,
           field_escape(selected_tx, 20).c_str(),
           field_escape(selected_rx, 20).c_str(),
           (unsigned) rtp_tx_packets,
           (unsigned) rtp_rx_packets,
           (unsigned) rtp_tx_bytes,
           (unsigned) rtp_rx_bytes,
           field_escape(this->last_reason_, 22).c_str(),
           field_escape(last_event, 22).c_str());
  return out;
}

void IntercomApi::publish_sip_snapshot_() {
  if (this->sip_snapshot_sensor_ == nullptr) return;
  const std::string snapshot = this->build_sip_snapshot_string_();
  if (snapshot == this->last_sip_snapshot_) return;
  this->last_sip_snapshot_ = snapshot;
  this->sip_snapshot_sensor_->publish_state(snapshot);
}

void IntercomApi::publish_endpoint_() {
  std::string endpoint = this->build_endpoint_string_();
  if (endpoint.empty()) {
    ESP_LOGW(TAG, "Intercom endpoint waiting for IPv4 address from ESPHome network");
    return;
  }
  if (this->endpoint_sensor_ != nullptr && endpoint != this->last_endpoint_) {
    this->last_endpoint_ = endpoint;
    this->endpoint_sensor_->publish_state(endpoint);
  }
  this->publish_sip_snapshot_();
}

void IntercomApi::request_endpoint_publish_() {
  this->endpoint_publish_requested_.store(true, std::memory_order_release);
  this->enable_loop_soon_any_context();
}

void IntercomApi::ip_event_handler_(void *arg, esp_event_base_t event_base,
                                    int32_t event_id, void *event_data) {
  if (event_base != IP_EVENT) return;
  if (event_id != IP_EVENT_STA_GOT_IP && event_id != IP_EVENT_ETH_GOT_IP) return;
  static_cast<IntercomApi *>(arg)->request_endpoint_publish_();
}

// Wired from YAML via `api.on_client_connected:`.
void IntercomApi::publish_entity_states() {
  // Re-publish on every HA reconnect so intercom_native sees it without
  // depending on HA restart timing. Restore-backed switches are applied only
  // once; a reconnect must not roll runtime state back to the boot preference.
  const bool apply_restore = !this->entity_restore_applied_;
  this->entity_restore_applied_ = true;

  this->publish_transport_();
  this->publish_endpoint_();
  this->publish_sip_snapshot_();
  if (this->last_reason_sensor_ != nullptr) {
    this->last_reason_sensor_->publish_state(this->last_reason_);
  }

  if (this->auto_answer_switch_ != nullptr) {
    if (apply_restore) {
      auto initial = this->auto_answer_switch_->get_initial_state_with_restore_mode();
      if (initial.has_value()) {
        this->auto_answer_ = *initial;
      }
    }
    this->auto_answer_switch_->publish_state(this->auto_answer_);
  }

  if (this->dnd_switch_ != nullptr) {
    if (apply_restore) {
      auto initial = this->dnd_switch_->get_initial_state_with_restore_mode();
      if (initial.has_value()) {
        this->do_not_disturb_ = *initial;
      }
    }
    this->dnd_switch_->publish_state(this->do_not_disturb_);
  }

  ESP_LOGD(TAG, "Entity states synced (vol=%.0f%%, mic=%.1fdB, auto=%s, dnd=%s)",
           this->volume_.load(std::memory_order_relaxed) * 100.0f, this->mic_gain_db_,
           this->auto_answer_ ? "ON" : "OFF", this->do_not_disturb_ ? "ON" : "OFF");

  if (this->volume_number_ != nullptr) {
    this->volume_number_->publish_state(this->volume_.load(std::memory_order_relaxed) * 100.0f);
  }
  if (this->mic_gain_number_ != nullptr) {
    this->mic_gain_number_->publish_state(this->mic_gain_db_);
  }
}

}  // namespace intercom_api
}  // namespace esphome

#endif  // USE_ESP32
