#include "sip_transport.h"

#if defined(USE_ESP32) && defined(USE_INTERCOM_SIP_TRANSPORT)

#include <cerrno>
#include <algorithm>
#include <cctype>
#include <cstdlib>
#include <cstring>
#include <fcntl.h>
#include <cstdio>
#include <sys/select.h>
#include <sys/socket.h>
#include <netinet/tcp.h>

#include <esp_system.h>

#include "esphome/core/hal.h"
#include "esphome/core/log.h"
#include "esphome/components/network/util.h"
#include "../audio_processor/task_utils.h"
#include "net_utils.h"

namespace esphome {
namespace intercom_api {

static const char *const TAG = "intercom_api.sip";

namespace {

std::string trim_copy(const std::string &s) {
  size_t begin = 0;
  while (begin < s.size() && (s[begin] == ' ' || s[begin] == '\t' || s[begin] == '\r' || s[begin] == '\n')) begin++;
  size_t end = s.size();
  while (end > begin && (s[end - 1] == ' ' || s[end - 1] == '\t' || s[end - 1] == '\r' || s[end - 1] == '\n')) end--;
  return s.substr(begin, end - begin);
}

std::string sanitize_user(const std::string &raw) {
  std::string out;
  for (char ch : raw) {
    if (std::isalnum(static_cast<unsigned char>(ch)) || ch == '_' || ch == '-' || ch == '.') {
      out.push_back(ch);
    } else if (ch == ' ') {
      out.push_back('_');
    }
  }
  if (out.empty()) out = "intercom";
  return out;
}

std::string header_value(const std::string &msg, const char *name) {
  const std::string needle = std::string(name) + ":";
  size_t pos = 0;
  while (pos < msg.size()) {
    const size_t end = msg.find("\r\n", pos);
    const size_t line_end = end == std::string::npos ? msg.size() : end;
    const std::string line = msg.substr(pos, line_end - pos);
    if (line.size() >= needle.size()) {
      bool match = true;
      for (size_t i = 0; i < needle.size(); i++) {
        if (std::tolower(static_cast<unsigned char>(line[i])) !=
            std::tolower(static_cast<unsigned char>(needle[i]))) {
          match = false;
          break;
        }
      }
      if (match) return trim_copy(line.substr(needle.size()));
    }
    if (end == std::string::npos) break;
    pos = end + 2;
  }
  return "";
}

std::string message_body(const std::string &msg) {
  const size_t sep = msg.find("\r\n\r\n");
  if (sep == std::string::npos) return "";
  return msg.substr(sep + 4);
}

size_t sip_content_length(const std::string &msg) {
  const size_t sep = msg.find("\r\n\r\n");
  const size_t header_end = sep == std::string::npos ? msg.size() : sep;
  size_t pos = 0;
  while (pos < header_end) {
    const size_t end = msg.find("\r\n", pos);
    const size_t line_end = end == std::string::npos ? header_end : std::min(end, header_end);
    const std::string line = msg.substr(pos, line_end - pos);
    const size_t colon = line.find(':');
    if (colon != std::string::npos) {
      std::string key = line.substr(0, colon);
      for (char &ch : key) ch = static_cast<char>(std::tolower(static_cast<unsigned char>(ch)));
      if (trim_copy(key) == "content-length") {
        return static_cast<size_t>(std::strtoul(trim_copy(line.substr(colon + 1)).c_str(), nullptr, 10));
      }
    }
    if (end == std::string::npos || end >= header_end) break;
    pos = end + 2;
  }
  return 0;
}

std::string sip_header_token(const std::string &raw) {
  std::string out;
  for (char ch : raw) {
    if (ch == '\r' || ch == '\n') continue;
    if (std::isalnum(static_cast<unsigned char>(ch)) ||
        ch == '_' || ch == '-' || ch == '.' || ch == ' ') {
      out.push_back(ch);
    }
  }
  return trim_copy(out);
}

std::string sip_quoted(const std::string &raw) {
  std::string out = "\"";
  for (char ch : raw) {
    if (ch == '\r' || ch == '\n') continue;
    if (ch == '"' || ch == '\\') out.push_back('\\');
    out.push_back(ch);
  }
  out.push_back('"');
  return out;
}

std::string reason_text_from_header(const std::string &value) {
  const size_t key = value.find("text=");
  if (key == std::string::npos) return "";
  size_t begin = key + 5;
  if (begin >= value.size()) return "";
  if (value[begin] != '"') return sip_header_token(value.substr(begin));
  begin++;
  std::string out;
  bool escaped = false;
  for (size_t i = begin; i < value.size(); i++) {
    const char ch = value[i];
    if (escaped) {
      out.push_back(ch);
      escaped = false;
      continue;
    }
    if (ch == '\\') {
      escaped = true;
      continue;
    }
    if (ch == '"') break;
    out.push_back(ch);
  }
  return sip_header_token(out);
}

std::string cseq_method(const std::string &cseq) {
  const std::string trimmed = trim_copy(cseq);
  const size_t space = trimmed.find_last_of(" \t");
  if (space == std::string::npos || space + 1 >= trimmed.size()) return "";
  return trim_copy(trimmed.substr(space + 1));
}

bool sip_method_known_(const std::string &method) {
  return method == "INVITE" || method == "ACK" || method == "CANCEL" ||
         method == "BYE" || method == "OPTIONS" || method == "REGISTER";
}

std::string sip_failure_reason_(int status) {
  if (status == 401) return "auth_required_unsupported";
  if (status == 407) return "proxy_auth_required_unsupported";
  if (status == 486) return "busy";
  if (status == 487) return "cancelled";
  if (status == 488) return "media_incompatible";
  if (status == 603) return "declined";
  return "sip_" + std::to_string(status);
}

std::string tag_from_header(const std::string &value) {
  const size_t tag = value.find("tag=");
  if (tag == std::string::npos) return "";
  size_t begin = tag + 4;
  size_t end = begin;
  while (end < value.size() && value[end] != ';' && value[end] != ' ' && value[end] != '\r') end++;
  return value.substr(begin, end - begin);
}

std::string strip_angle_uri(const std::string &value) {
  std::string out = trim_copy(value);
  if (out.size() >= 2 && out.front() == '<' && out.back() == '>') {
    out = out.substr(1, out.size() - 2);
  }
  return out;
}

std::string sip_user_from_header(const std::string &value) {
  std::string uri;
  const size_t left = value.find('<');
  const size_t right = left == std::string::npos ? std::string::npos : value.find('>', left + 1);
  if (left != std::string::npos && right != std::string::npos && right > left + 1) {
    uri = value.substr(left + 1, right - left - 1);
  } else {
    uri = trim_copy(value);
    const size_t semicolon = uri.find(';');
    if (semicolon != std::string::npos) uri = uri.substr(0, semicolon);
  }
  uri = trim_copy(uri);
  const char *prefix = "sip:";
  if (uri.rfind(prefix, 0) == 0) uri = uri.substr(4);
  const size_t at = uri.find('@');
  if (at == std::string::npos || at == 0) return "";
  return sip_header_token(uri.substr(0, at));
}

std::string make_token(const char *prefix) {
  char buf[40];
  snprintf(buf, sizeof(buf), "%s%08x%08x", prefix,
           static_cast<unsigned>(esp_random()),
           static_cast<unsigned>(millis()));
  return buf;
}

const char *rtp_encoding(const AudioFormat &fmt) {
  if (fmt.channels != 1) return nullptr;
  if (fmt.nominal_frame_bytes() > UDP_SAFE_AUDIO_PAYLOAD_BYTES) return nullptr;
  if (fmt.pcm_format == PcmFormat::S16LE) return "L16";
  if (fmt.pcm_format == PcmFormat::S24LE || fmt.pcm_format == PcmFormat::S24LE_IN_S32) return "L24";
  return nullptr;
}

bool parse_rtpmap_format(const std::string &line, AudioFormat *fmt, uint8_t *payload_type) {
  // a=rtpmap:96 L16/16000/1
  const size_t colon = line.find(':');
  const size_t space = line.find(' ', colon == std::string::npos ? 0 : colon + 1);
  if (colon == std::string::npos || space == std::string::npos) return false;
  const int pt = std::atoi(line.substr(colon + 1, space - colon - 1).c_str());
  if (pt < 0 || pt > 127) return false;
  const std::string spec = trim_copy(line.substr(space + 1));
  const size_t slash1 = spec.find('/');
  const size_t slash2 = slash1 == std::string::npos ? std::string::npos : spec.find('/', slash1 + 1);
  if (slash1 == std::string::npos) return false;
  const std::string enc = spec.substr(0, slash1);
  const uint32_t rate = static_cast<uint32_t>(std::strtoul(spec.substr(slash1 + 1, slash2 - slash1 - 1).c_str(), nullptr, 10));
  const uint8_t channels = slash2 == std::string::npos ? 1 : static_cast<uint8_t>(std::strtoul(spec.substr(slash2 + 1).c_str(), nullptr, 10));
  AudioFormat candidate;
  candidate.sample_rate = rate;
  candidate.channels = channels;
  candidate.frame_ms = 20;
  if (enc == "L16" || enc == "l16") {
    candidate.pcm_format = PcmFormat::S16LE;
  } else if (enc == "L24" || enc == "l24") {
    candidate.pcm_format = PcmFormat::S24LE;
  } else {
    return false;
  }
  if (!candidate.is_valid()) return false;
  *fmt = candidate;
  *payload_type = static_cast<uint8_t>(pt);
  return true;
}

std::string decline_reason_from_payload(const uint8_t *payload, size_t len) {
  if (payload == nullptr || len == 0) return "";
  std::string call_id;
  size_t off = decode_call_id_prefix(payload, len, &call_id);
  if (off == 0 || off >= len) return "";
  std::string reason;
  const size_t n = decode_lp_string(payload + off, len - off, &reason);
  if (n == 0) return "";
  return sip_header_token(reason);
}

}  // namespace

SipTransport::SipTransport(uint16_t sip_port, uint16_t rtp_port, std::string remote_host,
                           bool task_stacks_in_psram)
    : sip_port_(sip_port), rtp_port_(rtp_port), task_stacks_in_psram_(task_stacks_in_psram) {
  audio_format_list_legacy(&this->offer_tx_formats_);
  audio_format_list_legacy(&this->offer_rx_formats_);
  this->rtp_ssrc_ = esp_random();
  this->parse_remote_(remote_host);
}

SipTransport::~SipTransport() { this->stop(); }

void SipTransport::set_audio_formats(const AudioFormatList &tx, const AudioFormatList &rx) {
  this->offer_tx_formats_ = tx;
  this->offer_rx_formats_ = rx;
  if (this->offer_tx_formats_.count == 0) audio_format_list_legacy(&this->offer_tx_formats_);
  if (this->offer_rx_formats_.count == 0) audio_format_list_legacy(&this->offer_rx_formats_);
  ESP_LOGI(TAG, "SIP media capabilities: tx=%u rx=%u",
           (unsigned) this->offer_tx_formats_.count,
           (unsigned) this->offer_rx_formats_.count);
}

bool SipTransport::parse_remote_(const std::string &host) {
  if (host.empty()) return false;
  struct in_addr a{};
  if (inet_aton(host.c_str(), &a) == 0) return false;
  this->remote_ip_v4_.store(ntohl(a.s_addr), std::memory_order_release);
  return true;
}

bool SipTransport::bind_udp_(int *fd, uint16_t port, const char *label) {
  *fd = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
  if (*fd < 0) {
    const int err = errno;
    ESP_LOGE(TAG, "Failed to create %s socket: %s (%d: %s)",
             label, socket_errno_name(err), err, socket_errno_text(err));
    return false;
  }
  int flags = fcntl(*fd, F_GETFL, 0);
  fcntl(*fd, F_SETFL, flags | O_NONBLOCK);
  struct sockaddr_in addr{};
  addr.sin_family = AF_INET;
  addr.sin_addr.s_addr = INADDR_ANY;
  addr.sin_port = htons(port);
  if (bind(*fd, reinterpret_cast<struct sockaddr *>(&addr), sizeof(addr)) < 0) {
    const int err = errno;
    ESP_LOGE(TAG, "%s bind on UDP/%u failed: %s (%d: %s)",
             label, (unsigned) port, socket_errno_name(err), err, socket_errno_text(err));
    close(*fd);
    *fd = -1;
    return false;
  }
  return true;
}

bool SipTransport::bind_tcp_(int *fd, uint16_t port, const char *label) {
  *fd = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
  if (*fd < 0) {
    const int err = errno;
    ESP_LOGE(TAG, "Failed to create %s TCP socket: %s (%d: %s)",
             label, socket_errno_name(err), err, socket_errno_text(err));
    return false;
  }
  int opt = 1;
  setsockopt(*fd, SOL_SOCKET, SO_REUSEADDR, &opt, sizeof(opt));
  setsockopt(*fd, IPPROTO_TCP, TCP_NODELAY, &opt, sizeof(opt));
  int flags = fcntl(*fd, F_GETFL, 0);
  fcntl(*fd, F_SETFL, flags | O_NONBLOCK);
  struct sockaddr_in addr{};
  addr.sin_family = AF_INET;
  addr.sin_addr.s_addr = INADDR_ANY;
  addr.sin_port = htons(port);
  if (bind(*fd, reinterpret_cast<struct sockaddr *>(&addr), sizeof(addr)) < 0) {
    const int err = errno;
    ESP_LOGE(TAG, "%s bind on TCP/%u failed: %s (%d: %s)",
             label, (unsigned) port, socket_errno_name(err), err, socket_errno_text(err));
    close(*fd);
    *fd = -1;
    return false;
  }
  if (listen(*fd, 2) < 0) {
    const int err = errno;
    ESP_LOGE(TAG, "%s listen on TCP/%u failed: %s (%d: %s)",
             label, (unsigned) port, socket_errno_name(err), err, socket_errno_text(err));
    close(*fd);
    *fd = -1;
    return false;
  }
  return true;
}

bool SipTransport::start() {
  if (this->running_.load(std::memory_order_acquire)) return true;
  if (!this->bind_udp_(&this->sip_socket_, this->sip_port_, "SIP")) return false;
  if (!this->bind_tcp_(&this->sip_tcp_listener_socket_, this->sip_port_, "SIP")) {
    close(this->sip_socket_);
    this->sip_socket_ = -1;
    return false;
  }
  this->running_.store(true, std::memory_order_release);
  if (!audio_processor::start_pinned_task(SipTransport::sip_task_trampoline_, "intercom_sip",
                                          kSipTaskStackBytes, this, 4, 1,
                                          this->task_stacks_in_psram_, TAG,
                                          &this->sip_task_handle_, &this->sip_task_tcb_,
                                          &this->sip_task_stack_)) {
    this->running_.store(false, std::memory_order_release);
    close(this->sip_socket_);
    this->sip_socket_ = -1;
    close(this->sip_tcp_listener_socket_);
    this->sip_tcp_listener_socket_ = -1;
    return false;
  }
  ESP_LOGI(TAG, "SIP listening on UDP+TCP/%u, RTP base UDP/%u", (unsigned) this->sip_port_, (unsigned) this->rtp_port_);
  this->emit_connection_change_(true);
  return true;
}

void SipTransport::stop() {
  this->stop_audio_path();
  if (!this->running_.exchange(false, std::memory_order_acq_rel)) return;
  if (this->sip_socket_ >= 0) {
    close(this->sip_socket_);
    this->sip_socket_ = -1;
  }
  if (this->sip_tcp_listener_socket_ >= 0) {
    close(this->sip_tcp_listener_socket_);
    this->sip_tcp_listener_socket_ = -1;
  }
  if (this->sip_tcp_client_socket_ >= 0) {
    close(this->sip_tcp_client_socket_);
    this->sip_tcp_client_socket_ = -1;
  }
  audio_processor::force_delete_pinned_task(&this->sip_task_handle_, &this->sip_task_stack_, kSipTaskStackBytes);
  this->emit_connection_change_(false);
}

bool SipTransport::is_connected() const {
  return this->running_.load(std::memory_order_acquire);
}

bool SipTransport::start_audio_path() {
  if (this->rtp_running_.load(std::memory_order_acquire)) return true;
  if (!this->bind_udp_(&this->rtp_socket_, this->rtp_port_, "RTP")) return false;
  this->rtp_running_.store(true, std::memory_order_release);
  if (!audio_processor::start_pinned_task(SipTransport::rtp_task_trampoline_, "intercom_rtp",
                                          kRtpTaskStackBytes, this, 5, 1,
                                          this->task_stacks_in_psram_, TAG,
                                          &this->rtp_task_handle_, &this->rtp_task_tcb_,
                                          &this->rtp_task_stack_)) {
    this->rtp_running_.store(false, std::memory_order_release);
    close(this->rtp_socket_);
    this->rtp_socket_ = -1;
    return false;
  }
  return true;
}

void SipTransport::stop_audio_path() {
  if (!this->rtp_running_.exchange(false, std::memory_order_acq_rel)) return;
  if (this->rtp_socket_ >= 0) {
    close(this->rtp_socket_);
    this->rtp_socket_ = -1;
  }
  audio_processor::force_delete_pinned_task(&this->rtp_task_handle_, &this->rtp_task_stack_, kRtpTaskStackBytes);
}

bool SipTransport::originate(const std::string &host, uint16_t port) {
  if (!this->parse_remote_(host)) return false;
  const uint16_t sip_port = port ? port : 5060;
  this->remote_sip_port_.store(sip_port, std::memory_order_release);
  if (!this->remote_sip_tcp_.load(std::memory_order_acquire)) {
    ESP_LOGI(TAG, "SIP UDP originate target set to %s:%u", host.c_str(), (unsigned) sip_port);
    return true;
  }

  if (this->sip_tcp_client_socket_ >= 0) {
    close(this->sip_tcp_client_socket_);
    this->sip_tcp_client_socket_ = -1;
  }

  const uint32_t ip_v4 = this->remote_ip_v4_.load(std::memory_order_acquire);
  if (ip_v4 == 0) return false;
  const int fd = socket(AF_INET, SOCK_STREAM, IPPROTO_TCP);
  if (fd < 0) {
    const int err = errno;
    ESP_LOGW(TAG, "SIP TCP socket create failed: %s (%d: %s)",
             socket_errno_name(err), err, socket_errno_text(err));
    return false;
  }

  int opt = 1;
  setsockopt(fd, IPPROTO_TCP, TCP_NODELAY, &opt, sizeof(opt));
  int flags = fcntl(fd, F_GETFL, 0);
  fcntl(fd, F_SETFL, flags | O_NONBLOCK);

  struct sockaddr_in dest{};
  dest.sin_family = AF_INET;
  dest.sin_addr.s_addr = htonl(ip_v4);
  dest.sin_port = htons(sip_port);
  int rc = connect(fd, reinterpret_cast<struct sockaddr *>(&dest), sizeof(dest));
  if (rc != 0 && errno == EINPROGRESS) {
    fd_set writefds;
    FD_ZERO(&writefds);
    FD_SET(fd, &writefds);
    struct timeval timeout{};
    timeout.tv_sec = 2;
    timeout.tv_usec = 0;
    rc = select(fd + 1, nullptr, &writefds, nullptr, &timeout);
    if (rc > 0) {
      int so_error = 0;
      socklen_t len = sizeof(so_error);
      if (getsockopt(fd, SOL_SOCKET, SO_ERROR, &so_error, &len) == 0) {
        errno = so_error;
        rc = so_error == 0 ? 0 : -1;
      } else {
        rc = -1;
      }
    } else {
      errno = ETIMEDOUT;
      rc = -1;
    }
  }
  if (rc != 0) {
    const int err = errno;
    close(fd);
    ESP_LOGW(TAG, "SIP TCP connect to %s:%u failed: %s (%d: %s)",
             host.c_str(), (unsigned) sip_port, socket_errno_name(err), err, socket_errno_text(err));
    return false;
  }

  this->sip_tcp_client_socket_ = fd;
  this->sip_tcp_rx_buffer_.clear();
  ESP_LOGI(TAG, "SIP TCP originate connected to %s:%u", host.c_str(), (unsigned) sip_port);
  return true;
}

void SipTransport::set_remote(const std::string &ip, uint16_t port, uint16_t control_port) {
  this->parse_remote_(ip);
  if (port) this->remote_sip_port_.store(port, std::memory_order_release);
  if (control_port) this->remote_rtp_port_.store(control_port, std::memory_order_release);
}

void SipTransport::set_sip_signaling_transport(bool tcp) {
  const bool was_tcp = this->remote_sip_tcp_.exchange(tcp, std::memory_order_acq_rel);
  if (!tcp && was_tcp && this->sip_tcp_client_socket_ >= 0) {
    close(this->sip_tcp_client_socket_);
    this->sip_tcp_client_socket_ = -1;
    this->sip_tcp_rx_buffer_.clear();
  }
}

void SipTransport::reset_dialog_() {
  this->call_id_.clear();
  this->local_tag_.clear();
  this->remote_tag_.clear();
  this->branch_.clear();
  this->local_uri_.clear();
  this->remote_uri_.clear();
  this->last_invite_via_.clear();
  this->last_invite_from_.clear();
  this->last_invite_to_.clear();
  this->last_invite_cseq_.clear();
  this->caller_route_.clear();
  this->caller_name_.clear();
  this->dest_route_.clear();
  this->dest_name_.clear();
  this->selected_tx_format_ = LEGACY_AUDIO_FORMAT;
  this->selected_rx_format_ = LEGACY_AUDIO_FORMAT;
  this->rtp_tx_payload_type_ = 96;
  this->rtp_rx_payload_type_ = 96;
  this->call_active_.store(false, std::memory_order_release);
  this->outgoing_invite_pending_.store(false, std::memory_order_release);
}

bool SipTransport::local_ip_for_peer_(uint32_t peer_ip_v4, std::string *out) const {
  int fd = socket(AF_INET, SOCK_DGRAM, IPPROTO_UDP);
  bool ok = false;
  if (fd >= 0) {
    struct sockaddr_in peer{};
    peer.sin_family = AF_INET;
    peer.sin_port = htons(9);
    peer.sin_addr.s_addr = htonl(peer_ip_v4);
    if (connect(fd, reinterpret_cast<struct sockaddr *>(&peer), sizeof(peer)) == 0) {
      struct sockaddr_in local{};
      socklen_t len = sizeof(local);
      if (getsockname(fd, reinterpret_cast<struct sockaddr *>(&local), &len) == 0 &&
          local.sin_addr.s_addr != 0) {
        char ip[16];
        inet_ntoa_r(local.sin_addr, ip, sizeof(ip));
        *out = ip;
        ok = true;
      }
    }
    close(fd);
  }
  if (ok) return true;
  char ip[network::IP_ADDRESS_BUFFER_SIZE];
  for (auto &address : network::get_ip_addresses()) {
    if (!address.is_ip4()) continue;
    address.str_to(ip);
    if (std::strcmp(ip, "0.0.0.0") != 0) {
      *out = ip;
      ESP_LOGW(TAG, "SIP local IP fallback selected %s for peer %08x", ip, (unsigned) peer_ip_v4);
      return true;
    }
  }
  return false;
}

bool SipTransport::send_sip_(const std::string &message, uint32_t ip_v4, uint16_t port) {
  if (this->remote_sip_tcp_.load(std::memory_order_acquire)) {
    return this->send_sip_tcp_(message);
  }
  if (this->sip_socket_ < 0 || ip_v4 == 0 || port == 0) return false;
  struct sockaddr_in dest{};
  dest.sin_family = AF_INET;
  dest.sin_addr.s_addr = htonl(ip_v4);
  dest.sin_port = htons(port);
  const int sent = sendto(this->sip_socket_, message.data(), message.size(), 0,
                          reinterpret_cast<struct sockaddr *>(&dest), sizeof(dest));
  if (sent != static_cast<int>(message.size())) {
    const int err = errno;
    ESP_LOGW(TAG, "SIP TX failed: %s (%d: %s)", socket_errno_name(err), err, socket_errno_text(err));
    return false;
  }
  char ip[16];
  struct in_addr a{};
  a.s_addr = htonl(ip_v4);
  inet_ntoa_r(a, ip, sizeof(ip));
  ESP_LOGI(TAG, "SIP TX %u bytes to %s:%u", (unsigned) message.size(), ip, (unsigned) port);
  return true;
}

bool SipTransport::send_sip_tcp_(const std::string &message) {
  const int socket = this->sip_tcp_client_socket_;
  if (socket < 0 || message.empty()) return false;
  size_t sent_total = 0;
  while (sent_total < message.size()) {
    const int sent = send(socket, message.data() + sent_total, message.size() - sent_total, 0);
    if (sent <= 0) {
      const int err = errno;
      ESP_LOGW(TAG, "SIP TCP TX failed: %s (%d: %s)", socket_errno_name(err), err, socket_errno_text(err));
      return false;
    }
    sent_total += static_cast<size_t>(sent);
  }
  ESP_LOGI(TAG, "SIP TCP TX %u bytes", (unsigned) message.size());
  return true;
}

std::string SipTransport::build_sdp_offer_() const {
  const uint32_t remote_ip = this->remote_ip_v4_.load(std::memory_order_acquire);
  std::string local_ip = "0.0.0.0";
  this->local_ip_for_peer_(remote_ip, &local_ip);
  std::string payloads;
  std::string maps;
  uint8_t pt = 96;
  uint8_t first_ptime = 20;
  auto append_format = [&](const AudioFormat &fmt) {
    if (pt >= 120) return;
    const char *enc = rtp_encoding(fmt);
    if (enc == nullptr) return;
    if (payloads.empty()) {
      first_ptime = fmt.frame_ms;
    } else if (fmt.frame_ms != first_ptime) {
      return;
    }
    if (!payloads.empty()) payloads.push_back(' ');
    payloads += std::to_string(pt);
    maps += "a=rtpmap:" + std::to_string(pt) + " " + enc + "/" +
            std::to_string(fmt.sample_rate) + "/" + std::to_string(fmt.channels) + "\r\n";
    pt++;
  };
  auto list_contains = [](const AudioFormatList &list, uint8_t limit, const AudioFormat &fmt) -> bool {
    for (uint8_t i = 0; i < limit; i++) {
      const AudioFormat &candidate = list.formats[i];
      if (candidate.sample_rate == fmt.sample_rate &&
          candidate.pcm_format == fmt.pcm_format &&
          candidate.channels == fmt.channels &&
          candidate.frame_ms == fmt.frame_ms) {
        return true;
      }
    }
    return false;
  };
  for (uint8_t i = 0; i < this->offer_tx_formats_.count && pt < 120; i++) {
    if (!list_contains(this->offer_rx_formats_, this->offer_rx_formats_.count,
                       this->offer_tx_formats_.formats[i])) {
      continue;
    }
    append_format(this->offer_tx_formats_.formats[i]);
  }
  if (payloads.empty()) {
    ESP_LOGW(TAG, "SIP SDP offer has no common UDP-safe RTP PCM format");
    return "";
  }
  return "v=0\r\n"
         "o=- 0 0 IN IP4 " + local_ip + "\r\n"
         "s=ESPHome Intercom\r\n"
         "c=IN IP4 " + local_ip + "\r\n"
         "t=0 0\r\n"
         "m=audio " + std::to_string(this->rtp_port_) + " RTP/AVP " + payloads + "\r\n" +
         maps +
         "a=ptime:" + std::to_string(first_ptime) + "\r\n"
         "a=maxptime:" + std::to_string(first_ptime) + "\r\n"
         "a=sendrecv\r\n";
}

std::string SipTransport::build_sdp_answer_() const {
  const uint32_t remote_ip = this->remote_ip_v4_.load(std::memory_order_acquire);
  std::string local_ip = "0.0.0.0";
  this->local_ip_for_peer_(remote_ip, &local_ip);
  const char *tx_enc = rtp_encoding(this->selected_tx_format_);
  const char *rx_enc = rtp_encoding(this->selected_rx_format_);
  if (tx_enc == nullptr) tx_enc = "L16";
  if (rx_enc == nullptr) rx_enc = "L16";
  const bool same_payload = this->rtp_tx_payload_type_ == this->rtp_rx_payload_type_;
  std::string payloads = std::to_string(this->rtp_tx_payload_type_);
  if (!same_payload) payloads += " " + std::to_string(this->rtp_rx_payload_type_);
  std::string maps;
  maps += "a=rtpmap:" + std::to_string(this->rtp_tx_payload_type_) + " " + tx_enc + "/" +
          std::to_string(this->selected_tx_format_.sample_rate) + "/" +
          std::to_string(this->selected_tx_format_.channels) + "\r\n";
  if (!same_payload) {
    maps += "a=rtpmap:" + std::to_string(this->rtp_rx_payload_type_) + " " + rx_enc + "/" +
            std::to_string(this->selected_rx_format_.sample_rate) + "/" +
            std::to_string(this->selected_rx_format_.channels) + "\r\n";
  }
  return "v=0\r\n"
         "o=- 0 0 IN IP4 " + local_ip + "\r\n"
         "s=ESPHome Intercom\r\n"
         "c=IN IP4 " + local_ip + "\r\n"
         "t=0 0\r\n"
         "m=audio " + std::to_string(this->rtp_port_) + " RTP/AVP " + payloads + "\r\n" +
         maps +
         "a=ptime:" + std::to_string(this->selected_tx_format_.frame_ms) + "\r\n"
         "a=maxptime:" + std::to_string(this->selected_tx_format_.frame_ms) + "\r\n"
         "a=sendrecv\r\n";
}

bool SipTransport::learn_remote_rtp_from_sdp_(const std::string &sdp, uint32_t fallback_ip) {
  uint16_t media_port = 0;
  uint32_t media_ip = fallback_ip;
  bool selected_tx = false;
  bool selected_rx = false;
  AudioFormat selected_tx_format;
  AudioFormat selected_rx_format;
  uint8_t selected_tx_payload_type = 0;
  uint8_t selected_rx_payload_type = 0;
  auto supports_wire_format = [](const AudioFormatList &list, const AudioFormat &remote, AudioFormat *local) -> bool {
    if (remote.nominal_frame_bytes() > UDP_SAFE_AUDIO_PAYLOAD_BYTES) return false;
    for (uint8_t i = 0; i < list.count; i++) {
      const AudioFormat &candidate = list.formats[i];
      if (candidate.sample_rate == remote.sample_rate &&
          candidate.pcm_format == remote.pcm_format &&
          candidate.channels == remote.channels &&
          candidate.nominal_frame_bytes() <= UDP_SAFE_AUDIO_PAYLOAD_BYTES) {
        if (local != nullptr) *local = candidate;
        return true;
      }
    }
    return false;
  };
  size_t pos = 0;
  while (pos < sdp.size()) {
    size_t end = sdp.find("\r\n", pos);
    if (end == std::string::npos) end = sdp.size();
    const std::string line = sdp.substr(pos, end - pos);
    if (line.rfind("c=IN IP4 ", 0) == 0) {
      struct in_addr a{};
      if (inet_aton(line.substr(9).c_str(), &a) != 0 && a.s_addr != 0) media_ip = ntohl(a.s_addr);
    } else if (line.rfind("m=audio ", 0) == 0) {
      media_port = static_cast<uint16_t>(std::strtoul(line.substr(8).c_str(), nullptr, 10));
    } else if (line.rfind("a=rtpmap:", 0) == 0) {
      AudioFormat fmt;
      uint8_t pt = 0;
      if (parse_rtpmap_format(line, &fmt, &pt)) {
        AudioFormat local_rx;
        AudioFormat local_tx;
        const bool tx_ok = supports_wire_format(this->offer_tx_formats_, fmt, &local_tx);
        const bool rx_ok = supports_wire_format(this->offer_rx_formats_, fmt, &local_rx);
        if (!selected_tx && !selected_rx && tx_ok && rx_ok) {
          selected_tx_format = local_tx;
          selected_rx_format = local_rx;
          selected_tx_payload_type = pt;
          selected_rx_payload_type = pt;
          selected_tx = true;
          selected_rx = true;
          ESP_LOGI(TAG, "SIP SDP selected TX PT=%u L%u/%u/%u frame=%ums",
                   (unsigned) pt,
                   fmt.pcm_format == PcmFormat::S24LE ? 24u : 16u,
                   (unsigned) selected_tx_format.sample_rate,
                   (unsigned) selected_tx_format.channels,
                   (unsigned) selected_tx_format.frame_ms);
          ESP_LOGI(TAG, "SIP SDP selected RX PT=%u L%u/%u/%u frame=%ums",
                   (unsigned) pt,
                   fmt.pcm_format == PcmFormat::S24LE ? 24u : 16u,
                   (unsigned) selected_rx_format.sample_rate,
                   (unsigned) selected_rx_format.channels,
                   (unsigned) selected_rx_format.frame_ms);
        } else if (!selected_tx && !selected_rx) {
          ESP_LOGD(TAG, "SIP SDP skipping unsupported PT=%u rate=%u pcm=%u channels=%u",
                   (unsigned) pt,
                   (unsigned) fmt.sample_rate,
                   (unsigned) fmt.pcm_format,
                   (unsigned) fmt.channels);
        }
      }
    }
    if (end == sdp.size()) break;
    pos = end + 2;
  }
  if (media_port == 0 || media_ip == 0 || !selected_tx || !selected_rx) {
    ESP_LOGW(TAG, "SIP SDP rejected: body_len=%u media_port=%u media_ip=%08x selected_tx=%s selected_rx=%s",
             (unsigned) sdp.size(), (unsigned) media_port, (unsigned) media_ip,
             selected_tx ? "yes" : "no", selected_rx ? "yes" : "no");
    return false;
  }
  this->selected_tx_format_ = selected_tx_format;
  this->selected_rx_format_ = selected_rx_format;
  this->rtp_tx_payload_type_ = selected_tx_payload_type;
  this->rtp_rx_payload_type_ = selected_rx_payload_type;
  this->remote_ip_v4_.store(media_ip, std::memory_order_release);
  this->remote_rtp_port_.store(media_port, std::memory_order_release);
  return true;
}

bool SipTransport::parse_start_payload_(const uint8_t *payload, size_t len) {
  size_t off = decode_call_id_prefix(payload, len, &this->call_id_);
  if (off == 0) return false;
  auto decode_field = [&](std::string *out) -> bool {
    size_t n = decode_lp_string(payload + off, len - off, out);
    if (n == 0) return false;
    off += n;
    return true;
  };
  if (!decode_field(&this->caller_route_) || !decode_field(&this->caller_name_) ||
      !decode_field(&this->dest_route_) || !decode_field(&this->dest_name_)) {
    return false;
  }
  audio_format_list_legacy(&this->offer_tx_formats_);
  audio_format_list_legacy(&this->offer_rx_formats_);
  if (off == len) return true;
  static const uint8_t START_V2_MAGIC[] = {'I', 'C', 'A', 'F', '2'};
  if (len < off + sizeof(START_V2_MAGIC) + 1 ||
      std::memcmp(payload + off, START_V2_MAGIC, sizeof(START_V2_MAGIC)) != 0) {
    return false;
  }
  off += sizeof(START_V2_MAGIC);
  if (payload[off++] != 1) return false;
  auto decode_list = [&](AudioFormatList *list) -> bool {
    if (len < off + 1) return false;
    const uint8_t count = payload[off++];
    if (count == 0 || count > INTERCOM_MAX_AUDIO_FORMATS) return false;
    list->count = count;
    for (uint8_t i = 0; i < count; i++) {
      const size_t n = decode_audio_format(payload + off, len - off, &list->formats[i]);
      if (n == 0) return false;
      off += n;
    }
    return true;
  };
  return decode_list(&this->offer_tx_formats_) && decode_list(&this->offer_rx_formats_) && off == len;
}

bool SipTransport::send_request_(const std::string &method, const std::string &body, uint32_t cseq) {
  const uint32_t ip = this->remote_ip_v4_.load(std::memory_order_acquire);
  const uint16_t port = this->remote_sip_port_.load(std::memory_order_acquire);
  if (ip == 0 || port == 0 || this->call_id_.empty()) return false;
  if (this->branch_.empty()) this->branch_ = "z9hG4bK" + make_token("");
  std::string local_ip = "0.0.0.0";
  this->local_ip_for_peer_(ip, &local_ip);
  const std::string request_uri = this->remote_uri_.empty()
      ? ("sip:intercom@" + local_ip)
      : strip_angle_uri(this->remote_uri_);
  const char *transport = this->remote_sip_tcp_.load(std::memory_order_acquire) ? "TCP" : "UDP";
  std::string msg = method + " " + request_uri + " SIP/2.0\r\n";
  msg += "Via: SIP/2.0/" + std::string(transport) + " " + local_ip + ":" +
         std::to_string(this->sip_port_) + ";branch=" + this->branch_ + ";rport\r\n";
  msg += "Max-Forwards: 70\r\n";
  msg += "From: " + this->local_uri_ + ";tag=" + this->local_tag_ + "\r\n";
  msg += "To: " + this->remote_uri_;
  if (!this->remote_tag_.empty()) msg += ";tag=" + this->remote_tag_;
  msg += "\r\n";
  msg += "Call-ID: " + this->call_id_ + "\r\n";
  const uint32_t request_cseq = cseq == 0 ? this->cseq_++ : cseq;
  msg += "CSeq: " + std::to_string(request_cseq) + " " + method + "\r\n";
  msg += "Contact: " + this->local_uri_ + "\r\n";
  msg += "User-Agent: ESPHome-Intercom-SIP\r\n";
  if (method == "INVITE") {
    msg += "X-Intercom-Caller-Route: " + this->caller_route_ + "\r\n";
    msg += "X-Intercom-Caller-Name: " + this->caller_name_ + "\r\n";
    msg += "X-Intercom-Dest-Route: " + this->dest_route_ + "\r\n";
    msg += "X-Intercom-Dest-Name: " + this->dest_name_ + "\r\n";
  }
  if (!body.empty()) msg += "Content-Type: application/sdp\r\n";
  msg += "Content-Length: " + std::to_string(body.size()) + "\r\n\r\n";
  msg += body;
  return this->send_sip_(msg, ip, port);
}

bool SipTransport::send_response_(uint16_t status, const char *reason, const std::string &body,
                                  const std::string &app_reason) {
  const uint32_t ip = this->remote_ip_v4_.load(std::memory_order_acquire);
  const uint16_t port = this->remote_sip_port_.load(std::memory_order_acquire);
  if (ip == 0 || port == 0 || this->last_invite_via_.empty()) return false;
  std::string msg = "SIP/2.0 " + std::to_string(status) + " " + reason + "\r\n";
  msg += "Via: " + this->last_invite_via_ + "\r\n";
  msg += "From: " + this->last_invite_from_ + "\r\n";
  msg += "To: " + this->last_invite_to_;
  if (this->last_invite_to_.find("tag=") == std::string::npos) {
    if (this->local_tag_.empty()) this->local_tag_ = make_token("tag");
    msg += ";tag=" + this->local_tag_;
  }
  msg += "\r\n";
  msg += "Call-ID: " + this->call_id_ + "\r\n";
  msg += "CSeq: " + this->last_invite_cseq_ + "\r\n";
  msg += "Contact: " + this->local_uri_ + "\r\n";
  msg += "User-Agent: ESPHome-Intercom-SIP\r\n";
  const std::string clean_reason = sip_header_token(app_reason);
  if (!clean_reason.empty()) {
    msg += "Reason: X-Intercom;cause=" + std::to_string(status) + ";text=" + sip_quoted(clean_reason) + "\r\n";
    msg += "X-Intercom-Decline-Reason: " + clean_reason + "\r\n";
  }
  if (!body.empty()) msg += "Content-Type: application/sdp\r\n";
  msg += "Content-Length: " + std::to_string(body.size()) + "\r\n\r\n";
  msg += body;
  return this->send_sip_(msg, ip, port);
}

bool SipTransport::send_stateless_response_(const std::string &request, const sockaddr_in &src,
                                            uint16_t status, const char *reason,
                                            const std::string &app_reason) {
  const uint32_t ip = ntohl(src.sin_addr.s_addr);
  const uint16_t port = ntohs(src.sin_port);
  const std::string via = header_value(request, "Via");
  const std::string from = header_value(request, "From");
  const std::string to = header_value(request, "To");
  const std::string call_id = header_value(request, "Call-ID");
  const std::string cseq = header_value(request, "CSeq");
  if (via.empty() || from.empty() || to.empty() || call_id.empty() || cseq.empty()) return false;
  std::string msg = "SIP/2.0 " + std::to_string(status) + " " + reason + "\r\n";
  msg += "Via: " + via + "\r\n";
  msg += "From: " + from + "\r\n";
  msg += "To: " + to + "\r\n";
  msg += "Call-ID: " + call_id + "\r\n";
  msg += "CSeq: " + cseq + "\r\n";
  const std::string clean_reason = sip_header_token(app_reason);
  if (!clean_reason.empty() && status >= 300) {
    msg += "Reason: X-Intercom;cause=" + std::to_string(status) + ";text=" + sip_quoted(clean_reason) + "\r\n";
    msg += "X-Intercom-Decline-Reason: " + clean_reason + "\r\n";
  }
  msg += "Content-Length: 0\r\n\r\n";
  return this->send_sip_(msg, ip, port);
}

bool SipTransport::send_invite_(const uint8_t *payload, size_t len) {
  if (!this->parse_start_payload_(payload, len)) return false;
  const uint32_t ip = this->remote_ip_v4_.load(std::memory_order_acquire);
  if (ip == 0) return false;
  if (this->local_tag_.empty()) this->local_tag_ = make_token("tag");
  this->branch_ = "z9hG4bK" + make_token("");
  this->invite_cseq_ = this->cseq_;
  std::string local_ip = "0.0.0.0";
  this->local_ip_for_peer_(ip, &local_ip);
  struct in_addr a{};
  a.s_addr = htonl(ip);
  char ip_text[16];
  inet_ntoa_r(a, ip_text, sizeof(ip_text));
  this->local_uri_ = "<sip:" + sanitize_user(this->caller_name_) + "@" + local_ip + ">";
  this->remote_uri_ = "<sip:" + sanitize_user(this->dest_name_) + "@" + std::string(ip_text) + ">";
  ESP_LOGI(TAG, "SIP INVITE call_id=%s from=%s to=%s", this->call_id_.c_str(),
           this->caller_name_.c_str(), this->dest_name_.c_str());
  const std::string sdp = this->build_sdp_offer_();
  if (sdp.empty()) return false;
  const bool sent = this->send_request_("INVITE", sdp, this->invite_cseq_);
  if (sent) {
    this->outgoing_invite_pending_.store(true, std::memory_order_release);
    if (this->cseq_ <= this->invite_cseq_) this->cseq_ = this->invite_cseq_ + 1;
  }
  return sent;
}

void SipTransport::send_audio_frame(const uint8_t *pcm, size_t bytes) {
  if (!this->rtp_running_.load(std::memory_order_acquire) || this->rtp_socket_ < 0 || pcm == nullptr || bytes == 0) return;
  const uint32_t ip = this->remote_ip_v4_.load(std::memory_order_acquire);
  const uint16_t port = this->remote_rtp_port_.load(std::memory_order_acquire);
  if (ip == 0 || port == 0 || bytes > UDP_SAFE_AUDIO_PAYLOAD_BYTES) return;
  uint8_t packet[1500];
  packet[0] = 0x80;
  packet[1] = this->rtp_tx_payload_type_ & 0x7F;
  const uint16_t seq = this->rtp_sequence_.fetch_add(1, std::memory_order_acq_rel);
  packet[2] = static_cast<uint8_t>(seq >> 8);
  packet[3] = static_cast<uint8_t>(seq & 0xFF);
  const uint32_t ts = this->rtp_timestamp_.load(std::memory_order_acquire);
  packet[4] = static_cast<uint8_t>(ts >> 24);
  packet[5] = static_cast<uint8_t>((ts >> 16) & 0xFF);
  packet[6] = static_cast<uint8_t>((ts >> 8) & 0xFF);
  packet[7] = static_cast<uint8_t>(ts & 0xFF);
  packet[8] = static_cast<uint8_t>(this->rtp_ssrc_ >> 24);
  packet[9] = static_cast<uint8_t>((this->rtp_ssrc_ >> 16) & 0xFF);
  packet[10] = static_cast<uint8_t>((this->rtp_ssrc_ >> 8) & 0xFF);
  packet[11] = static_cast<uint8_t>(this->rtp_ssrc_ & 0xFF);
  const uint8_t bps = this->selected_tx_format_.container_bytes_per_sample();
  const size_t input_bytes = bytes;
  uint8_t *dst = packet + 12;
  if (this->selected_tx_format_.pcm_format == PcmFormat::S16LE) {
    if ((bytes % 2) != 0) return;
    for (size_t i = 0; i < bytes; i += 2) {
      dst[i] = pcm[i + 1];
      dst[i + 1] = pcm[i];
    }
  } else if (this->selected_tx_format_.pcm_format == PcmFormat::S24LE) {
    if ((bytes % 3) != 0) return;
    for (size_t i = 0; i < bytes; i += 3) {
      dst[i] = pcm[i + 2];
      dst[i + 1] = pcm[i + 1];
      dst[i + 2] = pcm[i];
    }
  } else if (this->selected_tx_format_.pcm_format == PcmFormat::S24LE_IN_S32) {
    if ((bytes % 4) != 0 || bytes / 4 * 3 > sizeof(packet) - 12) return;
    size_t out = 0;
    for (size_t i = 0; i < bytes; i += 4) {
      dst[out++] = pcm[i + 2];
      dst[out++] = pcm[i + 1];
      dst[out++] = pcm[i];
    }
    bytes = out;
  } else {
    return;
  }
  const uint32_t samples = bps == 0 || this->selected_tx_format_.channels == 0
      ? 0
      : static_cast<uint32_t>(input_bytes / bps / this->selected_tx_format_.channels);
  this->rtp_timestamp_.store(ts + samples, std::memory_order_release);
  struct sockaddr_in dest{};
  dest.sin_family = AF_INET;
  dest.sin_addr.s_addr = htonl(ip);
  dest.sin_port = htons(port);
  sendto(this->rtp_socket_, packet, 12 + bytes, 0,
         reinterpret_cast<struct sockaddr *>(&dest), sizeof(dest));
}

bool SipTransport::send_control(MessageType type, const uint8_t *payload, size_t len) {
  ESP_LOGI(TAG, "SIP control request %s len=%u", message_type_name(type), (unsigned) len);
  switch (type) {
    case MessageType::START:
      return payload != nullptr && this->send_invite_(payload, len);
    case MessageType::RING:
      return this->send_response_(180, "Ringing");
    case MessageType::ANSWER:
      this->outgoing_invite_pending_.store(false, std::memory_order_release);
      this->call_active_.store(true, std::memory_order_release);
      return this->send_response_(200, "OK", this->build_sdp_answer_());
    case MessageType::DECLINE:
      if (this->outgoing_invite_pending_.load(std::memory_order_acquire)) {
        const bool sent = this->send_request_("CANCEL", "", this->invite_cseq_);
        this->reset_dialog_();
        return sent;
      }
      if (!this->call_active_.load(std::memory_order_acquire)) {
        const bool sent = this->send_response_(486, "Busy Here", "", decline_reason_from_payload(payload, len));
        this->reset_dialog_();
        return sent;
      }
      return this->send_request_("BYE");
    case MessageType::HANGUP:
      return this->send_request_("BYE");
    case MessageType::ERROR:
      return this->send_response_(500, "Server Internal Error");
    case MessageType::PING:
    case MessageType::PONG:
    case MessageType::AUDIO:
    default:
      return true;
  }
}

bool SipTransport::handle_invite_(const std::string &message, const sockaddr_in &src) {
  const std::string body = message_body(message);
  const uint32_t src_ip = ntohl(src.sin_addr.s_addr);
  const std::string incoming_call_id = header_value(message, "Call-ID");
  if (!incoming_call_id.empty() && !this->call_id_.empty() && incoming_call_id != this->call_id_) {
    ESP_LOGW(TAG, "SIP INVITE rejected busy: active_call_id=%s incoming_call_id=%s",
             this->call_id_.c_str(), incoming_call_id.c_str());
    return this->send_stateless_response_(message, src, 486, "Busy Here", "busy");
  }
  this->remote_ip_v4_.store(src_ip, std::memory_order_release);
  this->remote_sip_port_.store(ntohs(src.sin_port), std::memory_order_release);
  this->call_id_ = incoming_call_id;
  this->last_invite_via_ = header_value(message, "Via");
  this->last_invite_from_ = header_value(message, "From");
  this->last_invite_to_ = header_value(message, "To");
  this->last_invite_cseq_ = header_value(message, "CSeq");
  this->remote_tag_ = tag_from_header(this->last_invite_from_);
  if (this->local_tag_.empty()) this->local_tag_ = make_token("tag");
  this->remote_uri_ = this->last_invite_from_;
  this->local_uri_ = this->last_invite_to_;
  if (this->call_id_.empty() || this->last_invite_via_.empty() || this->last_invite_from_.empty() ||
      this->last_invite_to_.empty() || this->last_invite_cseq_.empty()) {
    const bool sent = this->send_response_(400, "Bad Request");
    this->reset_dialog_();
    return sent;
  }
  if (!this->learn_remote_rtp_from_sdp_(body, src_ip)) {
    const bool sent = this->send_response_(488, "Not Acceptable Here");
    this->reset_dialog_();
    return sent;
  }
  this->send_response_(100, "Trying");

  std::string from_user = sip_header_token(header_value(message, "X-Intercom-Caller-Name"));
  std::string to_user = sip_header_token(header_value(message, "X-Intercom-Dest-Name"));
  if (from_user.empty()) from_user = sip_user_from_header(this->last_invite_from_);
  if (to_user.empty()) to_user = sip_user_from_header(this->last_invite_to_);
  if (from_user.empty() || to_user.empty()) {
    const bool sent = this->send_response_(400, "Bad Request");
    this->reset_dialog_();
    return sent;
  }
  this->caller_name_ = from_user;
  this->dest_name_ = to_user;
  this->caller_route_ = header_value(message, "X-Intercom-Caller-Route");
  this->dest_route_ = header_value(message, "X-Intercom-Dest-Route");
  if (this->caller_route_.empty()) this->caller_route_ = this->caller_name_;
  if (this->dest_route_.empty()) this->dest_route_ = this->dest_name_;

  uint8_t payload[INTERCOM_MAX_CALL_ID_LEN + 4 * (INTERCOM_MAX_NAME_LEN + 1) + 176];
  size_t off = encode_call_id_prefix(payload, sizeof(payload), this->call_id_);
  if (off == 0) {
    const bool sent = this->send_response_(400, "Bad Request");
    this->reset_dialog_();
    return sent;
  }
  auto enc = [&](const std::string &value, size_t max_len) -> bool {
    const size_t n = encode_lp_string(payload + off, sizeof(payload) - off, value, max_len);
    if (n == 0) return false;
    off += n;
    return true;
  };
  if (!enc(this->caller_route_, INTERCOM_MAX_ROUTE_ID_LEN) ||
      !enc(this->caller_name_, INTERCOM_MAX_NAME_LEN) ||
      !enc(this->dest_route_, INTERCOM_MAX_ROUTE_ID_LEN) ||
      !enc(this->dest_name_, INTERCOM_MAX_NAME_LEN)) {
    const bool sent = this->send_response_(400, "Bad Request");
    this->reset_dialog_();
    return sent;
  }
  static const uint8_t START_V2_MAGIC[] = {'I', 'C', 'A', 'F', '2'};
  std::memcpy(payload + off, START_V2_MAGIC, sizeof(START_V2_MAGIC));
  off += sizeof(START_V2_MAGIC);
  payload[off++] = 1;
  auto enc_list = [&](const AudioFormat &fmt) -> bool {
    if (sizeof(payload) - off < 1 + 8) return false;
    payload[off++] = 1;
    const size_t n = encode_audio_format(payload + off, sizeof(payload) - off, fmt);
    if (n == 0) return false;
    off += n;
    return true;
  };
  if (!enc_list(this->selected_rx_format_) || !enc_list(this->selected_tx_format_)) {
    const bool sent = this->send_response_(488, "Not Acceptable Here");
    this->reset_dialog_();
    return sent;
  }
  ESP_LOGI(TAG, "SIP INVITE accepted into FSM call_id=%s", this->call_id_.c_str());
  this->emit_control_(MessageType::START, payload, off);
  return true;
}

bool SipTransport::handle_response_(const std::string &message, const sockaddr_in &src) {
  const uint32_t src_ip = ntohl(src.sin_addr.s_addr);
  this->remote_ip_v4_.store(src_ip, std::memory_order_release);
  this->remote_sip_port_.store(ntohs(src.sin_port), std::memory_order_release);
  if (message.rfind("SIP/2.0 ", 0) != 0 || message.size() < 12) return false;
  const std::string response_call_id = header_value(message, "Call-ID");
  if (response_call_id.empty() || this->call_id_.empty() || response_call_id != this->call_id_) {
    ESP_LOGD(TAG, "SIP response ignored for stale/unknown call_id=%s current=%s",
             response_call_id.empty() ? "(empty)" : response_call_id.c_str(),
             this->call_id_.empty() ? "(none)" : this->call_id_.c_str());
    return true;
  }
  const int status = std::atoi(message.substr(8, 3).c_str());
  const std::string method = cseq_method(header_value(message, "CSeq"));
  if (status == 180 && method == "INVITE") {
    uint8_t payload[INTERCOM_MAX_CALL_ID_LEN + 4];
    const size_t n = encode_call_id_prefix(payload, sizeof(payload), this->call_id_);
    if (n > 0) this->emit_control_(MessageType::RING, payload, n);
    return true;
  }
  if (status >= 200 && status < 300) {
    if (method == "BYE") {
      ESP_LOGI(TAG, "SIP BYE completed call_id=%s", this->call_id_.c_str());
      this->reset_dialog_();
      return true;
    }
    if (method == "CANCEL") {
      ESP_LOGI(TAG, "SIP CANCEL completed call_id=%s", this->call_id_.c_str());
      return true;
    }
    if (method != "INVITE") {
      ESP_LOGI(TAG, "SIP %u response for %s ignored", status, method.c_str());
      return true;
    }
    this->outgoing_invite_pending_.store(false, std::memory_order_release);
    const std::string to = header_value(message, "To");
    this->remote_tag_ = tag_from_header(to);
    this->learn_remote_rtp_from_sdp_(message_body(message), src_ip);
    this->send_request_("ACK", "", this->invite_cseq_);
    this->call_active_.store(true, std::memory_order_release);
    uint8_t payload[INTERCOM_MAX_CALL_ID_LEN + 40];
    size_t off = encode_call_id_prefix(payload, sizeof(payload), this->call_id_);
    if (off > 0) {
      static const uint8_t ANSWER_V2_MAGIC[] = {'I', 'C', 'A', 'A', '2'};
      std::memcpy(payload + off, ANSWER_V2_MAGIC, sizeof(ANSWER_V2_MAGIC));
      off += sizeof(ANSWER_V2_MAGIC);
      payload[off++] = 1;
      size_t n = encode_audio_format(payload + off, sizeof(payload) - off, this->selected_tx_format_);
      if (n > 0) {
        off += n;
        n = encode_audio_format(payload + off, sizeof(payload) - off, this->selected_rx_format_);
        if (n > 0) {
          off += n;
          this->emit_control_(MessageType::ANSWER, payload, off);
        }
      }
    }
    return true;
  }
  if (status >= 300) {
    if (method != "INVITE") {
      ESP_LOGW(TAG, "SIP %u response for %s", status, method.c_str());
      return true;
    }
    this->outgoing_invite_pending_.store(false, std::memory_order_release);
    uint8_t payload[INTERCOM_MAX_CALL_ID_LEN + INTERCOM_MAX_REASON_LEN + 8];
    size_t off = encode_call_id_prefix(payload, sizeof(payload), this->call_id_);
    std::string reason = sip_header_token(header_value(message, "X-Intercom-Decline-Reason"));
    if (reason.empty()) reason = reason_text_from_header(header_value(message, "Reason"));
    if (reason.empty()) reason = sip_failure_reason_(status);
    const size_t n = encode_lp_string(payload + off, sizeof(payload) - off, reason, INTERCOM_MAX_REASON_LEN);
    if (off > 0 && n > 0) {
      off += n;
      this->emit_control_(MessageType::DECLINE, payload, off);
    }
    this->reset_dialog_();
    return true;
  }
  return true;
}

void SipTransport::handle_sip_datagram_(const char *data, size_t len, const sockaddr_in &src) {
  const std::string msg(data, len);
  if (msg.rfind("SIP/2.0 ", 0) == 0) {
    this->handle_response_(msg, src);
    return;
  }
  const size_t first_space = msg.find(' ');
  const std::string method = first_space == std::string::npos ? "" : msg.substr(0, first_space);
  ESP_LOGI(TAG, "SIP RX method=%s len=%u", method.c_str(), (unsigned) len);
  if (method == "INVITE") {
    this->handle_invite_(msg, src);
  } else if (method == "ACK") {
    const std::string request_call_id = header_value(msg, "Call-ID");
    if (request_call_id.empty() || this->call_id_.empty() || request_call_id != this->call_id_) {
      ESP_LOGD(TAG, "SIP ACK ignored for stale/unknown call_id=%s current=%s",
               request_call_id.empty() ? "(empty)" : request_call_id.c_str(),
               this->call_id_.empty() ? "(none)" : this->call_id_.c_str());
      return;
    }
    this->outgoing_invite_pending_.store(false, std::memory_order_release);
    this->call_active_.store(true, std::memory_order_release);
  } else if (method == "BYE") {
    const std::string request_call_id = header_value(msg, "Call-ID");
    if (!request_call_id.empty() && !this->call_id_.empty() && request_call_id != this->call_id_) {
      ESP_LOGW(TAG, "SIP BYE ignored for stale call_id=%s current=%s",
               request_call_id.c_str(), this->call_id_.c_str());
      this->send_stateless_response_(msg, src, 481, "Call/Transaction Does Not Exist");
      return;
    }
    this->send_stateless_response_(msg, src, 200, "OK");
    uint8_t payload[INTERCOM_MAX_CALL_ID_LEN + 4];
    const size_t n = encode_call_id_prefix(payload, sizeof(payload), this->call_id_);
    if (n > 0) this->emit_control_(MessageType::HANGUP, payload, n);
    this->reset_dialog_();
  } else if (method == "CANCEL") {
    const std::string request_call_id = header_value(msg, "Call-ID");
    if (!request_call_id.empty() && !this->call_id_.empty() && request_call_id != this->call_id_) {
      ESP_LOGW(TAG, "SIP CANCEL ignored for stale call_id=%s current=%s",
               request_call_id.c_str(), this->call_id_.c_str());
      this->send_stateless_response_(msg, src, 481, "Call/Transaction Does Not Exist");
      return;
    }
    this->send_stateless_response_(msg, src, 200, "OK");
    this->send_response_(487, "Request Terminated");
    uint8_t payload[INTERCOM_MAX_CALL_ID_LEN + INTERCOM_MAX_REASON_LEN + 8];
    size_t off = encode_call_id_prefix(payload, sizeof(payload), this->call_id_);
    const size_t n = encode_lp_string(payload + off, sizeof(payload) - off, "", INTERCOM_MAX_REASON_LEN);
    if (off > 0 && n > 0) {
      off += n;
      this->emit_control_(MessageType::DECLINE, payload, off);
    }
    this->reset_dialog_();
  } else if (method == "OPTIONS") {
    this->send_stateless_response_(msg, src, 200, "OK");
  } else if (sip_method_known_(method)) {
    this->send_stateless_response_(msg, src, 405, "Method Not Allowed");
  } else {
    this->send_stateless_response_(msg, src, 501, "Not Implemented");
  }
}

void SipTransport::handle_sip_stream_(int socket, const sockaddr_in &src) {
  char buf[1024];
  while (true) {
    const int n = recv(socket, buf, sizeof(buf), 0);
    if (n > 0) {
      this->sip_tcp_rx_buffer_.append(buf, static_cast<size_t>(n));
      continue;
    }
    if (n == 0) {
      ESP_LOGI(TAG, "SIP TCP peer closed");
      close(socket);
      if (this->sip_tcp_client_socket_ == socket) this->sip_tcp_client_socket_ = -1;
      this->remote_sip_tcp_.store(false, std::memory_order_release);
      this->sip_tcp_rx_buffer_.clear();
      return;
    }
    const int err = errno;
    if (err == EWOULDBLOCK || err == EAGAIN) break;
    ESP_LOGW(TAG, "SIP TCP RX failed: %s (%d: %s)", socket_errno_name(err), err, socket_errno_text(err));
    close(socket);
    if (this->sip_tcp_client_socket_ == socket) this->sip_tcp_client_socket_ = -1;
    this->remote_sip_tcp_.store(false, std::memory_order_release);
    this->sip_tcp_rx_buffer_.clear();
    return;
  }

  while (true) {
    const size_t sep = this->sip_tcp_rx_buffer_.find("\r\n\r\n");
    if (sep == std::string::npos) return;
    const size_t body_len = sip_content_length(this->sip_tcp_rx_buffer_);
    const size_t total = sep + 4 + body_len;
    if (this->sip_tcp_rx_buffer_.size() < total) return;
    const std::string msg = this->sip_tcp_rx_buffer_.substr(0, total);
    this->sip_tcp_rx_buffer_.erase(0, total);
    this->remote_sip_tcp_.store(true, std::memory_order_release);
    this->handle_sip_datagram_(msg.data(), msg.size(), src);
  }
}

void SipTransport::sip_task_trampoline_(void *param) {
  static_cast<SipTransport *>(param)->sip_task_();
}

void SipTransport::rtp_task_trampoline_(void *param) {
  static_cast<SipTransport *>(param)->rtp_task_();
}

void SipTransport::sip_task_() {
  uint8_t buf[2048];
  while (this->running_.load(std::memory_order_acquire)) {
    fd_set readfds;
    FD_ZERO(&readfds);
    int max_fd = -1;
    if (this->sip_socket_ >= 0) {
      FD_SET(this->sip_socket_, &readfds);
      max_fd = std::max(max_fd, this->sip_socket_);
    }
    if (this->sip_tcp_listener_socket_ >= 0) {
      FD_SET(this->sip_tcp_listener_socket_, &readfds);
      max_fd = std::max(max_fd, this->sip_tcp_listener_socket_);
    }
    if (this->sip_tcp_client_socket_ >= 0) {
      FD_SET(this->sip_tcp_client_socket_, &readfds);
      max_fd = std::max(max_fd, this->sip_tcp_client_socket_);
    }
    struct timeval timeout{};
    timeout.tv_sec = 0;
    timeout.tv_usec = 10000;
    const int ready = max_fd >= 0 ? select(max_fd + 1, &readfds, nullptr, nullptr, &timeout) : 0;
    if (ready <= 0) {
      delay(1);
      continue;
    }

    if (this->sip_tcp_listener_socket_ >= 0 && FD_ISSET(this->sip_tcp_listener_socket_, &readfds)) {
      struct sockaddr_in src{};
      socklen_t slen = sizeof(src);
      int client = accept(this->sip_tcp_listener_socket_, reinterpret_cast<struct sockaddr *>(&src), &slen);
      if (client >= 0) {
        int opt = 1;
        setsockopt(client, IPPROTO_TCP, TCP_NODELAY, &opt, sizeof(opt));
        int flags = fcntl(client, F_GETFL, 0);
        fcntl(client, F_SETFL, flags | O_NONBLOCK);
        if (this->sip_tcp_client_socket_ >= 0) close(this->sip_tcp_client_socket_);
        this->sip_tcp_client_socket_ = client;
        this->sip_tcp_rx_buffer_.clear();
        this->remote_sip_tcp_.store(true, std::memory_order_release);
        char ip[16];
        inet_ntoa_r(src.sin_addr, ip, sizeof(ip));
        ESP_LOGI(TAG, "SIP TCP accepted from %s:%u", ip, (unsigned) ntohs(src.sin_port));
      }
    }

    if (this->sip_socket_ >= 0 && FD_ISSET(this->sip_socket_, &readfds)) {
      struct sockaddr_in src{};
      socklen_t slen = sizeof(src);
      int n = recvfrom(this->sip_socket_, buf, sizeof(buf) - 1, 0,
                       reinterpret_cast<struct sockaddr *>(&src), &slen);
      if (n > 0) {
        this->remote_sip_tcp_.store(false, std::memory_order_release);
        buf[n] = 0;
        char ip[16];
        inet_ntoa_r(src.sin_addr, ip, sizeof(ip));
        ESP_LOGI(TAG, "SIP UDP RX %d bytes from %s:%u", n, ip, (unsigned) ntohs(src.sin_port));
        this->handle_sip_datagram_(reinterpret_cast<const char *>(buf), static_cast<size_t>(n), src);
      }
    }

    if (this->sip_tcp_client_socket_ >= 0 && FD_ISSET(this->sip_tcp_client_socket_, &readfds)) {
      struct sockaddr_in src{};
      socklen_t slen = sizeof(src);
      getpeername(this->sip_tcp_client_socket_, reinterpret_cast<struct sockaddr *>(&src), &slen);
      this->handle_sip_stream_(this->sip_tcp_client_socket_, src);
    }
  }
  vTaskDelete(nullptr);
}

void SipTransport::rtp_task_() {
  uint8_t buf[1600];
  uint8_t pcm[1500];
  while (this->rtp_running_.load(std::memory_order_acquire)) {
    struct sockaddr_in src{};
    socklen_t slen = sizeof(src);
    int n = recvfrom(this->rtp_socket_, buf, sizeof(buf), 0,
                     reinterpret_cast<struct sockaddr *>(&src), &slen);
    if (n > 12 && (buf[0] & 0xC0) == 0x80) {
      const uint8_t csrc_count = buf[0] & 0x0F;
      size_t header = 12u + static_cast<size_t>(csrc_count) * 4u;
      if (static_cast<size_t>(n) <= header) {
        delay(1);
        continue;
      }
      if ((buf[0] & 0x10) != 0) {
        if (static_cast<size_t>(n) < header + 4) continue;
        const uint16_t ext_len = static_cast<uint16_t>((buf[header + 2] << 8) | buf[header + 3]);
        header += 4u + static_cast<size_t>(ext_len) * 4u;
        if (static_cast<size_t>(n) <= header) continue;
      }
      size_t payload_len = static_cast<size_t>(n) - header;
      if ((buf[0] & 0x20) != 0 && payload_len > 0) {
        const uint8_t pad = buf[n - 1];
        if (pad == 0 || pad > payload_len) continue;
        payload_len -= pad;
      }
      if ((buf[1] & 0x7F) != this->rtp_rx_payload_type_) continue;
      const uint8_t *payload = buf + header;
      size_t out_len = payload_len;
      if (this->selected_rx_format_.pcm_format == PcmFormat::S16LE) {
        if ((payload_len % 2) != 0 || payload_len > sizeof(pcm)) continue;
        for (size_t i = 0; i < payload_len; i += 2) {
          pcm[i] = payload[i + 1];
          pcm[i + 1] = payload[i];
        }
      } else if (this->selected_rx_format_.pcm_format == PcmFormat::S24LE) {
        if ((payload_len % 3) != 0 || payload_len > sizeof(pcm)) continue;
        for (size_t i = 0; i < payload_len; i += 3) {
          pcm[i] = payload[i + 2];
          pcm[i + 1] = payload[i + 1];
          pcm[i + 2] = payload[i];
        }
      } else if (this->selected_rx_format_.pcm_format == PcmFormat::S24LE_IN_S32) {
        if ((payload_len % 3) != 0 || payload_len / 3 * 4 > sizeof(pcm)) continue;
        out_len = 0;
        for (size_t i = 0; i < payload_len; i += 3) {
          pcm[out_len++] = payload[i + 2];
          pcm[out_len++] = payload[i + 1];
          pcm[out_len++] = payload[i];
          pcm[out_len++] = payload[i] & 0x80 ? 0xFF : 0x00;
        }
      } else {
        continue;
      }
      this->emit_audio_frame_(pcm, out_len);
    } else {
      delay(5);
    }
  }
  vTaskDelete(nullptr);
}

}  // namespace intercom_api
}  // namespace esphome

#endif  // USE_ESP32 && USE_INTERCOM_SIP_TRANSPORT
