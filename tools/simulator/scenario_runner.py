#!/usr/bin/env python3
"""Run virtual-device scenarios against the simulator JSON-RPC socket."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import random
import sys
from typing import Any

try:
    import yaml
except Exception:  # pragma: no cover - dependency diagnosed at runtime.
    yaml = None

from simctl import DEFAULT_SOCKET, call


SCENARIO_DIR = Path("tests/simulator/scenarios")


def _load(path: Path) -> dict[str, Any]:
    text = path.read_text(encoding="utf-8")
    if path.suffix == ".json":
        data = json.loads(text)
    else:
        if yaml is None:
            raise RuntimeError("PyYAML is required for YAML scenarios")
        data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise RuntimeError(f"{path}: scenario root must be a mapping")
    steps = data.get("steps")
    if not isinstance(steps, list):
        raise RuntimeError(f"{path}: missing list field 'steps'")
    return data


def _resolve(name: str) -> list[Path]:
    if name == "all":
        return sorted(SCENARIO_DIR.glob("*.yaml")) + sorted(SCENARIO_DIR.glob("*.json"))
    path = SCENARIO_DIR / f"{name}.yaml"
    if path.exists():
        return [path]
    path = SCENARIO_DIR / f"{name}.json"
    if path.exists():
        return [path]
    direct = Path(name)
    if direct.exists():
        return [direct]
    raise RuntimeError(f"scenario not found: {name}")


def _expect(snapshot: dict[str, Any], expected: dict[str, Any], prefix: str = "") -> None:
    for key, value in expected.items():
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(value, dict):
            actual = snapshot.get(key)
            if not isinstance(actual, dict):
                raise AssertionError(f"{path}: expected mapping, got {actual!r}")
            _expect(actual, value, path)
        else:
            actual = snapshot.get(key)
            if actual != value:
                raise AssertionError(f"{path}: expected {value!r}, got {actual!r}")


def _run_step(socket_path: Path, step: dict[str, Any]) -> None:
    if "press_button" in step:
        call(socket_path, "press_button", button=step["press_button"])
    elif "touch" in step:
        call(socket_path, "touch", target=step["touch"])
    elif "advance_time" in step:
        call(socket_path, "advance_time", duration_ms=int(step["advance_time"]))
    elif "inject_fault" in step:
        call(socket_path, "inject_fault", name=step["inject_fault"])
    elif "inject_event" in step:
        event = step["inject_event"]
        if not isinstance(event, dict):
            raise RuntimeError("inject_event must be a mapping")
        call(socket_path, "inject_event", **event)
    elif "expect" in step:
        expected = step["expect"]
        if not isinstance(expected, dict):
            raise RuntimeError("expect must be a mapping")
        snapshot = call(socket_path, "get_snapshot")
        _expect(snapshot, expected)
    else:
        raise RuntimeError(f"unknown step: {step!r}")


def run_scenario(socket_path: Path, path: Path, *, repeat: int, seed: int | None) -> None:
    if seed is not None:
        random.seed(seed)
    scenario = _load(path)
    for iteration in range(repeat):
        call(socket_path, "reset")
        for raw_step in scenario["steps"]:
            if not isinstance(raw_step, dict):
                raise RuntimeError(f"{path}: step must be a mapping: {raw_step!r}")
            _run_step(socket_path, raw_step)
        print(f"ok {path.name} iteration={iteration + 1}")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("scenario", nargs="?", default="all")
    parser.add_argument("--socket", type=Path, default=DEFAULT_SOCKET)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--seed", type=int)
    args = parser.parse_args(argv)
    try:
        for path in _resolve(args.scenario):
            run_scenario(args.socket, path, repeat=args.repeat, seed=args.seed)
        return 0
    except Exception as err:
        print(f"scenario_runner: {err}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
