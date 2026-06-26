#!/usr/bin/env python3
"""Visual smoke checks for Intercom Native Lovelace cards."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from playwright.async_api import async_playwright


BASE_URL = "https://f0260ef3d722.sn.mynetname.net"
USER = "codex"
PASSWORD = "Codex-2026!"
OUT = Path("test_runs/playwright")


async def _login(page) -> None:
    await page.goto(f"{BASE_URL}/lovelace/default_view", wait_until="domcontentloaded")
    if "auth/authorize" not in page.url and await page.locator("home-assistant").count():
        return
    await page.screenshot(path=OUT / "login.png", full_page=True)
    user_input = page.locator('input[name="username"], input[type="text"], ha-textfield').first
    pass_input = page.locator('input[name="password"], input[type="password"]').first
    await user_input.fill(USER)
    await pass_input.fill(PASSWORD)
    await pass_input.press("Enter")
    await page.wait_for_load_state("networkidle")


async def _snapshot(page, name: str) -> dict:
    await page.screenshot(path=OUT / f"{name}.png", full_page=True)
    data = await page.evaluate(
        """() => {
          const texts = [];
          const selects = [];
          const buttons = [];
          const visit = (root) => {
            if (!root) return;
            const walker = document.createTreeWalker(root, NodeFilter.SHOW_ELEMENT | NodeFilter.SHOW_TEXT);
            let node;
            while ((node = walker.nextNode())) {
              if (node.nodeType === Node.TEXT_NODE) {
                const text = node.textContent && node.textContent.trim();
                if (text) texts.push(text);
                continue;
              }
              if (node.localName === 'select') {
                selects.push({
                  value: node.value,
                  options: Array.from(node.options).map(o => o.textContent.trim()).filter(Boolean)
                });
              }
              if (node.localName === 'button') {
                const text = node.textContent && node.textContent.trim();
                buttons.push({
                  text: text || '',
                  aria: node.getAttribute('aria-label') || '',
                  title: node.getAttribute('title') || '',
                  disabled: !!node.disabled
                });
              }
              if (node.shadowRoot) visit(node.shadowRoot);
            }
          };
          visit(document);
          return {text: texts.join('\\n'), selects, buttons};
        }"""
    )
    return {"url": page.url, **data}


async def main() -> int:
    OUT.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page(viewport={"width": 1440, "height": 1100}, ignore_https_errors=True)
        await _login(page)

        results: dict[str, dict] = {}
        for name, path in (
            ("ha_softphone", "/lovelace/default_view"),
            ("esp_mirror", "/dashboard-intercom/0"),
        ):
            await page.goto(f"{BASE_URL}{path}", wait_until="networkidle")
            await page.wait_for_timeout(2000)
            data = await _snapshot(page, name)
            results[name] = data
            lowered = data["text"].lower()
            if "no endpoint" in lowered or "no endpoints" in lowered:
                raise AssertionError(f"{name}: card reports no endpoint")
            if "intercom" not in lowered:
                raise AssertionError(f"{name}: no visible intercom content")
            if not any(sel["options"] for sel in data["selects"]):
                raise AssertionError(f"{name}: no populated select controls")

        (OUT / "ha_card_visual_smoke.json").write_text(json.dumps(results, indent=2), encoding="utf-8")
        await browser.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
