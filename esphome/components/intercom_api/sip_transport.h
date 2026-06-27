#pragma once

#include "esphome/core/defines.h"

#if defined(USE_ESP32) && defined(USE_INTERCOM_SIP_TRANSPORT)

#include "transport.h"

#include <lwip/sockets.h>
#include <freertos/FreeRTOS.h>
#include <freertos/task.h>

#include <atomic>
#include <cstdint>
#include <string>
#include <vector>

namespace esphome {
namespace intercom_api {

class SipTransport : public SipPhoneTransport {
 public:
  static constexpr uint32_t kSipTaskStackBytes = 8192;
  static constexpr uint32_t kRtpTaskStackBytes = 8192;

  SipTransport(uint16_t sip_port, uint16_t rtp_port, std::string remote_host,
               bool task_stacks_in_psram);
  ~SipTransport() override;

  bool start() override;
  void stop() override;
  bool is_connected() const override;
  void send_audio_frame(const uint8_t *pcm, size_t bytes) override;
  bool send_invite(const std::string &call_id,
                   const std::string &caller_route,
                   const std::string &caller_name,
                   const std::string &dest_route,
                   const std::string &dest_name) override;
  bool send_ringing(const std::string &call_id) override;
  bool send_answer(const std::string &call_id,
                   const AudioFormat &caller_to_dest_format,
                   const AudioFormat &dest_to_caller_format) override;
  bool send_cancel(const std::string &call_id) override;
  bool send_bye(const std::string &call_id) override;
  bool send_final_response(const std::string &call_id,
                           uint16_t status,
                           const std::string &reason) override;
  const char *transport_name() const override { return "sip"; }
  bool start_audio_path() override;
  void stop_audio_path() override;
  bool originate(const std::string &host, uint16_t port) override;
  void set_remote(const std::string &ip, uint16_t port, uint16_t control_port = 0) override;
  void set_sip_signaling_transport(bool tcp) override;
  void set_audio_formats(const AudioFormatList &tx, const AudioFormatList &rx) override;
  SipTransportSnapshot snapshot() const override;

 protected:
  enum class SipEvent : uint8_t {
    NONE = 0,
    INVITE,
    ACK,
    CANCEL,
    BYE,
    OPTIONS,
    RESPONSE,
  };

  static void sip_task_trampoline_(void *param);
  static void rtp_task_trampoline_(void *param);
  void sip_task_();
  void rtp_task_();
  bool bind_udp_(int *fd, uint16_t port, const char *label);
  bool bind_tcp_(int *fd, uint16_t port, const char *label);
  bool parse_remote_(const std::string &host);
  bool send_sip_(const std::string &message, uint32_t ip_v4, uint16_t port);
  bool send_sip_tcp_(const std::string &message);
  bool send_request_(const std::string &method, const std::string &body = "", uint32_t cseq = 0);
  bool send_response_(uint16_t status, const char *reason, const std::string &body = "",
                      const std::string &app_reason = "");
  bool send_stateless_response_(const std::string &request, const sockaddr_in &src,
                                uint16_t status, const char *reason,
                                const std::string &app_reason = "");
  void handle_sip_datagram_(const char *data, size_t len, const sockaddr_in &src);
  void handle_sip_stream_(int socket, const sockaddr_in &src);
  bool handle_invite_(const std::string &message, const sockaddr_in &src);
  bool handle_response_(const std::string &message, const sockaddr_in &src);
  std::string build_sdp_offer_() const;
  std::string build_sdp_answer_() const;
  bool learn_remote_rtp_from_sdp_(const std::string &sdp, uint32_t default_ip);
  bool local_ip_for_peer_(uint32_t peer_ip_v4, std::string *out) const;
  void reset_dialog_();
  void mark_sip_event_(SipEvent event, uint16_t status = 0);
  static const char *sip_event_name_(SipEvent event);

  uint16_t sip_port_{5060};
  uint16_t rtp_port_{40000};
  bool task_stacks_in_psram_{false};
  std::atomic<uint32_t> remote_ip_v4_{0};
  std::atomic<uint16_t> remote_sip_port_{5060};
  std::atomic<uint16_t> remote_rtp_port_{0};
  std::atomic<uint16_t> rtp_sequence_{0};
  std::atomic<uint32_t> rtp_timestamp_{0};
  uint32_t rtp_ssrc_{0x49434150};

  std::string call_id_;
  std::string local_tag_;
  std::string remote_tag_;
  std::string branch_;
  std::string local_uri_;
  std::string remote_uri_;
  std::string last_invite_via_;
  std::string last_invite_from_;
  std::string last_invite_to_;
  std::string last_invite_cseq_;
  std::string caller_route_;
  std::string caller_name_;
  std::string dest_route_;
  std::string dest_name_;
  std::string sip_tcp_rx_buffer_;
  AudioFormatList offer_tx_formats_{};
  AudioFormatList offer_rx_formats_{};
  AudioFormat selected_tx_format_{DEFAULT_AUDIO_FORMAT};
  AudioFormat selected_rx_format_{DEFAULT_AUDIO_FORMAT};
  uint8_t rtp_tx_payload_type_{96};
  uint8_t rtp_rx_payload_type_{96};
  uint32_t cseq_{1};
  uint32_t invite_cseq_{1};

  int sip_socket_{-1};
  int sip_tcp_listener_socket_{-1};
  int sip_tcp_client_socket_{-1};
  int rtp_socket_{-1};
  TaskHandle_t sip_task_handle_{nullptr};
  StaticTask_t sip_task_tcb_{};
  StackType_t *sip_task_stack_{nullptr};
  TaskHandle_t rtp_task_handle_{nullptr};
  StaticTask_t rtp_task_tcb_{};
  StackType_t *rtp_task_stack_{nullptr};
  std::atomic<bool> running_{false};
  std::atomic<bool> rtp_running_{false};
  std::atomic<bool> call_active_{false};
  std::atomic<bool> outgoing_invite_pending_{false};
  std::atomic<bool> remote_sip_tcp_{false};
  std::atomic<uint32_t> rtp_tx_packets_{0};
  std::atomic<uint32_t> rtp_rx_packets_{0};
  std::atomic<uint32_t> rtp_tx_bytes_{0};
  std::atomic<uint32_t> rtp_rx_bytes_{0};
  std::atomic<uint16_t> last_sip_status_code_{0};
  std::atomic<uint8_t> last_sip_event_{0};
};

}  // namespace intercom_api
}  // namespace esphome

#endif  // USE_ESP32 && USE_INTERCOM_SIP_TRANSPORT
