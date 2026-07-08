#!/usr/bin/env python3
"""Contract tests for the live HA/ESP qualification runner."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tools" / "live_voip_qualification.py"


def _load_tool():
    spec = importlib.util.spec_from_file_location("live_voip_qualification", TOOL)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load live VoIP qualification runner")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


runner = _load_tool()


class LiveVoipQualificationContractTest(unittest.TestCase):
    def test_matrix_covers_real_ha_and_esp_paths(self) -> None:
        scenarios = runner.SCENARIOS
        self.assertIn("ha_to_esp_extension_answer_hangup", scenarios)
        self.assertIn("esp_to_ha_extension_cancel", scenarios)
        self.assertTrue(all("esp" in scenario.requires for scenario in scenarios.values()))
        self.assertTrue(any("ha" in scenario.requires for scenario in scenarios.values()))

    def test_matrix_covers_groups_dnd_trunk_and_self_call(self) -> None:
        scenarios = runner.SCENARIOS
        self.assertTrue(any("ring_group" in scenario.requires for scenario in scenarios.values()))
        self.assertTrue(any("conference_group" in scenario.requires for scenario in scenarios.values()))
        self.assertTrue(any("dnd" in scenario.requires for scenario in scenarios.values()))
        self.assertTrue(any("trunk" in scenario.requires for scenario in scenarios.values()))
        self.assertTrue(any("busy" in scenario.requires for scenario in scenarios.values()))

    def test_every_scenario_has_visible_terminal_or_state_assertions(self) -> None:
        for scenario in runner.SCENARIOS.values():
            with self.subTest(scenario=scenario.id):
                self.assertTrue(scenario.assertions)
                self.assertTrue(
                    any(
                        token in scenario.assertions
                        for token in (
                            "esp_idle",
                            "both_idle",
                            "cleanup_idle",
                            "ha_terminal_reason",
                            "winner_not_group_label",
                        )
                    ),
                    scenario.assertions,
                )


if __name__ == "__main__":
    unittest.main()
