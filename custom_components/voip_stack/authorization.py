"""Home Assistant authorization boundaries for VoIP control surfaces."""

from __future__ import annotations

import asyncio
import re
from typing import Any

from homeassistant.exceptions import Unauthorized, UnknownUser


POLICY_READ = "read"
POLICY_CONTROL = "control"
VOIP_CONTROL_ENTITY_ID = "event.voip_stack_call"
_MEDIA_CLIENT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._~-]{15,127}\Z", re.ASCII)
_DOMAIN = "voip_stack"


def _require_entity_permission(user: Any, policy: str, *, context: Any = None) -> str:
    """Require one HA entity policy and return the authenticated user id."""

    user_id = str(getattr(user, "id", "") or "")
    permissions = getattr(user, "permissions", None)
    check_entity = getattr(permissions, "check_entity", None)
    if not user_id or not callable(check_entity) or not check_entity(
        VOIP_CONTROL_ENTITY_ID, policy
    ):
        raise Unauthorized(
            context=context,
            entity_id=VOIP_CONTROL_ENTITY_ID,
            permission=policy,
            user_id=user_id or None,
        )
    return user_id


async def _service_user(hass: Any, call: Any) -> Any | None:
    """Resolve a service caller; an empty user is an internal HA automation."""

    context = getattr(call, "context", None)
    user_id = str(getattr(context, "user_id", "") or "")
    if not user_id:
        return None
    user = await hass.auth.async_get_user(user_id)
    if user is None:
        raise UnknownUser(context=context, user_id=user_id)
    return user


async def async_require_service_control(hass: Any, call: Any) -> None:
    """Allow internal automations or users allowed to control VoIP Stack."""

    user = await _service_user(hass, call)
    if user is not None:
        _require_entity_permission(
            user,
            POLICY_CONTROL,
            context=getattr(call, "context", None),
        )


async def async_require_service_admin(hass: Any, call: Any) -> None:
    """Allow internal automations or authenticated HA administrators."""

    user = await _service_user(hass, call)
    if user is not None and not bool(getattr(user, "is_admin", False)):
        raise Unauthorized(
            context=getattr(call, "context", None),
            user_id=str(getattr(user, "id", "") or "") or None,
        )


def require_websocket_read(connection: Any) -> str:
    """Require read access before exposing call/device topology."""

    return _require_entity_permission(getattr(connection, "user", None), POLICY_READ)


def require_websocket_control(connection: Any) -> str:
    """Require control access before starting or mutating a call."""

    return _require_entity_permission(
        getattr(connection, "user", None), POLICY_CONTROL
    )


def require_http_control(request: Any) -> str:
    """Require control access before attaching private browser media."""

    getter = getattr(request, "get", None)
    user = getter("hass_user") if callable(getter) else None
    return _require_entity_permission(user, POLICY_CONTROL)


def require_media_client_id(request: Any) -> str:
    """Return one bounded browser-instance token covered by HA's signed path."""

    query = getattr(request, "query", None)
    getter = getattr(query, "get", None)
    client_id = str(getter("client_id") if callable(getter) else "")
    if _MEDIA_CLIENT_ID.fullmatch(client_id) is None:
        raise ValueError("a valid signed media client_id is required")
    return client_id


async def async_require_media_controller(
    hass: Any,
    registry: Any,
    call_id: str,
    user: Any,
) -> str:
    """Authorize a media attachment against the call's sticky controller.

    Calls started or answered by an authenticated user remain private to that
    user, including administrators.  Calls created by internal automations do
    not have a user identity; an administrator may bind the first browser
    atomically, after which the same rule applies for reconnects.
    """

    user_id = str(getattr(user, "id", "") or "")
    if not user_id:
        raise Unauthorized(user_id=None)
    bucket = hass.data.setdefault(_DOMAIN, {})
    lock: asyncio.Lock = bucket.setdefault("media_controller_lock", asyncio.Lock())
    async with lock:
        session_id = registry.resolve_session_id(str(call_id or "").strip())
        session = registry.sessions.get(session_id)
        if session is None:
            raise Unauthorized(user_id=user_id)
        controller_user_id = str(
            session.metadata.get("controller_user_id") or ""
        ).strip()
        if controller_user_id:
            if controller_user_id != user_id:
                raise Unauthorized(user_id=user_id)
            return user_id
        if not bool(getattr(user, "is_admin", False)):
            raise Unauthorized(user_id=user_id)
        try:
            registry.bind_controller(session_id, user_id=user_id)
        except ValueError as err:
            raise Unauthorized(user_id=user_id) from err
        return user_id
