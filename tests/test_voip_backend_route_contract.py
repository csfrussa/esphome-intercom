#!/usr/bin/env python3
"""Static backend contracts for SIP route handling."""

from __future__ import annotations

from pathlib import Path
import unittest


ROOT = Path(__file__).resolve().parents[1]
BACKEND = ROOT / "custom_components" / "voip_stack" / "endpoint_runtime.py"
INIT = ROOT / "custom_components" / "voip_stack" / "__init__.py"
AUDIO_WS = ROOT / "custom_components" / "voip_stack" / "audio_ws_view.py"
CARD_JS = ROOT / "custom_components" / "voip_stack" / "frontend" / "voip-stack-card.js"
SERVICES = ROOT / "custom_components" / "voip_stack" / "services.py"
ACCOUNT_SERVICES = ROOT / "custom_components" / "voip_stack" / "account_services.py"
SERVICES_YAML = ROOT / "custom_components" / "voip_stack" / "services.yaml"
ICONS_JSON = ROOT / "custom_components" / "voip_stack" / "icons.json"
CONFIG_FLOW = ROOT / "custom_components" / "voip_stack" / "config_flow.py"
STRINGS_JSON = ROOT / "custom_components" / "voip_stack" / "strings.json"


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
        self.assertIn("_defer_invite_to_ha_softphone(invite, route_kind=decision.action.value", answer_ha_branch)
        self.assertIn('return SipInviteResult(180, "Ringing", to_tag="", defer_final=True)', answer_ha_branch)
        self.assertNotIn('return SipInviteResult(200, "OK"', answer_ha_branch)
        self.assertIn("def _defer_invite_to_ha_softphone(", self.source)
        self.assertIn("registry.pending_invites[invite.call_id] = invite", self.source)
        self.assertIn("_set_ha_softphone_call_state(", self.source)
        self.assertIn("CallState.RINGING.value", self.source)

    def test_retransmitted_invite_is_not_rejected_as_busy(self) -> None:
        start = self.source.index("async def _on_invite(invite:")
        source = self.source[start:]
        busy_guard = source.split('if invite.call_id in route_bucket:', 1)[1].split('if _is_trunk_invite(invite):', 1)[0]
        self.assertIn('return SipInviteResult(100, "Trying"', busy_guard)
        self.assertIn('if invite.call_id in pending:', busy_guard)
        self.assertIn('return SipInviteResult(180, "Ringing"', busy_guard)
        self.assertIn("other_routes", busy_guard)
        self.assertIn("other_pending", busy_guard)
        self.assertNotIn("if route_bucket or pending", busy_guard)

    def test_entryless_sip_uri_route_is_guarded_and_uses_fallback_uri(self) -> None:
        start = self.source.index("routeable_sip_target =")
        bridge_path = self.source[start : self.source.index("if decision_uri is not None and decision_uri.host != local_ip", start)]
        self.assertIn("decision.entry is not None and decision.entry.sip_uri", bridge_path)
        self.assertIn("decision.entry is not None and not decision.entry.metadata.get", bridge_path)
        self.assertIn("parse_sip_uri(decision.sip_uri) if decision.sip_uri else None", bridge_path)
        self.assertNotIn("elif decision.entry.sip_uri", bridge_path)
        self.assertNotIn("elif not decision.entry.metadata.get", bridge_path)

    def test_bridge_relay_uses_bounded_port_pool_and_release_callback(self) -> None:
        self.assertIn("_allocate_sip_rtp_port_pair(hass)", self.source)
        self.assertNotIn('bucket.get("sip_rtp_next_port"', self.source)
        self.assertIn("on_release=lambda ports: _release_sip_rtp_port_pair(hass, ports)", self.source)
        self.assertIn("_release_sip_rtp_port_pair(hass, (source_relay_port, dest_relay_port))", self.source)

    def test_group_routes_have_dedicated_dispatch_not_generic_bridge(self) -> None:
        on_invite = self.source[self.source.index("async def _on_invite(invite:"):]
        routeable = on_invite[
            on_invite.index("routeable_sip_target =") : on_invite.index("if not force_ha_softphone and (bridge_to_trunk or routeable_sip_target):")
        ]
        self.assertNotIn("RouteAction.GROUP", routeable)
        self.assertIn("if decision.action is RouteAction.GROUP:", on_invite)
        self.assertIn("conference_manager(hass, local_ip=local_ip).join", on_invite)
        self.assertIn("_run_ring_group_call(invite, decision.entry, peers, roster_entries)", on_invite)
        self.assertIn('return SipInviteResult(180, "Ringing", to_tag="", defer_final=True)', on_invite)

    def test_ha_softphone_media_path_can_join_conference_without_card_logic(self) -> None:
        init_py = INIT.read_text()
        audio_ws = AUDIO_WS.read_text()

        self.assertIn('call_id.startswith("conference:")', init_py)
        self.assertIn("manager.join_ha_softphone(room_name)", init_py)
        self.assertIn('"conference_queue": queue', init_py)
        self.assertIn("conference_queue = item.get(\"conference_queue\")", audio_ws)
        self.assertIn("_run_conference_audio_session", audio_ws)
        self.assertIn("manager.push_ha_audio(session.conference_room, pcm)", audio_ws)
        self.assertNotIn("conference", CARD_JS.read_text().lower())

    def test_softphone_account_list_service_is_registered_and_documented(self) -> None:
        services = SERVICES.read_text()
        account_services = ACCOUNT_SERVICES.read_text()
        services_yaml = SERVICES_YAML.read_text()
        icons_json = ICONS_JSON.read_text()

        self.assertIn('"list_accounts", handler_for("list_accounts")', services)
        self.assertIn('"list_accounts": list_accounts', account_services)
        self.assertIn("list_accounts:", services_yaml)
        self.assertIn('"list_accounts"', icons_json)

    def test_add_contact_accepts_group_membership_metadata(self) -> None:
        services = SERVICES.read_text()
        services_yaml = SERVICES_YAML.read_text()

        self.assertIn('"conference_group"', services)
        self.assertIn('"ring_group"', services)
        self.assertIn("conference_group:", services_yaml)
        self.assertIn("ring_group:", services_yaml)

    def test_ring_group_fallback_is_configured_and_uses_ha_softphone(self) -> None:
        config_flow = CONFIG_FLOW.read_text()
        strings = STRINGS_JSON.read_text()

        self.assertIn("CONF_RING_GROUP_FALLBACK", config_flow)
        self.assertIn('SelectSelectorConfig(options=["reject", "answer_ha"])', config_flow)
        self.assertIn('"ring_group_fallback"', strings)
        self.assertIn("CONF_RING_GROUP_FALLBACK", self.source)
        self.assertIn("_defer_invite_to_ha_softphone(invite, route_kind=GROUP_TYPE_RING", self.source)


if __name__ == "__main__":
    unittest.main()
