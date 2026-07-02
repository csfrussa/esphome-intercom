#!/usr/bin/env python3
"""Static backend contracts for SIP route handling."""

from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "custom_components" / "voip_stack" / "endpoint_runtime.py"


class VoipBackendRouteContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = BACKEND.read_text()

    def test_default_answer_ha_invite_rings_until_explicit_answer(self) -> None:
        start = self.source.index("async def _on_invite(invite:")
        source = self.source[start:]
        marker = "if not force_ha_softphone and decision.action is RouteAction.ANSWER_HA:"
        fallback = "local_rtp_port = _allocate_sip_rtp_port(hass)"
        self.assertIn(marker, source)
        self.assertLess(source.index(marker), source.index(fallback))
        answer_ha_branch = source[source.index(marker) : source.index(fallback)]
        self.assertIn('pending[invite.call_id] = invite', answer_ha_branch)
        self.assertIn('_set_ha_softphone_call_state(', answer_ha_branch)
        self.assertIn('"ringing"', answer_ha_branch)
        self.assertIn('return SipInviteResult(180, "Ringing", to_tag="", defer_final=True)', answer_ha_branch)
        self.assertNotIn('return SipInviteResult(200, "OK"', answer_ha_branch)


if __name__ == "__main__":
    unittest.main()
