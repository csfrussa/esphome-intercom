"""Unified HA SIP endpoint for UDP and TCP signaling."""

from __future__ import annotations

from dataclasses import dataclass
import logging
from .audio_format import AudioFormat
from .sip_listener import InviteHandler, SipTcpServer, SipUdpServer, TerminateHandler


_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class SipEndpointSnapshot:
    udp_ready: bool
    tcp_ready: bool
    pending_transactions: int
    active_dialogs: int
    pending_call_ids: tuple[str, ...]
    active_call_ids: tuple[str, ...]
    last_sip_event: str
    last_sip_status_code: int
    last_sip_reason: str

    @property
    def pending_invites(self) -> int:
        return self.pending_transactions


class SipEndpointManager:
    """Own the HA SIP signaling endpoint across UDP and TCP.

    The manager is the integration-facing object. The UDP and TCP server
    classes remain transport-specific adapters, but call control code should
    not pick one blindly.
    """

    def __init__(
        self,
        *,
        host: str,
        port: int,
        local_ip: str,
        local_rtp_port: int,
        supported_formats: list[AudioFormat],
        supported_send_formats: list[AudioFormat] | None = None,
        supported_recv_formats: list[AudioFormat] | None = None,
        on_invite: InviteHandler,
        on_terminated: TerminateHandler | None = None,
        udp_enabled: bool = True,
        tcp_enabled: bool = True,
    ) -> None:
        self.host = host
        self.port = int(port)
        self.local_ip = local_ip
        self.local_rtp_port = int(local_rtp_port)
        self.supported_formats = supported_formats
        self.supported_send_formats = supported_send_formats
        self.supported_recv_formats = supported_recv_formats
        self.on_invite = on_invite
        self.on_terminated = on_terminated
        self.udp_enabled = bool(udp_enabled)
        self.tcp_enabled = bool(tcp_enabled)
        self.udp_server: SipUdpServer | None = None
        self.tcp_server: SipTcpServer | None = None

    async def start(self) -> bool:
        if not self.udp_enabled and not self.tcp_enabled:
            _LOGGER.error("Cannot start SIP endpoint manager: no signaling transport is enabled")
            return False
        if (
            (not self.udp_enabled or self.udp_server is not None)
            and (not self.tcp_enabled or self.tcp_server is not None)
        ):
            return True

        udp: SipUdpServer | None = None
        tcp: SipTcpServer | None = None
        if self.udp_enabled:
            udp = SipUdpServer(
                host=self.host,
                port=self.port,
                local_ip=self.local_ip,
                local_rtp_port=self.local_rtp_port,
                supported_formats=self.supported_formats,
                supported_send_formats=self.supported_send_formats,
                supported_recv_formats=self.supported_recv_formats,
                on_invite=self.on_invite,
                on_terminated=self.on_terminated,
            )
            if not await udp.start():
                return False

        if self.tcp_enabled:
            tcp = SipTcpServer(
                host=self.host,
                port=self.port,
                local_ip=self.local_ip,
                local_rtp_port=self.local_rtp_port,
                supported_formats=self.supported_formats,
                supported_send_formats=self.supported_send_formats,
                supported_recv_formats=self.supported_recv_formats,
                on_invite=self.on_invite,
                on_terminated=self.on_terminated,
            )
            if not await tcp.start():
                if udp is not None:
                    await udp.stop()
                return False

        self.udp_server = udp
        self.tcp_server = tcp
        transports = "+".join(
            name
            for name, enabled in (("UDP", self.udp_enabled), ("TCP", self.tcp_enabled))
            if enabled
        )
        _LOGGER.info("SIP endpoint manager ready on %s/%s", transports, self.port)
        return True

    async def stop(self) -> None:
        if self.udp_server is not None:
            await self.udp_server.stop()
            self.udp_server = None
        if self.tcp_server is not None:
            await self.tcp_server.stop()
            self.tcp_server = None

    @property
    def servers(self) -> list[object]:
        return [server for server in (self.udp_server, self.tcp_server) if server is not None]

    def _pending_call_ids(self, server: object) -> set[str]:
        endpoint = getattr(server, "endpoint", None)
        pending = getattr(endpoint, "pending_invites", None)
        if isinstance(pending, dict):
            return set(pending)
        ids: set[str] = set()
        endpoints = getattr(server, "endpoints", None)
        if isinstance(endpoints, set):
            for endpoint in endpoints:
                pending = getattr(endpoint, "pending_invites", None)
                if isinstance(pending, dict):
                    ids.update(pending)
        return ids

    def server_for_pending_call(self, call_id: str) -> object | None:
        if not call_id:
            return None
        for server in self.servers:
            if call_id in self._pending_call_ids(server):
                return server
        return None

    def pending_call_ids(self) -> set[str]:
        ids: set[str] = set()
        for server in self.servers:
            ids.update(self._pending_call_ids(server))
        return ids

    def send_final_response(
        self,
        call_id: str,
        status: int,
        reason: str,
        *,
        answer_sdp: str = "",
        decline_reason: str = "",
    ) -> bool:
        preferred = self.server_for_pending_call(call_id)
        candidates = [preferred] if preferred is not None else self.servers
        for server in candidates:
            send = getattr(server, "send_final_response", None)
            if callable(send) and send(
                call_id,
                status,
                reason,
                answer_sdp=answer_sdp,
                decline_reason=decline_reason,
            ):
                return True
        return False

    def send_bye(self, call_id: str = "") -> bool:
        for server in self.servers:
            send = getattr(server, "send_bye", None)
            if callable(send) and send(call_id):
                return True
        return False

    def _active_dialog_count_for(self, server: object) -> int:
        endpoint = getattr(server, "endpoint", None)
        active = getattr(endpoint, "active_dialogs", None)
        count = len(active) if isinstance(active, dict) else 0
        endpoints = getattr(server, "endpoints", None)
        if isinstance(endpoints, set):
            for endpoint in endpoints:
                active = getattr(endpoint, "active_dialogs", None)
                if isinstance(active, dict):
                    count += len(active)
        return count

    def _active_call_ids(self, server: object) -> set[str]:
        endpoint = getattr(server, "endpoint", None)
        active = getattr(endpoint, "active_dialogs", None)
        ids = set(active) if isinstance(active, dict) else set()
        endpoints = getattr(server, "endpoints", None)
        if isinstance(endpoints, set):
            for endpoint in endpoints:
                active = getattr(endpoint, "active_dialogs", None)
                if isinstance(active, dict):
                    ids.update(active)
        return ids

    def _endpoint_snapshots(self) -> list[dict]:
        snapshots: list[dict] = []
        for server in self.servers:
            endpoint = getattr(server, "endpoint", None)
            snap = getattr(endpoint, "snapshot", None)
            if callable(snap):
                snapshots.append(dict(snap()))
            endpoints = getattr(server, "endpoints", None)
            if isinstance(endpoints, set):
                for endpoint in endpoints:
                    snap = getattr(endpoint, "snapshot", None)
                    if callable(snap):
                        snapshots.append(dict(snap()))
        return snapshots

    def pending_invite_count(self) -> int:
        return sum(len(self._pending_call_ids(server)) for server in self.servers)

    def active_dialog_count(self) -> int:
        return sum(self._active_dialog_count_for(server) for server in self.servers)

    def snapshot(self) -> SipEndpointSnapshot:
        pending_ids: set[str] = set()
        active_ids: set[str] = set()
        for server in self.servers:
            pending_ids.update(self._pending_call_ids(server))
            active_ids.update(self._active_call_ids(server))
        last_event = ""
        last_status = 0
        last_reason = ""
        for snap in self._endpoint_snapshots():
            if snap.get("last_sip_event"):
                last_event = str(snap.get("last_sip_event") or "")
                last_status = int(snap.get("last_sip_status_code") or 0)
                last_reason = str(snap.get("last_sip_reason") or "")
        return SipEndpointSnapshot(
            udp_ready=self.udp_server is not None,
            tcp_ready=self.tcp_server is not None,
            pending_transactions=len(pending_ids),
            active_dialogs=self.active_dialog_count(),
            pending_call_ids=tuple(sorted(pending_ids)),
            active_call_ids=tuple(sorted(active_ids)),
            last_sip_event=last_event,
            last_sip_status_code=last_status,
            last_sip_reason=last_reason,
        )
