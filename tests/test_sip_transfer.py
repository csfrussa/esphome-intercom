#!/usr/bin/env python3
"""RFC 3515 REFER/NOTIFY semantic tests."""

from __future__ import annotations

from dataclasses import replace
import importlib.util
from pathlib import Path
import sys
import types
import unittest


ROOT = Path(__file__).resolve().parents[1]
PKG_NAME = "custom_components.voip_stack"
PKG_DIR = ROOT / "custom_components" / "voip_stack"


def _load_module(name: str):
    if "custom_components" not in sys.modules:
        root = types.ModuleType("custom_components")
        root.__path__ = [str(ROOT / "custom_components")]
        sys.modules["custom_components"] = root
    if PKG_NAME not in sys.modules:
        package = types.ModuleType(PKG_NAME)
        package.__path__ = [str(PKG_DIR)]
        sys.modules[PKG_NAME] = package
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


sip = _load_module("sip")
sip_transfer = _load_module("sip_transfer")


def _notify(*, status: int, state: str = "terminated;reason=noresource"):
    return sip.SipMessage(
        method="NOTIFY",
        uri="sip:transferor@example.test",
        headers=(
            ("Event", "refer"),
            ("Subscription-State", state),
            ("Content-Type", "message/sipfrag;version=2.0"),
        ),
        body=f"SIP/2.0 {status} Result\r\n".encode(),
    )


class SipTransferTest(unittest.TestCase):
    def test_blind_and_attended_refer_to_headers(self) -> None:
        self.assertEqual(
            sip_transfer.build_refer_to("sip:desk@example.test"),
            "<sip:desk@example.test>",
        )
        attended = sip_transfer.build_refer_to(
            "sip:desk@example.test",
            replaces="call-2;to-tag=remote;from-tag=local",
        )
        self.assertEqual(
            attended,
            "<sip:desk@example.test?Replaces=call-2%3Bto-tag%3Dremote%3Bfrom-tag%3Dlocal>",
        )

    def test_refer_headers_validate_referred_by_uri(self) -> None:
        headers = dict(
            sip_transfer.refer_headers(
                "sip:desk@example.test",
                referred_by="sip:ha@example.test",
            )
        )
        self.assertEqual(headers["Event"], "refer")
        self.assertEqual(headers["Referred-By"], "<sip:ha@example.test>")

    def test_notify_progress_and_final_status(self) -> None:
        progress = sip_transfer.parse_refer_notify(
            _notify(status=100, state="active;expires=30")
        )
        final = sip_transfer.parse_refer_notify(_notify(status=200))

        self.assertFalse(progress.final)
        self.assertEqual(progress.phase, sip_transfer.SubscriptionPhase.ACTIVE)
        self.assertTrue(final.final)
        self.assertEqual(final.status, 200)
        self.assertEqual(final.reason, "noresource")

    def test_notify_failure_is_still_a_final_subscription_result(self) -> None:
        result = sip_transfer.parse_refer_notify(
            _notify(status=503, state="terminated;reason=failed;retry-after=12")
        )

        self.assertTrue(result.final)
        self.assertEqual(result.status, 503)
        self.assertEqual(result.retry_after, 12)

    def test_wrong_event_content_type_and_sipfrag_are_rejected(self) -> None:
        wrong_event = _notify(status=200)
        wrong_event = replace(
            wrong_event,
            headers=tuple(
                (name, "presence" if name == "Event" else value)
                for name, value in wrong_event.headers
            ),
        )
        with self.assertRaises(ValueError):
            sip_transfer.parse_refer_notify(wrong_event)

        malformed = _notify(status=200)
        malformed = replace(malformed, body=b"not a sip status")
        with self.assertRaises(ValueError):
            sip_transfer.parse_refer_notify(malformed)
