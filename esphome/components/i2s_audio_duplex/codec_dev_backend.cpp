#include "codec_dev_backend.h"

#ifdef USE_ESP32

#include <audio_codec_ctrl_if.h>
#include <audio_codec_data_if.h>
#include <audio_codec_if.h>
#include <es7210_adc.h>
#include <es8311_codec.h>
#include <esp_codec_dev_types.h>

#ifdef USE_I2C
#include "esphome/components/i2c/i2c_bus.h"
#endif
#include "esphome/core/log.h"

#include <cstdlib>
#include <cstring>

namespace esphome {
namespace i2s_audio_duplex {

static const char *const TAG = "i2s_codec_dev";

namespace {

#ifdef USE_I2C
struct EsphomeI2cCtrl {
  audio_codec_ctrl_if_t base;
  i2c::I2CBus *bus;
  uint8_t address;
  bool open;
};

static int ctrl_open(const audio_codec_ctrl_if_t *ctrl, void *cfg, int cfg_size) {
  (void) cfg;
  (void) cfg_size;
  if (ctrl == nullptr) {
    return ESP_CODEC_DEV_INVALID_ARG;
  }
  auto *self = reinterpret_cast<EsphomeI2cCtrl *>(const_cast<audio_codec_ctrl_if_t *>(ctrl));
  self->open = self->bus != nullptr;
  return self->open ? ESP_CODEC_DEV_OK : ESP_CODEC_DEV_INVALID_ARG;
}

static bool ctrl_is_open(const audio_codec_ctrl_if_t *ctrl) {
  if (ctrl == nullptr) {
    return false;
  }
  auto *self = reinterpret_cast<EsphomeI2cCtrl *>(const_cast<audio_codec_ctrl_if_t *>(ctrl));
  return self->open;
}

static bool encode_reg(int reg, int reg_len, uint8_t *out) {
  if (out == nullptr || reg_len < 1 || reg_len > 2) {
    return false;
  }
  if (reg_len == 2) {
    out[0] = static_cast<uint8_t>((reg >> 8) & 0xFF);
    out[1] = static_cast<uint8_t>(reg & 0xFF);
  } else {
    out[0] = static_cast<uint8_t>(reg & 0xFF);
  }
  return true;
}

static int ctrl_read_reg(const audio_codec_ctrl_if_t *ctrl, int reg, int reg_len, void *data, int data_len) {
  if (ctrl == nullptr || data == nullptr || data_len <= 0) {
    return ESP_CODEC_DEV_INVALID_ARG;
  }
  auto *self = reinterpret_cast<EsphomeI2cCtrl *>(const_cast<audio_codec_ctrl_if_t *>(ctrl));
  if (!self->open || self->bus == nullptr) {
    return ESP_CODEC_DEV_WRONG_STATE;
  }
  uint8_t reg_buf[2]{};
  if (!encode_reg(reg, reg_len, reg_buf)) {
    return ESP_CODEC_DEV_INVALID_ARG;
  }
  auto result = self->bus->write_readv(self->address, reg_buf, reg_len,
                                       static_cast<uint8_t *>(data), data_len);
  return result == i2c::NO_ERROR ? ESP_CODEC_DEV_OK : ESP_CODEC_DEV_READ_FAIL;
}

static int ctrl_write_reg(const audio_codec_ctrl_if_t *ctrl, int reg, int reg_len, void *data, int data_len) {
  if (ctrl == nullptr || data == nullptr || data_len < 0) {
    return ESP_CODEC_DEV_INVALID_ARG;
  }
  auto *self = reinterpret_cast<EsphomeI2cCtrl *>(const_cast<audio_codec_ctrl_if_t *>(ctrl));
  if (!self->open || self->bus == nullptr) {
    return ESP_CODEC_DEV_WRONG_STATE;
  }
  if (reg_len < 1 || reg_len > 2 || data_len > 8) {
    return ESP_CODEC_DEV_NOT_SUPPORT;
  }
  uint8_t write_buf[10]{};
  if (!encode_reg(reg, reg_len, write_buf)) {
    return ESP_CODEC_DEV_INVALID_ARG;
  }
  memcpy(write_buf + reg_len, data, data_len);
  auto result = self->bus->write_readv(self->address, write_buf, reg_len + data_len, nullptr, 0);
  return result == i2c::NO_ERROR ? ESP_CODEC_DEV_OK : ESP_CODEC_DEV_WRITE_FAIL;
}

static int ctrl_close(const audio_codec_ctrl_if_t *ctrl) {
  if (ctrl == nullptr) {
    return ESP_CODEC_DEV_INVALID_ARG;
  }
  auto *self = reinterpret_cast<EsphomeI2cCtrl *>(const_cast<audio_codec_ctrl_if_t *>(ctrl));
  self->open = false;
  return ESP_CODEC_DEV_OK;
}
#endif  // USE_I2C

}  // namespace

CodecDevBackend::~CodecDevBackend() { this->teardown(); }

const audio_codec_ctrl_if_t *CodecDevBackend::new_i2c_ctrl_(uint8_t address) {
#ifdef USE_I2C
  if (this->i2c_bus_ == nullptr) {
    ESP_LOGE(TAG, "Codec I2C bus is not configured");
    return nullptr;
  }
  auto *ctrl = static_cast<EsphomeI2cCtrl *>(calloc(1, sizeof(EsphomeI2cCtrl)));
  if (ctrl == nullptr) {
    return nullptr;
  }
  ctrl->base.open = ctrl_open;
  ctrl->base.is_open = ctrl_is_open;
  ctrl->base.read_reg = ctrl_read_reg;
  ctrl->base.write_reg = ctrl_write_reg;
  ctrl->base.close = ctrl_close;
  ctrl->bus = this->i2c_bus_;
  ctrl->address = address;
  ctrl->open = false;
  return &ctrl->base;
#else
  (void) address;
  ESP_LOGE(TAG, "Codec I2C support is not compiled in");
  return nullptr;
#endif
}

esp_codec_dev_sample_info_t CodecDevBackend::make_sample_info_(const SampleConfig &config) {
  return {
      .bits_per_sample = config.bits_per_sample,
      .channel = config.channels,
      .channel_mask = config.channel_mask,
      .sample_rate = config.sample_rate,
      .mclk_multiple = static_cast<int>(config.mclk_multiple),
  };
}

const char *CodecDevBackend::input_codec_name() const {
  if (this->es7210_.enabled) {
    return "ES7210";
  }
  if (this->es8311_input_.enabled) {
    return "ES8311";
  }
  return "none";
}

bool CodecDevBackend::setup(uint8_t i2s_port, i2s_chan_handle_t tx_handle, i2s_chan_handle_t rx_handle,
                            i2s_clock_src_t clk_src) {
  this->teardown();

  audio_codec_i2s_cfg_t i2s_cfg = {
      .port = i2s_port,
      .rx_handle = rx_handle,
      .tx_handle = tx_handle,
      .clk_src = static_cast<int>(clk_src),
  };
  this->data_if_ = audio_codec_new_i2s_data(&i2s_cfg);
  if (this->data_if_ == nullptr) {
    ESP_LOGE(TAG, "Failed to create esp_codec_dev I2S data interface");
    return false;
  }

  if (rx_handle != nullptr && this->es7210_.enabled) {
    this->es7210_ctrl_ = this->new_i2c_ctrl_(this->es7210_.address);
    if (this->es7210_ctrl_ == nullptr) {
      return false;
    }
    es7210_codec_cfg_t cfg = {};
    cfg.ctrl_if = this->es7210_ctrl_;
    cfg.master_mode = false;
    cfg.mic_selected = this->es7210_.mic_selected;
    cfg.mclk_div = 256;
    this->rx_codec_if_ = es7210_codec_new(&cfg);
    if (this->rx_codec_if_ == nullptr) {
      ESP_LOGE(TAG, "Failed to create ES7210 codec interface");
      return false;
    }
  } else if (rx_handle != nullptr && this->es8311_input_.enabled) {
    this->es8311_input_ctrl_ = this->new_i2c_ctrl_(this->es8311_input_.address);
    if (this->es8311_input_ctrl_ == nullptr) {
      return false;
    }
    es8311_codec_cfg_t cfg = {};
    cfg.ctrl_if = this->es8311_input_ctrl_;
    cfg.gpio_if = nullptr;
    cfg.codec_mode = ESP_CODEC_DEV_WORK_MODE_ADC;
    cfg.pa_pin = -1;
    cfg.master_mode = false;
    cfg.use_mclk = this->es8311_input_.use_mclk;
    cfg.no_dac_ref = this->es8311_input_.no_dac_ref;
    cfg.mclk_div = 256;
    this->rx_codec_if_ = es8311_codec_new(&cfg);
    if (this->rx_codec_if_ == nullptr) {
      ESP_LOGE(TAG, "Failed to create ES8311 ADC codec interface");
      return false;
    }
  }

  if (tx_handle != nullptr && this->es8311_.enabled) {
    this->es8311_ctrl_ = this->new_i2c_ctrl_(this->es8311_.address);
    if (this->es8311_ctrl_ == nullptr) {
      return false;
    }
    es8311_codec_cfg_t cfg = {};
    cfg.ctrl_if = this->es8311_ctrl_;
    cfg.gpio_if = nullptr;
    cfg.codec_mode = ESP_CODEC_DEV_WORK_MODE_DAC;
    cfg.pa_pin = -1;
    cfg.master_mode = false;
    cfg.use_mclk = this->es8311_.use_mclk;
    cfg.no_dac_ref = this->es8311_.no_dac_ref;
    cfg.mclk_div = 256;
    this->tx_codec_if_ = es8311_codec_new(&cfg);
    if (this->tx_codec_if_ == nullptr) {
      ESP_LOGE(TAG, "Failed to create ES8311 codec interface");
      return false;
    }
  }

  if (rx_handle != nullptr) {
    esp_codec_dev_cfg_t rx_cfg = {
        .dev_type = ESP_CODEC_DEV_TYPE_IN,
        .codec_if = this->rx_codec_if_,
        .data_if = this->data_if_,
    };
    this->rx_dev_ = esp_codec_dev_new(&rx_cfg);
    if (this->rx_dev_ == nullptr) {
      ESP_LOGE(TAG, "Failed to create esp_codec_dev RX device");
      return false;
    }
  }

  if (tx_handle != nullptr) {
    esp_codec_dev_cfg_t tx_cfg = {
        .dev_type = ESP_CODEC_DEV_TYPE_OUT,
        .codec_if = this->tx_codec_if_,
        .data_if = this->data_if_,
    };
    this->tx_dev_ = esp_codec_dev_new(&tx_cfg);
    if (this->tx_dev_ == nullptr) {
      ESP_LOGE(TAG, "Failed to create esp_codec_dev TX device");
      return false;
    }
  }

  this->prepared_ = true;
  ESP_LOGI(TAG, "esp_codec_dev backend ready (rx_codec=%s, tx_codec=%s)",
           this->input_codec_name(), this->output_codec_name());
  return true;
}

bool CodecDevBackend::open(const SampleConfig *tx_config, const SampleConfig *rx_config) {
  if (!this->prepared_) {
    return false;
  }
  if (this->open_) {
    return true;
  }

  if (this->rx_dev_ != nullptr && rx_config != nullptr) {
    auto fs = make_sample_info_(*rx_config);
    int ret = esp_codec_dev_open(this->rx_dev_, &fs);
    if (ret != ESP_CODEC_DEV_OK) {
      ESP_LOGE(TAG, "Failed to open RX codec device: %d", ret);
      return false;
    }
    if (this->es7210_.enabled) {
      this->set_input_gain(this->es7210_.input_gain_db);
      if (this->es7210_.has_ref_channel_gain) {
        this->set_input_channel_gain(this->es7210_.ref_channel, this->es7210_.ref_channel_gain_db);
      }
    } else if (this->es8311_input_.enabled) {
      this->set_input_gain(this->es8311_input_.input_gain_db);
    }
  }
  if (this->tx_dev_ != nullptr && tx_config != nullptr) {
    auto fs = make_sample_info_(*tx_config);
    int ret = esp_codec_dev_open(this->tx_dev_, &fs);
    if (ret != ESP_CODEC_DEV_OK) {
      ESP_LOGE(TAG, "Failed to open TX codec device: %d", ret);
      if (this->rx_dev_ != nullptr) {
        esp_codec_dev_close(this->rx_dev_);
      }
      return false;
    }
    if (!this->es8311_.enabled) {
      esp_codec_dev_set_out_vol(this->tx_dev_, 100);
    }
  }

  this->open_ = true;
  return true;
}

void CodecDevBackend::close() {
  if (!this->open_) {
    return;
  }
  if (this->rx_dev_ != nullptr) {
    esp_codec_dev_close(this->rx_dev_);
  }
  if (this->tx_dev_ != nullptr) {
    esp_codec_dev_close(this->tx_dev_);
  }
  this->open_ = false;
}

bool CodecDevBackend::read(void *data, size_t len) {
  if (this->rx_dev_ == nullptr || data == nullptr || len == 0) {
    return false;
  }
  return esp_codec_dev_read(this->rx_dev_, data, static_cast<int>(len)) == ESP_CODEC_DEV_OK;
}

bool CodecDevBackend::write(void *data, size_t len) {
  if (this->tx_dev_ == nullptr || data == nullptr || len == 0) {
    return false;
  }
  return esp_codec_dev_write(this->tx_dev_, data, static_cast<int>(len)) == ESP_CODEC_DEV_OK;
}

void CodecDevBackend::set_output_volume(float volume) {
  if (this->tx_dev_ == nullptr) {
    return;
  }
  if (!this->es8311_.enabled) {
    esp_codec_dev_set_out_vol(this->tx_dev_, 100);
    return;
  }
  if (!(volume > 0.0f)) {
    volume = 0.0f;
  } else if (volume > 1.0f) {
    volume = 1.0f;
  }
  esp_codec_dev_set_out_vol(this->tx_dev_, static_cast<int>(volume * 100.0f + 0.5f));
}

void CodecDevBackend::set_output_mute(bool mute) {
  if (this->tx_dev_ != nullptr) {
    esp_codec_dev_set_out_mute(this->tx_dev_, mute);
  }
}

void CodecDevBackend::set_input_gain(float gain_db) {
  if (this->rx_dev_ != nullptr) {
    esp_codec_dev_set_in_gain(this->rx_dev_, gain_db);
  }
}

void CodecDevBackend::set_input_channel_gain(uint8_t channel, float gain_db) {
  if (this->rx_dev_ != nullptr && channel < 16) {
    esp_codec_dev_set_in_channel_gain(this->rx_dev_, ESP_CODEC_DEV_MAKE_CHANNEL_MASK(channel), gain_db);
  }
}

void CodecDevBackend::destroy_codecs_() {
  if (this->rx_codec_if_ != nullptr) {
    audio_codec_delete_codec_if(this->rx_codec_if_);
    this->rx_codec_if_ = nullptr;
  }
  if (this->tx_codec_if_ != nullptr) {
    audio_codec_delete_codec_if(this->tx_codec_if_);
    this->tx_codec_if_ = nullptr;
  }
  if (this->es7210_ctrl_ != nullptr) {
    audio_codec_delete_ctrl_if(this->es7210_ctrl_);
    this->es7210_ctrl_ = nullptr;
  }
  if (this->es8311_input_ctrl_ != nullptr) {
    audio_codec_delete_ctrl_if(this->es8311_input_ctrl_);
    this->es8311_input_ctrl_ = nullptr;
  }
  if (this->es8311_ctrl_ != nullptr) {
    audio_codec_delete_ctrl_if(this->es8311_ctrl_);
    this->es8311_ctrl_ = nullptr;
  }
}

void CodecDevBackend::teardown() {
  this->close();
  if (this->rx_dev_ != nullptr) {
    esp_codec_dev_delete(this->rx_dev_);
    this->rx_dev_ = nullptr;
  }
  if (this->tx_dev_ != nullptr) {
    esp_codec_dev_delete(this->tx_dev_);
    this->tx_dev_ = nullptr;
  }
  this->destroy_codecs_();
  if (this->data_if_ != nullptr) {
    audio_codec_delete_data_if(this->data_if_);
    this->data_if_ = nullptr;
  }
  this->prepared_ = false;
  this->open_ = false;
}

}  // namespace i2s_audio_duplex
}  // namespace esphome

#endif  // USE_ESP32
