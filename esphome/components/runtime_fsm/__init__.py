import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.components import media_player, micro_wake_word, script, switch, voice_assistant
from esphome.components.mixer import speaker as mixer_speaker
from esphome.const import CONF_ID, CONF_MEDIA_PLAYER

CODEOWNERS = ["@n-IA-hane"]
DEPENDENCIES = ["media_player", "voice_assistant"]

runtime_fsm_ns = cg.esphome_ns.namespace("runtime_fsm")
RuntimeFsm = runtime_fsm_ns.class_("RuntimeFsm", cg.Component)

CONF_VOICE_ASSISTANT_ID = "voice_assistant_id"
CONF_MICRO_WAKE_WORD_ID = "micro_wake_word_id"
CONF_MEDIA_MIXER_INPUT = "media_mixer_input"
CONF_OUTPUT_SCRIPT = "output_script"
CONF_INTERCOM_ID = "intercom_id"
CONF_MIC_MUTE_SWITCH = "mic_mute_switch"
CONF_SPEAKER_MUTE_SWITCH = "speaker_mute_switch"

CONFIG_SCHEMA = cv.Schema(
    {
        cv.GenerateID(): cv.declare_id(RuntimeFsm),
        cv.Required(CONF_VOICE_ASSISTANT_ID): cv.use_id(voice_assistant.VoiceAssistant),
        cv.Optional(CONF_MICRO_WAKE_WORD_ID): cv.use_id(micro_wake_word.MicroWakeWord),
        cv.Required(CONF_MEDIA_MIXER_INPUT): cv.use_id(mixer_speaker.SourceSpeaker),
        cv.Required(CONF_MEDIA_PLAYER): cv.use_id(media_player.MediaPlayer),
        cv.Optional(CONF_OUTPUT_SCRIPT): cv.use_id(script.Script),
        cv.Optional(CONF_INTERCOM_ID): cv.use_id(cg.esphome_ns.namespace("intercom_api").class_("IntercomApi", cg.Component)),
        cv.Optional(CONF_MIC_MUTE_SWITCH): cv.use_id(switch.Switch),
        cv.Optional(CONF_SPEAKER_MUTE_SWITCH): cv.use_id(switch.Switch),
    }
).extend(cv.COMPONENT_SCHEMA)


async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)

    va = await cg.get_variable(config[CONF_VOICE_ASSISTANT_ID])
    cg.add(var.set_voice_assistant(va))

    if CONF_MICRO_WAKE_WORD_ID in config:
        mww = await cg.get_variable(config[CONF_MICRO_WAKE_WORD_ID])
        cg.add(var.set_micro_wake_word(mww))

    media_mixer = await cg.get_variable(config[CONF_MEDIA_MIXER_INPUT])
    cg.add(var.set_media_mixer_input(media_mixer))

    mp = await cg.get_variable(config[CONF_MEDIA_PLAYER])
    cg.add(var.set_media_player(mp))

    if CONF_OUTPUT_SCRIPT in config:
        output_script = await cg.get_variable(config[CONF_OUTPUT_SCRIPT])
        cg.add(var.set_output_script(output_script))

    if CONF_INTERCOM_ID in config:
        intercom = await cg.get_variable(config[CONF_INTERCOM_ID])
        cg.add(var.set_intercom(intercom))

    if CONF_MIC_MUTE_SWITCH in config:
        mic_mute = await cg.get_variable(config[CONF_MIC_MUTE_SWITCH])
        cg.add(var.set_mic_mute_switch(mic_mute))

    if CONF_SPEAKER_MUTE_SWITCH in config:
        speaker_mute = await cg.get_variable(config[CONF_SPEAKER_MUTE_SWITCH])
        cg.add(var.set_speaker_mute_switch(speaker_mute))
