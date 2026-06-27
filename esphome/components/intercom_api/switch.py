import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.components import switch
from esphome.const import ENTITY_CATEGORY_CONFIG

from . import intercom_api_ns, IntercomApi, CONF_INTERCOM_API_ID

DEPENDENCIES = ["intercom_api"]

# Switch types
CONF_ACTIVE = "active"
CONF_AUTO_ANSWER = "auto_answer"
CONF_DND = "dnd"
CONF_AEC = "aec"

# C++ classes (simple - parent syncs state after boot)
IntercomApiSwitch = intercom_api_ns.class_(
    "IntercomApiSwitch", switch.Switch, cg.Parented.template(IntercomApi)
)
IntercomApiAutoAnswer = intercom_api_ns.class_(
    "IntercomApiAutoAnswer", switch.Switch, cg.Parented.template(IntercomApi)
)
IntercomApiDndSwitch = intercom_api_ns.class_(
    "IntercomApiDndSwitch", switch.Switch, cg.Parented.template(IntercomApi)
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
            cv.GenerateID(CONF_INTERCOM_API_ID): cv.use_id(IntercomApi),
        }
    )


CONFIG_SCHEMA = cv.Schema(
    {
        cv.GenerateID(CONF_INTERCOM_API_ID): cv.use_id(IntercomApi),
        # On/off control for intercom
        cv.Optional(CONF_ACTIVE): _switch_schema(IntercomApiSwitch, "mdi:phone"),
        # Auto-answer incoming calls (default ON)
        cv.Optional(CONF_AUTO_ANSWER): _switch_schema(
            IntercomApiAutoAnswer, "mdi:phone-in-talk", entity_category=ENTITY_CATEGORY_CONFIG
        ),
        # Do-not-disturb: reject incoming SIP INVITE with 486 Busy Here.
        cv.Optional(CONF_DND): _switch_schema(
            IntercomApiDndSwitch, "mdi:minus-circle", entity_category=ENTITY_CATEGORY_CONFIG
        ),
        cv.Optional(CONF_AEC): cv.invalid(
            "intercom_api AEC switch was removed with standalone intercom AEC. "
            "Use esp_audio_stack/esp_afe/esp_aec controls for software AEC."
        ),
    }
)


async def to_code(config):
    parent = await cg.get_variable(config[CONF_INTERCOM_API_ID])

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
