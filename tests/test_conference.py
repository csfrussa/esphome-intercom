#!/usr/bin/env python3
"""Conference mixer contract tests."""

from __future__ import annotations

from array import array
import importlib.util
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PKG_NAME = "custom_components.voip_stack"
PKG_DIR = ROOT / "custom_components" / "voip_stack"


def _install_ha_fakes() -> None:
    ha = sys.modules.get("homeassistant")
    if ha is None:
        ha = types.ModuleType("homeassistant")
        sys.modules["homeassistant"] = ha
    if not hasattr(ha, "__path__"):
        ha.__path__ = []

    components = sys.modules.setdefault("homeassistant.components", types.ModuleType("homeassistant.components"))
    if not hasattr(components, "__path__"):
        components.__path__ = []
    helpers = sys.modules.setdefault("homeassistant.helpers", types.ModuleType("homeassistant.helpers"))
    if not hasattr(helpers, "__path__"):
        helpers.__path__ = []
    core = sys.modules.setdefault("homeassistant.core", types.ModuleType("homeassistant.core"))
    config_entries = sys.modules.setdefault("homeassistant.config_entries", types.ModuleType("homeassistant.config_entries"))
    device_registry = sys.modules.setdefault("homeassistant.helpers.device_registry", types.ModuleType("homeassistant.helpers.device_registry"))
    entity_registry = sys.modules.setdefault("homeassistant.helpers.entity_registry", types.ModuleType("homeassistant.helpers.entity_registry"))
    websocket_api = sys.modules.setdefault("homeassistant.components.websocket_api", types.ModuleType("homeassistant.components.websocket_api"))

    core.HomeAssistant = getattr(core, "HomeAssistant", type("HomeAssistant", (), {}))
    core.ServiceCall = getattr(core, "ServiceCall", type("ServiceCall", (), {}))
    core.callback = getattr(core, "callback", lambda fn: fn)
    config_entries.ConfigEntry = getattr(config_entries, "ConfigEntry", type("ConfigEntry", (), {}))
    device_registry.async_get = getattr(device_registry, "async_get", lambda _hass: None)
    entity_registry.async_get = getattr(entity_registry, "async_get", lambda _hass: None)
    websocket_api.async_register_command = getattr(websocket_api, "async_register_command", lambda *args, **kwargs: None)
    websocket_api.websocket_command = getattr(websocket_api, "websocket_command", lambda _schema: (lambda fn: fn))
    websocket_api.async_response = getattr(websocket_api, "async_response", lambda fn: fn)


def _load_module(name: str):
    _install_ha_fakes()
    if "custom_components" not in sys.modules:
        root_pkg = types.ModuleType("custom_components")
        root_pkg.__path__ = [str(ROOT / "custom_components")]
        sys.modules["custom_components"] = root_pkg
    if PKG_NAME not in sys.modules:
        pkg = types.ModuleType(PKG_NAME)
        pkg.__path__ = [str(PKG_DIR)]
        sys.modules[PKG_NAME] = pkg
    full_name = f"{PKG_NAME}.{name}"
    if full_name in sys.modules:
        return sys.modules[full_name]
    spec = importlib.util.spec_from_file_location(full_name, PKG_DIR / f"{name}.py")
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {full_name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module


conference = _load_module("conference")


def _frame(value: int) -> bytes:
    return array("h", [value] * (conference.CONFERENCE_FRAME_BYTES // 2)).tobytes()


def _first_sample(frame: bytes) -> int:
    pcm = array("h")
    pcm.frombytes(frame[:2])
    return pcm[0]


class ConferenceMixerTest(unittest.TestCase):
    def test_mix_frames_is_n_minus_one(self) -> None:
        out = conference.mix_frames([_frame(1000), _frame(2000), _frame(-500)])
        self.assertEqual([_first_sample(frame) for frame in out], [1500, 500, 3000])

    def test_mix_frames_clips(self) -> None:
        out = conference.mix_frames([_frame(30000), _frame(30000), _frame(30000)])
        self.assertEqual([_first_sample(frame) for frame in out], [32767, 32767, 32767])

    def test_bad_length_is_silence(self) -> None:
        out = conference.mix_frames([_frame(1000), b""])
        self.assertEqual([_first_sample(frame) for frame in out], [0, 1000])


if __name__ == "__main__":
    unittest.main()
