#!/usr/bin/env python3
"""Compare simulator JSON snapshots with stable golden files."""

from __future__ import annotations

import argparse
import json
from pathlib import Path


def _load(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("actual", type=Path)
    parser.add_argument("golden", type=Path)
    args = parser.parse_args()
    actual = _load(args.actual)
    golden = _load(args.golden)
    if actual != golden:
        print(json.dumps({"actual": actual, "golden": golden}, indent=2, sort_keys=True))
        return 1
    print("snapshot ok")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
