#!/usr/bin/env python3
"""Anti-regressions for concurrent audio/video diagnostic publishing."""

from __future__ import annotations

import importlib.util
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
MODULE = ROOT / "custom_components" / "voip_stack" / "media_debug.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("voip_stack_media_debug_test", MODULE)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_audio_video_audio_interleaving_preserves_both_channels() -> None:
    merge = _load_module().merge_media_debug
    store = {"call_id": "call-1", "media_debug": {}}

    assert merge(store, call_id="call-1", channel="audio", values={"rtp_rx": 1})
    assert merge(store, call_id="call-1", channel="video", values={"rtp_rx": 2})
    assert merge(store, call_id="call-1", channel="audio", values={"rtp_rx": 3})

    assert store["media_debug"] == {
        "audio": {"call_id": "call-1", "rtp_rx": 3},
        "video": {"call_id": "call-1", "rtp_rx": 2},
    }


def test_stale_call_cannot_overwrite_current_diagnostics() -> None:
    merge = _load_module().merge_media_debug
    store = {
        "call_id": "current",
        "last_terminal_call_id": "previous",
        "media_debug": {"audio": {"call_id": "current", "rtp_rx": 4}},
    }

    assert not merge(store, call_id="previous", channel="video", values={"rtp_rx": 99})
    assert store["media_debug"] == {
        "audio": {"call_id": "current", "rtp_rx": 4}
    }


def test_terminal_snapshot_accepts_final_audio_and_video_updates() -> None:
    merge = _load_module().merge_media_debug
    store = {"call_id": "", "last_terminal_call_id": "finished"}

    assert merge(store, call_id="finished", channel="audio", values={"rtp_rx": 5})
    assert merge(store, call_id="finished", channel="video", values={"rtp_rx": 6})
    assert set(store["media_debug"]) == {"audio", "video"}


def test_reporter_values_cannot_override_validated_call_id() -> None:
    merge = _load_module().merge_media_debug
    store = {"call_id": "current"}

    assert merge(
        store,
        call_id="current",
        channel="video",
        values={"call_id": "stale", "rtp_rx": 7},
    )
    assert store["media_debug"]["video"] == {
        "call_id": "current",
        "rtp_rx": 7,
    }
