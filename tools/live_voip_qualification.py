#!/usr/bin/env python3
"""Live HA + ESP qualification matrix for VoIP Stack.

This is intentionally a real-system runner, not a simulator. It drives the
Home Assistant integration through REST/websocket and drives ESP devices through
the native ESPHome API, then asserts both sides converge to the expected state.
Use it after deploying HA/ESP firmware changes that touch routing, signaling,
phonebook sync, card-visible state, ring groups, or conference groups.
"""

from __future__ import annotations

import argparse
import asyncio
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
import json
from pathlib import Path
import ssl
import time
from typing import Any
import urllib.error
import urllib.request

try:
    from aioesphomeapi import APIClient
except ModuleNotFoundError:  # pragma: no cover - dependency-light CI only imports contracts.
    APIClient = None

try:
    import websockets
except ModuleNotFoundError:  # pragma: no cover - dependency-light CI only imports contracts.
    websockets = None


DEFAULT_HA_URL = "https://f0260ef3d722.sn.mynetname.net"
DEFAULT_TOKEN_FILE = Path("/home/codex/.secrets/esphome-intercom/ha_token_codex")
OUT = Path("test_runs/live_voip_qualification")


def norm(value: Any) -> str:
    if isinstance(value, bool):
        return "on" if value else "off"
    return str(value or "").strip().lower().replace(" ", "_")


async def maybe_await(result: Any) -> None:
    if hasattr(result, "__await__"):
        await result


@dataclass(frozen=True)
class EspDevice:
    key: str
    name: str
    host: str
    port: int = 6053
    password: str = ""
    ha_state_entity: str = ""


DEFAULT_ESPS = {
    "ws3": EspDevice("ws3", "Waveshare S3 Audio", "192.168.1.47", ha_state_entity="sensor.cucina_waveshare_s3_audio_voip_state"),
    "spotpear": EspDevice("spotpear", "Spotpear Ball v2", "192.168.1.31", ha_state_entity="sensor.casa_spotpear_ball_v2_voip_state"),
}


@dataclass(frozen=True)
class Scenario:
    id: str
    title: str
    requires: frozenset[str]
    assertions: frozenset[str]
    run: Callable[["LiveContext"], Awaitable[None]]


class HaRest:
    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.ssl_context = ssl._create_unverified_context()

    def _request(self, method: str, path: str, data: dict[str, Any] | None = None) -> Any:
        raw = None
        headers = {"Authorization": f"Bearer {self.token}"}
        if data is not None:
            raw = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(f"{self.base_url}{path}", data=raw, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=14, context=self.ssl_context) as resp:
                body = resp.read().decode()
        except urllib.error.HTTPError as err:
            detail = err.read().decode(errors="replace")
            raise AssertionError(f"HA {method} {path} failed: {err.code} {detail}") from err
        return json.loads(body) if body else None

    async def state(self, entity_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._request, "GET", f"/api/states/{entity_id}")

    async def service(self, domain: str, service: str, data: dict[str, Any] | None = None) -> Any:
        return await asyncio.to_thread(self._request, "POST", f"/api/services/{domain}/{service}", data or {})


class HaWs:
    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.ssl_context = ssl._create_unverified_context()
        self.ws: Any = None
        self._next_id = 1
        self.events: list[dict[str, Any]] = []

    async def __aenter__(self) -> "HaWs":
        if websockets is None:
            raise RuntimeError("websockets is required to run live HA websocket qualification")
        url = self.base_url.replace("https://", "wss://").replace("http://", "ws://") + "/api/websocket"
        self.ws = await websockets.connect(url, ssl=self.ssl_context)
        hello = json.loads(await self.ws.recv())
        if hello.get("type") != "auth_required":
            raise AssertionError(f"unexpected HA websocket hello: {hello}")
        await self.ws.send(json.dumps({"type": "auth", "access_token": self.token}))
        auth = json.loads(await self.ws.recv())
        if auth.get("type") != "auth_ok":
            raise AssertionError(f"HA websocket auth failed: {auth}")
        await self.command({"type": "subscribe_events", "event_type": "voip_stack.call_event"})
        return self

    async def __aexit__(self, *_: object) -> None:
        if self.ws is not None:
            await self.ws.close()

    async def command(self, msg: dict[str, Any], timeout: float = 8.0) -> dict[str, Any]:
        assert self.ws is not None
        msg_id = self._next_id
        self._next_id += 1
        await self.ws.send(json.dumps({"id": msg_id, **msg}))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            raw = await asyncio.wait_for(self.ws.recv(), timeout=max(0.1, deadline - time.monotonic()))
            packet = json.loads(raw)
            if packet.get("type") == "event":
                self.events.append(packet)
                continue
            if packet.get("id") == msg_id:
                if packet.get("success") is False:
                    raise AssertionError(f"HA websocket command failed: {packet}")
                return packet
        raise AssertionError(f"HA websocket command timed out: {msg}")

    async def softphone_state(self) -> dict[str, Any]:
        msg = await self.command({"type": "voip_stack/ha_softphone_state"})
        return dict(msg.get("result") or {})

    async def drain_events(self, seconds: float) -> None:
        assert self.ws is not None
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                raw = await asyncio.wait_for(self.ws.recv(), timeout=max(0.05, deadline - time.monotonic()))
            except asyncio.TimeoutError:
                return
            packet = json.loads(raw)
            if packet.get("type") == "event":
                self.events.append(packet)


class EspApi:
    def __init__(self, spec: EspDevice) -> None:
        if APIClient is None:
            raise RuntimeError("aioesphomeapi is required to run live ESP qualification")
        self.spec = spec
        self.client = APIClient(spec.host, spec.port, spec.password)
        self.entities: dict[str, Any] = {}
        self.services: dict[str, Any] = {}
        self.values: dict[str, Any] = {}
        self._object_by_key: dict[int, str] = {}
        self._updates: asyncio.Queue[tuple[str, Any]] = asyncio.Queue()

    async def __aenter__(self) -> "EspApi":
        await self.client.connect(login=True)
        entities, services = await self.client.list_entities_services()
        self.entities = {str(getattr(entity, "object_id", "")): entity for entity in entities}
        self.services = {str(getattr(service, "name", "")): service for service in services}
        self._object_by_key = {int(getattr(entity, "key", -1)): object_id for object_id, entity in self.entities.items()}
        await maybe_await(self.client.subscribe_states(self._on_state))
        await asyncio.sleep(0.6)
        return self

    async def __aexit__(self, *_: object) -> None:
        await self.client.disconnect()

    def _on_state(self, state: Any) -> None:
        key = int(getattr(state, "key", -1))
        object_id = self._object_by_key.get(key)
        if not object_id:
            return
        value = getattr(state, "state", None)
        if value is None:
            value = getattr(state, "value", None)
        self.values[object_id] = value
        self._updates.put_nowait((object_id, value))

    async def service(self, name: str, data: dict[str, Any] | None = None) -> None:
        service = self.services.get(name)
        if service is None:
            raise AssertionError(f"{self.spec.key}: ESP service {name!r} not exposed")
        await maybe_await(self.client.execute_service(service, data or {}))

    async def button(self, object_id: str) -> None:
        entity = self.entities.get(object_id)
        if entity is None:
            raise AssertionError(f"{self.spec.key}: ESP button {object_id!r} not exposed")
        await maybe_await(self.client.button_command(entity.key))

    async def switch(self, object_id: str, value: bool) -> None:
        entity = self.entities.get(object_id)
        if entity is None:
            raise AssertionError(f"{self.spec.key}: ESP switch {object_id!r} not exposed")
        await maybe_await(self.client.switch_command(entity.key, value))
        await self.wait(object_id, {"on" if value else "off"}, timeout=5)

    async def text(self, object_id: str, value: str) -> None:
        entity = self.entities.get(object_id)
        if entity is None:
            raise AssertionError(f"{self.spec.key}: ESP text {object_id!r} not exposed")
        await maybe_await(self.client.text_command(entity.key, value))
        await self.wait(object_id, {value}, timeout=6, exact=True)

    async def wait(
        self,
        object_id: str,
        wanted: set[str],
        *,
        timeout: float = 10.0,
        exact: bool = False,
    ) -> Any:
        deadline = time.monotonic() + timeout
        wanted_norm = {norm(value) for value in wanted}
        while time.monotonic() < deadline:
            current = self.values.get(object_id)
            if exact:
                if str(current or "") in wanted:
                    return current
            elif norm(current) in wanted_norm:
                return current
            try:
                await asyncio.wait_for(self._updates.get(), timeout=0.2)
            except asyncio.TimeoutError:
                pass
        raise AssertionError(f"{self.spec.key}: {object_id} expected {sorted(wanted)}, current={self.values.get(object_id)!r}")

    async def wait_predicate(
        self,
        predicate: Callable[[], bool],
        description: str,
        *,
        timeout: float = 10.0,
    ) -> None:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            try:
                await asyncio.wait_for(self._updates.get(), timeout=0.2)
            except asyncio.TimeoutError:
                pass
        raise AssertionError(f"{self.spec.key}: timed out waiting for {description}; snapshot={self.snapshot()}")

    def snapshot(self) -> dict[str, Any]:
        return {
            "device": self.spec.key,
            "state": self.values.get("voip_state"),
            "caller": self.values.get("voip_caller"),
            "destination": self.values.get("voip_destination"),
            "last_reason": self.values.get("voip_last_reason"),
            "endpoint": self.values.get("voip_endpoint"),
            "contacts": self.values.get("voip_contacts"),
            "extension": self.values.get("voip_extension"),
            "ring_groups": self.values.get("voip_ring_groups"),
            "conference_groups": self.values.get("voip_conference_groups"),
            "ring_on_conference": self.values.get("voip_ring_on_conference"),
            "dnd": self.values.get("do_not_disturb"),
            "auto_answer": self.values.get("auto_answer"),
        }


@dataclass
class LiveContext:
    ha: HaRest
    ws: HaWs
    esp: EspApi
    args: argparse.Namespace
    artifacts: list[dict[str, Any]] = field(default_factory=list)

    async def cleanup(self) -> None:
        for _ in range(2):
            await self.ha.service("voip_stack", "hangup", {})
            await self.ha.service("voip_stack", "decline", {"reason": "cleanup", "decline_reason": "cleanup"})
        with contextlib_suppress():
            await self.esp.service("decline_call", {"reason": "cleanup"})
        deadline = time.monotonic() + 8.0
        last: dict[str, Any] | None = None
        while time.monotonic() < deadline:
            last = await self.ws.softphone_state()
            active = int(last.get("active_dialogs") or 0)
            pending = list(last.get("pending_call_ids") or [])
            state = norm(self.esp.values.get("voip_state"))
            if active == 0 and not pending and state == "idle":
                await asyncio.sleep(0.8)
                return
            await asyncio.sleep(0.3)
        raise AssertionError(f"cleanup did not settle HA/ESP runtime: softphone={last} esp={self.esp.snapshot()}")

    def capture(self, label: str) -> None:
        self.artifacts.append({
            "label": label,
            "t": time.monotonic(),
            "esp": self.esp.snapshot(),
        })


class contextlib_suppress:
    def __enter__(self) -> None:
        return None

    def __exit__(self, *_: object) -> bool:
        return True


async def wait_phonebook_contains(ha: HaRest, target: str, *, timeout: float = 12.0) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        last = await ha.state("sensor.voip_phonebook")
        raw = last.get("attributes", {}).get("roster_json")
        if raw:
            payload = json.loads(raw)
            contacts = payload.get("contacts") or []
            for item in contacts:
                values = {
                    str(item.get("id") or ""),
                    str(item.get("name") or ""),
                    str(item.get("extension") or ""),
                }
                if target in values:
                    return item
        await asyncio.sleep(0.35)
    raise AssertionError(f"phonebook did not expose {target!r}; last={last}")


async def wait_phonebook_group_member(
    ha: HaRest,
    group: str,
    member: str,
    member_key: str,
    *,
    timeout: float = 12.0,
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        last = await ha.state("sensor.voip_phonebook")
        raw = last.get("attributes", {}).get("roster_json")
        if raw:
            payload = json.loads(raw)
            for item in payload.get("contacts") or []:
                if str(item.get("id") or item.get("name") or "") != group:
                    continue
                metadata = item.get("metadata") if isinstance(item.get("metadata"), dict) else {}
                values = [str(value).strip() for value in metadata.get(member_key) or []]
                if member in values:
                    return item
        await asyncio.sleep(0.35)
    raise AssertionError(f"phonebook group {group!r} did not expose {member!r} in {member_key}; last={last}")


async def wait_softphone_state(ctx: LiveContext, wanted: set[str], *, timeout: float = 10.0) -> dict[str, Any]:
    wanted_norm = {norm(value) for value in wanted}
    deadline = time.monotonic() + timeout
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        last = await ctx.ws.softphone_state()
        if norm(last.get("state")) in wanted_norm:
            return last
        await asyncio.sleep(0.2)
    raise AssertionError(f"HA softphone expected {sorted(wanted)}, last={last}")


async def wait_esp_voip_state(ctx: LiveContext, wanted: set[str], *, timeout: float = 10.0) -> Any:
    try:
        return await ctx.esp.wait("voip_state", wanted, timeout=timeout)
    except AssertionError as err:
        if not ctx.esp.spec.ha_state_entity:
            raise
        wanted_norm = {norm(item) for item in wanted}
        deadline = time.monotonic() + 3.0
        while time.monotonic() < deadline:
            state = await ctx.ha.state(ctx.esp.spec.ha_state_entity)
            value = state.get("state")
            ctx.esp.values["voip_state"] = value
            if norm(value) in wanted_norm:
                return value
            await asyncio.sleep(0.25)
        raise err


async def set_baseline(ctx: LiveContext) -> dict[str, Any]:
    original = {
        "extension": str(ctx.esp.values.get("voip_extension") or ""),
        "ring_groups": str(ctx.esp.values.get("voip_ring_groups") or ""),
        "conference_groups": str(ctx.esp.values.get("voip_conference_groups") or ""),
        "ring_on_conference": norm(ctx.esp.values.get("voip_ring_on_conference")) == "on",
        "dnd": norm(ctx.esp.values.get("do_not_disturb")) == "on",
        "auto_answer": norm(ctx.esp.values.get("auto_answer")) == "on",
    }
    await ctx.esp.text("voip_extension", ctx.args.esp_extension)
    await ctx.esp.text("voip_ring_groups", ctx.args.ring_group)
    await ctx.esp.text("voip_conference_groups", ctx.args.conference_group)
    await ctx.esp.switch("voip_ring_on_conference", False)
    await ctx.esp.switch("do_not_disturb", False)
    await ctx.esp.switch("auto_answer", False)
    await wait_phonebook_contains(ctx.ha, ctx.args.esp_extension)
    await wait_phonebook_contains(ctx.ha, ctx.args.ring_group)
    await wait_phonebook_contains(ctx.ha, ctx.args.conference_group)
    await ctx.ha.service(
        "voip_stack",
        "set_ha_softphone_settings",
        {
            "extension": ctx.args.ha_extension,
            "ring_group": "",
            "conference_group": ctx.args.conference_group,
            "conference_ring": False,
        },
    )
    await wait_phonebook_contains(ctx.ha, ctx.args.ha_extension)
    return original


async def restore_baseline(ctx: LiveContext, original: dict[str, Any]) -> None:
    await ctx.cleanup()
    await ctx.esp.text("voip_extension", original["extension"])
    await ctx.esp.text("voip_ring_groups", original["ring_groups"])
    await ctx.esp.text("voip_conference_groups", original["conference_groups"])
    await ctx.esp.switch("voip_ring_on_conference", bool(original["ring_on_conference"]))
    await ctx.esp.switch("do_not_disturb", bool(original["dnd"]))
    await ctx.esp.switch("auto_answer", bool(original["auto_answer"]))


async def scenario_ha_to_esp_extension_answer_hangup(ctx: LiveContext) -> None:
    await ctx.cleanup()
    await ctx.ha.service("voip_stack", "call", {"destination": ctx.args.esp_extension})
    await wait_esp_voip_state(ctx, {"ringing", "incoming"}, timeout=12)
    await ctx.esp.button("call")
    await wait_esp_voip_state(ctx, {"in_call"}, timeout=12)
    soft = await wait_softphone_state(ctx, {"in_call"}, timeout=8)
    if str(soft.get("peer_name") or "") not in {ctx.esp.spec.name, ctx.args.esp_extension}:
        raise AssertionError(f"HA softphone did not resolve ESP extension to ESP peer: {soft}")
    await ctx.ha.service("voip_stack", "hangup", {})
    await wait_esp_voip_state(ctx, {"idle"}, timeout=12)
    ctx.capture("ha_to_esp_extension_answer_hangup")


async def scenario_ha_to_esp_dnd(ctx: LiveContext) -> None:
    await ctx.cleanup()
    await ctx.esp.switch("do_not_disturb", True)
    try:
        await ctx.ha.service("voip_stack", "call", {"destination": ctx.args.esp_extension})
        await wait_esp_voip_state(ctx, {"idle"}, timeout=10)
        soft = await wait_softphone_state(ctx, {"idle", "busy", "declined"}, timeout=10)
        if norm(soft.get("terminal_reason")) not in {"busy", "dnd", "declined", "remote_hangup"}:
            raise AssertionError(f"HA softphone did not surface ESP DND/busy terminal: {soft}")
    finally:
        await ctx.esp.switch("do_not_disturb", False)
    ctx.capture("ha_to_esp_dnd")


async def scenario_ha_to_ring_group_answer(ctx: LiveContext) -> None:
    await ctx.cleanup()
    await ctx.ha.service("voip_stack", "call", {"destination": ctx.args.ring_group})
    await wait_esp_voip_state(ctx, {"ringing", "incoming"}, timeout=12)
    await ctx.esp.button("call")
    await wait_esp_voip_state(ctx, {"in_call"}, timeout=12)
    soft = await wait_softphone_state(ctx, {"in_call"}, timeout=8)
    if str(soft.get("peer_name") or "") == ctx.args.ring_group:
        raise AssertionError(f"HA softphone still displays ring group instead of winning member: {soft}")
    await ctx.ha.service("voip_stack", "hangup", {})
    await wait_esp_voip_state(ctx, {"idle"}, timeout=12)
    ctx.capture("ha_to_ring_group_answer")


async def scenario_ha_to_conference_group_rings_esp(ctx: LiveContext) -> None:
    await ctx.cleanup()
    await ctx.esp.switch("voip_ring_on_conference", True)
    try:
        await wait_phonebook_group_member(
            ctx.ha,
            ctx.args.conference_group,
            ctx.esp.spec.name,
            "ring_members",
            timeout=15,
        )
        await ctx.ha.service("voip_stack", "call", {"destination": ctx.args.conference_group})
        await wait_esp_voip_state(ctx, {"ringing", "incoming"}, timeout=12)
        await ctx.esp.button("call")
        await wait_esp_voip_state(ctx, {"in_call"}, timeout=12)
        await ctx.ha.service("voip_stack", "hangup", {})
        await asyncio.sleep(0.5)
        await ctx.esp.service("decline_call", {"reason": "qualification_cleanup"})
        await wait_esp_voip_state(ctx, {"idle"}, timeout=12)
    finally:
        await ctx.esp.switch("voip_ring_on_conference", False)
    ctx.capture("ha_to_conference_group_rings_esp")


async def scenario_esp_to_ha_extension_cancel(ctx: LiveContext) -> None:
    await ctx.cleanup()
    await ctx.esp.service("start_call", {"dest": ctx.args.ha_extension})
    await wait_esp_voip_state(ctx, {"calling", "remote_ringing"}, timeout=12)
    soft = await wait_softphone_state(ctx, {"ringing"}, timeout=8)
    if str(soft.get("dialed_target") or soft.get("callee") or "") != ctx.args.ha_extension:
        raise AssertionError(f"HA softphone did not preserve dialed extension: {soft}")
    await ctx.esp.service("decline_call", {"reason": "qualification_cancel"})
    await wait_esp_voip_state(ctx, {"idle"}, timeout=12)
    soft = await wait_softphone_state(ctx, {"idle", "cancelled"}, timeout=10)
    if soft.get("active_dialogs") or soft.get("pending_call_ids"):
        raise AssertionError(f"HA softphone kept SIP runtime after ESP cancel: {soft}")
    ctx.capture("esp_to_ha_extension_cancel")


async def scenario_esp_to_self_extension_busy(ctx: LiveContext) -> None:
    await ctx.cleanup()
    await ctx.esp.service("start_call", {"dest": ctx.args.esp_extension})
    await ctx.esp.wait_predicate(
        lambda: str(ctx.esp.values.get("voip_destination") or "") == ctx.args.esp_extension
        or norm(ctx.esp.values.get("voip_last_reason")) in {"busy", "declined", "cancelled", "routing_failed", "local_hangup"},
        f"ESP self-extension attempt to {ctx.args.esp_extension}",
        timeout=4,
    )
    await wait_esp_voip_state(ctx, {"idle"}, timeout=12)
    reason = norm(ctx.esp.values.get("voip_last_reason"))
    if reason not in {"busy", "declined", "cancelled", "routing_failed", "local_hangup"}:
        raise AssertionError(f"ESP self-extension terminal reason was not explicit: {ctx.esp.snapshot()}")
    ctx.capture("esp_to_self_extension_busy")


async def scenario_esp_to_trunk_cancel(ctx: LiveContext) -> None:
    if not ctx.args.allow_trunk:
        raise RuntimeError("trunk scenario requires --allow-trunk")
    await ctx.cleanup()
    await ctx.esp.service("start_call", {"dest": ctx.args.trunk_number})
    await wait_esp_voip_state(ctx, {"calling", "remote_ringing"}, timeout=18)
    await ctx.esp.service("decline_call", {"reason": "qualification_cancel"})
    await wait_esp_voip_state(ctx, {"idle"}, timeout=18)
    snap = ctx.esp.snapshot()
    if str(snap.get("destination") or "") not in {ctx.args.trunk_number, ""}:
        raise AssertionError(f"ESP trunk terminal target was rewritten unexpectedly: {snap}")
    ctx.capture("esp_to_trunk_cancel")


SCENARIOS: dict[str, Scenario] = {
    "ha_to_esp_extension_answer_hangup": Scenario(
        "ha_to_esp_extension_answer_hangup",
        "HA calls ESP by dynamic extension; ESP answers; HA hangs up",
        frozenset({"ha", "esp", "phonebook", "extension"}),
        frozenset({"esp_ringing", "esp_in_call", "ha_in_call", "remote_bye", "esp_idle"}),
        scenario_ha_to_esp_extension_answer_hangup,
    ),
    "ha_to_esp_dnd": Scenario(
        "ha_to_esp_dnd",
        "HA calls ESP by extension while ESP DND is enabled",
        frozenset({"ha", "esp", "dnd", "phonebook"}),
        frozenset({"esp_no_ringing", "ha_terminal_reason", "esp_idle"}),
        scenario_ha_to_esp_dnd,
    ),
    "ha_to_ring_group_answer": Scenario(
        "ha_to_ring_group_answer",
        "HA calls ring group; ESP rings, answers, and becomes visible winner",
        frozenset({"ha", "esp", "ring_group", "phonebook"}),
        frozenset({"esp_ringing", "winner_not_group_label", "esp_in_call", "cleanup_idle"}),
        scenario_ha_to_ring_group_answer,
    ),
    "ha_to_conference_group_rings_esp": Scenario(
        "ha_to_conference_group_rings_esp",
        "HA joins conference group; ESP ring-on-conference rings and joins",
        frozenset({"ha", "esp", "conference_group", "ring_on_conference"}),
        frozenset({"conference_started", "esp_ringing", "esp_joined", "cleanup_idle"}),
        scenario_ha_to_conference_group_rings_esp,
    ),
    "esp_to_ha_extension_cancel": Scenario(
        "esp_to_ha_extension_cancel",
        "ESP calls HA by extension; HA rings; ESP cancels before answer",
        frozenset({"ha", "esp", "extension", "cancel"}),
        frozenset({"ha_ringing", "dialed_target_preserved", "esp_cancel", "both_idle"}),
        scenario_esp_to_ha_extension_cancel,
    ),
    "esp_to_self_extension_busy": Scenario(
        "esp_to_self_extension_busy",
        "ESP calls its own extension and must not self-ring",
        frozenset({"esp", "extension", "busy"}),
        frozenset({"self_call_rejected", "esp_idle", "terminal_reason"}),
        scenario_esp_to_self_extension_busy,
    ),
    "esp_to_trunk_cancel": Scenario(
        "esp_to_trunk_cancel",
        "ESP dials an external trunk number and cancels while ringing",
        frozenset({"esp", "ha", "trunk", "cancel"}),
        frozenset({"trunk_route", "cancel_propagated", "esp_idle", "target_preserved"}),
        scenario_esp_to_trunk_cancel,
    ),
}


def selected_scenarios(args: argparse.Namespace) -> list[Scenario]:
    if args.list:
        return []
    names = list(args.scenario)
    if args.all:
        names = list(SCENARIOS)
    if not names:
        names = [
            "ha_to_esp_extension_answer_hangup",
            "esp_to_ha_extension_cancel",
            "ha_to_ring_group_answer",
            "ha_to_conference_group_rings_esp",
            "ha_to_esp_dnd",
            "esp_to_self_extension_busy",
        ]
    return [SCENARIOS[name] for name in names]


async def run(args: argparse.Namespace) -> int:
    if args.list:
        for scenario in SCENARIOS.values():
            print(f"{scenario.id}: {scenario.title} requires={','.join(sorted(scenario.requires))}")
        return 0
    token = args.token or args.token_file.read_text(encoding="utf-8").strip()
    ha = HaRest(args.ha_url, token)
    esp_spec = DEFAULT_ESPS[args.esp]
    OUT.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []
    async with HaWs(args.ha_url, token) as ws:
        async with EspApi(esp_spec) as esp:
            ctx = LiveContext(ha=ha, ws=ws, esp=esp, args=args)
            await ctx.cleanup()
            original = await set_baseline(ctx)
            try:
                for scenario in selected_scenarios(args):
                    if "trunk" in scenario.requires and not args.allow_trunk:
                        results.append({"scenario": scenario.id, "status": "skipped", "reason": "requires --allow-trunk"})
                        continue
                    start = time.monotonic()
                    try:
                        await scenario.run(ctx)
                    except Exception as err:  # noqa: BLE001 - write artifact before failing.
                        ctx.capture(f"{scenario.id}_failed")
                        results.append({"scenario": scenario.id, "status": "failed", "error": str(err), "duration_s": time.monotonic() - start})
                        raise
                    results.append({"scenario": scenario.id, "status": "passed", "duration_s": time.monotonic() - start})
                    print(f"PASS {scenario.id}")
                    await ctx.cleanup()
            finally:
                await restore_baseline(ctx, original)
                artifact = {
                    "created_at": datetime.now(UTC).isoformat(),
                    "esp": esp_spec.key,
                    "results": results,
                    "samples": ctx.artifacts,
                    "events": ws.events,
                }
                path = OUT / f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}_{esp_spec.key}_live_matrix.json"
                path.write_text(json.dumps(artifact, indent=2, ensure_ascii=False), encoding="utf-8")
                print(f"artifact={path}")
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ha-url", default=DEFAULT_HA_URL)
    parser.add_argument("--token-file", type=Path, default=DEFAULT_TOKEN_FILE)
    parser.add_argument("--token")
    parser.add_argument("--esp", choices=sorted(DEFAULT_ESPS), default="ws3")
    parser.add_argument("--scenario", choices=sorted(SCENARIOS), action="append", default=[])
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--list", action="store_true")
    parser.add_argument("--allow-trunk", action="store_true")
    parser.add_argument("--esp-extension", default="1000")
    parser.add_argument("--ha-extension", default="666")
    parser.add_argument("--ring-group", default="RG Casa")
    parser.add_argument("--conference-group", default="CG Casa")
    parser.add_argument("--trunk-number", default="3519968203")
    return parser.parse_args()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(parse_args())))
