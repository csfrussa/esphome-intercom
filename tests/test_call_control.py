#!/usr/bin/env python3
"""Session-level hold and REFER transfer contracts."""

from __future__ import annotations

import asyncio
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


_load_module("session_cleanup")
endpoint_session = _load_module("endpoint_session")
call_control = _load_module("call_control")


class _Subscription:
    def __init__(self, status: int, gate: asyncio.Event | None = None) -> None:
        self.status = status
        self.gate = gate
        self.closed = 0

    async def wait_final(self) -> int:
        if self.gate is not None:
            await self.gate.wait()
        return self.status

    async def close(self) -> None:
        self.closed += 1


class _Leg:
    def __init__(
        self,
        leg_id: str,
        *,
        hold_ok: bool = True,
        subscription: _Subscription | None = None,
    ) -> None:
        self.leg_id = leg_id
        self.hold_ok = hold_ok
        self.hold_calls: list[bool] = []
        self.subscription = subscription or _Subscription(200)
        self.refers: list[tuple[str, str]] = []

    async def set_hold(self, held: bool) -> bool:
        self.hold_calls.append(held)
        return self.hold_ok if len(self.hold_calls) == 1 else True

    async def refer(self, target_uri: str, *, replaces: str = ""):
        self.refers.append((target_uri, replaces))
        return self.subscription


class SessionCallControllerTest(unittest.IsolatedAsyncioTestCase):
    def _session(self):
        return endpoint_session.EndpointCallSession(
            "call-1", 1, phase=endpoint_session.SessionPhase.ESTABLISHED
        )

    async def test_hold_and_resume_commit_all_legs(self) -> None:
        session = self._session()
        controller = call_control.SessionCallController(session)
        legs = (_Leg("a"), _Leg("b"))

        held = await controller.set_hold(legs, True)
        resumed = await controller.set_hold(legs, False)

        self.assertEqual(held.disposition, call_control.ControlDisposition.SUCCEEDED)
        self.assertEqual(resumed.disposition, call_control.ControlDisposition.SUCCEEDED)
        self.assertEqual(session.phase, endpoint_session.SessionPhase.ESTABLISHED)
        self.assertEqual(legs[0].hold_calls, [True, False])

    async def test_partial_hold_is_rolled_back_without_false_held_state(self) -> None:
        session = self._session()
        controller = call_control.SessionCallController(session)
        accepted = _Leg("accepted")
        rejected = _Leg("rejected", hold_ok=False)

        result = await controller.set_hold((accepted, rejected), True)

        self.assertEqual(result.disposition, call_control.ControlDisposition.FAILED)
        self.assertEqual(accepted.hold_calls, [True, False])
        self.assertEqual(rejected.hold_calls, [True])
        self.assertEqual(session.phase, endpoint_session.SessionPhase.ESTABLISHED)

    async def test_successful_notify_terminates_original_session(self) -> None:
        session = self._session()
        controller = call_control.SessionCallController(session)
        subscription = _Subscription(200)
        leg = _Leg("remote", subscription=subscription)

        result = await controller.transfer(
            leg,
            "sip:desk@example.test",
            replaces="other-call;to-tag=a;from-tag=b",
        )

        self.assertEqual(result.disposition, call_control.ControlDisposition.SUCCEEDED)
        self.assertEqual(session.phase, endpoint_session.SessionPhase.TERMINATED)
        self.assertEqual(
            leg.refers,
            [("sip:desk@example.test", "other-call;to-tag=a;from-tag=b")],
        )
        self.assertEqual(subscription.closed, 1)

    async def test_failed_notify_keeps_original_call_established(self) -> None:
        session = self._session()
        controller = call_control.SessionCallController(session)
        subscription = _Subscription(503)

        result = await controller.transfer(
            _Leg("remote", subscription=subscription),
            "sip:desk@example.test",
        )

        self.assertEqual(result.disposition, call_control.ControlDisposition.FAILED)
        self.assertEqual(result.notify_status, 503)
        self.assertEqual(session.phase, endpoint_session.SessionPhase.ESTABLISHED)
        self.assertEqual(subscription.closed, 1)

    async def test_transfer_timeout_closes_subscription_and_keeps_call(self) -> None:
        session = self._session()
        controller = call_control.SessionCallController(session)
        subscription = _Subscription(200, asyncio.Event())

        result = await controller.transfer(
            _Leg("remote", subscription=subscription),
            "sip:desk@example.test",
            timeout=0.005,
        )

        self.assertEqual(result.disposition, call_control.ControlDisposition.TIMEOUT)
        self.assertEqual(session.phase, endpoint_session.SessionPhase.ESTABLISHED)
        self.assertEqual(subscription.closed, 1)

    async def test_source_termination_wins_over_late_transfer_notify(self) -> None:
        session = self._session()
        controller = call_control.SessionCallController(session)
        gate = asyncio.Event()
        subscription = _Subscription(200, gate)
        running = asyncio.create_task(
            controller.transfer(
                _Leg("remote", subscription=subscription),
                "sip:desk@example.test",
            )
        )
        while session.phase is not endpoint_session.SessionPhase.TRANSFERRING:
            await asyncio.sleep(0)

        await session.terminate("remote_hangup")
        gate.set()
        result = await running

        self.assertEqual(result.disposition, call_control.ControlDisposition.CANCELLED)
        self.assertEqual(session.terminal_reason, "remote_hangup")
