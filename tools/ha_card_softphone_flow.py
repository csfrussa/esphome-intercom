#!/usr/bin/env python3
"""Playwright checks for HA softphone card actions during SIP calls."""

from __future__ import annotations

import asyncio
import argparse
import json
from pathlib import Path
import ssl
import time
import urllib.error
import urllib.request
from typing import Any

from playwright.async_api import Page, async_playwright


BASE_URL = "https://f0260ef3d722.sn.mynetname.net"
TOKEN_FILE = Path("/tmp/ha_token_codex")
OUT = Path("test_runs/playwright/ha_softphone_flow")

HA_SOFTPHONE_DEVICE_ID = "__intercom_native_ha_softphone__"
WS3_DEVICE_ID = "35bb14eb59bd920b964b61d0b0f1b8fc"
WS3_STATE = "sensor.waveshare_s3_audio_intercom_state"
WS3_CALLER = "sensor.waveshare_s3_audio_caller"
WS3_DEST = "sensor.waveshare_s3_audio_destination"

DEVICE_PRESETS = {
    "ws3": {
        "device_id": "35bb14eb59bd920b964b61d0b0f1b8fc",
        "state": "sensor.waveshare_s3_audio_intercom_state",
        "caller": "sensor.waveshare_s3_audio_caller",
        "destination": "sensor.waveshare_s3_audio_destination",
    },
    "spotpear": {
        "device_id": "df18a94e7c6ebcb84b183ac7c081805d",
        "state": "sensor.intercom_xiaozhi_intercom_state",
        "caller": "sensor.intercom_xiaozhi_caller",
        "destination": "sensor.intercom_xiaozhi_destination",
    },
}


class HaRest:
    def __init__(self, token: str) -> None:
        self.token = token
        self.ssl_context = ssl._create_unverified_context()

    def request(self, method: str, path: str, data: dict[str, Any] | None = None) -> Any:
        raw = None
        headers = {"Authorization": f"Bearer {self.token}"}
        if data is not None:
            raw = json.dumps(data).encode()
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(f"{BASE_URL}{path}", data=raw, headers=headers, method=method)
        try:
            with urllib.request.urlopen(req, timeout=10, context=self.ssl_context) as resp:
                body = resp.read().decode()
        except urllib.error.HTTPError as err:
            detail = err.read().decode(errors="replace")
            raise AssertionError(f"HA {method} {path} failed: {err.code} {detail}") from err
        return json.loads(body) if body else None

    async def state(self, entity_id: str) -> dict[str, Any]:
        return await asyncio.to_thread(self.request, "GET", f"/api/states/{entity_id}")

    async def service(self, domain: str, service: str, data: dict[str, Any] | None = None) -> Any:
        return await asyncio.to_thread(self.request, "POST", f"/api/services/{domain}/{service}", data or {})


async def wait_entity(ha: HaRest, entity_id: str, wanted: set[str], timeout: float, label: str) -> dict[str, Any]:
    deadline = time.monotonic() + timeout
    last = None
    while time.monotonic() < deadline:
        last = await ha.state(entity_id)
        if str(last.get("state")) in wanted:
            return last
        await asyncio.sleep(0.15)
    raise AssertionError(f"{label}: expected {sorted(wanted)}, last={last}")


async def ha_softphone_state(page: Page) -> dict[str, Any]:
    deadline = time.monotonic() + 12.0
    last_error = None
    while time.monotonic() < deadline:
        try:
            return await page.evaluate(
                """async () => {
          const cards = [];
          const visit = (root) => {
            if (!root) return;
            const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
            let node;
            while ((node = walker.nextNode())) {
              if (node.localName === 'intercom-card') cards.push(node);
              if (node.shadowRoot) visit(node.shadowRoot);
            }
          };
          visit(document);
          const card = cards.find(c => (c.config?.mode || c.config?.card_mode) === 'ha_softphone');
          if (!card?._hass?.connection) throw new Error('HA softphone card not found');
          return await card._hass.connection.sendMessagePromise({type: 'intercom_native/ha_softphone_state'});
        }"""
            )
        except Exception as err:  # noqa: BLE001 - report the last browser-side reason.
            last_error = err
            await page.wait_for_timeout(300)
    raise AssertionError(f"HA softphone card not ready: {last_error}")


async def click_card_button(page: Page, *, mode: str | None = None, name: str | None = None, text: str) -> None:
    ok = await page.evaluate(
        """({mode, name, text}) => {
          const cards = [];
          const visit = (root) => {
            if (!root) return;
            const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
            let node;
            while ((node = walker.nextNode())) {
              if (node.localName === 'intercom-card') cards.push(node);
              if (node.shadowRoot) visit(node.shadowRoot);
            }
          };
          visit(document);
          const card = cards.find(c => {
            if (mode && (c.config?.mode || c.config?.card_mode) !== mode) return false;
            if (name && c.config?.name !== name) return false;
            return true;
          });
          if (!card || !card.shadowRoot) return false;
          const btn = Array.from(card.shadowRoot.querySelectorAll('button'))
            .find(b => (b.textContent || '').trim() === text && !b.disabled);
          if (!btn) return false;
          btn.click();
          return true;
        }""",
        {"mode": mode, "name": name, "text": text},
    )
    if not ok:
        raise AssertionError(f"button {text!r} not found on card mode={mode!r} name={name!r}")


async def snapshot(page: Page, name: str, extra: dict[str, Any]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    await page.screenshot(path=OUT / f"{name}.png", full_page=True)
    data = await page.evaluate(
        """() => {
          const cards = [];
          const visit = (root) => {
            if (!root) return;
            const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT);
            let node;
            while ((node = walker.nextNode())) {
              if (node.localName === 'intercom-card') {
                const r = node.shadowRoot;
                cards.push({
                  config: node.config || null,
                  text: r ? (r.textContent || '') : (node.textContent || ''),
                  buttons: r ? Array.from(r.querySelectorAll('button')).map(b => ({
                    text: (b.textContent || '').trim(), disabled: b.disabled, className: String(b.className)
                  })) : [],
                  selects: r ? Array.from(r.querySelectorAll('select')).map(s => ({
                    value: s.value,
                    options: Array.from(s.options).map(o => ({value: o.value, text: o.textContent}))
                  })) : [],
                });
              }
              if (node.shadowRoot) visit(node.shadowRoot);
            }
          };
          visit(document);
          return cards;
        }"""
    )
    (OUT / f"{name}.json").write_text(json.dumps({"extra": extra, "cards": data}, indent=2, ensure_ascii=False), encoding="utf-8")


async def open_page(page: Page, path: str) -> None:
    await page.goto(f"{BASE_URL}{path}", wait_until="networkidle")
    await page.wait_for_timeout(2500)


async def cleanup(ha: HaRest) -> None:
    try:
        await ha.service("intercom_native", "sip_hangup", {})
    except AssertionError as err:
        if "400" not in str(err):
            raise
    try:
        await ha.service("intercom_native", "decline", {"device_id": WS3_DEVICE_ID})
    except AssertionError as err:
        if "400" not in str(err):
            raise
    await asyncio.sleep(0.5)


async def run_decline_flow(page: Page, ha: HaRest) -> None:
    await cleanup(ha)
    await wait_entity(ha, WS3_STATE, {"Idle"}, 8, "ws3 initial idle")
    await open_page(page, "/dashboard-intercom/0")
    await snapshot(page, "decline_01_dashboard_idle", {"ws3": await ha.state(WS3_STATE), "dest": await ha.state(WS3_DEST)})
    await ha.service("intercom_native", "call", {"source": WS3_DEVICE_ID, "destination": "Casa"})
    await wait_entity(ha, WS3_STATE, {"Calling"}, 8, "ws3 outgoing to HA")
    await open_page(page, "/lovelace/default_view")
    state = await ha_softphone_state(page)
    if str(state.get("state")).lower() not in {"ringing", "incoming"}:
        raise AssertionError(f"HA softphone did not enter ringing: {state}")
    await snapshot(page, "decline_02_ha_ringing", {"ha_softphone": state, "ws3": await ha.state(WS3_STATE)})
    await click_card_button(page, mode="ha_softphone", text="Decline")
    await wait_entity(ha, WS3_STATE, {"Idle"}, 10, "ws3 idle after HA card decline")
    await asyncio.sleep(0.6)
    state = await ha_softphone_state(page)
    await snapshot(page, "decline_03_after_decline", {"ha_softphone": state, "ws3": await ha.state(WS3_STATE)})
    if str(state.get("state")).lower() not in {"idle", "declined"}:
        raise AssertionError(f"HA softphone did not terminate after decline: {state}")


async def run_answer_flow(page: Page, ha: HaRest) -> None:
    await cleanup(ha)
    await wait_entity(ha, WS3_STATE, {"Idle"}, 8, "ws3 initial idle")
    await ha.service("intercom_native", "call", {"source": WS3_DEVICE_ID, "destination": "Casa"})
    await wait_entity(ha, WS3_STATE, {"Calling"}, 8, "ws3 outgoing before answer")
    await open_page(page, "/lovelace/default_view")
    state = await ha_softphone_state(page)
    if str(state.get("state")).lower() not in {"ringing", "incoming"}:
        raise AssertionError(f"HA softphone did not enter ringing before answer: {state}")
    await snapshot(page, "answer_01_ha_ringing", {"ha_softphone": state, "ws3": await ha.state(WS3_STATE)})
    await click_card_button(page, mode="ha_softphone", text="Answer")
    await wait_entity(ha, WS3_STATE, {"In Call"}, 12, "ws3 in_call after HA card answer")
    await asyncio.sleep(1.0)
    state = await ha_softphone_state(page)
    await snapshot(page, "answer_02_in_call", {"ha_softphone": state, "ws3": await ha.state(WS3_STATE)})
    if str(state.get("state")).lower() != "in_call":
        raise AssertionError(f"HA softphone did not enter in_call after answer: {state}")
    await click_card_button(page, mode="ha_softphone", text="Hangup")
    await wait_entity(ha, WS3_STATE, {"Idle"}, 12, "ws3 idle after HA card hangup")
    await asyncio.sleep(0.8)
    state = await ha_softphone_state(page)
    await snapshot(page, "answer_03_after_hangup", {"ha_softphone": state, "ws3": await ha.state(WS3_STATE)})
    if str(state.get("state")).lower() not in {"idle", "disconnected"}:
        raise AssertionError(f"HA softphone did not terminate after hangup: {state}")


async def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--device", choices=sorted(DEVICE_PRESETS), default="ws3")
    args = parser.parse_args()
    preset = DEVICE_PRESETS[args.device]
    global WS3_DEVICE_ID, WS3_STATE, WS3_CALLER, WS3_DEST
    WS3_DEVICE_ID = preset["device_id"]
    WS3_STATE = preset["state"]
    WS3_CALLER = preset["caller"]
    WS3_DEST = preset["destination"]

    OUT.mkdir(parents=True, exist_ok=True)
    token = TOKEN_FILE.read_text().strip()
    ha = HaRest(token)
    init = f"""
      const tokens = {{
        hassUrl: {BASE_URL!r},
        clientId: {(BASE_URL + '/')!r},
        access_token: {token!r},
        token_type: 'Bearer',
        expires_in: 315360000,
        expires: Date.now() + 315360000000,
        refresh_token: 'codex-long-lived-token'
      }};
      localStorage.setItem('hassTokens', JSON.stringify(tokens));
      localStorage.setItem('selectedLanguage', JSON.stringify('en'));
    """
    async with async_playwright() as p:
        browser = await p.chromium.launch(
            headless=True,
            args=["--use-fake-ui-for-media-stream", "--use-fake-device-for-media-stream"],
        )
        context = await browser.new_context(ignore_https_errors=True, viewport={"width": 1440, "height": 1100})
        await context.add_init_script(init)
        await context.grant_permissions(["microphone"], origin=BASE_URL)
        page = await context.new_page()
        page.on("console", lambda msg: print(f"BROWSER {msg.type}: {msg.text}"))
        await run_decline_flow(page, ha)
        await run_answer_flow(page, ha)
        await cleanup(ha)
        await browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
