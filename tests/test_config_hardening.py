"""Behavioral contracts for configuration and service input hardening."""

from __future__ import annotations

import ast
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest
import voluptuous as vol
import yaml


ROOT = Path(__file__).resolve().parents[1]
CONFIG_FLOW = ROOT / "custom_components" / "voip_stack" / "config_flow.py"
CONST = ROOT / "custom_components" / "voip_stack" / "const.py"
SERVICES = ROOT / "custom_components" / "voip_stack" / "services.py"
SERVICES_YAML = ROOT / "custom_components" / "voip_stack" / "services.yaml"
STRINGS = ROOT / "custom_components" / "voip_stack" / "strings.json"
TRANSLATIONS = ROOT / "custom_components" / "voip_stack" / "translations"
CONFIG = ROOT / "custom_components" / "voip_stack" / "config.py"


def _load_disabled_trunk_data():
    """Load the pure config helper without importing Home Assistant."""

    config_tree = ast.parse(CONFIG_FLOW.read_text())
    helper = next(
        node
        for node in config_tree.body
        if isinstance(node, ast.FunctionDef) and node.name == "_disabled_trunk_data"
    )
    const_namespace: dict[str, object] = {"__file__": str(CONST)}
    exec(compile(CONST.read_text(), str(CONST), "exec"), const_namespace)
    namespace = {
        "Mapping": dict,
        "Any": object,
        **const_namespace,
    }
    exec(
        compile(
            ast.fix_missing_locations(ast.Module(body=[helper], type_ignores=[])),
            str(CONFIG_FLOW),
            "exec",
        ),
        namespace,
    )
    return namespace["_disabled_trunk_data"], namespace


def _load_service_schemas() -> dict[str, vol.Schema]:
    """Register services against a tiny HA facade and return their schemas."""

    def boolean(value: object) -> bool:
        if isinstance(value, bool):
            return value
        normalized = str(value).strip().lower()
        if normalized in {"1", "true", "yes", "on", "enable"}:
            return True
        if normalized in {"0", "false", "no", "off", "disable"}:
            return False
        raise vol.Invalid("invalid boolean value")

    source = SERVICES.read_text()
    source = source.replace(
        "from homeassistant.core import HomeAssistant, ServiceCall, SupportsResponse\n",
        "HomeAssistant = object\nServiceCall = object\n"
        "SupportsResponse = SimpleNamespace(OPTIONAL='optional', ONLY='only')\n",
    )
    source = source.replace(
        "from homeassistant.helpers import config_validation as cv\n",
        "",
    )
    source = source.replace(
        "from .authorization import (\n"
        "    async_require_service_admin,\n"
        "    async_require_service_control,\n"
        ")\n",
        "async def async_require_service_admin(_hass, _call):\n"
        "    return None\n\n"
        "async def async_require_service_control(_hass, _call):\n"
        "    return None\n",
    )
    source = source.replace(
        "from .const import DOMAIN\n",
        'DOMAIN = "voip_stack"\n',
    )
    namespace = {
        "__name__": "voip_stack_services_schema_test",
        "SimpleNamespace": SimpleNamespace,
        "cv": SimpleNamespace(
            string=str,
            entity_id=str,
            boolean=boolean,
        ),
    }
    exec(compile(source, str(SERVICES), "exec"), namespace)

    schemas: dict[str, vol.Schema] = {}

    class ServiceRegistry:
        def async_register(
            self,
            _domain,
            service,
            _handler,
            *,
            schema=None,
            **_kwargs,
        ):
            if schema is not None:
                schemas[service] = schema

    hass = SimpleNamespace(services=ServiceRegistry())
    asyncio.run(namespace["async_register_services"](hass, {}))
    return schemas


@pytest.mark.parametrize("legacy_value", [None, ""])
def test_disabled_trunk_uses_safe_defaults_for_empty_legacy_values(
    legacy_value: object,
) -> None:
    helper, constants = _load_disabled_trunk_data()
    existing = {
        constants["CONF_TRUNK_TRANSPORT"]: legacy_value,
        constants["CONF_TRUNK_PORT"]: legacy_value,
        constants["CONF_TRUNK_EXPIRES"]: legacy_value,
        constants["CONF_TRUNK_DTMF_TIMEOUT_MS"]: legacy_value,
        "sip_accounts": legacy_value,
        constants["CONF_PHONEBOOK_CONTACTS"]: legacy_value,
    }

    result = helper({}, existing)

    assert result[constants["CONF_TRUNK_TRANSPORT"]] == "udp"
    assert result[constants["CONF_TRUNK_PORT"]] == constants["VOIP_STACK_SIP_PORT"]
    assert result[constants["CONF_TRUNK_EXPIRES"]] == 300
    assert result[constants["CONF_TRUNK_DTMF_TIMEOUT_MS"]] == 3000
    assert result["sip_accounts"] == []
    assert result[constants["CONF_PHONEBOOK_CONTACTS"]] == []


def test_trunk_password_config_field_is_masked() -> None:
    source = CONFIG_FLOW.read_text()
    trunk_step = source[source.index("async def async_step_trunk") :]

    assert "TextSelectorConfig" in source
    assert "CONF_TRUNK_PASSWORD, default=defaults[CONF_TRUNK_PASSWORD]" in trunk_step
    assert (
        "TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))" in trunk_step
    )
    assert (
        source.count("TextSelector(TextSelectorConfig(type=TextSelectorType.PASSWORD))")
        == 1
    )


def test_create_account_password_service_field_is_masked() -> None:
    document = yaml.safe_load(SERVICES_YAML.read_text())

    assert document["create_account"]["fields"]["password"]["selector"] == {
        "text": {"type": "password"}
    }


@pytest.mark.parametrize(
    "path",
    [STRINGS, TRANSLATIONS / "en.json", TRANSLATIONS / "it.json"],
)
def test_debug_option_discloses_private_audio_capture_and_retention(path: Path) -> None:
    document = json.loads(path.read_text())
    user_step = document["config"]["step"]["user"]
    title = user_step["data"]["debug_mode"].lower()
    description = user_step["data_description"]["debug_mode"].lower()

    assert "audio" in title
    assert "privat" in title
    assert "15" in description
    assert "8" in description
    assert "~/.cache/voip_stack_debug" in description
    assert "0700" in description
    assert "24" in description
    assert "64 mib" in description


@pytest.mark.parametrize("status", [300, 486, 603, 699])
def test_decline_schema_accepts_only_negative_final_sip_statuses(status: int) -> None:
    schema = _load_service_schemas()["decline"]

    assert schema({"status": status})["status"] == status


@pytest.mark.parametrize("status", [-1, 0, 99, 100, 200, 299, 700, 999])
def test_decline_schema_rejects_non_failure_sip_statuses(status: int) -> None:
    schema = _load_service_schemas()["decline"]

    with pytest.raises(vol.Invalid):
        schema({"status": status})


@pytest.mark.parametrize("status", [0, 300, 486, 603, 699])
def test_route_schema_accepts_default_or_negative_final_sip_status(status: int) -> None:
    schema = _load_service_schemas()["route"]

    assert schema({"call_id": "call-1", "status": status})["status"] == status


@pytest.mark.parametrize("status", [-1, 1, 100, 200, 299, 700, 999])
def test_route_schema_rejects_invalid_override_status(status: int) -> None:
    schema = _load_service_schemas()["route"]

    with pytest.raises(vol.Invalid):
        schema({"call_id": "call-1", "status": status})


@pytest.mark.parametrize(
    ("service", "payload"),
    [
        ("call", {"destination": "x" * 2049}),
        ("set_contacts", {"roster_json": "[" + " " * (256 * 1024) + "]"}),
        ("add_contact", {"name": "desk", "port": 0}),
        ("add_contact", {"name": "desk", "rtp_port": 70000}),
        ("add_contact", {"name": "desk", "tx_formats": ["L16"] * 33}),
        ("create_account", {"username": "u" * 65}),
        ("create_account", {"username": "desk", "password": "p" * 257}),
    ],
)
def test_service_schemas_reject_oversized_or_invalid_resource_inputs(
    service: str,
    payload: dict[str, object],
) -> None:
    schema = _load_service_schemas()[service]

    with pytest.raises(vol.Invalid):
        schema(payload)


def test_phonebook_schema_accepts_bounded_standard_media_fields() -> None:
    schema = _load_service_schemas()["add_contact"]

    validated = schema(
        {
            "name": "Desk phone",
            "sip_uri": "sip:desk@192.0.2.10:5060;transport=tcp",
            "port": 5060,
            "rtp_port": 40000,
            "tx_rate": 48000,
            "rx_rate": "auto",
            "tx_formats": ["OPUS/48000/2/20", "PCMA/8000/1/20"],
            "max_payload_bytes": 1400,
        }
    )

    assert validated["port"] == 5060
    assert validated["tx_formats"] == ["OPUS/48000/2/20", "PCMA/8000/1/20"]


def test_advanced_assist_context_is_opt_in_and_persisted_by_config_flow() -> None:
    config_source = CONFIG.read_text()
    flow_source = CONFIG_FLOW.read_text()

    assert (
        'CONF_ASSIST_ADVANCED_CALL_CONTEXT = "assist_advanced_call_context"'
        in CONST.read_text()
    )
    assert "data.get(CONF_ASSIST_ADVANCED_CALL_CONTEXT, False)" in config_source
    assert "CONF_ASSIST_ADVANCED_CALL_CONTEXT" in flow_source
    assert "BooleanSelector()" in flow_source
    assert "user_input.get(CONF_ASSIST_ADVANCED_CALL_CONTEXT, False)" in flow_source
    assert "data = dict(self._base_input)" in flow_source


@pytest.mark.parametrize(
    "path",
    [STRINGS, TRANSLATIONS / "en.json", TRANSLATIONS / "it.json"],
)
def test_assist_config_explains_agent_instructions_and_untrusted_context(
    path: Path,
) -> None:
    assist = json.loads(path.read_text())["config"]["step"]["assist"]
    description = assist["description"]
    advanced = assist["data_description"]["assist_advanced_call_context"].lower()

    assert 'Incoming SIP call from "Daniele".' in description
    assert "Instructions" in description
    assert "do not repeat their name" in description
    assert "s**t" in description
    assert "f**k" in description
    assert "untrusted" in advanced or "non attendibili" in advanced
    assert (
        "not authentication" in advanced or "non costituisce autenticazione" in advanced
    )
