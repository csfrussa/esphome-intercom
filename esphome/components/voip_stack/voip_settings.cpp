#include "voip_stack.h"

#ifdef USE_ESP32

#include <algorithm>
#include <cctype>
#include <cmath>

#include "cJSON.h"
#include "esphome/core/helpers.h"
#include "esphome/core/log.h"

namespace esphome {
namespace voip_stack {

static const char *const TAG = "voip_stack.settings";

namespace {

struct ParsedPhonebookSlot {
  ContactEntry entry;
};

struct JsonRosterSlot {
  std::string name;
  std::string number;
  std::string kind;
  std::string address;
  std::string endpoint_kind;
  std::string sip_transport;
  bool ha_bridge{false};
  uint16_t sip_port{0};
  uint16_t rtp_port{0};
};

float db_to_linear(float db) {
  return std::pow(10.0f, db / 20.0f);
}

float clamp_volume_(float volume) {
  if (!std::isfinite(volume)) return 0.0f;
  return std::max(0.0f, std::min(1.0f, volume));
}

float clamp_mic_gain_db_(float db) {
  if (!std::isfinite(db)) return 0.0f;
  return std::max(-20.0f, std::min(20.0f, db));
}

bool parse_slot_for_normalize(const std::string &raw, ParsedPhonebookSlot *slot) {
  if (!Phonebook::parse_entry(raw, &slot->entry)) return false;
  return slot->entry.endpoint_kind == ContactEndpointKind::UNKNOWN ||
         slot->entry.endpoint_kind == ContactEndpointKind::SIP;
}

void append_csv(std::string *out, const std::string &entry) {
  if (entry.empty()) return;
  if (!out->empty()) out->push_back(',');
  *out += entry;
}

std::string serialize_endpoint(const std::string &name, ContactEndpointKind endpoint_kind,
                               const std::string &ip, uint16_t port,
                               uint16_t rtp_port, bool sip_transport_tcp = false) {
  if (name.empty()) return "";
  if (ip.empty() || port == 0) return name;
  if (endpoint_kind == ContactEndpointKind::SIP) {
    return name + "|" + ip + "|" + std::to_string(port) + "|" +
           std::to_string(rtp_port) + "|" + (sip_transport_tcp ? "sip_tcp" : "sip_udp");
  }
  return name;
}

const char *json_string(const cJSON *obj, const char *key) {
  const cJSON *item = cJSON_GetObjectItemCaseSensitive(obj, key);
  return cJSON_IsString(item) && item->valuestring != nullptr ? item->valuestring : "";
}

bool json_bool(const cJSON *obj, const char *key) {
  const cJSON *item = cJSON_GetObjectItemCaseSensitive(obj, key);
  return cJSON_IsTrue(item);
}

uint16_t json_u16(const cJSON *obj, const char *key, uint16_t default_value = 0) {
  const cJSON *item = cJSON_GetObjectItemCaseSensitive(obj, key);
  if (cJSON_IsNumber(item) && item->valuedouble >= 0 && item->valuedouble <= 65535) {
    return static_cast<uint16_t>(item->valuedouble);
  }
  if (cJSON_IsString(item) && item->valuestring != nullptr) {
    uint16_t parsed = 0;
    if (Phonebook::parse_u16(Phonebook::trim(item->valuestring), &parsed)) return parsed;
  }
  return default_value;
}

std::string json_metadata_string(const cJSON *obj, const char *key) {
  const cJSON *meta = cJSON_GetObjectItemCaseSensitive(obj, "metadata");
  if (!cJSON_IsObject(meta)) return "";
  return json_string(meta, key);
}

uint16_t json_metadata_u16(const cJSON *obj, const char *key, uint16_t default_value = 0) {
  const cJSON *meta = cJSON_GetObjectItemCaseSensitive(obj, "metadata");
  if (!cJSON_IsObject(meta)) return default_value;
  return json_u16(meta, key, default_value);
}

bool parse_json_roster_slot(const cJSON *obj, JsonRosterSlot *slot) {
  if (!cJSON_IsObject(obj)) return false;
  std::string id = Phonebook::trim(json_string(obj, "id"));
  std::string name = Phonebook::trim(json_string(obj, "name"));
  if (name.empty()) name = id;
  if (id.empty()) id = name;
  if (name.empty()) return false;

  slot->name = name;
  slot->kind = Phonebook::trim(json_string(obj, "kind"));
  if (slot->kind.empty()) {
    ESP_LOGW(TAG, "Ignoring roster JSON entry '%s': kind is required", name.c_str());
    return false;
  }
  std::transform(slot->kind.begin(), slot->kind.end(), slot->kind.begin(), ::tolower);
  if (slot->kind == "softphone" && !id.empty()) {
    name = id;
    slot->name = id;
  }
  slot->number = Phonebook::trim(json_string(obj, "number"));
  slot->address = Phonebook::trim(json_string(obj, "address"));
  if (slot->address.empty()) slot->address = Phonebook::trim(json_string(obj, "host"));
  slot->ha_bridge = json_bool(obj, "ha_bridge");
  if (slot->kind == "ha" || slot->kind == "esp" || slot->kind == "softphone") {
    slot->endpoint_kind = "sip";
  }
  std::transform(slot->endpoint_kind.begin(), slot->endpoint_kind.end(), slot->endpoint_kind.begin(), ::tolower);
  slot->sip_transport = Phonebook::trim(json_metadata_string(obj, "sip_transport"));
  if (slot->sip_transport.empty()) {
    slot->sip_transport = Phonebook::trim(json_metadata_string(obj, "signaling_transport"));
  }
  if (slot->sip_transport.empty()) {
    slot->sip_transport = Phonebook::trim(json_string(obj, "sip_transport"));
  }
  if (slot->sip_transport.empty()) {
    const std::string sip_uri = Phonebook::trim(json_string(obj, "sip_uri"));
    const std::string marker = "transport=";
    const size_t pos = sip_uri.find(marker);
    if (pos != std::string::npos) {
      size_t start = pos + marker.size();
      size_t end = sip_uri.find(';', start);
      slot->sip_transport = sip_uri.substr(start, end == std::string::npos ? std::string::npos : end - start);
    }
  }
  std::transform(slot->sip_transport.begin(), slot->sip_transport.end(), slot->sip_transport.begin(), ::tolower);
  if (slot->sip_transport != "tcp" && slot->sip_transport != "udp") slot->sip_transport.clear();
  slot->sip_port = json_metadata_u16(obj, "sip_port", json_u16(obj, "sip_port", 5060));
  slot->rtp_port = json_metadata_u16(obj, "rtp_port", json_u16(obj, "rtp_port", 40000));
  return true;
}

}  // namespace

// === Settings persistence ===

void VoipStack::load_settings_() {
  this->settings_pref_ = global_preferences->make_preference<StoredSettings>(fnv1_hash("voip_stack_settings"));

  StoredSettings stored;
  if (this->settings_pref_.load(&stored) && stored.version == SETTINGS_VERSION) {
    this->suppress_save_ = true;

    // Volume only when voip_stack owns the master_volume number;
    // template-number setups own their own DAC + AEC sync.
    const float volume = clamp_volume_(stored.volume_pct / 100.0f);
    this->volume_.store(volume, std::memory_order_relaxed);
    if (this->volume_number_ != nullptr) {
#ifdef USE_ESPHOME_VOIP_STACK_SPEAKER
      if (this->speaker_ != nullptr) {
        this->speaker_->set_volume(volume);
      }
#endif
      ESP_LOGD(TAG, "Loaded volume: %.0f%%", volume * 100.0f);
    }

    // Skip mic_gain when esp_audio_stack owns it (its own persistence).
    this->mic_gain_db_ = clamp_mic_gain_db_(stored.mic_gain_db);
    if (this->mic_gain_number_ != nullptr && this->has_microphone_()) {
      if (this->mic_gain_db_ != 0.0f && !this->ensure_mic_processing_buffer_()) {
        ESP_LOGE(TAG, "Stored mic_gain %.1fdB ignored: processing buffer unavailable", this->mic_gain_db_);
        this->mic_gain_db_ = 0.0f;
      }
      this->mic_gain_.store(db_to_linear(this->mic_gain_db_), std::memory_order_relaxed);
      ESP_LOGD(TAG, "Loaded mic_gain: %.1fdB", this->mic_gain_db_);
    }

    // auto_answer / AEC use switch restore_mode, not this struct.
    this->suppress_save_ = false;
  } else {
    ESP_LOGD(TAG, "No saved settings, using defaults");
  }
}

void VoipStack::schedule_save_settings_() {
  if (this->suppress_save_ || this->save_scheduled_) {
    return;
  }
  this->save_scheduled_ = true;
  // 250 ms debounce against slider drag.
  this->set_timeout(SCHED_SAVE_SETTINGS, 250, [this]() {
    this->save_scheduled_ = false;
    this->save_settings_();
  });
}

void VoipStack::save_settings_() {
  StoredSettings stored;
  stored.version = SETTINGS_VERSION;
  const float volume = clamp_volume_(this->volume_.load(std::memory_order_relaxed));
  stored.volume_pct = static_cast<uint8_t>(
      std::lround(volume * 100.0f));
  stored.mic_gain_db = static_cast<int8_t>(std::lround(clamp_mic_gain_db_(this->mic_gain_db_)));

  this->settings_pref_.save(&stored);
  ESP_LOGD(TAG, "Saved settings: vol=%d%%, mic=%ddB",
           stored.volume_pct, stored.mic_gain_db);
}

// === User-facing setters ===

void VoipStack::set_volume(float volume) {
  volume = clamp_volume_(volume);
  this->volume_.store(volume, std::memory_order_relaxed);
#ifdef USE_ESPHOME_VOIP_STACK_SPEAKER
  if (this->speaker_ != nullptr) {
    this->speaker_->set_volume(volume);
  }
#endif
  this->schedule_save_settings_();
}

void VoipStack::set_auto_answer(bool enabled) {
  this->auto_answer_ = enabled;
  ESP_LOGI(TAG, "Auto-answer set to %s", enabled ? "ON" : "OFF");
  // Persisted via switch restore_mode, not save_settings_.
}

void VoipStack::set_do_not_disturb(bool enabled) {
  this->do_not_disturb_ = enabled;
  ESP_LOGI(TAG, "Do-not-disturb set to %s", enabled ? "ON" : "OFF");
  // Persisted via switch restore_mode, not save_settings_.
}

void VoipStack::set_mic_gain_db(float db) {
  if (!this->has_microphone_()) {
    ESP_LOGW(TAG, "Ignoring mic_gain: this voip endpoint has no microphone");
    return;
  }
  // gain = 10^(dB/20); clamp to -20..+20 dB.
  db = clamp_mic_gain_db_(db);
  if (db != 0.0f && !this->ensure_mic_processing_buffer_()) {
    ESP_LOGE(TAG, "Mic gain %.1f dB ignored: processing buffer unavailable", db);
    return;
  }
  this->mic_gain_db_ = db;
  const float gain = db_to_linear(db);
  this->mic_gain_.store(gain, std::memory_order_relaxed);
  ESP_LOGD(TAG, "Mic gain set to %.1f dB (%.2fx)", db, gain);
  this->schedule_save_settings_();
}

// === Contacts ===
// Phonebook owns merge logic. add/set route through the same idempotent
// merge: same shape = noop, missing endpoint = upgrade, mismatch = replace.
// Slot order is stable; only flush_contacts wipes.

void VoipStack::add_contact(const std::string &entry) {
  this->phonebook_.set_self_name(this->device_name_);
  const std::string before = this->phonebook_.current_name();
  const std::string normalized_entry = this->normalize_phonebook_for_transport_(entry);
  const AddResult r = this->phonebook_.add_one(normalized_entry);
  switch (r) {
    case AddResult::Added:
      ESP_LOGI(TAG, "Contact added: %s", normalized_entry.c_str());
      this->publish_contacts_();
      if (this->phonebook_.current_name() != before) this->publish_destination_();
      break;
    case AddResult::Upgraded:
    case AddResult::EndpointReplaced:
      ESP_LOGI(TAG, "Contact endpoint updated: %s", normalized_entry.c_str());
      this->publish_contacts_();
      break;
    case AddResult::Noop:
    case AddResult::Rejected:
      break;
  }
}

void VoipStack::remove_contact(const std::string &name) {
  const std::string before = this->phonebook_.current_name();
  if (!this->phonebook_.remove_one(name)) {
    return;
  }
  ESP_LOGI(TAG, "Contact removed: %s", name.c_str());
  this->publish_contacts_();
  if (this->phonebook_.current_name() != before) this->publish_destination_();
}

void VoipStack::set_contacts(const std::string &contacts_csv) {
  this->phonebook_.set_self_name(this->device_name_);
  const std::string trimmed = Phonebook::trim(contacts_csv);
  if (!trimmed.empty() && (trimmed[0] == '{' || trimmed[0] == '[')) {
    const bool changed = this->apply_roster_json_contacts_(trimmed);
    const bool selected = this->maybe_auto_select_ha_first_();
    if (changed || selected) {
      ESP_LOGI(TAG, "JSON contacts applied: %zu total", this->phonebook_.size());
      this->publish_destination_();
      if (changed) this->publish_contacts_();
    }
    return;
  }
  const std::string normalized_csv = this->normalize_phonebook_for_transport_(contacts_csv);
  if (normalized_csv.empty()) return;
  // Inside an open update cycle (started by update_contacts()), record names
  // so commit_cycle_() can reset their counters. Outside a cycle this is a
  // no-op - pruning only applies to the official update_contacts() flow.
  if (this->cycle_active_) this->track_csv_(normalized_csv);
  // Just add_one per entry; for a clean slate call flush_contacts() first.
  const bool changed = this->phonebook_.add_batch(normalized_csv);
  const bool selected = this->maybe_auto_select_ha_first_();
  if (changed || selected) {
    ESP_LOGI(TAG, "Contacts batch applied: %zu total", this->phonebook_.size());
    this->publish_destination_();
    if (changed) this->publish_contacts_();
  }
}

std::string VoipStack::normalize_phonebook_for_transport_(const std::string &contacts_csv) {
  const auto raw_entries = Phonebook::split(contacts_csv, ',');
  std::vector<ParsedPhonebookSlot> slots;
  slots.reserve(raw_entries.size());

  for (const auto &raw : raw_entries) {
    ParsedPhonebookSlot slot;
    if (!parse_slot_for_normalize(raw, &slot)) {
      continue;
    }
    slots.push_back(slot);
  }

  if (slots.empty()) return contacts_csv;

  std::string out;
  for (const auto &slot : slots) {
    const auto &entry = slot.entry;

    if (entry.endpoint_kind == ContactEndpointKind::SIP) {
      append_csv(&out, serialize_endpoint(entry.name, entry.endpoint_kind, entry.ip,
                                          entry.port, entry.rtp_port,
                                          entry.sip_transport_tcp));
      continue;
    }

    append_csv(&out, entry.name);
  }

  return out;
}

std::string VoipStack::normalize_roster_json_for_transport_(const std::string &roster_json) {
  if (roster_json.empty()) return "";

  cJSON *root = cJSON_ParseWithLength(roster_json.data(), roster_json.size());
  if (root == nullptr) {
    ESP_LOGW(TAG, "Ignoring HA roster_json: invalid JSON");
    return "";
  }

  const cJSON *contacts = nullptr;
  if (cJSON_IsArray(root)) {
    contacts = root;
  } else if (cJSON_IsObject(root)) {
    contacts = cJSON_GetObjectItemCaseSensitive(root, "contacts");
    if (!cJSON_IsArray(contacts)) contacts = cJSON_GetObjectItemCaseSensitive(root, "entries");
  }
  if (!cJSON_IsArray(contacts)) {
    ESP_LOGW(TAG, "Ignoring HA roster_json: missing contacts array");
    cJSON_Delete(root);
    return "";
  }

  std::vector<JsonRosterSlot> slots;
  slots.reserve(std::min(static_cast<size_t>(cJSON_GetArraySize(contacts)), Phonebook::MAX_CONTACTS));
  JsonRosterSlot ha_slot;
  bool has_ha = false;

  const cJSON *item = nullptr;
  cJSON_ArrayForEach(item, contacts) {
    if (slots.size() >= Phonebook::MAX_CONTACTS) break;
    JsonRosterSlot slot;
    if (!parse_json_roster_slot(item, &slot)) continue;
    if (slot.name == this->device_name_ || slot.name == this->device_route_id_) continue;
    slots.push_back(slot);
    if (slot.kind == "ha" && !slot.address.empty()) {
      ha_slot = slot;
      has_ha = true;
    }
  }

  cJSON_Delete(root);
  if (slots.empty()) return "";

  if (has_ha && this->ha_peer_name_ != ha_slot.name) {
    this->ha_peer_name_ = ha_slot.name;
    ESP_LOGI(TAG, "HA peer name learned from roster JSON: %s", this->ha_peer_name_.c_str());
  }

  const ContactEndpointKind local_endpoint_kind = ContactEndpointKind::SIP;
  const uint16_t ha_local_port = ha_slot.sip_port;
  const uint16_t ha_local_rtp_port = ha_slot.rtp_port;

  std::string out;
  for (const auto &slot : slots) {
    ContactEndpointKind endpoint_kind = ContactEndpointKind::UNKNOWN;
    Phonebook::parse_endpoint_kind(slot.endpoint_kind, &endpoint_kind);
    const bool missing_sip_transport = endpoint_kind == ContactEndpointKind::SIP && slot.sip_transport.empty();

    if (slot.kind == "ha") {
      if (slot.address.empty()) continue;
      append_csv(&out, serialize_endpoint(slot.name, local_endpoint_kind, slot.address, slot.sip_port, slot.rtp_port,
                                          slot.sip_transport == "tcp"));
      continue;
    }

    if ((slot.kind == "phone" || slot.kind == "group" || slot.ha_bridge ||
         slot.address.empty() ||
         endpoint_kind != ContactEndpointKind::SIP || missing_sip_transport) &&
        has_ha) {
      append_csv(&out, serialize_endpoint(slot.name, local_endpoint_kind, ha_slot.address,
                                          ha_local_port, ha_local_rtp_port,
                                          ha_slot.sip_transport == "tcp"));
      continue;
    }

    if (endpoint_kind == ContactEndpointKind::SIP) {
      if (missing_sip_transport) {
        ESP_LOGW(TAG, "Ignoring SIP roster entry '%s': metadata.sip_transport is required for direct SIP",
                 slot.name.c_str());
        continue;
      }
      append_csv(&out, serialize_endpoint(slot.name, endpoint_kind, slot.address,
                                          slot.sip_port, slot.rtp_port,
                                          slot.sip_transport == "tcp"));
    } else if (has_ha) {
      append_csv(&out, serialize_endpoint(slot.name, local_endpoint_kind, ha_slot.address,
                                          ha_local_port, ha_local_rtp_port,
                                          ha_slot.sip_transport == "tcp"));
    } else {
      append_csv(&out, slot.name);
    }
    if (has_ha && !slot.number.empty() && slot.number != slot.name && slot.kind != "ha") {
      append_csv(&out, serialize_endpoint(slot.number, local_endpoint_kind, ha_slot.address,
                                          ha_local_port, ha_local_rtp_port,
                                          ha_slot.sip_transport == "tcp"));
    }
  }

  return out;
}

bool VoipStack::apply_roster_json_contacts_(const std::string &roster_json) {
  if (roster_json.empty()) return false;

  cJSON *root = cJSON_ParseWithLength(roster_json.data(), roster_json.size());
  if (root == nullptr) {
    ESP_LOGW(TAG, "Ignoring HA roster_json: invalid JSON");
    return false;
  }

  const cJSON *contacts = nullptr;
  if (cJSON_IsArray(root)) {
    contacts = root;
  } else if (cJSON_IsObject(root)) {
    contacts = cJSON_GetObjectItemCaseSensitive(root, "contacts");
    if (!cJSON_IsArray(contacts)) contacts = cJSON_GetObjectItemCaseSensitive(root, "entries");
  }
  if (!cJSON_IsArray(contacts)) {
    ESP_LOGW(TAG, "Ignoring HA roster_json: missing contacts array");
    cJSON_Delete(root);
    return false;
  }

  std::vector<JsonRosterSlot> slots;
  slots.reserve(std::min(static_cast<size_t>(cJSON_GetArraySize(contacts)), Phonebook::MAX_CONTACTS));
  JsonRosterSlot ha_slot;
  bool has_ha = false;

  const cJSON *item = nullptr;
  cJSON_ArrayForEach(item, contacts) {
    if (slots.size() >= Phonebook::MAX_CONTACTS) break;
    JsonRosterSlot slot;
    if (!parse_json_roster_slot(item, &slot)) continue;
    if (slot.name == this->device_name_ || slot.name == this->device_route_id_) continue;
    slots.push_back(slot);
    if (slot.kind == "ha" && !slot.address.empty()) {
      ha_slot = slot;
      has_ha = true;
    }
  }
  cJSON_Delete(root);
  if (slots.empty()) return false;

  if (has_ha && this->ha_peer_name_ != ha_slot.name) {
    this->ha_peer_name_ = ha_slot.name;
    ESP_LOGI(TAG, "HA peer name learned from roster JSON: %s", this->ha_peer_name_.c_str());
  }

  const ContactEndpointKind local_endpoint_kind = ContactEndpointKind::SIP;
  const uint16_t ha_local_port = ha_slot.sip_port;
  const uint16_t ha_local_rtp_port = ha_slot.rtp_port;

  std::vector<ContactEntry> entries;
  entries.reserve(std::min(Phonebook::MAX_CONTACTS, slots.size() * 2));
  for (const auto &slot : slots) {
    if (entries.size() >= Phonebook::MAX_CONTACTS) break;
    ContactEndpointKind endpoint_kind = ContactEndpointKind::UNKNOWN;
    Phonebook::parse_endpoint_kind(slot.endpoint_kind, &endpoint_kind);
    const bool missing_sip_transport = endpoint_kind == ContactEndpointKind::SIP && slot.sip_transport.empty();

    ContactEntry entry;
    entry.name = slot.name;

    if (slot.kind == "ha") {
      entry.endpoint_kind = local_endpoint_kind;
      entry.ip = slot.address;
      entry.port = slot.sip_port;
      entry.rtp_port = slot.rtp_port;
      entry.sip_transport_tcp = slot.sip_transport == "tcp";
    } else if ((slot.kind == "phone" || slot.kind == "group" || slot.ha_bridge ||
                slot.address.empty() ||
                endpoint_kind != ContactEndpointKind::SIP || missing_sip_transport) &&
               has_ha) {
      entry.endpoint_kind = local_endpoint_kind;
      entry.ip = ha_slot.address;
      entry.port = ha_local_port;
      entry.rtp_port = ha_local_rtp_port;
      entry.sip_transport_tcp = ha_slot.sip_transport == "tcp";
    } else {
      entry.endpoint_kind = endpoint_kind;
      entry.ip = slot.address;
      if (endpoint_kind == ContactEndpointKind::SIP) {
        if (missing_sip_transport) {
          ESP_LOGW(TAG, "Ignoring SIP roster entry '%s': metadata.sip_transport is required for direct SIP",
                   slot.name.c_str());
          continue;
        }
        entry.port = slot.sip_port;
        entry.rtp_port = slot.rtp_port;
        entry.sip_transport_tcp = slot.sip_transport == "tcp";
      } else {
        continue;
      }
    }

    if (this->cycle_active_) this->seen_in_cycle_.insert(entry.name);
    entries.push_back(std::move(entry));

    if (has_ha && !slot.number.empty() && slot.number != slot.name && slot.kind != "ha") {
      if (entries.size() >= Phonebook::MAX_CONTACTS) break;
      ContactEntry alias;
      alias.name = slot.number;
      alias.endpoint_kind = local_endpoint_kind;
      alias.ip = ha_slot.address;
      alias.port = ha_local_port;
      alias.rtp_port = ha_local_rtp_port;
      alias.sip_transport_tcp = ha_slot.sip_transport == "tcp";
      if (this->cycle_active_) this->seen_in_cycle_.insert(alias.name);
      entries.push_back(std::move(alias));
    }
  }
  return this->phonebook_.replace_all(std::move(entries));
}

void VoipStack::update_contacts() {
  // Re-entry: commit any cycle still open (e.g. previous trigger chain didn't
  // wake the timeout). Counter advance + prune happen here, never inside the
  // cycle itself.
  if (this->cycle_active_) this->commit_cycle_();

  this->phonebook_.set_self_name(this->device_name_);
  this->cycle_active_ = true;
  this->cycle_started_at_ = millis();
  this->seen_in_cycle_.clear();
  this->enable_loop_soon_any_context();

  if (this->ha_phonebook_sensor_ != nullptr) {
    const std::string &raw_phonebook = this->ha_phonebook_sensor_->state;
    if (!raw_phonebook.empty()) {
      const std::string trimmed = Phonebook::trim(raw_phonebook);
      if (!trimmed.empty() && (trimmed[0] == '{' || trimmed[0] == '[')) {
        this->set_contacts(trimmed);
      } else {
        this->set_contacts(raw_phonebook);
      }
    }
  }

  // Fire trigger for downstream YAML sources. If nothing is wired, the loop()
  // timeout will commit the cycle in CYCLE_TIMEOUT_MS.
  this->update_contacts_trigger_.trigger();
}

void VoipStack::track_csv_(const std::string &csv) {
  // Parse names only - endpoints are merged via Phonebook::add_batch already.
  const auto entries = Phonebook::split(csv, ',');
  for (const auto &entry : entries) {
    const size_t bar = entry.find('|');
    std::string name = Phonebook::trim((bar == std::string::npos) ? entry : entry.substr(0, bar));
    if (!name.empty()) this->seen_in_cycle_.insert(std::move(name));
  }
}

void VoipStack::commit_cycle_() {
  const bool pruned = this->phonebook_.commit_cycle(this->seen_in_cycle_, this->prune_threshold_);
  if (pruned) {
    ESP_LOGI(TAG, "Phonebook cycle: pruned stale contacts, %zu remain", this->phonebook_.size());
    this->maybe_auto_select_ha_first_();
    this->publish_destination_();
    this->publish_contacts_();
  }
  this->seen_in_cycle_.clear();
  this->cycle_active_ = false;
}

bool VoipStack::maybe_auto_select_ha_first_() {
  if (!this->use_ha_as_first_contact_) return false;
  if (this->first_contacts_batch_committed_) return false;
  if (this->ha_peer_name_.empty()) return false;
  if (this->call_state_.load(std::memory_order_acquire) != CallState::IDLE) return false;
  if (this->phonebook_.find(this->ha_peer_name_) == nullptr) return false;
  if (!this->phonebook_.select(this->ha_peer_name_)) return false;

  this->first_contacts_batch_committed_ = true;
  ESP_LOGI(TAG, "Selected HA peer '%s' as initial destination", this->ha_peer_name_.c_str());
  return true;
}

void VoipStack::flush_contacts() {
  this->phonebook_.clear();
  this->publish_destination_();
  this->publish_contacts_();
  ESP_LOGI(TAG, "Contacts flushed (empty list)");
}

bool VoipStack::set_contact(const std::string &name) {
  if (this->phonebook_.empty()) {
    ESP_LOGW(TAG, "set_contact('%s') failed: contacts list is empty", name.c_str());
    std::string msg = std::string("Contact not found: ") + name;
    this->defer([this, msg]() { this->call_failed_trigger_.trigger(msg); });
    return false;
  }
  if (this->phonebook_.select(name)) {
    this->publish_destination_();
    ESP_LOGI(TAG, "Selected contact: %s", name.c_str());
    return true;
  }
  ESP_LOGW(TAG, "set_contact('%s') failed: not found in %zu contacts",
           name.c_str(), this->phonebook_.size());
  std::string msg = std::string("Contact not found: ") + name;
  this->defer([this, msg]() { this->call_failed_trigger_.trigger(msg); });
  return false;
}

void VoipStack::call_contact(const std::string &name) {
  if (!this->set_contact(name)) {
    return;
  }
  this->start();
}

void VoipStack::next_contact() {
  if (this->phonebook_.empty()) return;
  this->phonebook_.next();
  this->publish_destination_();
  ESP_LOGD(TAG, "Selected contact: %s", this->get_current_destination().c_str());
}

void VoipStack::prev_contact() {
  if (this->phonebook_.empty()) return;
  this->phonebook_.prev();
  this->publish_destination_();
  ESP_LOGD(TAG, "Selected contact: %s", this->get_current_destination().c_str());
}

const std::string &VoipStack::get_current_destination() const {
  return this->phonebook_.current_name();
}

const std::string &VoipStack::get_current_contact_ip() const {
  return this->phonebook_.current_ip();
}

uint16_t VoipStack::get_current_contact_port() const {
  return this->phonebook_.current_port();
}

uint16_t VoipStack::get_current_contact_rtp_port() const {
  const auto *c = this->phonebook_.current();
  return c ? c->rtp_port : 0;
}

bool VoipStack::get_current_contact_sip_transport_tcp() const {
  const auto *c = this->phonebook_.current();
  return c != nullptr && c->endpoint_kind == ContactEndpointKind::SIP && c->sip_transport_tcp;
}

void VoipStack::publish_destination_() {
  const std::string current = this->get_current_destination();
  if (this->destination_sensor_ != nullptr) {
    this->destination_sensor_->publish_state(current);
  }
  if (current != this->last_published_destination_) {
    this->last_published_destination_ = current;
    this->destination_changed_trigger_.trigger();
  }
  this->publish_sip_snapshot_();
}

void VoipStack::publish_caller_(const std::string &caller_name) {
  if (this->caller_sensor_ != nullptr) {
    this->caller_sensor_->publish_state(caller_name);
  }
  this->publish_sip_snapshot_();
}

void VoipStack::publish_contacts_() {
  if (this->contacts_sensor_ != nullptr) {
    // Count only ("3 contacts"); the CSV is on demand via get_contacts_csv().
    char buf[32];
    snprintf(buf, sizeof(buf), "%zu contact%s",
             this->phonebook_.size(),
             this->phonebook_.size() == 1 ? "" : "s");
    this->contacts_sensor_->publish_state(buf);
  }
}

std::string VoipStack::get_contacts_csv() const {
  // Names only (no endpoints) - for UI / diagnostic display.
  return this->phonebook_.names_csv();
}

}  // namespace voip_stack
}  // namespace esphome

#endif  // USE_ESP32
