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
ENDPOINT_ROUTING = ROOT / "custom_components" / "voip_stack" / "endpoint_routing.py"
SENSOR = ROOT / "custom_components" / "voip_stack" / "sensor.py"
SERVICES = ROOT / "custom_components" / "voip_stack" / "services.py"
WEBSOCKET_API = ROOT / "custom_components" / "voip_stack" / "websocket_api.py"
ACCOUNT_SERVICES = ROOT / "custom_components" / "voip_stack" / "account_services.py"
SERVICES_YAML = ROOT / "custom_components" / "voip_stack" / "services.yaml"
ICONS_JSON = ROOT / "custom_components" / "voip_stack" / "icons.json"
CONFIG_FLOW = ROOT / "custom_components" / "voip_stack" / "config_flow.py"
STRINGS_JSON = ROOT / "custom_components" / "voip_stack" / "strings.json"


class VoipBackendRouteContractTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.source = BACKEND.read_text()
        cls.init_source = INIT.read_text()

    def test_default_answer_ha_invite_rings_until_explicit_answer(self) -> None:
        start = self.source.index("async def _on_invite(invite:")
        source = self.source[start:]
        marker = (
            "if not force_ha_softphone and decision.action is RouteAction.ANSWER_HA:"
        )
        fallback = "local_rtp_port = _allocate_sip_rtp_port(hass)"
        self.assertIn(marker, source)
        self.assertLess(source.index(marker), source.index(fallback))
        answer_ha_branch = source[source.index(marker) : source.index(fallback)]
        self.assertIn(
            "_defer_invite_to_ha_softphone(invite, route_kind=decision.action.value",
            answer_ha_branch,
        )
        self.assertIn(
            'return SipInviteResult(180, "Ringing", to_tag="", defer_final=True)',
            answer_ha_branch,
        )
        self.assertNotIn('return SipInviteResult(200, "OK"', answer_ha_branch)
        self.assertIn("def _defer_invite_to_ha_softphone(", self.source)
        self.assertIn("registry.pending_invites[invite.call_id] = invite", self.source)
        self.assertIn("_set_ha_softphone_call_state(", self.source)
        self.assertIn("CallState.RINGING.value", self.source)

    def test_mid_call_dtmf_is_an_event_not_a_second_dialplan(self) -> None:
        websocket_source = WEBSOCKET_API.read_text()
        self.assertIn('SIP_DTMF_EVENT = "voip_stack.dtmf"', websocket_source)
        self.assertIn("def _attach_dtmf_event_bridge(", self.source)
        self.assertIn('"source_leg": "caller" if source_is_caller else "callee"', self.source)
        self.assertIn('client.on_info_dtmf = lambda digit: _emit("right", digit, "sip_info")', self.source)
        self.assertIn('callback("left", digit, "sip_info")', self.source)
        self.assertEqual(self.source.count("_attach_dtmf_event_bridge("), 4)
        self.assertNotIn("dtmf_sequence", self.source)

    def test_ha_softphone_busy_is_scoped_to_answer_ha_route(self) -> None:
        start = self.source.index("async def _on_invite(invite:")
        source = self.source[start:]
        answer_ha = (
            "if not force_ha_softphone and decision.action is RouteAction.ANSWER_HA:"
        )
        self.assertIn(answer_ha, source)
        before_answer_ha = source[: source.index(answer_ha)]
        answer_ha_branch = source[
            source.index(answer_ha) : source.index(
                "local_rtp_port = _allocate_sip_rtp_port(hass)"
            )
        ]

        self.assertNotIn("HA SIP endpoint is busy", before_answer_ha)
        self.assertNotIn("HA softphone is busy", before_answer_ha)
        self.assertIn("HA softphone is busy", answer_ha_branch)
        self.assertIn(
            "_ha_softphone_has_active_call(hass, ignore_call_id=invite.call_id)",
            answer_ha_branch,
        )

    def test_ha_softphone_dnd_declines_with_dnd_reason(self) -> None:
        start = self.source.index("if _ha_softphone_dnd(hass):")
        dnd_branch = self.source[
            start : self.source.index("_defer_invite_to_ha_softphone", start)
        ]
        self.assertIn('reason="dnd"', dnd_branch)
        self.assertIn('terminal_reason="dnd"', dnd_branch)
        self.assertIn('decline_reason="dnd"', dnd_branch)
        self.assertNotIn('reason="busy"', dnd_branch)
        self.assertNotIn('decline_reason="busy"', dnd_branch)

    def test_retransmitted_invite_is_not_rejected_as_busy(self) -> None:
        start = self.source.index("async def _on_invite(invite:")
        source = self.source[start:]
        busy_guard = source.split("if invite.call_id in route_bucket:", 1)[1].split(
            "if _is_trunk_invite(invite):", 1
        )[0]
        self.assertIn('return SipInviteResult(100, "Trying"', busy_guard)
        self.assertIn("if invite.call_id in pending:", busy_guard)
        self.assertIn('return SipInviteResult(180, "Ringing"', busy_guard)
        self.assertNotIn("HA SIP endpoint is busy", busy_guard)
        self.assertNotIn("other_routes", busy_guard)
        self.assertNotIn("other_pending", busy_guard)
        self.assertNotIn("ha_softphone_active", busy_guard)
        self.assertNotIn("if route_bucket or pending", busy_guard)

    def test_registered_sip_callers_bypass_route_requested(self) -> None:
        on_invite = self.source[self.source.index("async def _on_invite(invite:") :]
        pre_route = on_invite[: on_invite.index("if caller_is_registered_endpoint:")]
        registered_branch = on_invite[
            on_invite.index("if caller_is_registered_endpoint:") : on_invite.index(
                'if route_action in {"decline", "busy", "cancel"}:'
            )
        ]
        self.assertIn(
            "registered_entries = _registered_roster_entries(hass)", pre_route
        )
        self.assertIn(
            "caller_entry = _roster_entry_for_target(invite.caller, registered_entries)",
            pre_route,
        )
        self.assertIn(
            "if caller_entry is None and invite.caller_uri is not None:", pre_route
        )
        self.assertIn(
            "caller_entry = _roster_entry_for_target(invite.caller_uri.user, registered_entries)",
            pre_route,
        )
        self.assertIn('caller_entry.metadata.get("registered")', pre_route)
        self.assertIn(
            "SIP registered endpoint uses central dialplan", registered_branch
        )
        self.assertIn("else:", registered_branch)
        route_requested_branch = registered_branch[registered_branch.index("else:") :]
        self.assertIn('"route_requested"', route_requested_branch)
        self.assertIn(
            "await asyncio.wait_for(future, timeout=SIP_ROUTE_DECISION_TIMEOUT)",
            route_requested_branch,
        )

    def test_forward_to_registered_account_keeps_contact_uri(self) -> None:
        call_service = self.init_source[
            self.init_source.index("async def _handle_sip_call_target_service") :
        ]
        force_bridge = call_service[
            call_service.index(
                'if (force_ha_bridge or bool(call.data.get("ha_bridge", False)))'
            ) : call_service.index("use_trunk = route.action is RouteAction.TRUNK", 1)
        ]
        self.assertIn(
            'if route.entry is not None and route.entry.metadata.get("registered"):',
            force_bridge,
        )
        self.assertIn("bridge_uri = route.sip_uri", force_bridge)
        self.assertIn(
            "bridge_uri = ha_uri_for(route.target or target, contacts)", force_bridge
        )
        self.assertIn(
            "route = replace(route, action=RouteAction.BRIDGE, sip_uri=bridge_uri)",
            force_bridge,
        )

    def test_entryless_sip_uri_route_is_guarded_and_uses_fallback_uri(self) -> None:
        start = self.source.index("routeable_sip_target =")
        bridge_path = self.source[
            start : self.source.index(
                "if decision_uri is not None and decision_uri.host != local_ip", start
            )
        ]
        self.assertIn(
            "decision.entry is not None and decision.entry.sip_uri", bridge_path
        )
        self.assertIn(
            "decision.entry is not None and not decision.entry.metadata.get",
            bridge_path,
        )
        self.assertIn(
            "parse_sip_uri(decision.sip_uri) if decision.sip_uri else None", bridge_path
        )
        self.assertNotIn("elif decision.entry.sip_uri", bridge_path)
        self.assertNotIn("elif not decision.entry.metadata.get", bridge_path)

    def test_bridge_relay_uses_bounded_port_pool_and_release_callback(self) -> None:
        self.assertIn("RtpPortReservation.allocate(hass)", self.source)
        self.assertNotIn('bucket.get("sip_rtp_next_port"', self.source)
        self.assertIn(
            "on_release=lambda ports: _release_sip_rtp_port_pair(hass, ports)",
            self.source,
        )
        self.assertNotIn(
            "_release_sip_rtp_port_pair(hass, (source_relay_port, dest_relay_port))",
            self.source,
        )
        self.assertIn("class OutboundLeg", self.source)
        self.assertIn("async def _close_client_and_release(", self.source)
        self.assertIn("async def _close_outbound_leg(", self.source)
        self.assertIn("attempt.ports.release()", self.source)
        self.assertIn("winner.ports.detach()", self.source)
        self.assertIn("bridge_ports.detach()", self.source)
        self.assertNotIn(
            "await client.close()\n            bridge_ports.release()", self.source
        )
        self.assertNotIn(
            "await client.close()\n                    bridge_ports.release()",
            self.source,
        )

    def test_bridge_invite_does_not_register_after_caller_cancel(self) -> None:
        bridge_path = self.source[
            self.source.index("result = await client.invite(") : self.source.index(
                'if result not in {"ringing", "in_call"}:'
            )
        ]
        self.assertIn(
            'invite.call_id in bucket.get("trunk_closed_calls", set())', bridge_path
        )
        self.assertIn(
            'bucket["trunk_closed_calls"].discard(invite.call_id)', bridge_path
        )
        self.assertIn("client.bye_or_cancel()", bridge_path)
        self.assertIn(
            "await _close_client_and_release(client, bridge_ports)", bridge_path
        )
        self.assertIn(
            'return SipInviteResult(\n                        487,\n                        "Request Terminated"',
            bridge_path,
        )
        self.assertNotIn(
            "await attempt.client.close()\n                    attempt.ports.release()",
            self.source,
        )

    def test_group_routes_have_dedicated_dispatch_not_generic_bridge(self) -> None:
        on_invite = self.source[self.source.index("async def _on_invite(invite:") :]
        routeable = on_invite[
            on_invite.index("routeable_sip_target =") : on_invite.index(
                "if not force_ha_softphone and (bridge_to_trunk or routeable_sip_target):"
            )
        ]
        self.assertNotIn("RouteAction.GROUP", routeable)
        self.assertIn("if decision.action is RouteAction.GROUP:", on_invite)
        self.assertIn(
            "ring_ha = any(_is_ha_target(member) for member in ring_members)", on_invite
        )
        self.assertIn(
            "conference_manager(hass, local_ip=local_ip).join(invite, decision.entry, ring_ha=ring_ha)",
            on_invite,
        )
        self.assertIn("async def _ring_conference_members_from_ha", self.source)
        self.assertIn(
            'hass.data.setdefault(DOMAIN, {})["async_ring_conference_members"] = _ring_conference_members_from_ha',
            self.source,
        )
        self.assertIn("caller=_ha_peer_name(hass)", self.source)
        self.assertIn("source_host=local_ip", self.source)
        self.assertIn("_ring_conference_members(", on_invite)
        self.assertIn(
            "_run_ring_group_call(invite, decision.entry, peers, roster_entries)",
            on_invite,
        )
        self.assertIn(
            'return SipInviteResult(180, "Ringing", to_tag="", defer_final=True)',
            on_invite,
        )

    def test_ha_softphone_runtime_settings_publish_virtual_endpoint(self) -> None:
        config_flow = CONFIG_FLOW.read_text()
        strings = STRINGS_JSON.read_text()
        init_py = INIT.read_text()
        websocket = (
            ROOT / "custom_components" / "voip_stack" / "websocket_api.py"
        ).read_text()
        sensor = SENSOR.read_text()
        const = (ROOT / "custom_components" / "voip_stack" / "const.py").read_text()

        for token in (
            "CONF_HA_RING_GROUP",
            "CONF_HA_CONFERENCE_GROUP",
            "CONF_HA_CONFERENCE_RING",
        ):
            self.assertNotIn(token, config_flow)
            self.assertNotIn(token, websocket)
            self.assertNotIn(token, init_py)
            self.assertNotIn(token, const)
        self.assertNotIn('"ha_ring_group"', strings)
        self.assertNotIn('"ha_conference_group"', strings)
        self.assertNotIn('"ha_conference_ring"', strings)
        self.assertIn("async_set_ha_softphone_settings", websocket)
        self.assertNotIn("WS_TYPE_SET_HA_SOFTPHONE_SETTINGS", websocket)
        self.assertNotIn("WS_TYPE_SET_HA_SOFTPHONE_GROUPS", websocket)
        self.assertNotIn("WS_TYPE_SET_HA_SOFTPHONE_DND", websocket)
        self.assertIn(
            '"set_ha_softphone_settings": _handle_set_ha_softphone_settings_service',
            init_py,
        )
        self.assertIn(
            'hass.services.async_register(\n        DOMAIN,\n        "set_ha_softphone_settings"',
            SERVICES.read_text(),
        )
        self.assertIn("_ha_softphone_extension", websocket)
        self.assertIn("HA_SOFTPHONE_ENDPOINT_ENTITY_ID", const)
        self.assertIn("class HaSoftphoneEndpointSensor", sensor)
        self.assertIn("HA_SIP_PCM_FORMATS", sensor)
        self.assertIn("HA_ENDPOINT_AUDIO_FORMATS", sensor)
        self.assertIn("HA_SIP_PCM_FORMATS[:8]", sensor)
        self.assertIn('tx = ";".join(HA_ENDPOINT_AUDIO_FORMATS)', sensor)
        self.assertNotIn("HA_ENDPOINT_FORMATS", sensor)
        self.assertIn('self._attr_native_value = "online"', sensor)
        self.assertIn('"endpoint": endpoint', sensor)
        self.assertIn('"extension": extension', sensor)
        self.assertIn('"ring_group": groups["ring_group"]', sensor)
        self.assertIn('"conference_group": groups["conference_group"]', sensor)
        self.assertNotIn(
            "f\"{extension}|{groups['conference_group']}|{groups['ring_group']}|\"",
            sensor.replace("\n", ""),
        )
        self.assertIn("old_endpoint", sensor)
        self.assertIn("new_endpoint", sensor)
        self.assertIn("voip_ring_groups", sensor)
        self.assertIn("voip_conference_groups", sensor)
        self.assertIn("voip_ring_on_conference", sensor)
        self.assertIn("new_set.add(HA_SOFTPHONE_ENDPOINT_ENTITY_ID)", sensor)
        self.assertIn("hass.states.get(HA_SOFTPHONE_ENDPOINT_ENTITY_ID)", init_py)
        self.assertIn("ha_endpoint_state.attributes or {}", init_py)
        self.assertIn('get("endpoint")', init_py)
        self.assertIn("parse_voip_endpoint", init_py)
        self.assertNotIn("async_prune_ha_softphone_groups", sensor)
        self.assertNotIn("async_prune_ha_softphone_groups", websocket)
        self.assertNotIn("local_ha_seen = False", websocket)
        self.assertNotIn("HA_SOFTPHONE_GROUPS_UPDATED_EVENT", websocket)
        self.assertNotIn("HA_SOFTPHONE_GROUPS_UPDATED_EVENT", sensor)
        self.assertIn("def _clean_group_token", websocket)
        self.assertIn('.replace("|", " ")', websocket)
        self.assertIn('.replace(";", " ")', websocket)
        self.assertIn("[:32]", websocket)
        self.assertIn('for raw in str(value or "").split(",")', websocket)

    def test_config_flow_has_no_softphone_group_or_ring_fallback_policy(self) -> None:
        config_flow = CONFIG_FLOW.read_text()
        strings = STRINGS_JSON.read_text()
        const = (ROOT / "custom_components" / "voip_stack" / "const.py").read_text()

        for token in (
            "CONF_RING_GROUP_FALLBACK",
            "CONF_HA_RING_GROUP",
            "CONF_HA_CONFERENCE_GROUP",
            "CONF_HA_CONFERENCE_RING",
            "ring_group_fallback",
            "ha_ring_group",
            "ha_conference_group",
            "ha_conference_ring",
        ):
            self.assertNotIn(token, config_flow)
            self.assertNotIn(token, strings)
            self.assertNotIn(token, const)

    def test_ha_softphone_can_join_conference_group_without_sip_self_invite(
        self,
    ) -> None:
        init_py = INIT.read_text()
        call_service = init_py[
            init_py.index("async def _handle_sip_call_target_service") : init_py.index(
                "async def _handle_sip_route_service"
            )
        ]
        self.assertIn("if route.action is RouteAction.GROUP:", call_service)
        self.assertIn("manager.start_ha_softphone(room_name)", call_service)
        self.assertIn('"async_ring_conference_members"', call_service)
        self.assertIn(
            "create_runtime_task(hass, ring_members(route.entry))", call_service
        )
        self.assertIn('last_sip_event="LOCAL_CONFERENCE_JOIN"', call_service)
        self.assertIn(
            "RouteAction.GROUP",
            call_service[call_service.index("if not use_trunk and") :],
        )

    def test_ha_softphone_starts_ring_group_without_sip_self_invite(self) -> None:
        init_py = INIT.read_text()
        runtime = BACKEND.read_text()
        call_service = init_py[
            init_py.index("async def _handle_sip_call_target_service") : init_py.index(
                "async def _handle_sip_route_service"
            )
        ]
        self.assertIn('"async_start_ring_group_from_ha"', call_service)
        self.assertIn("await start_ring_group(route.entry)", call_service)
        self.assertIn(
            "return",
            call_service[
                call_service.index('if group_type == "ring":') : call_service.index(
                    'if group_type == "conference":'
                )
            ],
        )
        self.assertIn(
            'hass.data.setdefault(DOMAIN, {})["async_start_ring_group_from_ha"] = _start_ring_group_from_ha',
            runtime,
        )
        self.assertIn('last_sip_event="LOCAL_RING_GROUP"', runtime)
        self.assertIn("fmt.nominal_frame_bytes <= 1200", runtime)

    def test_ha_direct_esp_calls_prefer_roster_audio_profile_over_device_registry(
        self,
    ) -> None:
        init_py = INIT.read_text()
        call_service = init_py[
            init_py.index("async def _handle_sip_call_target_service") : init_py.index(
                "async def _handle_sip_route_service"
            )
        ]
        self.assertIn(
            'remote_tx_formats = _roster_entry_formats(route.entry, "tx_formats") or _device_formats(dest_device, "tx_formats")',
            call_service,
        )
        self.assertIn(
            'remote_rx_formats = _roster_entry_formats(route.entry, "rx_formats") or _device_formats(dest_device, "rx_formats")',
            call_service,
        )
        self.assertNotIn(
            "if dest_device is not None\n        else _roster_entry_formats",
            call_service,
        )

    def test_phonebook_sensor_exposes_only_the_central_roster_for_the_card(
        self,
    ) -> None:
        routing = ENDPOINT_ROUTING.read_text()
        sensor = SENSOR.read_text()
        card = CARD_JS.read_text()

        self.assertIn('"tx_formats": list(peer.tx_formats or [])', routing)
        self.assertIn('"rx_formats": list(peer.rx_formats or [])', routing)
        self.assertIn('"group_type": group.group_type', routing)
        self.assertNotIn("def softphone_targets_from_roster", routing)
        self.assertNotIn("softphone_targets_json", sensor)
        self.assertNotIn("softphone_targets_json", card)
        self.assertIn("_targetFromRosterEntry(entry)", card)
        self.assertIn("metadata.local_ha", card)

    def test_ha_softphone_media_path_can_join_conference_without_card_logic(
        self,
    ) -> None:
        init_py = INIT.read_text()
        audio_ws = AUDIO_WS.read_text()

        self.assertIn('call_id.startswith("conference:")', init_py)
        self.assertIn("manager.join_ha_softphone(room_name)", init_py)
        self.assertIn("manager.start_ha_softphone(room_name)", init_py)
        self.assertIn('"conference_queue": queue', init_py)
        self.assertIn('last_sip_event="LOCAL_CONFERENCE_JOIN"', init_py)
        self.assertIn('conference_queue = item.get("conference_queue")', audio_ws)
        self.assertIn("_run_conference_audio_session", audio_ws)
        self.assertIn("manager.push_ha_audio(session.conference_room, pcm)", audio_ws)
        self.assertIn("await manager.leave_ha_softphone(conference_room)", init_py)
        card = CARD_JS.read_text()
        self.assertNotIn("conference_manager", card)
        self.assertNotIn("_ringConference", card)
        self.assertNotIn("_run_conference_audio_session", card)

    def test_conference_tracks_leg_roles_and_participant_lifetime(self) -> None:
        conference = (
            ROOT / "custom_components" / "voip_stack" / "conference.py"
        ).read_text()
        self.assertIn("role: str", conference)
        self.assertIn('role="owner" if was_empty else "manual"', conference)
        self.assertIn('role="auto_invited"', self.source)
        self.assertNotIn('await self.close(reason="owner_left")', conference)
        self.assertNotIn("self._owner_call_id", conference)
        self.assertIn("port_reservation: RtpPortReservation", conference)

    def test_sip_endpoint_account_list_service_is_registered_and_documented(
        self,
    ) -> None:
        services = SERVICES.read_text()
        account_services = ACCOUNT_SERVICES.read_text()
        services_yaml = SERVICES_YAML.read_text()
        icons_json = ICONS_JSON.read_text()

        self.assertIn('"list_accounts", handler_for("list_accounts")', services)
        self.assertIn('"list_accounts": list_accounts', account_services)
        self.assertIn("list_accounts:", services_yaml)
        self.assertIn('"list_accounts"', icons_json)
        self.assertIn("SIP Endpoint Accounts", services_yaml)
        self.assertIn("VoIP Stack SIP Endpoint Accounts", account_services)
        self.assertNotIn("SIP Softphone Account", services_yaml)
        self.assertNotIn("SIP Softphone Accounts", services_yaml)

    def test_add_contact_accepts_group_membership_metadata(self) -> None:
        services = SERVICES.read_text()
        services_yaml = SERVICES_YAML.read_text()

        self.assertIn('"conference_group"', services)
        self.assertIn('"conference_ring"', services)
        self.assertIn('"ring_group"', services)
        self.assertIn("conference_group:", services_yaml)
        self.assertIn("conference_ring:", services_yaml)
        self.assertIn("ring_group:", services_yaml)

    def test_registered_sip_endpoint_accounts_accept_group_membership_metadata(
        self,
    ) -> None:
        services = SERVICES.read_text()
        account_services = ACCOUNT_SERVICES.read_text()
        services_yaml = SERVICES_YAML.read_text()

        account_schema = services[
            services.index("sip_account_create_schema") : services.index(
                "sip_account_name_schema"
            )
        ]
        self.assertIn('"conference_group"', account_schema)
        self.assertIn('"conference_ring"', account_schema)
        self.assertIn('"ring_group"', account_schema)
        self.assertIn('"extension"', account_schema)
        self.assertIn("extension=str(call.data.get", account_services)
        self.assertIn('"extension": str(item.get("extension")', account_services)
        self.assertIn("conference_group=str(call.data.get", account_services)
        self.assertIn("conference_ring=bool(call.data.get", account_services)
        self.assertIn("ring_group=str(call.data.get", account_services)
        self.assertIn(
            "extension:", services_yaml[services_yaml.index("create_account:") :]
        )
        self.assertIn(
            "conference_group:", services_yaml[services_yaml.index("create_account:") :]
        )
        self.assertIn(
            "conference_ring:", services_yaml[services_yaml.index("create_account:") :]
        )
        self.assertIn(
            "ring_group:", services_yaml[services_yaml.index("create_account:") :]
        )

    def test_create_account_always_notifies_without_echoing_manual_password(
        self,
    ) -> None:
        account_services = ACCOUNT_SERVICES.read_text()
        create_account = account_services[
            account_services.index(
                "async def create_account("
            ) : account_services.index("async def remove_account(")
        ]
        self.assertIn("persistent_notification.async_create", create_account)
        self.assertNotIn(
            "if not provided_password:\n            persistent_notification.async_create",
            create_account,
        )
        self.assertIn("Password: user-provided value (not shown).", create_account)
        self.assertIn("This generated password is shown only now.", create_account)
        self.assertIn(
            'provided_password = str(call.data.get("password") or "")', create_account
        )
        self.assertNotIn(
            'provided_password = str(call.data.get("password") or "").strip()',
            create_account,
        )
        self.assertIn("if generated_password:", create_account)
        event_block = create_account[
            create_account.index("event = {") : create_account.index(
                "password_note = ("
            )
        ]
        self.assertNotIn(
            '"password": password', event_block.split("if generated_password:", 1)[0]
        )

    def test_rotated_account_password_is_delivered_once_reliably(self) -> None:
        account_services = ACCOUNT_SERVICES.read_text()
        rotate = account_services[
            account_services.index(
                "async def rotate_account_password("
            ) : account_services.index("async def set_account_enabled(")
        ]
        self.assertIn('"state": "sip_account_password_rotated"', rotate)
        self.assertIn("persistent_notification.async_create", rotate)
        self.assertIn("This generated password is shown only now.", rotate)

    def test_ring_group_timeout_has_no_configurable_ha_fallback(self) -> None:
        config_flow = CONFIG_FLOW.read_text()
        strings = STRINGS_JSON.read_text()

        self.assertNotIn("CONF_RING_GROUP_FALLBACK", config_flow)
        self.assertNotIn(
            'SelectSelectorConfig(options=["reject", "answer_ha"])', config_flow
        )
        self.assertNotIn('"ring_group_fallback"', strings)
        self.assertNotIn("CONF_RING_GROUP_FALLBACK", self.source)
        self.assertNotIn(
            "_defer_invite_to_ha_softphone(invite, route_kind=GROUP_TYPE_RING",
            self.source,
        )

    def test_trunk_dtmf_uses_phonebook_extensions_not_manual_route_map(self) -> None:
        config_flow = CONFIG_FLOW.read_text()
        strings = STRINGS_JSON.read_text()
        const = (ROOT / "custom_components" / "voip_stack" / "const.py").read_text()

        for source in (config_flow, strings, const, self.source):
            self.assertNotIn("CONF_TRUNK_DTMF_ROUTES", source)
            self.assertNotIn("trunk_dtmf_routes", source)
            self.assertNotIn("parse_dtmf_route_map", source)
        self.assertIn("def _dtmf_extension_routes(entries)", self.source)
        self.assertIn("routes = _dtmf_extension_routes(roster_entries)", self.source)
        self.assertIn("route_hint = destination or digits", self.source)
        trunk_route = self.source[
            self.source.index("async def _run_trunk_inbound_route(") : self.source.index(
                "async def _run_ring_group_call("
            )
        ]
        self.assertIn("collect_info_digits(", trunk_route)
        self.assertIn("DtmfCollector(", trunk_route)
        self.assertIn("asyncio.FIRST_COMPLETED", trunk_route)
        after_collect = trunk_route.split(
            'bucket.setdefault("trunk_info_queues", {}).pop(invite.call_id, None)',
            1,
        )[1]
        self.assertIn('invite.call_id in bucket.get("trunk_closed_calls", set())', after_collect)
        self.assertIn("bridge_ports.release()", after_collect)
        self.assertIn("remote_host=invite.remote_rtp_host", trunk_route)
        preanswer = self.source[
            self.source.index("if _is_trunk_invite(invite):") : self.source.index(
                'route_action = "default"',
                self.source.index("if _is_trunk_invite(invite):"),
            )
        ]
        self.assertIn(
            'bucket.setdefault("trunk_closed_calls", set()).discard(invite.call_id)',
            preanswer,
        )
        task_prelude = trunk_route.split("trunk_cfg = _get_trunk_config(hass)", 1)[0]
        self.assertNotIn("discard(invite.call_id)", task_prelude)

    def test_ring_group_treats_ha_member_as_parallel_contender(self) -> None:
        ring_group = self.source[
            self.source.index("async def _run_ring_group_call(") : self.source.index(
                "async def _ring_conference_members("
            )
        ]
        self.assertIn("ha_member = False", ring_group)
        self.assertIn("if _is_ha_target(member):", ring_group)
        self.assertIn("_set_ha_softphone_call_state(", ring_group)
        self.assertIn("async def _wait_ha()", ring_group)
        self.assertIn('if result == "in_call_ha"', ring_group)
        self.assertIn("registry.softphone_media[invite.call_id]", ring_group)

    def test_ring_group_simultaneous_results_are_deterministic(self) -> None:
        ring_group = self.source[
            self.source.index("async def _run_ring_group_call(") : self.source.index(
                "async def _ring_conference_members("
            )
        ]
        self.assertIn("for task in tasks:", ring_group)
        self.assertIn("if task not in done:", ring_group)
        self.assertIn("control_cancel = next(", ring_group)
        self.assertIn("and isinstance(attempt, dict)", ring_group)
        self.assertLess(
            ring_group.index("control_cancel = next("),
            ring_group.index('result == "in_call"'),
        )
        self.assertIn(
            "failure_priority.get(result, 1) > failure_priority.get(final_result, 0)",
            ring_group,
        )

    def test_ring_group_winner_clears_pending_route_before_active_hangup(self) -> None:
        ring_group = self.source[
            self.source.index("async def _run_ring_group_call(") : self.source.index(
                "async def _ring_conference_members("
            )
        ]
        external_winner = ring_group[
            ring_group.index("if ha_winner:") : ring_group.index(
                "registry.register_bridge("
            )
        ]
        self.assertIn(
            "_pending_routes(hass).pop(invite.call_id, None)", external_winner
        )

        init_py = INIT.read_text()
        hangup = init_py[
            init_py.index("async def _handle_sip_hangup_service") : init_py.index(
                "async def _refresh_phonebook_sensor"
            )
        ]
        self.assertIn('future = _pending_routes(hass)[call_id].get("future")', hangup)
        self.assertIn("if future is not None and future.done():", hangup)
        self.assertIn("_pending_routes(hass).pop(call_id, None)", hangup)

    def test_ring_group_ha_winner_publishes_connected_party_to_esp_mirrors(self) -> None:
        ring_group = self.source[
            self.source.index("async def _run_ring_group_call(") : self.source.index(
                "async def _ring_conference_members("
            )
        ]
        ha_winner = ring_group[
            ring_group.index("if ha_winner:") : ring_group.index(
                "if not isinstance(winner, OutboundLeg):"
            )
        ]
        self.assertIn("connected_party = _ha_peer_name(hass)", ha_winner)
        self.assertIn("_set_sip_bridge_call_state(", ha_winner)
        self.assertIn("callee=entry.display_name", ha_winner)
        self.assertIn("peer_name=connected_party", ha_winner)
        self.assertIn("dialed_target=entry.display_name", ha_winner)
        self.assertIn("connected_party=connected_party", ha_winner)
        self.assertIn("answered_by=connected_party", ha_winner)

    def test_ring_group_external_winner_publishes_connected_party(self) -> None:
        ring_group = self.source[
            self.source.index("async def _run_ring_group_call(") : self.source.index(
                "async def _ring_conference_members("
            )
        ]
        external_winner = ring_group[
            ring_group.index("client = winner.client") : ring_group.index(
                "terminal = await client.wait_for_dialog_termination()"
            )
        ]

        self.assertIn(
            "dialed_target = entry.display_name or invite.target", external_winner
        )
        self.assertIn(
            'connected_party = str(winner.member or "").strip() or invite.target',
            external_winner,
        )
        self.assertIn("callee=dialed_target", external_winner)
        self.assertIn("peer_name=connected_party", external_winner)
        self.assertIn("dialed_target=dialed_target", external_winner)
        self.assertIn("connected_party=connected_party", external_winner)
        self.assertIn("answered_by=connected_party", external_winner)
        self.assertIn("ha_origin = _is_ha_target(invite.caller)", external_winner)
        self.assertIn("if ha_origin:", external_winner)
        self.assertIn("_set_ha_softphone_call_state(", external_winner)

        websocket = WEBSOCKET_API.read_text()
        state = websocket[
            websocket.index("def _ha_softphone_state(") : websocket.index(
                "def _set_ha_softphone_call_state("
            )
        ]
        terminal = websocket[
            websocket.index("def _set_ha_softphone_call_state(") : websocket.index(
                "def _set_sip_bridge_call_state("
            )
        ]
        self.assertIn('"dialed_target": dialed_target', state)
        self.assertIn('store.get("last_terminal_dialed_target", "")', state)
        for field in ("connected_party", "answered_by"):
            self.assertIn(f'"{field}": store.get("{field}", "")', state)
            self.assertIn(f'"{field}",', terminal)

    def test_esp_origin_forward_to_same_source_host_is_rejected(self) -> None:
        self.assertIn("peer_target.host == invite.source_host", self.source)
        self.assertIn('SipInviteResult(486, "Busy Here"', self.source)
        self.assertIn("decline_reason=TerminalReason.BUSY.value", self.source)

    def test_softphone_settings_changes_do_not_emit_call_lifecycle_events(self) -> None:
        websocket = WEBSOCKET_API.read_text()
        group_update = websocket[
            websocket.index(
                "async def async_set_ha_softphone_settings("
            ) : websocket.index("def _sip_runtime_snapshot(")
        ]
        init_py = INIT.read_text()
        dnd_service = init_py[
            init_py.index("async def _handle_set_dnd_service(") : init_py.index(
                "async def _handle_set_ha_softphone_settings_service("
            )
        ]
        settings_service = init_py[
            init_py.index(
                "async def _handle_set_ha_softphone_settings_service("
            ) : init_py.index("async def _handle_sip_call_target_service(")
        ]

        self.assertNotIn("_fire_call_event", group_update)
        self.assertIn('if not groups["conference_group"]:', group_update)
        self.assertIn('groups["conference_ring"] = False', group_update)
        self.assertNotIn("websocket_set_ha_softphone_dnd", websocket)
        self.assertNotIn("websocket_set_ha_softphone_groups", websocket)
        self.assertNotIn("websocket_set_ha_softphone_settings", websocket)
        self.assertNotIn("_fire_call_event", dnd_service)
        self.assertNotIn("_fire_call_event", settings_service)

    def test_trunk_without_dtmf_preanswer_does_not_allocate_relay_ports(self) -> None:
        on_invite = self.source[self.source.index("async def _on_invite(invite:") :]
        trunk_branch = on_invite[
            on_invite.index("if _is_trunk_invite(invite):") : on_invite.index(
                "loop = asyncio.get_running_loop()"
            )
        ]
        no_dtmf_branch = trunk_branch[
            trunk_branch.index("if not dtmf_preanswer:") : trunk_branch.index("else:")
        ]
        dtmf_branch = trunk_branch[trunk_branch.index("else:") :]
        self.assertNotIn("RtpPortReservation.allocate", no_dtmf_branch)
        self.assertIn("RtpPortReservation.allocate(hass)", dtmf_branch)

    def test_remote_bridge_termination_closes_winning_leg_and_relay(self) -> None:
        terminated = self.source[self.source.index("async def _on_terminated(") :]
        bridge_branch = terminated[
            terminated.index("if relay is not None or client is not None:") :
        ]
        bridge_branch = bridge_branch[: bridge_branch.index("if (")]
        pre_cleanup = terminated[
            : terminated.index("if relay is not None or client is not None:")
        ]
        self.assertIn(
            "session = registry.sessions.get(registry.resolve_session_id(call_id))",
            pre_cleanup,
        )
        self.assertIn(
            'event_caller = invite.caller if invite is not None else (session.caller if session is not None else "")',
            pre_cleanup,
        )
        self.assertIn(
            'event_callee = invite.target if invite is not None else (session.callee if session is not None else "")',
            pre_cleanup,
        )
        self.assertIn("await async_cleanup_sip_runtime(", bridge_branch)
        self.assertIn("relay=relay", bridge_branch)
        self.assertIn("client=client", bridge_branch)
        self.assertIn("watcher=watcher", bridge_branch)
        self.assertIn("terminate_client=True", bridge_branch)
        self.assertIn("caller=event_caller", bridge_branch)
        self.assertIn("callee=event_callee", bridge_branch)
        self.assertIn('"target": event_callee', bridge_branch)

    def test_config_entry_reload_restores_runtime_event_listeners(self) -> None:
        init_py = INIT.read_text()
        setup = init_py[
            init_py.index("async def _async_setup_shared(") : init_py.index(
                "async def async_setup("
            )
        ]
        initialized = setup[
            setup.index('if hass.data.get(DOMAIN, {}).get("initialized"):') : setup.index(
                "hass.data.setdefault(DOMAIN, {})"
            )
        ]
        self.assertIn("_register_esp_state_event_bridge(hass)", initialized)
        self.assertIn("_register_phonebook_service_event_sync(hass)", initialized)


if __name__ == "__main__":
    unittest.main()
