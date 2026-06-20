from esphome import automation
import esphome.codegen as cg
import esphome.config_validation as cv
from esphome.components import globals as globals_component, script
from esphome.const import CONF_ID, CONF_NAME, CONF_THEN

CODEOWNERS = ["@n-IA-hane"]
DEPENDENCIES = []

runtime_fsm_ns = cg.esphome_ns.namespace("runtime_fsm")
RuntimeFsm = runtime_fsm_ns.class_("RuntimeFsm", cg.Component)
EventAction = runtime_fsm_ns.class_(
    "EventAction", automation.Action, cg.Parented.template(RuntimeFsm)
)
SetActivityAction = runtime_fsm_ns.class_(
    "SetActivityAction", automation.Action, cg.Parented.template(RuntimeFsm)
)
SetActivitiesAction = runtime_fsm_ns.class_(
    "SetActivitiesAction", automation.Action, cg.Parented.template(RuntimeFsm)
)
RequestActionAction = runtime_fsm_ns.class_(
    "RequestActionAction", automation.Action, cg.Parented.template(RuntimeFsm)
)
DumpAction = runtime_fsm_ns.class_(
    "DumpAction", automation.Action, cg.Parented.template(RuntimeFsm)
)
IsActiveCondition = runtime_fsm_ns.class_(
    "IsActiveCondition", automation.Condition, cg.Parented.template(RuntimeFsm)
)

CONF_DEBUG = "debug"
CONF_OUTPUT_SCRIPT = "output_script"
CONF_STATE_OUTPUTS = "state_outputs"
CONF_ACTIVITY_MASK = "activity_mask"
CONF_SEQUENCE = "sequence"
CONF_INTERCOM_ID = "intercom_id"
CONF_INTERCOM = "intercom"
CONF_ACTIVITY_PREFIX = "activity_prefix"
CONF_ACTIVITIES = "activities"
CONF_GROUPS = "groups"
CONF_AUTO_EVENTS = "auto_events"
CONF_DERIVED_ACTIVITIES = "derived_activities"
CONF_EVENTS = "events"
CONF_SET = "set"
CONF_POLICIES = "policies"
CONF_GROUP = "group"
CONF_PRIORITY = "priority"
CONF_INITIAL = "initial"
CONF_ACTIONS = "actions"
CONF_VALUES = "values"
CONF_OUTPUT = "output"
CONF_ON_CHANGE = "on_change"
CONF_VALUE = "value"
CONF_EVENT = "event"
CONF_ACTIVITY = "activity"
CONF_ACTIVE = "active"
CONF_ACTION = "action"
CONF_REASON = "reason"
CONF_DUMP = "dump"
CONF_RULES = "rules"
CONF_WHEN = "when"
CONF_ANY_ACTIVE = "any_active"
CONF_ALL_ACTIVE = "all_active"
CONF_NONE_ACTIVE = "none_active"
CONF_ACTIVATE = "activate"
CONF_DEACTIVATE = "deactivate"
CONF_CASES = "cases"
CONF_ANY = "any"
CONF_ALL = "all"
CONF_NONE = "none"
CONF_STATES = "states"


def _list_or_one(value_schema):
    return cv.Any(value_schema, cv.ensure_list(value_schema))


def _as_list(value):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]

ACTIVITY_BODY_SCHEMA = cv.Schema(
    {
        cv.Optional(CONF_PRIORITY, default=0): cv.int_range(min=-32768, max=32767),
        cv.Optional(CONF_INITIAL, default=False): cv.boolean,
        cv.Optional(CONF_POLICIES, default={}): cv.Schema({cv.string_strict: cv.string_strict}),
    }
)
ACTIVITIES_SCHEMA = cv.Schema({cv.string_strict: ACTIVITY_BODY_SCHEMA})
GROUPS_SCHEMA = cv.Schema({cv.string_strict: cv.ensure_list(cv.string_strict)})

ACTION_TRIGGER_SCHEMA = automation.validate_automation(single=True)
POLICY_VALUE_SCHEMA = cv.Any(
    cv.int_,
    automation.validate_automation({cv.Optional(CONF_VALUE): cv.int_}, single=True),
)
POLICY_SCHEMA = cv.Schema(
    {
        cv.Optional(CONF_OUTPUT): cv.use_id(globals_component.GlobalsComponent),
        cv.Optional(CONF_VALUES, default={}): cv.Schema({cv.string_strict: POLICY_VALUE_SCHEMA}),
        cv.Optional(CONF_ON_CHANGE): ACTION_TRIGGER_SCHEMA,
    }
)

EVENT_CASE_SCHEMA = cv.Schema(
    {
        cv.Optional(CONF_ANY, default=[]): _list_or_one(cv.string_strict),
        cv.Optional(CONF_ALL, default=[]): _list_or_one(cv.string_strict),
        cv.Optional(CONF_NONE, default=[]): _list_or_one(cv.string_strict),
        cv.Optional(CONF_ACTIVATE, default=[]): _list_or_one(cv.string_strict),
        cv.Optional(CONF_DEACTIVATE, default=[]): _list_or_one(cv.string_strict),
        cv.Optional(CONF_ACTION): cv.string_strict,
    }
)
EVENT_SCHEMA = cv.Schema(
    {
        cv.Optional(CONF_ACTIVATE, default=[]): _list_or_one(cv.string_strict),
        cv.Optional(CONF_DEACTIVATE, default=[]): _list_or_one(cv.string_strict),
        cv.Optional(CONF_ACTION): cv.string_strict,
        cv.Optional(CONF_CASES, default=[]): cv.ensure_list(EVENT_CASE_SCHEMA),
        cv.Optional(CONF_THEN): ACTION_TRIGGER_SCHEMA,
    }
)

DERIVED_ACTIVITY_SCHEMA = cv.Schema(
    {
        cv.Required(CONF_NAME): cv.string_strict,
        cv.Optional(CONF_WHEN, default={}): cv.Schema(
            {
                cv.Optional(CONF_ANY_ACTIVE, default=[]): cv.ensure_list(cv.string_strict),
                cv.Optional(CONF_ALL_ACTIVE, default=[]): cv.ensure_list(cv.string_strict),
                cv.Optional(CONF_NONE_ACTIVE, default=[]): cv.ensure_list(cv.string_strict),
            }
        ),
    }
)

CONFIG_SCHEMA = cv.Schema(
    {
        cv.GenerateID(): cv.declare_id(RuntimeFsm),
        cv.Optional(CONF_DEBUG, default=False): cv.boolean,
        cv.Optional(CONF_OUTPUT_SCRIPT): cv.use_id(script.Script),
        cv.Optional(CONF_STATE_OUTPUTS, default={}): cv.Schema(
            {
                cv.Optional(CONF_ACTIVITY_MASK): cv.use_id(globals_component.GlobalsComponent),
                cv.Optional(CONF_SEQUENCE): cv.use_id(globals_component.GlobalsComponent),
            }
        ),
        cv.Optional(CONF_INTERCOM): cv.Schema(
            {
                cv.Required(CONF_ID): cv.use_id(cg.esphome_ns.namespace("intercom_api").class_("IntercomApi", cg.Component)),
                cv.Optional(CONF_ACTIVITY_PREFIX, default="intercom:"): cv.string_strict,
                cv.Optional(CONF_STATES, default={}): cv.Schema({cv.string_strict: ACTIVITY_BODY_SCHEMA}),
            }
        ),
        cv.Optional(CONF_ACTIVITIES, default={}): ACTIVITIES_SCHEMA,
        cv.Optional(CONF_GROUPS, default={}): GROUPS_SCHEMA,
        cv.Optional(CONF_AUTO_EVENTS, default=True): cv.boolean,
        cv.Optional(CONF_DERIVED_ACTIVITIES, default=[]): cv.ensure_list(DERIVED_ACTIVITY_SCHEMA),
        cv.Optional(CONF_EVENTS, default={}): cv.Schema({cv.string_strict: EVENT_SCHEMA}),
        cv.Optional(CONF_ACTIONS, default={}): cv.Schema({cv.string_strict: ACTION_TRIGGER_SCHEMA}),
        cv.Optional(CONF_POLICIES, default={}): cv.Schema({cv.string_strict: POLICY_SCHEMA}),
    }
).extend(cv.COMPONENT_SCHEMA)


async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
    cg.add(var.set_debug(config[CONF_DEBUG]))
    if config[CONF_DEBUG]:
        cg.add_define("USE_RUNTIME_FSM_DEBUG")

    if CONF_OUTPUT_SCRIPT in config:
        output_script = await cg.get_variable(config[CONF_OUTPUT_SCRIPT])
        cg.add(var.set_output_script(output_script))

    state_outputs = config[CONF_STATE_OUTPUTS]
    if CONF_ACTIVITY_MASK in state_outputs:
        full_id, output = await cg.get_variable_with_full_id(state_outputs[CONF_ACTIVITY_MASK])
        template_arg = cg.TemplateArguments(full_id.type)
        cg.add(var.set_activity_mask_output.template(template_arg)(output))
    if CONF_SEQUENCE in state_outputs:
        full_id, output = await cg.get_variable_with_full_id(state_outputs[CONF_SEQUENCE])
        template_arg = cg.TemplateArguments(full_id.type)
        cg.add(var.set_sequence_output.template(template_arg)(output))

    if CONF_INTERCOM in config:
        intercom_conf = config[CONF_INTERCOM]
        intercom = await cg.get_variable(intercom_conf[CONF_ID])
        cg.add(var.set_intercom(intercom))
        cg.add(var.set_intercom_activity_prefix(intercom_conf[CONF_ACTIVITY_PREFIX]))
        cg.add_define("USE_RUNTIME_FSM_INTERCOM")
        for state, activity in intercom_conf[CONF_STATES].items():
            name = f"{intercom_conf[CONF_ACTIVITY_PREFIX]}{state}"
            cg.add(var.add_activity(name, activity[CONF_PRIORITY], activity[CONF_INITIAL]))
            for policy, value in activity[CONF_POLICIES].items():
                cg.add(var.add_activity_policy(name, policy, value))

    for name, activity in config[CONF_ACTIVITIES].items():
        cg.add(
            var.add_activity(
                name,
                activity[CONF_PRIORITY],
                activity[CONF_INITIAL],
            )
        )
        for policy, value in activity[CONF_POLICIES].items():
            cg.add(var.add_activity_policy(name, policy, value))

    for group, activities in config[CONF_GROUPS].items():
        for activity in activities:
            cg.add(var.set_activity_group(activity, group))

    if config[CONF_AUTO_EVENTS]:
        for name in config[CONF_ACTIVITIES]:
            cg.add(var.add_event_activity(name, name, True))

    for derived in config[CONF_DERIVED_ACTIVITIES]:
        cg.add(var.add_derived_activity(derived[CONF_NAME]))
        for activity in derived[CONF_WHEN][CONF_ANY_ACTIVE]:
            cg.add(var.add_derived_any_active(activity))
        for activity in derived[CONF_WHEN][CONF_ALL_ACTIVE]:
            cg.add(var.add_derived_all_active(activity))
        for activity in derived[CONF_WHEN][CONF_NONE_ACTIVE]:
            cg.add(var.add_derived_none_active(activity))

    for name, event_conf in config[CONF_EVENTS].items():
        for rule in event_conf[CONF_CASES]:
            cg.add(var.add_event_rule(name, rule.get(CONF_ACTION, "")))
            for activity in _as_list(rule[CONF_ANY]):
                cg.add(var.add_event_rule_any_active(activity))
            for activity in _as_list(rule[CONF_ALL]):
                cg.add(var.add_event_rule_all_active(activity))
            for activity in _as_list(rule[CONF_NONE]):
                cg.add(var.add_event_rule_none_active(activity))
            for activity in _as_list(rule[CONF_ACTIVATE]):
                cg.add(var.add_event_rule_update(activity, True))
            for activity in _as_list(rule[CONF_DEACTIVATE]):
                cg.add(var.add_event_rule_update(activity, False))
        default_activate = _as_list(event_conf[CONF_ACTIVATE])
        default_deactivate = _as_list(event_conf[CONF_DEACTIVATE])
        default_action = event_conf.get(CONF_ACTION, "")
        if default_activate or default_deactivate or default_action:
            cg.add(var.add_event_rule(name, default_action))
            for activity in default_activate:
                cg.add(var.add_event_rule_update(activity, True))
            for activity in default_deactivate:
                cg.add(var.add_event_rule_update(activity, False))
        if CONF_THEN in event_conf:
            trigger = cg.new_Pvariable(event_conf[automation.CONF_TRIGGER_ID], cg.TemplateArguments())
            cg.add(var.add_action_trigger(name, trigger))
            await automation.build_automation(trigger, [], event_conf)

    for name, action_conf in config[CONF_ACTIONS].items():
        trigger = cg.new_Pvariable(action_conf[automation.CONF_TRIGGER_ID], cg.TemplateArguments())
        cg.add(var.add_action_trigger(name, trigger))
        await automation.build_automation(trigger, [], action_conf)

    for policy, policy_conf in config[CONF_POLICIES].items():
        if CONF_OUTPUT in policy_conf:
            full_id, output = await cg.get_variable_with_full_id(policy_conf[CONF_OUTPUT])
            template_arg = cg.TemplateArguments(full_id.type)
            cg.add(var.add_policy_global_output.template(template_arg)(policy, output))
        if CONF_ON_CHANGE in policy_conf:
            trigger = cg.new_Pvariable(
                policy_conf[CONF_ON_CHANGE][automation.CONF_TRIGGER_ID],
                cg.TemplateArguments(cg.int32),
            )
            cg.add(var.set_policy_change_trigger(policy, trigger))
            await automation.build_automation(trigger, [(cg.int32, "value")], policy_conf[CONF_ON_CHANGE])
        for value, action_conf in policy_conf[CONF_VALUES].items():
            if isinstance(action_conf, int):
                cg.add(var.add_policy_output(policy, value, action_conf))
                continue
            if CONF_VALUE in action_conf:
                cg.add(var.add_policy_output(policy, value, action_conf[CONF_VALUE]))
            if CONF_THEN in action_conf:
                trigger = cg.new_Pvariable(action_conf[automation.CONF_TRIGGER_ID], cg.TemplateArguments())
                cg.add(var.add_policy_value_trigger(policy, value, trigger))
                await automation.build_automation(trigger, [], action_conf)


async def _new_parented_action(config, action_id, template_arg):
    var = cg.new_Pvariable(action_id, template_arg)
    await cg.register_parented(var, config[CONF_ID])
    return var


@automation.register_action(
    "runtime_fsm.event",
    EventAction,
    cv.Schema(
        {
            cv.GenerateID(): cv.use_id(RuntimeFsm),
            cv.Required(CONF_EVENT): cv.templatable(cv.string),
            cv.Optional(CONF_DUMP, default=False): cv.boolean,
            cv.Optional(CONF_REASON, default=""): cv.templatable(cv.string),
        }
    ),
    synchronous=True,
)
async def event_action_to_code(config, action_id, template_arg, args):
    var = await _new_parented_action(config, action_id, template_arg)
    templ = await cg.templatable(config[CONF_EVENT], args, cg.std_string)
    cg.add(var.set_event(templ))
    cg.add(var.set_dump(config[CONF_DUMP]))
    reason = await cg.templatable(config[CONF_REASON], args, cg.std_string)
    cg.add(var.set_reason(reason))
    return var


@automation.register_action(
    "runtime_fsm.set_activity",
    SetActivityAction,
    cv.Schema(
        {
            cv.GenerateID(): cv.use_id(RuntimeFsm),
            cv.Required(CONF_ACTIVITY): cv.templatable(cv.string),
            cv.Required(CONF_ACTIVE): cv.templatable(cv.boolean),
        }
    ),
    synchronous=True,
)
async def set_activity_action_to_code(config, action_id, template_arg, args):
    var = await _new_parented_action(config, action_id, template_arg)
    activity = await cg.templatable(config[CONF_ACTIVITY], args, cg.std_string)
    active = await cg.templatable(config[CONF_ACTIVE], args, cg.bool_)
    cg.add(var.set_activity(activity))
    cg.add(var.set_active(active))
    return var


@automation.register_action(
    "runtime_fsm.set_activities",
    SetActivitiesAction,
    cv.Schema(
        {
            cv.GenerateID(): cv.use_id(RuntimeFsm),
            cv.Required(CONF_SET): cv.Schema({cv.string_strict: cv.boolean}),
        }
    ),
    synchronous=True,
)
async def set_activities_action_to_code(config, action_id, template_arg, args):
    var = await _new_parented_action(config, action_id, template_arg)
    for activity, active in config[CONF_SET].items():
        cg.add(var.add_activity_state(activity, active))
    return var


@automation.register_action(
    "runtime_fsm.request_action",
    RequestActionAction,
    cv.Schema(
        {
            cv.GenerateID(): cv.use_id(RuntimeFsm),
            cv.Required(CONF_ACTION): cv.templatable(cv.string),
        }
    ),
    synchronous=True,
)
async def request_action_action_to_code(config, action_id, template_arg, args):
    var = await _new_parented_action(config, action_id, template_arg)
    action = await cg.templatable(config[CONF_ACTION], args, cg.std_string)
    cg.add(var.set_action(action))
    return var


@automation.register_action(
    "runtime_fsm.dump",
    DumpAction,
    cv.Schema(
        {
            cv.GenerateID(): cv.use_id(RuntimeFsm),
            cv.Optional(CONF_REASON, default="manual"): cv.templatable(cv.string),
        }
    ),
    synchronous=True,
)
async def dump_action_to_code(config, action_id, template_arg, args):
    var = await _new_parented_action(config, action_id, template_arg)
    reason = await cg.templatable(config[CONF_REASON], args, cg.std_string)
    cg.add(var.set_reason(reason))
    return var


@automation.register_condition(
    "runtime_fsm.is_active",
    IsActiveCondition,
    cv.Schema(
        {
            cv.GenerateID(): cv.use_id(RuntimeFsm),
            cv.Required(CONF_ACTIVITY): cv.templatable(cv.string),
        }
    ),
)
async def is_active_condition_to_code(config, condition_id, template_arg, args):
    var = cg.new_Pvariable(condition_id, template_arg)
    await cg.register_parented(var, config[CONF_ID])
    activity = await cg.templatable(config[CONF_ACTIVITY], args, cg.std_string)
    cg.add(var.set_activity(activity))
    return var
