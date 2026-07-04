import esphome.codegen as cg
from esphome.components import audio, speaker
import esphome.config_validation as cv
from esphome.const import (
    CONF_BITS_PER_SAMPLE,
    CONF_ID,
    CONF_NUM_CHANNELS,
    CONF_SAMPLE_RATE,
)
from esphome.const import PLATFORM_HOST

CODEOWNERS = ["@n-IA-hane"]
AUTO_LOAD = ["audio"]

CONF_OUTPUT_PATH = "output_path"

virtual_speaker_ns = cg.esphome_ns.namespace("virtual_speaker")
VirtualSpeaker = virtual_speaker_ns.class_("VirtualSpeaker", cg.Component, speaker.Speaker)


def _set_stream_limits(config):
    audio.set_stream_limits(
        min_bits_per_sample=config[CONF_BITS_PER_SAMPLE],
        max_bits_per_sample=config[CONF_BITS_PER_SAMPLE],
        min_channels=config[CONF_NUM_CHANNELS],
        max_channels=config[CONF_NUM_CHANNELS],
        min_sample_rate=config[CONF_SAMPLE_RATE],
        max_sample_rate=config[CONF_SAMPLE_RATE],
    )(config)
    return config


CONFIG_SCHEMA = cv.All(
    speaker.SPEAKER_SCHEMA.extend(
        {
            cv.GenerateID(): cv.declare_id(VirtualSpeaker),
            cv.Required(CONF_OUTPUT_PATH): cv.string,
            cv.Optional(CONF_SAMPLE_RATE, default=16000): cv.int_range(8000, 48000),
            cv.Optional(CONF_BITS_PER_SAMPLE, default=16): cv.one_of(16, int=True),
            cv.Optional(CONF_NUM_CHANNELS, default=1): cv.one_of(1, int=True),
        }
    ).extend(cv.COMPONENT_SCHEMA),
    cv.only_on(PLATFORM_HOST),
    _set_stream_limits,
)


async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
    await speaker.register_speaker(var, config)

    cg.add(var.set_output_path(config[CONF_OUTPUT_PATH]))
    cg.add(
        var.set_audio_stream_info(
            config[CONF_BITS_PER_SAMPLE],
            config[CONF_NUM_CHANNELS],
            config[CONF_SAMPLE_RATE],
        )
    )
