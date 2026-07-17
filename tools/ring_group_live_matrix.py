#!/usr/bin/env python3
"""Real two-browser ring-group qualification through the Wildix trunk."""

from __future__ import annotations

import json
import os
from pathlib import Path
import sys
import time
from typing import Any

from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tools"))
sys.path.insert(0, str(ROOT / "test_runs"))

from ha_playwright_auth import context_kwargs  # noqa: E402
from ha_softphone_matrix import (  # noqa: E402
    BareSip,
    CLICK,
    HA_BASE,
    SET_AUTO_ANSWER,
    SET_SEND_VIDEO,
    WILDIX_CONFIG,
    wait_card,
)


CASA_URL = f"{HA_BASE}/lovelace/default_view"
TEST_URL = f"{HA_BASE}/lovelace/test"
EXPECT_VIDEO = os.environ.get("EXPECT_VIDEO", "") == "1"
CALLER_CONFIG = (
    Path("/home/codex/.baresip-wildix-426-video")
    if EXPECT_VIDEO and "WILDIX_CONFIG" not in os.environ
    else WILDIX_CONFIG
)


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


def _winner(page: Any, call_id: str, label: str) -> dict[str, Any]:
    return wait_card(
        page,
        lambda item: (
            item["backend"]["state"] == "in_call"
            and item["backend"]["call_id"] == call_id
            and (
                not EXPECT_VIDEO
                or (
                    item["backend"]["video_direction"] == "sendrecv"
                    and item["backend"]["video_rtp_tx_packets"] > 0
                    and item["backend"]["video_rtp_rx_packets"] > 0
                )
            )
        ),
        10,
        label,
    )


def _dial() -> BareSip:
    caller = BareSip(CALLER_CONFIG)
    caller.dial("427", wait_for="183 Session Progress")
    return caller


def main() -> int:
    output = Path(
        os.environ.get(
            "RING_GROUP_MATRIX_OUT",
            ROOT / "test_captures" / "ring_group_live_matrix.json",
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
            page.evaluate(SET_AUTO_ANSWER, False)
            if EXPECT_VIDEO and not page.evaluate(SET_SEND_VIDEO, True):
                raise RuntimeError(f"failed to enable Send Camera on {name}")

        def run_case(
            name: str,
            *,
            winner_page: Any | None,
            winner_name: str = "",
            decline_page: Any | None = None,
            decline_name: str = "",
            remote_cancel: bool = False,
        ) -> None:
            started = time.monotonic()
            caller: BareSip | None = None
            try:
                caller = _dial()
                casa_ringing = _state(casa, "ringing", f"{name}: Casa ringing")
                test_ringing = _state(test, "ringing", f"{name}: Test ringing")
                call_id = casa_ringing["backend"]["call_id"]
                if test_ringing["backend"]["call_id"] != call_id:
                    raise RuntimeError("ring-group members received different Call-IDs")
                if remote_cancel:
                    caller.hangup()
                    _state(casa, "idle", f"{name}: Casa idle")
                    _state(test, "idle", f"{name}: Test idle")
                else:
                    if decline_page is not None:
                        if not decline_page.evaluate(CLICK, "Decline"):
                            raise RuntimeError(f"Decline unavailable on {decline_name}")
                        _state(
                            decline_page,
                            "idle",
                            f"{name}: {decline_name} declined",
                        )
                        other = test if decline_page is casa else casa
                        _state(other, "ringing", f"{name}: remaining member ringing")
                    if winner_page is None or not winner_page.evaluate(CLICK, "Answer"):
                        raise RuntimeError(f"Answer unavailable on {winner_name}")
                    answered = _winner(
                        winner_page,
                        call_id,
                        f"{name}: {winner_name} winner media",
                    )
                    caller.wait_for("Call established", 5)
                    loser = test if winner_page is casa else casa
                    _state(loser, "idle", f"{name}: losing member idle")
                    caller.hangup()
                    _state(winner_page, "idle", f"{name}: winner idle")
                    results.append(
                        {
                            "name": name,
                            "status": "pass",
                            "seconds": round(time.monotonic() - started, 3),
                            "call_id": call_id,
                            "winner": winner_name,
                            "video_direction": answered["backend"]["video_direction"],
                            "video_rtp_tx_packets": answered["backend"]["video_rtp_tx_packets"],
                            "video_rtp_rx_packets": answered["backend"]["video_rtp_rx_packets"],
                        }
                    )
                    return
                results.append(
                    {
                        "name": name,
                        "status": "pass",
                        "seconds": round(time.monotonic() - started, 3),
                        "call_id": call_id,
                    }
                )
            except Exception as err:  # noqa: BLE001 - preserve every matrix result.
                results.append(
                    {
                        "name": name,
                        "status": "fail",
                        "seconds": round(time.monotonic() - started, 3),
                        "error": str(err),
                    }
                )
            finally:
                if caller is not None:
                    caller.close()
                time.sleep(0.5)

        run_case("casa_answers", winner_page=casa, winner_name="Casa")
        run_case("test_answers", winner_page=test, winner_name="Test")
        run_case(
            "casa_declines_test_answers",
            winner_page=test,
            winner_name="Test",
            decline_page=casa,
            decline_name="Casa",
        )
        run_case(
            "test_declines_casa_answers",
            winner_page=casa,
            winner_name="Casa",
            decline_page=test,
            decline_name="Test",
        )
        run_case("caller_cancels", winner_page=None, remote_cancel=True)
        context.close()
        browser.close()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(results, indent=2))
    print(json.dumps(results, indent=2))
    return 1 if any(item["status"] != "pass" for item in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
