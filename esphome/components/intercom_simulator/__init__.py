import esphome.config_validation as cv
import esphome.codegen as cg
from esphome.const import CONF_ID

CODEOWNERS = ["@n-IA-hane"]

intercom_simulator_ns = cg.esphome_ns.namespace("intercom_simulator")
IntercomSimulator = intercom_simulator_ns.class_("IntercomSimulator", cg.Component)

CONF_SOURCE_PROFILE = "source_profile"

CONFIG_SCHEMA = cv.Schema(
    {
        cv.GenerateID(): cv.declare_id(IntercomSimulator),
        cv.Optional(CONF_SOURCE_PROFILE, default=""): cv.string,
    }
).extend(cv.COMPONENT_SCHEMA)


async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
    cg.add(var.set_source_profile(config[CONF_SOURCE_PROFILE]))
