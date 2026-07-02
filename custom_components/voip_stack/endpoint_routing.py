"""Pure routing helpers used by the HA SIP endpoint."""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant

from .audio_format import AudioFormat, parse_audio_format_list
from .peer import Peer
from .store import manual_roster_entries

_LOGGER = logging.getLogger(__name__)


def same_route_name(left: str, right: str) -> bool:
    def norm(value: str) -> str:
        return "".join(ch for ch in value.lower() if ch.isalnum())

    return bool(left and right and norm(left) == norm(right))


def peer_for_target(target: str, peers: list[Peer]) -> Peer | None:
    for peer in peers:
        if peer.is_ha:
            continue
        if same_route_name(target, peer.name):
            return peer
    return None


def peer_audio_formats(peer: Peer | None, key: str) -> list[AudioFormat]:
    if peer is None:
        return []
    raw = ";".join(str(item) for item in (peer.tx_formats if key == "tx_formats" else peer.rx_formats) or [])
    if not raw.strip():
        return []
    try:
        return parse_audio_format_list(raw)
    except ValueError as err:
        _LOGGER.warning("Ignoring invalid peer %s on %s: %s", key, peer.name, err)
        return []


def roster_from_peers(hass: HomeAssistant, peers: list[Peer], registered_entries) -> list:
    from .roster import RosterEntry, merge_roster_overrides

    entries: list[RosterEntry] = []
    for peer in peers:
        entries.append(
            RosterEntry(
                id=peer.name,
                name=peer.name,
                kind="ha" if peer.is_ha else "esp",
                address=peer.host,
                metadata={
                    "sip_transport": (
                        str((peer.device or {}).get("sip_transport") or "tcp").lower()
                        if peer.is_ha or peer.device is not None
                        else ""
                    ),
                    "sip_port": peer.sip_port,
                    "rtp_port": peer.rtp_port,
                    "audio_mode": peer.audio_mode,
                },
            )
        )
    entries = merge_roster_overrides(entries, manual_roster_entries(hass))
    entries.extend(registered_entries)
    return entries
