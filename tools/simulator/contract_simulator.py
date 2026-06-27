#!/usr/bin/env python3
"""Small JSON-RPC contract simulator for intercom scenario matrices.

This is not the ESPHome host backend. It is a deterministic contract runner
used while the full virtual device is being built, so scenario files can fail
fast when expected SIP/FSM/card semantics drift.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import socket
import sys
from typing import Any


def _idle_state() -> dict[str, Any]:
    return {
        "esp": {"state": "idle", "caller": "", "destination": "", "last_reason": "", "visible_contacts": 0, "selected": ""},
        "caller": {"state": "idle", "last_reason": ""},
        "callee": {"state": "idle", "caller": "", "last_reason": ""},
        "second": {"state": "idle", "last_reason": ""},
        "softphone": {"state": "idle", "caller": "", "last_reason": ""},
        "bridge": {"state": "idle", "left": "", "right": ""},
        "audio": {"tx_ready": False, "rx_ready": False, "owner": "none"},
        "sip": {"last_status": 0, "decline_reason": "", "auth_reason": ""},
        "led": {"color": "", "effect": None, "forbidden_effect": "Spin"},
        "media": {"state": "idle"},
        "intercom": {"state": "idle", "caller": ""},
        "card": {"mode": "", "controlled_device": "", "rendered_state": "idle", "source": ""},
        "backend": {"browser_audio": False},
        "phonebook": {"revision": 0, "duplicate_ids": False},
        "ha": {"visible_contacts": 0},
        "options": {"esp": {}, "caller": {}},
    }


class ContractSimulator:
    def __init__(self) -> None:
        self.state = _idle_state()

    def reset(self) -> dict[str, Any]:
        self.state = _idle_state()
        return self.get_snapshot()

    def get_snapshot(self) -> dict[str, Any]:
        return json.loads(json.dumps(self.state))

    def shutdown(self) -> dict[str, Any]:
        return {"ok": True, "shutdown": True}

    def press_button(self, button: str) -> dict[str, Any]:
        if button == "call":
            self.state["esp"].update({"state": "calling"})
            self.state["backend"]["browser_audio"] = False
        elif button == "answer":
            if self.state["softphone"]["state"] == "ringing":
                self.state["softphone"]["state"] = "in_call"
                self.state["audio"].update({"tx_ready": True, "rx_ready": True, "owner": "intercom"})
                self.state["sip"]["last_status"] = 200
            if self.state["esp"]["state"] == "ringing":
                self.state["esp"]["state"] = "in_call"
                self.state["audio"].update({"tx_ready": True, "rx_ready": True, "owner": "intercom"})
                self.state["led"].update({"color": "green", "effect": None})
        return self.get_snapshot()

    def touch(self, target: str) -> dict[str, Any]:
        return {"ok": True, "target": target}

    def advance_time(self, duration_ms: int) -> dict[str, Any]:
        return {"ok": True, "duration_ms": duration_ms}

    def inject_fault(self, name: str) -> dict[str, Any]:
        self.state["fault"] = {"name": name}
        return self.get_snapshot()

    def inject_event(self, **event: Any) -> dict[str, Any]:
        typ = str(event.get("type") or "")
        if typ == "set_option":
            target = str(event.get("target") or "esp")
            option = str(event.get("option") or "")
            self.state["options"].setdefault(target, {})[option] = event.get("value")
        elif typ == "ha_call":
            self._ha_call(str(event.get("target") or ""), str(event.get("caller") or "Casa"))
        elif typ == "esp_call":
            self._esp_call(str(event.get("source") or ""), str(event.get("destination") or ""), str(event.get("route") or "direct"))
        elif typ == "sip_invite":
            self._sip_invite(str(event.get("caller") or ""), str(event.get("callee") or ""), str(event.get("call_id") or ""))
        elif typ == "sip_offer":
            codecs = [str(item).upper() for item in event.get("codecs", []) if str(item)]
            if not any(codec in {"L16", "L24"} for codec in codecs):
                self.state["sip"].update({"last_status": 488, "decline_reason": "media_incompatible"})
                self.state["softphone"].update({"state": "idle", "last_reason": "media_incompatible"})
            else:
                self._sip_invite(str(event.get("caller") or ""), str(event.get("callee") or ""), str(event.get("call_id") or ""))
        elif typ == "sip_auth_challenge":
            status = int(event.get("status") or 401)
            reason = "proxy_auth_required_unsupported" if status == 407 else "auth_required_unsupported"
            self.state["sip"].update({"last_status": status, "auth_reason": reason})
            self.state["caller"].update({"state": "idle", "last_reason": reason})
            self.state["softphone"].update({"state": "idle", "last_reason": reason})
        elif typ == "browser_audio_ready":
            self.state["audio"].update({"tx_ready": True, "rx_ready": True})
        elif typ in {"esp_bye", "remote_bye"}:
            self.state["softphone"].update({"state": "idle", "last_reason": "remote_hangup"})
            self.state["card"].update({"rendered_state": "idle"})
        elif typ == "remote_cancel":
            self.state["softphone"].update({"state": "idle", "last_reason": "cancelled"})
            self.state["card"].update({"rendered_state": "idle"})
        elif typ == "ha_bye":
            self.state["esp"].update({"state": "idle"})
            self.state["led"].update({"effect": None})
        elif typ == "ha_cancel":
            self.state["esp"].update({"state": "idle"})
            self.state["led"].update({"effect": None})
        elif typ == "late_media_after_terminal":
            self.state["esp"].update({"state": "idle"})
            self.state["intercom"].update({"state": "idle"})
            self.state["led"].update({"effect": None})
        elif typ == "ha_softphone_decline":
            reason = str(event.get("reason") or "declined")
            self.state["esp"].update({"state": "idle", "last_reason": reason})
            self.state["softphone"].update({"state": "idle", "last_reason": reason})
        elif typ == "callee_decline":
            reason = str(event.get("reason") or "declined")
            self.state["caller"].update({"state": "idle", "last_reason": reason})
            self.state["callee"].update({"state": "idle", "last_reason": reason})
            self.state["bridge"].update({"state": "idle"})
        elif typ == "media_start":
            self.state["media"]["state"] = "playing"
            self.state["intercom"]["state"] = "idle"
        elif typ == "phonebook_push":
            contacts = event.get("contacts") if isinstance(event.get("contacts"), list) else []
            self.state["phonebook"].update({"revision": self.state["phonebook"]["revision"] + 1, "duplicate_ids": False})
            visible = max(0, len(contacts) - 1)
            self.state["ha"]["visible_contacts"] = visible
            self.state["esp"].update({"visible_contacts": visible, "selected": "Casa" if contacts else ""})
        elif typ == "card_select":
            self.state["card"].update({"mode": str(event.get("card") or ""), "controlled_device": str(event.get("target") or "")})
        elif typ == "esp_state":
            self.state["card"].update({"rendered_state": str(event.get("state") or ""), "source": "esp_snapshot"})
            self.state["esp"].update({"state": str(event.get("state") or ""), "caller": str(event.get("caller") or "")})
        return self.get_snapshot()

    def _ha_call(self, target: str, caller: str) -> None:
        if self.state["options"]["esp"].get("dnd"):
            self.state["esp"].update({"state": "idle", "last_reason": "DND"})
            self.state["sip"].update({"last_status": 486, "decline_reason": "DND"})
            return
        if self.state["options"]["esp"].get("auto_answer"):
            self.state["esp"].update({"state": "in_call", "caller": caller})
            self.state["audio"].update({"owner": "intercom"})
            self.state["led"].update({"color": "green", "effect": None})
            return
        self.state["esp"].update({"state": "ringing", "caller": caller, "destination": target})
        self.state["led"].update({"color": "red", "effect": "Ringing"})

    def _esp_call(self, source: str, destination: str, route: str) -> None:
        if destination == "Casa":
            if self.state["softphone"]["state"] in {"ringing", "in_call"}:
                self.state["second"].update({"state": "idle", "last_reason": "busy"})
                self.state["sip"]["last_status"] = 486
                return
            self.state["esp"].update({"state": "calling", "destination": "Casa"})
            self.state["softphone"].update({"state": "ringing", "caller": source})
            return
        if destination == "Virtual S3" and self.state["esp"]["state"] in {"ringing", "in_call"}:
            self.state["second"].update({"state": "idle", "last_reason": "busy"})
            self.state["bridge"].update({"state": "idle"})
            return
        if route == "bridge" or self.state["options"]["caller"].get("sip_bridge"):
            self.state["bridge"].update({"state": "ringing", "left": source, "right": destination})
        self.state["caller"].update({"state": "calling"})
        self.state["callee"].update({"state": "ringing", "caller": source})

    def _sip_invite(self, caller: str, callee: str, call_id: str) -> None:
        if callee == "Casa":
            self.state["softphone"].update({"state": "ringing", "caller": caller})
            self.state["card"].update({"mode": "ha_softphone", "rendered_state": "ringing", "source": "ha_softphone_snapshot"})
        else:
            self.state["intercom"].update({"state": "ringing", "caller": caller})
            self.state["media"]["state"] = "paused"
            self.state["audio"]["owner"] = "intercom"


def serve(socket_path: Path) -> int:
    if socket_path.exists():
        socket_path.unlink()
    socket_path.parent.mkdir(parents=True, exist_ok=True)
    sim = ContractSimulator()
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as server:
        server.bind(str(socket_path))
        server.listen(8)
        while True:
            conn, _ = server.accept()
            with conn:
                line = conn.recv(65536).split(b"\n", 1)[0]
                if not line:
                    continue
                request = json.loads(line.decode("utf-8"))
                method = request.get("method")
                params = request.get("params") or {}
                try:
                    result = getattr(sim, method)(**params)
                    response = {"jsonrpc": "2.0", "id": request.get("id"), "result": result}
                except Exception as err:  # noqa: BLE001 - returned over JSON-RPC.
                    response = {"jsonrpc": "2.0", "id": request.get("id"), "error": str(err)}
                conn.sendall(json.dumps(response).encode("utf-8") + b"\n")
                if method == "shutdown":
                    break
    socket_path.unlink(missing_ok=True)
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--socket", type=Path, default=Path("test_runs/simulator/intercom-sim.sock"))
    args = parser.parse_args(argv)
    try:
        return serve(args.socket)
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
