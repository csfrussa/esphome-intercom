#include "runtime_fsm.h"

#include "esphome/core/hal.h"
#include "esphome/core/log.h"

#include <cstdio>
#include <cstring>
#include <iterator>

#ifdef USE_RUNTIME_FSM_INTERCOM
#include "esphome/components/intercom_api/intercom_api.h"
#endif

namespace esphome::runtime_fsm {

static const char *const TAG = "runtime_fsm";

void RuntimeFsm::setup() {
  const ResolvedPolicies old_policies = this->resolved_policies_;
  const uint32_t old_mask = this->generic_activity_mask_;
  (void) this->sync_intercom_activity_();
  (void) this->apply_derived_activities_();
  this->apply_generic_outputs_();
  this->commit_outputs_("setup", old_mask, old_policies);
}

void RuntimeFsm::loop() {
  this->drain_pending_actions_();

  const uint32_t old_mask = this->generic_activity_mask_;
  const ResolvedPolicies old_policies = this->resolved_policies_;
  if (!this->sync_intercom_activity_())
    return;
  (void) this->apply_derived_activities_();
  this->apply_generic_outputs_();
  this->commit_outputs_("observer", old_mask, old_policies);
}

void RuntimeFsm::dump_config() {
  ESP_LOGCONFIG(TAG, "Runtime FSM:");
  ESP_LOGCONFIG(TAG, "  Activities: %u/%u", static_cast<unsigned>(this->activity_count_),
                static_cast<unsigned>(MAX_ACTIVITIES));
  ESP_LOGCONFIG(TAG, "  Actions: %u/%u", static_cast<unsigned>(this->action_count_),
                static_cast<unsigned>(MAX_ACTIONS));
  ESP_LOGCONFIG(TAG, "  Debug: %s", YESNO(this->debug_));
#ifdef USE_RUNTIME_FSM_INTERCOM
  ESP_LOGCONFIG(TAG, "  Intercom observer: %s", this->intercom_ != nullptr ? "configured" : "missing");
#endif
}

void RuntimeFsm::add_activity(const char *name, int16_t priority, bool initial) {
  if (name == nullptr || name[0] == '\0')
    return;
  if (this->activity_count_ >= MAX_ACTIVITIES) {
    ESP_LOGE(TAG, "Cannot add activity '%s': maximum %u activities reached", name,
             static_cast<unsigned>(MAX_ACTIVITIES));
    return;
  }
  ActivityConfig activity;
  activity.name = name;
  activity.bit = 1u << this->activity_count_;
  activity.priority = priority;
  activity.active = initial;
  this->activities_[this->activity_count_++] = activity;
}

void RuntimeFsm::set_activity_group(const char *activity_name, const char *group) {
  int index = this->find_activity_(activity_name);
  if (index < 0 || group == nullptr || group[0] == '\0')
    return;
  this->activities_[index].group = group;
}

void RuntimeFsm::add_activity_policy(const char *activity_name, const char *policy, const char *value) {
  int index = this->find_activity_(activity_name);
  if (index < 0) {
    ESP_LOGE(TAG, "Cannot add policy '%s': unknown activity '%s'", policy != nullptr ? policy : "-",
             activity_name != nullptr ? activity_name : "-");
    return;
  }
  auto &activity = this->activities_[index];
  if (activity.policy_count >= MAX_ACTIVITY_POLICIES) {
    ESP_LOGE(TAG, "Cannot add policy '%s' to activity '%s': maximum %u policies reached",
             policy != nullptr ? policy : "-", activity_name, static_cast<unsigned>(MAX_ACTIVITY_POLICIES));
    return;
  }
  activity.policies[activity.policy_count++] = PolicyValue{policy, value};
}

void RuntimeFsm::add_event_activity(const char *event, const char *activity, bool active) {
  if (event == nullptr || event[0] == '\0' || activity == nullptr || activity[0] == '\0')
    return;
  if (this->event_update_count_ >= this->event_updates_.size()) {
    ESP_LOGE(TAG, "Cannot add event update '%s:%s': maximum reached", event, activity);
    return;
  }
  this->event_updates_[this->event_update_count_++] = EventActivity{event, activity, active};
}

void RuntimeFsm::add_event_rule(const char *event, const char *action) {
  if (event == nullptr || event[0] == '\0')
    return;
  if (this->event_rule_count_ >= this->event_rules_.size()) {
    ESP_LOGE(TAG, "Cannot add event rule '%s': maximum reached", event);
    return;
  }
  this->event_rules_[this->event_rule_count_++] = EventRule{event, action};
}

void RuntimeFsm::add_event_rule_update(const char *activity, bool active) {
  if (this->event_rule_count_ == 0 || activity == nullptr || activity[0] == '\0')
    return;
  auto &rule = this->event_rules_[this->event_rule_count_ - 1];
  if (rule.update_count >= std::size(rule.updates)) {
    ESP_LOGE(TAG, "Cannot add event rule update '%s': maximum reached", activity);
    return;
  }
  rule.updates[rule.update_count++] = ActivityUpdate{activity, active};
}

void RuntimeFsm::add_event_rule_any_active(const char *activity) {
  if (this->event_rule_count_ == 0 || activity == nullptr || activity[0] == '\0')
    return;
  auto &rule = this->event_rules_[this->event_rule_count_ - 1];
  if (rule.any_count < std::size(rule.any_active))
    rule.any_active[rule.any_count++] = activity;
}

void RuntimeFsm::add_event_rule_all_active(const char *activity) {
  if (this->event_rule_count_ == 0 || activity == nullptr || activity[0] == '\0')
    return;
  auto &rule = this->event_rules_[this->event_rule_count_ - 1];
  if (rule.all_count < std::size(rule.all_active))
    rule.all_active[rule.all_count++] = activity;
}

void RuntimeFsm::add_event_rule_none_active(const char *activity) {
  if (this->event_rule_count_ == 0 || activity == nullptr || activity[0] == '\0')
    return;
  auto &rule = this->event_rules_[this->event_rule_count_ - 1];
  if (rule.none_count < std::size(rule.none_active))
    rule.none_active[rule.none_count++] = activity;
}

void RuntimeFsm::add_derived_activity(const char *activity) {
  if (activity == nullptr || activity[0] == '\0')
    return;
  if (this->derived_activity_count_ >= this->derived_activities_.size()) {
    ESP_LOGE(TAG, "Cannot add derived activity '%s': maximum reached", activity);
    return;
  }
  this->derived_activities_[this->derived_activity_count_++] = DerivedActivity{activity};
}

void RuntimeFsm::add_derived_any_active(const char *activity) {
  if (this->derived_activity_count_ == 0 || activity == nullptr || activity[0] == '\0')
    return;
  auto &derived = this->derived_activities_[this->derived_activity_count_ - 1];
  if (derived.any_count < std::size(derived.any_active))
    derived.any_active[derived.any_count++] = activity;
}

void RuntimeFsm::add_derived_all_active(const char *activity) {
  if (this->derived_activity_count_ == 0 || activity == nullptr || activity[0] == '\0')
    return;
  auto &derived = this->derived_activities_[this->derived_activity_count_ - 1];
  if (derived.all_count < std::size(derived.all_active))
    derived.all_active[derived.all_count++] = activity;
}

void RuntimeFsm::add_derived_none_active(const char *activity) {
  if (this->derived_activity_count_ == 0 || activity == nullptr || activity[0] == '\0')
    return;
  auto &derived = this->derived_activities_[this->derived_activity_count_ - 1];
  if (derived.none_count < std::size(derived.none_active))
    derived.none_active[derived.none_count++] = activity;
}

void RuntimeFsm::add_action_trigger(const char *name, Trigger<> *trigger) {
  if (name == nullptr || name[0] == '\0' || trigger == nullptr)
    return;
  if (this->action_count_ >= MAX_ACTIONS) {
    ESP_LOGE(TAG, "Cannot add action '%s': maximum %u actions reached", name, static_cast<unsigned>(MAX_ACTIONS));
    return;
  }
  this->actions_[this->action_count_++] = NamedAction{name, trigger};
}

void RuntimeFsm::add_policy_value_trigger(const char *policy, const char *value, Trigger<> *trigger) {
  if (policy == nullptr || policy[0] == '\0' || value == nullptr || trigger == nullptr)
    return;
  if (this->policy_value_action_count_ >= this->policy_value_actions_.size()) {
    ESP_LOGE(TAG, "Cannot add policy action '%s:%s': maximum reached", policy, value);
    return;
  }
  this->policy_value_actions_[this->policy_value_action_count_++] = PolicyValueAction{policy, value, trigger};
}

void RuntimeFsm::add_policy_output(const char *policy, const char *value, int32_t output) {
  if (policy == nullptr || policy[0] == '\0' || value == nullptr)
    return;
  if (this->policy_output_count_ >= this->policy_outputs_.size()) {
    ESP_LOGE(TAG, "Cannot add policy output '%s:%s': maximum reached", policy, value);
    return;
  }
  this->policy_outputs_[this->policy_output_count_++] = PolicyOutput{policy, value, output};
}

void RuntimeFsm::set_policy_change_trigger(const char *policy, Trigger<int32_t> *trigger) {
  if (policy == nullptr || policy[0] == '\0' || trigger == nullptr)
    return;
  if (this->policy_change_trigger_count_ >= this->policy_change_triggers_.size()) {
    ESP_LOGE(TAG, "Cannot add policy on_change '%s': maximum reached", policy);
    return;
  }
  this->policy_change_triggers_[this->policy_change_trigger_count_++] = PolicyChangeTrigger{policy, trigger};
}

void RuntimeFsm::on_intercom_event() {
  const uint32_t old_mask = this->generic_activity_mask_;
  const ResolvedPolicies old_policies = this->resolved_policies_;
  if (!this->sync_intercom_activity_())
    return;
  (void) this->apply_derived_activities_();
  this->apply_generic_outputs_();
  this->commit_outputs_("intercom_event", old_mask, old_policies);
}

void RuntimeFsm::event(const char *name) {
  if (this->dispatching_) {
    (void) this->enqueue_event_(name);
    return;
  }
  if (this->debug_) {
    ESP_LOGI(TAG, "EVENT seq=%" PRIu32 " name=%s mask=0x%08" PRIx32, this->sequence_,
             name != nullptr ? name : "-", this->generic_activity_mask_);
  }
  const uint32_t old_mask = this->generic_activity_mask_;
  const ResolvedPolicies old_policies = this->resolved_policies_;
  bool changed = false;
  bool event_known = false;

  for (size_t i = 0; i < this->event_rule_count_; i++) {
    auto &rule = this->event_rules_[i];
    if (rule.event == nullptr || name == nullptr || strcmp(rule.event, name) != 0)
      continue;
    event_known = true;
    if (!this->rule_matches_(rule))
      continue;
    for (size_t j = 0; j < rule.update_count; j++)
      changed |= this->apply_activity_update_(rule.updates[j].name, rule.updates[j].active);
    changed |= this->apply_derived_activities_();
    if (changed) {
      this->apply_generic_outputs_();
      this->commit_outputs_(name != nullptr ? name : "event", old_mask, old_policies);
    }
    this->run_named_action_(rule.action);
    return;
  }

  for (size_t i = 0; i < this->event_update_count_; i++) {
    const auto &update = this->event_updates_[i];
    if (update.event != nullptr && name != nullptr && strcmp(update.event, name) == 0) {
      event_known = true;
      changed |= this->apply_activity_update_(update.activity, update.active);
    }
  }
  if (changed) {
    changed |= this->apply_derived_activities_();
    this->apply_generic_outputs_();
    this->commit_outputs_(name != nullptr ? name : "event", old_mask, old_policies);
  }
  int action_index = this->find_action_(name);
  if (action_index >= 0) {
    this->run_named_action_(this->actions_[action_index].name);
  } else if (!event_known && !changed) {
    ESP_LOGW(TAG, "Ignoring unknown event '%s'", name != nullptr ? name : "-");
  }
}

void RuntimeFsm::set_activity(const char *name, bool active) {
  if (this->dispatching_) {
    (void) this->enqueue_activity_update_(name, active);
    return;
  }
  const uint32_t old_mask = this->generic_activity_mask_;
  const ResolvedPolicies old_policies = this->resolved_policies_;
  bool changed = this->apply_activity_update_(name, active);
  changed |= this->apply_derived_activities_();
  if (!changed)
    return;

  this->apply_generic_outputs_();
  this->commit_outputs_(name != nullptr ? name : "set_activity", old_mask, old_policies);
}

void RuntimeFsm::set_activities(const ActivityUpdate *updates, size_t count) {
  if (updates == nullptr || count == 0)
    return;
  if (this->dispatching_) {
    (void) this->enqueue_activity_updates_(updates, count);
    return;
  }

  const uint32_t old_mask = this->generic_activity_mask_;
  const ResolvedPolicies old_policies = this->resolved_policies_;
  bool changed = false;
  for (size_t i = 0; i < count; i++)
    changed |= this->apply_activity_update_(updates[i].name, updates[i].active);
  changed |= this->apply_derived_activities_();
  if (!changed)
    return;

  this->apply_generic_outputs_();
  this->commit_outputs_("set_activities", old_mask, old_policies);
}

void RuntimeFsm::request_action(const char *name) {
  this->run_named_action_(name);
}

void RuntimeFsm::dump_state(const char *reason) {
#ifdef USE_RUNTIME_FSM_DEBUG
  ESP_LOGI(TAG, "SNAPSHOT reason=%s seq=%" PRIu32 " mask=0x%08" PRIx32,
           reason != nullptr ? reason : "-", this->sequence_, this->generic_activity_mask_);
  for (size_t i = 0; i < this->activity_count_; i++) {
    const auto &activity = this->activities_[i];
    if (activity.active) {
      ESP_LOGI(TAG, "  activity %s priority=%d group=%s", activity.name != nullptr ? activity.name : "-",
               static_cast<int>(activity.priority), activity.group != nullptr ? activity.group : "-");
    }
  }
  for (size_t i = 0; i < this->resolved_policies_.value_count; i++) {
    const auto &policy = this->resolved_policies_.values[i];
    ESP_LOGI(TAG, "  policy %s=%s output=%" PRId32, policy.policy != nullptr ? policy.policy : "-",
             policy.value != nullptr ? policy.value : "-", this->resolve_policy_output_(policy.policy, policy.value));
  }
#ifdef USE_RUNTIME_FSM_INTERCOM
  if (this->intercom_ != nullptr) {
    ESP_LOGI(TAG, "  observed intercom=%s activity=%s", this->intercom_->get_call_state_str(),
             this->last_intercom_activity_[0] != '\0' ? this->last_intercom_activity_ : "-");
  }
#endif
}
#else
  (void) reason;
  if (this->debug_) {
    ESP_LOGI(TAG, "SNAPSHOT seq=%" PRIu32 " mask=0x%08" PRIx32, this->sequence_, this->generic_activity_mask_);
  }
}
#endif

bool RuntimeFsm::is_activity_active(const char *name) const {
  const int index = this->find_activity_(name);
  if (index < 0)
    return false;
  return this->activities_[index].active;
}

bool RuntimeFsm::rule_matches_(const RuntimeFsm::EventRule &rule) const {
  if (rule.any_count > 0) {
    bool any = false;
    for (size_t i = 0; i < rule.any_count; i++)
      any |= this->is_activity_active(rule.any_active[i]);
    if (!any)
      return false;
  }
  for (size_t i = 0; i < rule.all_count; i++) {
    if (!this->is_activity_active(rule.all_active[i]))
      return false;
  }
  for (size_t i = 0; i < rule.none_count; i++) {
    if (this->is_activity_active(rule.none_active[i]))
      return false;
  }
  return true;
}

bool RuntimeFsm::derived_matches_(const RuntimeFsm::DerivedActivity &derived) const {
  if (derived.any_count > 0) {
    bool any = false;
    for (size_t i = 0; i < derived.any_count; i++)
      any |= this->is_activity_active(derived.any_active[i]);
    if (!any)
      return false;
  }
  for (size_t i = 0; i < derived.all_count; i++) {
    if (!this->is_activity_active(derived.all_active[i]))
      return false;
  }
  for (size_t i = 0; i < derived.none_count; i++) {
    if (this->is_activity_active(derived.none_active[i]))
      return false;
  }
  return true;
}

bool RuntimeFsm::apply_derived_activities_() {
  bool changed = false;
  for (size_t i = 0; i < this->derived_activity_count_; i++) {
    const auto &derived = this->derived_activities_[i];
    changed |= this->set_activity_value_(derived.activity, this->derived_matches_(derived));
  }
  return changed;
}

void RuntimeFsm::publish_outputs_() {
  this->publish_state_outputs_();
  if (this->output_script_ != nullptr)
    this->output_script_->execute();
}

void RuntimeFsm::publish_state_outputs_() {
  if (this->activity_mask_output_.target != nullptr && this->activity_mask_output_.set != nullptr)
    this->activity_mask_output_.set(this->activity_mask_output_.target, this->generic_activity_mask_);
  if (this->sequence_output_.target != nullptr && this->sequence_output_.set != nullptr)
    this->sequence_output_.set(this->sequence_output_.target, this->sequence_);
}

void RuntimeFsm::apply_generic_outputs_() {
  std::array<GenericActivity, MAX_ACTIVITIES> generic{};
  for (size_t i = 0; i < this->activity_count_; i++) {
    generic[i].bit = this->activities_[i].bit;
    generic[i].priority = this->activities_[i].priority;
    generic[i].active = this->activities_[i].active;
    generic[i].policy_count = this->activities_[i].policy_count;
    for (size_t j = 0; j < this->activities_[i].policy_count; j++)
      generic[i].policies[j] = this->activities_[i].policies[j];
  }
  auto policies = reduce_generic_activities(generic.data(), this->activity_count_);
  this->generic_activity_mask_ = policies.mask;
  this->resolved_policies_ = policies;
}

bool RuntimeFsm::set_activity_value_(const char *name, bool active) {
  int index = this->find_activity_(name);
  if (index < 0) {
    ESP_LOGW(TAG, "Ignoring unknown activity '%s'", name != nullptr ? name : "-");
    return false;
  }

  ActivityConfig &activity = this->activities_[index];
  if (activity.active == active)
    return false;
  activity.active = active;
  return true;
}

bool RuntimeFsm::apply_activity_update_(const char *name, bool active) {
  int index = this->find_activity_(name);
  if (index < 0) {
    ESP_LOGW(TAG, "Ignoring unknown activity '%s'", name != nullptr ? name : "-");
    return false;
  }

  bool changed = false;
  ActivityConfig &activity = this->activities_[index];
  if (active && activity.group != nullptr && activity.group[0] != '\0') {
    for (size_t i = 0; i < this->activity_count_; i++) {
      if (i == static_cast<size_t>(index))
        continue;
      ActivityConfig &peer = this->activities_[i];
      if (peer.group != nullptr && strcmp(peer.group, activity.group) == 0 && peer.active) {
        peer.active = false;
        changed = true;
      }
    }
  }
  changed |= this->set_activity_value_(name, active);
  return changed;
}

bool RuntimeFsm::set_activity_value_if_known_(const char *name, bool active) {
  return this->find_activity_(name) >= 0 && this->apply_activity_update_(name, active);
}

void RuntimeFsm::commit_outputs_(const char *reason, uint32_t old_mask, const ResolvedPolicies &old_policies) {
  if (old_mask == this->generic_activity_mask_) {
    bool policy_changed = old_policies.value_count != this->resolved_policies_.value_count;
    for (size_t i = 0; !policy_changed && i < this->resolved_policies_.value_count; i++) {
      const char *policy = this->resolved_policies_.values[i].policy;
      const char *old_value = find_policy_value(old_policies, policy, nullptr);
      const char *new_value = this->resolved_policies_.values[i].value;
      policy_changed = old_value == nullptr || new_value == nullptr || strcmp(old_value, new_value) != 0;
    }
    if (!policy_changed)
      return;
  }

  this->sequence_++;
  if (this->debug_) {
    ESP_LOGI(TAG, "REDUCE seq=%" PRIu32 " reason=%s mask=0x%08" PRIx32 "->0x%08" PRIx32, this->sequence_,
             reason != nullptr ? reason : "-", old_mask, this->generic_activity_mask_);
  }
  const bool was_dispatching = this->dispatching_;
  this->dispatching_ = true;
  this->run_policy_actions_(old_policies, this->resolved_policies_);
  this->publish_outputs_();
  this->dispatching_ = was_dispatching;
  if (!this->dispatching_)
    this->drain_pending_events_();
}

void RuntimeFsm::build_intercom_activity_name_(const char *state) {
  this->intercom_activity_[0] = '\0';
#ifdef USE_RUNTIME_FSM_INTERCOM
  if (this->intercom_activity_prefix_ == nullptr || this->intercom_activity_prefix_[0] == '\0' || state == nullptr ||
      state[0] == '\0')
    return;
  std::snprintf(this->intercom_activity_, sizeof(this->intercom_activity_), "%s%s", this->intercom_activity_prefix_,
                state);
#else
  (void) state;
#endif
}

bool RuntimeFsm::sync_intercom_activity_() {
#ifdef USE_RUNTIME_FSM_INTERCOM
  if (this->intercom_ == nullptr || this->intercom_activity_prefix_ == nullptr)
    return false;

  this->build_intercom_activity_name_(this->intercom_->get_call_state_str());
  if (std::strcmp(this->intercom_activity_, this->last_intercom_activity_) == 0)
    return false;

  bool changed = false;
  if (this->last_intercom_activity_[0] != '\0')
    changed |= this->set_activity_value_if_known_(this->last_intercom_activity_, false);
  if (this->intercom_activity_[0] != '\0')
    changed |= this->set_activity_value_if_known_(this->intercom_activity_, true);

  std::snprintf(this->last_intercom_activity_, sizeof(this->last_intercom_activity_), "%s", this->intercom_activity_);
  return changed;
#else
  return false;
#endif
}

void RuntimeFsm::run_policy_actions_(const ResolvedPolicies &old_policies, const ResolvedPolicies &new_policies) {
  for (size_t i = 0; i < new_policies.value_count; i++) {
    const char *policy = new_policies.values[i].policy;
    const char *value = new_policies.values[i].value;
    const char *old_value = find_policy_value(old_policies, policy, nullptr);
    if (old_value != nullptr && value != nullptr && strcmp(old_value, value) == 0)
      continue;
    for (size_t j = 0; j < this->policy_value_action_count_; j++) {
      const auto &action = this->policy_value_actions_[j];
      if (action.policy != nullptr && action.value != nullptr && strcmp(action.policy, policy) == 0 &&
          strcmp(action.value, value) == 0) {
        if (this->debug_)
          ESP_LOGI(TAG, "POLICY seq=%" PRIu32 " %s=%s", this->sequence_, policy, value);
        action.trigger->trigger();
      }
    }
    const int32_t output = this->resolve_policy_output_(policy, value);
    for (size_t j = 0; j < this->policy_global_output_count_; j++) {
      const auto &target = this->policy_global_outputs_[j];
      if (target.policy != nullptr && target.set != nullptr && strcmp(target.policy, policy) == 0) {
        target.set(target.target, output);
      }
    }
    for (size_t j = 0; j < this->policy_change_trigger_count_; j++) {
      const auto &trigger = this->policy_change_triggers_[j];
      if (trigger.policy != nullptr && strcmp(trigger.policy, policy) == 0) {
        if (this->debug_) {
          ESP_LOGI(TAG, "POLICY_CHANGE seq=%" PRIu32 " %s=%s output=%" PRId32, this->sequence_, policy, value,
                   output);
        }
        trigger.trigger->trigger(output);
      }
    }
  }
}

int32_t RuntimeFsm::resolve_policy_output_(const char *policy, const char *value) const {
  if (policy == nullptr || value == nullptr)
    return 0;
  for (size_t i = 0; i < this->policy_output_count_; i++) {
    const auto &entry = this->policy_outputs_[i];
    if (entry.policy != nullptr && entry.value != nullptr && strcmp(entry.policy, policy) == 0 &&
        strcmp(entry.value, value) == 0)
      return entry.output;
  }
  return 0;
}

int RuntimeFsm::find_activity_(const char *name) const {
  if (name == nullptr)
    return -1;
  for (size_t i = 0; i < this->activity_count_; i++) {
    if (strcmp(this->activities_[i].name, name) == 0)
      return static_cast<int>(i);
  }
  return -1;
}

int RuntimeFsm::find_action_(const char *name) const {
  if (name == nullptr)
    return -1;
  for (size_t i = 0; i < this->action_count_; i++) {
    if (strcmp(this->actions_[i].name, name) == 0)
      return static_cast<int>(i);
  }
  return -1;
}

bool RuntimeFsm::enqueue_event_(const char *name) {
  if (name == nullptr || name[0] == '\0')
    return false;
  if (this->pending_event_count_ >= this->pending_events_.size()) {
    ESP_LOGE(TAG, "Cannot queue event '%s': queue full", name);
    return false;
  }
  auto &event = this->pending_events_[this->pending_event_count_++];
  event = PendingEvent{};
  event.kind = PendingEventKind::EVENT;
  std::snprintf(event.name, sizeof(event.name), "%s", name);
  if (this->debug_)
    ESP_LOGI(TAG, "EVENT_QUEUE seq=%" PRIu32 " name=%s", this->sequence_, event.name);
  return true;
}

bool RuntimeFsm::enqueue_activity_update_(const char *name, bool active) {
  ActivityUpdate update{name, active};
  return this->enqueue_activity_updates_(&update, 1);
}

bool RuntimeFsm::enqueue_activity_updates_(const ActivityUpdate *updates, size_t count) {
  if (updates == nullptr || count == 0)
    return false;
  if (this->pending_event_count_ >= this->pending_events_.size()) {
    ESP_LOGE(TAG, "Cannot queue activity update: queue full");
    return false;
  }
  auto &event = this->pending_events_[this->pending_event_count_++];
  event = PendingEvent{};
  event.kind = PendingEventKind::SET_ACTIVITIES;
  event.update_count = count > std::size(event.updates) ? std::size(event.updates) : count;
  for (size_t i = 0; i < event.update_count; i++)
    event.updates[i] = updates[i];
  if (this->debug_)
    ESP_LOGI(TAG, "SET_QUEUE seq=%" PRIu32 " count=%u", this->sequence_, static_cast<unsigned>(event.update_count));
  return true;
}

void RuntimeFsm::drain_pending_events_() {
  while (!this->dispatching_ && this->pending_event_count_ > 0) {
    PendingEvent event = this->pending_events_[0];
    for (size_t i = 1; i < this->pending_event_count_; i++)
      this->pending_events_[i - 1] = this->pending_events_[i];
    this->pending_events_[--this->pending_event_count_] = PendingEvent{};

    if (event.kind == PendingEventKind::EVENT) {
      this->event(event.name);
    } else {
      this->set_activities(event.updates, event.update_count);
    }
  }
}

void RuntimeFsm::run_named_action_(const char *name) {
  if (name == nullptr || name[0] == '\0')
    return;
  if (this->find_action_(name) < 0) {
    ESP_LOGW(TAG, "Ignoring unknown action '%s'", name);
    return;
  }
  for (size_t i = 0; i < this->pending_action_count_; i++) {
    if (this->pending_actions_[i] != nullptr && strcmp(this->pending_actions_[i], name) == 0) {
      if (this->debug_)
        ESP_LOGI(TAG, "ACTION_SKIP_DUP seq=%" PRIu32 " name=%s", this->sequence_, name);
      return;
    }
  }
  if (this->pending_action_count_ >= this->pending_actions_.size()) {
    ESP_LOGE(TAG, "Cannot queue action '%s': queue full", name);
    return;
  }
  if (this->debug_)
    ESP_LOGI(TAG, "ACTION_QUEUE seq=%" PRIu32 " name=%s", this->sequence_, name);
  this->pending_actions_[this->pending_action_count_++] = name;
}

void RuntimeFsm::execute_named_action_(const char *name) {
  int index = this->find_action_(name);
  if (index < 0) {
    ESP_LOGW(TAG, "Ignoring unknown queued action '%s'", name != nullptr ? name : "-");
    return;
  }
  if (this->debug_)
    ESP_LOGI(TAG, "ACTION_RUN seq=%" PRIu32 " name=%s", this->sequence_, this->actions_[index].name);
  this->actions_[index].trigger->trigger();
}

void RuntimeFsm::drain_pending_actions_() {
  while (this->pending_action_count_ > 0) {
    const char *name = this->pending_actions_[0];
    for (size_t i = 1; i < this->pending_action_count_; i++)
      this->pending_actions_[i - 1] = this->pending_actions_[i];
    this->pending_actions_[--this->pending_action_count_] = nullptr;
    this->execute_named_action_(name);
  }
}

}  // namespace esphome::runtime_fsm
