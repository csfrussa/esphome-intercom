#!/usr/bin/env python3
"""End-to-end HA softphone state/card/routing qualification matrix."""

from __future__ import annotations

import argparse
import json
import os
import pty
import select
import subprocess
import sys
import time
import urllib.request
from contextlib import suppress
from pathlib import Path
from typing import Any, Callable

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "test_runs"))
from ha_playwright_auth import context_kwargs, ha_token  # noqa: E402


HA_BASE = "http://192.168.1.10:8123"
HA_URL = f"{HA_BASE}/lovelace/default_view"
# The stored frontend token is origin-scoped. Keep it on the same local origin
# used by the matrix so an unauthenticated dashboard cannot masquerade as a
# missing card.
os.environ["HA_URL"] = HA_BASE
AUTOMATION = "automation.voip_ha_non_risponde_inoltra_ad_assist"
WILDIX_CONFIG = Path("/home/codex/.baresip-wildix-426")
LOCAL_CONFIG = Path("/home/codex/.baresip-codex")

CARD_STATE = r"""
async () => {
  const deep = (selector, root = document) => {
    const found = [...root.querySelectorAll(selector)];
    for (const node of root.querySelectorAll("*")) if (node.shadowRoot) found.push(...deep(selector, node.shadowRoot));
    return found;
  };
  const card = deep("voip-stack-card, intercom-card")
    .find((item) => (item.config?.mode || item.config?.card_mode || "") === "ha_softphone");
  if (!card) return null;
  const backend = await card._hass.connection.sendMessagePromise({type: "voip_stack/ha_softphone_state"});
  const snapshot = card._softphoneSnapshot || {};
  return {
    backend: {
      state: backend?.state || "", call_id: backend?.call_id || "", caller: backend?.caller || "",
      callee: backend?.callee || "", terminal_reason: backend?.terminal_reason || "",
    },
    card: {
      state: snapshot.state || "", call_id: snapshot.call_id || "", caller: snapshot.caller || "",
      callee: snapshot.callee || "", terminal_reason: snapshot.terminal_reason || "",
    },
    text: (card.shadowRoot?.innerText || card.shadowRoot?.textContent || "").replace(/\s+/g, " ").trim(),
    auto_answer: !!card._autoAnswer,
    softphone_subscribers: window.__voipStackEngine?._softphoneSubscribers?.size ?? -1,
    call_subscribers: window.__voipStackEngine?._callSubscribers?.size ?? -1,
  };
}
"""

CLICK = r"""
(label) => {
  const deep = (selector, root = document) => {
    const found = [...root.querySelectorAll(selector)];
    for (const node of root.querySelectorAll("*")) if (node.shadowRoot) found.push(...deep(selector, node.shadowRoot));
    return found;
  };
  const card = deep("voip-stack-card, intercom-card")
    .find((item) => (item.config?.mode || item.config?.card_mode || "") === "ha_softphone");
  if (!card?.shadowRoot) return false;
  const button = [...card.shadowRoot.querySelectorAll("button")].find((item) =>
    (item.innerText || item.textContent || "").trim() === label && !item.hidden && !item.disabled && item.offsetParent !== null
  );
  if (!button) return false;
  button.click();
  return true;
}
"""

SET_AUTO_ANSWER = r"""
(enabled) => {
  const deep = (selector, root = document) => {
    const found = [...root.querySelectorAll(selector)];
    for (const node of root.querySelectorAll("*")) if (node.shadowRoot) found.push(...deep(selector, node.shadowRoot));
    return found;
  };
  const card = deep("voip-stack-card, intercom-card")
    .find((item) => (item.config?.mode || item.config?.card_mode || "") === "ha_softphone");
  if (!card?.shadowRoot) return false;
  const input = card.shadowRoot.querySelector("#auto-answer-cb");
  if (!input) return false;
  if (!!input.checked !== !!enabled) input.click();
  return !!card._autoAnswer === !!enabled;
}
"""


class BareSip:
    def __init__(self, config: Path) -> None:
        self.master, slave = pty.openpty()
        self.proc = subprocess.Popen(
            ["baresip", "-f", str(config)],
            stdin=slave,
            stdout=slave,
            stderr=slave,
            close_fds=True,
        )
        os.close(slave)
        os.set_blocking(self.master, False)
        self.output = ""
        self.wait_for("registered successfully", 8)

    def read(self) -> str:
        while True:
            ready, _, _ = select.select([self.master], [], [], 0)
            if not ready:
                break
            try:
                chunk = os.read(self.master, 65536)
            except (BlockingIOError, OSError):
                break
            if not chunk:
                break
            self.output += chunk.decode(errors="replace")
        return self.output

    def wait_for(self, needle: str, timeout: float) -> str:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if needle.lower() in self.read().lower():
                return self.output
            time.sleep(0.05)
        raise RuntimeError(
            f"bareSIP timeout waiting for {needle}: {self.output[-2000:]}"
        )

    def command(self, command: str) -> None:
        os.write(self.master, f"{command}\n".encode())

    def dial(self, target: str, *, wait_for: str = "Call established") -> None:
        self.command(f"/dial {target}")
        self.wait_for(wait_for, 10)

    def hangup(self) -> None:
        self.command("/hangup")

    def close(self) -> None:
        if self.proc.poll() is None:
            with suppress(Exception):
                self.command("/hangup")
            with suppress(Exception):
                self.command("/quit")
            try:
                self.proc.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.proc.terminate()
                with suppress(subprocess.TimeoutExpired):
                    self.proc.wait(timeout=2)
        with suppress(OSError):
            os.close(self.master)


def ha_request(path: str, data: dict[str, Any] | None = None) -> Any:
    body = None if data is None else json.dumps(data).encode()
    request = urllib.request.Request(
        f"{HA_BASE}{path}",
        data=body,
        headers={
            "Authorization": f"Bearer {ha_token()}",
            "Content-Type": "application/json",
        },
    )
    with urllib.request.urlopen(request, timeout=15) as response:
        payload = response.read()
    return json.loads(payload) if payload else None


def service(domain: str, name: str, data: dict[str, Any] | None = None) -> Any:
    return ha_request(f"/api/services/{domain}/{name}", data or {})


def event_state() -> dict[str, Any]:
    return ha_request("/api/states/event.voip_stack_call")["attributes"]


def wait_card(
    page, predicate: Callable[[dict[str, Any]], bool], timeout: float, label: str
) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last: dict[str, Any] | None = None
    while time.monotonic() < deadline:
        last = page.evaluate(CARD_STATE)
        if last and predicate(last):
            return last
        page.wait_for_timeout(100)
    raise RuntimeError(
        f"timeout waiting for {label}: {json.dumps(last, ensure_ascii=False)}"
    )


def matching(page, state: str) -> dict[str, Any]:
    return wait_card(
        page,
        lambda item: (
            item["backend"]["state"] == state
            and item["card"]["state"] == state
            and item["backend"]["call_id"] == item["card"]["call_id"]
        ),
        12,
        f"backend/card {state}",
    )


def dial_trunk() -> BareSip:
    caller = BareSip(WILDIX_CONFIG)
    caller.dial("427")
    return caller


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--out", default=str(ROOT / "test_runs" / "ha_softphone_matrix.json")
    )
    parser.add_argument("--only", action="append", default=[])
    args = parser.parse_args()
    results: list[dict[str, Any]] = []
    active: list[BareSip] = []
    automation_was_on = ha_request(f"/api/states/{AUTOMATION}")["state"] == "on"

    def case(name: str, run: Callable[[], dict[str, Any]]) -> None:
        if args.only and name not in args.only:
            return
        started = time.monotonic()
        try:
            detail = run()
            results.append(
                {
                    "name": name,
                    "status": "pass",
                    "seconds": round(time.monotonic() - started, 3),
                    **detail,
                }
            )
        except Exception as error:  # noqa: BLE001 - matrix must continue and report every row.
            results.append(
                {
                    "name": name,
                    "status": "fail",
                    "seconds": round(time.monotonic() - started, 3),
                    "error": str(error),
                }
            )
        finally:
            while active:
                active.pop().close()
            time.sleep(0.5)

    service("automation", "turn_off", {"entity_id": AUTOMATION, "stop_actions": True})
    try:
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
            page = context.new_page()
            page.goto(HA_URL, wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_timeout(5_000)
            card_ready = """() => {
                  const all = (root = document) => {
                    let found = [...root.querySelectorAll('voip-stack-card, intercom-card')];
                    for (const node of root.querySelectorAll('*')) if (node.shadowRoot) found = found.concat(all(node.shadowRoot));
                    return found;
                  };
                  return all().some((card) => (card.config?.mode || card.config?.card_mode || '') === 'ha_softphone');
                }"""
            try:
                page.wait_for_function(card_ready, timeout=30_000)
            except PlaywrightTimeoutError:
                # Directly after an HA restart the dashboard can finish before
                # the integration-owned Lovelace resource is registered. One
                # ordinary reload is the same recovery HA asks of a browser;
                # a second failure remains a real test failure.
                page.reload(wait_until="domcontentloaded", timeout=30_000)
                page.wait_for_function(card_ready, timeout=30_000)
            wait_card(
                page,
                lambda item: item["backend"]["state"] == "idle",
                15,
                "initial idle",
            )
            page.evaluate(SET_AUTO_ANSWER, False)

            def remote_hangup() -> dict[str, Any]:
                caller = dial_trunk()
                active.append(caller)
                ringing = matching(page, "ringing")
                caller.hangup()
                idle = matching(page, "idle")
                if idle["card"]["terminal_reason"] != "remote_hangup":
                    raise RuntimeError(f"wrong terminal reason: {idle}")
                return {
                    "call_id": ringing["card"]["call_id"],
                    "terminal": idle["card"]["terminal_reason"],
                }

            case("trunk_live_ringing_remote_hangup", remote_hangup)

            def refresh_ringing() -> dict[str, Any]:
                caller = dial_trunk()
                active.append(caller)
                ringing = matching(page, "ringing")
                page.reload(wait_until="domcontentloaded")
                page.wait_for_timeout(3_000)
                restored = matching(page, "ringing")
                if restored["card"]["call_id"] != ringing["card"]["call_id"]:
                    raise RuntimeError("refresh changed call_id")
                caller.hangup()
                matching(page, "idle")
                return {"call_id": ringing["card"]["call_id"]}

            case("refresh_during_ringing", refresh_ringing)

            def answer_from_card() -> dict[str, Any]:
                caller = dial_trunk()
                active.append(caller)
                ringing = matching(page, "ringing")
                if not page.evaluate(CLICK, "Answer"):
                    raise RuntimeError("Answer button unavailable")
                answered = matching(page, "in_call")
                caller.hangup()
                matching(page, "idle")
                return {
                    "call_id": ringing["card"]["call_id"],
                    "answered": answered["card"],
                }

            case("manual_answer_from_card", answer_from_card)

            def decline_from_card() -> dict[str, Any]:
                caller = dial_trunk()
                active.append(caller)
                ringing = matching(page, "ringing")
                if not page.evaluate(CLICK, "Decline"):
                    raise RuntimeError("Decline button unavailable")
                idle = matching(page, "idle")
                return {
                    "call_id": ringing["card"]["call_id"],
                    "terminal": idle["card"]["terminal_reason"],
                }

            case("decline_from_card", decline_from_card)

            def auto_answer() -> dict[str, Any]:
                if not page.evaluate(SET_AUTO_ANSWER, True):
                    raise RuntimeError("failed to enable Auto Answer")
                page.wait_for_timeout(500)
                caller = dial_trunk()
                active.append(caller)
                answered = matching(page, "in_call")
                caller.hangup()
                matching(page, "idle")
                page.evaluate(SET_AUTO_ANSWER, False)
                return {"call_id": answered["card"]["call_id"]}

            case("auto_answer", auto_answer)

            def forward_assist() -> dict[str, Any]:
                caller = dial_trunk()
                active.append(caller)
                ringing = matching(page, "ringing")
                service(
                    "voip_stack",
                    "forward",
                    {
                        "destination": "1666",
                        "on_failure": "resume",
                    },
                )
                released = matching(page, "idle")
                if released["card"]["terminal_reason"] != "forwarded":
                    raise RuntimeError(f"forward was not exposed as forwarded: {released}")
                deadline = time.monotonic() + 10
                aggregate = event_state()
                while time.monotonic() < deadline and not (
                    aggregate.get("state") == "in_call"
                    and aggregate.get("callee") == "Troiaio"
                ):
                    time.sleep(0.1)
                    aggregate = event_state()
                if (
                    aggregate.get("state") != "in_call"
                    or aggregate.get("callee") != "Troiaio"
                ):
                    raise RuntimeError(f"Assist did not answer: {aggregate}")
                if aggregate.get("call_id") != ringing["card"]["call_id"]:
                    raise RuntimeError("logical call_id changed during forward")
                caller.hangup()
                matching(page, "idle")
                return {
                    "call_id": aggregate["call_id"],
                    "released": released["card"],
                    "aggregate": aggregate,
                }

            case("forward_releases_ha_and_keeps_call_alive", forward_assist)

            def failed_forward_resume() -> dict[str, Any]:
                caller = dial_trunk()
                active.append(caller)
                ringing = matching(page, "ringing")
                attrs = event_state()
                service(
                    "voip_stack",
                    "forward",
                    {
                        "call_id": ringing["backend"]["call_id"],
                        "destination": "sip:nobody@127.0.0.1:9",
                        "expected_state": attrs["state"],
                        "expected_sequence": attrs["sequence"],
                        "on_failure": "resume",
                    },
                )
                resumed = matching(page, "ringing")
                if resumed["card"]["call_id"] != ringing["card"]["call_id"]:
                    raise RuntimeError("resume changed call_id")
                caller.hangup()
                matching(page, "idle")
                return {"call_id": ringing["card"]["call_id"]}

            case("failed_forward_resumes_ha", failed_forward_resume)

            def two_browsers() -> dict[str, Any]:
                second = context.new_page()
                second.goto(HA_URL, wait_until="domcontentloaded")
                second.wait_for_timeout(4_000)
                caller = dial_trunk()
                active.append(caller)
                first_state = matching(page, "ringing")
                second_state = matching(second, "ringing")
                caller.hangup()
                matching(page, "idle")
                matching(second, "idle")
                second.close()
                if first_state["card"]["call_id"] != second_state["card"]["call_id"]:
                    raise RuntimeError("browser cards observed different calls")
                return {"call_id": first_state["card"]["call_id"]}

            case("two_browser_subscribers", two_browsers)

            def local_registered_sip() -> dict[str, Any]:
                caller = BareSip(LOCAL_CONFIG)
                active.append(caller)
                caller.dial(
                    "sip:Casa@192.168.1.10:5060;transport=tcp", wait_for="180 Ringing"
                )
                ringing = matching(page, "ringing")
                caller.hangup()
                matching(page, "idle")
                return {
                    "call_id": ringing["card"]["call_id"],
                    "caller": ringing["card"]["caller"],
                }

            case("registered_sip_live_ringing", local_registered_sip)

            service("automation", "turn_on", {"entity_id": AUTOMATION})

            def automation_fallback() -> dict[str, Any]:
                caller = dial_trunk()
                active.append(caller)
                ringing = matching(page, "ringing")
                released = wait_card(
                    page,
                    lambda item: (
                        item["backend"]["state"] == "idle"
                        and item["card"]["state"] == "idle"
                    ),
                    45,
                    "automation forward releasing HA",
                )
                deadline = time.monotonic() + 8
                aggregate = event_state()
                while time.monotonic() < deadline and not (
                    aggregate.get("state") == "in_call"
                    and aggregate.get("callee") == "Troiaio"
                ):
                    time.sleep(0.1)
                    aggregate = event_state()
                if (
                    aggregate.get("state") != "in_call"
                    or aggregate.get("callee") != "Troiaio"
                ):
                    raise RuntimeError(f"automation fallback failed: {aggregate}")
                if released["card"]["terminal_reason"] not in {"", "forwarded"}:
                    raise RuntimeError(
                        f"forward exposed an unexpected terminal reason: {released}"
                    )
                caller.hangup()
                matching(page, "idle")
                return {"call_id": ringing["card"]["call_id"], "aggregate": aggregate}

            case("single_automation_30s_fallback", automation_fallback)
            context.close()
            browser.close()
    finally:
        restore_data: dict[str, Any] = {"entity_id": AUTOMATION}
        if not automation_was_on:
            restore_data["stop_actions"] = True
        service(
            "automation", "turn_on" if automation_was_on else "turn_off", restore_data
        )
        for caller in active:
            caller.close()
        for directory in (Path("/home/codex"), ROOT):
            for path in directory.glob("dump-sip:*.wav"):
                path.unlink(missing_ok=True)

    Path(args.out).write_text(json.dumps(results, indent=2, ensure_ascii=False))
    print(json.dumps(results, indent=2, ensure_ascii=False))
    return 1 if any(item["status"] != "pass" for item in results) else 0


if __name__ == "__main__":
    raise SystemExit(main())
