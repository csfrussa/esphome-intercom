import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.components import switch
from esphome.core import CORE
from esphome.const import ENTITY_CATEGORY_CONFIG

from . import intercom_api_ns, IntercomApi, CONF_INTERCOM_API_ID, CONF_PROCESSOR_ID

DEPENDENCIES = ["intercom_api"]

# Switch types
CONF_ACTIVE = "active"
CONF_AUTO_ANSWER = "auto_answer"
CONF_DND = "dnd"
CONF_AEC = "aec"
CONF_HA_PBX_MODE = "ha_pbx_mode"

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
IntercomAecSwitch = intercom_api_ns.class_(
    "IntercomAecSwitch", switch.Switch, cg.Parented.template(IntercomApi)
)
IntercomRoutingModeSwitch = intercom_api_ns.class_(
    "IntercomRoutingModeSwitch", switch.Switch, cg.Parented.template(IntercomApi)
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
        # Do-not-disturb: reject incoming START with DECLINE("DND").
        cv.Optional(CONF_DND): _switch_schema(
            IntercomApiDndSwitch, "mdi:minus-circle", entity_category=ENTITY_CATEGORY_CONFIG
        ),
        # AEC (Echo Cancellation) - default OFF
        cv.Optional(CONF_AEC): _switch_schema(
            IntercomAecSwitch, "mdi:ear-hearing", entity_category=ENTITY_CATEGORY_CONFIG
        ),
        # Routing mode runtime toggle: ON = ha_pbx (HA bridges every call),
        # OFF = device_independent (peer-to-peer dial). Mirrors the YAML
        # `intercom_api.routing_mode` option so users can A/B test from
        # the HA frontend without re-flashing.
        cv.Optional(CONF_HA_PBX_MODE): _switch_schema(
            IntercomRoutingModeSwitch, "mdi:phone-forward",
            entity_category=ENTITY_CATEGORY_CONFIG,
        ),
    }
)


def _final_validate(config):
    if CONF_AEC not in config:
        return config

    full_config = CORE.config or {}
    intercom_configs = full_config.get("intercom_api", [])
    if isinstance(intercom_configs, dict):
        intercom_configs = [intercom_configs]
    has_standalone_audio = any(
        isinstance(intercom, dict) and CONF_PROCESSOR_ID in intercom for intercom in intercom_configs
    )
    if not has_standalone_audio:
        raise cv.Invalid(
            "intercom_api.switch.aec is only available for the legacy standalone "
            "intercom_api processor_id path. With esp_audio_stack, put AEC/AFE on "
            "esp_audio_stack and do not create an intercom_api AEC switch."
        )
    return config


FINAL_VALIDATE_SCHEMA = _final_validate


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

    if CONF_AEC in config:
        conf = config[CONF_AEC]
        var = await switch.new_switch(conf)
        cg.add(var.set_parent(parent))
        # Register with parent for state sync after boot
        cg.add(parent.register_aec_switch(var))

    if CONF_HA_PBX_MODE in config:
        conf = config[CONF_HA_PBX_MODE]
        var = await switch.new_switch(conf)
        cg.add(var.set_parent(parent))
        cg.add(parent.register_routing_mode_switch(var))
