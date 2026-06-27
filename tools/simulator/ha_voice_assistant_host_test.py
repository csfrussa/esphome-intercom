#!/usr/bin/env python3
"""Run a real Home Assistant Voice Assistant cycle against the ESPHome Host target.

Required environment:
  HA_URL    Base URL, for example https://homeassistant.local:8123
  HA_TOKEN  Long-lived access token

The ESPHome Host process must already be running and connected to Home Assistant.
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shutil
import sys
import time
import wave

import requests
import urllib3


DEFAULT_ENTITY = "assist_satellite.spotpear_ball_v2_full_afe_sip_host_assist_satellite"
DEFAULT_SERVICE = "spotpear_sip_host_start_va"
DEFAULT_OUTPUT = Path("test_runs/simulator/spotpear_host_va_speaker_output.pcm")
DEFAULT_MIC_INPUT = Path("tests/simulator/audio/mic_input.pcm")


def request_json(method: str, url: str, token: str, **kwargs):
    headers = kwargs.pop("headers", {})
    headers["Authorization"] = f"Bearer {token}"
    response = requests.request(method, url, headers=headers, verify=False, timeout=kwargs.pop("timeout", 20), **kwargs)
    if response.status_code >= 400:
        raise RuntimeError(f"{method} {url} failed with {response.status_code}: {response.text[:1000]}")
    if response.text:
        return response.json()
    return None


def entity_state(base_url: str, token: str, entity_id: str) -> str:
    data = request_json("GET", f"{base_url}/api/states/{entity_id}", token, timeout=10)
    return str(data.get("state", "unknown"))


def write_wav(pcm_path: Path, wav_path: Path, sample_rate: int, channels: int, sample_width: int) -> None:
    with pcm_path.open("rb") as src, wave.open(str(wav_path), "wb") as dst:
        dst.setnchannels(channels)
        dst.setsampwidth(sample_width)
        dst.setframerate(sample_rate)
        dst.writeframes(src.read())


def wav_to_pcm(wav_path: Path, pcm_path: Path, sample_rate: int, channels: int, sample_width: int) -> None:
    with wave.open(str(wav_path), "rb") as src:
        actual = {
            "sample_rate": src.getframerate(),
            "channels": src.getnchannels(),
            "sample_width": src.getsampwidth(),
        }
        expected = {
            "sample_rate": sample_rate,
            "channels": channels,
            "sample_width": sample_width,
        }
        if actual != expected:
            raise RuntimeError(f"Input WAV format mismatch: expected {expected}, got {actual}")
        frames = src.readframes(src.getnframes())

    pcm_path.parent.mkdir(parents=True, exist_ok=True)
    pcm_path.write_bytes(frames)


def prepare_microphone_input(args: argparse.Namespace) -> None:
    if args.mic_wav and args.mic_pcm:
        raise RuntimeError("--mic-wav and --mic-pcm are mutually exclusive")
    if args.mic_wav:
        wav_to_pcm(args.mic_wav, args.mic_input, args.sample_rate, args.channels, args.sample_width)
    elif args.mic_pcm:
        args.mic_input.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(args.mic_pcm, args.mic_input)


def output_path_for_iteration(base: Path, iteration: int, repeat: int) -> Path:
    if repeat <= 1:
        return base
    return base.with_name(f"{base.stem}_{iteration + 1:03d}{base.suffix}")


def run_cycle(args: argparse.Namespace, base_url: str, iteration: int) -> dict[str, object]:
    output = output_path_for_iteration(args.output, iteration, args.repeat)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        output.unlink()
    runtime_output = args.output
    if runtime_output.exists():
        runtime_output.unlink()

    before = entity_state(base_url, args.ha_token, args.entity)
    request_json(
        "POST",
        f"{base_url}/api/services/esphome/{args.service}",
        args.ha_token,
        headers={"Content-Type": "application/json"},
        json={},
        timeout=20,
    )

    timeline: list[dict[str, object]] = [{"t": 0.0, "state": before}]
    start = time.monotonic()
    saw_active = False
    last_state = before

    while time.monotonic() - start < args.timeout:
        state = entity_state(base_url, args.ha_token, args.entity)
        elapsed = round(time.monotonic() - start, 3)
        if state != last_state:
            timeline.append({"t": elapsed, "state": state})
            last_state = state
        if state != "idle":
            saw_active = True
        if saw_active and state == "idle":
            break
        time.sleep(0.5)
    else:
        return {"ok": False, "error": "timeout", "iteration": iteration + 1, "timeline": timeline}

    if runtime_output.exists() and runtime_output != output:
        shutil.copyfile(runtime_output, output)

    output_bytes = output.stat().st_size if output.exists() else 0
    wav_path = output.with_suffix(".wav")
    if output_bytes:
        write_wav(output, wav_path, args.sample_rate, args.channels, args.sample_width)

    ok = saw_active and output_bytes >= args.min_output_bytes
    duration_s = output_bytes / (args.sample_rate * args.channels * args.sample_width) if output_bytes else 0.0
    return {
        "ok": ok,
        "iteration": iteration + 1,
        "entity": args.entity,
        "service": f"esphome.{args.service}",
        "timeline": timeline,
        "output_pcm": str(output),
        "output_wav": str(wav_path) if output_bytes else None,
        "output_bytes": output_bytes,
        "output_duration_s": round(duration_s, 3),
    }


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--ha-url", default=os.environ.get("HA_URL"))
    parser.add_argument("--ha-token", default=os.environ.get("HA_TOKEN"))
    parser.add_argument("--entity", default=DEFAULT_ENTITY)
    parser.add_argument("--service", default=DEFAULT_SERVICE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--mic-input", type=Path, default=DEFAULT_MIC_INPUT)
    parser.add_argument("--mic-pcm", type=Path, help="Copy raw PCM into the virtual microphone input before running.")
    parser.add_argument("--mic-wav", type=Path, help="Convert WAV into the virtual microphone PCM input before running.")
    parser.add_argument("--timeout", type=float, default=90.0)
    parser.add_argument("--sample-rate", type=int, default=16000)
    parser.add_argument("--channels", type=int, default=1)
    parser.add_argument("--sample-width", type=int, default=2)
    parser.add_argument("--min-output-bytes", type=int, default=1024)
    parser.add_argument("--repeat", type=int, default=1)
    parser.add_argument("--insecure", action="store_true", help="Suppress TLS verification warnings.")
    args = parser.parse_args()

    if not args.ha_url or not args.ha_token:
        print("HA_URL and HA_TOKEN are required", file=sys.stderr)
        return 2

    if args.insecure:
        urllib3.disable_warnings()

    if args.repeat < 1:
        print("--repeat must be >= 1", file=sys.stderr)
        return 2

    try:
        prepare_microphone_input(args)
    except Exception as err:
        print(f"Unable to prepare microphone input: {err}", file=sys.stderr)
        return 2

    base_url = args.ha_url.rstrip("/")
    results = [run_cycle(args, base_url, iteration) for iteration in range(args.repeat)]
    ok = all(bool(result.get("ok")) for result in results)
    print(json.dumps({"ok": ok, "results": results}, indent=2))
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
