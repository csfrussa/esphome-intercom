#!/usr/bin/env python3
"""Generate host-platform virtual profile stubs from real profile paths."""

from __future__ import annotations

import argparse
from pathlib import Path
import re


OUT = Path("yamls/generated/virtual")


def _profile_paths() -> list[Path]:
    return [
        path
        for path in sorted(Path("yamls").glob("**/*.yaml"))
        if ".esphome" not in path.parts
        and "generated" not in path.parts
        and path.name != "secrets.yaml"
    ]


def _virtual_name(path: Path) -> str:
    rel = path.with_suffix("")
    name = "-".join(rel.parts[1:])
    name = re.sub(r"[^a-zA-Z0-9_-]+", "-", name)
    return f"virtual-{name}.yaml"


def generate(limit: int | None = None) -> list[Path]:
    OUT.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for path in _profile_paths()[:limit]:
        out = OUT / _virtual_name(path)
        content = f"""# Generated virtual profile stub.
# Source profile: {path}
# TODO(phase-00v): replace hardware packages through the profile registry,
# not by maintaining this file manually.

esphome:
  name: {out.stem}
  friendly_name: {out.stem}

host:
  mac_address: "06:35:69:00:00:01"

logger:
  level: DEBUG

api:

external_components:
  - source: ../../../esphome/components

intercom_simulator:
  source_profile: "{path}"
"""
        out.write_text(content, encoding="utf-8")
        written.append(out)
    return written


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int)
    args = parser.parse_args()
    for path in generate(args.limit):
        print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
