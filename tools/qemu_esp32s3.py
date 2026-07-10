#!/usr/bin/env python3
"""Run an existing ESPHome ESP32-S3 build under Espressif QEMU."""

from __future__ import annotations

import argparse
import binascii
import shutil
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_QEMU = ROOT.parent / ".espressif/tools/qemu-xtensa/esp_develop_9.2.2_20250817/qemu/bin/qemu-system-xtensa"
ESP32S3_DEFAULT_EFUSE = binascii.unhexlify(
    "00000000000000000000000000000000000000000000000000000000000000000000000000000c00"
    "00000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "00000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "00000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "00000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "00000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "00000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "00000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "00000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "00000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "00000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "00000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "00000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "00000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "00000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "00000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "00000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "00000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "00000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "00000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "00000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "00000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "00000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "00000000000000000000000000000000000000000000000000000000000000000000000000000000"
    "000000000000000000000000000000000000000000000000"
)


def _find_build(device: str) -> Path:
    candidates = sorted(ROOT.glob("yamls/**/.esphome/build/*/.pioenvs/*/flash_args"), key=lambda p: p.stat().st_mtime)
    filtered = [path for path in candidates if device in str(path)]
    if filtered:
        return filtered[-1].parent
    if candidates:
        return candidates[-1].parent
    raise SystemExit("No ESPHome build flash_args found; compile a profile first or pass --build-dir")


def _flash_size(build_dir: Path) -> str:
    flash_args = (build_dir / "flash_args").read_text(encoding="utf-8")
    parts = flash_args.split()
    if "--flash_size" in parts:
        return parts[parts.index("--flash_size") + 1]
    return "16MB"


def _sdkconfig_path(build_dir: Path) -> Path | None:
    build_root = build_dir.resolve()
    while build_root.parent != build_root:
        if list(build_root.glob("sdkconfig*")) and (build_root / ".pioenvs").exists():
            break
        build_root = build_root.parent
    else:
        return None
    exact = sorted(build_root.glob("sdkconfig.*"))
    return exact[-1] if exact else None


def _sdkconfig_has(build_dir: Path, key: str) -> bool:
    sdkconfig = _sdkconfig_path(build_dir)
    if sdkconfig is None:
        return False
    return f"{key}=y" in sdkconfig.read_text(encoding="utf-8", errors="ignore")


def _size_bytes(size: str) -> int:
    value = size.strip().upper()
    if value.endswith("MB"):
        return int(value[:-2]) * 1024 * 1024
    if value.endswith("M"):
        return int(value[:-1]) * 1024 * 1024
    if value.endswith("KB"):
        return int(value[:-2]) * 1024
    if value.endswith("K"):
        return int(value[:-1]) * 1024
    return int(value)


def _copy_padded(source: Path, output: Path, fill_size: str) -> bool:
    if not source.exists():
        return False
    output.parent.mkdir(parents=True, exist_ok=True)
    target_size = _size_bytes(fill_size)
    data = source.read_bytes()
    if len(data) > target_size:
        raise SystemExit(f"{source} is larger than requested flash size {fill_size}")
    with output.open("wb") as out:
        out.write(data)
        out.write(b"\xff" * (target_size - len(data)))
    return True


def _merge_flash(build_dir: Path, output: Path, chip: str, fill_size: str) -> None:
    factory = build_dir / "firmware.factory.bin"
    if _copy_padded(factory, output, fill_size):
        return
    flash_args = build_dir / "flash_args"
    if not flash_args.exists():
        raise SystemExit(f"flash_args not found: {flash_args}")
    output.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        sys.executable,
        "-m",
        "esptool",
        "--chip",
        chip,
        "merge-bin",
        "--fill-flash-size",
        fill_size,
        "-o",
        str(output),
        f"@{flash_args}",
    ]
    subprocess.run(cmd, cwd=build_dir, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(
        description=__doc__,
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--device", default="spotpear", help="Substring used to auto-pick an ESPHome build")
    parser.add_argument("--build-dir", type=Path, help="Directory containing flash_args and firmware binaries")
    parser.add_argument("--qemu", type=Path, default=DEFAULT_QEMU, help="Espressif qemu-system-xtensa path")
    parser.add_argument("--flash-file", type=Path, help="Existing or generated flash image path")
    parser.add_argument("--efuse-file", type=Path, help="Existing or generated efuse image path")
    parser.add_argument("--flash-size", help="Flash image size for esptool merge-bin")
    parser.add_argument("--timeout", type=float, default=20.0, help="Seconds to run before terminating QEMU")
    parser.add_argument(
        "--psram-mode",
        choices=("auto", "quad", "octal", "off"),
        default="auto",
        help="QEMU PSRAM mode. auto reads CONFIG_SPIRAM_MODE_OCT from sdkconfig.",
    )
    parser.add_argument("--no-merge", action="store_true", help="Use --flash-file as-is")
    parser.add_argument("--gdb", action="store_true", help="Expose QEMU GDB server on :1234 and wait for a debugger")
    parser.add_argument("--extra-arg", action="append", default=[], help="Extra QEMU argument; repeatable")
    args = parser.parse_args()

    qemu = args.qemu.expanduser()
    if not qemu.exists():
        found = shutil.which("qemu-system-xtensa")
        if found:
            qemu = Path(found)
        else:
            raise SystemExit(f"QEMU not found: {args.qemu}")

    build_dir = args.build_dir.expanduser().resolve() if args.build_dir else _find_build(args.device)
    flash_size = args.flash_size or _flash_size(build_dir)
    flash_file = args.flash_file or (ROOT / "test_runs/qemu" / args.device / "qemu_flash.bin")
    flash_file = flash_file.expanduser().resolve()
    efuse_file = args.efuse_file or (ROOT / "test_runs/qemu" / args.device / "qemu_efuse.bin")
    efuse_file = efuse_file.expanduser().resolve()

    if not args.no_merge:
        _merge_flash(build_dir, flash_file, "esp32s3", flash_size)
    if not efuse_file.exists():
        efuse_file.parent.mkdir(parents=True, exist_ok=True)
        efuse_file.write_bytes(ESP32S3_DEFAULT_EFUSE)

    cmd = [
        str(qemu),
        "-M",
        "esp32s3",
        "-m",
        "32M",
        "-drive",
        f"file={flash_file},if=mtd,format=raw",
        "-drive",
        f"file={efuse_file},if=none,format=raw,id=efuse",
        "-global",
        "driver=nvram.esp32s3.efuse,property=drive,value=efuse",
        "-global",
        "driver=timer.esp32s3.timg,property=wdt_disable,value=true",
        "-nic",
        "user,model=open_eth",
        "-nographic",
        "-serial",
        "mon:stdio",
        "-action",
        "panic=exit-failure",
    ]
    psram_mode = args.psram_mode
    if psram_mode == "auto":
        psram_mode = "octal" if _sdkconfig_has(build_dir, "CONFIG_SPIRAM_MODE_OCT") else "quad"
    if psram_mode == "octal":
        cmd[5:5] = ["-global", "driver=ssi_psram,property=is_octal,value=true"]
    elif psram_mode == "off":
        cmd[3:5] = ["0"]
    if args.gdb:
        cmd.extend(["-S", "-s"])
    cmd.extend(args.extra_arg)

    print(f"build_dir: {build_dir}", flush=True)
    print(f"flash: {flash_file} ({flash_size})", flush=True)
    print(f"psram_mode: {psram_mode}", flush=True)
    print("qemu:", " ".join(cmd), flush=True)
    try:
        result = subprocess.run(cmd, timeout=args.timeout)
        return result.returncode
    except subprocess.TimeoutExpired:
        print(f"QEMU timeout after {args.timeout:.1f}s; terminating", file=sys.stderr)
        return 124


if __name__ == "__main__":
    raise SystemExit(main())
