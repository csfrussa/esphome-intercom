#!/usr/bin/env python3
"""Concurrency tests for browser media WebSocket ownership."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys
import unittest


MODULE_PATH = (
    Path(__file__).resolve().parents[1]
    / "custom_components"
    / "voip_stack"
    / "websocket_owner.py"
)
SPEC = importlib.util.spec_from_file_location("voip_stack_websocket_owner_test", MODULE_PATH)
assert SPEC is not None and SPEC.loader is not None
OWNER_MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = OWNER_MODULE
SPEC.loader.exec_module(OWNER_MODULE)

MediaWebSocketOwner = OWNER_MODULE.MediaWebSocketOwner
WebSocketOwnerBusyError = OWNER_MODULE.WebSocketOwnerBusyError
async_claim_media_owner = OWNER_MODULE.async_claim_media_owner
async_release_media_owner = OWNER_MODULE.async_release_media_owner
async_revoke_media_owners = OWNER_MODULE.async_revoke_media_owners


class _FakeWebSocket:
    def __init__(self, *, fail: bool = False) -> None:
        self.force_close_calls = 0
        self.fail = fail

    def force_close(self) -> None:
        self.force_close_calls += 1
        if self.fail:
            raise RuntimeError("synthetic close failure")


class _FakeTransport:
    def __init__(self, *, fail: bool = False) -> None:
        self.close_calls = 0
        self.fail = fail

    def close(self) -> None:
        self.close_calls += 1
        if self.fail:
            raise RuntimeError("synthetic transport failure")


class WebSocketOwnerTest(unittest.IsolatedAsyncioTestCase):
    async def test_reconnect_revokes_then_waits_for_previous_teardown(self) -> None:
        owners: dict[str, object] = {}
        lock = asyncio.Lock()
        previous_ws = _FakeWebSocket()
        previous_transport = _FakeTransport()
        previous = MediaWebSocketOwner(
            websocket=previous_ws,
            transport=previous_transport,  # type: ignore[arg-type]
            user_id="user-a",
            client_id="tab-a",
        )
        owners["call-1"] = previous
        replacement = MediaWebSocketOwner(user_id="user-a", client_id="tab-a")

        claim = asyncio.create_task(
            async_claim_media_owner(
                owners,
                lock,
                "call-1",
                replacement,
                timeout=1.0,
            )
        )
        await asyncio.sleep(0)

        self.assertEqual(previous_ws.force_close_calls, 1)
        self.assertEqual(previous_transport.close_calls, 1)
        self.assertFalse(claim.done())
        await async_release_media_owner(owners, lock, "call-1", previous)
        await claim
        self.assertIs(owners["call-1"], replacement)

    async def test_different_user_cannot_revoke_or_claim_active_media(self) -> None:
        owners: dict[str, object] = {}
        lock = asyncio.Lock()
        previous_ws = _FakeWebSocket()
        previous_transport = _FakeTransport()
        previous = MediaWebSocketOwner(
            websocket=previous_ws,
            transport=previous_transport,  # type: ignore[arg-type]
            user_id="user-a",
            client_id="tab-a",
        )
        owners["private-call"] = previous

        with self.assertRaises(WebSocketOwnerBusyError):
            await async_claim_media_owner(
                owners,
                lock,
                "private-call",
                MediaWebSocketOwner(user_id="user-b", client_id="tab-b"),
                timeout=1.0,
            )

        self.assertIs(owners["private-call"], previous)
        self.assertEqual(previous_ws.force_close_calls, 0)
        self.assertEqual(previous_transport.close_calls, 0)
        self.assertFalse(previous.handoff_requested.is_set())

    async def test_second_tab_of_same_user_cannot_preempt_active_media(self) -> None:
        owners: dict[str, object] = {}
        lock = asyncio.Lock()
        previous_ws = _FakeWebSocket()
        previous = MediaWebSocketOwner(
            websocket=previous_ws,
            user_id="user-a",
            client_id="tab-a",
        )
        owners["private-call"] = previous

        with self.assertRaises(WebSocketOwnerBusyError):
            await async_claim_media_owner(
                owners,
                lock,
                "private-call",
                MediaWebSocketOwner(user_id="user-a", client_id="tab-b"),
                timeout=1.0,
            )

        self.assertIs(owners["private-call"], previous)
        self.assertEqual(previous_ws.force_close_calls, 0)
        self.assertFalse(previous.handoff_requested.is_set())

    async def test_late_old_teardown_cannot_remove_replacement(self) -> None:
        owners: dict[str, object] = {}
        lock = asyncio.Lock()
        replacement = MediaWebSocketOwner()
        owners["call-2"] = replacement
        stale = MediaWebSocketOwner()

        await async_release_media_owner(owners, lock, "call-2", stale)

        self.assertIs(owners["call-2"], replacement)
        self.assertTrue(stale.released.is_set())

    async def test_repeated_cancellation_cannot_interrupt_owner_release(self) -> None:
        owners: dict[str, object] = {}
        lock = asyncio.Lock()
        owner = MediaWebSocketOwner()
        owners["call-cancel"] = owner

        await lock.acquire()
        release = asyncio.create_task(
            async_release_media_owner(owners, lock, "call-cancel", owner)
        )
        await asyncio.sleep(0)
        release.cancel()
        await asyncio.sleep(0)
        release.cancel()
        await asyncio.sleep(0)
        self.assertFalse(release.done())
        self.assertIs(owners["call-cancel"], owner)
        self.assertFalse(owner.released.is_set())

        lock.release()
        with self.assertRaises(asyncio.CancelledError):
            await release

        self.assertEqual(owners, {})
        self.assertTrue(owner.released.is_set())

    async def test_timeout_preserves_previous_owner(self) -> None:
        owners: dict[str, object] = {}
        lock = asyncio.Lock()
        previous = MediaWebSocketOwner(
            websocket=_FakeWebSocket(),
            transport=_FakeTransport(),  # type: ignore[arg-type]
        )
        owners["call-3"] = previous

        with self.assertRaises(WebSocketOwnerBusyError):
            await async_claim_media_owner(
                owners,
                lock,
                "call-3",
                MediaWebSocketOwner(),
                timeout=0.001,
            )

        self.assertIs(owners["call-3"], previous)

    async def test_teardown_failures_do_not_prevent_handoff(self) -> None:
        owners: dict[str, object] = {}
        lock = asyncio.Lock()
        previous = MediaWebSocketOwner(
            websocket=_FakeWebSocket(fail=True),
            transport=_FakeTransport(fail=True),  # type: ignore[arg-type]
        )
        owners["call-4"] = previous
        replacement = MediaWebSocketOwner()
        claim = asyncio.create_task(
            async_claim_media_owner(
                owners,
                lock,
                "call-4",
                replacement,
                timeout=1.0,
            )
        )
        await asyncio.sleep(0)
        await async_release_media_owner(owners, lock, "call-4", previous)
        await claim

        self.assertIs(owners["call-4"], replacement)

    async def test_shutdown_revokes_all_owners_and_waits_for_release(self) -> None:
        owners: dict[str, object] = {}
        lock = asyncio.Lock()
        first = MediaWebSocketOwner(websocket=_FakeWebSocket())
        second = MediaWebSocketOwner(websocket=_FakeWebSocket())
        owners.update({"call-a": first, "call-b": second})

        shutdown = asyncio.create_task(
            async_revoke_media_owners(owners, lock, timeout=1.0)
        )
        await asyncio.sleep(0)

        self.assertTrue(first.handoff_requested.is_set())
        self.assertTrue(second.handoff_requested.is_set())
        self.assertFalse(shutdown.done())
        await async_release_media_owner(owners, lock, "call-a", first)
        await async_release_media_owner(owners, lock, "call-b", second)
        self.assertEqual(await shutdown, set())
        self.assertEqual(owners, {})

    async def test_shutdown_timeout_keeps_owner_mapping_fail_closed(self) -> None:
        owners: dict[str, object] = {}
        lock = asyncio.Lock()
        owner = MediaWebSocketOwner(websocket=_FakeWebSocket())
        owners["stuck"] = owner

        pending = await async_revoke_media_owners(owners, lock, timeout=0.001)

        self.assertEqual(pending, {"stuck"})
        self.assertIs(owners["stuck"], owner)

    async def test_shutdown_gate_prevents_waiting_reconnect_from_claiming(self) -> None:
        owners: dict[str, object] = {}
        lock = asyncio.Lock()
        shutdown = asyncio.Event()
        previous = MediaWebSocketOwner(websocket=_FakeWebSocket())
        owners["call-race"] = previous
        replacement = MediaWebSocketOwner()
        claim = asyncio.create_task(
            async_claim_media_owner(
                owners,
                lock,
                "call-race",
                replacement,
                timeout=1.0,
                shutdown_event=shutdown,
            )
        )
        await asyncio.sleep(0)
        self.assertTrue(previous.handoff_requested.is_set())

        shutdown.set()
        await async_release_media_owner(owners, lock, "call-race", previous)
        with self.assertRaises(WebSocketOwnerBusyError):
            await claim

        self.assertEqual(owners, {})
        self.assertFalse(replacement.released.is_set())

    async def test_shutdown_gate_rejects_new_owner_before_lookup(self) -> None:
        owners: dict[str, object] = {}
        shutdown = asyncio.Event()
        shutdown.set()

        with self.assertRaises(WebSocketOwnerBusyError):
            await async_claim_media_owner(
                owners,
                asyncio.Lock(),
                "call-new",
                MediaWebSocketOwner(),
                timeout=1.0,
                shutdown_event=shutdown,
            )

        self.assertEqual(owners, {})

    async def test_shutdown_closes_and_removes_legacy_owner(self) -> None:
        owners: dict[str, object] = {}
        lock = asyncio.Lock()
        legacy = _FakeWebSocket()
        owners["legacy"] = legacy

        pending = await async_revoke_media_owners(owners, lock, timeout=1.0)

        self.assertEqual(pending, set())
        self.assertEqual(legacy.force_close_calls, 1)
        self.assertEqual(owners, {})


if __name__ == "__main__":
    unittest.main()
