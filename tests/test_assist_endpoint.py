"""Tests for the optional native Assist SIP route."""

import importlib.util
from pathlib import Path
import sys
import types


ROOT = Path(__file__).resolve().parents[1]
PKG_NAME = "custom_components.voip_stack"
PKG_DIR = ROOT / "custom_components" / "voip_stack"


def _load(name: str):
    if "custom_components" not in sys.modules:
        root = types.ModuleType("custom_components")
        root.__path__ = [str(ROOT / "custom_components")]
        sys.modules["custom_components"] = root
    if PKG_NAME not in sys.modules:
        package = types.ModuleType(PKG_NAME)
        package.__path__ = [str(PKG_DIR)]
        sys.modules[PKG_NAME] = package
    full_name = f"{PKG_NAME}.{name}"
    spec = importlib.util.spec_from_file_location(full_name, PKG_DIR / f"{name}.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[full_name] = module
    spec.loader.exec_module(module)
    return module


const = _load("const")
validation = _load("config_validation")
roster = _load("roster")
router = _load("router")


def test_virtual_assist_route_resolves_by_extension_and_name() -> None:
    entry = roster.RosterEntry(
        id="Assist",
        name="Voice Assistant",
        extension="7319",
        sip_uri="sip:assist@127.0.0.1:5065;transport=udp",
        ha_bridge=True,
        metadata={"virtual_endpoint": "assist_satellite"},
    )

    for target in ("7319", "Assist", "Voice Assistant"):
        decision = router.resolve_ha_router(target, [entry])
        assert decision.action is router.RouteAction.ASSIST
        assert decision.sip_uri == "sip:assist@127.0.0.1:5065;transport=udp"


def test_assist_extension_collision_checks_persisted_routes() -> None:
    existing = {
        "sip_accounts": [{"username": "200", "extension": "2200"}],
        "phonebook_contacts": [{"id": "desk", "extension": "3300"}],
    }

    assert validation.extension_conflicts("200", existing)
    assert validation.extension_conflicts("2200", existing)
    assert validation.extension_conflicts("3300", existing)
    assert not validation.extension_conflicts("7319", existing)
