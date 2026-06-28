#!/usr/bin/env python3
"""Device resolver checks for SIP endpoint discovery."""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PKG_NAME = "custom_components.intercom_native"
PKG_DIR = ROOT / "custom_components" / "intercom_native"


def _install_ha_fakes() -> None:
    if "homeassistant.core" not in sys.modules:
        ha = types.ModuleType("homeassistant")
        helpers = types.ModuleType("homeassistant.helpers")
        core = types.ModuleType("homeassistant.core")
        device_registry = types.ModuleType("homeassistant.helpers.device_registry")
        entity_registry = types.ModuleType("homeassistant.helpers.entity_registry")
        core.HomeAssistant = type("HomeAssistant", (), {})
        core.ServiceCall = type("ServiceCall", (), {})
        core.callback = lambda fn: fn
        device_registry.async_get = lambda _hass: None
        entity_registry.async_get = lambda _hass: None
        sys.modules["homeassistant"] = ha
        sys.modules["homeassistant.core"] = core
        sys.modules["homeassistant.helpers"] = helpers
        sys.modules["homeassistant.helpers.device_registry"] = device_registry
        sys.modules["homeassistant.helpers.entity_registry"] = entity_registry


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
    spec = importlib.util.spec_from_file_location(full_name, PKG_DIR / f"{name}.py")
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {full_name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module


device_resolver = _load_module("device_resolver")


class SipEndpointParseTest(unittest.TestCase):
    def test_parses_sip_endpoint_sensor_with_audio_formats(self) -> None:
        endpoint = (
            "Waveshare S3 Audio | 192.168.1.47 | 5060 | 40000 | "
            "full_duplex | 16000:s16le:1:16 | 48000:s16le:1:10;16000:s16le:1:16;16000:s16le:1:32 | sip_tcp"
        )
        parsed = device_resolver.parse_intercom_endpoint(endpoint)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed["sip_transport"], "tcp")
        self.assertEqual(parsed["host"], "192.168.1.47")
        self.assertEqual(parsed["sip_port"], 5060)
        self.assertEqual(parsed["rtp_port"], 40000)
        self.assertEqual([fmt.wire_token() for fmt in parsed["tx_formats"]], ["16000:s16le:1:16"])
        self.assertEqual(
            [fmt.wire_token() for fmt in parsed["rx_formats"]],
            ["48000:s16le:1:10", "16000:s16le:1:16", "16000:s16le:1:32"],
        )

    def test_rejects_obsolete_minimal_endpoint_sensor(self) -> None:
        parsed = device_resolver.parse_intercom_endpoint("Kitchen|192.168.1.4|5060|40000|sip_udp")
        self.assertIsNone(parsed)

    def test_rejects_malformed_sip_endpoint(self) -> None:
        self.assertIsNone(device_resolver.parse_intercom_endpoint("Kitchen|sip|192.168.1.4|5060"))
        self.assertIsNone(device_resolver.parse_intercom_endpoint("Kitchen|192.168.1.4|x|40000|sip_udp"))
        self.assertIsNone(device_resolver.parse_intercom_endpoint("Kitchen|192.168.1.4|5060|40000|udp"))


if __name__ == "__main__":
    unittest.main()
