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

    def test_revision_advances_for_owner_and_destination_without_state_change(self) -> None:
        registry = call_registry.CallRegistry()
        session = registry.upsert(
            "call-1", state="connecting", callee="Home Assistant", owner="ha_softphone"
        )
        initial = session.revision

        redirected = registry.transition(
            "call-1",
            state="connecting",
            owner="router",
            callee="Assist",
            expected_revision=initial,
            expected_owner="ha_softphone",
        )

        self.assertIsNotNone(redirected)
        self.assertEqual(redirected.revision, initial + 1)
        self.assertEqual(redirected.owner, "router")
        self.assertEqual(redirected.callee, "Assist")
        fields = registry.event_fields("call-1", "connecting")
        self.assertEqual(fields["revision"], redirected.revision)
        self.assertEqual(fields["owner"], "router")

    def test_stale_revision_or_owner_cannot_mutate_session(self) -> None:
        registry = call_registry.CallRegistry()
        session = registry.upsert("call-1", state="ringing", owner="ha_softphone")

        self.assertIsNone(
            registry.transition(
                "call-1",
                owner="router",
                expected_revision=session.revision + 1,
                expected_owner="ha_softphone",
            )
        )
        self.assertIsNone(
            registry.transition(
                "call-1",
                owner="router",
                expected_revision=session.revision,
                expected_owner="bridge",
            )
        )
        self.assertEqual(session.owner, "ha_softphone")

    def test_queued_ringing_callback_cannot_resurrect_released_ha_owner(self) -> None:
        registry = call_registry.CallRegistry()
        session = registry.upsert("call-1", state="ringing", owner="ha_softphone")
        queued_revision = session.revision
        published: list[str] = []

        def queued_ringing_callback() -> None:
            if registry.is_current(
                "call-1", revision=queued_revision, owner="ha_softphone"
            ):
                published.append("ringing")

        registry.transition(
            "call-1",
            state="connecting",
            owner="router",
            expected_revision=queued_revision,
            expected_owner="ha_softphone",
        )
        queued_ringing_callback()

        self.assertEqual(published, [])
        self.assertEqual(registry.sessions["call-1"].owner, "router")

    def test_failed_route_resumes_ha_owner_exactly_once(self) -> None:
        registry = call_registry.CallRegistry()
        session = registry.upsert("call-1", state="connecting", owner="router")

        resumed = registry.transition(
            "call-1",
            state="ringing",
            owner="ha_softphone",
            expected_revision=session.revision,
            expected_owner="router",
        )
        duplicate = registry.transition(
            "call-1",
            state="ringing",
            owner="ha_softphone",
            expected_revision=session.revision - 1,
            expected_owner="router",
        )

        self.assertIsNotNone(resumed)
        self.assertIsNone(duplicate)
        self.assertEqual(session.owner, "ha_softphone")

    def test_leg_add_replace_remove_and_finish_advance_control_revision(self) -> None:
        registry = call_registry.CallRegistry()
        session = registry.upsert("call-1", state="connecting", owner="router")
        initial = session.revision
        registry.add_leg("call-1", "leg-1", role="callee", state="ringing")
        after_add = session.revision
        registry.add_leg("call-1", "leg-1", role="callee", state="in_call")
        after_replace = session.revision
        registry.remove_leg("call-1", "leg-1")
        after_remove = session.revision
        registry.finish("call-1", reason="remote_hangup")

        self.assertGreater(after_add, initial)
        self.assertGreater(after_replace, after_add)
        self.assertGreater(after_remove, after_replace)
        self.assertGreater(session.revision, after_remove)
        self.assertEqual(session.owner, "terminal")
        self.assertEqual(session.outcome, "remote_hangup")

    def test_terminal_pop_removes_event_context_and_pending_indexes(self) -> None:
        registry = call_registry.CallRegistry()
        registry.upsert("call-1", state="ringing", owner="ha_softphone")
        registry.event_fields("call-1", "ringing")
        registry.pending_invites["call-1"] = object()
        registry.pending_routes["call-1"] = {"future": object()}

        registry.finish_and_pop("call-1", reason="remote_hangup")

        self.assertNotIn("call-1", registry.event_contexts)
        self.assertNotIn("call-1", registry.pending_invites)
        self.assertNotIn("call-1", registry.pending_routes)


class AutomationEventTypeTest(unittest.TestCase):
    def test_maps_routing_and_call_lifecycle_to_native_event_types(self) -> None:
        cases = (
            ({"state": "route_requested", "direction": "incoming"}, "route_requested"),
            ({"state": "connecting", "direction": "incoming"}, "state_changed"),
            ({"state": "connecting", "direction": "incoming", "event_type": "forwarding"}, "forwarding"),
            ({"state": "connecting", "direction": "outgoing"}, "calling"),
            ({"state": "calling", "direction": "outgoing"}, "outgoing_call"),
            ({"state": "remote_ringing"}, "remote_ringing"),
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

    def test_forward_call_id_is_inferred_only_when_unambiguous(self) -> None:
        self.assertEqual(
            automation_routing.resolve_forward_call_id("", {"call-1": {}}, {}),
            "call-1",
        )
        self.assertEqual(
            automation_routing.resolve_forward_call_id(
                "chosen", {"call-1": {}}, {"call-2": object()}
            ),
            "chosen",
        )
        with self.assertRaisesRegex(ValueError, "No forwardable"):
            automation_routing.resolve_forward_call_id("", {}, {})
        with self.assertRaisesRegex(ValueError, "More than one"):
            automation_routing.resolve_forward_call_id(
                "", {"call-1": {}}, {"call-2": object()}
            )


if __name__ == "__main__":
    unittest.main()
