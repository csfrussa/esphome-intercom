#!/usr/bin/env python3
"""Run ESPHome Host scenarios with process telemetry and timeline extraction."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import re
import signal
import socket
import subprocess
import sys
import threading
import time
from typing import Any


ROOT = Path(__file__).resolve().parents[2]
DEFAULT_PROFILE = Path("yamls/generated/virtual/spotpear-ball-v2-full-afe-sip-host.yaml")
DEFAULT_SOCKET = Path("test_runs/simulator/spotpear-host-sim.sock")
DEFAULT_REPORT = Path("test_runs/simulator/host_runtime_audit.json")
DEFAULT_LOG = Path("test_runs/simulator/esphome-host-audit.log")
PYTHON = Path(os.environ.get("PYTHON_BIN", "/home/codex/.venv/bin/python"))
ESPHOME = Path(os.environ.get("ESPHOME_BIN", "/home/codex/.venv/bin/esphome"))


def proc_stat(pid: int) -> dict[str, int] | None:
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8")
        after_name = stat.rsplit(")", 1)[1].strip().split()
        utime = int(after_name[11])
        stime = int(after_name[12])
    except (FileNotFoundError, IndexError, ValueError):
        return None

    rss_kb = 0
    try:
        for line in Path(f"/proc/{pid}/status").read_text(encoding="utf-8").splitlines():
            if line.startswith("VmRSS:"):
                rss_kb = int(line.split()[1])
                break
    except (FileNotFoundError, IndexError, ValueError):
        pass
    return {"cpu_ticks": utime + stime, "rss_kb": rss_kb}


def sample_process(pid: int, stop: threading.Event, interval: float, out: list[dict[str, Any]]) -> None:
    clk_tck = os.sysconf(os.sysconf_names["SC_CLK_TCK"])
    previous = proc_stat(pid)
    previous_t = time.monotonic()
    while not stop.wait(interval):
        current = proc_stat(pid)
        now = time.monotonic()
        if current is None:
            break
        cpu_pct = 0.0
        if previous is not None:
            elapsed = max(now - previous_t, 0.001)
            cpu_pct = ((current["cpu_ticks"] - previous["cpu_ticks"]) / clk_tck) / elapsed * 100.0
        out.append(
            {
                "t": round(now, 3),
                "cpu_pct_one_core": round(cpu_pct, 2),
                "rss_kb": current["rss_kb"],
            }
        )
        previous = current
        previous_t = now


def wait_socket(socket_path: Path, proc: subprocess.Popen[Any], timeout: float) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if socket_path.exists():
            return
        if proc.poll() is not None:
            raise RuntimeError(f"ESPHome Host exited before socket was created: rc={proc.returncode}")
        time.sleep(0.1)
    raise RuntimeError(f"timeout waiting for simulator socket: {socket_path}")


def wait_log_pattern(log_path: Path, pattern: str, proc: subprocess.Popen[Any], timeout: float) -> None:
    compiled = re.compile(pattern)
    deadline = time.monotonic() + timeout
    offset = 0
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise RuntimeError(f"ESPHome Host exited while waiting for log pattern: {pattern}")
        if log_path.exists():
            text = log_path.read_text(encoding="utf-8", errors="replace")
            chunk = text[offset:]
            offset = len(text)
            if compiled.search(chunk):
                return
        time.sleep(0.25)
    raise RuntimeError(f"timeout waiting for log pattern: {pattern}")


def shutdown_socket(socket_path: Path) -> None:
    if not socket_path.exists():
        return
    payload = b'{"jsonrpc":"2.0","id":1,"method":"shutdown","params":{}}\n'
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(2)
            client.connect(str(socket_path))
            client.sendall(payload)
            client.recv(4096)
    except OSError:
        pass


def run_cmd(cmd: list[str], *, env: dict[str, str] | None = None) -> dict[str, Any]:
    start = time.monotonic()
    proc = subprocess.run(cmd, cwd=ROOT, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    return {
        "cmd": cmd,
        "returncode": proc.returncode,
        "duration_s": round(time.monotonic() - start, 3),
        "stdout": proc.stdout[-20000:],
        "stderr": proc.stderr[-20000:],
    }


STATE_RE = re.compile(r"\[(?P<time>[0-9:.]+)]\[.*?]\[(?P<component>[^:\]]+):(?P<line>\d+)]: (?P<msg>.*)")


def extract_timeline(log_path: Path) -> list[dict[str, str]]:
    if not log_path.exists():
        return []
    interesting = (
        "State changed",
        "Desired state",
        "Assist Pipeline",
        "STT ",
        "TTS ",
        "Virtual microphone",
        "Virtual speaker",
        "intercom",
        "sip",
        "RTP",
        "Warning",
        "Error",
    )
    events: list[dict[str, str]] = []
    for raw in log_path.read_text(encoding="utf-8", errors="replace").splitlines():
        clean = re.sub(r"\x1b\[[0-9;]*m", "", raw)
        if not any(token in clean for token in interesting):
            continue
        match = STATE_RE.search(clean)
        if match:
            events.append(match.groupdict())
        else:
            events.append({"time": "", "component": "", "line": "", "msg": clean})
    return events[-1000:]


def summarize_findings(log_path: Path, telemetry: list[dict[str, Any]], commands: list[dict[str, Any]]) -> list[dict[str, str]]:
    findings: list[dict[str, str]] = []
    text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
    if "Connection refused" in text or any("Connection refused" in c.get("stderr", "") for c in commands):
        findings.append({"severity": "high", "issue": "Simulator socket refused a client connection during audit."})
    if "Speaker has finished outputting all audio" not in text and "STREAMING_RESPONSE" in text:
        findings.append({"severity": "high", "issue": "Voice Assistant streamed a response but did not log speaker drain completion."})
    if "State changed from RESPONSE_FINISHED to IDLE" not in text and "RESPONSE_FINISHED" in text:
        findings.append({"severity": "high", "issue": "Voice Assistant reached RESPONSE_FINISHED without returning to IDLE."})
    if telemetry:
        max_rss = max(int(sample["rss_kb"]) for sample in telemetry)
        max_cpu = max(float(sample["cpu_pct_one_core"]) for sample in telemetry)
        if max_rss > 512 * 1024:
            findings.append({"severity": "medium", "issue": f"Host process RSS exceeded 512 MiB: {max_rss} KiB."})
        if max_cpu > 150:
            findings.append({"severity": "medium", "issue": f"Host process CPU exceeded 150% of one core: {max_cpu}%."})
    for command in commands:
        if command["returncode"] != 0:
            findings.append({"severity": "critical", "issue": f"Command failed: {' '.join(command['cmd'])}"})
    return findings


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--profile", type=Path, default=DEFAULT_PROFILE)
    parser.add_argument("--socket", type=Path, default=DEFAULT_SOCKET)
    parser.add_argument("--scenario", default="all")
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--trace-dir", type=Path, default=Path("test_runs/simulator/traces/host-runtime-audit"))
    parser.add_argument("--report", type=Path, default=DEFAULT_REPORT)
    parser.add_argument("--log", type=Path, default=DEFAULT_LOG)
    parser.add_argument("--sample-interval", type=float, default=0.25)
    parser.add_argument("--va-repeat", type=int, default=0)
    parser.add_argument("--startup-timeout", type=float, default=120.0)
    parser.add_argument("--ha-connect-delay", type=float, default=20.0)
    parser.add_argument("--insecure", action="store_true")
    args = parser.parse_args()

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.log.parent.mkdir(parents=True, exist_ok=True)
    args.socket.unlink(missing_ok=True)
    args.log.unlink(missing_ok=True)

    with args.log.open("wb") as log_file:
        host = subprocess.Popen(
            [str(ESPHOME), "run", str(args.profile)],
            cwd=ROOT,
            stdout=log_file,
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        stop_sampling = threading.Event()
        telemetry: list[dict[str, Any]] = []
        sampler = threading.Thread(
            target=sample_process,
            args=(host.pid, stop_sampling, args.sample_interval, telemetry),
            daemon=True,
        )
        sampler.start()

        commands: list[dict[str, Any]] = []
        try:
            wait_socket(args.socket, host, args.startup_timeout)
            scenario_cmd = [
                str(PYTHON),
                "tools/simulator/scenario_runner.py",
                args.scenario,
                "--socket",
                str(args.socket),
                "--repeat",
                str(args.repeat),
                "--trace-dir",
                str(args.trace_dir),
            ]
            commands.append(run_cmd(scenario_cmd))

            if args.va_repeat:
                time.sleep(max(0.0, args.ha_connect_delay))
                va_cmd = [
                    str(PYTHON),
                    "tools/simulator/ha_voice_assistant_host_test.py",
                    "--repeat",
                    str(args.va_repeat),
                ]
                if args.insecure:
                    va_cmd.append("--insecure")
                commands.append(run_cmd(va_cmd, env=os.environ.copy()))
        finally:
            shutdown_socket(args.socket)
            try:
                host.wait(timeout=10)
            except subprocess.TimeoutExpired:
                os.killpg(host.pid, signal.SIGTERM)
                host.wait(timeout=10)
            stop_sampling.set()
            sampler.join(timeout=2)

    report = {
        "ok": all(command["returncode"] == 0 for command in commands),
        "profile": str(args.profile),
        "socket": str(args.socket),
        "log": str(args.log),
        "trace_dir": str(args.trace_dir),
        "telemetry": {
            "samples": telemetry,
            "max_rss_kb": max((int(sample["rss_kb"]) for sample in telemetry), default=0),
            "max_cpu_pct_one_core": max((float(sample["cpu_pct_one_core"]) for sample in telemetry), default=0.0),
        },
        "commands": commands,
        "timeline": extract_timeline(args.log),
    }
    report["findings"] = summarize_findings(args.log, telemetry, commands)
    args.report.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    print(json.dumps({"ok": report["ok"], "report": str(args.report), "findings": report["findings"]}, indent=2))
    return 0 if report["ok"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
