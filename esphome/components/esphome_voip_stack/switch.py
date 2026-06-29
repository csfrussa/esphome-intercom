import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.components import switch
from esphome.const import ENTITY_CATEGORY_CONFIG

from . import esphome_voip_stack_ns, ESPHomeVoipStack, CONF_ESPHOME_VOIP_STACK_ID

DEPENDENCIES = ["esphome_voip_stack"]

# Switch types
CONF_ACTIVE = "active"
CONF_AUTO_ANSWER = "auto_answer"
CONF_DND = "dnd"
CONF_AEC = "aec"

# C++ classes (simple - parent syncs state after boot)
ESPHomeVoipStackSwitch = esphome_voip_stack_ns.class_(
    "ESPHomeVoipStackSwitch", switch.Switch, cg.Parented.template(ESPHomeVoipStack)
)
ESPHomeVoipStackAutoAnswer = esphome_voip_stack_ns.class_(
    "ESPHomeVoipStackAutoAnswer", switch.Switch, cg.Parented.template(ESPHomeVoipStack)
)
ESPHomeVoipStackDndSwitch = esphome_voip_stack_ns.class_(
    "ESPHomeVoipStackDndSwitch", switch.Switch, cg.Parented.template(ESPHomeVoipStack)
)

def _switch_schema(switch_class, icon, entity_category=None):
    """Create switch schema for a specific switch type."""
    kwargs = {"icon": icon}
    if entity_category is not None:
        kwargs["entity_category"] = entity_category
    return switch.switch_schema(
        switch_class,
        **kwargs,
    ).extend(
        {
            cv.GenerateID(CONF_ESPHOME_VOIP_STACK_ID): cv.use_id(ESPHomeVoipStack),
        }
    )


CONFIG_SCHEMA = cv.Schema(
    {
        cv.GenerateID(CONF_ESPHOME_VOIP_STACK_ID): cv.use_id(ESPHomeVoipStack),
        # On/off control for VoIP calls
        cv.Optional(CONF_ACTIVE): _switch_schema(ESPHomeVoipStackSwitch, "mdi:phone"),
        # Auto-answer incoming calls (default ON)
        cv.Optional(CONF_AUTO_ANSWER): _switch_schema(
            ESPHomeVoipStackAutoAnswer, "mdi:phone-in-talk", entity_category=ENTITY_CATEGORY_CONFIG
        ),
        # Do-not-disturb: reject incoming SIP INVITE with 486 Busy Here.
        cv.Optional(CONF_DND): _switch_schema(
            ESPHomeVoipStackDndSwitch, "mdi:minus-circle", entity_category=ENTITY_CATEGORY_CONFIG
        ),
        cv.Optional(CONF_AEC): cv.invalid(
            "esphome_voip_stack AEC switch was removed with standalone VoIP AEC. "
            "Use esp_audio_stack/esp_afe/esp_aec controls for software AEC."
        ),
    }
)


async def to_code(config):
    parent = await cg.get_variable(config[CONF_ESPHOME_VOIP_STACK_ID])

    if CONF_ACTIVE in config:
        conf = config[CONF_ACTIVE]
        var = await switch.new_switch(conf)
        cg.add(var.set_parent(parent))

    if CONF_AUTO_ANSWER in config:
        conf = config[CONF_AUTO_ANSWER]
        var = await switch.new_switch(conf)
        cg.add(var.set_parent(parent))
        # Register with parent for state sync after boot
        cg.add(parent.register_auto_answer_switch(var))

    if CONF_DND in config:
        conf = config[CONF_DND]
        var = await switch.new_switch(conf)
        cg.add(var.set_parent(parent))
        cg.add(parent.register_dnd_switch(var))
