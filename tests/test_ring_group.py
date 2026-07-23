"""Behavioral tests for ring-group preflight and candidate settlement."""

from __future__ import annotations

from enum import Enum
import importlib.util
from pathlib import Path
import sys
import types
from types import SimpleNamespace
from unittest.mock import Mock

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "custom_components" / "voip_stack" / "ring_group.py"


class _Disposition(Enum):
    BUSY = "busy"
    DND = "dnd"
    UNAVAILABLE = "unavailable"


class _Availability(Enum):
    AVAILABLE = "available"
    OFFLINE = "offline"
    UNAVAILABLE = "unavailable"


class _Kind(Enum):
    BROWSER = "browser"
    ESPHOME = "esphome"


@pytest.fixture
def ring_group(monkeypatch):
    package_name = "voip_stack_ring_group_test"
    package = types.ModuleType(package_name)
    package.__path__ = [str(MODULE.parent)]
    monkeypatch.setitem(sys.modules, package_name, package)

    homeassistant = types.ModuleType("homeassistant")
    homeassistant.__path__ = []
    core = types.ModuleType("homeassistant.core")
    core.HomeAssistant = type("HomeAssistant", (), {})
    monkeypatch.setitem(sys.modules, "homeassistant", homeassistant)
    monkeypatch.setitem(sys.modules, "homeassistant.core", core)

    dependencies = {
        "dial_fork": {"DialDisposition": _Disposition},
        "outbound_attempts": {"BrowserLeg": object},
        "phone_endpoint": {
            "EndpointAvailability": _Availability,
            "EndpointKind": _Kind,
        },
        "websocket_api": {"_set_ha_softphone_call_state": Mock()},
    }
    for name, values in dependencies.items():
        dependency = types.ModuleType(f"{package_name}.{name}")
        for key, value in values.items():
            setattr(dependency, key, value)
        monkeypatch.setitem(sys.modules, dependency.__name__, dependency)

    module_name = f"{package_name}.ring_group"
    spec = importlib.util.spec_from_file_location(module_name, MODULE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, module_name, module)
    spec.loader.exec_module(module)
    return module


@pytest.mark.parametrize(
    ("browser", "availability", "dnd", "active_call", "expected"),
    [
        (True, _Availability.AVAILABLE, False, "", None),
        (True, _Availability.OFFLINE, False, "", None),
        (True, _Availability.UNAVAILABLE, False, "", _Disposition.UNAVAILABLE),
        (False, _Availability.OFFLINE, False, "", _Disposition.UNAVAILABLE),
        (False, _Availability.AVAILABLE, True, "", _Disposition.DND),
        (False, _Availability.AVAILABLE, False, "other", _Disposition.BUSY),
        (False, _Availability.AVAILABLE, False, "call-1", None),
    ],
)
def test_endpoint_preflight_matrix(
    ring_group,
    browser,
    availability,
    dnd,
    active_call,
    expected,
) -> None:
    endpoint = SimpleNamespace(
        availability=availability,
        dnd=dnd,
        active_call_id=active_call,
    )
    assert (
        ring_group.endpoint_preflight_disposition(
            endpoint,
            call_id="call-1",
            browser=browser,
        )
        is expected
    )


def test_settlement_releases_every_loser_even_if_one_observer_fails(
    ring_group,
) -> None:
    registry = SimpleNamespace(release_endpoint_claim=Mock())
    legs = [
        SimpleNamespace(endpoint_id="casa", device_id="device-casa"),
        SimpleNamespace(endpoint_id="test", device_id="device-test"),
        SimpleNamespace(endpoint_id="ws3", device_id="device-ws3"),
    ]

    def publish(_hass, _state, **kwargs):
        if kwargs["endpoint_id"] == "test":
            raise RuntimeError("observer failed")

    ring_group._set_ha_softphone_call_state = Mock(side_effect=publish)
    ring_group.settle_browser_candidates(
        SimpleNamespace(),
        registry,
        legs,
        call_id="call-1",
        caller="Door",
        callee="RG Casa",
        state="cancelled",
        reason="cancelled",
        route_kind="ring",
        keep_endpoint_id="casa",
    )

    assert registry.release_endpoint_claim.call_args_list == [
        (("call-1", "test"),),
        (("call-1", "ws3"),),
    ]
    assert ring_group._set_ha_softphone_call_state.call_count == 2


def test_esphome_transport_adoption_is_explicit(ring_group) -> None:
    assert ring_group.endpoint_is_esphome(
        SimpleNamespace(kind=_Kind.ESPHOME)
    )
    assert not ring_group.endpoint_is_esphome(
        SimpleNamespace(kind=_Kind.BROWSER)
    )
