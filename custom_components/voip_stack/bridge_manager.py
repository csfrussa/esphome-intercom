"""B2BUA bridge lifecycle helpers."""

from __future__ import annotations

from collections.abc import Callable

from homeassistant.core import HomeAssistant

from .endpoint_lifecycle import call_registry
from .fsm import TerminalReason
from .session_cleanup import async_cleanup_sip_runtime


async def async_terminate_sip_bridge(
    hass: HomeAssistant,
    call_id: str,
    *,
    terminal_reason: str = TerminalReason.LOCAL_HANGUP.value,
    send_bye: Callable[[str], bool],
) -> tuple[bool, str, str, bool, bool]:
    """Terminate a B2BUA bridge by either source or destination leg call-id."""
    if not call_id:
        return False, "", "", False, False
    registry = call_registry(hass)
    source_call_id, dest_call_id, relay, client, watcher, called_by_dest = registry.detach_bridge(call_id)
    if not source_call_id:
        return False, "", "", False, False

    cleanup = await async_cleanup_sip_runtime(
        relay=relay,
        client=client if dest_call_id else None,
        watcher=watcher if dest_call_id else None,
        terminate_client=not called_by_dest,
        relay_first=True,
    )

    source_bye = send_bye(source_call_id)
    registry.finish_and_pop(source_call_id, reason=terminal_reason or TerminalReason.LOCAL_HANGUP.value)
    return True, source_call_id, dest_call_id, cleanup.client_closed, source_bye
