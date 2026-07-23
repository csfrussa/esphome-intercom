"""Pure ownership helpers for media reservations and reserved sockets."""

from __future__ import annotations

from typing import Any


def release_video_media_reservation(item: Any) -> None:
    """Release only video resources stored in runtime dict metadata."""

    if not isinstance(item, dict):
        return
    for key in ("video_rtp_socket", "video_rtcp_socket"):
        video_socket = item.pop(key, None)
        if video_socket is not None and hasattr(video_socket, "close"):
            video_socket.close()
    reservation = item.pop("video_rtp_reservation", None)
    if reservation is not None and hasattr(reservation, "release"):
        reservation.release()


def release_media_reservation(item: Any) -> None:
    """Release all owned RTP resources stored in runtime dict metadata."""

    if not isinstance(item, dict):
        return
    release_video_media_reservation(item)
    reservation = item.pop("rtp_reservation", None)
    if reservation is not None and hasattr(reservation, "release"):
        reservation.release()
