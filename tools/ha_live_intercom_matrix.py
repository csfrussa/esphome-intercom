#!/usr/bin/env python3
"""Live HA/ESP intercom matrix checks.

This runner intentionally checks user-visible state, not only service return
codes. It samples HA entity states while calls are in progress so transient LED
effects such as an unexpected media "Spin" are caught.
"""

from __future__ import annotations

import argparse
import asyncio
from dataclasses import dataclass
from datetime import datetime, UTC
import json
from pathlib import Path
import ssl
import time
import urllib.error
import urllib.request
from typing import Any


DEFAULT_HA_URL = "https://f0260ef3d722.sn.mynetname.net"
DEFAULT_TOKEN_FILE = Path("/tmp/ha_token_codex")
OUT = Path("test_runs/ha_live_matrix")


@dataclass(frozen=True)
class DeviceSpec:
    key: str
    name: str
    device_id: str
    state_entity: str
    caller_entity: str
    destination_entity: str
    last_reason_entity: str
    led_entity: str
    auto_answer_switch: str
    dnd_switch: str
    ha_pbx_switch: str
    mic_mute_switch: str = ""
    speaker_mute_switch: str = ""


DEVICES = {
    "ws3": DeviceSpec(
        key="ws3",
        name="Waveshare S3 Audio",
        device_id="35bb14eb59bd920b964b61d0b0f1b8fc",
        state_entity="sensor.waveshare_s3_audio_intercom_state",
        caller_entity="sensor.waveshare_s3_audio_caller",
        destination_entity="sensor.waveshare_s3_audio_destination",
        last_reason_entity="sensor.waveshare_s3_audio_intercom_last_reason",
        led_entity="light.waveshare_s3_audio_status_led",
        auto_answer_switch="switch.waveshare_s3_audio_auto_answer",
        dnd_switch="switch.waveshare_s3_audio_do_not_disturb",
        ha_pbx_switch="switch.waveshare_s3_audio_ha_pbx_mode",
        mic_mute_switch="switch.waveshare_s3_audio_mic_mute",
        speaker_mute_switch="switch.waveshare_s3_audio_speaker_mute",
    ),
    "spotpear": DeviceSpec(
        key="spotpear",
        name="Spotpear Ball v2",
        device_id="df18a94e7c6ebcb84b183ac7c081805d",
        state_entity="sensor.intercom_xiaozhi_intercom_state",
        caller_entity="sensor.intercom_xiaozhi_caller",
        destination_entity="sensor.intercom_xiaozhi_destination",
        last_reason_entity="sensor.spotpear_ball_v2_udp_intercom_last_reason",
        led_entity="light.spotpear_ball_v2",
        auto_answer_switch="switch.intercom_xiaozhi_auto_answer",
        dnd_switch="switch.spotpear_ball_v2_do_not_disturb",
        ha_pbx_switch="switch.xiaozhi_udp_ha_pbx_mode",
    ),
}


class HaClient:
    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token
        self.ssl_context = ssl._create_unverified_context()

    def _request(self, method: str, path: str, data: dict[str, Any] | None = None) -> Any:
        raw = None
        headers = {"Authorization": f"Bearer {self.token}"}
        if data is not None:
            raw = json.dumps(data).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(f"{self.base_url}{path}", data=raw, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10, context=self.ssl_context) as resp:
                body = resp.read().decode("utf-8")
        except urllib.error.HTTPError as err:
            detail = err.read().decode("utf-8", errors="replace")
            raise AssertionError(f"HA {method} {path} failed: {err.code} {detail}") from err
        return json.loads(body) if body else None

    async def state(self, entity_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(self._request, "GET", f"/api/states/{entity_id}")

    async def service(self, domain: str, service: str, data: dict[str, Any] | None = None) -> Any:
        return await asyncio.to_thread(self._request, "POST", f"/api/services/{domain}/{service}", data or {})


class Sampler:
    def __init__(self, ha: HaClient, device: DeviceSpec, label: str, interval: float = 0.08) -> None:
        self.ha = ha
        self.device = device
        self.label = label
        self.interval = interval
        self.samples: list[dict[str, Any]] = []
        self._stop = asyncio.Event()
        self._task: asyncio.Task[None] | None = None

    async def __aenter__(self) -> "Sampler":
        self._task = asyncio.create_task(self._run())
        return self

    async def __aexit__(self, *_: object) -> None:
        self._stop.set()
        if self._task is not None:
            await self._task

    async def _run(self) -> None:
        while not self._stop.is_set():
            try:
                state, caller, dest, reason, led = await asyncio.gather(
                    self.ha.state(self.device.state_entity),
                    self.ha.state(self.device.caller_entity),
                    self.ha.state(self.device.destination_entity),
                    self.ha.state(self.device.last_reason_entity),
                    self.ha.state(self.device.led_entity),
                )
                self.samples.append(
                    {
                        "t": time.monotonic(),
                        "intercom_state": state.get("state"),
                        "caller": caller.get("state"),
                        "destination": dest.get("state"),
                        "last_reason": reason.get("state"),
                        "led_state": led.get("state"),
                        "led_effect": led.get("attributes", {}).get("effect"),
                        "led_rgb": led.get("attributes", {}).get("rgb_color"),
                    }
                )
            except Exception as err:  # noqa: BLE001 - keep sampling diagnostics.
                self.samples.append({"t": time.monotonic(), "error": str(err)})
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=self.interval)
            except asyncio.TimeoutError:
                pass

    def assert_no_spin(self, reason: str) -> None:
        spin = [
            sample
            for sample in self.samples
            if str(sample.get("led_effect") or "").lower() == "spin"
        ]
        if spin:
            raise AssertionError(f"{self.label}: unexpected LED Spin during {reason}: {spin[:5]}")

    def write(self) -> None:
        OUT.mkdir(parents=True, exist_ok=True)
        path = OUT / f"{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}_{self.device.key}_{self.label}.json"
        path.write_text(json.dumps(self.samples, indent=2, ensure_ascii=False), encoding="utf-8")


async def wait_state(
    ha: HaClient,
    entity_id: str,
    wanted: set[str],
    *,
    timeout: float = 8.0,
    label: str = "",
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        last = await ha.state(entity_id)
        if str(last.get("state")) in wanted:
            return last
        await asyncio.sleep(0.1)
    raise AssertionError(f"{label or entity_id}: expected {sorted(wanted)}, last={last}")


async def wait_text(
    ha: HaClient,
    entity_id: str,
    wanted: set[str],
    *,
    timeout: float = 8.0,
    label: str = "",
) -> dict[str, Any]:
    normalized = {value.lower() for value in wanted}
    deadline = time.monotonic() + timeout
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        last = await ha.state(entity_id)
        value = str(last.get("state") or "").strip().lower()
        if value in normalized:
            return last
        await asyncio.sleep(0.1)
    raise AssertionError(f"{label or entity_id}: expected {sorted(wanted)}, last={last}")


async def switch_state(ha: HaClient, entity_id: str) -> str:
    return str((await ha.state(entity_id)).get("state"))


async def set_switch(ha: HaClient, entity_id: str, on: bool) -> None:
    await ha.service("switch", "turn_on" if on else "turn_off", {"entity_id": entity_id})


async def restore_switch(ha: HaClient, entity_id: str, original: str) -> None:
    if original == "on":
        await set_switch(ha, entity_id, True)
        await wait_text(ha, entity_id, {"on"}, timeout=4, label=f"{entity_id} restore on")
    elif original == "off":
        await set_switch(ha, entity_id, False)
        await wait_text(ha, entity_id, {"off"}, timeout=4, label=f"{entity_id} restore off")


async def force_switch(ha: HaClient, entity_id: str, on: bool) -> None:
    await set_switch(ha, entity_id, on)
    await wait_text(ha, entity_id, {"on" if on else "off"}, timeout=4, label=f"{entity_id} force")


async def ensure_idle(ha: HaClient, device: DeviceSpec) -> None:
    state = str((await ha.state(device.state_entity)).get("state"))
    if state != "Idle":
        await ha.service("intercom_native", "sip_hangup", {})
        await ha.service("intercom_native", "hangup", {"device_id": device.device_id})
        await wait_state(ha, device.state_entity, {"Idle"}, timeout=8, label=f"{device.key} cleanup")


async def cleanup_all(ha: HaClient, *devices: DeviceSpec) -> None:
    await ha.service("intercom_native", "sip_hangup", {})
    for device in devices:
        await ha.service("intercom_native", "hangup", {"device_id": device.device_id})
        await ha.service("intercom_native", "decline", {"device_id": device.device_id})
    await asyncio.sleep(0.5)
    await asyncio.gather(*(ensure_idle(ha, device) for device in devices))


def assert_led_red(samples: list[dict[str, Any]], label: str) -> None:
    for sample in samples:
        if sample.get("intercom_state") not in {"Ringing", "Incoming"}:
            continue
        rgb = sample.get("led_rgb") or []
        effect = sample.get("led_effect")
        if effect == "Ringing" and len(rgb) == 3 and rgb[0] >= 180 and rgb[1] <= 80 and rgb[2] <= 80:
            return
    raise AssertionError(f"{label}: no red Ringing LED sample")


def assert_led_green_fixed(samples: list[dict[str, Any]], label: str) -> None:
    for sample in samples:
        if sample.get("intercom_state") != "Streaming":
            continue
        rgb = sample.get("led_rgb") or []
        effect = sample.get("led_effect")
        if len(rgb) == 3 and rgb[1] >= 150 and rgb[0] <= 160 and rgb[2] <= 180 and effect in (None, "None", ""):
            return
    raise AssertionError(f"{label}: no fixed green Streaming LED sample")


async def test_phonebook_json(ha: HaClient) -> None:
    state = await ha.state("sensor.intercom_phonebook")
    raw = state.get("attributes", {}).get("roster_json")
    if not raw:
        raise AssertionError("sensor.intercom_phonebook has no roster_json")
    payload = json.loads(raw)
    contacts = payload.get("contacts")
    if not isinstance(contacts, list) or len(contacts) < 2:
        raise AssertionError(f"invalid roster_json contacts: {payload}")
    ids = [str(item.get("id") or "") for item in contacts]
    if len(ids) != len(set(ids)):
        raise AssertionError(f"duplicate roster ids: {ids}")
    if "Casa" not in ids:
        raise AssertionError(f"HA identity Casa missing from roster: {ids}")


async def ha_to_esp_cancel(ha: HaClient, device: DeviceSpec) -> None:
    await ensure_idle(ha, device)
    aa = await switch_state(ha, device.auto_answer_switch)
    try:
        await set_switch(ha, device.auto_answer_switch, False)
        async with Sampler(ha, device, "ha_to_esp_cancel") as sampler:
            await ha.service("intercom_native", "call", {"device_id": device.device_id})
            await wait_state(ha, device.state_entity, {"Ringing", "Incoming"}, label=f"{device.key} ringing")
            caller = await ha.state(device.caller_entity)
            if str(caller.get("state")) != "Casa":
                raise AssertionError(f"{device.key}: caller should be Casa, got {caller}")
            await asyncio.sleep(0.5)
            await ha.service("intercom_native", "sip_hangup", {})
            await wait_state(ha, device.state_entity, {"Idle"}, label=f"{device.key} cancel idle")
            await asyncio.sleep(0.5)
        sampler.write()
        assert_led_red(sampler.samples, f"{device.key} cancel")
        sampler.assert_no_spin("cancel/ringing teardown")
    finally:
        await restore_switch(ha, device.auto_answer_switch, aa)


async def ha_to_esp_answer_hangup(ha: HaClient, device: DeviceSpec) -> None:
    await ensure_idle(ha, device)
    aa = await switch_state(ha, device.auto_answer_switch)
    try:
        await set_switch(ha, device.auto_answer_switch, False)
        async with Sampler(ha, device, "ha_to_esp_answer_hangup") as sampler:
            await ha.service("intercom_native", "call", {"device_id": device.device_id})
            await wait_state(ha, device.state_entity, {"Ringing", "Incoming"}, label=f"{device.key} ringing")
            await ha.service("intercom_native", "answer", {"device_id": device.device_id})
            await wait_state(ha, device.state_entity, {"Streaming"}, timeout=10, label=f"{device.key} streaming")
            await asyncio.sleep(0.7)
            await ha.service("intercom_native", "sip_hangup", {})
            await wait_state(ha, device.state_entity, {"Idle"}, timeout=10, label=f"{device.key} idle after bye")
            await asyncio.sleep(0.5)
        sampler.write()
        assert_led_red(sampler.samples, f"{device.key} answer")
        assert_led_green_fixed(sampler.samples, f"{device.key} answer")
        sampler.assert_no_spin("answer/hangup")
    finally:
        await restore_switch(ha, device.auto_answer_switch, aa)


async def ha_to_esp_auto_answer(ha: HaClient, device: DeviceSpec) -> None:
    await ensure_idle(ha, device)
    aa = await switch_state(ha, device.auto_answer_switch)
    try:
        await set_switch(ha, device.auto_answer_switch, True)
        async with Sampler(ha, device, "ha_to_esp_auto_answer") as sampler:
            await ha.service("intercom_native", "call", {"device_id": device.device_id})
            await wait_state(ha, device.state_entity, {"Streaming"}, timeout=10, label=f"{device.key} auto streaming")
            await asyncio.sleep(0.7)
            await ha.service("intercom_native", "sip_hangup", {})
            await wait_state(ha, device.state_entity, {"Idle"}, timeout=10, label=f"{device.key} auto idle")
            await asyncio.sleep(0.5)
        sampler.write()
        assert_led_green_fixed(sampler.samples, f"{device.key} auto")
        sampler.assert_no_spin("auto-answer")
    finally:
        await restore_switch(ha, device.auto_answer_switch, aa)


async def ha_to_esp_dnd_decline(ha: HaClient, device: DeviceSpec) -> None:
    await ensure_idle(ha, device)
    aa = await switch_state(ha, device.auto_answer_switch)
    dnd = await switch_state(ha, device.dnd_switch)
    try:
        await set_switch(ha, device.auto_answer_switch, False)
        await set_switch(ha, device.dnd_switch, True)
        async with Sampler(ha, device, "ha_to_esp_dnd_decline") as sampler:
            await ha.service("intercom_native", "call", {"device_id": device.device_id})
            await wait_state(ha, device.state_entity, {"Idle"}, timeout=10, label=f"{device.key} dnd idle")
            await asyncio.sleep(0.4)
        sampler.write()
        sampler.assert_no_spin("dnd decline")
    finally:
        await restore_switch(ha, device.dnd_switch, dnd)
        await restore_switch(ha, device.auto_answer_switch, aa)


async def ha_to_esp_muted_ringing(ha: HaClient, device: DeviceSpec) -> None:
    if not device.mic_mute_switch or not device.speaker_mute_switch:
        return
    await ensure_idle(ha, device)
    aa = await switch_state(ha, device.auto_answer_switch)
    mic = await switch_state(ha, device.mic_mute_switch)
    speaker = await switch_state(ha, device.speaker_mute_switch)
    try:
        await set_switch(ha, device.auto_answer_switch, False)
        await set_switch(ha, device.mic_mute_switch, True)
        await set_switch(ha, device.speaker_mute_switch, True)
        async with Sampler(ha, device, "ha_to_esp_muted_ringing") as sampler:
            await ha.service("intercom_native", "call", {"device_id": device.device_id})
            await wait_state(ha, device.state_entity, {"Ringing", "Incoming"}, label=f"{device.key} muted ringing")
            await asyncio.sleep(0.6)
            await ha.service("intercom_native", "sip_hangup", {})
            await wait_state(ha, device.state_entity, {"Idle"}, timeout=10, label=f"{device.key} muted idle")
            await asyncio.sleep(0.4)
        sampler.write()
        assert_led_red(sampler.samples, f"{device.key} muted ringing")
        sampler.assert_no_spin("muted ringing")
    finally:
        await restore_switch(ha, device.speaker_mute_switch, speaker)
        await restore_switch(ha, device.mic_mute_switch, mic)
        await restore_switch(ha, device.auto_answer_switch, aa)


async def esp_to_ha_decline(ha: HaClient, source: DeviceSpec) -> None:
    await ensure_idle(ha, source)
    async with Sampler(ha, source, "esp_to_ha_decline") as sampler:
        await ha.service(
            "intercom_native",
            "call",
            {"source": source.device_id, "destination": "Casa"},
        )
        await wait_state(ha, source.state_entity, {"Outgoing", "Calling"}, timeout=8, label=f"{source.key} outgoing HA")
        await asyncio.sleep(0.5)
        await ha.service("intercom_native", "sip_decline", {"status": 486, "reason": "Test Decline"})
        await wait_state(ha, source.state_entity, {"Idle"}, timeout=10, label=f"{source.key} ESP->HA idle")
        await asyncio.sleep(0.4)
    sampler.write()
    sampler.assert_no_spin("ESP->HA decline")


async def esp_to_ha_busy_while_ha_ringing(
    ha: HaClient,
    first: DeviceSpec,
    second: DeviceSpec,
) -> None:
    await cleanup_all(ha, first, second)
    label = "esp_to_ha_busy_while_ha_ringing"
    async with Sampler(ha, first, f"{label}_first") as first_sampler:
        async with Sampler(ha, second, f"{label}_second") as second_sampler:
            await ha.service("intercom_native", "call", {"source": first.device_id, "destination": "Casa"})
            await wait_state(ha, first.state_entity, {"Outgoing", "Calling"}, timeout=8, label=f"{first.key} first HA ringing")
            await asyncio.sleep(0.5)
            await ha.service("intercom_native", "call", {"source": second.device_id, "destination": "Casa"})
            await wait_state(ha, second.state_entity, {"Idle"}, timeout=10, label=f"{second.key} HA busy idle")
            await wait_text(ha, second.last_reason_entity, {"busy"}, timeout=4, label=f"{second.key} HA busy reason")
            still = await ha.state(first.state_entity)
            if str(still.get("state")) not in {"Outgoing", "Calling"}:
                raise AssertionError(f"{label}: first caller should remain outgoing, got {still}")
            await ha.service(
                "intercom_native",
                "sip_decline",
                {"reason": "Busy Here", "decline_reason": "declined"},
            )
            await wait_state(ha, first.state_entity, {"Idle"}, timeout=10, label=f"{first.key} first cleanup")
            await wait_text(ha, first.last_reason_entity, {"declined"}, timeout=4, label=f"{first.key} first declined")
    first_sampler.write()
    second_sampler.write()
    first_sampler.assert_no_spin(label)
    second_sampler.assert_no_spin(label)


async def esp_to_ha_busy_while_ha_streaming(
    ha: HaClient,
    first: DeviceSpec,
    second: DeviceSpec,
) -> None:
    await cleanup_all(ha, first, second)
    label = "esp_to_ha_busy_while_ha_streaming"
    async with Sampler(ha, first, f"{label}_first") as first_sampler:
        async with Sampler(ha, second, f"{label}_second") as second_sampler:
            await ha.service("intercom_native", "call", {"source": first.device_id, "destination": "Casa"})
            await wait_state(ha, first.state_entity, {"Outgoing", "Calling"}, timeout=8, label=f"{first.key} first outgoing")
            await ha.service("intercom_native", "sip_answer", {})
            await wait_state(ha, first.state_entity, {"Streaming"}, timeout=12, label=f"{first.key} HA streaming")
            await asyncio.sleep(0.6)
            await ha.service("intercom_native", "call", {"source": second.device_id, "destination": "Casa"})
            await wait_state(ha, second.state_entity, {"Idle"}, timeout=10, label=f"{second.key} HA streaming busy idle")
            await wait_text(ha, second.last_reason_entity, {"busy"}, timeout=4, label=f"{second.key} HA streaming busy reason")
            still = await ha.state(first.state_entity)
            if str(still.get("state")) != "Streaming":
                raise AssertionError(f"{label}: first caller should remain streaming, got {still}")
            await ha.service("intercom_native", "sip_hangup", {})
            await wait_state(ha, first.state_entity, {"Idle"}, timeout=12, label=f"{first.key} streaming cleanup")
    first_sampler.write()
    second_sampler.write()
    assert_led_green_fixed(first_sampler.samples, label)
    first_sampler.assert_no_spin(label)
    second_sampler.assert_no_spin(label)


async def esp_busy_while_ringing(
    ha: HaClient,
    busy_dest: DeviceSpec,
    second: DeviceSpec,
    *,
    via_ha: bool,
) -> None:
    await cleanup_all(ha, busy_dest, second)
    aa = await switch_state(ha, busy_dest.auto_answer_switch)
    second_pbx = await switch_state(ha, second.ha_pbx_switch)
    label = f"esp_busy_while_ringing_{'via_ha' if via_ha else 'direct'}"
    try:
        await set_switch(ha, busy_dest.auto_answer_switch, False)
        await set_switch(ha, second.ha_pbx_switch, via_ha)
        async with Sampler(ha, busy_dest, f"{label}_dest") as dest_sampler:
            async with Sampler(ha, second, f"{label}_second") as second_sampler:
                await ha.service("intercom_native", "call", {"device_id": busy_dest.device_id})
                await wait_state(ha, busy_dest.state_entity, {"Ringing", "Incoming"}, timeout=8, label=f"{busy_dest.key} HA incoming")
                await asyncio.sleep(0.5)
                await ha.service(
                    "intercom_native",
                    "call",
                    {"source": second.device_id, "destination": busy_dest.name},
                )
                await wait_state(ha, second.state_entity, {"Idle"}, timeout=10, label=f"{second.key} ESP ringing busy idle")
                await wait_text(ha, second.last_reason_entity, {"busy"}, timeout=4, label=f"{second.key} ESP ringing busy reason")
                still = await ha.state(busy_dest.state_entity)
                if str(still.get("state")) not in {"Ringing", "Incoming"}:
                    raise AssertionError(f"{label}: busy destination should remain ringing, got {still}")
                await ha.service("intercom_native", "sip_hangup", {})
                await wait_state(ha, busy_dest.state_entity, {"Idle"}, timeout=10, label=f"{busy_dest.key} ringing cleanup")
        dest_sampler.write()
        second_sampler.write()
        assert_led_red(dest_sampler.samples, label)
        dest_sampler.assert_no_spin(label)
        second_sampler.assert_no_spin(label)
    finally:
        await restore_switch(ha, second.ha_pbx_switch, second_pbx)
        await restore_switch(ha, busy_dest.auto_answer_switch, aa)


async def esp_busy_while_streaming(
    ha: HaClient,
    busy_dest: DeviceSpec,
    second: DeviceSpec,
    *,
    via_ha: bool,
) -> None:
    await cleanup_all(ha, busy_dest, second)
    aa = await switch_state(ha, busy_dest.auto_answer_switch)
    second_pbx = await switch_state(ha, second.ha_pbx_switch)
    label = f"esp_busy_while_streaming_{'via_ha' if via_ha else 'direct'}"
    try:
        await set_switch(ha, busy_dest.auto_answer_switch, False)
        await set_switch(ha, second.ha_pbx_switch, via_ha)
        async with Sampler(ha, busy_dest, f"{label}_dest") as dest_sampler:
            async with Sampler(ha, second, f"{label}_second") as second_sampler:
                await ha.service("intercom_native", "call", {"device_id": busy_dest.device_id})
                await wait_state(ha, busy_dest.state_entity, {"Ringing", "Incoming"}, timeout=8, label=f"{busy_dest.key} HA incoming")
                await ha.service("intercom_native", "answer", {"device_id": busy_dest.device_id})
                await wait_state(ha, busy_dest.state_entity, {"Streaming"}, timeout=12, label=f"{busy_dest.key} HA streaming")
                await asyncio.sleep(0.6)
                await ha.service(
                    "intercom_native",
                    "call",
                    {"source": second.device_id, "destination": busy_dest.name},
                )
                await wait_state(ha, second.state_entity, {"Idle"}, timeout=10, label=f"{second.key} ESP streaming busy idle")
                await wait_text(ha, second.last_reason_entity, {"busy"}, timeout=4, label=f"{second.key} ESP streaming busy reason")
                still = await ha.state(busy_dest.state_entity)
                if str(still.get("state")) != "Streaming":
                    raise AssertionError(f"{label}: busy destination should remain streaming, got {still}")
                await ha.service("intercom_native", "sip_hangup", {})
                await wait_state(ha, busy_dest.state_entity, {"Idle"}, timeout=12, label=f"{busy_dest.key} streaming cleanup")
        dest_sampler.write()
        second_sampler.write()
        assert_led_green_fixed(dest_sampler.samples, label)
        dest_sampler.assert_no_spin(label)
        second_sampler.assert_no_spin(label)
    finally:
        await restore_switch(ha, second.ha_pbx_switch, second_pbx)
        await restore_switch(ha, busy_dest.auto_answer_switch, aa)


async def esp_to_esp_decline(
    ha: HaClient,
    source: DeviceSpec,
    dest: DeviceSpec,
    *,
    via_ha: bool,
) -> None:
    await ensure_idle(ha, source)
    await ensure_idle(ha, dest)
    source_pbx = await switch_state(ha, source.ha_pbx_switch)
    dest_aa = await switch_state(ha, dest.auto_answer_switch)
    dest_dnd = await switch_state(ha, dest.dnd_switch)
    label = "esp_to_esp_via_ha_decline" if via_ha else "esp_to_esp_direct_decline"
    try:
        await force_switch(ha, source.ha_pbx_switch, via_ha)
        await force_switch(ha, dest.auto_answer_switch, False)
        await force_switch(ha, dest.dnd_switch, False)
        async with Sampler(ha, source, f"{label}_source") as source_sampler:
            async with Sampler(ha, dest, f"{label}_dest") as dest_sampler:
                await ha.service(
                    "intercom_native",
                    "call",
                    {"source": source.device_id, "destination": dest.name},
                )
                await wait_state(
                    ha,
                    source.state_entity,
                    {"Outgoing", "Calling"},
                    timeout=8,
                    label=f"{source.key} {label} outgoing",
                )
                await wait_state(
                    ha,
                    dest.state_entity,
                    {"Ringing", "Incoming"},
                    timeout=10,
                    label=f"{dest.key} {label} ringing",
                )
                caller = await ha.state(dest.caller_entity)
                if str(caller.get("state")) != source.name:
                    raise AssertionError(f"{label}: {dest.key} caller should be {source.name}, got {caller}")
                await asyncio.sleep(0.5)
                await ha.service("intercom_native", "decline", {"device_id": dest.device_id, "reason": "Test Decline"})
                await wait_state(ha, dest.state_entity, {"Idle"}, timeout=10, label=f"{dest.key} {label} idle")
                await wait_state(ha, source.state_entity, {"Idle"}, timeout=10, label=f"{source.key} {label} idle")
                await asyncio.sleep(0.4)
        source_sampler.write()
        dest_sampler.write()
        assert_led_red(dest_sampler.samples, f"{label} {dest.key}")
        source_sampler.assert_no_spin(label)
        dest_sampler.assert_no_spin(label)
    finally:
        await restore_switch(ha, dest.dnd_switch, dest_dnd)
        await restore_switch(ha, dest.auto_answer_switch, dest_aa)
        await restore_switch(ha, source.ha_pbx_switch, source_pbx)


async def run(args: argparse.Namespace) -> int:
    token = args.token or DEFAULT_TOKEN_FILE.read_text(encoding="utf-8").strip()
    ha = HaClient(args.ha_url, token)
    await test_phonebook_json(ha)
    selected = [DEVICES[key] for key in args.device]
    for device in selected:
        await ha_to_esp_cancel(ha, device)
        await ha_to_esp_answer_hangup(ha, device)
        if args.full:
            await ha_to_esp_auto_answer(ha, device)
            await ha_to_esp_dnd_decline(ha, device)
            if args.mute_tests:
                await ha_to_esp_muted_ringing(ha, device)
            await esp_to_ha_decline(ha, device)
    if args.full and len(selected) >= 2:
        await esp_to_esp_decline(ha, selected[0], selected[1], via_ha=False)
        await esp_to_esp_decline(ha, selected[0], selected[1], via_ha=True)
        await esp_to_ha_busy_while_ha_ringing(ha, selected[0], selected[1])
        await esp_to_ha_busy_while_ha_streaming(ha, selected[0], selected[1])
        await esp_busy_while_ringing(ha, selected[0], selected[1], via_ha=False)
        await esp_busy_while_ringing(ha, selected[0], selected[1], via_ha=True)
        await esp_busy_while_streaming(ha, selected[0], selected[1], via_ha=False)
        await esp_busy_while_streaming(ha, selected[0], selected[1], via_ha=True)
        await esp_to_ha_busy_while_ha_ringing(ha, selected[1], selected[0])
        await esp_to_ha_busy_while_ha_streaming(ha, selected[1], selected[0])
        await esp_busy_while_ringing(ha, selected[1], selected[0], via_ha=False)
        await esp_busy_while_ringing(ha, selected[1], selected[0], via_ha=True)
        await esp_busy_while_streaming(ha, selected[1], selected[0], via_ha=False)
        await esp_busy_while_streaming(ha, selected[1], selected[0], via_ha=True)
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ha-url", default=DEFAULT_HA_URL)
    parser.add_argument("--token")
    parser.add_argument("--device", choices=sorted(DEVICES), action="append", default=[])
    parser.add_argument("--full", action="store_true", help="Include DND and auto-answer cases.")
    parser.add_argument("--mute-tests", action="store_true", help="Also toggle ESP mic/speaker mute switches.")
    args = parser.parse_args()
    if not args.device:
        args.device = ["ws3"]
    return args


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run(parse_args())))
