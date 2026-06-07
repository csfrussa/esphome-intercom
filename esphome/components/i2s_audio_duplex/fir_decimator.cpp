#include "i2s_audio_duplex.h"

#ifdef USE_ESP32

#include <esp_heap_caps.h>
#include <esp_log.h>
#include <dsps_fir.h>
#include <algorithm>
#include <cstring>

namespace esphome {
namespace i2s_audio_duplex {

// FIR coefficients: 32-tap (31 original + 1 zero pad), cutoff=7500Hz, fs=48kHz, Kaiser beta=8.0
// ~35dB stopband, symmetric linear phase. Q15 fixed-point for dsps_fird_s16_aes3 SIMD.
// Source float max |c| = 0.3125 -> q15 10238 (no overflow). DC gain ~0.975.
static_assert(FIR_NUM_TAPS % 8 == 0, "FIR_NUM_TAPS must be divisible by 8 for dsps_fird_s16_aes3");

namespace {
constexpr int16_t FIR_COEFFS_Q15[FIR_NUM_TAPS] = {
        1,     7,     4,   -33,   -88,   -61,   146,   415,
      350,  -357, -1335, -1407,   583,  4507,  8528, 10238,
     8528,  4507,   583, -1407, -1335,  -357,   350,   415,
      146,   -61,   -88,   -33,     4,     7,     1,     0,
};

// Float-precision FIR coefficients (31 real taps + 1 zero pad for the shared
// 32-tap implementation). Kaiser beta=8.0, cutoff 7500 Hz at fs=48 kHz,
// unity DC gain. Used by the optional `fir_decimator: custom` kernel.
// Selected per-yaml: ESP32-P4 needs this to bypass the dsps_fird_s16 ASM kernel
// bug on RISC-V (esp-dsp issues #117/#102, rect-wave artifacts that propagate
// quantization noise into esp_aec adaptive filter -> musical noise on the wire).
constexpr float FIR_COEFFS_FLOAT[FIR_NUM_TAPS] = {
    4.1270231666e-05f, 2.1633893589e-04f, 1.2531119530e-04f, -9.9999988238e-04f,
    -2.6821920740e-03f, -1.8518117881e-03f, 4.4563387256e-03f, 1.2653483833e-02f,
    1.0683467077e-02f, -1.0893520506e-02f, -4.0743026823e-02f, -4.2934182572e-02f,
    1.7799016112e-02f, 1.3755146771e-01f, 2.6031620059e-01f, 3.1252367847e-01f,
    2.6031620059e-01f, 1.3755146771e-01f, 1.7799016112e-02f, -4.2934182572e-02f,
    -4.0743026823e-02f, -1.0893520506e-02f, 1.0683467077e-02f, 1.2653483833e-02f,
    4.4563387256e-03f, -1.8518117881e-03f, -2.6821920740e-03f, -9.9999988238e-04f,
    1.2531119530e-04f, 2.1633893589e-04f, 4.1270231666e-05f, 0.0f,
};

// Scalar float FIR decimator. Drop-in replacement for dsps_fird_s16, used when
// the YAML selects `fir_decimator: custom`. Slower than the SIMD kernel but
// numerically clean on every chip variant.
inline void fir_decimate_float(const int16_t *in, int16_t *out, int32_t out_count,
                               uint32_t ratio, float *delay_line, uint32_t *delay_pos) {
  for (int32_t o = 0; o < out_count; o++) {
    for (uint32_t r = 0; r < ratio; r++) {
      delay_line[*delay_pos] = static_cast<float>(*in++);
      *delay_pos = (*delay_pos + 1) & (FIR_NUM_TAPS - 1);
    }
    float acc = 0.0f;
    uint32_t idx = *delay_pos;
    for (size_t t = 0; t < FIR_NUM_TAPS; t++) {
      acc += delay_line[idx] * FIR_COEFFS_FLOAT[t];
      idx = (idx + 1) & (FIR_NUM_TAPS - 1);
    }
    if (acc > 32767.0f) acc = 32767.0f;
    if (acc < -32768.0f) acc = -32768.0f;
    out[o] = static_cast<int16_t>(acc);
  }
}

// FIR scratch buffers are part of the audio decimator hot path: each frame
// reads/writes them, and any cache miss directly stretches the audio cycle.
// Force INTERNAL placement (rejecting silent fallback) so we either have a
// fast scratch or fail loudly, never quietly degrade to PSRAM stalls under
// high load. Caller is responsible for handling the nullptr return.
int16_t *alloc_fir_int16_internal(size_t count, const char *who) {
  const size_t bytes = count * sizeof(int16_t);
  int16_t *p = static_cast<int16_t *>(heap_caps_malloc(bytes, MALLOC_CAP_INTERNAL | MALLOC_CAP_8BIT));
  if (p == nullptr) {
    const size_t free_internal = heap_caps_get_free_size(MALLOC_CAP_INTERNAL);
    const size_t largest_internal = heap_caps_get_largest_free_block(MALLOC_CAP_INTERNAL);
    ESP_LOGE(who, "FIR scratch alloc failed: %u bytes requested, %u internal free, %u largest block",
             static_cast<unsigned>(bytes), static_cast<unsigned>(free_internal),
             static_cast<unsigned>(largest_internal));
  }
  return p;
}
}  // namespace

class FirDecimatorImpl {
 public:
  ~FirDecimatorImpl() {
    dsps_fird_s16_aexx_free(&this->fir_);
    if (this->scratch_ != nullptr)
      heap_caps_free(this->scratch_);
  }

  void init(uint32_t ratio) {
    this->ratio_ = ratio;
    memcpy(this->coeffs_local_, FIR_COEFFS_Q15, sizeof(FIR_COEFFS_Q15));
    dsps_fird_init_s16(&this->fir_, this->coeffs_local_, this->delay_,
                       FIR_NUM_TAPS, static_cast<int16_t>(ratio), 0, 0);
    dsps_16_array_rev(this->fir_.coeffs, this->fir_.coeffs_len);
  }

  void reset() {
    memset(this->delay_, 0, sizeof(this->delay_));
    this->fir_.pos = 0;
    this->fir_.d_pos = 0;
    memset(this->fdelay_, 0, sizeof(this->fdelay_));
    this->fdelay_pos_ = 0;
  }

  void set_use_float_fir(bool b) { this->use_float_fir_ = b; }

  void process(const int16_t *in, int16_t *out, size_t in_count) {
    if (this->ratio_ <= 1) {
      memcpy(out, in, in_count * sizeof(int16_t));
      return;
    }
    int32_t out_count = static_cast<int32_t>(in_count / this->ratio_);
    if (this->use_float_fir_) {
      fir_decimate_float(in, out, out_count, this->ratio_, this->fdelay_, &this->fdelay_pos_);
    } else {
      dsps_fird_s16(&this->fir_, in, out, out_count);
    }
  }

  bool prepare(size_t in_count) {
    if (this->ratio_ <= 1)
      return true;
    return this->ensure_scratch_(in_count);
  }

  void process_strided(const int16_t *in, int16_t *out, size_t out_count,
                       size_t stride, size_t offset) {
    if (this->ratio_ <= 1) {
      for (size_t i = 0; i < out_count; i++)
        out[i] = in[i * stride + offset];
      return;
    }
    size_t in_count = out_count * this->ratio_;
    if (!this->ensure_scratch_(in_count))
      return;
    for (size_t i = 0; i < in_count; i++)
      this->scratch_[i] = in[i * stride + offset];
    if (this->use_float_fir_) {
      fir_decimate_float(this->scratch_, out, static_cast<int32_t>(out_count),
                         this->ratio_, this->fdelay_, &this->fdelay_pos_);
    } else {
      dsps_fird_s16(&this->fir_, this->scratch_, out, static_cast<int32_t>(out_count));
    }
  }

  void process_strided_32(const int32_t *in, int16_t *out, size_t out_count,
                          size_t stride, size_t offset) {
    if (this->ratio_ <= 1) {
      for (size_t i = 0; i < out_count; i++)
        out[i] = static_cast<int16_t>(in[i * stride + offset] >> 16);
      return;
    }
    size_t in_count = out_count * this->ratio_;
    if (!this->ensure_scratch_(in_count))
      return;
    for (size_t i = 0; i < in_count; i++)
      this->scratch_[i] = static_cast<int16_t>(in[i * stride + offset] >> 16);
    if (this->use_float_fir_) {
      fir_decimate_float(this->scratch_, out, static_cast<int32_t>(out_count),
                         this->ratio_, this->fdelay_, &this->fdelay_pos_);
    } else {
      dsps_fird_s16(&this->fir_, this->scratch_, out, static_cast<int32_t>(out_count));
    }
  }

 private:
  bool ensure_scratch_(size_t count) {
    if (this->scratch_size_ >= count)
      return true;
    if (this->scratch_ != nullptr)
      heap_caps_free(this->scratch_);
    this->scratch_ = alloc_fir_int16_internal(count, "FirDecim");
    this->scratch_size_ = (this->scratch_ != nullptr) ? count : 0;
    return this->scratch_ != nullptr;
  }

  uint32_t ratio_{1};
  fir_s16_t fir_{};
  alignas(16) int16_t coeffs_local_[FIR_NUM_TAPS]{};
  alignas(16) int16_t delay_[FIR_NUM_TAPS]{};
  int16_t *scratch_{nullptr};
  size_t scratch_size_{0};
  // Custom float FIR state (used when use_float_fir_ is true).
  // Always present; init/reset are cheap (memset + 1 word).
  bool use_float_fir_{false};
  alignas(16) float fdelay_[FIR_NUM_TAPS]{};
  uint32_t fdelay_pos_{0};
};

class MultiChannelFirDecimatorImpl {
 public:
  ~MultiChannelFirDecimatorImpl() {
    for (uint8_t c = 0; c < MC_FIR_MAX_CH; c++) {
      dsps_fird_s16_aexx_free(&this->fir_ch_[c]);
      if (this->out_ch_[c] != nullptr)
        heap_caps_free(this->out_ch_[c]);
    }
    if (this->scratch_ != nullptr)
      heap_caps_free(this->scratch_);
  }

  void init(uint32_t ratio, uint8_t num_channels) {
    this->ratio_ = ratio;
    this->num_channels_ = num_channels > MC_FIR_MAX_CH ? MC_FIR_MAX_CH : num_channels;
    for (uint8_t c = 0; c < this->num_channels_; c++) {
      memcpy(this->coeffs_local_ch_[c], FIR_COEFFS_Q15, sizeof(FIR_COEFFS_Q15));
      dsps_fird_init_s16(&this->fir_ch_[c], this->coeffs_local_ch_[c],
                         this->delay_ch_[c], FIR_NUM_TAPS,
                         static_cast<int16_t>(ratio), 0, 0);
      dsps_16_array_rev(this->fir_ch_[c].coeffs, this->fir_ch_[c].coeffs_len);
    }
  }

  void reset() {
    for (uint8_t c = 0; c < MC_FIR_MAX_CH; c++) {
      memset(this->delay_ch_[c], 0, sizeof(this->delay_ch_[c]));
      this->fir_ch_[c].pos = 0;
      this->fir_ch_[c].d_pos = 0;
      memset(this->fdelay_ch_[c], 0, sizeof(this->fdelay_ch_[c]));
      this->fdelay_pos_ch_[c] = 0;
    }
  }

  void set_use_float_fir(bool b) { this->use_float_fir_ = b; }

  bool prepare(size_t in_count, size_t out_count, uint8_t num_channels) {
    if (this->ratio_ <= 1)
      return true;
    const uint8_t nch = std::min<uint8_t>(num_channels, MC_FIR_MAX_CH);
    return this->ensure_buffers_(in_count, out_count, nch);
  }

  void process_multi(const int16_t *in, size_t out_count, size_t in_stride,
                     const uint8_t *channel_offsets,
                     int16_t *mic_interleaved, int16_t *mic_mono,
                     int16_t *ref_out, uint8_t num_mic_ch) {
    if (this->ratio_ <= 1) {
      this->process_multi_passthrough_(in, out_count, in_stride, channel_offsets,
                                       mic_interleaved, mic_mono, ref_out, num_mic_ch);
      return;
    }
    const uint8_t nch = this->num_channels_;
    size_t in_count = out_count * this->ratio_;
    if (!this->ensure_buffers_(in_count, out_count, nch))
      return;

    for (uint8_t c = 0; c < nch; c++) {
      const uint8_t off = channel_offsets[c];
      for (size_t i = 0; i < in_count; i++)
        this->scratch_[i] = in[i * in_stride + off];
      if (this->use_float_fir_) {
        fir_decimate_float(this->scratch_, this->out_ch_[c], static_cast<int32_t>(out_count),
                           this->ratio_, this->fdelay_ch_[c], &this->fdelay_pos_ch_[c]);
      } else {
        dsps_fird_s16(&this->fir_ch_[c], this->scratch_, this->out_ch_[c],
                      static_cast<int32_t>(out_count));
      }
    }
    this->distribute_output_(out_count, mic_interleaved, mic_mono, ref_out, num_mic_ch);
  }

  void process_multi_32(const int32_t *in, size_t out_count, size_t in_stride,
                        const uint8_t *channel_offsets,
                        int16_t *mic_interleaved, int16_t *mic_mono,
                        int16_t *ref_out, uint8_t num_mic_ch) {
    if (this->ratio_ <= 1) {
      for (size_t o = 0; o < out_count; o++) {
        int16_t s0 = static_cast<int16_t>(in[o * in_stride + channel_offsets[0]] >> 16);
        if (mic_mono)
          mic_mono[o] = s0;
        if (num_mic_ch >= 2 && this->num_channels_ >= 2) {
          int16_t s1 = static_cast<int16_t>(in[o * in_stride + channel_offsets[1]] >> 16);
          if (mic_interleaved) {
            mic_interleaved[o * 2] = s0;
            mic_interleaved[o * 2 + 1] = s1;
          }
          if (ref_out && this->num_channels_ >= 3)
            ref_out[o] = static_cast<int16_t>(in[o * in_stride + channel_offsets[2]] >> 16);
        } else {
          if (ref_out && this->num_channels_ >= 2)
            ref_out[o] = static_cast<int16_t>(in[o * in_stride + channel_offsets[1]] >> 16);
        }
      }
      return;
    }
    const uint8_t nch = this->num_channels_;
    size_t in_count = out_count * this->ratio_;
    if (!this->ensure_buffers_(in_count, out_count, nch))
      return;

    for (uint8_t c = 0; c < nch; c++) {
      const uint8_t off = channel_offsets[c];
      for (size_t i = 0; i < in_count; i++)
        this->scratch_[i] = static_cast<int16_t>(in[i * in_stride + off] >> 16);
      if (this->use_float_fir_) {
        fir_decimate_float(this->scratch_, this->out_ch_[c], static_cast<int32_t>(out_count),
                           this->ratio_, this->fdelay_ch_[c], &this->fdelay_pos_ch_[c]);
      } else {
        dsps_fird_s16(&this->fir_ch_[c], this->scratch_, this->out_ch_[c],
                      static_cast<int32_t>(out_count));
      }
    }
    this->distribute_output_(out_count, mic_interleaved, mic_mono, ref_out, num_mic_ch);
  }

 private:
  void process_multi_passthrough_(const int16_t *in, size_t out_count, size_t in_stride,
                                  const uint8_t *channel_offsets,
                                  int16_t *mic_interleaved, int16_t *mic_mono,
                                  int16_t *ref_out, uint8_t num_mic_ch) {
    for (size_t o = 0; o < out_count; o++) {
      int16_t s0 = in[o * in_stride + channel_offsets[0]];
      if (mic_mono)
        mic_mono[o] = s0;
      if (num_mic_ch >= 2 && this->num_channels_ >= 2) {
        int16_t s1 = in[o * in_stride + channel_offsets[1]];
        if (mic_interleaved) {
          mic_interleaved[o * 2] = s0;
          mic_interleaved[o * 2 + 1] = s1;
        }
        if (ref_out && this->num_channels_ >= 3)
          ref_out[o] = in[o * in_stride + channel_offsets[2]];
      } else {
        if (ref_out && this->num_channels_ >= 2)
          ref_out[o] = in[o * in_stride + channel_offsets[1]];
      }
    }
  }

  void distribute_output_(size_t out_count, int16_t *mic_interleaved, int16_t *mic_mono,
                          int16_t *ref_out, uint8_t num_mic_ch) {
    for (size_t o = 0; o < out_count; o++) {
      int16_t s0 = this->out_ch_[0][o];
      if (mic_mono)
        mic_mono[o] = s0;
      if (num_mic_ch >= 2 && this->num_channels_ >= 2) {
        int16_t s1 = this->out_ch_[1][o];
        if (mic_interleaved) {
          mic_interleaved[o * 2] = s0;
          mic_interleaved[o * 2 + 1] = s1;
        }
        if (ref_out && this->num_channels_ >= 3)
          ref_out[o] = this->out_ch_[2][o];
      } else {
        if (ref_out && this->num_channels_ >= 2)
          ref_out[o] = this->out_ch_[1][o];
      }
    }
  }

  bool ensure_buffers_(size_t in_count, size_t out_count, uint8_t nch) {
    if (this->scratch_size_ >= in_count && this->out_size_ >= out_count &&
        this->out_channels_ >= nch)
      return true;
    if (this->scratch_ != nullptr) {
      heap_caps_free(this->scratch_);
      this->scratch_ = nullptr;
      this->scratch_size_ = 0;
    }
    for (uint8_t c = 0; c < MC_FIR_MAX_CH; c++) {
      if (this->out_ch_[c] != nullptr) {
        heap_caps_free(this->out_ch_[c]);
        this->out_ch_[c] = nullptr;
      }
    }
    this->out_size_ = 0;
    this->out_channels_ = 0;

    this->scratch_ = alloc_fir_int16_internal(in_count, "MCFirDecim");
    if (this->scratch_ == nullptr)
      return false;
    this->scratch_size_ = in_count;
    for (uint8_t c = 0; c < nch; c++) {
      this->out_ch_[c] = alloc_fir_int16_internal(out_count, "MCFirDecim");
      if (this->out_ch_[c] == nullptr) {
        for (uint8_t i = 0; i <= c; i++) {
          if (this->out_ch_[i] != nullptr) {
            heap_caps_free(this->out_ch_[i]);
            this->out_ch_[i] = nullptr;
          }
        }
        heap_caps_free(this->scratch_);
        this->scratch_ = nullptr;
        this->scratch_size_ = 0;
        return false;
      }
    }
    this->out_size_ = out_count;
    this->out_channels_ = nch;
    return true;
  }

  uint32_t ratio_{1};
  uint8_t num_channels_{0};
  fir_s16_t fir_ch_[MC_FIR_MAX_CH]{};
  alignas(16) int16_t coeffs_local_ch_[MC_FIR_MAX_CH][FIR_NUM_TAPS]{};
  alignas(16) int16_t delay_ch_[MC_FIR_MAX_CH][FIR_NUM_TAPS]{};
  int16_t *scratch_{nullptr};
  int16_t *out_ch_[MC_FIR_MAX_CH]{};
  size_t scratch_size_{0};
  size_t out_size_{0};
  uint8_t out_channels_{0};
  bool use_float_fir_{false};
  alignas(16) float fdelay_ch_[MC_FIR_MAX_CH][FIR_NUM_TAPS]{};
  uint32_t fdelay_pos_ch_[MC_FIR_MAX_CH]{};
};

// FirDecimator (outer) - thin Pimpl wrapper.
FirDecimator::FirDecimator() : impl_(std::make_unique<FirDecimatorImpl>()) {}
FirDecimator::~FirDecimator() = default;
void FirDecimator::init(uint32_t ratio) { this->impl_->init(ratio); }
void FirDecimator::reset() { this->impl_->reset(); }
void FirDecimator::set_use_float_fir(bool b) { this->impl_->set_use_float_fir(b); }
bool FirDecimator::prepare(size_t in_count) {
  return this->impl_->prepare(in_count);
}
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

// MultiChannelFirDecimator (outer) - thin Pimpl wrapper.
MultiChannelFirDecimator::MultiChannelFirDecimator()
    : impl_(std::make_unique<MultiChannelFirDecimatorImpl>()) {}
MultiChannelFirDecimator::~MultiChannelFirDecimator() = default;
void MultiChannelFirDecimator::init(uint32_t ratio, uint8_t num_channels) {
  this->impl_->init(ratio, num_channels);
}
void MultiChannelFirDecimator::reset() { this->impl_->reset(); }
void MultiChannelFirDecimator::set_use_float_fir(bool b) { this->impl_->set_use_float_fir(b); }
bool MultiChannelFirDecimator::prepare(size_t in_count, size_t out_count, uint8_t num_channels) {
  return this->impl_->prepare(in_count, out_count, num_channels);
}
void MultiChannelFirDecimator::process_multi(const int16_t *in, size_t out_count, size_t in_stride,
                                             const uint8_t *channel_offsets,
                                             int16_t *mic_interleaved, int16_t *mic_mono,
                                             int16_t *ref_out, uint8_t num_mic_ch) {
  this->impl_->process_multi(in, out_count, in_stride, channel_offsets,
                             mic_interleaved, mic_mono, ref_out, num_mic_ch);
}
void MultiChannelFirDecimator::process_multi_32(const int32_t *in, size_t out_count, size_t in_stride,
                                                const uint8_t *channel_offsets,
                                                int16_t *mic_interleaved, int16_t *mic_mono,
                                                int16_t *ref_out, uint8_t num_mic_ch) {
  this->impl_->process_multi_32(in, out_count, in_stride, channel_offsets,
                                mic_interleaved, mic_mono, ref_out, num_mic_ch);
}

}  // namespace i2s_audio_duplex
}  // namespace esphome

#endif  // USE_ESP32
