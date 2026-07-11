#!/usr/bin/env python3
"""Event-by-event validation of display content ordering during real HA TTS."""

from __future__ import annotations

import argparse
import asyncio
from contextlib import suppress

from runtime_state_qualification import (
    DEVICES,
    HAWebSocket,
    parse_snapshot,
    resolve_device_entities,
)


async def qualify_once(ha: HAWebSocket, key: str, run: int) -> list[str]:
    device = await resolve_device_entities(ha, DEVICES[key])
    message = f":-) Callback visual probe {run}"
    service_task = asyncio.create_task(
        ha.service(
            "assist_satellite",
            "announce",
            {"entity_id": device.assist_entity, "message": message},
        )
    )
    snapshots: list[dict] = []
    deadline = asyncio.get_running_loop().time() + 20
    saw_responding = False
    while asyncio.get_running_loop().time() < deadline:
        try:
            event = await asyncio.wait_for(ha.state_events.get(), 0.5)
        except TimeoutError:
            current = parse_snapshot(ha.states.get(device.snapshot_entity))
            if saw_responding and current and current.get("ui") == 0:
                break
            continue
        if event.get("entity_id") != device.snapshot_entity:
            continue
        snapshot = parse_snapshot(event.get("new_state"))
        if snapshot is None:
            continue
        snapshots.append(snapshot)
        if snapshot.get("ui") == 10:
            saw_responding = True
    with suppress(Exception):
        await service_task

    errors: list[str] = []
    responding = [item for item in snapshots if item.get("ui") == 10]
    if not responding:
        errors.append(f"{key}: no responding snapshot")
    for item in responding:
        if item.get("visual") != 2 or item.get("text_ready") != 1:
            errors.append(f"{key}/run-{run}: incomplete responding render: {item}")
    if key == "p4":
        for item in responding:
            if "replying" not in str(item.get("page")):
                errors.append(f"{key}/run-{run}: responding rendered a non-replying page: {item}")
    if key == "spotpear" and responding and not any(item.get("visual") == 2 for item in responding):
        errors.append(f"{key}/run-{run}: responding never rendered the happy visual")
    if not snapshots or snapshots[-1].get("ui") != 0:
        errors.append(f"{key}/run-{run}: did not return to idle: {snapshots[-1] if snapshots else None}")
    print(f"{key}/run-{run}: {len(snapshots)} callback snapshots", flush=True)
    for item in snapshots:
        print(
            f"  seq={item.get('seq')} ui={item.get('ui')} media={item.get('media')} "
            f"page={item.get('page')} visual={item.get('visual')} text={item.get('text_ready')}"
        )
    return errors


async def qualify(ha: HAWebSocket, key: str, repetitions: int) -> list[str]:
    errors: list[str] = []
    for run in range(1, repetitions + 1):
        errors.extend(await qualify_once(ha, key, run))
    return errors


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ha-url", default="http://192.168.1.10:8123")
    parser.add_argument("--token-file", default="/home/codex/.secrets/esphome-intercom/ha_token_codex")
    parser.add_argument("--device", choices=("p4", "spotpear", "all"), default="all")
    parser.add_argument("--repetitions", type=int, default=5)
    args = parser.parse_args()
    token = open(args.token_file, encoding="utf-8").read().strip()
    ws_url = args.ha_url.replace("https://", "wss://", 1).replace("http://", "ws://", 1)
    keys = ("p4", "spotpear") if args.device == "all" else (args.device,)
    errors: list[str] = []
    async with HAWebSocket(ws_url, token) as ha:
        for key in keys:
            errors.extend(await qualify(ha, key, args.repetitions))
    for error in errors:
        print(f"FAIL: {error}")
    return bool(errors)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
