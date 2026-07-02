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


def summarize(path: Path) -> str:
    text = path.read_text(encoding="utf-8", errors="replace")
    snapshots = _split_snapshots(text)
    lines: list[str] = [f"file: {path}", f"snapshots: {len(snapshots)}", ""]

    total = Counter()
    running_total = Counter()
    per_task = defaultdict(Counter)

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
