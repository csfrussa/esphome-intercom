#include "i2s_audio_duplex.h"

#ifdef USE_ESP32

#include <algorithm>
#include <cstring>

#include <esp_ae_rate_cvt.h>
#include <esp_heap_caps.h>
#include <esp_log.h>

namespace esphome {
namespace i2s_audio_duplex {

static const char *const TAG = "i2s_rate_cvt";

namespace {
int16_t *alloc_internal(size_t count, const char *who) {
  auto *p = static_cast<int16_t *>(heap_caps_malloc(count * sizeof(int16_t), MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT));
  if (p == nullptr) {
    ESP_LOGE(who, "rate converter buffer alloc failed: %u bytes, %u internal free, %u largest",
             static_cast<unsigned>(count * sizeof(int16_t)),
             static_cast<unsigned>(heap_caps_get_free_size(MALLOC_CAP_INTERNAL)),
             static_cast<unsigned>(heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL)));
  }
  return p;
}

bool ensure_buffer(int16_t *&ptr, size_t &cap, size_t needed, const char *who) {
  if (cap >= needed)
    return true;
  if (ptr != nullptr)
    heap_caps_free(ptr);
  ptr = alloc_internal(needed, who);
  cap = ptr != nullptr ? needed : 0;
  return ptr != nullptr;
}

void zero_samples(int16_t *ptr, size_t count) {
  if (ptr != nullptr && count > 0)
    memset(ptr, 0, count * sizeof(int16_t));
}

void copy_slot_16(const int16_t *src, int16_t *dst, size_t count, size_t stride, uint8_t offset) {
  for (size_t i = 0; i < count; i++)
    dst[i] = src[i * stride + offset];
}

void copy_slot_32(const int32_t *src, int16_t *dst, size_t count, size_t stride, uint8_t offset) {
  for (size_t i = 0; i < count; i++)
    dst[i] = static_cast<int16_t>(src[i * stride + offset] >> 16);
}

void distribute_channels(int16_t *const *ch, uint8_t nch, size_t count, int16_t *mic_interleaved,
                         int16_t *mic_mono, int16_t *ref_out, uint8_t num_mic_ch) {
  for (size_t i = 0; i < count; i++) {
    const int16_t s0 = ch[0][i];
    if (mic_mono != nullptr)
      mic_mono[i] = s0;
    if (num_mic_ch >= 2 && nch >= 2) {
      const int16_t s1 = ch[1][i];
      if (mic_interleaved != nullptr) {
        mic_interleaved[i * 2] = s0;
        mic_interleaved[i * 2 + 1] = s1;
      }
      if (ref_out != nullptr && nch >= 3)
        ref_out[i] = ch[2][i];
    } else if (ref_out != nullptr && nch >= 2) {
      ref_out[i] = ch[1][i];
    }
  }
}

class RateCvtHandle {
 public:
  ~RateCvtHandle() { this->close(); }

  void init(uint32_t ratio, uint32_t src_rate, uint32_t dest_rate, uint8_t channels) {
    this->ratio_ = ratio;
    this->src_rate_ = src_rate;
    this->dest_rate_ = dest_rate;
    this->channels_ = channels;
    this->close();
  }

  void reset() { this->close(); }
  bool bypass() const { return this->ratio_ <= 1; }

  bool ready() {
    if (this->bypass())
      return true;
    if (this->handle_ != nullptr)
      return true;
    if (this->src_rate_ == 0 || this->dest_rate_ == 0 || this->src_rate_ == this->dest_rate_ ||
        this->channels_ == 0) {
      ESP_LOGE(TAG, "invalid esp_ae_rate_cvt config: src=%u dest=%u ch=%u",
               static_cast<unsigned>(this->src_rate_), static_cast<unsigned>(this->dest_rate_),
               static_cast<unsigned>(this->channels_));
      return false;
    }

    esp_ae_rate_cvt_cfg_t cfg{};
    cfg.src_rate = this->src_rate_;
    cfg.dest_rate = this->dest_rate_;
    cfg.channel = this->channels_;
    cfg.bits_per_sample = 16;
    cfg.complexity = 3;
    cfg.perf_type = ESP_AE_RATE_CVT_PERF_TYPE_SPEED;
    const esp_ae_err_t err = esp_ae_rate_cvt_open(&cfg, &this->handle_);
    if (err == ESP_AE_ERR_OK && this->handle_ != nullptr)
      return true;

    ESP_LOGE(TAG, "esp_ae_rate_cvt_open failed: err=%d src=%u dest=%u ch=%u",
             static_cast<int>(err), static_cast<unsigned>(this->src_rate_),
             static_cast<unsigned>(this->dest_rate_), static_cast<unsigned>(this->channels_));
    return false;
  }

  bool process(int16_t *in, size_t in_count, int16_t *out, size_t expected_out, const char *scope) {
    if (!this->ready())
      return false;
    uint32_t out_samples = static_cast<uint32_t>(expected_out);
    const esp_ae_err_t err = esp_ae_rate_cvt_process(this->handle_, in, static_cast<uint32_t>(in_count),
                                                     out, &out_samples);
    return this->check_(scope, err, out_samples, expected_out);
  }

  bool process_deintlv(int16_t **in, size_t in_count, int16_t **out, size_t expected_out, const char *scope) {
    if (!this->ready())
      return false;
    esp_ae_sample_t in_args[MC_FIR_MAX_CH]{};
    esp_ae_sample_t out_args[MC_FIR_MAX_CH]{};
    for (uint8_t i = 0; i < this->channels_; i++) {
      in_args[i] = in[i];
      out_args[i] = out[i];
    }
    uint32_t out_samples = static_cast<uint32_t>(expected_out);
    const esp_ae_err_t err = esp_ae_rate_cvt_deintlv_process(this->handle_, in_args,
                                                             static_cast<uint32_t>(in_count),
                                                             out_args, &out_samples);
    return this->check_(scope, err, out_samples, expected_out);
  }

 private:
  bool check_(const char *scope, esp_ae_err_t err, uint32_t actual, size_t expected) {
    if (err == ESP_AE_ERR_OK && actual == expected)
      return true;
    const uint32_t n = ++this->failures_;
    if (n <= 3 || (n % 128) == 0) {
      ESP_LOGW(TAG, "esp_ae_rate_cvt %s failed/misaligned: err=%d out=%u expected=%u ch=%u",
               scope, static_cast<int>(err), static_cast<unsigned>(actual),
               static_cast<unsigned>(expected), static_cast<unsigned>(this->channels_));
    }
    return false;
  }

  void close() {
    if (this->handle_ != nullptr) {
      esp_ae_rate_cvt_close(this->handle_);
      this->handle_ = nullptr;
    }
  }

  uint32_t ratio_{1};
  uint32_t src_rate_{0};
  uint32_t dest_rate_{0};
  uint8_t channels_{0};
  uint32_t failures_{0};
  esp_ae_rate_cvt_handle_t handle_{nullptr};
};
}  // namespace

class FirDecimatorImpl {
 public:
  ~FirDecimatorImpl() {
    if (this->scratch_ != nullptr)
      heap_caps_free(this->scratch_);
  }

  void init(uint32_t ratio, uint32_t src_rate, uint32_t dest_rate) {
    this->ratio_ = ratio;
    this->rate_cvt_.init(ratio, src_rate, dest_rate, 1);
  }

  void reset() { this->rate_cvt_.reset(); }

  bool prepare(size_t in_count) {
    return this->ratio_ <= 1 || (ensure_buffer(this->scratch_, this->scratch_cap_, in_count, "RateCvt") &&
                                 this->rate_cvt_.ready());
  }

  void process(const int16_t *in, int16_t *out, size_t in_count) {
    if (this->ratio_ <= 1) {
      memcpy(out, in, in_count * sizeof(int16_t));
      return;
    }
    const size_t out_count = in_count / this->ratio_;
    if (!this->rate_cvt_.process(const_cast<int16_t *>(in), in_count, out, out_count, "mono"))
      zero_samples(out, out_count);
  }

  void process_strided(const int16_t *in, int16_t *out, size_t out_count, size_t stride, size_t offset) {
    if (this->ratio_ <= 1) {
      copy_slot_16(in, out, out_count, stride, static_cast<uint8_t>(offset));
      return;
    }
    const size_t in_count = out_count * this->ratio_;
    if (!ensure_buffer(this->scratch_, this->scratch_cap_, in_count, "RateCvt")) {
      zero_samples(out, out_count);
      return;
    }
    copy_slot_16(in, this->scratch_, in_count, stride, static_cast<uint8_t>(offset));
    this->process(this->scratch_, out, in_count);
  }

  void process_strided_32(const int32_t *in, int16_t *out, size_t out_count, size_t stride, size_t offset) {
    if (this->ratio_ <= 1) {
      copy_slot_32(in, out, out_count, stride, static_cast<uint8_t>(offset));
      return;
    }
    const size_t in_count = out_count * this->ratio_;
    if (!ensure_buffer(this->scratch_, this->scratch_cap_, in_count, "RateCvt")) {
      zero_samples(out, out_count);
      return;
    }
    copy_slot_32(in, this->scratch_, in_count, stride, static_cast<uint8_t>(offset));
    this->process(this->scratch_, out, in_count);
  }

 private:
  uint32_t ratio_{1};
  RateCvtHandle rate_cvt_;
  int16_t *scratch_{nullptr};
  size_t scratch_cap_{0};
};

class MultiChannelFirDecimatorImpl {
 public:
  ~MultiChannelFirDecimatorImpl() {
    for (uint8_t i = 0; i < MC_FIR_MAX_CH; i++) {
      if (this->in_ch_[i] != nullptr)
        heap_caps_free(this->in_ch_[i]);
      if (this->out_ch_[i] != nullptr)
        heap_caps_free(this->out_ch_[i]);
    }
  }

  void init(uint32_t ratio, uint8_t num_channels, uint32_t src_rate, uint32_t dest_rate) {
    this->ratio_ = ratio;
    this->channels_ = std::min<uint8_t>(num_channels, MC_FIR_MAX_CH);
    this->rate_cvt_.init(ratio, src_rate, dest_rate, this->channels_);
  }

  void reset() { this->rate_cvt_.reset(); }

  bool prepare(size_t in_count, size_t out_count, uint8_t num_channels) {
    if (this->ratio_ <= 1)
      return true;
    const uint8_t nch = std::min<uint8_t>(num_channels, MC_FIR_MAX_CH);
    return this->ensure_channel_buffers_(nch, in_count, out_count) && this->rate_cvt_.ready();
  }

  void process_multi(const int16_t *in, size_t out_count, size_t stride, const uint8_t *offsets,
                     int16_t *mic_interleaved, int16_t *mic_mono, int16_t *ref_out, uint8_t num_mic_ch) {
    this->process_multi_t_(in, out_count, stride, offsets, mic_interleaved, mic_mono, ref_out, num_mic_ch, false);
  }

  void process_multi_32(const int32_t *in, size_t out_count, size_t stride, const uint8_t *offsets,
                        int16_t *mic_interleaved, int16_t *mic_mono, int16_t *ref_out, uint8_t num_mic_ch) {
    this->process_multi_t_(in, out_count, stride, offsets, mic_interleaved, mic_mono, ref_out, num_mic_ch, true);
  }

 private:
  template<typename T>
  void process_multi_t_(const T *in, size_t out_count, size_t stride, const uint8_t *offsets,
                        int16_t *mic_interleaved, int16_t *mic_mono, int16_t *ref_out,
                        uint8_t num_mic_ch, bool source_32bit) {
    if (this->ratio_ <= 1) {
      this->copy_passthrough_(in, out_count, stride, offsets, mic_interleaved, mic_mono, ref_out,
                              num_mic_ch, source_32bit);
      return;
    }

    const size_t in_count = out_count * this->ratio_;
    if (!this->ensure_channel_buffers_(this->channels_, in_count, out_count)) {
      this->zero_outputs_(out_count, mic_interleaved, mic_mono, ref_out, num_mic_ch);
      return;
    }
    this->copy_input_channels_(in, this->in_ch_, in_count, stride, offsets, source_32bit);
    if (this->rate_cvt_.process_deintlv(this->in_ch_, in_count, this->out_ch_, out_count, "multi")) {
      distribute_channels(this->out_ch_, this->channels_, out_count, mic_interleaved, mic_mono, ref_out, num_mic_ch);
    } else {
      this->zero_outputs_(out_count, mic_interleaved, mic_mono, ref_out, num_mic_ch);
    }
  }

  template<typename T>
  void copy_passthrough_(const T *in, size_t count, size_t stride, const uint8_t *offsets,
                         int16_t *mic_interleaved, int16_t *mic_mono, int16_t *ref_out,
                         uint8_t num_mic_ch, bool source_32bit) {
    for (size_t i = 0; i < count; i++) {
      auto sample = [&](uint8_t ch) -> int16_t {
        if (source_32bit)
          return static_cast<int16_t>(reinterpret_cast<const int32_t *>(in)[i * stride + offsets[ch]] >> 16);
        return reinterpret_cast<const int16_t *>(in)[i * stride + offsets[ch]];
      };
      const int16_t s0 = sample(0);
      if (mic_mono != nullptr)
        mic_mono[i] = s0;
      if (num_mic_ch >= 2 && this->channels_ >= 2) {
        const int16_t s1 = sample(1);
        if (mic_interleaved != nullptr) {
          mic_interleaved[i * 2] = s0;
          mic_interleaved[i * 2 + 1] = s1;
        }
        if (ref_out != nullptr && this->channels_ >= 3)
          ref_out[i] = sample(2);
      } else if (ref_out != nullptr && this->channels_ >= 2) {
        ref_out[i] = sample(1);
      }
    }
  }

  template<typename T>
  void copy_input_channels_(const T *in, int16_t **dst, size_t count, size_t stride,
                            const uint8_t *offsets, bool source_32bit) {
    for (uint8_t c = 0; c < this->channels_; c++) {
      if (source_32bit) {
        copy_slot_32(reinterpret_cast<const int32_t *>(in), dst[c], count, stride, offsets[c]);
      } else {
        copy_slot_16(reinterpret_cast<const int16_t *>(in), dst[c], count, stride, offsets[c]);
      }
    }
  }

  bool ensure_channel_buffers_(uint8_t channels, size_t in_count, size_t out_count) {
    for (uint8_t c = 0; c < channels; c++) {
      if (!ensure_buffer(this->in_ch_[c], this->in_cap_[c], in_count, "MCRateCvt") ||
          !ensure_buffer(this->out_ch_[c], this->out_cap_[c], out_count, "MCRateCvt")) {
        return false;
      }
    }
    return true;
  }

  void zero_outputs_(size_t count, int16_t *mic_interleaved, int16_t *mic_mono,
                     int16_t *ref_out, uint8_t num_mic_ch) {
    zero_samples(mic_mono, count);
    if (mic_interleaved != nullptr && num_mic_ch >= 2)
      zero_samples(mic_interleaved, count * 2);
    zero_samples(ref_out, count);
  }

  uint32_t ratio_{1};
  uint8_t channels_{0};
  RateCvtHandle rate_cvt_;
  int16_t *in_ch_[MC_FIR_MAX_CH]{};
  int16_t *out_ch_[MC_FIR_MAX_CH]{};
  size_t in_cap_[MC_FIR_MAX_CH]{};
  size_t out_cap_[MC_FIR_MAX_CH]{};
};

FirDecimator::FirDecimator() : impl_(std::make_unique<FirDecimatorImpl>()) {}
FirDecimator::~FirDecimator() = default;
void FirDecimator::init(uint32_t ratio, uint32_t src_rate, uint32_t dest_rate) {
  this->impl_->init(ratio, src_rate, dest_rate);
}
void FirDecimator::reset() { this->impl_->reset(); }
bool FirDecimator::prepare(size_t in_count) { return this->impl_->prepare(in_count); }
void FirDecimator::process(const int16_t *in, int16_t *out, size_t in_count) {
  this->impl_->process(in, out, in_count);
}
void FirDecimator::process_strided(const int16_t *in, int16_t *out, size_t out_count,
                                   size_t stride, size_t offset) {
  this->impl_->process_strided(in, out, out_count, stride, offset);
}
void FirDecimator::process_strided_32(const int32_t *in, int16_t *out, size_t out_count,
                                      size_t stride, size_t offset) {
  this->impl_->process_strided_32(in, out, out_count, stride, offset);
}

MultiChannelFirDecimator::MultiChannelFirDecimator()
    : impl_(std::make_unique<MultiChannelFirDecimatorImpl>()) {}
MultiChannelFirDecimator::~MultiChannelFirDecimator() = default;
void MultiChannelFirDecimator::init(uint32_t ratio, uint8_t num_channels,
                                    uint32_t src_rate, uint32_t dest_rate) {
  this->impl_->init(ratio, num_channels, src_rate, dest_rate);
}
void MultiChannelFirDecimator::reset() { this->impl_->reset(); }
bool MultiChannelFirDecimator::prepare(size_t in_count, size_t out_count, uint8_t num_channels) {
  return this->impl_->prepare(in_count, out_count, num_channels);
}
void MultiChannelFirDecimator::process_multi(const int16_t *in, size_t out_count, size_t in_stride,
                                             const uint8_t *channel_offsets, int16_t *mic_interleaved,
                                             int16_t *mic_mono, int16_t *ref_out, uint8_t num_mic_ch) {
  this->impl_->process_multi(in, out_count, in_stride, channel_offsets, mic_interleaved, mic_mono,
                             ref_out, num_mic_ch);
}
void MultiChannelFirDecimator::process_multi_32(const int32_t *in, size_t out_count, size_t in_stride,
                                                const uint8_t *channel_offsets, int16_t *mic_interleaved,
                                                int16_t *mic_mono, int16_t *ref_out, uint8_t num_mic_ch) {
  this->impl_->process_multi_32(in, out_count, in_stride, channel_offsets, mic_interleaved, mic_mono,
                                ref_out, num_mic_ch);
}

}  // namespace i2s_audio_duplex
}  // namespace esphome

#endif  // USE_ESP32
