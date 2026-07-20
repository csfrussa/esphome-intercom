"""Shared ring-group preflight and browser-leg settlement."""

from __future__ import annotations

import logging

from homeassistant.core import HomeAssistant

from .dial_fork import DialDisposition
from .outbound_attempts import BrowserLeg
from .phone_endpoint import EndpointAvailability, EndpointKind
from .websocket_api import _set_ha_softphone_call_state


_LOGGER = logging.getLogger(__name__)


def endpoint_preflight_disposition(
    endpoint,
    *,
    call_id: str,
    browser: bool,
) -> DialDisposition | None:
    """Classify a logical endpoint before creating or claiming a dial leg."""

    if endpoint is None:
        return None
    if endpoint.dnd:
        return DialDisposition.DND
    if browser:
        if endpoint.availability is EndpointAvailability.UNAVAILABLE:
            return DialDisposition.UNAVAILABLE
    elif endpoint.availability is not EndpointAvailability.AVAILABLE:
        return DialDisposition.UNAVAILABLE
    if endpoint.active_call_id and endpoint.active_call_id != call_id:
        return DialDisposition.BUSY
    return None


def settle_browser_candidates(
    hass: HomeAssistant,
    registry,
    browser_legs: list[BrowserLeg],
    *,
    call_id: str,
    caller: str,
    callee: str,
    state: str,
    reason: str,
    route_kind: str,
    keep_endpoint_id: str = "",
) -> None:
    """Release and publish every browser candidate except one committed winner."""

    for leg in browser_legs:
        if leg.endpoint_id == keep_endpoint_id:
            continue
        registry.release_endpoint_claim(call_id, leg.endpoint_id)
        try:
            _set_ha_softphone_call_state(
                hass,
                state,
                endpoint_id=leg.endpoint_id,
                session_device_id=leg.device_id,
                caller=caller,
                callee=callee,
                peer_name=caller,
                direction="incoming",
                call_id=call_id,
                reason=reason,
                terminal_reason=reason,
                route_kind=route_kind,
                last_sip_event="SIP_RESPONSE",
            )
        except Exception:  # noqa: BLE001 - observer failure must not leak claims.
            _LOGGER.exception(
                "SIP ring group candidate cleanup publication failed "
                "call_id=%s endpoint_id=%s",
                call_id,
                leg.endpoint_id,
            )


def endpoint_is_esphome(endpoint) -> bool:
    """Return whether a claimed logical destination owns an ESP transport."""

    return endpoint is not None and endpoint.kind is EndpointKind.ESPHOME
