#include "voip_simulator.h"

#include <algorithm>
#include <cctype>
#include <cerrno>
#include <chrono>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <filesystem>
#include <fstream>
#include <sstream>
#include <sys/socket.h>
#include <sys/un.h>
#include <unistd.h>

#include "esphome/core/log.h"

namespace esphome {
namespace voip_simulator {

static const char *const TAG = "voip_simulator";

namespace {

std::string json_escape(const std::string &value) {
  std::string out;
  out.reserve(value.size() + 8);
  for (char ch : value) {
    switch (ch) {
      case '\\':
        out += "\\\\";
        break;
      case '"':
        out += "\\\"";
        break;
      case '\n':
        out += "\\n";
        break;
      case '\r':
        out += "\\r";
        break;
      case '\t':
        out += "\\t";
        break;
      default:
        out += ch;
        break;
    }
  }
  return out;
}

std::string q(const std::string &value) { return "\"" + json_escape(value) + "\""; }
std::string b(bool value) { return value ? "true" : "false"; }

uintmax_t file_size_or_zero(const std::string &path) {
  if (path.empty())
    return 0;
  std::error_code ec;
  const auto size = std::filesystem::file_size(path, ec);
  return ec ? 0 : size;
}

std::string find_json_string(const std::string &json, const std::string &key, const std::string &default_value = "") {
  const std::string needle = "\"" + key + "\"";
  size_t pos = json.find(needle);
  if (pos == std::string::npos)
    return default_value;
  pos = json.find(':', pos + needle.size());
  if (pos == std::string::npos)
    return default_value;
  pos = json.find('"', pos + 1);
  if (pos == std::string::npos)
    return default_value;
  std::string out;
  bool escaped = false;
  for (size_t i = pos + 1; i < json.size(); i++) {
    char ch = json[i];
    if (escaped) {
      out += ch;
      escaped = false;
      continue;
    }
    if (ch == '\\') {
      escaped = true;
      continue;
    }
    if (ch == '"')
      return out;
    out += ch;
  }
  return default_value;
}

bool find_json_bool(const std::string &json, const std::string &key, bool default_value = false) {
  const std::string needle = "\"" + key + "\"";
  size_t pos = json.find(needle);
  if (pos == std::string::npos)
    return default_value;
  pos = json.find(':', pos + needle.size());
  if (pos == std::string::npos)
    return default_value;
  pos++;
  while (pos < json.size() && std::isspace(static_cast<unsigned char>(json[pos])))
    pos++;
  if (json.compare(pos, 4, "true") == 0)
    return true;
  if (json.compare(pos, 5, "false") == 0)
    return false;
  return default_value;
}

int find_json_int(const std::string &json, const std::string &key, int default_value = 0) {
  const std::string needle = "\"" + key + "\"";
  size_t pos = json.find(needle);
  if (pos == std::string::npos)
    return default_value;
  pos = json.find(':', pos + needle.size());
  if (pos == std::string::npos)
    return default_value;
  pos++;
  while (pos < json.size() && std::isspace(static_cast<unsigned char>(json[pos])))
    pos++;
  char *end = nullptr;
  long value = std::strtol(json.c_str() + pos, &end, 10);
  return end == json.c_str() + pos ? default_value : static_cast<int>(value);
}

std::string endpoint_json(const VoipSimulator::EndpointState &endpoint, bool include_visible = false) {
  std::ostringstream out;
  out << "{\"state\":" << q(endpoint.state)
      << ",\"caller\":" << q(endpoint.caller)
      << ",\"destination\":" << q(endpoint.destination)
      << ",\"last_reason\":" << q(endpoint.last_reason);
  if (include_visible) {
    out << ",\"visible_contacts\":" << endpoint.visible_contacts
        << ",\"selected\":" << q(endpoint.selected);
  }
  out << "}";
  return out.str();
}

void ensure_parent_dir(const std::string &path) {
  std::filesystem::path p(path);
  if (!p.parent_path().empty())
    std::filesystem::create_directories(p.parent_path());
}

}  // namespace

void VoipSimulator::setup() {
  this->reset_state_();
  this->write_framebuffer_();
  this->running_ = true;
  this->server_thread_ = std::thread([this]() { this->server_main_(); });
  ESP_LOGCONFIG(TAG, "Phase 00V host simulator started on %s", this->socket_path_.c_str());
}

void VoipSimulator::loop() {}

void VoipSimulator::on_shutdown() {
  this->running_ = false;
  if (!this->socket_path_.empty()) {
    int client = ::socket(AF_UNIX, SOCK_STREAM, 0);
    if (client >= 0) {
      sockaddr_un addr{};
      addr.sun_family = AF_UNIX;
      std::snprintf(addr.sun_path, sizeof(addr.sun_path), "%s", this->socket_path_.c_str());
      ::connect(client, reinterpret_cast<sockaddr *>(&addr), sizeof(addr));
      ::close(client);
    }
  }
  if (this->server_thread_.joinable())
    this->server_thread_.join();
  if (!this->socket_path_.empty())
    ::unlink(this->socket_path_.c_str());
}

void VoipSimulator::dump_config() {
  ESP_LOGCONFIG(TAG, "VoIP Simulator:");
  ESP_LOGCONFIG(TAG, "  Source profile: %s", this->source_profile_.c_str());
  ESP_LOGCONFIG(TAG, "  Device profile: %s", this->device_profile_.c_str());
  ESP_LOGCONFIG(TAG, "  Socket path: %s", this->socket_path_.c_str());
  ESP_LOGCONFIG(TAG, "  Mic input: %s", this->microphone_input_path_.c_str());
  ESP_LOGCONFIG(TAG, "  Speaker output: %s", this->speaker_output_path_.c_str());
  ESP_LOGCONFIG(TAG, "  Framebuffer: %s", this->framebuffer_path_.c_str());
}

void VoipSimulator::reset_state_() {
  std::lock_guard<std::mutex> lock(this->mutex_);
  this->state_ = SimulatorState{};
  if (!this->speaker_output_path_.empty()) {
    std::error_code ec;
    std::filesystem::remove(this->speaker_output_path_, ec);
  }
}

void VoipSimulator::server_main_() {
  ensure_parent_dir(this->socket_path_);
  ::unlink(this->socket_path_.c_str());
  int server = ::socket(AF_UNIX, SOCK_STREAM, 0);
  if (server < 0) {
    ESP_LOGE(TAG, "socket(AF_UNIX) failed: %s", std::strerror(errno));
    return;
  }
  sockaddr_un addr{};
  addr.sun_family = AF_UNIX;
  std::snprintf(addr.sun_path, sizeof(addr.sun_path), "%s", this->socket_path_.c_str());
  if (::bind(server, reinterpret_cast<sockaddr *>(&addr), sizeof(addr)) < 0) {
    ESP_LOGE(TAG, "bind(%s) failed: %s", this->socket_path_.c_str(), std::strerror(errno));
    ::close(server);
    return;
  }
  ::listen(server, 8);
  while (this->running_) {
    int client = ::accept(server, nullptr, nullptr);
    if (client < 0) {
      if (errno == EINTR)
        continue;
      break;
    }
    char buffer[65536];
    ssize_t n = ::read(client, buffer, sizeof(buffer) - 1);
    if (n > 0) {
      buffer[n] = '\0';
      std::string response = this->handle_request_(std::string(buffer, static_cast<size_t>(n)));
      response += "\n";
      ::write(client, response.data(), response.size());
    }
    ::close(client);
    if (this->state_.shutdown)
      this->running_ = false;
  }
  ::close(server);
  ::unlink(this->socket_path_.c_str());
}

std::string VoipSimulator::handle_request_(const std::string &line) {
  const std::string method = find_json_string(line, "method");
  const size_t params_pos = line.find("\"params\"");
  const std::string params = params_pos == std::string::npos ? "{}" : line.substr(params_pos);
  std::string result = this->dispatch_(method, params);
  return "{\"jsonrpc\":\"2.0\",\"id\":1,\"result\":" + result + "}";
}

std::string VoipSimulator::dispatch_(const std::string &method, const std::string &params) {
  if (method == "get_snapshot")
    return this->snapshot_json_();
  if (method == "reset") {
    this->reset_state_();
    this->write_framebuffer_();
    return this->snapshot_json_();
  }
  if (method == "shutdown") {
    std::lock_guard<std::mutex> lock(this->mutex_);
    this->state_.shutdown = true;
    std::thread([]() {
      std::this_thread::sleep_for(std::chrono::milliseconds(50));
      std::exit(0);
    }).detach();
    return "{\"ok\":true,\"shutdown\":true}";
  }
  if (method == "press_button") {
    this->press_button_(find_json_string(params, "button"));
    return this->snapshot_json_();
  }
  if (method == "touch") {
    {
      std::lock_guard<std::mutex> lock(this->mutex_);
      this->state_.touch_last = find_json_string(params, "target");
    }
    return this->snapshot_json_();
  }
  if (method == "advance_time") {
    int duration = find_json_int(params, "duration_ms", 0);
    {
      std::lock_guard<std::mutex> lock(this->mutex_);
      this->state_.now_ms += static_cast<uint64_t>(std::max(0, duration));
    }
    return "{\"ok\":true,\"duration_ms\":" + std::to_string(duration) + "}";
  }
  if (method == "inject_fault") {
    {
      std::lock_guard<std::mutex> lock(this->mutex_);
      this->state_.sip_decline_reason = "fault:" + find_json_string(params, "name");
    }
    return this->snapshot_json_();
  }
  if (method == "inject_pcm") {
    {
      std::lock_guard<std::mutex> lock(this->mutex_);
      this->state_.audio_tx_ready = true;
      this->state_.audio_tx_frames++;
      this->write_audio_marker_("inject_pcm");
    }
    return this->snapshot_json_();
  }
  if (method == "set_network_condition")
    return "{\"ok\":true}";
  if (method == "inject_event") {
    this->inject_event_(params);
    return this->snapshot_json_();
  }
  return "{\"ok\":false,\"error\":" + q("unknown method: " + method) + "}";
}

std::string VoipSimulator::snapshot_json_() const {
  std::lock_guard<std::mutex> lock(this->mutex_);
  std::ostringstream out;
  out << "{";
  out << "\"esp\":" << endpoint_json(this->state_.esp, true) << ",";
  out << "\"caller\":" << endpoint_json(this->state_.caller) << ",";
  out << "\"callee\":" << endpoint_json(this->state_.callee) << ",";
  out << "\"second\":" << endpoint_json(this->state_.second) << ",";
  out << "\"softphone\":" << endpoint_json(this->state_.softphone) << ",";
  out << "\"bridge\":{\"state\":" << q(this->state_.bridge_state)
      << ",\"left\":" << q(this->state_.bridge_left)
      << ",\"right\":" << q(this->state_.bridge_right) << "},";
  out << "\"audio\":{\"tx_ready\":" << b(this->state_.audio_tx_ready)
      << ",\"rx_ready\":" << b(this->state_.audio_rx_ready)
      << ",\"owner\":" << q(this->state_.audio_owner)
      << ",\"tx_frames\":" << this->state_.audio_tx_frames
      << ",\"rx_frames\":" << this->state_.audio_rx_frames
      << ",\"browser_tx_ready_latency_ms\":" << this->state_.browser_tx_ready_latency_ms
      << ",\"mic_input_path\":" << q(this->microphone_input_path_)
      << ",\"mic_input_bytes\":" << file_size_or_zero(this->microphone_input_path_)
      << ",\"speaker_output_path\":" << q(this->speaker_output_path_)
      << ",\"speaker_output_bytes\":" << file_size_or_zero(this->speaker_output_path_) << "},";
  out << "\"sip\":{\"last_status\":" << this->state_.sip_last_status
      << ",\"decline_reason\":" << q(this->state_.sip_decline_reason)
      << ",\"auth_reason\":" << q(this->state_.sip_auth_reason) << "},";
  out << "\"led\":{\"color\":" << q(this->state_.led_color)
      << ",\"effect\":" << (this->state_.led_effect.empty() ? "null" : q(this->state_.led_effect))
      << ",\"forbidden_effect\":\"Spin\"},";
  out << "\"media\":{\"state\":" << q(this->state_.media_state) << "},";
  out << "\"voip\":{\"state\":" << q(this->state_.voip_state)
      << ",\"caller\":" << q(this->state_.voip_caller) << "},";
  out << "\"voice_assistant\":{\"state\":" << q(this->state_.voice_assistant_state)
      << ",\"phase\":" << q(this->state_.voice_assistant_phase)
      << ",\"wake_word\":" << q(this->state_.wake_word)
      << ",\"events\":" << this->state_.voice_assistant_events << "},";
  out << "\"aec\":{\"frames\":" << this->state_.aec_frames
      << ",\"last_processing_us\":" << this->state_.aec_last_processing_us
      << ",\"max_processing_us\":" << this->state_.aec_max_processing_us << "},";
  out << "\"afe\":{\"frames\":" << this->state_.afe_frames
      << ",\"last_latency_us\":" << this->state_.afe_last_latency_us
      << ",\"max_latency_us\":" << this->state_.afe_max_latency_us << "},";
  out << "\"display\":{\"page\":" << q(this->state_.display_page)
      << ",\"status\":" << q(this->state_.display_status)
      << ",\"backlight_on\":" << b(this->state_.backlight_on)
      << ",\"framebuffer\":" << q(this->framebuffer_path_) << "},";
  out << "\"touch\":{\"last\":" << q(this->state_.touch_last) << "},";
  out << "\"controls\":{\"mic_muted\":" << b(this->state_.mic_muted)
      << ",\"speaker_muted\":" << b(this->state_.speaker_muted)
      << ",\"auto_answer\":" << b(this->state_.opt_esp_auto_answer)
      << ",\"dnd\":" << b(this->state_.opt_esp_dnd) << "},";
  out << "\"card\":{\"mode\":" << q(this->state_.card_mode)
      << ",\"controlled_device\":" << q(this->state_.card_controlled_device)
      << ",\"rendered_state\":" << q(this->state_.card_rendered_state)
      << ",\"source\":" << q(this->state_.card_source) << "},";
  out << "\"backend\":{\"browser_audio\":" << b(this->state_.backend_browser_audio) << "},";
  out << "\"phonebook\":{\"revision\":" << this->state_.phonebook_revision
      << ",\"duplicate_ids\":" << b(this->state_.phonebook_duplicate_ids) << "},";
  out << "\"ha\":{\"visible_contacts\":" << this->state_.ha_visible_contacts << "},";
  out << "\"framebuffer\":{\"path\":" << q(this->framebuffer_path_) << "},";
  out << "\"runtime\":{\"now_ms\":" << this->state_.now_ms
      << ",\"source_profile\":" << q(this->source_profile_)
      << ",\"device_profile\":" << q(this->device_profile_) << "}";
  out << "}";
  return out.str();
}

void VoipSimulator::write_framebuffer_() const {
  static const unsigned char png_1x1[] = {
      0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a, 0x00, 0x00, 0x00, 0x0d, 0x49, 0x48, 0x44, 0x52,
      0x00, 0x00, 0x00, 0x01, 0x00, 0x00, 0x00, 0x01, 0x08, 0x02, 0x00, 0x00, 0x00, 0x90, 0x77, 0x53,
      0xde, 0x00, 0x00, 0x00, 0x0c, 0x49, 0x44, 0x41, 0x54, 0x08, 0xd7, 0x63, 0x60, 0xf8, 0xcf, 0x00,
      0x00, 0x03, 0x01, 0x01, 0x00, 0x18, 0xdd, 0x8d, 0xb0, 0x00, 0x00, 0x00, 0x00, 0x49, 0x45, 0x4e,
      0x44, 0xae, 0x42, 0x60, 0x82};
  ensure_parent_dir(this->framebuffer_path_);
  std::ofstream out(this->framebuffer_path_, std::ios::binary);
  out.write(reinterpret_cast<const char *>(png_1x1), sizeof(png_1x1));
}

void VoipSimulator::write_audio_marker_(const std::string &label) const {
  ensure_parent_dir(this->speaker_output_path_);
  std::ofstream out(this->speaker_output_path_, std::ios::binary | std::ios::app);
  out << "voip_simulator:" << label << "\n";
}

void VoipSimulator::press_button_(const std::string &button) {
  std::lock_guard<std::mutex> lock(this->mutex_);
  if (button == "call") {
    this->state_.esp.state = "calling";
    this->state_.backend_browser_audio = false;
  } else if (button == "answer") {
    if (this->state_.softphone.state == "ringing") {
      if (!this->state_.audio_tx_ready) {
        this->state_.softphone.state = "ringing";
        this->state_.sip_last_status = 0;
        this->state_.browser_tx_ready_latency_ms = -1;
        this->state_.ha_answer_pending = true;
      } else {
        this->state_.softphone.state = "in_call";
        this->state_.audio_rx_ready = true;
        this->state_.audio_owner = "voip";
        this->state_.sip_last_status = 200;
        this->state_.browser_tx_ready_latency_ms = 0;
        this->state_.ha_answer_pending = false;
      }
    }
    if (this->state_.esp.state == "ringing") {
      this->state_.esp.state = "in_call";
      this->state_.audio_tx_ready = true;
      this->state_.audio_rx_ready = true;
      this->state_.audio_owner = "voip";
      this->state_.led_color = "green";
      this->state_.led_effect.clear();
    }
  }
}

void VoipSimulator::inject_event_(const std::string &params) {
  const std::string type = find_json_string(params, "type");
  std::lock_guard<std::mutex> lock(this->mutex_);
  if (type == "set_option") {
    const std::string target = find_json_string(params, "target", "esp");
    const std::string option = find_json_string(params, "option");
    const bool value = find_json_bool(params, "value");
    if (target == "esp" && option == "dnd")
      this->state_.opt_esp_dnd = value;
    else if (target == "esp" && option == "auto_answer")
      this->state_.opt_esp_auto_answer = value;
    else if (target == "caller" && option == "sip_bridge")
      this->state_.opt_caller_sip_bridge = value;
  } else if (type == "ha_call") {
    this->ha_call_(find_json_string(params, "target"), find_json_string(params, "caller", "Casa"));
  } else if (type == "esp_call") {
    this->esp_call_(find_json_string(params, "source"), find_json_string(params, "destination"),
                    find_json_string(params, "route", "direct"));
  } else if (type == "sip_invite") {
    this->sip_invite_(find_json_string(params, "caller"), find_json_string(params, "callee"),
                      find_json_string(params, "call_id"));
  } else if (type == "sip_offer") {
    std::string codecs = params;
    std::transform(codecs.begin(), codecs.end(), codecs.begin(), [](unsigned char c) { return std::toupper(c); });
    if (codecs.find("L16") == std::string::npos && codecs.find("L24") == std::string::npos) {
      this->state_.sip_last_status = 488;
      this->state_.sip_decline_reason = "media_incompatible";
      this->state_.softphone.state = "idle";
      this->state_.softphone.last_reason = "media_incompatible";
      this->state_.led_effect.clear();
    } else {
      this->sip_invite_(find_json_string(params, "caller"), find_json_string(params, "callee"),
                        find_json_string(params, "call_id"));
    }
  } else if (type == "sip_auth_challenge") {
    int status = find_json_int(params, "status", 401);
    this->state_.sip_last_status = status;
    this->state_.sip_auth_reason = status == 407 ? "proxy_auth_required_unsupported" : "auth_required_unsupported";
    this->state_.caller.state = "idle";
    this->state_.caller.last_reason = this->state_.sip_auth_reason;
    this->state_.softphone.state = "idle";
    this->state_.softphone.last_reason = this->state_.sip_auth_reason;
  } else if (type == "browser_audio_ready") {
    this->state_.audio_tx_ready = true;
    this->state_.audio_rx_ready = true;
    this->state_.audio_tx_frames++;
    this->state_.browser_tx_ready_latency_ms = 0;
    if (this->state_.ha_answer_pending && this->state_.softphone.state == "ringing") {
      this->state_.softphone.state = "in_call";
      this->state_.audio_owner = "voip";
      this->state_.sip_last_status = 200;
      this->state_.ha_answer_pending = false;
    }
    this->write_audio_marker_("browser_audio_ready");
  } else if (type == "browser_audio_delayed") {
    int delay = find_json_int(params, "delay_ms", 5000);
    this->state_.audio_tx_ready = true;
    this->state_.audio_rx_ready = true;
    this->state_.audio_tx_frames++;
    this->state_.browser_tx_ready_latency_ms = delay;
    if (this->state_.ha_answer_pending && this->state_.softphone.state == "ringing") {
      this->state_.softphone.state = "in_call";
      this->state_.audio_owner = "voip";
      this->state_.sip_last_status = 200;
      this->state_.ha_answer_pending = false;
    }
    this->write_audio_marker_("browser_audio_delayed");
  } else if (type == "mww_detected") {
    this->state_.wake_word = find_json_string(params, "wake_word", "okay_nabu");
    this->state_.voice_assistant_state = "running";
    this->state_.voice_assistant_phase = "wake";
    this->state_.display_page = "voice_assistant";
    this->state_.display_status = "va_wake";
    this->state_.voice_assistant_events++;
  } else if (type == "va_start") {
    this->state_.voice_assistant_state = "running";
    this->state_.voice_assistant_phase = "starting";
    this->state_.display_page = "voice_assistant";
    this->state_.display_status = "va_starting";
    this->state_.voice_assistant_events++;
  } else if (type == "va_listening") {
    this->state_.voice_assistant_state = "running";
    this->state_.voice_assistant_phase = "listening";
    this->state_.display_page = "voice_assistant";
    this->state_.display_status = "va_listening";
    this->state_.voice_assistant_events++;
  } else if (type == "va_thinking") {
    this->state_.voice_assistant_state = "running";
    this->state_.voice_assistant_phase = "thinking";
    this->state_.display_page = "voice_assistant";
    this->state_.display_status = "va_thinking";
    this->state_.voice_assistant_events++;
  } else if (type == "va_responding") {
    this->state_.voice_assistant_state = "running";
    this->state_.voice_assistant_phase = "responding";
    this->state_.media_state = "playing";
    this->state_.display_page = "voice_assistant";
    this->state_.display_status = "va_responding";
    this->state_.voice_assistant_events++;
  } else if (type == "va_end") {
    this->state_.voice_assistant_state = "idle";
    this->state_.voice_assistant_phase = "idle";
    this->state_.display_page = "idle";
    this->state_.display_status = "idle";
    this->state_.media_state = "idle";
    this->state_.audio_owner = "none";
    this->state_.wake_word.clear();
    this->state_.voice_assistant_events++;
  } else if (type == "va_error") {
    this->state_.voice_assistant_state = "idle";
    this->state_.voice_assistant_phase = "error";
    this->state_.display_page = "voice_assistant";
    this->state_.display_status = "va_error";
    this->state_.media_state = "idle";
    this->state_.voice_assistant_events++;
  } else if (type == "set_control") {
    const std::string control = find_json_string(params, "control");
    const bool value = find_json_bool(params, "value");
    if (control == "mic_muted")
      this->state_.mic_muted = value;
    else if (control == "speaker_muted")
      this->state_.speaker_muted = value;
    else if (control == "backlight")
      this->state_.backlight_on = value;
    else if (control == "auto_answer")
      this->state_.opt_esp_auto_answer = value;
    else if (control == "dnd")
      this->state_.opt_esp_dnd = value;
  } else if (type == "display_page") {
    this->state_.display_page = find_json_string(params, "page", this->state_.display_page);
    this->state_.display_status = find_json_string(params, "status", this->state_.display_status);
  } else if (type == "aec_frame") {
    int processing_us = find_json_int(params, "processing_us", 0);
    this->state_.aec_frames++;
    this->state_.aec_last_processing_us = processing_us;
    this->state_.aec_max_processing_us = std::max(this->state_.aec_max_processing_us, processing_us);
  } else if (type == "afe_frame") {
    int latency_us = find_json_int(params, "latency_us", 0);
    this->state_.afe_frames++;
    this->state_.afe_last_latency_us = latency_us;
    this->state_.afe_max_latency_us = std::max(this->state_.afe_max_latency_us, latency_us);
  } else if (type == "esp_bye" || type == "remote_bye") {
    this->state_.softphone.state = "idle";
    this->state_.softphone.last_reason = "remote_hangup";
    this->state_.card_rendered_state = "idle";
  } else if (type == "remote_cancel") {
    this->state_.softphone.state = "idle";
    this->state_.softphone.last_reason = "cancelled";
    this->state_.card_rendered_state = "idle";
  } else if (type == "ha_bye" || type == "ha_cancel" || type == "late_media_after_terminal") {
    this->state_.esp.state = "idle";
    this->state_.voip_state = "idle";
    this->state_.led_effect.clear();
  } else if (type == "ha_softphone_decline") {
    const std::string reason = find_json_string(params, "reason", "declined");
    this->state_.esp.state = "idle";
    this->state_.esp.last_reason = reason;
    this->state_.softphone.state = "idle";
    this->state_.softphone.last_reason = reason;
  } else if (type == "callee_decline") {
    const std::string reason = find_json_string(params, "reason", "declined");
    this->state_.caller.state = "idle";
    this->state_.caller.last_reason = reason;
    this->state_.callee.state = "idle";
    this->state_.callee.last_reason = reason;
    this->state_.bridge_state = "idle";
  } else if (type == "media_start") {
    this->state_.media_state = "playing";
    this->state_.voip_state = "idle";
  } else if (type == "phonebook_push") {
    this->state_.phonebook_revision++;
    this->state_.phonebook_duplicate_ids = false;
    int contacts = 0;
    size_t pos = 0;
    while ((pos = params.find("\"kind\"", pos)) != std::string::npos) {
      contacts++;
      pos += 6;
    }
    int visible = std::max(0, contacts - 1);
    this->state_.ha_visible_contacts = visible;
    this->state_.esp.visible_contacts = visible;
    this->state_.esp.selected = contacts > 0 ? "Casa" : "";
  } else if (type == "card_select") {
    this->state_.card_mode = find_json_string(params, "card");
    this->state_.card_controlled_device = find_json_string(params, "target");
  } else if (type == "esp_state") {
    this->state_.card_rendered_state = find_json_string(params, "state");
    this->state_.card_source = "esp_snapshot";
    this->state_.esp.state = find_json_string(params, "state");
    this->state_.esp.caller = find_json_string(params, "caller");
  }
  this->write_framebuffer_();
}

void VoipSimulator::ha_call_(const std::string &target, const std::string &caller) {
  if (this->state_.opt_esp_dnd) {
    this->state_.esp.state = "idle";
    this->state_.esp.last_reason = "DND";
    this->state_.sip_last_status = 486;
    this->state_.sip_decline_reason = "DND";
    return;
  }
  if (this->state_.opt_esp_auto_answer) {
    this->state_.esp.state = "in_call";
    this->state_.esp.caller = caller;
    this->state_.audio_owner = "voip";
    this->state_.led_color = "green";
    this->state_.led_effect.clear();
    return;
  }
  this->state_.esp.state = "ringing";
  this->state_.esp.caller = caller;
  this->state_.esp.destination = target;
  this->state_.led_color = "red";
  this->state_.led_effect = "Ringing";
}

void VoipSimulator::esp_call_(const std::string &source, const std::string &destination, const std::string &route) {
  if (destination == "Casa") {
    if (this->state_.softphone.state == "ringing" || this->state_.softphone.state == "in_call") {
      this->state_.second.state = "idle";
      this->state_.second.last_reason = "busy";
      this->state_.sip_last_status = 486;
      return;
    }
    this->state_.esp.state = "calling";
    this->state_.esp.destination = "Casa";
    this->state_.softphone.state = "ringing";
    this->state_.softphone.caller = source;
    this->state_.card_mode = "ha_softphone";
    this->state_.card_rendered_state = "ringing";
    this->state_.card_source = "ha_softphone_snapshot";
    return;
  }
  if (destination == "Virtual S3" && (this->state_.esp.state == "ringing" || this->state_.esp.state == "in_call")) {
    this->state_.second.state = "idle";
    this->state_.second.last_reason = "busy";
    this->state_.bridge_state = "idle";
    return;
  }
  if (route == "bridge" || this->state_.opt_caller_sip_bridge) {
    this->state_.bridge_state = "ringing";
    this->state_.bridge_left = source;
    this->state_.bridge_right = destination;
  }
  this->state_.caller.state = "calling";
  this->state_.callee.state = "ringing";
  this->state_.callee.caller = source;
}

void VoipSimulator::sip_invite_(const std::string &caller, const std::string &callee, const std::string &call_id) {
  if (callee == "Casa") {
    this->state_.softphone.state = "ringing";
    this->state_.softphone.caller = caller;
    this->state_.card_mode = "ha_softphone";
    this->state_.card_rendered_state = "ringing";
    this->state_.card_source = "ha_softphone_snapshot";
  } else {
    this->state_.voip_state = "ringing";
    this->state_.voip_caller = caller;
    this->state_.media_state = "paused";
    this->state_.audio_owner = "voip";
    this->state_.voice_assistant_state = "idle";
    this->state_.voice_assistant_phase = "idle";
    this->state_.display_page = "voip";
    this->state_.display_status = "voip_ringing";
  }
  (void) call_id;
}

}  // namespace voip_simulator
}  // namespace esphome
