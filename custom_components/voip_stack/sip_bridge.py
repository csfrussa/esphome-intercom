"""Shared SIP B2BUA bridge primitives."""

from __future__ import annotations

from collections.abc import Callable

from . import sdp
from .sip_client import SipCallClient, SipDialog
from .sip_listener import SipInvite
from .sip_rtp_bridge import RtpPeer, SipRtpRelay
from .sip_video_relay import VideoRtpPeer


def _invite_dtmf_payload_type(invite: SipInvite) -> int | None:
    formats = sdp.offered_dtmf_formats(invite.remote_sdp)
    return formats[0].payload_type if formats else None


def invite_rtp_peer(invite: SipInvite) -> RtpPeer:
    """Build the relay peer represented by one inbound offer."""

    return RtpPeer(
        host=invite.remote_rtp_host,
        port=invite.remote_rtp_port,
        payload_type=invite.recv_format.payload_type,
        audio_format=invite.recv_format.audio_format,
        rtp_format=invite.recv_format,
        send_payload_type=invite.send_format.payload_type,
        send_audio_format=invite.send_format.audio_format,
        send_rtp_format=invite.send_format,
        dtmf_payload_type=_invite_dtmf_payload_type(invite),
        can_send=invite.remote_audio_direction in {"sendonly", "sendrecv"},
        can_receive=(
            invite.remote_audio_direction in {"recvonly", "sendrecv"}
            and not invite.remote_audio_connection_held
        ),
        connection_held=invite.remote_audio_connection_held,
        signaling_host=invite.source_host,
    )


def dialog_rtp_peer(dialog: SipDialog) -> RtpPeer:
    """Build the relay peer represented by one established outbound dialog."""

    return RtpPeer(
        host=dialog.remote_rtp_host,
        port=dialog.remote_rtp_port,
        payload_type=dialog.recv_format.payload_type,
        audio_format=dialog.recv_format.audio_format,
        rtp_format=dialog.recv_format,
        send_payload_type=dialog.send_format.payload_type,
        send_audio_format=dialog.send_format.audio_format,
        send_rtp_format=dialog.send_format,
        dtmf_payload_type=dialog.dtmf_payload_type,
        can_send=dialog.remote_audio_direction in {"sendonly", "sendrecv"},
        can_receive=(
            dialog.remote_audio_direction in {"recvonly", "sendrecv"}
            and not dialog.remote_audio_connection_held
        ),
        connection_held=dialog.remote_audio_connection_held,
        signaling_host=dialog.remote_host,
    )


def invite_video_rtp_peer(invite: SipInvite) -> VideoRtpPeer:
    """Build a directional video peer represented by an inbound offer."""

    if invite.video_format is None:
        raise ValueError("cannot build video peer for an audio-only INVITE")
    return VideoRtpPeer(
        host=invite.remote_video_rtp_host,
        port=int(invite.remote_video_rtp_port),
        rtcp_host=invite.remote_video_rtcp_host or invite.remote_video_rtp_host,
        rtcp_port=int(
            invite.remote_video_rtcp_port or invite.remote_video_rtp_port + 1
        ),
        video_format=invite.video_format,
        local_video_format=invite.recv_video_format,
        signaling_host=invite.source_host,
        connection_held=invite.remote_video_connection_held,
    )


def dialog_video_rtp_peer(dialog: SipDialog) -> VideoRtpPeer:
    """Build a directional video peer from an established outbound dialog."""

    if dialog.video_format is None:
        raise ValueError("cannot build video peer for an audio-only dialog")
    return VideoRtpPeer(
        host=dialog.remote_video_rtp_host,
        port=int(dialog.remote_video_rtp_port),
        rtcp_host=dialog.remote_video_rtcp_host or dialog.remote_video_rtp_host,
        rtcp_port=int(
            dialog.remote_video_rtcp_port or dialog.remote_video_rtp_port + 1
        ),
        video_format=dialog.video_format,
        local_video_format=dialog.recv_video_format,
        signaling_host=dialog.remote_host,
        connection_held=dialog.remote_video_connection_held,
    )


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
        left=invite_rtp_peer(invite),
        right=dialog_rtp_peer(client.dialog),
        left_port=source_relay_port,
        right_port=dest_relay_port,
        debug_capture=debug_capture,
        capture_name=f"{invite.call_id}_{client.dialog_ids.call_id}",
        on_release=on_release,
    )
