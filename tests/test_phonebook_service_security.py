#!/usr/bin/env python3
"""Regression tests for private administrator-only phonebook exports."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys
import types


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "custom_components" / "voip_stack" / "phonebook_services.py"
SERVICES = ROOT / "custom_components" / "voip_stack" / "services.py"
PACKAGE = "voip_stack_phonebook_security_test"


def _load_phonebook_services(monkeypatch):
    package = types.ModuleType(PACKAGE)
    package.__path__ = [str(MODULE.parent)]
    monkeypatch.setitem(sys.modules, PACKAGE, package)

    homeassistant = types.ModuleType("homeassistant")
    homeassistant.__path__ = []
    core = types.ModuleType("homeassistant.core")
    core.ServiceCall = object
    monkeypatch.setitem(sys.modules, "homeassistant", homeassistant)
    monkeypatch.setitem(sys.modules, "homeassistant.core", core)

    const = types.ModuleType(f"{PACKAGE}.const")
    const.DOMAIN = "voip_stack"
    runtime = types.ModuleType(f"{PACKAGE}.phonebook_runtime")
    runtime.push_roster_json_to_esps = lambda *_args, **_kwargs: None
    roster = types.ModuleType(f"{PACKAGE}.roster")
    roster.RosterEntry = object
    roster.parse_roster_json = lambda _value: []
    store = types.ModuleType(f"{PACKAGE}.store")
    store.manual_roster_entries = lambda _hass: []
    store.store_manual_roster_entries = lambda _hass, _entries: None
    for dependency in (const, runtime, roster, store):
        monkeypatch.setitem(sys.modules, dependency.__name__, dependency)

    name = f"{PACKAGE}.phonebook_services"
    spec = importlib.util.spec_from_file_location(name, MODULE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, name, module)
    spec.loader.exec_module(module)
    return module


def test_export_returns_roster_only_in_service_response(monkeypatch) -> None:
    module = _load_phonebook_services(monkeypatch)
    roster_json = '[{"name":"Private","sip_uri":"sip:427@10.0.0.7"}]'

    class Sensor:
        extra_state_attributes = {"roster_json": roster_json}

        async def async_update(self) -> None:
            return None

    async def refresh(_hass) -> None:
        return None

    hass = types.SimpleNamespace(
        data={"voip_stack": {"phonebook_sensor": Sensor()}},
    )
    call = types.SimpleNamespace(hass=hass, data={})
    handlers = module.build_phonebook_service_handlers(refresh)

    result = asyncio.run(handlers["export_phonebook"](call))

    assert result == {"roster_json": roster_json}
    assert "_fire_call_event" not in MODULE.read_text()


def test_export_service_requires_a_private_response() -> None:
    source = SERVICES.read_text()
    registration = source[source.index('"export_phonebook"') :]

    assert "supports_response=SupportsResponse.ONLY" in registration
