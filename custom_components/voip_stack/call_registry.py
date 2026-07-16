"""Authoritative HA-side SIP call session registry."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal


LegRole = Literal["caller", "callee", "trunk", "ha_softphone", "esp", "softphone", "router", "assist", "local_phone"]
CallOwner = Literal["", "ha_softphone", "router", "bridge", "assist", "local_bridge", "terminal"]
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
    media: Any | None = None


@dataclass(slots=True)
class CallSession:
    id: str
    revision: int = 0
    state: str = "new"
    owner: CallOwner = ""
    outcome: str = ""
    caller: str = ""
    callee: str = ""
    route_kind: str = ""
    terminal_reason: str = ""
    legs: dict[str, CallLeg] = field(default_factory=dict)
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class CallEventContext:
    """Small bounded automation-facing history for one logical call."""

    sequence: int = 0
    state: str = ""
    previous_state: str = ""
    route_history: list[dict[str, Any]] = field(default_factory=list)


class CallRegistry:
    """Small session index shared by softphone, router and trunk handlers."""

    def __init__(self) -> None:
        self.sessions: dict[str, CallSession] = {}
        self.leg_index: dict[str, str] = {}
        self.pending_routes: dict[str, dict[str, Any]] = {}
        self.pending_invites: dict[str, Any] = {}
        self.preanswered: dict[str, dict[str, Any]] = {}
        self.softphone_media: dict[str, dict[str, Any]] = {}
        self.video_parameter_sets: dict[str, tuple[bytes, ...]] = {}
        self.sip_clients: dict[str, Any] = {}
        self.client_watchers: dict[str, Any] = {}
        self.relays: dict[str, Any] = {}
        self.bridge_clients: dict[str, str] = {}
        self.event_contexts: dict[str, CallEventContext] = {}
        self.endpoint_claims: dict[str, dict[str, str]] = {}
        self.terminated_call_ids: set[str] = set()
        self._endpoint_registry: Any | None = None

    def begin_termination(self, call_id: str) -> bool:
        """Atomically claim teardown ownership for a call or one of its legs.

        SIP transports, client watchers and local UI actions may all observe
        the same terminal event.  Exactly one of them may perform teardown;
        later notifications are acknowledgements, not new state transitions.
        """
        call_id = str(call_id or "").strip()
        if not call_id:
            return False
        session_id = self.resolve_session_id(call_id)
        if call_id in self.terminated_call_ids or session_id in self.terminated_call_ids:
            return False
        if len(self.terminated_call_ids) >= 512:
            self.terminated_call_ids.pop()
        self.terminated_call_ids.update((call_id, session_id))
        return True

    def bind_endpoint_registry(self, registry: Any | None) -> None:
        """Bind the logical endpoint registry used for atomic busy claims.

        The call registry deliberately depends only on the tiny ``claim_call`` /
        ``release_call`` protocol.  This keeps the SIP session model reusable in
        pure tests while making teardown the single owner of endpoint release.
        """
        if registry is self._endpoint_registry:
            return
        if self.endpoint_claims:
            self._release_all_endpoint_claims()
        self._endpoint_registry = registry

    def claim_endpoint(
        self,
        call_id: str,
        endpoint_id: str,
        *,
        role: str = "endpoint",
        adopt_transport: bool = False,
    ) -> bool:
        """Atomically reserve an endpoint for this logical call.

        Returns ``False`` only when no endpoint registry is configured (the
        supported YAML-only compatibility mode).  Busy errors from the bound
        registry remain authoritative and are intentionally propagated.
        ``adopt_transport`` may replace only the provisional ``physical:``
        claim emitted by an ESP state entity; it can never steal a real call.
        """
        registry = self._endpoint_registry
        endpoint_id = str(endpoint_id or "").strip()
        if registry is None or not endpoint_id:
            return False
        session_id = self.resolve_session_id(str(call_id or "").strip())
        if not session_id:
            raise ValueError("call_id must not be empty")
        if adopt_transport and hasattr(registry, "adopt_transport_call"):
            registry.adopt_transport_call(endpoint_id, session_id)
        else:
            registry.claim_call(endpoint_id, session_id)
        claims = self.endpoint_claims.setdefault(session_id, {})
        previous = claims.get(endpoint_id)
        claims[endpoint_id] = str(role or "endpoint")
        if previous != claims[endpoint_id]:
            session = self.sessions.get(session_id)
            if session is not None:
                session.revision += 1
        return True

    def release_endpoint_claims(self, call_id: str) -> None:
        """Release every logical endpoint owned by a call or one of its legs."""
        session_id = self.resolve_session_id(str(call_id or "").strip())
        claims = self.endpoint_claims.get(session_id, {})
        registry = self._endpoint_registry
        if registry is None:
            self.endpoint_claims.pop(session_id, None)
            return
        for endpoint_id in tuple(claims):
            # Config removal may have already detached the endpoint from a
            # third-party registry implementation. Teardown must continue and
            # release every remaining participant rather than leak a session.
            if not hasattr(registry, "get") or registry.get(endpoint_id) is not None:
                registry.release_call(endpoint_id, session_id)
            claims.pop(endpoint_id, None)
        self.endpoint_claims.pop(session_id, None)

    def release_endpoint_claim(self, call_id: str, endpoint_id: str) -> bool:
        """Release one losing/finished endpoint leg without ending the call."""
        session_id = self.resolve_session_id(str(call_id or "").strip())
        endpoint_id = str(endpoint_id or "").strip()
        claims = self.endpoint_claims.get(session_id)
        if not endpoint_id or claims is None or endpoint_id not in claims:
            return False
        registry = self._endpoint_registry
        released = False
        if registry is not None and (
            not hasattr(registry, "get") or registry.get(endpoint_id) is not None
        ):
            released = bool(registry.release_call(endpoint_id, session_id))
        claims.pop(endpoint_id, None)
        if not claims:
            self.endpoint_claims.pop(session_id, None)
        session = self.sessions.get(session_id)
        if session is not None:
            session.revision += 1
        return released

    def _release_all_endpoint_claims(self) -> None:
        for session_id in tuple(self.endpoint_claims):
            self.release_endpoint_claims(session_id)

    def event_fields(self, call_id: str, state: str) -> dict[str, Any]:
        """Return stable automation fields, advancing only on a state change."""
        call_id = str(call_id or "").strip()
        state = str(state or "").strip()
        if not call_id:
            return {
                "schema_version": 1,
                "sequence": 0,
                "revision": 0,
                "owner": "",
                "previous_state": "",
                "route_history": [],
            }
        call_id = self.resolve_session_id(call_id)
        context = self.event_contexts.get(call_id)
        if context is None:
            if len(self.event_contexts) >= 256:
                self.event_contexts.pop(next(iter(self.event_contexts)))
            context = CallEventContext()
            self.event_contexts[call_id] = context
        if state and state != context.state:
            context.previous_state = context.state
            context.state = state
            context.sequence += 1
        session = self.sessions.get(call_id)
        fields = {
            "schema_version": 1,
            "sequence": context.sequence,
            "revision": session.revision if session is not None else 0,
            "owner": session.owner if session is not None else "",
            "previous_state": context.previous_state,
            "route_history": [dict(item) for item in context.route_history],
        }
        if session is None:
            return fields

        # Event entities must be attributed from call ownership, never by
        # resolving a caller-controlled display name. Preserve the explicit
        # source/destination metadata and include every atomically claimed
        # phone for ring groups and conferences.
        identity_keys = (
            "endpoint_id",
            "source_endpoint_id",
            "dest_endpoint_id",
            "target_endpoint_id",
            "device_id",
            "source_device_id",
            "dest_device_id",
            "target_device_id",
        )
        fields.update(
            {
                key: value
                for key in identity_keys
                if (value := session.metadata.get(key)) not in (None, "")
            }
        )
        participant_endpoint_ids = {
            str(value).strip()
            for key in (
                "endpoint_id",
                "source_endpoint_id",
                "dest_endpoint_id",
                "target_endpoint_id",
            )
            if (value := session.metadata.get(key)) not in (None, "")
        }
        participant_endpoint_ids.update(self.endpoint_claims.get(call_id, {}))
        participant_endpoint_ids.discard("")
        if participant_endpoint_ids:
            fields["participant_endpoint_ids"] = sorted(
                participant_endpoint_ids
            )
        return fields

    def event_context(self, call_id: str) -> CallEventContext | None:
        """Return the current automation event context for a call or leg."""
        return self.event_contexts.get(self.resolve_session_id(str(call_id or "").strip()))

    def record_route(
        self,
        call_id: str,
        *,
        action: str,
        destination: str = "",
        source: str = "automation",
    ) -> list[dict[str, Any]]:
        """Append one bounded routing decision to the call history."""
        call_id = self.resolve_session_id(str(call_id or "").strip())
        context = self.event_contexts.get(call_id)
        if context is None:
            self.event_fields(call_id, "")
            context = self.event_contexts[call_id]
        context.route_history.append(
            {
                "action": str(action or "").strip(),
                "destination": str(destination or "").strip(),
                "source": str(source or "automation").strip(),
            }
        )
        del context.route_history[:-8]
        session = self.sessions.get(call_id)
        if session is not None:
            session.revision += 1
        return [dict(item) for item in context.route_history]

    def upsert(
        self,
        call_id: str,
        *,
        state: str,
        caller: str = "",
        callee: str = "",
        route_kind: str = "",
        terminal_reason: str = "",
        owner: CallOwner = "",
        **metadata: Any,
    ) -> CallSession:
        session = self.sessions.get(call_id)
        if session is None:
            session = CallSession(id=call_id)
            self.sessions[call_id] = session
        changed = False
        for attribute, value in (
            ("state", state),
            ("owner", owner),
            ("caller", caller),
            ("callee", callee),
            ("route_kind", route_kind),
            ("terminal_reason", terminal_reason),
        ):
            if value and getattr(session, attribute) != value:
                setattr(session, attribute, value)
                changed = True
        clean_metadata = {
            key: value for key, value in metadata.items() if value not in (None, "")
        }
        if any(session.metadata.get(key) != value for key, value in clean_metadata.items()):
            session.metadata.update(clean_metadata)
            changed = True
        if changed:
            session.revision += 1
        return session

    def transition(
        self,
        call_id: str,
        *,
        state: str = "",
        owner: CallOwner | None = None,
        outcome: str | None = None,
        caller: str = "",
        callee: str = "",
        route_kind: str = "",
        expected_revision: int | None = None,
        expected_owner: CallOwner | None = None,
        **metadata: Any,
    ) -> CallSession | None:
        """Apply one guarded control mutation and advance its revision once."""
        session_id = self.resolve_session_id(str(call_id or "").strip())
        session = self.sessions.get(session_id)
        if session is None:
            return None
        if expected_revision is not None and session.revision != int(expected_revision):
            return None
        if expected_owner is not None and session.owner != expected_owner:
            return None
        if state:
            session.state = state
        if owner is not None:
            session.owner = owner
        if outcome is not None:
            session.outcome = outcome
        if caller:
            session.caller = caller
        if callee:
            session.callee = callee
        if route_kind:
            session.route_kind = route_kind
        session.metadata.update(
            {key: value for key, value in metadata.items() if value not in (None, "")}
        )
        session.revision += 1
        return session

    def is_current(
        self,
        call_id: str,
        *,
        revision: int,
        owner: CallOwner | None = None,
    ) -> bool:
        """Return whether an asynchronous callback still owns this revision."""
        session = self.sessions.get(self.resolve_session_id(str(call_id or "").strip()))
        return bool(
            session is not None
            and session.revision == int(revision)
            and (owner is None or session.owner == owner)
        )

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
        changed = False
        if leg is None:
            leg = CallLeg(leg_id=leg_id, role=role, sip_call_id=sip_call_id or leg_id)
            session.legs[leg_id] = leg
            changed = True
        next_state = state or leg.state
        next_sip_call_id = sip_call_id or leg.sip_call_id
        if leg.role != role or leg.state != next_state or leg.sip_call_id != next_sip_call_id:
            changed = True
        leg.role = role
        leg.state = next_state
        leg.sip_call_id = next_sip_call_id
        self.leg_index[leg_id] = call_id
        if changed:
            session.revision += 1
        return leg

    def remove_leg(self, call_id: str, leg_id: str) -> CallLeg | None:
        """Remove one destination leg without ending its source call."""
        session_id = self.resolve_session_id(call_id)
        session = self.sessions.get(session_id)
        if session is None:
            return None
        leg = session.legs.pop(leg_id, None)
        self.leg_index.pop(leg_id, None)
        if leg is not None:
            session.revision += 1
        return leg

    def resolve_session_id(self, call_id: str) -> str:
        return self.leg_index.get(call_id, call_id)

    def bind_controller(
        self,
        call_id: str,
        *,
        context: Any | None = None,
        user_id: str = "",
        endpoint_id: str = "",
    ) -> CallSession:
        """Bind one logical call to its initiating HA user and context.

        The user identity is deliberately sticky for the whole call.  A later
        browser reconnect may reclaim media only as that same user; it cannot
        silently transfer a microphone or camera to another authenticated HA
        session.  A local browser-to-browser call instead owns one sticky user
        per endpoint leg, allowing two tablets with different HA users to talk
        without granting either user access to the other leg. Internal
        automations still retain their original HA Context so lifecycle events
        preserve trace/parent provenance.
        """

        session_id = self.resolve_session_id(str(call_id or "").strip())
        session = self.sessions.get(session_id)
        if session is None:
            raise ValueError(f"unknown call_id {call_id!r}")
        requested_user_id = str(
            user_id or getattr(context, "user_id", "") or ""
        ).strip()
        requested_endpoint_id = str(endpoint_id or "").strip()
        scoped = bool(
            requested_endpoint_id and session.metadata.get("local_bridge")
        )
        if scoped:
            controllers = session.metadata.setdefault("controller_user_ids", {})
            current_user_id = str(
                controllers.get(requested_endpoint_id) or ""
            ).strip()
        else:
            current_user_id = str(
                session.metadata.get("controller_user_id") or ""
            ).strip()
        if current_user_id and requested_user_id and current_user_id != requested_user_id:
            raise ValueError(
                f"call_id {session_id}"
                + (
                    f" endpoint {requested_endpoint_id}"
                    if scoped
                    else ""
                )
                + " is already controlled by another HA user"
            )
        changed = False
        if requested_user_id and not current_user_id:
            if scoped:
                controllers[requested_endpoint_id] = requested_user_id
            else:
                session.metadata["controller_user_id"] = requested_user_id
            changed = True
        if context is not None and session.metadata.get("ha_context") is None:
            session.metadata["ha_context"] = context
            changed = True
        if changed:
            session.revision += 1
        return session

    def ha_context(self, call_id: str) -> Any | None:
        """Return the original HA Context for a call or one of its legs."""

        session = self.sessions.get(
            self.resolve_session_id(str(call_id or "").strip())
        )
        return session.metadata.get("ha_context") if session is not None else None

    def finish(self, call_id: str, *, reason: str = "", state: str = "idle") -> CallSession | None:
        session_id = self.resolve_session_id(call_id)
        session = self.sessions.get(session_id)
        if session is None:
            return None
        session.state = state
        session.terminal_reason = reason or session.terminal_reason
        session.owner = "terminal"
        session.outcome = reason or session.outcome
        session.revision += 1
        return session

    def pop(self, call_id: str) -> CallSession | None:
        call_id = str(call_id or "").strip()
        session_id = self.resolve_session_id(call_id)
        self.release_endpoint_claims(session_id)
        session = self.sessions.pop(session_id, None)
        event_context_ids = {call_id, session_id}
        if session is not None:
            for leg_id in list(session.legs):
                event_context_ids.add(leg_id)
                self.leg_index.pop(leg_id, None)
        self.leg_index.pop(call_id, None)
        for context_id in event_context_ids:
            self.event_contexts.pop(context_id, None)
        self.pending_invites.pop(session_id, None)
        self.video_parameter_sets.pop(session_id, None)
        self.video_parameter_sets.pop(call_id, None)
        route = self.pending_routes.pop(session_id, None)
        if route is not None:
            future = route.get("future")
            if (
                future is not None
                and hasattr(future, "done")
                and not future.done()
            ):
                future.cancel()
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
        session_id = self.resolve_session_id(str(call_id or "").strip())
        if session_id:
            if len(self.terminated_call_ids) >= 512:
                self.terminated_call_ids.pop()
            self.terminated_call_ids.update((str(call_id or "").strip(), session_id))
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
            owner="bridge",
            caller=caller,
            callee=callee,
            route_kind=route_kind,
        )
        self.add_leg(source_call_id, source_call_id, role=source_role, state=source_state or state)
        self.add_leg(source_call_id, dest_call_id, role=dest_role, state=dest_state or state)
        return session

    def clear_runtime(self) -> None:
        self._release_all_endpoint_claims()
        self.sessions.clear()
        self.leg_index.clear()
        self.pending_routes.clear()
        self.pending_invites.clear()
        self.preanswered.clear()
        self.softphone_media.clear()
        self.video_parameter_sets.clear()
        self.sip_clients.clear()
        self.client_watchers.clear()
        self.relays.clear()
        self.bridge_clients.clear()
        self.event_contexts.clear()
        self.terminated_call_ids.clear()

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
            "endpoint_claims": {
                call_id: dict(claims)
                for call_id, claims in sorted(self.endpoint_claims.items())
            },
        }
