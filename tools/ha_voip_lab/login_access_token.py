#!/usr/bin/env python3
"""Create a short-lived Home Assistant access token for local qualification."""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
from urllib.parse import urlencode
from urllib.request import Request, urlopen


def _json_request(url: str, payload: dict[str, object]) -> dict[str, object]:
    request = Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with urlopen(request, timeout=15) as response:  # noqa: S310 - explicit lab URL.
        return json.load(response)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--url", default="http://192.168.1.10:8123")
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()
    username = os.environ.get("HA_USERNAME", "")
    password = os.environ.get("HA_PASSWORD", "")
    if not username or not password:
        parser.error("HA_USERNAME and HA_PASSWORD are required")
    base = args.url.rstrip("/")
    client_id = f"{base}/"
    started = _json_request(
        f"{base}/auth/login_flow",
        {
            "client_id": client_id,
            "handler": ["homeassistant", None],
            "redirect_uri": client_id,
        },
    )
    completed = _json_request(
        f"{base}/auth/login_flow/{started['flow_id']}",
        {
            "client_id": client_id,
            "username": username,
            "password": password,
        },
    )
    body = urlencode(
        {
            "grant_type": "authorization_code",
            "code": completed["result"],
            "client_id": client_id,
        }
    ).encode()
    request = Request(
        f"{base}/auth/token",
        data=body,
        headers={"Content-Type": "application/x-www-form-urlencoded"},
        method="POST",
    )
    with urlopen(request, timeout=15) as response:  # noqa: S310 - explicit lab URL.
        token = str(json.load(response)["access_token"])
    args.output.parent.mkdir(parents=True, exist_ok=True)
    temporary = args.output.with_suffix(args.output.suffix + ".tmp")
    temporary.write_text(token)
    os.chmod(temporary, 0o600)
    temporary.replace(args.output)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
