#!/usr/bin/env python3
"""Capture intrusive ESP32-S3 JTAG/GDB stop/resume snapshots.

This is intentionally different from runtime_diag:

* runtime_diag runs inside the firmware and is useful for counters/heap/task
  deltas without stopping the system;
* this tool halts the CPUs through OpenOCD, asks GDB for FreeRTOS threads and
  backtraces, then resumes. Audio will glitch while the target is halted, but
  the stack traces show exactly what loopTask and the other tasks were doing.
"""

from __future__ import annotations

import argparse
import shutil
import socket
import subprocess
import sys
import tempfile
import textwrap
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_GDB_CANDIDATES = (
    ROOT.parent / ".platformio/tools/tool-xtensa-esp-elf-gdb/bin/xtensa-esp32s3-elf-gdb",
    ROOT.parent / ".platformio/packages/tool-xtensa-esp-elf-gdb/bin/xtensa-esp32s3-elf-gdb",
)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _find_gdb(explicit: str | None) -> str:
    if explicit:
        return explicit
    for candidate in DEFAULT_GDB_CANDIDATES:
        if candidate.exists():
            return str(candidate)
    found = shutil.which("xtensa-esp32s3-elf-gdb")
    if found:
        return found
    raise SystemExit("xtensa-esp32s3-elf-gdb not found; build once with ESPHome/PlatformIO first")


def _find_elf(explicit: str | None, device: str | None) -> Path:
    if explicit:
        elf = Path(explicit).expanduser().resolve()
        if not elf.exists():
            raise SystemExit(f"ELF not found: {elf}")
        return elf
    candidates = sorted(ROOT.glob("yamls/**/.esphome/build/*/.pioenvs/*/firmware.elf"), key=lambda p: p.stat().st_mtime)
    if device:
        filtered = [p for p in candidates if device in str(p)]
        if filtered:
            return filtered[-1]
    if candidates:
        return candidates[-1]
    raise SystemExit("No firmware.elf found; pass --elf explicitly")


def _remote_openocd_command(gdb_port: int, openocd_bin: str, scripts: list[str], config: list[str]) -> str:
    script_args = " ".join(f"-s {path}" for path in scripts)
    config_args = " ".join(f"-f {path}" for path in config)
    return (
        f"exec {openocd_bin} "
        f"{script_args} "
        f"{config_args} "
        f"-c 'gdb_port {gdb_port}' "
        "-c 'telnet_port disabled' "
        "-c 'tcl_port disabled'"
    )


def _write_gdb_script(path: Path, samples: int, interval: float, backtrace_depth: int) -> None:
    lines: list[str] = [
        "set pagination off",
        "set confirm off",
        "set print thread-events off",
        "set remotetimeout 10",
        "target extended-remote 127.0.0.1:3333",
        "set $sample = 0",
        f"while $sample < {samples}",
        '  printf "\\n===== JTAG SNAPSHOT %d =====\\n", $sample',
        "  monitor halt",
        "  info threads",
        f"  thread apply all bt {backtrace_depth}",
        "  info registers pc a0 a1 a2 a3 ps",
        "  monitor resume",
        f"  shell sleep {interval:.3f}",
        "  set $sample = $sample + 1",
        "end",
        "detach",
        "quit",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Take intrusive OpenOCD/GDB stop/resume snapshots from an ESP32-S3.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--remote", default="daniele@192.168.1.20", help="SSH host connected to the ESP USB-JTAG")
    parser.add_argument("--elf", help="Matching firmware.elf. Required for useful file/line symbols.")
    parser.add_argument("--device", default="spotpear", help="Substring used to auto-pick firmware.elf")
    parser.add_argument("--gdb", help="xtensa-esp32s3-elf-gdb path")
    parser.add_argument("--samples", type=int, default=20, help="Number of stop/resume samples")
    parser.add_argument("--interval", type=float, default=1.0, help="Seconds between samples")
    parser.add_argument("--bt-depth", type=int, default=12, help="Backtrace depth per FreeRTOS thread")
    parser.add_argument("--out-dir", default="test_runs/jtag_snapshots", help="Output directory")
    parser.add_argument("--keep-openocd-log", action="store_true", help="Keep the OpenOCD log next to the GDB log")
    parser.add_argument("--openocd-bin", default="/usr/bin/openocd-esp32openocd", help="Remote OpenOCD binary")
    parser.add_argument(
        "--openocd-scripts",
        action="append",
        default=["/usr/share/openocd-esp32/scripts"],
        help="Remote OpenOCD script directory; repeatable",
    )
    parser.add_argument(
        "--openocd-config",
        action="append",
        default=[
            "board/esp32s3-builtin.cfg",
        ],
        help="Remote OpenOCD config file; repeatable and order-sensitive",
    )
    args = parser.parse_args()

    gdb = _find_gdb(args.gdb)
    elf = _find_elf(args.elf, args.device)
    out_dir = (ROOT / args.out_dir).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    stamp = time.strftime("%Y%m%d-%H%M%S")
    gdb_log = out_dir / f"{args.device}-jtag-{stamp}.log"
    openocd_log = out_dir / f"{args.device}-openocd-{stamp}.log"
    local_port = _free_port()

    print(f"ELF: {elf}")
    print(f"GDB: {gdb}")
    print(f"Remote OpenOCD: {args.remote}")
    print(f"Output: {gdb_log}")
    print("The target will halt briefly for every sample; audio glitches are expected.")

    openocd_cmd = _remote_openocd_command(3333, args.openocd_bin, args.openocd_scripts, args.openocd_config)
    ssh_openocd = subprocess.Popen(
        ["ssh", "-tt", "-L", f"{local_port}:127.0.0.1:3333", args.remote, openocd_cmd],
        stdout=open(openocd_log, "w", encoding="utf-8"),
        stderr=subprocess.STDOUT,
        text=True,
    )

    try:
        time.sleep(2.0)
        if ssh_openocd.poll() is not None:
            keep_log = True
            raise SystemExit(f"OpenOCD exited early; see {openocd_log}")

        with tempfile.TemporaryDirectory() as tmp:
            script = Path(tmp) / "snapshot.gdb"
            _write_gdb_script(script, args.samples, args.interval, args.bt_depth)
            # The tunnel maps local_port -> remote 3333, but the script keeps the
            # conventional 3333 to stay readable; patch it at runtime.
            text = script.read_text(encoding="utf-8").replace("127.0.0.1:3333", f"127.0.0.1:{local_port}")
            script.write_text(text, encoding="utf-8")
            with open(gdb_log, "w", encoding="utf-8") as log:
                result = subprocess.run([gdb, str(elf), "-x", str(script)], stdout=log, stderr=subprocess.STDOUT)
            if result.returncode != 0:
                keep_log = True
                print(f"GDB failed with exit code {result.returncode}; see {gdb_log}", file=sys.stderr)
                return result.returncode
    finally:
        ssh_openocd.terminate()
        try:
            ssh_openocd.wait(timeout=3)
        except subprocess.TimeoutExpired:
            ssh_openocd.kill()
        if not args.keep_openocd_log and not locals().get("keep_log", False) and openocd_log.exists():
            openocd_log.unlink()

    print(f"Wrote {gdb_log}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
