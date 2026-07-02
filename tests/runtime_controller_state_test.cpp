#include "../esphome/components/runtime_controller/runtime_controller_state.h"

#include <array>
#include <cstdlib>
#include <iostream>
#include <string>

using namespace esphome::runtime_controller;

struct NamedActivity {
  const char *name;
  GenericActivity activity;
};

static PolicyValue policy(const char *name, const char *value) { return PolicyValue{name, value}; }

static GenericActivity activity(uint32_t bit, int16_t priority, std::initializer_list<PolicyValue> policies) {
  GenericActivity result;
  result.bit = bit;
  result.priority = priority;
  size_t i = 0;
  for (auto item : policies) {
    if (i >= MAX_ACTIVITY_POLICIES)
      break;
    result.policies[i++] = item;
  }
  result.policy_count = i;
  return result;
}

template<size_t N> static void set(std::array<NamedActivity, N> &activities, const char *name, bool active) {
  for (auto &entry : activities) {
    if (std::string(entry.name) == name) {
      entry.activity.active = active;
      return;
    }
  }
  std::cerr << "unknown activity " << name << "\n";
  std::exit(1);
}

template<size_t N> static ResolvedPolicies eval(const std::array<NamedActivity, N> &activities) {
  std::array<GenericActivity, N> generic{};
  for (size_t i = 0; i < N; i++)
    generic[i] = activities[i].activity;
  return reduce_generic_activities(generic.data(), generic.size());
}

static void expect_policy(const char *label, const ResolvedPolicies &actual, const char *name, const char *expected) {
  const char *got = find_policy_value(actual, name, nullptr);
  if ((got == nullptr && expected != nullptr) || (got != nullptr && expected == nullptr) ||
      (got != nullptr && expected != nullptr && std::string(got) != expected)) {
    std::cerr << label << " failed: " << name << "=" << (got != nullptr ? got : "<none>")
              << " expected=" << (expected != nullptr ? expected : "<none>") << "\n";
    std::exit(1);
  }
}

static void expect_mask(const char *label, const ResolvedPolicies &actual, uint32_t expected) {
  if (actual.mask != expected) {
    std::cerr << label << " failed: mask=0x" << std::hex << actual.mask << " expected=0x" << expected << "\n";
    std::exit(1);
  }
}

static void esp_like_combinations() {
  std::array<NamedActivity, 13> activities{{
      {"boot", activity(1u << 0, 1000, {policy("led_status", "boot"), policy("display_status", "boot")})},
      {"no_wifi", activity(1u << 1, 990, {policy("led_status", "no_wifi"), policy("display_status", "no_wifi")})},
      {"no_ha", activity(1u << 2, 980, {policy("led_status", "no_ha"), policy("display_status", "no_ha")})},
      {"muted", activity(1u << 3, 650, {policy("led_status", "mic_muted"), policy("display_status", "muted")})},
      {"media", activity(1u << 4, 100, {policy("led_status", "media"), policy("display_status", "media"), policy("audio_policy", "normal")})},
      {"announcement", activity(1u << 5, 200, {policy("led_status", "announcement"), policy("display_status", "announcement"), policy("audio_policy", "duck")})},
      {"voip_ringing", activity(1u << 6, 975, {policy("led_status", "ringing"), policy("display_status", "voip_ringing"), policy("audio_policy", "duck")})},
      {"voip_in_call", activity(1u << 7, 700, {policy("led_status", "voip"), policy("display_status", "voip_in_call"), policy("audio_policy", "duck")})},
      {"assistant_thinking", activity(1u << 8, 830, {policy("led_status", "thinking"), policy("display_status", "thinking"), policy("audio_policy", "duck")})},
      {"assistant_response", activity(1u << 9, 840, {policy("led_status", "responding"), policy("display_status", "responding"), policy("audio_policy", "duck")})},
      {"screen_dim", activity(1u << 10, 50, {policy("screen_policy", "dim")})},
      {"timer_ringing", activity(1u << 11, 500, {policy("audio_policy", "duck"), policy("timer_alarm", "play")})},
      {"voip_ringtone", activity(1u << 12, 700, {policy("audio_policy", "duck"), policy("ringtone", "play")})},
  }};

  auto out = eval(activities);
  expect_mask("idle", out, 0);
  expect_policy("idle", out, "led_status", nullptr);

  set(activities, "media", true);
  out = eval(activities);
  expect_mask("media", out, 1u << 4);
  expect_policy("media", out, "led_status", "media");
  expect_policy("media", out, "display_status", "media");
  expect_policy("media", out, "audio_policy", "normal");

  set(activities, "screen_dim", true);
  out = eval(activities);
  expect_mask("media+screen", out, (1u << 4) | (1u << 10));
  expect_policy("media+screen", out, "led_status", "media");
  expect_policy("media+screen", out, "screen_policy", "dim");

  set(activities, "voip_in_call", true);
  out = eval(activities);
  expect_mask("media+voip", out, (1u << 4) | (1u << 7) | (1u << 10));
  expect_policy("media+voip", out, "led_status", "voip");
  expect_policy("media+voip", out, "display_status", "voip_in_call");
  expect_policy("media+voip", out, "audio_policy", "duck");
  expect_policy("media+voip", out, "screen_policy", "dim");

  set(activities, "assistant_response", true);
  out = eval(activities);
  expect_mask("media+voip+response", out, (1u << 4) | (1u << 7) | (1u << 9) | (1u << 10));
  expect_policy("assistant response overlays in-call led", out, "led_status", "responding");
  expect_policy("assistant response overlays in-call display", out, "display_status", "responding");
  expect_policy("media+voip+response", out, "audio_policy", "duck");

  set(activities, "assistant_response", false);
  set(activities, "muted", true);
  out = eval(activities);
  expect_mask("voip overrides muted visual", out, (1u << 3) | (1u << 4) | (1u << 7) | (1u << 10));
  expect_policy("voip muted led", out, "led_status", "voip");
  expect_policy("voip muted display", out, "display_status", "voip_in_call");
  expect_policy("muted keeps audio policy", out, "audio_policy", "duck");

  set(activities, "boot", true);
  out = eval(activities);
  expect_policy("boot led", out, "led_status", "boot");
  expect_policy("boot display", out, "display_status", "boot");
  expect_policy("boot keeps independent screen policy", out, "screen_policy", "dim");

  set(activities, "boot", false);
  set(activities, "no_ha", true);
  out = eval(activities);
  expect_policy("no_ha led", out, "led_status", "no_ha");
  expect_policy("no_ha display", out, "display_status", "no_ha");

  set(activities, "no_ha", false);
  set(activities, "muted", false);
  set(activities, "assistant_response", false);
  set(activities, "voip_in_call", false);
  set(activities, "voip_ringing", true);
  out = eval(activities);
  expect_mask("ringing+media+screen", out, (1u << 4) | (1u << 6) | (1u << 10));
  expect_policy("ringing led", out, "led_status", "ringing");
  expect_policy("ringing display", out, "display_status", "voip_ringing");
  expect_policy("ringing audio", out, "audio_policy", "duck");

  set(activities, "announcement", true);
  out = eval(activities);
  expect_policy("ringing overrides announcement led", out, "led_status", "ringing");
  expect_policy("ringing overrides announcement display", out, "display_status", "voip_ringing");
  expect_policy("ringing starts ringtone", out, "ringtone", nullptr);

  set(activities, "voip_ringtone", true);
  out = eval(activities);
  expect_policy("voip ringtone policy", out, "ringtone", "play");

  set(activities, "voip_ringing", false);
  set(activities, "voip_ringtone", false);
  set(activities, "assistant_thinking", true);
  out = eval(activities);
  expect_policy("thinking overrides announcement led", out, "led_status", "thinking");
  expect_policy("thinking overrides announcement display", out, "display_status", "thinking");
  expect_policy("thinking audio", out, "audio_policy", "duck");

  set(activities, "timer_ringing", true);
  out = eval(activities);
  expect_policy("timer alarm policy", out, "timer_alarm", "play");
  expect_policy("timer keeps ducking", out, "audio_policy", "duck");
}

static void explicit_stop_policies() {
  std::array<NamedActivity, 2> activities{{
      {"idle", activity(1u << 0, 0, {policy("timer_alarm", "stop"), policy("ringtone", "stop")})},
      {"timer_ringing", activity(1u << 1, 500, {policy("timer_alarm", "play")})},
  }};

  set(activities, "idle", true);
  auto out = eval(activities);
  expect_policy("idle declares timer stop", out, "timer_alarm", "stop");
  expect_policy("idle declares ringtone stop", out, "ringtone", "stop");

  set(activities, "timer_ringing", true);
  out = eval(activities);
  expect_policy("ringing overrides idle timer stop", out, "timer_alarm", "play");

  set(activities, "timer_ringing", false);
  out = eval(activities);
  expect_policy("timer stop returns after ringing", out, "timer_alarm", "stop");
}

int main() {
  esp_like_combinations();
  explicit_stop_policies();
  std::cout << "runtime_controller generic policy reducer tests passed\n";
}
