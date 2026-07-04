import esphome.codegen as cg
from esphome.components import audio, microphone
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

CONF_INPUT_PATH = "input_path"
CONF_FRAME_MS = "frame_ms"
CONF_REPEAT = "repeat"

virtual_microphone_ns = cg.esphome_ns.namespace("virtual_microphone")
VirtualMicrophone = virtual_microphone_ns.class_(
    "VirtualMicrophone", cg.Component, microphone.Microphone
)


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
    microphone.MICROPHONE_SCHEMA.extend(
        {
            cv.GenerateID(): cv.declare_id(VirtualMicrophone),
            cv.Required(CONF_INPUT_PATH): cv.string,
            cv.Optional(CONF_SAMPLE_RATE, default=16000): cv.int_range(8000, 48000),
            cv.Optional(CONF_BITS_PER_SAMPLE, default=16): cv.one_of(16, int=True),
            cv.Optional(CONF_NUM_CHANNELS, default=1): cv.one_of(1, int=True),
            cv.Optional(CONF_FRAME_MS, default=20): cv.int_range(5, 100),
            cv.Optional(CONF_REPEAT, default=False): cv.boolean,
        }
    ).extend(cv.COMPONENT_SCHEMA),
    cv.only_on(PLATFORM_HOST),
    _set_stream_limits,
)


async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
    await microphone.register_microphone(var, config)

    cg.add(var.set_input_path(config[CONF_INPUT_PATH]))
    cg.add(var.set_frame_ms(config[CONF_FRAME_MS]))
    cg.add(var.set_repeat(config[CONF_REPEAT]))
    cg.add(
        var.set_audio_stream_info(
            config[CONF_BITS_PER_SAMPLE],
            config[CONF_NUM_CHANNELS],
            config[CONF_SAMPLE_RATE],
        )
    )
