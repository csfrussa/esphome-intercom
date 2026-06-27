#!/usr/bin/env python3
"""Generate and validate the SIP intercom qualification matrix.

This is a planning harness, not a SIP runner. It gives the concrete scenario
IDs that the automated runners must implement with unit tests, simulator
scenarios, SIPp, PJSUA/baresip/manual checks, Playwright, and live ESP devices.
"""

from __future__ import annotations

import argparse
from dataclasses import asdict, dataclass
import json
from pathlib import Path
import sys
from typing import Iterable


TRANSPORTS = ("sip_udp", "sip_tcp")
ROUTES = ("direct", "ha_bridge", "ha_softphone", "explicit_uri", "name_via_ha")
DIRECTIONS = ("esp_to_esp", "esp_to_ha", "ha_to_esp", "pc_to_esp", "esp_to_pc")
AUDIO_ROLES = ("full_duplex", "mic_only", "speaker_only", "muted_mic", "muted_speaker")
FORMATS = (
    "16000:s16le:1:32",
    "16000:s16le:1:20",
    "48000:s16le:1:10",
    "16000:s24le:1:20",
)
FAILURES = (
    ("timeout_no_provisional", 408, "timeout"),
    ("dnd_busy", 486, "busy"),
    ("decline", 603, "declined"),
    ("cancel_before_answer", 487, "cancelled"),
    ("bye_remote", 200, "remote_hangup"),
    ("media_incompatible", 488, "media_incompatible"),
    ("auth_401", 401, "auth_required_unsupported"),
    ("auth_407", 407, "proxy_auth_required_unsupported"),
    ("endpoint_unreachable", 0, "transport_unreachable"),
)
RACES = (
    "incoming_while_ringing",
    "incoming_while_in_call",
    "double_invite_same_call_id",
    "simultaneous_hangup",
    "late_final_after_cancel",
    "rtp_after_terminal",
)


@dataclass(frozen=True, slots=True)
class Scenario:
    id: str
    category: str
    direction: str
    route: str
    caller_transport: str
    callee_transport: str
    audio_role: str
    tx_format: str
    rx_format: str
    expected_status: int
    terminal_reason: str
    tools: tuple[str, ...]
    assertions: tuple[str, ...]


def _slug(value: str) -> str:
    return value.replace(":", "_").replace(";", "_").replace("-", "_")


def _call_tools(direction: str, route: str) -> tuple[str, ...]:
    tools = ["pytest"]
    if direction in ("pc_to_esp", "esp_to_pc"):
        tools.append("external_softphone")
    if route in ("direct", "explicit_uri", "name_via_ha", "ha_bridge"):
        tools.append("sipp")
    if route in ("ha_softphone", "ha_bridge", "name_via_ha"):
        tools.append("ha_local")
    if direction in ("ha_to_esp", "esp_to_ha"):
        tools.append("playwright")
    return tuple(dict.fromkeys(tools))


def _base_assertions(route: str) -> tuple[str, ...]:
    assertions = [
        "SipPhoneState.state",
        "SipPhoneState.call_id",
        "SipPhoneState.caller",
        "SipPhoneState.callee",
        "SipPhoneState.sip_transport",
        "SipPhoneState.selected_tx_format",
        "SipPhoneState.selected_rx_format",
        "SipPhoneState.rtp_counters",
        "terminal_reason",
        "info_log_lifecycle",
        "debug_sip_trace",
    ]
    if route == "direct":
        assertions.append("ha_bridge_not_used")
    if route in ("ha_bridge", "name_via_ha"):
        assertions.append("ha_bridge_used")
    if route == "ha_softphone":
        assertions.append("ha_card_mirror")
    return tuple(assertions)


def generate_matrix() -> list[Scenario]:
    scenarios: list[Scenario] = []

    for direction in DIRECTIONS:
        for route in ROUTES:
            if direction == "esp_to_esp" and route == "ha_softphone":
                continue
            if direction in ("esp_to_ha", "ha_to_esp") and route == "direct":
                continue
            if direction in ("pc_to_esp", "esp_to_pc") and route == "ha_softphone":
                continue
            for caller_transport in TRANSPORTS:
                for callee_transport in TRANSPORTS:
                    for audio_role in AUDIO_ROLES:
                        for fmt in FORMATS:
                            scenario_id = "-".join(
                                (
                                    "ok",
                                    direction,
                                    route,
                                    caller_transport,
                                    callee_transport,
                                    audio_role,
                                    _slug(fmt),
                                )
                            )
                            scenarios.append(
                                Scenario(
                                    id=scenario_id,
                                    category="successful_call",
                                    direction=direction,
                                    route=route,
                                    caller_transport=caller_transport,
                                    callee_transport=callee_transport,
                                    audio_role=audio_role,
                                    tx_format=fmt,
                                    rx_format=fmt,
                                    expected_status=200,
                                    terminal_reason="",
                                    tools=_call_tools(direction, route),
                                    assertions=_base_assertions(route),
                                )
                            )

    for failure, status, reason in FAILURES:
        for caller_transport in TRANSPORTS:
            for callee_transport in TRANSPORTS:
                for route in ("direct", "ha_bridge", "ha_softphone", "name_via_ha"):
                    scenario_id = "-".join((failure, route, caller_transport, callee_transport))
                    scenarios.append(
                        Scenario(
                            id=scenario_id,
                            category="failure",
                            direction="esp_to_esp" if route != "ha_softphone" else "esp_to_ha",
                            route=route,
                            caller_transport=caller_transport,
                            callee_transport=callee_transport,
                            audio_role="full_duplex",
                            tx_format=FORMATS[0],
                            rx_format=FORMATS[0],
                            expected_status=status,
                            terminal_reason=reason,
                            tools=tuple(dict.fromkeys(("pytest", "sipp", "ha_local"))),
                            assertions=(
                                "SipPhoneState.terminal_reason",
                                "sip_status_code",
                                "caller_terminal_screen",
                                "callee_terminal_screen",
                                "info_log_lifecycle",
                                "debug_sip_trace",
                            ),
                        )
                    )

    for race in RACES:
        for route in ("direct", "ha_bridge", "ha_softphone"):
            scenarios.append(
                Scenario(
                    id=f"race-{race}-{route}",
                    category="race",
                    direction="esp_to_esp" if route != "ha_softphone" else "esp_to_ha",
                    route=route,
                    caller_transport="sip_udp",
                    callee_transport="sip_udp",
                    audio_role="full_duplex",
                    tx_format=FORMATS[0],
                    rx_format=FORMATS[0],
                    expected_status=0,
                    terminal_reason="protocol_error" if race == "double_invite_same_call_id" else "",
                    tools=tuple(dict.fromkeys(("pytest", "simulator", "sipp"))),
                    assertions=(
                        "single_terminal_transition",
                        "no_late_media_promotion",
                        "no_duplicate_dialog",
                        "debug_sip_trace",
                    ),
                )
            )

    return scenarios


def validate_matrix(scenarios: Iterable[Scenario]) -> list[str]:
    scenarios = list(scenarios)
    errors: list[str] = []
    ids = [scenario.id for scenario in scenarios]
    if len(ids) != len(set(ids)):
        errors.append("duplicate scenario ids")

    def has(**wanted: str) -> bool:
        return any(all(getattr(s, key) == value for key, value in wanted.items()) for s in scenarios)

    for caller in TRANSPORTS:
        for callee in TRANSPORTS:
            if not has(caller_transport=caller, callee_transport=callee):
                errors.append(f"missing transport pair {caller}->{callee}")
    for route in ROUTES:
        if not has(route=route):
            errors.append(f"missing route {route}")
    for direction in DIRECTIONS:
        if not has(direction=direction):
            errors.append(f"missing direction {direction}")
    for role in AUDIO_ROLES:
        if not has(audio_role=role):
            errors.append(f"missing audio role {role}")
    for fmt in FORMATS:
        if not has(tx_format=fmt):
            errors.append(f"missing format {fmt}")
    for failure, status, reason in FAILURES:
        if not any(s.id.startswith(failure) and s.expected_status == status and s.terminal_reason == reason for s in scenarios):
            errors.append(f"missing failure {failure}")
    for race in RACES:
        if not any(s.id.startswith(f"race-{race}-") for s in scenarios):
            errors.append(f"missing race {race}")
    if not any("playwright" in s.tools and "ha_card_mirror" in s.assertions for s in scenarios):
        errors.append("missing Playwright HA card coverage")
    if not any("external_softphone" in s.tools for s in scenarios):
        errors.append("missing external softphone media coverage")
    return errors


def summary(scenarios: list[Scenario]) -> dict[str, object]:
    by_category: dict[str, int] = {}
    by_tool: dict[str, int] = {}
    for scenario in scenarios:
        by_category[scenario.category] = by_category.get(scenario.category, 0) + 1
        for tool in scenario.tools:
            by_tool[tool] = by_tool.get(tool, 0) + 1
    return {
        "count": len(scenarios),
        "categories": dict(sorted(by_category.items())),
        "tools": dict(sorted(by_tool.items())),
        "transports": TRANSPORTS,
        "routes": ROUTES,
        "directions": DIRECTIONS,
        "audio_roles": AUDIO_ROLES,
        "formats": FORMATS,
        "failures": [name for name, _status, _reason in FAILURES],
        "races": RACES,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="print full matrix as JSON")
    parser.add_argument("--summary", action="store_true", help="print matrix summary as JSON")
    parser.add_argument("--write-json", type=Path, help="write full matrix to this path")
    parser.add_argument("--validate", action="store_true", help="validate coverage and exit non-zero on gaps")
    args = parser.parse_args(argv)

    scenarios = generate_matrix()
    errors = validate_matrix(scenarios)

    if args.write_json:
        args.write_json.parent.mkdir(parents=True, exist_ok=True)
        args.write_json.write_text(
            json.dumps([asdict(s) for s in scenarios], indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
    if args.json:
        print(json.dumps([asdict(s) for s in scenarios], indent=2, sort_keys=True))
    if args.summary or not args.json:
        print(json.dumps(summary(scenarios), indent=2, sort_keys=True))
    if args.validate and errors:
        for error in errors:
            print(f"matrix error: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
