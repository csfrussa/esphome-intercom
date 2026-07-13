#!/usr/bin/env python3
"""Run repeatable local checks for VoIP Stack / ESPHome intercom work."""

from __future__ import annotations

import argparse
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
PYTHON = ROOT / ".venv/bin/python"


def run(cmd: list[str]) -> None:
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, cwd=ROOT, check=True)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--compile-profiles", action="store_true", help="Compile maintained SIP ESPHome profiles.")
    args = parser.parse_args()

    py = str(PYTHON if PYTHON.exists() else Path(sys.executable))
    run([py, "-m", "py_compile",
         "custom_components/voip_stack/__init__.py",
         "custom_components/voip_stack/sip_client.py",
         "custom_components/voip_stack/sip_listener.py",
         "custom_components/voip_stack/video_rtp.py",
         "custom_components/voip_stack/video_ws_view.py",
         "custom_components/voip_stack/websocket_api.py",
         "tools/experimental_sip_video_browser_probe.py",
         "tests/support/qualification_matrix.py"])
    run([py, "tests/test_voip_phase1.py"])
    run([py, "tests/test_device_resolver_sip.py"])
    run([py, "tests/test_frontend_card_contract.py"])
    run([py, "-m", "pytest", "-q", "tests/test_ha_softphone_backend_contract.py"])
    run([py, "tests/test_qualification_matrix.py"])
    run([py, "tests/test_runtime_controller_target_model.py"])
    run([py, "tests/support/qualification_matrix.py", "--validate", "--summary"])
    run(["node", "--check", "custom_components/voip_stack/frontend/voip-stack-card.js"])
    run(["node", "--check", "custom_components/voip_stack/frontend/voip-stack-engine.js"])
    run(["node", "--check", "custom_components/voip_stack/frontend/voip-stack-video.js"])
    run(["git", "diff", "--check"])

    if args.compile_profiles:
        esphome = str(ROOT / ".venv/bin/esphome")
        run([esphome, "compile", "yamls/full-experience/single-bus/waveshare-s3-full-afe.yaml"])
        run([esphome, "compile", "yamls/full-experience/single-bus/spotpear-ball-v2-full-afe.yaml"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
