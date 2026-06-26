#!/usr/bin/env python3
"""Run repeatable local checks for Intercom Native / ESPHome intercom work."""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
PYTHON = Path("/home/codex/.venv/bin/python")


def run(cmd: list[str]) -> None:
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=ROOT, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--visual", action="store_true", help="Run Playwright HA card visual smoke.")
    parser.add_argument("--live", action="store_true", help="Run live HA/ESP call matrix.")
    parser.add_argument("--compile-profiles", action="store_true", help="Compile maintained SIP ESPHome profiles.")
    args = parser.parse_args()

    py = str(PYTHON if PYTHON.exists() else Path(sys.executable))
    run([py, "-m", "py_compile",
         "custom_components/intercom_native/__init__.py",
         "custom_components/intercom_native/sip_client.py",
         "custom_components/intercom_native/sip_listener.py",
         "custom_components/intercom_native/websocket_api.py",
         "tools/ha_live_intercom_matrix.py",
         "tools/ha_card_visual_smoke.py"])
    run([py, "tests/test_voip_phase1.py"])
    run([py, "tests/test_intercom_protocol.py"])
    run([py, "tests/test_device_resolver_sip.py"])
    run([py, "tests/test_frontend_card_contract.py"])
    run([py, "tests/test_runtime_fsm_target_model.py"])
    run(["node", "--check", "custom_components/intercom_native/frontend/intercom-card.js"])
    run([
        "g++", "-std=c++17",
        "tests/runtime_fsm_state_test.cpp",
        "esphome/components/runtime_fsm/runtime_fsm_state.cpp",
        "-o", "/tmp/runtime_fsm_state_test",
    ])
    run(["/tmp/runtime_fsm_state_test"])
    run(["git", "diff", "--check"])

    if args.visual:
        run([py, "tools/ha_card_visual_smoke.py"])
    if args.live:
        run([py, "tools/ha_live_intercom_matrix.py", "--device", "ws3", "--device", "spotpear", "--full"])
    if args.compile_profiles:
        run(["esphome", "compile", "yamls/full-experience/single-bus/waveshare-s3-full-afe-sip.yaml"])
        run(["esphome", "compile", "yamls/full-experience/single-bus/spotpear-ball-v2-full-afe-sip.yaml"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
