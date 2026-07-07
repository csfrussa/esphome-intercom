#!/usr/bin/env python3
"""RTP media port allocator tests."""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch


ROOT = Path(__file__).resolve().parents[1]
PKG_NAME = "custom_components.voip_stack"
PKG_DIR = ROOT / "custom_components" / "voip_stack"


def _install_ha_fakes() -> None:
    ha = sys.modules.get("homeassistant")
    if ha is not None and not hasattr(ha, "__path__"):
        ha.__path__ = []
    if "homeassistant.core" not in sys.modules:
        ha = types.ModuleType("homeassistant")
        ha.__path__ = []
        core = types.ModuleType("homeassistant.core")
        config_entries = types.ModuleType("homeassistant.config_entries")
        core.HomeAssistant = type("HomeAssistant", (), {})
        config_entries.ConfigEntry = type("ConfigEntry", (), {})
        sys.modules["homeassistant"] = ha
        sys.modules["homeassistant.core"] = core
        sys.modules["homeassistant.config_entries"] = config_entries
    elif "homeassistant.config_entries" not in sys.modules:
        config_entries = types.ModuleType("homeassistant.config_entries")
        config_entries.ConfigEntry = type("ConfigEntry", (), {})
        sys.modules["homeassistant.config_entries"] = config_entries


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


media_ports = _load_module("media_ports")


class FakeHass:
    def __init__(self) -> None:
        self.data = {
            "voip_stack": {
                "transport_config": {
                    "sip_port": 5060,
                    "rtp_port": 40000,
                    "advertise_host": "",
                }
            }
        }


class MediaPortPoolTest(unittest.TestCase):
    def test_single_allocator_uses_base_when_available(self) -> None:
        hass = FakeHass()
        with patch.object(media_ports, "rtp_port_available", return_value=True):
            self.assertEqual(media_ports.allocate_sip_rtp_port(hass), 40000)
            self.assertEqual(hass.data["voip_stack"]["sip_rtp_next_port"], 40002)

    def test_single_allocator_raises_when_no_port_is_available(self) -> None:
        hass = FakeHass()
        with patch.object(media_ports, "rtp_port_available", return_value=False):
            with self.assertRaises(RuntimeError):
                media_ports.allocate_sip_rtp_port(hass)

    def test_pair_allocator_wraps_and_reuses_released_ports(self) -> None:
        hass = FakeHass()
        with patch.object(media_ports, "RTP_RELAY_POOL_WIDTH", 4), patch.object(media_ports, "rtp_port_available", return_value=True):
            self.assertEqual(media_ports.allocate_sip_rtp_port_pair(hass), (40002, 40004))
            with self.assertRaises(RuntimeError):
                media_ports.allocate_sip_rtp_port_pair(hass)
            media_ports.release_sip_rtp_port_pair(hass, (40002, 40004))
            self.assertEqual(media_ports.allocate_sip_rtp_port_pair(hass), (40002, 40004))

    def test_pair_allocator_skips_in_use_and_unavailable_ports(self) -> None:
        hass = FakeHass()

        def available(port: int) -> bool:
            return int(port) not in {40002, 40004}

        with patch.object(media_ports, "rtp_port_available", side_effect=available):
            self.assertEqual(media_ports.allocate_sip_rtp_port_pair(hass), (40006, 40008))

    def test_pair_release_is_idempotent(self) -> None:
        hass = FakeHass()
        with patch.object(media_ports, "rtp_port_available", return_value=True):
            ports = media_ports.allocate_sip_rtp_port_pair(hass)
            media_ports.release_sip_rtp_port_pair(hass, ports)
            media_ports.release_sip_rtp_port_pair(hass, ports)
            self.assertEqual(hass.data["voip_stack"]["sip_rtp_port_pool"]["used"], set())

    def test_reservation_release_and_detach_ownership(self) -> None:
        hass = FakeHass()
        with patch.object(media_ports, "rtp_port_available", return_value=True):
            reservation = media_ports.RtpPortReservation.allocate(hass)
            self.assertEqual(hass.data["voip_stack"]["sip_rtp_port_pool"]["used"], set(reservation.ports))
            reservation.release()
            reservation.release()
            self.assertEqual(hass.data["voip_stack"]["sip_rtp_port_pool"]["used"], set())

            detached = media_ports.RtpPortReservation.allocate(hass)
            ports = detached.detach()
            detached.release()
            self.assertEqual(hass.data["voip_stack"]["sip_rtp_port_pool"]["used"], set(ports))
            media_ports.release_sip_rtp_port_pair(hass, ports)

    def test_release_media_reservation_releases_runtime_metadata(self) -> None:
        hass = FakeHass()
        with patch.object(media_ports, "rtp_port_available", return_value=True):
            reservation = media_ports.RtpPortReservation.allocate(hass)
            item = {"rtp_reservation": reservation}
            self.assertEqual(hass.data["voip_stack"]["sip_rtp_port_pool"]["used"], set(reservation.ports))
            media_ports.release_media_reservation(item)
            media_ports.release_media_reservation(item)
            self.assertEqual(hass.data["voip_stack"]["sip_rtp_port_pool"]["used"], set())


if __name__ == "__main__":
    unittest.main()
