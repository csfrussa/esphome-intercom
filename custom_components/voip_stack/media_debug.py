"""Atomic helpers for per-call media diagnostics."""

from __future__ import annotations

from collections.abc import Mapping, MutableMapping
from typing import Any


def merge_media_debug(
    store: MutableMapping[str, Any],
    *,
    call_id: str,
    channel: str,
    values: Mapping[str, Any],
) -> bool:
    """Merge one media channel without overwriting sibling diagnostics.

    Audio and video reporters run as independent asyncio tasks.  Keeping the
    read/merge/write operation synchronous makes it atomic on Home Assistant's
    event loop and prevents an audio tick from erasing the video snapshot (or
    vice versa).
    """

    wanted_call_id = str(call_id or "")
    current_call_id = str(store.get("call_id") or "")
    terminal_call_id = str(store.get("last_terminal_call_id") or "")
    if not wanted_call_id or not (
        wanted_call_id == current_call_id
        or (not current_call_id and wanted_call_id == terminal_call_id)
    ):
        return False

    current = store.get("media_debug")
    merged = dict(current) if isinstance(current, Mapping) else {}
    # The reporter may include a stale/spoofed ``call_id`` in its value map;
    # the validated function argument is authoritative for this channel.
    merged[str(channel)] = {**dict(values), "call_id": wanted_call_id}
    store["media_debug"] = merged
    return True
