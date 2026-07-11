#!/usr/bin/env python3
"""Deterministic runtime-controller qualification through Home Assistant.

The runner drives only the diagnostic runtime_event/runtime_set_activity API
actions. It does not start STT, TTS, media streams or SIP dialogs. A separate
live pass can then validate that real component callbacks produce the same
transition sequence.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass, replace
import json
from pathlib import Path
import sys
import time
from typing import Any
from itertools import permutations

from websockets.asyncio.client import connect


UI = {
    "idle": 0,
    "boot": 1,
    "no_wifi": 2,
    "no_ha": 3,
    "muted": 5,
    "wake": 7,
    "listening": 8,
    "thinking": 9,
    "responding": 10,
    "voip_ringing": 11,
    "voip_calling": 12,
    "voip_in_call": 13,
    "error": 14,
    "media": 15,
    "no_va": 16,
}

LED = {
    "idle": 0,
    "muted": 1,
    "mic_muted": 2,
    "speaker_muted": 3,
    "wake": 4,
    "listening": 5,
    "thinking": 6,
    "responding": 7,
    "voip_ringing": 8,
    "voip_calling": 9,
    "voip_in_call": 10,
    "error": 11,
    "media": 12,
    "boot": 13,
    "no_wifi": 14,
    "no_ha": 15,
    "no_va": 16,
    "timer_ringing": 17,
}


@dataclass(frozen=True)
class Device:
    key: str
    mac: str
    node_name: str
    service_prefix: str
    snapshot_entity: str
    media_entity: str
    voip_entity: str
    assist_entity: str
    light_entity: str | None
    led_preset: str | None


DEVICES = {
    "p4": Device(
        "p4",
        "30:ED:A0:E3:1D:39",
        "waveshare-p4-touch",
        "waveshare_p4_touch",
        "sensor.waveshare_p4_touch_runtime_snapshot",
        "media_player.waveshare_p4_touch",
        "sensor.waveshare_p4_touch_voip_state",
        "assist_satellite.waveshare_p4_touch_assist_satellite",
        None,
        None,
    ),
    "spotpear": Device(
        "spotpear",
        "B8:F8:62:E4:A1:38",
        "spotpear-ball-v2",
        "spotpear_ball_v2",
        "sensor.spotpear_ball_v2_runtime_snapshot",
        "media_player.spotpear_ball_v2",
        "sensor.spotpear_ball_v2_voip_state",
        "assist_satellite.spotpear_ball_v2_assist_satellite",
        "light.spotpear_ball_v2",
        "spotpear_rgb",
    ),
    "ws3": Device(
        "ws3",
        "1C:DB:D4:9B:8C:24",
        "waveshare-s3-audio",
        "waveshare_s3",
        "sensor.waveshare_s3_audio_runtime_snapshot",
        "media_player.waveshare_s3_audio",
        "sensor.waveshare_s3_audio_voip_state",
        "assist_satellite.waveshare_s3_audio_assist_satellite",
        "light.waveshare_s3_audio_status_led",
        "ws2812_ring",
    ),
}


def resolve_entity_id(states: dict[str, dict[str, Any]], expected: str) -> str:
    """Resolve HA's optional area prefix without making it part of the contract."""
    if expected in states:
        return expected
    domain, object_id = expected.split(".", 1)
    matches = [
        entity_id
        for entity_id in states
        if entity_id.startswith(domain + ".")
        and entity_id.split(".", 1)[1].endswith("_" + object_id)
    ]
    if len(matches) != 1:
        raise RuntimeError(f"Cannot uniquely resolve {expected}: {matches}")
    return matches[0]


async def resolve_device_entities(ha: "HAWebSocket", device: Device) -> Device:
    devices = await ha.command("config/device_registry/list")
    mac = device.mac.lower()
    matches = [
        item for item in devices
        if any(len(connection) > 1 and connection[0] == "mac" and connection[1].lower() == mac
               for connection in item.get("connections", []))
    ]
    if len(matches) != 1:
        raise RuntimeError(f"Cannot uniquely resolve {device.key} MAC {device.mac}: {matches}")
    device_id = matches[0]["id"]
    registry = await ha.command("config/entity_registry/list")
    entries = [item for item in registry if item.get("device_id") == device_id and item.get("platform") == "esphome"]

    def by_role(role: str) -> str:
        candidates = []
        for item in entries:
            entity_id = item.get("entity_id", "")
            unique_id = item.get("unique_id", "")
            if role == "snapshot" and unique_id.endswith("-text_sensor-runtime_snapshot"):
                candidates.append(entity_id)
            elif role == "voip" and unique_id.endswith("-text_sensor-voip_state"):
                candidates.append(entity_id)
            elif role == "assist" and unique_id.endswith("-assist_satellite"):
                candidates.append(entity_id)
            elif role == "media" and "-media_player-" in unique_id:
                candidates.append(entity_id)
            elif role == "light" and entity_id.startswith("light.") and not unique_id.endswith("-light-display_backlight"):
                candidates.append(entity_id)
        if len(candidates) != 1:
            raise RuntimeError(f"Cannot uniquely resolve {device.key}/{role} on {device_id}: {candidates}")
        return candidates[0]

    updates = {
        "snapshot_entity": by_role("snapshot"),
        "media_entity": by_role("media"),
        "voip_entity": by_role("voip"),
        "assist_entity": by_role("assist"),
    }
    if device.light_entity:
        updates["light_entity"] = by_role("light")
    return replace(device, **updates)


LED_EFFECTS = {
    "ws2812_ring": {
        LED["wake"]: "Spin",
        LED["listening"]: "Spin",
        LED["thinking"]: "Spin",
        LED["responding"]: "None",
        LED["media"]: "Spin",
        LED["voip_ringing"]: "Ringing",
        LED["voip_calling"]: "Calling",
        LED["voip_in_call"]: "None",
    },
    "spotpear_rgb": {
        LED["wake"]: "Slow Pulse",
        LED["listening"]: "Slow Pulse",
        LED["thinking"]: "Fast Pulse",
        LED["responding"]: "None",
        LED["media"]: "Slow Pulse",
        LED["voip_ringing"]: "Ringing",
        LED["voip_calling"]: "Calling",
        LED["voip_in_call"]: "None",
    },
}


@dataclass(frozen=True)
class Expected:
    ui: int
    led: int
    duck: int
    va: int


EXPECTED = {
    "idle": Expected(UI["idle"], LED["idle"], 0, 0),
    "media": Expected(UI["media"], LED["media"], 0, 0),
    "wake": Expected(UI["wake"], LED["wake"], 1, 0),
    "listening": Expected(UI["listening"], LED["listening"], 1, 1),
    "thinking": Expected(UI["thinking"], LED["thinking"], 1, 2),
    "responding": Expected(UI["responding"], LED["responding"], 1, 3),
    "in_call": Expected(UI["voip_in_call"], LED["voip_in_call"], 1, 0),
    "ringing": Expected(UI["voip_ringing"], LED["voip_ringing"], 1, 0),
    "calling": Expected(UI["voip_calling"], LED["voip_calling"], 1, 0),
    "mic_muted": Expected(UI["muted"], LED["mic_muted"], 0, 0),
    "speaker_muted": Expected(UI["muted"], LED["speaker_muted"], 0, 0),
    "both_muted": Expected(UI["muted"], LED["muted"], 0, 0),
    "timer": Expected(UI["media"], LED["timer_ringing"], 1, 0),
    "announcement": Expected(UI["media"], LED["media"], 1, 0),
    "no_wifi": Expected(UI["no_wifi"], LED["no_wifi"], 0, 0),
    "no_ha": Expected(UI["no_ha"], LED["no_ha"], 0, 0),
    "no_va": Expected(UI["no_va"], LED["no_va"], 0, 0),
}


class HAWebSocket:
    def __init__(self, url: str, token: str) -> None:
        self.url = url.rstrip("/") + "/api/websocket"
        self.token = token
        self.ws = None
        self.reader_task: asyncio.Task | None = None
        self.next_id = 1
        self.pending: dict[int, asyncio.Future] = {}
        self.states: dict[str, dict[str, Any]] = {}
        self.state_events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.transition_events: asyncio.Queue[dict[str, Any]] = asyncio.Queue()
        self.transition_seq: dict[str, int] = {}

    async def __aenter__(self) -> "HAWebSocket":
        self.ws = await connect(self.url, max_size=4 * 1024 * 1024)
        hello = json.loads(await self.ws.recv())
        if hello.get("type") != "auth_required":
            raise RuntimeError(f"Unexpected HA websocket greeting: {hello}")
        await self.ws.send(json.dumps({"type": "auth", "access_token": self.token}))
        auth = json.loads(await self.ws.recv())
        if auth.get("type") != "auth_ok":
            raise RuntimeError(f"Home Assistant authentication failed: {auth}")
        self.reader_task = asyncio.create_task(self._reader())
        states = await self.command("get_states")
        self.states = {item["entity_id"]: item for item in states}
        await self.command("subscribe_events", event_type="state_changed")
        await self.command("subscribe_events", event_type="esphome.runtime_transition")
        return self

    async def __aexit__(self, *_exc: object) -> None:
        if self.reader_task is not None:
            self.reader_task.cancel()
        if self.ws is not None:
            await self.ws.close()

    async def _reader(self) -> None:
        assert self.ws is not None
        async for raw in self.ws:
            message = json.loads(raw)
            if message.get("type") == "event":
                event = message.get("event", {})
                if event.get("event_type") == "esphome.runtime_transition":
                    data = event.get("data", {})
                    try:
                        payload = json.loads(data.get("snapshot", "{}"))
                        seq = int(payload.get("s", payload.get("seq", -1)))
                        node = str(data.get("node", ""))
                        if node and seq >= 0:
                            self.transition_seq[node] = max(seq, self.transition_seq.get(node, -1))
                    except (TypeError, ValueError, json.JSONDecodeError):
                        pass
                    await self.transition_events.put(data)
                    continue
                if event.get("event_type") != "state_changed":
                    continue
                data = event.get("data", {})
                entity_id = data.get("entity_id")
                new_state = data.get("new_state")
                if entity_id and new_state:
                    self.states[entity_id] = new_state
                    await self.state_events.put(data)
                continue
            message_id = message.get("id")
            future = self.pending.pop(message_id, None)
            if future is not None and not future.done():
                if message.get("success"):
                    future.set_result(message.get("result"))
                else:
                    future.set_exception(RuntimeError(str(message.get("error"))))

    async def command(self, command_type: str, **fields: Any) -> Any:
        assert self.ws is not None
        message_id = self.next_id
        self.next_id += 1
        future = asyncio.get_running_loop().create_future()
        self.pending[message_id] = future
        await self.ws.send(json.dumps({"id": message_id, "type": command_type, **fields}))
        return await asyncio.wait_for(future, 10)

    async def service(self, domain: str, service: str, data: dict[str, Any]) -> None:
        await self.command("call_service", domain=domain, service=service, service_data=data)


def parse_snapshot(state: dict[str, Any] | None) -> dict[str, Any] | None:
    if not state:
        return None
    raw = state.get("state", "")
    if raw in {"", "unknown", "unavailable"}:
        return None
    try:
        parsed = json.loads(raw)
        aliases = {
            "s": "seq", "a": "mask", "u": "ui", "l": "led",
            "d": "duck", "v": "va", "m": "media", "p": "voip",
            "w": "mww", "r": "ring", "g": "page", "h": "phase",
            "o": "mode", "x": "visual", "t": "text_ready",
        }
        for short, long_name in aliases.items():
            if short in parsed and long_name not in parsed:
                parsed[long_name] = parsed[short]
        if parsed.get("e") == "truncated":
            raise RuntimeError(f"Runtime snapshot was truncated: {raw}")
        return parsed
    except json.JSONDecodeError as err:
        raise RuntimeError(f"Invalid runtime snapshot {raw!r}: {err}") from err


async def wait_for_snapshot(
    ha: HAWebSocket,
    device: Device,
    previous_seq: int,
    timeout: float = 2.0,
) -> list[dict[str, Any]]:
    deadline = time.monotonic() + timeout
    snapshots: list[dict[str, Any]] = []
    last_seq = previous_seq
    last_snapshot: dict[str, Any] | None = None
    transition_started = False
    while time.monotonic() < deadline:
        remaining = deadline - time.monotonic()
        try:
            event = await asyncio.wait_for(ha.transition_events.get(), min(remaining, 0.20))
        except TimeoutError:
            if snapshots:
                break
            continue
        if event.get("node") != device.node_name:
            continue
        snapshot = parse_snapshot({"state": event.get("snapshot", "")})
        if snapshot:
            seq = int(snapshot.get("seq", -1))
            if seq > previous_seq:
                transition_started = True
            if transition_started and seq >= last_seq and snapshot != last_snapshot:
                if seq > last_seq + 1:
                    raise RuntimeError(
                        f"{device.key}: runtime sequence gap {last_seq}->{seq}; trace is incomplete"
                    )
                snapshots.append(snapshot)
                last_seq = seq
                last_snapshot = snapshot
    if not snapshots:
        current = parse_snapshot(ha.states.get(device.snapshot_entity))
        if current and int(current.get("seq", -1)) > previous_seq:
            snapshots.append(current)
    return snapshots


def validate_snapshot(snapshot: dict[str, Any], expected: Expected, label: str) -> list[str]:
    errors = []
    for key, value in (("ui", expected.ui), ("led", expected.led), ("duck", expected.duck), ("va", expected.va)):
        if snapshot.get(key) != value:
            errors.append(f"{label}: {key}={snapshot.get(key)!r}, expected {value!r}")
    return errors


def validate_light(ha: HAWebSocket, device: Device, expected_led: int, label: str) -> list[str]:
    if not device.light_entity or not device.led_preset:
        return []
    state = ha.states.get(device.light_entity)
    if not state:
        return [f"{label}: missing light entity {device.light_entity}"]
    if expected_led == LED["idle"]:
        return [] if state.get("state") == "off" else [f"{label}: LED is {state.get('state')}, expected off"]
    expected_effect = LED_EFFECTS.get(device.led_preset, {}).get(expected_led)
    actual_effect = state.get("attributes", {}).get("effect") or "None"
    if expected_effect is not None and actual_effect != expected_effect:
        return [f"{label}: effect={actual_effect!r}, expected {expected_effect!r}"]
    return []


async def inject_event(ha: HAWebSocket, device: Device, event: str) -> list[dict[str, Any]]:
    before = parse_snapshot(ha.states.get(device.snapshot_entity)) or {"seq": -1}
    before_seq = max(int(before["seq"]), ha.transition_seq.get(device.node_name, -1))
    await ha.service("esphome", f"{device.service_prefix}_runtime_event", {"event": event})
    return await wait_for_snapshot(ha, device, before_seq)


async def set_activity(ha: HAWebSocket, device: Device, activity: str, active: bool) -> list[dict[str, Any]]:
    before = parse_snapshot(ha.states.get(device.snapshot_entity)) or {"seq": -1}
    before_seq = max(int(before["seq"]), ha.transition_seq.get(device.node_name, -1))
    await ha.service(
        "esphome",
        f"{device.service_prefix}_runtime_set_activity",
        {"activity": activity, "active": active},
    )
    return await wait_for_snapshot(ha, device, before_seq)


async def baseline(ha: HAWebSocket, device: Device) -> dict[str, Any]:
    for activity in (
        "voip:ringing",
        "voip:calling",
        "voip:remote_ringing",
        "voip:connecting",
        "voip:in_call",
        "voip:terminating",
    ):
        await ha.service(
            "esphome",
            f"{device.service_prefix}_runtime_set_activity",
            {"activity": activity, "active": False},
        )
    for event in (
        "boot_ready",
        "wifi_connected",
        "ha_connected",
        "va_client_connected",
        "mic_unmuted",
        "speaker_unmuted",
        "timer_stopped",
        "media_idle",
        "va_idle",
    ):
        await ha.service("esphome", f"{device.service_prefix}_runtime_event", {"event": event})
    deadline = time.monotonic() + 2.0
    snapshot = parse_snapshot(ha.states.get(device.snapshot_entity))
    while (snapshot is None or snapshot.get("ui") != UI["idle"]) and time.monotonic() < deadline:
        try:
            event = await asyncio.wait_for(ha.state_events.get(), deadline - time.monotonic())
        except TimeoutError:
            break
        if event.get("entity_id") == device.snapshot_entity:
            snapshot = parse_snapshot(event.get("new_state"))
    if snapshot is None:
        raise RuntimeError(f"{device.key}: runtime snapshot is unavailable")
    if snapshot.get("ui") != UI["idle"]:
        raise RuntimeError(f"{device.key}: baseline did not converge to idle: {snapshot}")
    return snapshot


async def expect_event(
    ha: HAWebSocket,
    device: Device,
    event: str,
    expected_name: str,
    *,
    forbidden_ui: set[int] | None = None,
) -> list[str]:
    snapshots = await inject_event(ha, device, event)
    if not snapshots:
        return [f"{device.key}/{event}: no committed transition"]
    errors: list[str] = []
    if forbidden_ui:
        for snapshot in snapshots:
            if snapshot.get("ui") in forbidden_ui:
                errors.append(f"{device.key}/{event}: forbidden intermediate UI {snapshot.get('ui')} in {snapshot}")
    final = snapshots[-1]
    label = f"{device.key}/{event}"
    expected = EXPECTED[expected_name]
    errors.extend(validate_snapshot(final, expected, label))
    await asyncio.sleep(0.08)
    errors.extend(validate_light(ha, device, expected.led, label))
    print(f"{label:32} seq={final['seq']:>4} ui={final['ui']:>2} led={final['led']:>2} "
          f"duck={final['duck']} va={final['va']} media={final['media']:<10} voip={final['voip']}")
    return errors


async def expect_activity(
    ha: HAWebSocket,
    device: Device,
    activity: str,
    active: bool,
    expected_name: str,
) -> list[str]:
    snapshots = await set_activity(ha, device, activity, active)
    if not snapshots:
        return [f"{device.key}/{activity}={active}: no committed transition"]
    final = snapshots[-1]
    label = f"{device.key}/{activity}={active}"
    expected = EXPECTED[expected_name]
    errors = validate_snapshot(final, expected, label)
    await asyncio.sleep(0.08)
    errors.extend(validate_light(ha, device, expected.led, label))
    print(f"{label:32} seq={final['seq']:>4} ui={final['ui']:>2} led={final['led']:>2} "
          f"duck={final['duck']} va={final['va']} media={final['media']:<10} voip={final['voip']}")
    return errors


async def expect_timer_stop(ha: HAWebSocket, device: Device) -> list[str]:
    """Accept both valid callback timings, but require the same idle endpoint."""
    snapshots = await inject_event(ha, device, "timer_stopped")
    if not snapshots:
        return [f"{device.key}/timer_stopped: no committed transition"]
    final = snapshots[-1]
    if final.get("ui") == UI["idle"]:
        errors = validate_snapshot(final, EXPECTED["idle"], f"{device.key}/timer_stopped")
    else:
        errors = validate_snapshot(final, EXPECTED["announcement"], f"{device.key}/timer_stopped")
        errors += await expect_event(ha, device, "media_idle", "idle")
    print(f"{device.key + '/timer_stopped':32} seq={final['seq']:>4} ui={final['ui']:>2} "
          f"led={final['led']:>2} duck={final['duck']} media={final['media']}")
    return errors


async def run_matrix(ha: HAWebSocket, device: Device, force: bool) -> list[str]:
    for entity_id, accepted in (
        (device.voip_entity, {"idle", "unknown", "unavailable"}),
        (device.media_entity, {"idle", "paused", "off", "unknown", "unavailable"}),
        (device.assist_entity, {"idle", "unknown", "unavailable"}),
    ):
        state = ha.states.get(entity_id, {}).get("state", "missing")
        if not force and state not in accepted:
            return [f"{device.key}: refusing deterministic test while {entity_id}={state}; use --force to override"]

    errors: list[str] = []
    idle = await baseline(ha, device)
    errors.extend(validate_snapshot(idle, EXPECTED["idle"], f"{device.key}/baseline"))

    errors += await expect_event(ha, device, "media_playing", "media")
    errors += await expect_event(ha, device, "media_idle", "idle")

    errors += await expect_event(ha, device, "mic_muted", "mic_muted")
    errors += await expect_event(ha, device, "speaker_muted", "both_muted")
    errors += await expect_event(ha, device, "mic_unmuted", "speaker_muted")
    errors += await expect_event(ha, device, "speaker_unmuted", "idle")

    errors += await expect_event(ha, device, "timer_finished", "timer")
    errors += await expect_timer_stop(ha, device)

    errors += await expect_activity(ha, device, "voip:ringing", True, "ringing")
    errors += await expect_activity(ha, device, "voip:ringing", False, "idle")
    errors += await expect_activity(ha, device, "voip:calling", True, "calling")
    errors += await expect_activity(ha, device, "voip:calling", False, "idle")

    errors += await expect_event(ha, device, "va_client_disconnected", "no_va")
    errors += await expect_event(ha, device, "va_client_connected", "idle")
    errors += await expect_event(ha, device, "ha_disconnected", "no_ha")
    errors += await expect_event(ha, device, "ha_connected", "idle")
    errors += await expect_event(ha, device, "wifi_disconnected", "no_wifi")
    errors += await expect_event(ha, device, "wifi_connected", "idle")

    errors += await expect_event(ha, device, "va_start", "wake")
    errors += await expect_event(ha, device, "va_listening", "listening")
    errors += await expect_event(ha, device, "va_thinking", "thinking")
    errors += await expect_event(ha, device, "va_responding", "responding", forbidden_ui={UI["media"]})
    errors += await expect_event(ha, device, "announcement_started", "responding", forbidden_ui={UI["media"]})
    errors += await expect_event(ha, device, "va_end", "responding", forbidden_ui={UI["media"]})
    errors += await expect_event(ha, device, "media_idle", "idle", forbidden_ui={UI["media"]})
    await inject_event(ha, device, "va_idle")

    errors += await expect_activity(ha, device, "voip:in_call", True, "in_call")
    errors += await expect_event(ha, device, "va_start", "wake")
    errors += await expect_event(ha, device, "va_listening", "listening")
    errors += await expect_event(ha, device, "va_thinking", "thinking")
    errors += await expect_event(ha, device, "va_responding", "responding")
    errors += await expect_event(ha, device, "announcement_started", "responding")
    errors += await expect_event(ha, device, "va_end", "responding")
    # Critical contract: finishing TTS must reveal the active call and retain ducking.
    errors += await expect_event(ha, device, "media_idle", "in_call")
    await inject_event(ha, device, "va_idle")
    errors += await expect_activity(ha, device, "voip:in_call", False, "idle")

    await baseline(ha, device)
    return errors


async def cleanup_device(ha: HAWebSocket, device: Device) -> None:
    """Best-effort safety cleanup; must run even after a failed assertion."""
    for activity in (
        "voip:ringing", "voip:calling", "voip:remote_ringing",
        "voip:connecting", "voip:in_call", "voip:terminating",
    ):
        try:
            await ha.service(
                "esphome",
                f"{device.service_prefix}_runtime_set_activity",
                {"activity": activity, "active": False},
            )
        except Exception as err:  # cleanup must continue through unavailable devices
            print(f"{device.key}: cleanup {activity} failed: {err}", file=sys.stderr, flush=True)
    for event in ("timer_stopped", "media_idle", "va_idle"):
        try:
            await ha.service("esphome", f"{device.service_prefix}_runtime_event", {"event": event})
        except Exception as err:
            print(f"{device.key}: cleanup {event} failed: {err}", file=sys.stderr, flush=True)
    try:
        await ha.service("media_player", "media_stop", {"entity_id": device.media_entity})
    except Exception as err:
        print(f"{device.key}: cleanup media_stop failed: {err}", file=sys.stderr, flush=True)


async def run_permutation_matrix(ha: HAWebSocket, device: Device) -> list[str]:
    """Prove callback-order variants converge to one canonical endpoint."""
    errors: list[str] = []
    terminal_events = ("va_end", "media_idle", "va_idle")
    for in_call in (False, True):
        expected = EXPECTED["in_call" if in_call else "idle"]
        for order in permutations(terminal_events):
            await baseline(ha, device)
            if in_call:
                await set_activity(ha, device, "voip:in_call", True)
            await inject_event(ha, device, "va_responding")
            await inject_event(ha, device, "announcement_started")
            trace: list[dict[str, Any]] = []
            for event in order:
                trace.extend(await inject_event(ha, device, event))
            final = parse_snapshot(ha.states.get(device.snapshot_entity))
            label = f"{device.key}/perm/{'call' if in_call else 'idle'}/{'-'.join(order)}"
            if final is None:
                errors.append(f"{label}: missing final snapshot")
            else:
                errors.extend(validate_snapshot(final, expected, label))
            if in_call:
                await set_activity(ha, device, "voip:in_call", False)
            print(f"{label}: final ui={final.get('ui') if final else None} duck={final.get('duck') if final else None}")
    await baseline(ha, device)
    return errors


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ha-url", default="http://192.168.1.10:8123")
    parser.add_argument(
        "--token-file",
        type=Path,
        default=Path("/home/codex/.secrets/esphome-intercom/ha_token_codex"),
    )
    parser.add_argument("--device", choices=["all", *DEVICES], default="all")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--permutations", action="store_true")
    return parser.parse_args()


async def async_main() -> int:
    args = parse_args()
    token = args.token_file.read_text(encoding="utf-8").strip()
    ws_url = args.ha_url.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
    selected = DEVICES.values() if args.device == "all" else (DEVICES[args.device],)
    all_errors: list[str] = []
    async with HAWebSocket(ws_url, token) as ha:
        for configured_device in selected:
            device = await resolve_device_entities(ha, configured_device)
            print(f"\n== {device.key} ==", flush=True)
            try:
                errors = await run_matrix(ha, device, args.force)
                if args.permutations:
                    errors.extend(await run_permutation_matrix(ha, device))
                all_errors.extend(errors)
            finally:
                await cleanup_device(ha, device)
    if all_errors:
        print("\nFAILURES", file=sys.stderr)
        for error in all_errors:
            print(f"- {error}", file=sys.stderr)
        return 1
    print("\nAll deterministic runtime-state checks passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(async_main()))
