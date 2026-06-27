#pragma once

// Action / Condition templates for intercom_api YAML automation.
// intercom_api.h re-includes this at the bottom; pragma once handles the loop.

#include "intercom_api.h"

#ifdef USE_ESP32

namespace esphome {
namespace intercom_api {

template<typename... Ts>
class NextContactAction : public Action<Ts...>, public Parented<IntercomApi> {
 public:
  void play(const Ts &...x) override { this->parent_->next_contact(); }
};

template<typename... Ts>
class PrevContactAction : public Action<Ts...>, public Parented<IntercomApi> {
 public:
  void play(const Ts &...x) override { this->parent_->prev_contact(); }
};

template<typename... Ts>
class StartAction : public Action<Ts...>, public Parented<IntercomApi> {
 public:
  void play(const Ts &...x) override { this->parent_->start(); }
};
template<typename... Ts>
class StopAction : public Action<Ts...>, public Parented<IntercomApi> {
 public:
  void play(const Ts &...x) override { this->parent_->stop(); }
};

template<typename... Ts>
class AnswerCallAction : public Action<Ts...>, public Parented<IntercomApi> {
 public:
  void play(const Ts &...x) override { this->parent_->answer_call(); }
};

template<typename... Ts>
class DeclineCallAction : public Action<Ts...>, public Parented<IntercomApi> {
 public:
  TEMPLATABLE_VALUE(std::string, reason)
  void play(const Ts &...x) override {
    this->parent_->decline_call(this->reason_.optional_value(x...).value_or(""));
  }
};

template<typename... Ts>
class SetVolumeAction : public Action<Ts...>, public Parented<IntercomApi> {
 public:
  TEMPLATABLE_VALUE(float, volume)
  void play(const Ts &...x) override {
    this->parent_->set_volume(this->volume_.value(x...));
  }
};

template<typename... Ts>
class SetMicGainDbAction : public Action<Ts...>, public Parented<IntercomApi> {
 public:
  TEMPLATABLE_VALUE(float, gain_db)
  void play(const Ts &...x) override {
    this->parent_->set_mic_gain_db(this->gain_db_.value(x...));
  }
};

template<typename... Ts>
class SetContactsAction : public Action<Ts...>, public Parented<IntercomApi> {
 public:
  TEMPLATABLE_VALUE(std::string, contacts_csv)
  void play(const Ts &...x) override {
    this->parent_->set_contacts(this->contacts_csv_.value(x...));
  }
};

template<typename... Ts>
class SetContactAction : public Action<Ts...>, public Parented<IntercomApi> {
 public:
  TEMPLATABLE_VALUE(std::string, contact)
  void play(const Ts &...x) override {
    this->parent_->set_contact(this->contact_.value(x...));
  }
};

template<typename... Ts>
class CallContactAction : public Action<Ts...>, public Parented<IntercomApi> {
 public:
  TEMPLATABLE_VALUE(std::string, contact)
  void play(const Ts &...x) override {
    this->parent_->call_contact(this->contact_.value(x...));
  }
};

template<typename... Ts>
class CallToggleAction : public Action<Ts...>, public Parented<IntercomApi> {
 public:
  void play(const Ts &...x) override { this->parent_->call_toggle(); }
};

template<typename... Ts>
class FlushContactsAction : public Action<Ts...>, public Parented<IntercomApi> {
 public:
  void play(const Ts &...x) override { this->parent_->flush_contacts(); }
};

template<typename... Ts>
class UpdateContactsAction : public Action<Ts...>, public Parented<IntercomApi> {
 public:
  void play(const Ts &...x) override { this->parent_->update_contacts(); }
};

template<typename... Ts>
class AddContactAction : public Action<Ts...>, public Parented<IntercomApi> {
 public:
  TEMPLATABLE_VALUE(std::string, entry)
  TEMPLATABLE_VALUE(std::string, name)
  TEMPLATABLE_VALUE(std::string, ip)
  TEMPLATABLE_VALUE(std::string, protocol)
  TEMPLATABLE_VALUE(uint16_t, port)
  TEMPLATABLE_VALUE(uint16_t, rtp_port)
  void play(const Ts &...x) override {
    const auto raw = this->entry_.optional_value(x...);
    if (raw.has_value() && !raw.value().empty()) {
      this->parent_->add_contact(raw.value());
      return;
    }

    const std::string name = this->name_.value(x...);
    const auto ip = this->ip_.optional_value(x...);
    if (!ip.has_value() || ip.value().empty()) {
      this->parent_->add_contact(name);
      return;
    }

    const auto explicit_protocol = this->protocol_.optional_value(x...);
    const std::string protocol =
        explicit_protocol.has_value() && !explicit_protocol.value().empty()
            ? explicit_protocol.value()
            : this->parent_->configured_sip_transport_name();
    const uint16_t port = this->port_.optional_value(x...).value_or(5060);
    const uint16_t rtp_port = this->rtp_port_.optional_value(x...).value_or(40000);
    this->parent_->add_contact(name + "|sip|" + ip.value() + "|" +
                               std::to_string(port) + "|" +
                               std::to_string(rtp_port) + "|" + protocol);
  }
};

template<typename... Ts>
class RemoveContactAction : public Action<Ts...>, public Parented<IntercomApi> {
 public:
  TEMPLATABLE_VALUE(std::string, entry)
  void play(const Ts &...x) override {
    this->parent_->remove_contact(this->entry_.value(x...));
  }
};

template<typename... Ts>
class SetHaPeerNameAction : public Action<Ts...>, public Parented<IntercomApi> {
 public:
  TEMPLATABLE_VALUE(std::string, name)
  void play(const Ts &...x) override {
    this->parent_->set_ha_peer_name(this->name_.value(x...));
  }
};

template<typename... Ts>
class SetRemoteEndpointAction : public Action<Ts...>, public Parented<IntercomApi> {
 public:
  TEMPLATABLE_VALUE(std::string, ip)
  TEMPLATABLE_VALUE(uint16_t, port)
  TEMPLATABLE_VALUE(uint16_t, control_port)
  void play(const Ts &...x) override {
    this->parent_->set_remote_endpoint(
        this->ip_.value(x...), this->port_.value(x...),
        this->control_port_.value(x...));
  }
};

template<typename... Ts>
class PublishEntityStatesAction : public Action<Ts...>, public Parented<IntercomApi> {
 public:
  void play(const Ts &...x) override { this->parent_->publish_entity_states(); }
};

template<typename... Ts>
class IntercomIsIdleCondition : public Condition<Ts...>, public Parented<IntercomApi> {
 public:
  bool check(const Ts &...x) override { return this->parent_->is_idle(); }
};

template<typename... Ts>
class IntercomIsRingingCondition : public Condition<Ts...>, public Parented<IntercomApi> {
 public:
  bool check(const Ts &...x) override { return this->parent_->is_ringing(); }
};

template<typename... Ts>
class IntercomIsInCallCondition : public Condition<Ts...>, public Parented<IntercomApi> {
 public:
  bool check(const Ts &...x) override { return this->parent_->is_in_call(); }
};

template<typename... Ts>
class IntercomIsCallingCondition : public Condition<Ts...>, public Parented<IntercomApi> {
 public:
  bool check(const Ts &...x) override { return this->parent_->get_call_state() == CallState::CALLING; }
};

template<typename... Ts>
class IntercomIsIncomingCondition : public Condition<Ts...>, public Parented<IntercomApi> {
 public:
  bool check(const Ts &...x) override {
    return this->parent_->get_call_state() == CallState::RINGING;
  }
};

template<typename... Ts>
class IntercomIsInCallCondition : public Condition<Ts...>, public Parented<IntercomApi> {
 public:
  bool check(const Ts &...x) override {
    return this->parent_->get_call_state() == CallState::IN_CALL;
  }
};

template<typename... Ts>
class IntercomDestinationIsCondition : public Condition<Ts...>, public Parented<IntercomApi> {
 public:
  TEMPLATABLE_VALUE(std::string, destination)
  bool check(const Ts &...x) override {
    return this->parent_->get_current_destination() == this->destination_.value(x...);
  }
};

template<typename... Ts>
class IntercomIsHaDestinationCondition : public Condition<Ts...>, public Parented<IntercomApi> {
 public:
  bool check(const Ts &...x) override { return this->parent_->is_ha_destination(); }
};

}  // namespace intercom_api
}  // namespace esphome

#endif  // USE_ESP32
