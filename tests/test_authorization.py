#!/usr/bin/env python3
"""Behavioral authorization boundaries for VoIP control and media access."""

from __future__ import annotations

import asyncio
import importlib.util
from pathlib import Path
import sys
import types

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "custom_components" / "voip_stack" / "authorization.py"


class _Unauthorized(Exception):
    def __init__(self, **kwargs) -> None:
        self.details = kwargs


class _UnknownUser(Exception):
    def __init__(self, **kwargs) -> None:
        self.details = kwargs


@pytest.fixture
def authorization(monkeypatch):
    homeassistant = types.ModuleType("homeassistant")
    homeassistant.__path__ = []
    exceptions = types.ModuleType("homeassistant.exceptions")
    exceptions.Unauthorized = _Unauthorized
    exceptions.UnknownUser = _UnknownUser
    monkeypatch.setitem(sys.modules, "homeassistant", homeassistant)
    monkeypatch.setitem(sys.modules, "homeassistant.exceptions", exceptions)

    spec = importlib.util.spec_from_file_location("voip_stack_authorization_test", MODULE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class _Permissions:
    def __init__(self, allowed: set[str]) -> None:
        self.allowed = allowed
        self.checks: list[tuple[str, str]] = []

    def check_entity(self, entity_id: str, policy: str) -> bool:
        self.checks.append((entity_id, policy))
        return policy in self.allowed


class _User:
    def __init__(self, user_id: str, *, allowed: set[str], admin: bool = False) -> None:
        self.id = user_id
        self.is_admin = admin
        self.permissions = _Permissions(allowed)


def _service_call(user_id: str | None):
    return types.SimpleNamespace(context=types.SimpleNamespace(user_id=user_id))


def test_service_control_uses_entity_policy_and_preserves_internal_automation(
    authorization,
) -> None:
    allowed = _User("allowed", allowed={"control"})
    denied = _User("denied", allowed={"read"})
    users = {user.id: user for user in (allowed, denied)}
    hass = types.SimpleNamespace(
        auth=types.SimpleNamespace(
            async_get_user=lambda user_id: asyncio.sleep(0, result=users.get(user_id))
        )
    )

    asyncio.run(
        authorization.async_require_service_control(hass, _service_call(None))
    )
    asyncio.run(
        authorization.async_require_service_control(hass, _service_call("allowed"))
    )
    assert allowed.permissions.checks == [
        (authorization.VOIP_CONTROL_ENTITY_ID, "control")
    ]
    with pytest.raises(_Unauthorized):
        asyncio.run(
            authorization.async_require_service_control(hass, _service_call("denied"))
        )
    with pytest.raises(_UnknownUser):
        asyncio.run(
            authorization.async_require_service_control(hass, _service_call("missing"))
        )


def test_admin_service_boundary_allows_only_admin_or_internal_context(
    authorization,
) -> None:
    admin = _User("admin", allowed=set(), admin=True)
    ordinary = _User("ordinary", allowed={"control"})
    users = {user.id: user for user in (admin, ordinary)}
    hass = types.SimpleNamespace(
        auth=types.SimpleNamespace(
            async_get_user=lambda user_id: asyncio.sleep(0, result=users.get(user_id))
        )
    )

    asyncio.run(authorization.async_require_service_admin(hass, _service_call(None)))
    asyncio.run(
        authorization.async_require_service_admin(hass, _service_call("admin"))
    )
    with pytest.raises(_Unauthorized):
        asyncio.run(
            authorization.async_require_service_admin(hass, _service_call("ordinary"))
        )


def test_websocket_and_http_media_require_explicit_entity_permission(
    authorization,
) -> None:
    reader = _User("reader", allowed={"read"})
    controller = _User("controller", allowed={"read", "control"})

    assert authorization.require_websocket_read(
        types.SimpleNamespace(user=reader)
    ) == "reader"
    with pytest.raises(_Unauthorized):
        authorization.require_websocket_control(types.SimpleNamespace(user=reader))
    assert authorization.require_websocket_control(
        types.SimpleNamespace(user=controller)
    ) == "controller"

    with pytest.raises(_Unauthorized):
        authorization.require_http_control({"hass_user": reader})
    assert authorization.require_http_control({"hass_user": controller}) == "controller"
    with pytest.raises(_Unauthorized):
        authorization.require_http_control({})


@pytest.mark.parametrize(
    "client_id",
    [
        "tab-0123456789abcdef",
        "01234567-89ab-cdef-0123-456789abcdef",
        "browser.instance_01~reload",
    ],
)
def test_media_client_id_accepts_only_bounded_signed_path_tokens(
    authorization,
    client_id: str,
) -> None:
    request = types.SimpleNamespace(query={"client_id": client_id})
    assert authorization.require_media_client_id(request) == client_id


@pytest.mark.parametrize(
    "client_id",
    ["", "short", "x" * 129, "space is invalid", "slash/is/invalid", "line\nbreak"],
)
def test_media_client_id_rejects_missing_malformed_or_unbounded_tokens(
    authorization,
    client_id: str,
) -> None:
    request = types.SimpleNamespace(query={"client_id": client_id})
    with pytest.raises(ValueError):
        authorization.require_media_client_id(request)


class _Registry:
    def __init__(self, controller_user_id: str = "") -> None:
        self.session = types.SimpleNamespace(
            metadata={"controller_user_id": controller_user_id}
        )
        self.sessions = {"call-1": self.session}
        self.bound: list[str] = []

    def resolve_session_id(self, call_id: str) -> str:
        return call_id

    def bind_controller(self, call_id: str, *, user_id: str = "", **kwargs):
        del call_id, kwargs
        current = str(self.session.metadata.get("controller_user_id") or "")
        if current and current != user_id:
            raise ValueError("already controlled")
        self.session.metadata["controller_user_id"] = user_id
        self.bound.append(user_id)
        return self.session


def test_media_controller_accepts_only_the_user_who_controls_the_call(
    authorization,
) -> None:
    registry = _Registry("user-a")
    user_a = _User("user-a", allowed={"control"})
    admin_b = _User("admin-b", allowed={"control"}, admin=True)
    hass = types.SimpleNamespace(data={})

    assert (
        asyncio.run(
            authorization.async_require_media_controller(
                hass, registry, "call-1", user_a
            )
        )
        == "user-a"
    )
    with pytest.raises(_Unauthorized):
        asyncio.run(
            authorization.async_require_media_controller(
                hass, registry, "call-1", admin_b
            )
        )


def test_only_admin_can_atomically_bind_media_for_an_internal_call(
    authorization,
) -> None:
    registry = _Registry()
    ordinary = _User("ordinary", allowed={"control"})
    admin = _User("admin", allowed={"control"}, admin=True)
    hass = types.SimpleNamespace(data={})

    with pytest.raises(_Unauthorized):
        asyncio.run(
            authorization.async_require_media_controller(
                hass, registry, "call-1", ordinary
            )
        )
    assert registry.bound == []
    assert (
        asyncio.run(
            authorization.async_require_media_controller(
                hass, registry, "call-1", admin
            )
        )
        == "admin"
    )
    assert registry.bound == ["admin"]


def test_media_controller_rejects_unknown_call(authorization) -> None:
    registry = _Registry()
    registry.sessions.clear()
    hass = types.SimpleNamespace(data={})
    admin = _User("admin", allowed={"control"}, admin=True)

    with pytest.raises(_Unauthorized):
        asyncio.run(
            authorization.async_require_media_controller(
                hass, registry, "missing", admin
            )
        )


def test_all_external_surfaces_apply_authorization_and_ws_context() -> None:
    websocket = (ROOT / "custom_components" / "voip_stack" / "websocket_api.py").read_text()
    services = (ROOT / "custom_components" / "voip_stack" / "services.py").read_text()
    audio = (ROOT / "custom_components" / "voip_stack" / "audio_ws_view.py").read_text()
    video = (ROOT / "custom_components" / "voip_stack" / "video_ws_view.py").read_text()

    assert websocket.count("require_websocket_read(connection)") >= 5
    assert "require_websocket_control(connection)" in websocket
    assert "context=connection.context(msg)" in websocket
    assert "async_require_service_control" in services
    assert "async_require_service_admin" in services
    assert "require_http_control(request)" in audio
    assert "require_http_control(request)" in video
    assert "require_media_client_id(request)" in audio
    assert "require_media_client_id(request)" in video
    assert "async_require_media_controller(" in audio
    assert "async_require_media_controller(" in video
    assert "user_id=user_id" in audio
    assert "client_id=client_id" in audio
    assert "user_id=user_id" in video
    assert "client_id=client_id" in video
