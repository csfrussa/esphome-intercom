#include "intercom_simulator.h"

#include "esphome/core/log.h"

namespace esphome {
namespace intercom_simulator {

static const char *const TAG = "intercom_simulator";

void IntercomSimulator::setup() {
  ESP_LOGW(TAG, "Phase 00V simulator backend is scaffolded but not implemented yet");
}

void IntercomSimulator::loop() {}

void IntercomSimulator::dump_config() {
  ESP_LOGCONFIG(TAG, "Intercom Simulator:");
  ESP_LOGCONFIG(TAG, "  Source profile: %s", this->source_profile_.c_str());
}

}  // namespace intercom_simulator
}  // namespace esphome
