#!/usr/bin/env python3
"""Summarize GDB thread snapshots captured by tools/jtag_snapshots.py."""

from __future__ import annotations

import argparse
import re
from collections import Counter, defaultdict
from pathlib import Path


SNAPSHOT_RE = re.compile(r"^===== JTAG SNAPSHOT (\d+) =====$")
THREAD_RE = re.compile(r'^Thread \d+ \(Thread \d+ "([^"]+)"')
RUNNING_RE = re.compile(r'Thread \d+ "([^"]+)".*State: Running @CPU(\d)')
TASK_STATUS = {
    "ready",
    "blocked",
    "suspended",
    "deleted",
    "running",
    "delayed",
    "delayed_1",
    "delayed_2",
}


def _category(block: str) -> str:
    if "esp_aec3" in block or "afe_feed" in block or "EspAfe::process" in block:
        return "afe/aec running"
    if "ESPAudioStack::audio_session_" in block or "process_aec_and_callbacks_" in block:
        return "audio stack"
    if "VoipStack::tx_task_" in block:
        return "voip tx"
    if "VoipStack::rx_task_" in block:
        return "voip rx"
    if "SipTransport::sip_task_" in block:
        return "sip select"
    if "tcpip_thread" in block:
        return "lwip tcpip"
    if "httpd_server" in block:
        return "http server"
    if "sendspin::" in block:
        return "sendspin"
    if "MicroWakeWord::inference_task" in block:
        return "mww"
    if "EspAfe::direct_fetch_task_loop_" in block:
        return "afe fetch"
    if "Application::loop" in block or "loop_task" in block:
        return "main loop"
    if "esp_event_loop_run" in block:
        return "esp event"
    if "timer_task" in block:
        return "esp timer"
    if "prvIdleTask" in block or "esp_cpu_wait_for_intr" in block:
        return "idle"
    if "xQueueReceive" in block:
        return "queue wait"
    if "ulTaskGenericNotifyTake" in block:
        return "notify wait"
    if "xQueueSemaphoreTake" in block:
        return "semaphore wait"
    return "other"


def _split_snapshots(text: str) -> list[tuple[int, str]]:
    snapshots: list[tuple[int, str]] = []
    current_id: int | None = None
    current: list[str] = []
    for line in text.splitlines():
        match = SNAPSHOT_RE.match(line)
        if match:
            if current_id is not None:
                snapshots.append((current_id, "\n".join(current)))
            current_id = int(match.group(1))
            current = []
            continue
        if current_id is not None:
            current.append(line)
    if current_id is not None:
        snapshots.append((current_id, "\n".join(current)))
    return snapshots


def _thread_blocks(snapshot: str) -> list[tuple[str, str]]:
    blocks: list[tuple[str, str]] = []
    current_name: str | None = None
    current: list[str] = []
    for line in snapshot.splitlines():
        match = THREAD_RE.match(line)
        if match:
            if current_name is not None:
                blocks.append((current_name, "\n".join(current)))
            current_name = match.group(1)
            current = [line]
            continue
        if current_name is not None:
            current.append(line)
    if current_name is not None:
        blocks.append((current_name, "\n".join(current)))
    return blocks


def _freertos_tasks(snapshot: str) -> list[dict[str, str | int]]:
    tasks: list[dict[str, str | int]] = []
    in_table = False
    for line in snapshot.splitlines():
        if line.startswith(" CPU") and " NAME " in line and " STATUS " in line:
            in_table = True
            continue
        if not in_table:
            continue
        if not line.strip() or line.startswith("----"):
            continue
        if line.startswith("Thread ") or line.startswith("====="):
            break

        parts = line.split()
        status_index = next((i for i, part in enumerate(parts) if part in TASK_STATUS), None)
        if status_index is None or status_index < 1 or len(parts) < status_index + 6:
            continue
        try:
            tasks.append(
                {
                    "name": parts[status_index - 1],
                    "status": parts[status_index],
                    "affinity": parts[status_index + 1],
                    "priority": int(parts[status_index + 2]),
                    "base_priority": int(parts[status_index + 3]),
                    "mutexes": int(parts[status_index + 4]),
                    "stack_used": int(parts[status_index + 5]),
                    "stack_free": int(parts[status_index + 6]),
                    "running_cpu": parts[0] if parts and parts[0].startswith("CPU") else "-",
                }
            )
        except (IndexError, ValueError):
            continue
    return tasks


def summarize(path: Path) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    snapshots = _split_snapshots(text)
    lines: list[str] = [f"file: {path}", f"snapshots: {len(snapshots)}", ""]

    total = Counter()
    running_total = Counter()
    per_task = defaultdict(Counter)
    task_status = defaultdict(Counter)
    task_running = defaultdict(Counter)
    task_stack_min: dict[str, int] = {}
    task_stack_max_used: dict[str, int] = {}
    task_priorities: dict[str, int] = {}

    for snapshot_id, body in snapshots:
        blocks = _thread_blocks(body)
        categories = Counter()
        running = Counter(RUNNING_RE.findall(body))
        for name, block in blocks:
            category = _category(block)
            categories[category] += 1
            total[category] += 1
            per_task[name][category] += 1
        for task_name, cpu in running:
            running_total[f"{task_name}@CPU{cpu}"] += 1
        for task in _freertos_tasks(body):
            name = str(task["name"])
            task_status[name][str(task["status"])] += 1
            if str(task["running_cpu"]).startswith("CPU"):
                task_running[name][str(task["running_cpu"])] += 1
            stack_free = int(task["stack_free"])
            stack_used = int(task["stack_used"])
            task_stack_min[name] = min(task_stack_min.get(name, stack_free), stack_free)
            task_stack_max_used[name] = max(task_stack_max_used.get(name, stack_used), stack_used)
            task_priorities[name] = int(task["priority"])
        category_text = ", ".join(f"{key}={value}" for key, value in sorted(categories.items()))
        running_text = ", ".join(f"{name}@CPU{cpu}" for name, cpu in running) or "none"
        lines.append(f"snapshot {snapshot_id:03d}: running={running_text}; {category_text}")

    lines.extend(["", "category totals:"])
    for category, count in total.most_common():
        lines.append(f"  {category}: {count}")

    lines.extend(["", "running samples:"])
    for name, count in running_total.most_common():
        lines.append(f"  {name}: {count}")

    lines.extend(["", "task categories:"])
    for task_name in sorted(per_task):
        category_text = ", ".join(f"{key}={value}" for key, value in per_task[task_name].most_common())
        lines.append(f"  {task_name}: {category_text}")

    if task_status:
        lines.extend(["", "FreeRTOS task table:"])
        for task_name in sorted(task_status, key=lambda name: (-task_priorities.get(name, -1), name)):
            status_text = ", ".join(f"{key}={value}" for key, value in task_status[task_name].most_common())
            running_text = ", ".join(f"{key}={value}" for key, value in task_running[task_name].most_common()) or "-"
            lines.append(
                f"  {task_name}: pri={task_priorities.get(task_name, '?')} "
                f"status[{status_text}] running[{running_text}] "
                f"stack_used_max={task_stack_max_used.get(task_name, '?')} "
                f"stack_free_min={task_stack_min.get(task_name, '?')}"
            )
    return "\n".join(lines) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("logs", nargs="+", type=Path)
    parser.add_argument("--write-summary", action="store_true", help="Write <log>.summary.txt next to every input log")
    args = parser.parse_args()

    for index, path in enumerate(args.logs):
        output = summarize(path)
        if index:
            print()
        print(output, end="")
        if args.write_summary:
            path.with_suffix(path.suffix + ".summary.txt").write_text(output, encoding="utf-8")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
