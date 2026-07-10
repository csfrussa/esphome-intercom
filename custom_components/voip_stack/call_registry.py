"""Authoritative HA-side SIP call session registry."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


LegRole = Literal["caller", "callee", "trunk", "ha_softphone", "esp", "softphone", "router", "assist"]
TERMINAL_STATES = {
    "idle",
    "busy",
    "declined",
    "cancelled",
    "media_incompatible",
    "transport_unreachable",
    "auth_required_unsupported",
    "protocol_error",
    "error",
}


@dataclass(slots=True)
class CallLeg:
    leg_id: str
    role: LegRole
    sip_call_id: str = ""
    state: str = "idle"
    local_uri: str = ""
    remote_uri: str = ""
    remote_contact: str = ""
    media: Any | None = None


@dataclass(slots=True)
class CallSession:
    id: str
    state: str = "new"
    caller: str = ""
    callee: str = ""
    route_kind: str = ""
    terminal_reason: str = ""
    legs: dict[str, CallLeg] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


class CallRegistry:
    """Small session index shared by softphone, router and trunk handlers."""

    def __init__(self) -> None:
        self.sessions: dict[str, CallSession] = {}
        self.leg_index: dict[str, str] = {}
        self.pending_routes: dict[str, dict[str, Any]] = {}
        self.pending_invites: dict[str, Any] = {}
        self.preanswered: dict[str, dict[str, Any]] = {}
        self.softphone_media: dict[str, dict[str, Any]] = {}
        self.sip_clients: dict[str, Any] = {}
        self.client_watchers: dict[str, Any] = {}
        self.relays: dict[str, Any] = {}
        self.bridge_clients: dict[str, str] = {}

    def upsert(
        self,
        call_id: str,
        *,
        state: str,
        caller: str = "",
        callee: str = "",
        route_kind: str = "",
        terminal_reason: str = "",
        **metadata: Any,
    ) -> CallSession:
        session = self.sessions.get(call_id)
        if session is None:
            session = CallSession(id=call_id)
            self.sessions[call_id] = session
        session.state = state or session.state
        session.caller = caller or session.caller
        session.callee = callee or session.callee
        session.route_kind = route_kind or session.route_kind
        session.terminal_reason = terminal_reason or session.terminal_reason
        session.metadata.update({key: value for key, value in metadata.items() if value not in (None, "")})
        return session

    def add_leg(
        self,
        call_id: str,
        leg_id: str,
        *,
        role: LegRole,
        state: str = "",
        sip_call_id: str = "",
        **metadata: Any,
    ) -> CallLeg:
        session = self.upsert(call_id, state=state or "active", **metadata)
        leg = session.legs.get(leg_id)
        if leg is None:
            leg = CallLeg(leg_id=leg_id, role=role, sip_call_id=sip_call_id or leg_id)
            session.legs[leg_id] = leg
        leg.role = role
        leg.state = state or leg.state
        leg.sip_call_id = sip_call_id or leg.sip_call_id
        self.leg_index[leg_id] = call_id
        return leg

    def resolve_session_id(self, call_id: str) -> str:
        return self.leg_index.get(call_id, call_id)

    def finish(self, call_id: str, *, reason: str = "", state: str = "idle") -> CallSession | None:
        session_id = self.resolve_session_id(call_id)
        session = self.sessions.get(session_id)
        if session is None:
            return None
        session.state = state
        session.terminal_reason = reason or session.terminal_reason
        return session

    def pop(self, call_id: str) -> CallSession | None:
        session_id = self.resolve_session_id(call_id)
        session = self.sessions.pop(session_id, None)
        if session is not None:
            for leg_id in list(session.legs):
                self.leg_index.pop(leg_id, None)
        self.leg_index.pop(call_id, None)
        return session

    def bridge_for(self, call_id: str) -> tuple[str, str]:
        source_call_id = call_id if call_id in self.bridge_clients else ""
        dest_call_id = self.bridge_clients.get(source_call_id, "") if source_call_id else ""
        if source_call_id:
            return source_call_id, dest_call_id
        for source, dest in self.bridge_clients.items():
            if dest == call_id:
                return source, dest
        return "", ""

    def detach_bridge(self, call_id: str) -> tuple[str, str, Any | None, Any | None, Any | None, bool]:
        source_call_id, dest_call_id = self.bridge_for(call_id)
        if not source_call_id:
            return "", "", None, None, None, False
        called_by_dest = call_id == dest_call_id
        self.bridge_clients.pop(source_call_id, None)
        relay = self.relays.pop(source_call_id, None)
        client = self.sip_clients.pop(dest_call_id, None) if dest_call_id else None
        watcher = self.client_watchers.pop(dest_call_id, None) if dest_call_id else None
        return source_call_id, dest_call_id, relay, client, watcher, called_by_dest

    def finish_and_pop(self, call_id: str, *, reason: str = "", state: str = "idle") -> CallSession | None:
        self.finish(call_id, reason=reason, state=state)
        return self.pop(call_id)

    def discard_bridge_session(
        self,
        source_call_id: str,
        dest_call_id: str = "",
        *,
        reason: str = "",
        state: str = "idle",
    ) -> Any | None:
        dest = dest_call_id or self.bridge_clients.get(source_call_id, "")
        self.bridge_clients.pop(source_call_id, None)
        client = self.sip_clients.pop(dest, None) if dest else None
        self.finish_and_pop(source_call_id, reason=reason, state=state)
        return client

    def detach_client(self, call_id: str) -> tuple[Any | None, Any | None]:
        client = self.sip_clients.pop(call_id, None)
        watcher = self.client_watchers.pop(call_id, None)
        return client, watcher

    def register_bridge(
        self,
        *,
        source_call_id: str,
        dest_call_id: str,
        client: Any,
        state: str,
        caller: str = "",
        callee: str = "",
        route_kind: str = "",
        source_role: LegRole = "caller",
        dest_role: LegRole = "callee",
        source_state: str = "",
        dest_state: str = "",
    ) -> CallSession:
        self.sip_clients[dest_call_id] = client
        self.bridge_clients[source_call_id] = dest_call_id
        session = self.upsert(
            source_call_id,
            state=state,
            caller=caller,
            callee=callee,
            route_kind=route_kind,
        )
        self.add_leg(source_call_id, source_call_id, role=source_role, state=source_state or state)
        self.add_leg(source_call_id, dest_call_id, role=dest_role, state=dest_state or state)
        return session

    def clear_runtime(self) -> None:
        self.sessions.clear()
        self.leg_index.clear()
        self.pending_routes.clear()
        self.pending_invites.clear()
        self.preanswered.clear()
        self.softphone_media.clear()
        self.sip_clients.clear()
        self.client_watchers.clear()
        self.relays.clear()
        self.bridge_clients.clear()

    def active_count(self, *, include_ha_softphone: bool = True) -> int:
        count = 0
        for session in self.sessions.values():
            if session.state in TERMINAL_STATES:
                continue
            if include_ha_softphone or not any(leg.role == "ha_softphone" for leg in session.legs.values()):
                count += 1
        return count

    def snapshot(self) -> dict[str, Any]:
        return {
            "sessions": len(self.sessions),
            "active_sessions": self.active_count(),
            "call_ids": sorted(self.sessions),
            "pending_call_ids": sorted(self.pending_invites),
            "media_call_ids": sorted(self.softphone_media),
            "bridge_call_ids": sorted(self.bridge_clients),
        }
