from __future__ import annotations

import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_hacs_repo_exposes_only_voip_stack_domain() -> None:
    components = [
        path.name
        for path in (ROOT / "custom_components").iterdir()
        if path.is_dir() and not path.name.startswith("__")
    ]

    assert components == ["voip_stack"]


def test_hacs_and_manifest_names_match_current_domain() -> None:
    hacs = json.loads((ROOT / "hacs.json").read_text(encoding="utf-8"))
    manifest = json.loads((ROOT / "custom_components" / "voip_stack" / "manifest.json").read_text(encoding="utf-8"))

    assert hacs["name"] == "VoIP Stack"
    assert manifest["domain"] == "voip_stack"
    assert manifest["name"] == "VoIP Stack"
