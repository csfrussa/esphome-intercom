#pragma once

#include <string>

#include "esphome/core/component.h"

namespace esphome {
namespace intercom_simulator {

class IntercomSimulator : public Component {
 public:
  void set_source_profile(const std::string &source_profile) { this->source_profile_ = source_profile; }
  void setup() override;
  void loop() override;
  void dump_config() override;

 protected:
  std::string source_profile_;
};

}  // namespace intercom_simulator
}  // namespace esphome
