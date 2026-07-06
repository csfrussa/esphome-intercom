"""Shared SIP B2BUA bridge primitives."""

from __future__ import annotations

from collections.abc import Callable

from .sip_client import SipCallClient
from .sip_listener import SipInvite
from .sip_rtp_bridge import RtpPeer, SipRtpRelay


def build_invite_client_relay(
    *,
    invite: SipInvite,
    client: SipCallClient,
    source_relay_port: int,
    dest_relay_port: int,
    debug_capture: bool = False,
    on_release: Callable[[tuple[int, int]], None] | None = None,
) -> SipRtpRelay:
    """Build the RTP relay for an inbound INVITE bridged to an outbound client."""
    if client.dialog is None:
        raise ValueError("cannot bridge SIP client without an established dialog")
    return SipRtpRelay(
        left=RtpPeer(
            host=invite.remote_rtp_host,
            port=invite.remote_rtp_port,
            payload_type=invite.recv_format.payload_type,
            audio_format=invite.recv_format.audio_format,
            rtp_format=invite.recv_format,
            send_payload_type=invite.send_format.payload_type,
            send_audio_format=invite.send_format.audio_format,
            send_rtp_format=invite.send_format,
        ),
        right=RtpPeer(
            host=client.dialog.remote_rtp_host,
            port=client.dialog.remote_rtp_port,
            payload_type=client.dialog.recv_format.payload_type,
            audio_format=client.dialog.recv_format.audio_format,
            rtp_format=client.dialog.recv_format,
            send_payload_type=client.dialog.send_format.payload_type,
            send_audio_format=client.dialog.send_format.audio_format,
            send_rtp_format=client.dialog.send_format,
        ),
        left_port=source_relay_port,
        right_port=dest_relay_port,
        debug_capture=debug_capture,
        capture_name=f"{invite.call_id}_{client.dialog_ids.call_id}",
        on_release=on_release,
    )
