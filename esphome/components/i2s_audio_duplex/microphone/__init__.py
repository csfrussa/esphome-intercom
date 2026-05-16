"""I2S Audio Duplex Microphone Platform - Wraps duplex bus as standard ESPHome microphone"""
import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.components import audio, microphone
from esphome.const import CONF_BITS_PER_SAMPLE, CONF_ID, CONF_NUM_CHANNELS, CONF_SAMPLE_RATE
from .. import (
    i2s_audio_duplex_ns,
    I2SAudioDuplex,
    CONF_I2S_AUDIO_DUPLEX_ID,
)

DEPENDENCIES = ["i2s_audio_duplex"]
CODEOWNERS = ["@n-IA-hane"]

I2SAudioDuplexMicrophone = i2s_audio_duplex_ns.class_(
    "I2SAudioDuplexMicrophone",
    microphone.Microphone,
    cg.Component,
    cg.Parented.template(I2SAudioDuplex),
)


def _set_stream_limits(config):
    # Mic output is at output_sample_rate (e.g. 16kHz after decimation).
    # Allow range to cover the standard 16 kHz VA path and supported output rates.
    audio.set_stream_limits(
        min_bits_per_sample=16,
        max_bits_per_sample=16,
        min_channels=1,
        max_channels=1,
        min_sample_rate=8000,
        max_sample_rate=48000,
    )(config)
    return config


def _reject_child_audio_overrides(config):
    """The duplex microphone format is owned by the shared full-duplex bus.

    `microphone.MICROPHONE_SCHEMA` inherits the generic audio keys, but applying
    them on the child would be misleading: the C++ stream format is derived from
    the parent `i2s_audio_duplex` bus and `output_sample_rate`.
    """
    invalid = [
        key
        for key in (CONF_SAMPLE_RATE, CONF_BITS_PER_SAMPLE, CONF_NUM_CHANNELS)
        if key in config
    ]
    if invalid:
        fields = ", ".join(invalid)
        raise cv.Invalid(
            f"{fields} must be configured on i2s_audio_duplex, not on "
            "microphone.platform: i2s_audio_duplex. The duplex microphone "
            "publishes the parent output stream."
        )
    return config


CONFIG_SCHEMA = cv.All(
    microphone.MICROPHONE_SCHEMA.extend(
        {
            cv.GenerateID(): cv.declare_id(I2SAudioDuplexMicrophone),
            cv.GenerateID(CONF_I2S_AUDIO_DUPLEX_ID): cv.use_id(I2SAudioDuplex),
        }
    ).extend(cv.COMPONENT_SCHEMA),
    _reject_child_audio_overrides,
    _set_stream_limits,
)


async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
    await microphone.register_microphone(var, config)

    parent = await cg.get_variable(config[CONF_I2S_AUDIO_DUPLEX_ID])
    cg.add(var.set_parent(parent))
