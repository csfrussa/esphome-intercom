#!/usr/bin/env python3
"""Shared audio/video WebSocket lifecycle contracts."""

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
websocket_owner = _load_module("websocket_owner")
media_ws_session = _load_module("media_ws_session")


class _Registry:
    sessions: dict = {}
    softphone_media: dict = {}

    @staticmethod
    def resolve_session_id(call_id: str) -> str:
        return call_id


class _Lease:
    endpoint_id = "phone"
    call_id = "call-1"
    token = object()


class _Bridge:
    def __init__(self) -> None:
        self.releases: list[tuple[str, str, object]] = []

    def release_media(self, call_id: str, endpoint_id: str, token: object) -> bool:
        self.releases.append((call_id, endpoint_id, token))
        return True


class MediaWebSocketSessionTest(unittest.IsolatedAsyncioTestCase):
    async def test_context_claims_and_releases_audio_owner_then_publishes(self) -> None:
        bucket: dict = {}
        owner = websocket_owner.MediaWebSocketOwner(user_id="u", client_id="c")
        published: list[str] = []

        async with media_ws_session.async_media_websocket_session(
            bucket,
            _Registry(),
            "call-1",
            "phone",
            owner,
            channel="audio",
            timeout=0.1,
            shutdown_event=None,
            pin_client_identity=False,
            local_bridge=None,
            publish_state=lambda: published.append("published"),
        ):
            self.assertIs(bucket["audio_ws_owners"]["phone|call-1"], owner)

        self.assertEqual(bucket["audio_ws_owners"], {})
        self.assertTrue(owner.released.is_set())
        self.assertEqual(published, ["published"])

    async def test_local_lease_releases_only_after_both_media_owners_are_gone(self) -> None:
        bucket: dict = {}
        bridge = _Bridge()
        audio_owner = websocket_owner.MediaWebSocketOwner(user_id="u", client_id="c")
        video_owner = websocket_owner.MediaWebSocketOwner(user_id="u", client_id="c")
        bucket["video_ws_owners"] = {"phone|call-1": video_owner}

        async with media_ws_session.async_media_websocket_session(
            bucket,
            _Registry(),
            "call-1",
            "phone",
            audio_owner,
            channel="audio",
            timeout=0.1,
            shutdown_event=None,
            pin_client_identity=False,
            local_bridge=None,
            publish_state=lambda: None,
        ) as session:
            session.own_local_lease(bridge, _Lease())

        self.assertEqual(bridge.releases, [])
        bucket["video_ws_owners"].clear()
        video_session = media_ws_session.MediaWebSocketSession(
            bucket,
            bucket["video_ws_owners"],
            asyncio.Lock(),
            "phone|call-1",
            video_owner,
            lambda: None,
            local_bridge=bridge,
            local_lease=_Lease(),
        )
        await video_session.close()
        self.assertEqual(len(bridge.releases), 1)

    async def test_cancelled_close_waiter_cannot_skip_release_or_publication(self) -> None:
        bucket = {"audio_ws_owners": {}}
        owner_lock = asyncio.Lock()
        await owner_lock.acquire()
        owner = websocket_owner.MediaWebSocketOwner(user_id="u", client_id="c")
        bucket["audio_ws_owners"]["phone|call-1"] = owner
        published: list[str] = []
        session = media_ws_session.MediaWebSocketSession(
            bucket,
            bucket["audio_ws_owners"],
            owner_lock,
            "phone|call-1",
            owner,
            lambda: published.append("published"),
        )
        waiter = asyncio.create_task(session.close())
        await asyncio.sleep(0)
        waiter.cancel()
        await asyncio.sleep(0)
        self.assertFalse(waiter.done())

        owner_lock.release()
        with self.assertRaises(asyncio.CancelledError):
            await waiter
        self.assertEqual(bucket["audio_ws_owners"], {})
        self.assertEqual(published, ["published"])

    async def test_publication_failure_does_not_restore_released_owner(self) -> None:
        bucket: dict = {}
        owner = websocket_owner.MediaWebSocketOwner(user_id="u", client_id="c")

        async with media_ws_session.async_media_websocket_session(
            bucket,
            _Registry(),
            "call-1",
            "phone",
            owner,
            channel="video",
            timeout=0.1,
            shutdown_event=None,
            pin_client_identity=False,
            local_bridge=None,
            publish_state=lambda: (_ for _ in ()).throw(RuntimeError("observer")),
        ):
            pass

        self.assertEqual(bucket["video_ws_owners"], {})
