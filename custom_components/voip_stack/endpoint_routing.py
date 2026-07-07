"""Pure routing helpers used by the HA SIP endpoint."""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant

from .audio_format import (
    AudioFormat,
    HA_SIP_PCM_RX_FORMATS,
    HA_SIP_PCM_TX_FORMATS,
    choose_common_frame_ms,
    parse_audio_format_list,
)
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


def device_formats(device: dict | None, key: str) -> list[AudioFormat]:
    if not device:
        return []
    value = device.get(key)
    if value in (None, ""):
        return []
    raw = value if isinstance(value, str) else ";".join(value or [])
    if not raw.strip():
        return []
    try:
        return parse_audio_format_list(raw)
    except ValueError as err:
        _LOGGER.warning(
            "Ignoring invalid %s on %s: %s",
            key,
            (device or {}).get("name") or (device or {}).get("device_id"),
            err,
        )
        return []


def roster_entry_formats(entry, key: str) -> list[AudioFormat]:
    """Return audio formats from a canonical roster entry metadata field."""
    if entry is None:
        return []
    metadata = getattr(entry, "metadata", {}) or {}
    value = metadata.get(key)
    if value in (None, ""):
        return []
    raw = ";".join(str(item) for item in value) if isinstance(value, list) else str(value or "")
    if not raw.strip():
        return []
    try:
        return parse_audio_format_list(raw)
    except ValueError as err:
        _LOGGER.warning(
            "Ignoring invalid roster %s on %s: %s",
            key,
            getattr(entry, "display_name", None) or getattr(entry, "id", ""),
            err,
        )
        return []


def sip_target_audio_profile(
    *,
    remote_tx_formats: list[AudioFormat] | None,
    remote_rx_formats: list[AudioFormat] | None,
    target: str,
) -> tuple[list[AudioFormat], list[AudioFormat]]:
    """Constrain HA SIP offers to formats that can actually work with target."""
    remote_tx = list(remote_tx_formats or [])
    remote_rx = list(remote_rx_formats or [])
    send_candidates = (
        [fmt for fmt in HA_SIP_PCM_TX_FORMATS if fmt in set(remote_rx)]
        if remote_rx else list(HA_SIP_PCM_TX_FORMATS)
    )
    recv_candidates = (
        [fmt for fmt in HA_SIP_PCM_RX_FORMATS if fmt in set(remote_tx)]
        if remote_tx else list(HA_SIP_PCM_RX_FORMATS)
    )
    if not send_candidates or not recv_candidates:
        _LOGGER.warning(
            "No compatible directional SIP PCM profile for %s "
            "(ha_send=%s ha_recv=%s remote_tx=%s remote_rx=%s)",
            target,
            [fmt.wire_token() for fmt in HA_SIP_PCM_TX_FORMATS],
            [fmt.wire_token() for fmt in HA_SIP_PCM_RX_FORMATS],
            [fmt.wire_token() for fmt in remote_tx],
            [fmt.wire_token() for fmt in remote_rx],
        )
        return [], []

    common_frame_ms = choose_common_frame_ms(send_candidates, recv_candidates)
    if common_frame_ms is None:
        _LOGGER.warning(
            "No common SIP ptime for %s (send=%s recv=%s)",
            target,
            [fmt.wire_token() for fmt in send_candidates],
            [fmt.wire_token() for fmt in recv_candidates],
        )
        return [], []

    send_candidates = [fmt for fmt in send_candidates if fmt.frame_ms == common_frame_ms]
    recv_candidates = [fmt for fmt in recv_candidates if fmt.frame_ms == common_frame_ms]
    _LOGGER.debug(
        "Directional SIP PCM profile for %s: ptime=%sms send=%s recv=%s",
        target,
        common_frame_ms,
        [fmt.wire_token() for fmt in send_candidates],
        [fmt.wire_token() for fmt in recv_candidates],
    )
    return send_candidates, recv_candidates


def roster_from_peers(hass: HomeAssistant, peers: list[Peer], registered_entries) -> list:
    from .groups import collect_groups
    from .roster import RosterEntry, merge_roster_overrides

    entries: list[RosterEntry] = []
    for peer in peers:
        entries.append(
            RosterEntry(
                id=peer.name,
                name=peer.name,
                address=peer.host,
                extension=peer.extension,
                port=int(peer.sip_port or 0),
                metadata={
                    "local_ha": bool(peer.is_ha),
                    "sip_transport": (
                        str((peer.device or {}).get("sip_transport") or "tcp").lower()
                        if peer.is_ha or peer.device is not None
                        else ""
                    ),
                    "sip_port": peer.sip_port,
                    "rtp_port": peer.rtp_port,
                    "audio_mode": peer.audio_mode,
                    "tx_formats": list(peer.tx_formats or []),
                    "rx_formats": list(peer.rx_formats or []),
                    "conference_group": peer.conference_group,
                    "conference_ring": bool(peer.conference_ring),
                    "ring_group": peer.ring_group,
                },
            )
        )
    manual_entries = manual_roster_entries(hass)
    entries = merge_roster_overrides(entries, manual_entries)
    entries.extend(registered_entries)
    groups = collect_groups(peers, manual_entries, registered_entries, existing_entries=entries)
    for group in groups.values():
        entries.append(
            RosterEntry(
                id=group.name,
                name=group.name,
                ha_bridge=True,
                enabled=bool(group.members),
                metadata={
                    "group_type": group.group_type,
                    "members": list(group.members),
                    "ring_members": list(group.ring_members),
                    "auto": bool(group.auto),
                },
            )
        )
    return entries
