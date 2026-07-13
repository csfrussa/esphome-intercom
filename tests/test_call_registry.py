#!/usr/bin/env python3
"""Authoritative call registry and automation event context tests."""

from __future__ import annotations

import importlib.util
import sys
import types
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PKG_NAME = "custom_components.voip_stack"
PKG_DIR = ROOT / "custom_components" / "voip_stack"


def _load_module(name: str):
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


call_registry = _load_module("call_registry")
automation_routing = _load_module("automation_routing")


class CallRegistryEventContextTest(unittest.TestCase):
    def test_sequence_advances_only_for_canonical_state_changes(self) -> None:
        registry = call_registry.CallRegistry()

        first = registry.event_fields("call-1", "ringing")
        duplicate = registry.event_fields("call-1", "ringing")
        answered = registry.event_fields("call-1", "in_call")

        self.assertEqual(first["sequence"], 1)
        self.assertEqual(first["previous_state"], "")
        self.assertEqual(duplicate, first)
        self.assertEqual(answered["sequence"], 2)
        self.assertEqual(answered["previous_state"], "ringing")

    def test_route_history_is_bounded_and_returned_with_events(self) -> None:
        registry = call_registry.CallRegistry()
        registry.event_fields("call-1", "route_requested")
        for index in range(10):
            registry.record_route(
                "call-1",
                action="forward",
                destination=str(index),
            )

        event = registry.event_fields("call-1", "connecting")
        self.assertEqual(len(event["route_history"]), 8)
        self.assertEqual(event["route_history"][0]["destination"], "2")
        self.assertEqual(event["route_history"][-1]["destination"], "9")

    def test_leg_id_resolves_to_source_event_context(self) -> None:
        registry = call_registry.CallRegistry()
        registry.register_bridge(
            source_call_id="source",
            dest_call_id="destination",
            client=object(),
            state="ringing",
        )
        registry.event_fields("source", "ringing")

        self.assertIs(
            registry.event_context("destination"),
            registry.event_context("source"),
        )


class AutomationEventTypeTest(unittest.TestCase):
    def test_maps_routing_and_call_lifecycle_to_native_event_types(self) -> None:
        cases = (
            ({"state": "route_requested", "direction": "incoming"}, "incoming_call"),
            ({"state": "connecting", "direction": "incoming"}, "incoming_call"),
            ({"state": "connecting", "direction": "outgoing"}, "calling"),
            ({"state": "calling", "direction": "outgoing"}, "outgoing_call"),
            ({"state": "remote_ringing"}, "ringing"),
            ({"state": "in_call"}, "answered"),
            ({"state": "in_call", "direction": "outgoing"}, "connected"),
            ({"state": "idle", "type": "ended"}, "ended"),
        )
        for payload, expected in cases:
            with self.subTest(payload=payload):
                self.assertEqual(
                    automation_routing.automation_event_type(payload),
                    expected,
                )

    def test_deadline_only_matches_the_armed_state_revision(self) -> None:
        self.assertTrue(
            automation_routing.deadline_is_current(
                "ringing", 3, armed_state="ringing", armed_sequence=3
            )
        )
        self.assertFalse(
            automation_routing.deadline_is_current(
                "in_call", 4, armed_state="ringing", armed_sequence=3
            )
        )
        self.assertFalse(
            automation_routing.deadline_is_current(
                "ringing", 5, armed_state="ringing", armed_sequence=3
            )
        )


if __name__ == "__main__":
    unittest.main()
