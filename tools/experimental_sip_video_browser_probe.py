#!/usr/bin/env python3
"""Exercise an incoming or outgoing experimental SIP video call on the HA card.

The caller is intentionally external to this process (bareSIP or a real video
phone).  Start the probe, place the call while it is waiting, and let the probe
answer through the actual Lovelace card.  The resulting JSON records backend,
card, WebCodecs and canvas evidence rather than relying on visual inspection.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from urllib.parse import urlsplit

from playwright.sync_api import sync_playwright


DEFAULT_URL = os.environ.get("HA_URL", "")
DEFAULT_STORAGE_STATE = os.environ.get("PLAYWRIGHT_STORAGE_STATE", "")
DEFAULT_CHROMIUM = os.environ.get("CHROMIUM_PATH", "")

DEEP_CARD = r"""
() => {
  const deep = (selector, root = document) => {
    const found = [...root.querySelectorAll(selector)];
    for (const node of root.querySelectorAll("*")) {
      if (node.shadowRoot) found.push(...deep(selector, node.shadowRoot));
    }
    return found;
  };
  return deep("voip-stack-card, intercom-card")
    .find((card) => (card.config?.mode || card.config?.card_mode || "") === "ha_softphone");
}
"""

CARD_SAMPLE = r"""
() => {
  const deep = (selector, root = document) => {
    const found = [...root.querySelectorAll(selector)];
    for (const node of root.querySelectorAll("*")) {
      if (node.shadowRoot) found.push(...deep(selector, node.shadowRoot));
    }
    return found;
  };
  const card = deep("voip-stack-card, intercom-card")
    .find((item) => (item.config?.mode || item.config?.card_mode || "") === "ha_softphone");
  if (!card) return null;
  const snapshot = card._softphoneSnapshot || {};
  const canvas = deep("canvas.video-canvas", card.shadowRoot || card)[0] || null;
  let canvasEvidence = null;
  if (canvas && canvas.width && canvas.height) {
    const context = canvas.getContext("2d", { willReadFrequently: true });
    const points = [
      [0, 0],
      [Math.floor(canvas.width / 2), Math.floor(canvas.height / 2)],
      [Math.max(0, canvas.width - 1), Math.max(0, canvas.height - 1)],
    ];
    const pixels = points.map(([x, y]) => [...context.getImageData(x, y, 1, 1).data]);
    canvasEvidence = {
      width: canvas.width,
      height: canvas.height,
      hidden: canvas.hidden,
      pixels,
      non_black: pixels.some((pixel) => pixel[0] || pixel[1] || pixel[2]),
    };
  }
  return {
    card_state: String(snapshot.state || ""),
    call_id: String(snapshot.call_id || ""),
    video_active: Boolean(snapshot.video_active),
    video_offered: Boolean(snapshot.video_offered),
    video_format: String(snapshot.video_format || ""),
    video_direction: String(snapshot.video_direction || ""),
    engine_state: String(globalThis.__voipStackEngine?.state || ""),
    engine_device_id: String(globalThis.__voipStackEngine?.deviceId || ""),
    engine_call_id: String(globalThis.__voipStackEngine?.callId || ""),
    engine_video_active: Boolean(globalThis.__voipStackEngine?.videoActive),
    engine_video_visible: Boolean(globalThis.__voipStackEngine?.videoVisible),
    owns_current_call: Boolean(globalThis.__voipStackEngine?.ownsSoftphoneSession?.(snapshot.call_id)),
    starting: Boolean(card._starting),
    stopping: Boolean(card._stopping),
    has_audio_attach_task: Boolean(card._audioAttachTask),
    has_cleanup_task: Boolean(card._cleanupTask),
    engine_stats: globalThis.__voipStackEngine?.stats || null,
    canvas: canvasEvidence,
  };
}
"""

CLICK_ANSWER = r"""
() => {
  const deep = (selector, root = document) => {
    const found = [...root.querySelectorAll(selector)];
    for (const node of root.querySelectorAll("*")) {
      if (node.shadowRoot) found.push(...deep(selector, node.shadowRoot));
    }
    return found;
  };
  const card = deep("voip-stack-card, intercom-card")
    .find((item) => (item.config?.mode || item.config?.card_mode || "") === "ha_softphone");
  const button = card && deep("button", card.shadowRoot || card)
    .find((item) => item.textContent.trim().toLowerCase() === "answer" && !item.hidden && !item.disabled);
  if (!button) return false;
  button.click();
  return true;
}
"""

CLICK_HANGUP = CLICK_ANSWER.replace('=== "answer"', '=== "hangup"')

START_OUTBOUND = r"""
async (destination) => {
  const deep = (selector, root = document) => {
    const found = [...root.querySelectorAll(selector)];
    for (const node of root.querySelectorAll("*")) {
      if (node.shadowRoot) found.push(...deep(selector, node.shadowRoot));
    }
    return found;
  };
  const card = deep("voip-stack-card, intercom-card")
    .find((item) => (item.config?.mode || item.config?.card_mode || "") === "ha_softphone");
  if (!card) return false;
  card._softphoneKeypadOpen = true;
  card._softphoneManualTarget = String(destination || "");
  await card._startCall();
  return true;
}
"""


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--url",
        default=DEFAULT_URL,
        help="authenticated Home Assistant dashboard URL (or set HA_URL)",
    )
    parser.add_argument(
        "--storage-state",
        default=DEFAULT_STORAGE_STATE,
        help="Playwright storage-state JSON for an authenticated HA user",
    )
    parser.add_argument(
        "--chromium",
        default=DEFAULT_CHROMIUM,
        help="optional Chromium executable path (or set CHROMIUM_PATH)",
    )
    parser.add_argument("--ring-timeout", type=float, default=60)
    parser.add_argument("--video-timeout", type=float, default=25)
    parser.add_argument("--hold-seconds", type=float, default=8)
    parser.add_argument("--no-hangup", action="store_true")
    parser.add_argument(
        "--reload-during-ring",
        action="store_true",
        help="reload the HA page while the outbound call is ringing",
    )
    parser.add_argument(
        "--reload-in-call",
        action="store_true",
        help="reload the HA page after the audio/video dialog is connected",
    )
    parser.add_argument(
        "--outbound",
        metavar="DESTINATION",
        help="originate from the card instead of waiting for an incoming call",
    )
    parser.add_argument(
        "--out",
        default="/tmp/experimental_sip_video_browser_probe.json",
    )
    args = parser.parse_args()
    if not args.url:
        parser.error("--url or HA_URL is required")
    if not args.storage_state:
        parser.error("--storage-state or PLAYWRIGHT_STORAGE_STATE is required")
    storage_state = Path(args.storage_state).expanduser()
    if not storage_state.is_file():
        parser.error(f"Playwright storage state does not exist: {storage_state}")
    parsed_url = urlsplit(args.url)
    if parsed_url.scheme not in {"http", "https"} or not parsed_url.netloc:
        parser.error("--url must be an absolute HTTP(S) Home Assistant URL")
    origin = f"{parsed_url.scheme}://{parsed_url.netloc}"

    console: list[str] = []
    result: dict = {"samples": [], "console": console}
    failure: BaseException | None = None
    with sync_playwright() as playwright:
        launch_options = {
            "headless": True,
            "args": [
                "--use-fake-ui-for-media-stream",
                "--use-fake-device-for-media-stream",
                "--autoplay-policy=no-user-gesture-required",
            ],
        }
        if args.chromium:
            launch_options["executable_path"] = args.chromium
        browser = playwright.chromium.launch(
            **launch_options,
        )
        context = browser.new_context(storage_state=str(storage_state))
        context.grant_permissions(["camera", "microphone"], origin=origin)
        page = context.new_page()
        page.on("console", lambda msg: console.append(f"{msg.type}: {msg.text}"))
        page.on("pageerror", lambda error: console.append(f"pageerror: {error}"))
        page.goto(args.url, wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_timeout(5_000)
        page.wait_for_function(f"() => Boolean(({DEEP_CARD})())", timeout=30_000)

        def sample(label: str) -> dict:
            item = page.evaluate(CARD_SAMPLE) or {}
            item["label"] = label
            result["samples"].append(item)
            print(json.dumps(item, separators=(",", ":")), flush=True)
            return item

        try:
            sample("ready")
            if args.outbound:
                print(f"PLACING_VIDEO_CALL {args.outbound}", flush=True)
                if not page.evaluate(START_OUTBOUND, args.outbound):
                    raise RuntimeError("HA softphone card could not start the outbound call")
                page.wait_for_function(
                    f"() => ['calling','connecting','remote_ringing','in_call'].includes((({CARD_SAMPLE})()?.card_state || '').toLowerCase())",
                    timeout=15_000,
                )
                sample("outbound_progress")
                if args.reload_during_ring:
                    page.reload(wait_until="domcontentloaded", timeout=30_000)
                    page.wait_for_function(f"() => Boolean(({DEEP_CARD})())", timeout=30_000)
                    page.wait_for_function(
                        f"() => ['calling','connecting','remote_ringing','in_call'].includes((({CARD_SAMPLE})()?.card_state || '').toLowerCase())",
                        timeout=15_000,
                    )
                    sample("outbound_after_reload")
                print("WAITING_FOR_REMOTE_ANSWER", flush=True)
            else:
                print("WAITING_FOR_VIDEO_CALL", flush=True)
                page.wait_for_function(
                    f"() => (({CARD_SAMPLE})()?.card_state || '').toLowerCase() === 'ringing'",
                    timeout=int(args.ring_timeout * 1000),
                )
                ringing = sample("ringing")
                if not ringing.get("video_offered"):
                    raise RuntimeError(f"incoming call did not offer video: {ringing}")
                if not page.evaluate(CLICK_ANSWER):
                    raise RuntimeError("visible Answer button not found")
            page.wait_for_function(
                f"() => (({CARD_SAMPLE})()?.card_state || '').toLowerCase() === 'in_call'",
                timeout=int(args.ring_timeout * 1000),
            )
            sample("in_call")
            if args.reload_in_call:
                page.reload(wait_until="domcontentloaded", timeout=30_000)
                page.wait_for_function(f"() => Boolean(({DEEP_CARD})())", timeout=30_000)
                page.wait_for_function(
                    f"() => (({CARD_SAMPLE})()?.card_state || '').toLowerCase() === 'in_call'",
                    timeout=15_000,
                )
                sample("in_call_after_reload")
            page.wait_for_function(
                f"() => {{ const x = ({CARD_SAMPLE})(); const d = String(x?.video_direction || 'sendrecv'); const v = x?.engine_stats?.video || {{}}; const rx = !['recvonly','sendrecv'].includes(d) || (v.received > 0 && x?.canvas?.width > 0); const tx = !['sendonly','sendrecv'].includes(d) || v.sent > 0; return x?.engine_video_active && rx && tx; }}",
                timeout=int(args.video_timeout * 1000),
            )
            page.wait_for_timeout(int(args.hold_seconds * 1000))
            active = sample("video_flowing")
            video_stats = (active.get("engine_stats") or {}).get("video") or {}
            direction = str(active.get("video_direction") or "sendrecv")
            expects_receive = direction in {"recvonly", "sendrecv"}
            expects_send = direction in {"sendonly", "sendrecv"}
            if expects_receive and video_stats.get("received", 0) <= 0:
                raise RuntimeError(f"no remote video access units reached WebCodecs: {active}")
            if expects_send and video_stats.get("sent", 0) <= 0:
                raise RuntimeError(f"no browser video access units returned to SIP: {active}")
            if expects_receive and not (active.get("canvas") or {}).get("non_black"):
                raise RuntimeError(f"decoded canvas has no non-black sample: {active}")
            if not args.no_hangup:
                if not page.evaluate(CLICK_HANGUP):
                    raise RuntimeError("visible Hangup button not found")
                page.wait_for_function(
                    f"() => (({CARD_SAMPLE})()?.card_state || '').toLowerCase() === 'idle'",
                    timeout=15_000,
                )
                sample("idle_after_hangup")
            result["ok"] = True
        except BaseException as err:  # Persist browser evidence before re-raising.
            failure = err
            result["ok"] = False
            result["error"] = f"{type(err).__name__}: {err}"
            try:
                sample("failure")
            except BaseException:
                pass
        finally:
            Path(args.out).write_text(json.dumps(result, indent=2, ensure_ascii=False))
            for line in console:
                print(f"BROWSER {line}", flush=True)
            context.close()
            browser.close()
    if failure is not None:
        raise failure
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
