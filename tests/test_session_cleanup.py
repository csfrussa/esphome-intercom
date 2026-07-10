#!/usr/bin/env python3
"""SIP runtime cleanup primitive tests."""

from __future__ import annotations

import asyncio
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
    if full_name in sys.modules:
        return sys.modules[full_name]
    spec = importlib.util.spec_from_file_location(full_name, PKG_DIR / f"{name}.py")
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot load {full_name}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module


session_cleanup = _load_module("session_cleanup")


class FakeClient:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def terminate(self) -> None:
        self.events.append("client.terminate")

    async def close(self) -> None:
        self.events.append("client.close")


class FakeRelay:
    def __init__(self, events: list[str]) -> None:
        self.events = events

    async def stop(self) -> None:
        self.events.append("relay.stop")


class FailingRelay(FakeRelay):
    async def stop(self) -> None:
        await super().stop()
        raise OSError("relay stop failed")


class FailingClient(FakeClient):
    async def terminate(self) -> None:
        await super().terminate()
        raise OSError("terminate failed")

    async def close(self) -> None:
        await super().close()
        raise OSError("close failed")


async def _sleep_forever(events: list[str]) -> None:
    try:
        await asyncio.Event().wait()
    except asyncio.CancelledError:
        events.append("watcher.cancelled")
        raise


class SipRuntimeCleanupTest(unittest.IsolatedAsyncioTestCase):
    async def test_cleanup_closes_watcher_client_and_relay(self) -> None:
        events: list[str] = []
        watcher = asyncio.create_task(_sleep_forever(events))
        await asyncio.sleep(0)
        result = await session_cleanup.async_cleanup_sip_runtime(
            relay=FakeRelay(events),
            client=FakeClient(events),
            watcher=watcher,
            terminate_client=True,
        )
        self.assertEqual(events, ["watcher.cancelled", "client.terminate", "client.close", "relay.stop"])
        self.assertTrue(result.watcher_cancelled)
        self.assertTrue(result.client_closed)
        self.assertTrue(result.relay_stopped)

    async def test_cleanup_can_stop_relay_first_and_close_without_terminate(self) -> None:
        events: list[str] = []
        result = await session_cleanup.async_cleanup_sip_runtime(
            relay=FakeRelay(events),
            client=FakeClient(events),
            terminate_client=False,
            relay_first=True,
        )
        self.assertEqual(events, ["relay.stop", "client.close"])
        self.assertFalse(result.watcher_cancelled)
        self.assertTrue(result.client_closed)
        self.assertTrue(result.relay_stopped)

    async def test_cleanup_attempts_every_resource_after_independent_failures(self) -> None:
        events: list[str] = []
        watcher = asyncio.create_task(_sleep_forever(events))
        await asyncio.sleep(0)

        result = await session_cleanup.async_cleanup_sip_runtime(
            relay=FailingRelay(events),
            client=FailingClient(events),
            watcher=watcher,
            terminate_client=True,
            relay_first=True,
        )

        self.assertEqual(
            events,
            ["relay.stop", "watcher.cancelled", "client.terminate", "client.close"],
        )
        self.assertTrue(result.watcher_cancelled)
        self.assertFalse(result.client_closed)
        self.assertFalse(result.relay_stopped)


if __name__ == "__main__":
    unittest.main()
