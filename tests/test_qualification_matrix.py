#!/usr/bin/env python3
"""Coverage contract for the SIP qualification matrix."""

from __future__ import annotations

import importlib.util
import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
TOOL = ROOT / "tests" / "support" / "qualification_matrix.py"


def _load_tool():
    spec = importlib.util.spec_from_file_location("intercom_qualification_matrix", TOOL)
    if spec is None or spec.loader is None:
        raise RuntimeError("cannot load qualification matrix tool")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


matrix = _load_tool()


class QualificationMatrixTest(unittest.TestCase):
    def test_matrix_has_no_coverage_gaps(self) -> None:
        scenarios = matrix.generate_matrix()
        self.assertGreaterEqual(len(scenarios), 400)
        self.assertEqual(matrix.validate_matrix(scenarios), [])

    def test_matrix_covers_transport_mismatch_and_high_rate_audio(self) -> None:
        scenarios = matrix.generate_matrix()
        self.assertTrue(
            any(
                scenario.caller_transport == "sip_tcp"
                and scenario.callee_transport == "sip_udp"
                and scenario.route == "ha_bridge"
                for scenario in scenarios
            )
        )
        self.assertTrue(any(scenario.tx_format == "48000:s16le:1:10" for scenario in scenarios))
        self.assertTrue(any(scenario.terminal_reason == "media_incompatible" for scenario in scenarios))

    def test_matrix_requires_user_visible_and_protocol_assertions(self) -> None:
        scenarios = matrix.generate_matrix()
        self.assertTrue(any("ha_card_mirror" in scenario.assertions for scenario in scenarios))
        self.assertTrue(any("caller_terminal_screen" in scenario.assertions for scenario in scenarios))
        self.assertTrue(all("debug_sip_trace" in scenario.assertions for scenario in scenarios))


if __name__ == "__main__":
    unittest.main()
