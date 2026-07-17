#!/usr/bin/env python3
"""Real two-browser HA softphone audio/video qualification matrix."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time
from contextlib import suppress
from typing import Any

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "test_runs"))

from ha_playwright_auth import context_kwargs  # noqa: E402
from ha_softphone_matrix import (  # noqa: E402
    CLICK,
    HA_BASE,
    SET_AUTO_ANSWER,
    SET_SEND_VIDEO,
    wait_card,
)


EXPECT_VIDEO = os.environ.get("EXPECT_VIDEO", "") == "1"
CASA_URL = f"{HA_BASE}/lovelace/default_view"
TEST_URL = f"{HA_BASE}/lovelace/test"

SET_TARGET = r"""
(name) => {
  const deep = (selector, root = document) => {
    const found = [...root.querySelectorAll(selector)];
    for (const node of root.querySelectorAll("*")) if (node.shadowRoot) found.push(...deep(selector, node.shadowRoot));
    return found;
  };
  const card = deep("voip-stack-card, intercom-card")
    .find((item) => (item.config?.mode || item.config?.card_mode || "") === "ha_softphone");
  const target = (card?._softphoneTargets?.() || []).find((item) => String(item.name || "") === name);
  if (!card || !target?.device_id) return false;
  card._setSoftphoneTarget(target.device_id);
  return card._getSoftphoneTargetDevice?.()?.device_id === target.device_id;
}
"""

ENGINE_STATS = """() => ({
  state: String(globalThis.__voipStackEngine?.state || ""),
  call_id: String(globalThis.__voipStackEngine?.callId || ""),
  video_active: Boolean(globalThis.__voipStackEngine?.videoActive),
  stats: globalThis.__voipStackEngine?.stats || {},
})"""

HANGUP = r"""
async () => {
  const deep = (selector, root = document) => {
    const found = [...root.querySelectorAll(selector)];
    for (const node of root.querySelectorAll("*")) if (node.shadowRoot) found.push(...deep(selector, node.shadowRoot));
    return found;
  };
  const card = deep("voip-stack-card, intercom-card")
    .find((item) => (item.config?.mode || item.config?.card_mode || "") === "ha_softphone");
  if (!card?._hangup) return false;
  await card._hangup();
  return true;
}
"""


def _state(page: Any, expected: str, label: str, timeout: float = 12) -> dict[str, Any]:
    return wait_card(
        page,
        lambda item: (
            item["backend"]["state"] == expected
            and item["card"]["state"] == expected
            and item["backend"]["call_id"] == item["card"]["call_id"]
        ),
        timeout,
        label,
    )


def _wait_video(page: Any, label: str) -> dict[str, Any]:
    deadline = time.monotonic() + 12
    last: dict[str, Any] = {}
    while time.monotonic() < deadline:
        last = page.evaluate(ENGINE_STATS) or {}
        video = (last.get("stats") or {}).get("video") or {}
        if (
            last.get("video_active")
            and int(video.get("sent") or 0) > 0
            and int(video.get("received") or 0) > 0
        ):
            return last
        page.wait_for_timeout(100)
    raise RuntimeError(f"timeout waiting for {label}: {last}")


def main() -> int:
    output = Path(
        os.environ.get(
            "LOCAL_SOFTPHONE_MATRIX_OUT",
            ROOT / "test_captures" / "local_softphone_live_matrix.json",
        )
    )
    results: list[dict[str, Any]] = []
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(
            headless=True,
            executable_path="/usr/bin/chromium",
            args=[
                "--use-fake-ui-for-media-stream",
                "--use-fake-device-for-media-stream",
                "--autoplay-policy=no-user-gesture-required",
                f"--unsafely-treat-insecure-origin-as-secure={HA_BASE}",
            ],
        )
        context = browser.new_context(**context_kwargs())
        casa = context.new_page()
        test = context.new_page()
        casa.goto(CASA_URL, wait_until="domcontentloaded", timeout=30_000)
        test.goto(TEST_URL, wait_until="domcontentloaded", timeout=30_000)
        for page, name in ((casa, "Casa"), (test, "Test")):
            wait_card(page, lambda item: bool(item), 30, f"{name} card ready")
            current = wait_card(page, lambda item: bool(item), 5, f"{name} state")
            if current["backend"]["state"] != "idle":
                page.evaluate(HANGUP)
                _state(page, "idle", f"{name} initial cleanup")
            page.evaluate(SET_AUTO_ANSWER, False)
            if EXPECT_VIDEO and not page.evaluate(SET_SEND_VIDEO, True):
                raise RuntimeError(f"failed to enable Send Camera on {name}")

        def run_case(
            name: str,
            caller: Any,
            caller_name: str,
            callee: Any,
            callee_name: str,
            *,
            hangup: Any,
        ) -> None:
            started = time.monotonic()
            try:
                if not caller.evaluate(SET_TARGET, callee_name):
                    raise RuntimeError(f"{callee_name} is not selectable from {caller_name}")
                if not caller.evaluate(CLICK, "Call"):
                    raise RuntimeError(f"Call unavailable on {caller_name}")
                outgoing = _state(caller, "calling", f"{name}: caller calling")
                incoming = _state(callee, "ringing", f"{name}: callee ringing")
                call_id = outgoing["backend"]["call_id"]
                if incoming["backend"]["call_id"] != call_id:
                    raise RuntimeError("local endpoints received different Call-IDs")
                if not callee.evaluate(CLICK, "Answer"):
                    raise RuntimeError(f"Answer unavailable on {callee_name}")
                _state(caller, "in_call", f"{name}: caller in call")
                _state(callee, "in_call", f"{name}: callee in call")
                media: dict[str, Any] = {}
                if EXPECT_VIDEO:
                    media[caller_name] = _wait_video(caller, f"{caller_name} video")
                    media[callee_name] = _wait_video(callee, f"{callee_name} video")
                if not hangup.evaluate(HANGUP):
                    raise RuntimeError("Hangup unavailable")
                _state(caller, "idle", f"{name}: caller idle")
                _state(callee, "idle", f"{name}: callee idle")
                results.append(
                    {
                        "name": name,
                        "status": "pass",
                        "seconds": round(time.monotonic() - started, 3),
                        "call_id": call_id,
                        "media": media,
                    }
                )
            except Exception as err:  # noqa: BLE001
                results.append(
                    {
                        "name": name,
                        "status": "fail",
                        "seconds": round(time.monotonic() - started, 3),
                        "error": str(err),
                    }
                )
                for page in (caller, callee):
                    with suppress(Exception):
                        page.evaluate(HANGUP)
                for page, endpoint_name in (
                    (caller, caller_name),
                    (callee, callee_name),
                ):
                    with suppress(Exception):
                        _state(page, "idle", f"{name}: cleanup {endpoint_name}")
                time.sleep(0.5)

        run_case("casa_to_test_caller_hangup", casa, "Casa", test, "Test", hangup=casa)
        run_case("test_to_casa_callee_hangup", test, "Test", casa, "Casa", hangup=casa)
        context.close()
        browser.close()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(results, indent=2))
    print(json.dumps(results, indent=2))
    return 1 if any(item["status"] != "pass" for item in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
