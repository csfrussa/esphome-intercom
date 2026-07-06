#!/usr/bin/env python3
"""Device resolver checks for SIP endpoint discovery."""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PKG_NAME = "custom_components.voip_stack"
PKG_DIR = ROOT / "custom_components" / "voip_stack"


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
    if full_name in sys.modules:
        return sys.modules[full_name]
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
        parsed = device_resolver.parse_voip_endpoint(endpoint)
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

    def test_parses_optional_extension_from_endpoint_sensor(self) -> None:
        endpoint = (
            "Spotpear | 192.168.1.31 | 5060 | 40000 | "
            "full_duplex | 16000:s16le:1:10 | 48000:s16le:1:10 | sip_udp | 101"
        )
        parsed = device_resolver.parse_voip_endpoint(endpoint)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed["extension"], "101")
        self.assertEqual(parsed["extras"], [])

    def test_parses_forward_compatible_endpoint_extras(self) -> None:
        base = (
            "Spotpear | 192.168.1.31 | 5060 | 40000 | "
            "full_duplex | 16000:s16le:1:10 | 48000:s16le:1:10 | sip_udp"
        )
        for suffix, extension, extras in (
            ("", "", []),
            (" | 101", "101", []),
            (" | 101 | Conference", "101", ["Conference"]),
            (" | 101 | Conference | Casa", "101", ["Conference", "Casa"]),
            (" | 101 | Conference | Casa | future", "101", ["Conference", "Casa", "future"]),
        ):
            parsed = device_resolver.parse_voip_endpoint(base + suffix)
            self.assertIsNotNone(parsed)
            assert parsed is not None
            self.assertEqual(parsed["extension"], extension)
            self.assertEqual(parsed["extras"], extras)
            self.assertEqual(parsed["conference_group"], extras[0] if len(extras) >= 1 else "")
            self.assertEqual(parsed["ring_group"], extras[1] if len(extras) >= 2 else "")
            self.assertFalse(parsed["conference_ring"])

    def test_parses_conference_ring_from_forward_compatible_endpoint(self) -> None:
        endpoint = (
            "Spotpear | 192.168.1.31 | 5060 | 40000 | "
            "full_duplex | 16000:s16le:1:10 | 48000:s16le:1:10 | sip_udp | 101 | CG Casa | RG Casa | 1"
        )
        parsed = device_resolver.parse_voip_endpoint(endpoint)
        self.assertIsNotNone(parsed)
        assert parsed is not None
        self.assertEqual(parsed["conference_group"], "CG Casa")
        self.assertEqual(parsed["ring_group"], "RG Casa")
        self.assertTrue(parsed["conference_ring"])
        self.assertEqual(parsed["extras"], ["CG Casa", "RG Casa", "1"])

    def test_list_devices_preserves_group_membership_fields(self) -> None:
        source = (PKG_DIR / "device_resolver.py").read_text(encoding="utf-8")
        list_devices = source[source.index("async def list_devices") : source.index("async def resolve_target")]
        self.assertIn('"conference_group": endpoint.get("conference_group") or ""', list_devices)
        self.assertIn('"conference_ring": bool(endpoint.get("conference_ring", False))', list_devices)
        self.assertIn('"ring_group": endpoint.get("ring_group") or ""', list_devices)

    def test_rejects_obsolete_minimal_endpoint_sensor(self) -> None:
        parsed = device_resolver.parse_voip_endpoint("Kitchen|192.168.1.4|5060|40000|sip_udp")
        self.assertIsNone(parsed)

    def test_rejects_malformed_sip_endpoint(self) -> None:
        self.assertIsNone(device_resolver.parse_voip_endpoint("Kitchen|sip|192.168.1.4|5060"))
        self.assertIsNone(device_resolver.parse_voip_endpoint("Kitchen|192.168.1.4|x|40000|sip_udp"))
        self.assertIsNone(device_resolver.parse_voip_endpoint("Kitchen|192.168.1.4|5060|40000|udp"))


if __name__ == "__main__":
    unittest.main()
