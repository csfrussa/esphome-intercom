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
import time
from urllib.parse import urlsplit

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright


DEFAULT_URL = os.environ.get("HA_URL", "")
DEFAULT_STORAGE_STATE = os.environ.get("PLAYWRIGHT_STORAGE_STATE", "")
DEFAULT_CHROMIUM = os.environ.get("CHROMIUM_PATH", "")


class _ProbeComplete(Exception):
    """Internal successful short-circuit for a pre-answer cancellation test."""

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

BACKEND_SAMPLE = r"""
async () => {
  const hass = document.querySelector("home-assistant")?.hass;
  if (!hass?.connection) return {};
  return await hass.connection.sendMessagePromise({
    type: "voip_stack/ha_softphone_state",
  });
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
  const root = card.shadowRoot || card;
  const surface = deep("ha-card.card", root)[0] || null;
  const canvas = deep("canvas.video-canvas", card.shadowRoot || card)[0] || null;
  const hangup = deep("button.hangup", root)[0] || null;
  const header = deep(".header", root)[0] || null;
  const stats = deep(".hangup-stats", root)[0] || null;
  const rect = (element, relativeTo = null) => {
    if (!element || element.hidden) return null;
    const value = element.getBoundingClientRect();
    const base = relativeTo?.getBoundingClientRect?.() || { left: 0, top: 0 };
    return {
      x: value.left - base.left,
      y: value.top - base.top,
      width: value.width,
      height: value.height,
      right: value.right - base.left,
      bottom: value.bottom - base.top,
    };
  };
  const surfaceRect = rect(surface);
  const canvasRect = rect(canvas, surface);
  const hangupRect = rect(hangup, surface);
  const headerRect = rect(header, surface);
  const statsRect = rect(stats, surface);
  const overlaps = (left, right) => Boolean(
    left && right && left.x < right.right && left.right > right.x
      && left.y < right.bottom && left.bottom > right.y
  );
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
    debug_mode: Boolean(snapshot.debug_mode),
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
    video_debug: globalThis.__voipStackEngine?._video ? {
      frame_queue: (globalThis.__voipStackEngine._video._frameQueue || []).map((frame) => Number(frame.timestamp || 0)),
      render_handle: Number(globalThis.__voipStackEngine._video._renderHandle || 0),
      playout_base_wall: Number(globalThis.__voipStackEngine._video._playoutBaseWall || 0),
      playout_base_timestamp: Number(globalThis.__voipStackEngine._video._playoutBaseTimestamp || 0),
      last_rendered_timestamp: Number(globalThis.__voipStackEngine._video._lastRenderedTimestamp || 0),
      last_decoded_timestamp: Number(globalThis.__voipStackEngine._video._lastDecodedTimestamp || 0),
      performance_now: performance.now(),
    } : null,
    layout: surface ? {
      surface: surfaceRect,
      canvas: canvasRect,
      hangup: hangupRect,
      header: headerRect,
    stats: statsRect,
      horizontal_overflow: surface.scrollWidth > surface.clientWidth + 1,
      vertical_overflow: surface.scrollHeight > surface.clientHeight + 1,
    header_stats_overlap: overlaps(headerRect, statsRect),
    stats_outside_hangup: Boolean(
      statsRect && hangupRect && (
        statsRect.x < hangupRect.x || statsRect.right > hangupRect.right
          || statsRect.y < hangupRect.y || statsRect.bottom > hangupRect.bottom
      )
    ),
      usable_video_height: Math.max(
        0,
        Number(hangupRect?.y || surface.clientHeight)
          - Math.max(Number(headerRect?.bottom || 0), Number(statsRect?.bottom || 0)),
      ),
    } : null,
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

CLICK_HANGUP = r"""
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
    .find((item) => item.classList.contains("hangup") && !item.hidden && !item.disabled);
  if (!button) return false;
  button.click();
  return true;
}
"""

CLICK_CAMERA_SEND = r"""
async () => {
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
  if (globalThis.__voipStackEngine?.videoCameraEnabled) return true;
  const root = card.shadowRoot || card;
  const settings = deep("button", root).find((item) => item.textContent.trim() === "Options");
  if (settings && !card._settingsOpen) {
    settings.click();
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  const checkbox = deep("#ha-softphone-video-camera-cb", root)[0];
  if (!checkbox || checkbox.closest("[hidden]")) return false;
  if (!checkbox.checked) {
    checkbox.click();
    await new Promise((resolve) => setTimeout(resolve, 50));
  }
  return Boolean(globalThis.__voipStackEngine?.videoCameraEnabled && checkbox.checked);
}
"""

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
    parser.add_argument("--viewport-width", type=int, default=1280)
    parser.add_argument("--viewport-height", type=int, default=900)
    parser.add_argument(
        "--sample-interval",
        type=float,
        default=0,
        help="record intermediate runtime samples at this interval while holding the call",
    )
    parser.add_argument("--no-hangup", action="store_true")
    parser.add_argument(
        "--expect-remote-hangup",
        action="store_true",
        help="wait for the SIP peer to end the established dialog",
    )
    parser.add_argument(
        "--send-camera",
        action="store_true",
        help="enable the card's Send Camera checkbox after the dialog connects",
    )
    parser.add_argument(
        "--deny-camera",
        action="store_true",
        help=(
            "enable Send Camera but make browser camera acquisition fail; "
            "incoming video and audio must remain usable"
        ),
    )
    parser.add_argument(
        "--expect-audio-only",
        action="store_true",
        help="require a working browser audio call with no active video path",
    )
    parser.add_argument(
        "--screenshot",
        help="optional screenshot path captured while video is flowing",
    )
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
        "--cancel-during-ring",
        action="store_true",
        help="cancel an outbound INVITE before the remote endpoint answers",
    )
    parser.add_argument(
        "--out",
        default="/tmp/experimental_sip_video_browser_probe.json",
    )
    args = parser.parse_args()
    if args.expect_audio_only and (args.send_camera or args.deny_camera):
        parser.error("--expect-audio-only cannot be combined with camera options")
    if args.expect_remote_hangup and args.no_hangup:
        parser.error("--expect-remote-hangup cannot be combined with --no-hangup")
    if args.cancel_during_ring and not args.outbound:
        parser.error("--cancel-during-ring requires --outbound")
    if args.deny_camera:
        args.send_camera = True
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
        context = browser.new_context(
            storage_state=str(storage_state),
            viewport={"width": args.viewport_width, "height": args.viewport_height},
        )
        context.grant_permissions(["camera", "microphone"], origin=origin)
        if args.deny_camera:
            context.add_init_script(
                """
                (() => {
                  const original = navigator.mediaDevices.getUserMedia.bind(navigator.mediaDevices);
                  navigator.mediaDevices.getUserMedia = (constraints = {}) => {
                    if (constraints && constraints.video) {
                      return Promise.reject(new DOMException(
                        "Camera permission denied by qualification probe",
                        "NotAllowedError",
                      ));
                    }
                    return original(constraints);
                  };
                })();
                """
            )
        page = context.new_page()
        page.on("console", lambda msg: console.append(f"{msg.type}: {msg.text}"))
        page.on("pageerror", lambda error: console.append(f"pageerror: {error}"))
        page.goto(args.url, wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_timeout(5_000)
        try:
            page.wait_for_function(f"() => Boolean(({DEEP_CARD})())", timeout=15_000)
        except PlaywrightTimeoutError:
            # A lab browser can reach Lovelace during the short interval in
            # which HA is already serving HTTP but the custom-card resource
            # has not yet been registered. Reload once after integration
            # startup instead of turning that harmless race into a false
            # qualification failure.
            page.reload(wait_until="domcontentloaded", timeout=30_000)
            page.wait_for_function(f"() => Boolean(({DEEP_CARD})())", timeout=30_000)

        def sample(label: str) -> dict:
            item = page.evaluate(CARD_SAMPLE) or {}
            item["backend_state"] = page.evaluate(BACKEND_SAMPLE) or {}
            item["label"] = label
            result["samples"].append(item)
            print(json.dumps(item, separators=(",", ":")), flush=True)
            return item

        def wait_for_idle_cleanup(label: str) -> dict:
            page.wait_for_function(
                f"""() => {{
                  const x = ({CARD_SAMPLE})();
                  if (!x || String(x.card_state || '').toLowerCase() !== 'idle') return false;
                  if (x.engine_call_id || x.engine_video_active || x.engine_video_visible) return false;
                  if (x.has_audio_attach_task || x.has_cleanup_task) return false;
                  return true;
                }}""",
                timeout=15_000,
            )
            deadline = time.monotonic() + 15
            backend = {}
            while time.monotonic() < deadline:
                backend = page.evaluate(BACKEND_SAMPLE) or {}
                if not backend.get("debug_mode"):
                    break
                debug = backend.get("media_debug") or {}
                registry = debug.get("call_registry") or {}
                if (
                    int(registry.get("sessions") or 0) == 0
                    and int(registry.get("active_sessions") or 0) == 0
                    and not registry.get("pending_call_ids")
                    and not registry.get("media_call_ids")
                    and not registry.get("bridge_call_ids")
                    and not debug.get("audio_ws_owner_call_ids")
                    and not debug.get("video_ws_owner_call_ids")
                    and not debug.get("video_transcoder_call_id")
                ):
                    break
                page.wait_for_timeout(100)
            else:
                raise RuntimeError(f"backend resources survived teardown: {backend}")
            cleaned = sample(label)
            backend = cleaned.get("backend_state") or {}
            if backend.get("pending_transactions") or backend.get("active_dialogs"):
                raise RuntimeError(f"SIP transactions or dialogs survived teardown: {cleaned}")
            if backend.get("pending_call_ids") or backend.get("active_call_ids"):
                raise RuntimeError(f"SIP call ids survived teardown: {cleaned}")
            return cleaned

        try:
            reload_rendered = 0
            sample("ready")
            if args.send_camera:
                page.wait_for_function(
                    f"() => Boolean(({DEEP_CARD})()?._softphoneSnapshot?.video_camera_send_enabled)",
                    timeout=10_000,
                )
                if not page.evaluate(CLICK_CAMERA_SEND):
                    raise RuntimeError("Send Camera option was not available before the video call")
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
                if args.cancel_during_ring:
                    if not page.evaluate(CLICK_HANGUP):
                        raise RuntimeError("outbound Hangup button was unavailable during ringing")
                    wait_for_idle_cleanup("idle_after_outbound_cancel")
                    result["ok"] = True
                    raise _ProbeComplete
                print("WAITING_FOR_REMOTE_ANSWER", flush=True)
            else:
                print("WAITING_FOR_VIDEO_CALL", flush=True)
                page.wait_for_function(
                    f"() => (({CARD_SAMPLE})()?.card_state || '').toLowerCase() === 'ringing'",
                    timeout=int(args.ring_timeout * 1000),
                )
                ringing = sample("ringing")
                if not args.expect_audio_only and not ringing.get("video_offered"):
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
                after_reload = sample("in_call_after_reload")
                reload_rendered = int(
                    ((after_reload.get("engine_stats") or {}).get("video") or {}).get(
                        "rendered", 0
                    )
                )
            if args.expect_audio_only:
                page.wait_for_function(
                    f"() => {{ const x = ({CARD_SAMPLE})(); const s = x?.engine_stats || {{}}; return s.sent > 0 && s.received > 0 && !x?.video_active && !x?.engine_video_active; }}",
                    timeout=int(args.video_timeout * 1000),
                )
            else:
                page.wait_for_function(
                    f"() => {{ const x = ({CARD_SAMPLE})(); const d = String(x?.video_direction || 'sendrecv'); const v = x?.engine_stats?.video || {{}}; const rx = !['recvonly','sendrecv'].includes(d) || (v.received > 0 && x?.canvas?.width > 0); const tx = {str(args.deny_camera).lower()} || !['sendonly','sendrecv'].includes(d) || v.sent > 0; return x?.engine_video_active && rx && tx; }}",
                    timeout=int(args.video_timeout * 1000),
                )
            if args.sample_interval > 0:
                deadline = time.monotonic() + args.hold_seconds
                sample_number = 0
                while (remaining := deadline - time.monotonic()) > 0:
                    page.wait_for_timeout(int(min(args.sample_interval, remaining) * 1000))
                    sample_number += 1
                    sample(f"hold_{sample_number:03d}")
            else:
                page.wait_for_timeout(int(args.hold_seconds * 1000))
            active = sample("video_flowing")
            video_stats = (active.get("engine_stats") or {}).get("video") or {}
            if args.expect_audio_only:
                if active.get("video_active") or active.get("engine_video_active"):
                    raise RuntimeError(f"audio-only call unexpectedly attached video: {active}")
                if (active.get("engine_stats") or {}).get("sent", 0) <= 0:
                    raise RuntimeError(f"audio-only call did not transmit browser audio: {active}")
                if (active.get("engine_stats") or {}).get("received", 0) <= 0:
                    raise RuntimeError(f"audio-only call did not receive SIP audio: {active}")
            direction = str(active.get("video_direction") or "sendrecv")
            expects_receive = direction in {"recvonly", "sendrecv"}
            expects_send = direction in {"sendonly", "sendrecv"}
            if not args.expect_audio_only and expects_receive and video_stats.get("received", 0) <= 0:
                raise RuntimeError(f"no remote video access units reached WebCodecs: {active}")
            if (
                not args.expect_audio_only
                and expects_send
                and not args.deny_camera
                and video_stats.get("sent", 0) <= 0
            ):
                raise RuntimeError(f"no browser video access units returned to SIP: {active}")
            if args.deny_camera and video_stats.get("sent", 0) != 0:
                raise RuntimeError(f"camera denial still transmitted video: {active}")
            if (
                args.reload_in_call
                and not args.expect_audio_only
                and expects_receive
                and video_stats.get("rendered", 0)
                < reload_rendered + max(3, int(args.hold_seconds * 2))
            ):
                raise RuntimeError(
                    f"video did not continue rendering after page reload: {active}"
                )
            if (
                not args.expect_audio_only
                and expects_receive
                and not (active.get("canvas") or {}).get("non_black")
            ):
                raise RuntimeError(f"decoded canvas has no non-black sample: {active}")
            if not args.expect_audio_only:
                layout = active.get("layout") or {}
                surface = layout.get("surface") or {}
                canvas_layout = layout.get("canvas") or {}
                hangup_layout = layout.get("hangup") or {}
                if layout.get("horizontal_overflow"):
                    raise RuntimeError(f"video card has horizontal overflow: {active}")
                if layout.get("header_stats_overlap"):
                    raise RuntimeError(f"video debug overlay covers the card title: {active}")
                if layout.get("stats_outside_hangup"):
                    raise RuntimeError(f"video diagnostics escape the hangup bar: {active}")
                if abs(float(canvas_layout.get("width", 0)) - float(surface.get("width", 0))) > 2:
                    raise RuntimeError(f"video canvas does not fill card width: {active}")
                if abs(float(canvas_layout.get("height", 0)) - float(surface.get("height", 0))) > 2:
                    raise RuntimeError(f"video canvas does not fill card height: {active}")
                if abs(float(hangup_layout.get("bottom", 0)) - float(surface.get("height", 0))) > 2:
                    raise RuntimeError(f"video hangup bar is not bottom-aligned: {active}")
                if abs(float(hangup_layout.get("width", 0)) - float(surface.get("width", 0))) > 2:
                    raise RuntimeError(f"video hangup bar does not span card width: {active}")
                if not 48 <= float(hangup_layout.get("height", 0)) <= 84:
                    raise RuntimeError(f"video hangup bar has an unusable height: {active}")
                if float(layout.get("usable_video_height", 0)) < min(
                    80,
                    float(surface.get("height", 0)) * 0.25,
                ):
                    raise RuntimeError(f"video overlays leave too little visible video: {active}")
            if args.screenshot:
                page.screenshot(path=args.screenshot, full_page=True)
            if args.expect_remote_hangup:
                wait_for_idle_cleanup("idle_after_remote_hangup")
            elif not args.no_hangup:
                if not page.evaluate(CLICK_HANGUP):
                    raise RuntimeError("visible Hangup button not found")
                wait_for_idle_cleanup("idle_after_hangup")
            result["ok"] = True
        except _ProbeComplete:
            pass
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
