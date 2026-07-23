#!/usr/bin/env python3
"""Refresh the local HA lab token stored in a Playwright storage-state file."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen


LAB_ROOT = Path(os.environ.get("HA_VOIP_LAB_ROOT", Path.home() / "ha-voip-lab"))


def _credentials(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for line in path.read_text().splitlines():
        key, separator, value = line.partition("=")
        if separator and key.strip():
            values[key.strip()] = value.strip()
    return values


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://127.0.0.1:18123")
    parser.add_argument(
        "--client-id",
        default="",
        help="OAuth client ID; defaults to the lab URL with a trailing slash",
    )
    parser.add_argument(
        "--credentials",
        default=str(LAB_ROOT / ".credentials"),
    )
    parser.add_argument(
        "--storage-state",
        default=str(LAB_ROOT / "playwright-storage.json"),
    )
    args = parser.parse_args()
    credentials = _credentials(Path(args.credentials).expanduser())
    refresh_token = credentials.get("refresh_token", "")
    if not refresh_token:
        parser.error("credentials file has no refresh_token")

    storage_path = Path(args.storage_state).expanduser()
    storage = json.loads(storage_path.read_text())
    hass_tokens = None
    for origin in storage.get("origins", []):
        for item in origin.get("localStorage", []):
            if item.get("name") == "hassTokens":
                hass_tokens = json.loads(item.get("value") or "{}")
                token_item = item
                break
        if hass_tokens is not None:
            break
    if hass_tokens is None:
        parser.error("storage state has no hassTokens localStorage item")

    client_id = str(args.client_id or f"{args.url.rstrip('/')}/")
    body = urlencode(
        {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
        }
    ).encode()
    request = Request(
        f"{args.url.rstrip('/')}/auth/token",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urlopen(request, timeout=10) as response:  # noqa: S310 - loopback lab URL is explicit.
        refreshed = json.load(response)
    access_token = str(refreshed.get("access_token") or "")
    if not access_token:
        raise RuntimeError("Home Assistant token refresh returned no access token")

    hass_tokens.update(
        {
            "hassUrl": args.url.rstrip("/"),
            "clientId": client_id,
            "access_token": access_token,
            "expires": int((time.time() + int(refreshed.get("expires_in") or 1800)) * 1000),
        }
    )
    token_item["value"] = json.dumps(hass_tokens, separators=(",", ":"))
    temporary = storage_path.with_suffix(storage_path.suffix + ".tmp")
    temporary.write_text(json.dumps(storage, separators=(",", ":")))
    os.chmod(temporary, 0o600)
    temporary.replace(storage_path)
    print(f"Refreshed Playwright authentication for {args.url}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
