"""Async SIP/UDP endpoint for the phase-1 VoIP Stack profile."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
import logging
from typing import Any, Awaitable, Callable

from .audio_format import AudioFormat, HA_SIP_PCM_FORMATS
from .const import VOIP_STACK_RTP_PORT
from . import sdp, sip
from .sip_tcp_io import SipTcpWriter, read_sip_stream_message as _read_sip_stream_message
from .queue_utils import put_drop_oldest


_LOGGER = logging.getLogger(__name__)
_MAX_SIP_UDP_TASKS = 32
_MAX_SIP_INVITE_TASKS = 24
_MAX_COMPLETED_TRANSACTIONS = 256


@dataclass(frozen=True, slots=True)
class SipInvite:
    source_host: str
    source_port: int
    request_uri: sip.SipUri
    caller_uri: sip.SipUri | None
    target: str
    caller: str
    call_id: str
    cseq: str
    remote_sdp: bytes
    send_format: sdp.RtpPcmFormat
    recv_format: sdp.RtpPcmFormat
    remote_rtp_host: str
    remote_rtp_port: int
    video_format: sdp.RtpVideoFormat | None = None
    remote_video_rtp_host: str = ""
    remote_video_rtp_port: int = 0
    remote_video_rtcp_port: int = 0
    remote_video_rtcp_mux: bool = False
    remote_video_payload_types: tuple[int, ...] = ()

    @property
    def selected_format(self) -> sdp.RtpPcmFormat:
        return self.recv_format


@dataclass(frozen=True, slots=True)
class SipInviteResult:
    status: int
    reason: str
    answer_sdp: str = ""
    to_tag: str = ""
    defer_final: bool = False
    decline_reason: str = ""


@dataclass(slots=True)
class _PendingInvite:
    request: sip.SipMessage
    addr: tuple[str, int]
    to_tag: str
    transport: str
    status: int = 100
    reason: str = "Trying"
    answer_sdp: str = ""
    decline_reason: str = ""
    cancelled: bool = False


@dataclass(slots=True)
class _ActiveDialog:
    request: sip.SipMessage
    addr: tuple[str, int]
    to_tag: str
    cseq: int
    transport: str
    status: int = 200
    reason: str = "OK"
    answer_sdp: str = ""


@dataclass(slots=True)
class _CompletedRequest:
    request: sip.SipMessage
    addr: tuple[str, int]
    status: int
    reason: str


InviteHandler = Callable[[SipInvite], Awaitable[SipInviteResult]]
TerminateHandler = Callable[[str, str], Awaitable[None]]
RegisterHandler = Callable[[sip.SipMessage, tuple[str, int], str], Awaitable[Any]]
InfoHandler = Callable[[sip.SipMessage, tuple[str, int], str], Awaitable[None]]
SendHandler = Callable[[bytes, tuple[str, int]], bool | None]
TcpDialogSender = Callable[[bytes], bool | None]


def _uri_from_header(header: str) -> sip.SipUri | None:
    value = (header or "").strip()
    if "<" in value and ">" in value:
        value = value[value.index("<") + 1:value.index(">")]
    try:
        return sip.parse_sip_uri(value)
    except Exception:
        return None


def _uri_text_from_header(header: str) -> str:
    uri = _uri_from_header(header)
    return str(uri) if uri is not None else ""


def _identity_header(value: str) -> str:
    return "".join(ch for ch in str(value or "").strip() if ch not in "\r\n").strip()


def _cseq_number(value: str) -> int:
    try:
        return sip.parse_cseq(value).number
    except Exception:
        return 1


def _same_request_transaction(
    request: sip.SipMessage,
    original: sip.SipMessage,
    addr: tuple[str, int],
    original_addr: tuple[str, int],
) -> bool:
    """Match a request retransmission while allowing a NAT port rebind."""

    if addr[0] != original_addr[0]:
        return False
    try:
        current_cseq = sip.parse_cseq(request.header("CSeq"))
        original_cseq = sip.parse_cseq(original.header("CSeq"))
        request_vias = request.header_values("Via")
        original_vias = original.header_values("Via")
        current_branch = sip.parse_via(request_vias[0] if request_vias else "").branch
        original_branch = sip.parse_via(original_vias[0] if original_vias else "").branch
    except (TypeError, ValueError, sip.SipError):
        return False
    return (
        current_cseq.method == original_cseq.method
        and current_cseq.method == request.method
        and original_cseq.method == original.method
        and current_cseq.number == original_cseq.number
        and bool(current_branch)
        and current_branch == original_branch
    )


def _same_dialog_request(request: sip.SipMessage, dialog: _ActiveDialog, addr: tuple[str, int]) -> bool:
    """Match a new in-dialog request, not an INVITE retransmission."""
    try:
        cseq = sip.parse_cseq(request.header("CSeq"))
        from_tag = sip.extract_tag(request.header("From"))
        to_tag = sip.extract_tag(request.header("To"))
        remote_tag = sip.extract_tag(dialog.request.header("From"))
    except (TypeError, ValueError, sip.SipError):
        return False
    return bool(
        dialog.addr[0] == addr[0]
        and cseq.number >= dialog.cseq
        and from_tag
        and from_tag == remote_tag
        and to_tag
        and to_tag == dialog.to_tag
    )


def _same_video_media(previous: SipInvite, updated: SipInvite) -> bool:
    """Return whether an in-dialog request leaves video media unchanged.

    The current SIP profile does not renegotiate an established video RTP
    attachment. Accept session refreshes, but reject changes that the active
    browser socket or B2BUA relay cannot apply atomically.
    """

    if previous.video_format is None or updated.video_format is None:
        return previous.video_format is updated.video_format
    return bool(
        previous.video_format == updated.video_format
        and previous.remote_video_rtp_host == updated.remote_video_rtp_host
        and previous.remote_video_rtp_port == updated.remote_video_rtp_port
        and previous.remote_video_rtcp_port == updated.remote_video_rtcp_port
        and previous.remote_video_rtcp_mux == updated.remote_video_rtcp_mux
    )


def _response_via_header(request: sip.SipMessage, addr) -> str:
    values = request.header_values("Via")
    value = values[0] if values else ""
    if not value:
        return ""
    try:
        parsed = sip.parse_via(value)
    except Exception:
        return value
    if not any(key == "rport" for key, _ in parsed.params):
        return value

    sent_by = parsed.host
    if parsed.port:
        sent_by = f"{sent_by}:{parsed.port}"
    rendered = f"SIP/2.0/{parsed.transport} {sent_by}"
    for key, val in parsed.params:
        if key in {"rport", "received"}:
            continue
        rendered += f";{key}" if val is None else f";{key}={val}"
    rendered += f";received={addr[0]};rport={int(addr[1])}"
    return rendered


def _response_headers(request: sip.SipMessage, *, addr=None, to_tag: str = "") -> list[tuple[str, str]]:
    headers: list[tuple[str, str]] = []
    via_values = request.header_values("Via")
    if via_values:
        top_via = _response_via_header(request, addr) if addr is not None else via_values[0]
        headers.append(("Via", top_via))
        headers.extend(("Via", value) for value in via_values[1:])
    for name in ("From", "Call-ID", "CSeq"):
        value = request.header(name)
        if value:
            headers.append((name, value))
    to_value = request.header("To")
    if to_value and to_tag and not sip.extract_tag(to_value):
        to_value = f"{to_value};tag={to_tag}"
    if to_value:
        headers.insert(2, ("To", to_value))
    return headers


def _response_contact_uri(request: sip.SipMessage, *, local_ip: str, local_sip_port: int, transport: str) -> str:
    try:
        request_uri = sip.parse_sip_uri(request.uri)
        user = request_uri.user or "voip"
    except Exception:
        user = "voip"
    port = int(local_sip_port or 5060)
    return str(sip.SipUri(user, local_ip, port, params=(("transport", transport.lower()),)))


def _unsupported_method_response(method: str) -> tuple[int, str]:
    if method in sip.KNOWN_UNSUPPORTED_METHODS:
        return 405, "Method Not Allowed"
    return 501, "Not Implemented"


class SipUdpEndpoint(asyncio.DatagramProtocol):
    """Small SIP/UDP user agent.

    The protocol object is intentionally policy-light: it validates SIP/SDP and
    delegates call routing to `on_invite`. It sends only standards-compliant SIP
    responses.
    """

    def __init__(
        self,
        *,
        local_ip: str,
        local_rtp_port: int,
        local_sip_port: int = 5060,
        supported_formats: list[AudioFormat],
        supported_send_formats: list[AudioFormat] | None = None,
        supported_recv_formats: list[AudioFormat] | None = None,
        on_invite: InviteHandler,
        on_terminated: TerminateHandler | None = None,
        on_register: RegisterHandler | None = None,
        on_info: InfoHandler | None = None,
        send_override: SendHandler | None = None,
        signaling_transport: str = "UDP",
        enable_video: bool = False,
        enable_video_transcoding: bool = False,
        prefer_browser_video_send: bool = False,
    ) -> None:
        self.local_ip = local_ip
        self.local_sip_port = int(local_sip_port or 5060)
        self.local_rtp_port = local_rtp_port or VOIP_STACK_RTP_PORT
        base_formats = supported_formats or list(HA_SIP_PCM_FORMATS)
        self.supported_send_formats = supported_send_formats or base_formats
        self.supported_recv_formats = supported_recv_formats or base_formats
        self.on_invite = on_invite
        self.on_terminated = on_terminated
        self.on_register = on_register
        self.on_info = on_info
        self.send_override = send_override
        self.signaling_transport = (signaling_transport or "UDP").upper()
        self.enable_video = bool(enable_video)
        self.enable_video_transcoding = bool(enable_video_transcoding)
        self.prefer_browser_video_send = bool(prefer_browser_video_send)
        self.transport: asyncio.DatagramTransport | None = None
        self._closed_waiter: asyncio.Future[None] | None = None
        self.pending_invites: dict[str, _PendingInvite] = {}
        self.completed_invites: dict[str, _PendingInvite] = {}
        self.active_dialogs: dict[str, _ActiveDialog] = {}
        self.completed_byes: dict[str, _CompletedRequest] = {}
        self.completed_infos: dict[tuple[str, int], _CompletedRequest] = {}
        self._logged_incompatible_invites: set[str] = set()
        self._request_tasks: set[asyncio.Task[None]] = set()
        self._invite_tasks: set[asyncio.Task[None]] = set()
        self.dropped_datagrams = 0
        self.last_sip_event = ""
        self.last_sip_status_code = 0
        self.last_sip_reason = ""

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]
        self._closed_waiter = asyncio.get_running_loop().create_future()
        _LOGGER.info("SIP UDP listener ready on %s", transport.get_extra_info("sockname"))

    def connection_lost(self, exc: Exception | None) -> None:
        if exc:
            _LOGGER.warning("SIP UDP listener closed with error: %s", exc)
        else:
            _LOGGER.info("SIP UDP listener closed")
        self.transport = None
        self.cancel_request_tasks()
        if self._closed_waiter is not None and not self._closed_waiter.done():
            self._closed_waiter.set_result(None)

    async def wait_closed(self) -> None:
        waiter = self._closed_waiter
        if waiter is not None and not waiter.done():
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(waiter, timeout=1.0)
        tasks = tuple(self._request_tasks)
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)

    def datagram_received(self, data: bytes, addr) -> None:
        self.submit_datagram(data, addr)

    def submit_datagram(self, data: bytes, addr) -> bool:
        """Schedule one message while preserving capacity for dialog control."""

        is_invite = data[:7].upper() == b"INVITE "
        is_control = any(data[: len(prefix)].upper() == prefix for prefix in (b"ACK ", b"BYE ", b"CANCEL "))
        if (
            len(self._request_tasks) >= _MAX_SIP_UDP_TASKS
            or (not is_control and len(self._request_tasks) >= _MAX_SIP_INVITE_TASKS)
            or (is_invite and len(self._invite_tasks) >= _MAX_SIP_INVITE_TASKS)
        ):
            self.dropped_datagrams += 1
            if self.dropped_datagrams & (self.dropped_datagrams - 1) == 0:
                _LOGGER.warning(
                    "SIP UDP handler saturated; dropped %d datagrams",
                    self.dropped_datagrams,
                )
            return False
        task = asyncio.create_task(self._handle_datagram_guarded(data, addr))
        self._request_tasks.add(task)
        if is_invite:
            self._invite_tasks.add(task)
        task.add_done_callback(self._request_task_done)
        return True

    def _request_task_done(self, task: asyncio.Task[None]) -> None:
        self._request_tasks.discard(task)
        self._invite_tasks.discard(task)

    @staticmethod
    def _remember_completed(store: dict, key: str, value: Any) -> None:
        if key not in store and len(store) >= _MAX_COMPLETED_TRANSACTIONS:
            store.pop(next(iter(store)))
        store[key] = value

    def cancel_request_tasks(self) -> None:
        for task in tuple(self._request_tasks):
            task.cancel()

    async def _handle_datagram_guarded(self, data: bytes, addr) -> None:
        try:
            await self._handle_datagram(data, addr)
        except asyncio.CancelledError:
            raise
        except Exception:
            _LOGGER.exception("SIP UDP handler failed for %s:%s", addr[0], addr[1])

    def error_received(self, exc: Exception) -> None:
        _LOGGER.warning("SIP UDP socket error: %s", exc)

    def _send(self, data: bytes, addr) -> bool:
        try:
            if self.send_override is not None:
                return self.send_override(data, addr) is not False
            if self.transport is not None:
                self.transport.sendto(data, addr)
                return True
        except (ConnectionError, OSError, RuntimeError) as err:
            _LOGGER.debug("SIP send failed for %s:%s: %s", addr[0], addr[1], err)
        return False

    def _send_response(
        self,
        request: sip.SipMessage,
        addr,
        status: int,
        reason: str,
        *,
        body: bytes = b"",
        to_tag: str = "",
        decline_reason: str = "",
    ) -> bool:
        headers = _response_headers(request, addr=addr, to_tag=to_tag)
        if request.method == "INVITE" and 200 <= int(status) < 300:
            headers.append((
                "Contact",
                f"<{_response_contact_uri(request, local_ip=self.local_ip, local_sip_port=self.local_sip_port, transport=self.signaling_transport)}>",
            ))
        if body:
            headers.append(("Content-Type", "application/sdp"))
        if int(status) == 405:
            headers.append(("Allow", ", ".join(sorted(sip.SUPPORTED_METHODS))))
        clean_reason = _identity_header(decline_reason)
        if clean_reason and int(status) >= 300:
            quoted = clean_reason.replace("\\", "\\\\").replace('"', '\\"')
            headers.append(("Reason", f'X-Voip-Stack;cause={int(status)};text="{quoted}"'))
            headers.append(("X-Voip-Stack-Decline-Reason", clean_reason))
        raw = sip.build_response(status, reason, headers, body)
        if not self._send(raw, addr):
            _LOGGER.warning("SIP TX %s %s dropped for %s:%s", status, reason, addr[0], addr[1])
            return False
        _LOGGER.info("SIP TX %s %s to %s:%s", status, reason, addr[0], addr[1])
        sip.mark_sip_event(self, "SIP_RESPONSE", int(status), reason)
        return True

    async def _handle_datagram(self, data: bytes, addr) -> None:
        try:
            request = sip.parse_message(data)
        except Exception as err:
            _LOGGER.info("SIP RX malformed from %s:%s: %s", addr[0], addr[1], err)
            return
        if not request.is_request:
            _LOGGER.info("SIP RX response ignored from %s:%s", addr[0], addr[1])
            sip.mark_sip_event(self, "SIP_RESPONSE", int(request.status_code or 0), request.reason)
            return

        _LOGGER.info("SIP RX %s %s from %s:%s", request.method, request.uri, addr[0], addr[1])
        sip.mark_sip_event(self, request.method or "SIP_REQUEST")
        if request.method not in sip.SUPPORTED_METHODS:
            status, reason = _unsupported_method_response(request.method or "")
            self._send_response(request, addr, status, reason)
            return
        try:
            request_cseq = sip.parse_cseq(request.header("CSeq"))
        except (TypeError, ValueError, sip.SipError):
            self._send_response(request, addr, 400, "Bad Request")
            return
        if request_cseq.method != request.method:
            self._send_response(request, addr, 400, "Bad Request")
            return
        if request.method == "OPTIONS":
            self._send_response(request, addr, 200, "OK")
            return
        if request.method == "REGISTER":
            if self.on_register is not None:
                result = await self.on_register(request, addr, self.signaling_transport)
                headers = _response_headers(request, addr=addr, to_tag="")
                headers.extend(tuple(getattr(result, "headers", ()) or ()))
                raw = sip.build_response(int(result.status), str(result.reason), headers, b"")
                if self._send(raw, addr):
                    _LOGGER.info("SIP TX %s %s to %s:%s", result.status, result.reason, addr[0], addr[1])
                    sip.mark_sip_event(self, "SIP_RESPONSE", int(result.status), str(result.reason))
                else:
                    _LOGGER.warning("SIP REGISTER response dropped for %s:%s", addr[0], addr[1])
                return
            self._send_response(request, addr, 405, "Method Not Allowed")
            return
        if request.method == "INFO":
            call_id = request.header("Call-ID")
            info_key = (call_id, request_cseq.number)
            completed_info = self.completed_infos.get(info_key)
            if completed_info is not None:
                if _same_request_transaction(request, completed_info.request, addr, completed_info.addr):
                    self._send_response(request, addr, completed_info.status, completed_info.reason)
                else:
                    self._send_response(request, addr, 481, "Call/Transaction Does Not Exist")
                return
            dialog = self.active_dialogs.get(call_id)
            if dialog is None or not _same_dialog_request(request, dialog, addr):
                self._send_response(request, addr, 481, "Call/Transaction Does Not Exist")
                return
            if self.on_info is not None:
                await self.on_info(request, addr, self.signaling_transport)
            self._remember_completed(
                self.completed_infos,
                info_key,
                _CompletedRequest(request, addr, 200, "OK"),
            )
            dialog.cseq = max(dialog.cseq, request_cseq.number + 1)
            self._send_response(request, addr, 200, "OK")
            return
        if request.method == "CANCEL":
            call_id = request.header("Call-ID")
            pending = self.pending_invites.get(call_id)
            completed = self.completed_invites.get(call_id)
            invite_transaction = pending or (completed if completed is not None and completed.cancelled else None)
            # Match the transaction and originating host, but do not pin the
            # source port: NATs and SIP proxies may legitimately rewrite it.
            try:
                incoming_cseq = sip.parse_cseq(request.header("CSeq"))
                pending_cseq = (
                    sip.parse_cseq(invite_transaction.request.header("CSeq"))
                    if invite_transaction is not None
                    else None
                )
                incoming_vias = request.header_values("Via")
                incoming_branch = sip.parse_via(incoming_vias[0] if incoming_vias else "").branch
                pending_vias = invite_transaction.request.header_values("Via") if invite_transaction is not None else []
                pending_branch = (
                    sip.parse_via(pending_vias[0] if pending_vias else "").branch
                    if invite_transaction is not None
                    else ""
                )
                same_transaction = (
                    pending_cseq is not None
                    and incoming_cseq.method == "CANCEL"
                    and pending_cseq.method == "INVITE"
                    and incoming_cseq.number == pending_cseq.number
                    and bool(incoming_branch)
                    and incoming_branch == pending_branch
                )
            except (TypeError, ValueError, sip.SipError):
                same_transaction = False
            if invite_transaction is None or invite_transaction.addr[0] != addr[0] or not same_transaction:
                self._send_response(request, addr, 481, "Call/Transaction Does Not Exist")
                return
            if pending is None:
                self._send_response(request, addr, 200, "OK")
                return
            pending.cancelled = True
            pending.status = 487
            pending.reason = "Request Terminated"
            pending.decline_reason = "cancelled"
            self.pending_invites.pop(call_id, None)
            self._remember_completed(self.completed_invites, call_id, pending)
            self._send_response(request, addr, 200, "OK")
            self._send_response(
                pending.request,
                pending.addr,
                487,
                "Request Terminated",
                to_tag=pending.to_tag,
                decline_reason="cancelled",
            )
            if self.on_terminated is not None:
                await self.on_terminated(call_id, "cancelled")
            return
        if request.method == "BYE":
            call_id = request.header("Call-ID")
            dialog = self.active_dialogs.get(call_id)
            if dialog is None:
                completed = self.completed_byes.get(call_id)
                if completed is not None and _same_request_transaction(
                    request,
                    completed.request,
                    addr,
                    completed.addr,
                ):
                    self._send_response(request, addr, completed.status, completed.reason)
                else:
                    self._send_response(request, addr, 481, "Call/Transaction Does Not Exist")
                return
            try:
                invite_cseq = sip.parse_cseq(dialog.request.header("CSeq")) if dialog is not None else None
                from_tag = sip.extract_tag(request.header("From"))
                to_tag = sip.extract_tag(request.header("To"))
                remote_tag = sip.extract_tag(dialog.request.header("From")) if dialog is not None else ""
                same_dialog = (
                    dialog is not None
                    and invite_cseq is not None
                    and dialog.addr[0] == addr[0]
                    and request_cseq.number > invite_cseq.number
                    and bool(from_tag)
                    and from_tag == remote_tag
                    and bool(to_tag)
                    and to_tag == dialog.to_tag
                )
            except (TypeError, ValueError, sip.SipError):
                same_dialog = False
            if not same_dialog:
                self._send_response(request, addr, 481, "Call/Transaction Does Not Exist")
                return
            self.active_dialogs.pop(call_id, None)
            self._remember_completed(
                self.completed_byes,
                call_id,
                _CompletedRequest(request, addr, 200, "OK"),
            )
            self._send_response(request, addr, 200, "OK")
            if self.on_terminated is not None:
                await self.on_terminated(call_id, "remote_hangup")
            return
        if request.method == "ACK":
            return
        call_id = request.header("Call-ID")
        existing_dialog = self.active_dialogs.get(call_id)
        if existing_dialog is not None:
            retransmit = _same_request_transaction(request, existing_dialog.request, addr, existing_dialog.addr)
            if not retransmit and not _same_dialog_request(request, existing_dialog, addr):
                self._send_response(request, addr, 481, "Call/Transaction Does Not Exist", to_tag=existing_dialog.to_tag)
                return
            if not retransmit and request.body and request.body != existing_dialog.request.body:
                previous_invite = self._parse_invite(
                    existing_dialog.request, existing_dialog.addr
                )
                updated_invite = self._parse_invite(request, addr)
                same_media = bool(
                    previous_invite is not None
                    and updated_invite is not None
                    and previous_invite.send_format.wire_token()
                    == updated_invite.send_format.wire_token()
                    and previous_invite.recv_format.wire_token()
                    == updated_invite.recv_format.wire_token()
                    and _same_video_media(previous_invite, updated_invite)
                )
                if not same_media:
                    self._send_response(
                        request,
                        addr,
                        488,
                        "Not Acceptable Here",
                        to_tag=existing_dialog.to_tag,
                    )
                    return
                _LOGGER.info(
                    "SIP in-dialog re-INVITE accepted call_id=%s remote_rtp=%s:%s",
                    call_id,
                    updated_invite.remote_rtp_host,
                    updated_invite.remote_rtp_port,
                )
            body = existing_dialog.answer_sdp.encode("utf-8") if existing_dialog.answer_sdp else b""
            if self._send_response(request, addr, 200, "OK", body=body, to_tag=existing_dialog.to_tag):
                if not retransmit:
                    existing_dialog.request = request
                    existing_dialog.addr = addr
                    existing_dialog.cseq = request_cseq.number + 1
            return
        invite = self._parse_invite(request, addr)
        if invite is None:
            self._send_response(request, addr, 488, "Not Acceptable Here", to_tag=sip.make_tag())
            return
        existing_pending = self.pending_invites.get(invite.call_id)
        if existing_pending is not None:
            if not _same_request_transaction(request, existing_pending.request, addr, existing_pending.addr):
                self._send_response(request, addr, 488, "Not Acceptable Here", to_tag=existing_pending.to_tag)
                return
            body = existing_pending.answer_sdp.encode("utf-8") if existing_pending.answer_sdp else b""
            sent = self._send_response(
                request,
                addr,
                existing_pending.status,
                existing_pending.reason,
                body=body,
                to_tag=existing_pending.to_tag,
                decline_reason=existing_pending.decline_reason,
            )
            if sent and existing_pending.status >= 200:
                self.pending_invites.pop(invite.call_id, None)
                if existing_pending.status < 300:
                    self.active_dialogs[invite.call_id] = _ActiveDialog(
                        request=request,
                        addr=addr,
                        to_tag=existing_pending.to_tag,
                        cseq=_cseq_number(request.header("CSeq")) + 1,
                        transport=existing_pending.transport,
                        status=existing_pending.status,
                        reason=existing_pending.reason,
                        answer_sdp=existing_pending.answer_sdp,
                    )
                else:
                    self._remember_completed(self.completed_invites, invite.call_id, existing_pending)
            return
        completed_invite = self.completed_invites.get(invite.call_id)
        if completed_invite is not None:
            if not _same_request_transaction(request, completed_invite.request, addr, completed_invite.addr):
                self._send_response(request, addr, 488, "Not Acceptable Here", to_tag=completed_invite.to_tag)
                return
            body = completed_invite.answer_sdp.encode("utf-8") if completed_invite.answer_sdp else b""
            self._send_response(
                request,
                addr,
                completed_invite.status,
                completed_invite.reason,
                body=body,
                to_tag=completed_invite.to_tag,
                decline_reason=completed_invite.decline_reason,
            )
            return
        to_tag = sip.make_tag()
        pending = _PendingInvite(request, addr, to_tag, self.signaling_transport)
        self.pending_invites[invite.call_id] = pending
        self._send_response(request, addr, 100, "Trying")
        try:
            result = await self.on_invite(invite)
        except BaseException:
            if self.pending_invites.get(invite.call_id) is pending:
                self.pending_invites.pop(invite.call_id, None)
            raise
        if self.pending_invites.get(invite.call_id) is not pending:
            # A CANCEL/final decision won while routing awaited. Clean up any
            # resources the completed policy path may just have published.
            if pending.cancelled and self.on_terminated is not None:
                await self.on_terminated(invite.call_id, "cancelled")
            return
        to_tag = result.to_tag or pending.to_tag
        if result.defer_final:
            pending.to_tag = to_tag
            pending.status = int(result.status)
            pending.reason = str(result.reason)
            pending.answer_sdp = result.answer_sdp
            pending.decline_reason = result.decline_reason
            self._send_response(request, addr, result.status, result.reason, to_tag=to_tag)
            return

        pending.to_tag = to_tag
        pending.status = int(result.status)
        pending.reason = str(result.reason)
        pending.answer_sdp = result.answer_sdp
        pending.decline_reason = result.decline_reason
        body = result.answer_sdp.encode("utf-8") if result.answer_sdp else b""
        sent = self._send_response(
            request,
            addr,
            result.status,
            result.reason,
            body=body,
            to_tag=to_tag,
            decline_reason=result.decline_reason,
        )
        if sent:
            self.pending_invites.pop(invite.call_id, None)
        if sent and 200 <= int(result.status) < 300:
            self.active_dialogs[invite.call_id] = _ActiveDialog(
                request=request,
                addr=addr,
                to_tag=to_tag,
                cseq=_cseq_number(request.header("CSeq")) + 1,
                transport=self.signaling_transport,
                status=int(result.status),
                reason=str(result.reason),
                answer_sdp=result.answer_sdp,
            )
        elif sent and int(result.status) >= 300:
            self._remember_completed(self.completed_invites, invite.call_id, pending)

    def send_final_response(
        self,
        call_id: str,
        status: int,
        reason: str,
        *,
        answer_sdp: str = "",
        decline_reason: str = "",
    ) -> bool:
        pending = self.pending_invites.get(call_id)
        if pending is None:
            return False
        pending.status = int(status)
        pending.reason = str(reason)
        pending.answer_sdp = answer_sdp
        pending.decline_reason = decline_reason
        body = answer_sdp.encode("utf-8") if answer_sdp else b""
        if not self._send_response(
            pending.request,
            pending.addr,
            status,
            reason,
            body=body,
            to_tag=pending.to_tag,
            decline_reason=decline_reason,
        ):
            return False
        if int(status) < 200:
            return True
        self.pending_invites.pop(call_id, None)
        if int(status) < 300:
            self.active_dialogs[call_id] = _ActiveDialog(
                request=pending.request,
                addr=pending.addr,
                to_tag=pending.to_tag,
                cseq=_cseq_number(pending.request.header("CSeq")) + 1,
                transport=pending.transport,
                status=int(status),
                reason=str(reason),
                answer_sdp=answer_sdp,
            )
        else:
            self._remember_completed(self.completed_invites, call_id, pending)
        return True

    def send_bye(self, call_id: str = "") -> bool:
        if not call_id and len(self.active_dialogs) == 1:
            call_id = next(iter(self.active_dialogs))
        dialog = self.active_dialogs.get(call_id) if call_id else None
        if dialog is None:
            return False
        remote_uri = _uri_text_from_header(dialog.request.header("From"))
        remote_target_uri = _uri_text_from_header(dialog.request.header("Contact")) or remote_uri
        local_uri = _uri_text_from_header(dialog.request.header("To"))
        if not remote_target_uri or not remote_uri or not local_uri:
            return False
        remote_tag = sip.extract_tag(dialog.request.header("From"))
        ids = sip.SipDialogIds(
            call_id=call_id,
            local_tag=dialog.to_tag,
            remote_tag=remote_tag,
            cseq=dialog.cseq,
            branch=sip.make_branch(),
        )
        headers = sip.dialog_headers(
            request_uri=remote_target_uri,
            local_uri=local_uri,
            remote_uri=remote_uri,
            dialog=ids,
            method="BYE",
            contact_uri=local_uri,
            transport=dialog.transport,
        )
        raw = sip.build_request("BYE", remote_target_uri, headers, b"")
        target_addr = dialog.addr
        if dialog.transport == "UDP":
            try:
                target = sip.parse_sip_uri(remote_target_uri)
                target_addr = (target.host, int(target.port or 5060))
            except (TypeError, ValueError, sip.SipError):
                pass
        if not self._send(raw, target_addr):
            _LOGGER.warning("SIP TX BYE dropped call_id=%s", call_id)
            return False
        self.active_dialogs.pop(call_id, None)
        _LOGGER.info("SIP TX BYE call_id=%s to %s:%s", call_id, target_addr[0], target_addr[1])
        sip.mark_sip_event(self, "BYE")
        return True

    def snapshot(self) -> dict[str, Any]:
        return {
            "transport": self.signaling_transport.lower(),
            "pending_transactions": len(self.pending_invites),
            "active_dialogs": len(self.active_dialogs),
            "pending_call_ids": sorted(self.pending_invites),
            "active_call_ids": sorted(self.active_dialogs),
            "last_sip_event": self.last_sip_event,
            "last_sip_status_code": self.last_sip_status_code,
            "last_sip_reason": self.last_sip_reason,
        }

    def _parse_invite(self, request: sip.SipMessage, addr) -> SipInvite | None:
        try:
            request_uri = sip.parse_sip_uri(request.uri)
            from_uri = _uri_from_header(request.header("From"))
            selected = sdp.negotiate_directional(
                request.body,
                self.supported_send_formats,
                self.supported_recv_formats,
            )
            if selected is None:
                call_id = request.header("Call-ID")
                if call_id not in self._logged_incompatible_invites:
                    if len(self._logged_incompatible_invites) >= 256:
                        self._logged_incompatible_invites.pop()
                    self._logged_incompatible_invites.add(call_id)
                    try:
                        offered = ", ".join(sdp.offered_media_descriptions(request.body))
                    except Exception as err:
                        offered = f"unparseable SDP media: {err}"
                    _LOGGER.info(
                        "SIP INVITE rejected: no compatible PCM media in SDP call_id=%s offered=[%s] local_send=[%s] local_recv=[%s]",
                        call_id,
                        offered,
                        ", ".join(fmt.wire_token() for fmt in self.supported_send_formats),
                        ", ".join(fmt.wire_token() for fmt in self.supported_recv_formats),
                    )
                return None
            _LOGGER.info(
                "SIP INVITE media selected call_id=%s tx=%s rx=%s offered=[%s]",
                request.header("Call-ID"),
                selected.send.wire_token(),
                selected.recv.wire_token(),
                ", ".join(sdp.offered_media_descriptions(request.body)),
            )
            remote = sdp.parse_sdp(request.body)
            video_format = (
                sdp.negotiate_video(
                    request.body,
                    accepted_encodings=(
                        "H264",
                        "VP8",
                        "JPEG",
                        "H263",
                        "H263P",
                        "H265",
                    )
                    if self.enable_video_transcoding
                    else ("H264", "VP8", "JPEG"),
                    prefer_browser_send=self.prefer_browser_video_send,
                )
                if self.enable_video
                else None
            )
            remote_video = sdp.parse_video_sdp(request.body) if video_format is not None else None
            _LOGGER.info(
                "SIP INVITE video negotiation call_id=%s enabled=%s selected=%s remote=%s",
                request.header("Call-ID"),
                self.enable_video,
                video_format.wire_token() if video_format is not None else "none",
                (
                    f"{remote_video['connection_ip']}:{remote_video['media_port']}"
                    if remote_video is not None
                    else "none"
                ),
            )
            caller = _identity_header(request.header("X-Voip-Stack-Caller-Name"))
            target = _identity_header(request.header("X-Voip-Stack-Dest-Name"))
            if not caller:
                caller = from_uri.user if from_uri is not None else ""
            if not target:
                target = request_uri.user
            return SipInvite(
                source_host=addr[0],
                source_port=int(addr[1]),
                request_uri=request_uri,
                caller_uri=from_uri,
                target=target,
                caller=caller,
                call_id=request.header("Call-ID"),
                cseq=request.header("CSeq"),
                remote_sdp=request.body,
                send_format=selected.send,
                recv_format=selected.recv,
                remote_rtp_host=remote["connection_ip"],
                remote_rtp_port=remote["media_port"],
                video_format=video_format,
                remote_video_rtp_host=(str(remote_video["connection_ip"]) if remote_video else ""),
                remote_video_rtp_port=(int(remote_video["media_port"]) if remote_video else 0),
                remote_video_rtcp_port=(
                    int(remote_video["rtcp_port"] or int(remote_video["media_port"]) + 1)
                    if remote_video
                    else 0
                ),
                remote_video_rtcp_mux=(bool(remote_video["rtcp_mux"]) if remote_video else False),
                remote_video_payload_types=(
                    tuple(int(item) for item in remote_video["payload_order"])
                    if remote_video
                    else ()
                ),
            )
        except Exception as err:
            _LOGGER.info("SIP INVITE parse failed: %s", err)
            return None


class SipUdpServer:
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
        on_register: RegisterHandler | None = None,
        on_info: InfoHandler | None = None,
        enable_video: bool = False,
        enable_video_transcoding: bool = False,
        prefer_browser_video_send: bool = False,
    ) -> None:
        self.host = host
        self.port = port
        self.local_ip = local_ip
        self.local_rtp_port = local_rtp_port
        self.supported_formats = supported_formats
        self.supported_send_formats = supported_send_formats
        self.supported_recv_formats = supported_recv_formats
        self.on_invite = on_invite
        self.on_terminated = on_terminated
        self.on_register = on_register
        self.on_info = on_info
        self.enable_video = bool(enable_video)
        self.enable_video_transcoding = bool(enable_video_transcoding)
        self.prefer_browser_video_send = bool(prefer_browser_video_send)
        self.transport: asyncio.DatagramTransport | None = None
        self.endpoint: SipUdpEndpoint | None = None

    async def start(self) -> bool:
        if self.transport is not None:
            return True
        loop = asyncio.get_running_loop()
        try:
            def _factory() -> SipUdpEndpoint:
                self.endpoint = SipUdpEndpoint(
                    local_ip=self.local_ip,
                    local_sip_port=self.port,
                    local_rtp_port=self.local_rtp_port,
                    supported_formats=self.supported_formats,
                    supported_send_formats=self.supported_send_formats,
                    supported_recv_formats=self.supported_recv_formats,
                    on_invite=self.on_invite,
                    on_terminated=self.on_terminated,
                    on_register=self.on_register,
                    on_info=self.on_info,
                    signaling_transport="UDP",
                    enable_video=self.enable_video,
                    enable_video_transcoding=self.enable_video_transcoding,
                    prefer_browser_video_send=self.prefer_browser_video_send,
                )
                return self.endpoint

            transport, _ = await loop.create_datagram_endpoint(
                _factory,
                local_addr=(self.host, self.port),
            )
        except OSError as err:
            _LOGGER.error("Failed to bind SIP UDP %s:%s: %s", self.host, self.port, err)
            return False
        self.transport = transport  # type: ignore[assignment]
        return True

    def send_final_response(
        self,
        call_id: str,
        status: int,
        reason: str,
        *,
        answer_sdp: str = "",
        decline_reason: str = "",
    ) -> bool:
        return self.endpoint is not None and self.endpoint.send_final_response(
            call_id,
            status,
            reason,
            answer_sdp=answer_sdp,
            decline_reason=decline_reason,
        )

    def send_bye(self, call_id: str = "") -> bool:
        return self.endpoint is not None and self.endpoint.send_bye(call_id)

    async def stop(self) -> None:
        if self.transport is not None:
            endpoint = self.endpoint
            self.transport.close()
            self.transport = None
            if endpoint is not None:
                await endpoint.wait_closed()
        self.endpoint = None


class SipTcpServer:
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
        on_register: RegisterHandler | None = None,
        on_info: InfoHandler | None = None,
        enable_video: bool = False,
        enable_video_transcoding: bool = False,
        prefer_browser_video_send: bool = False,
    ) -> None:
        self.host = host
        self.port = port
        self.local_ip = local_ip
        self.local_rtp_port = local_rtp_port
        self.supported_formats = supported_formats
        self.supported_send_formats = supported_send_formats
        self.supported_recv_formats = supported_recv_formats
        self.on_invite = on_invite
        self.on_terminated = on_terminated
        self.on_register = on_register
        self.on_info = on_info
        self.enable_video = bool(enable_video)
        self.enable_video_transcoding = bool(enable_video_transcoding)
        self.prefer_browser_video_send = bool(prefer_browser_video_send)
        self.server: asyncio.AbstractServer | None = None
        self.endpoints: set[SipUdpEndpoint] = set()
        self._writers: dict[tuple[str, int], asyncio.StreamWriter] = {}
        self._tcp_writers: dict[tuple[str, int], SipTcpWriter] = {}
        self._dialog_queues: dict[tuple[tuple[str, int], str], asyncio.Queue[bytes]] = {}
        self._client_tasks: set[asyncio.Task] = set()

    async def start(self) -> bool:
        if self.server is not None:
            return True
        try:
            self.server = await asyncio.start_server(self._handle_client, self.host, self.port)
        except OSError as err:
            _LOGGER.error("Failed to bind SIP TCP %s:%s: %s", self.host, self.port, err)
            return False
        _LOGGER.info("SIP TCP listener ready on %s:%s", self.host, self.port)
        return True

    async def _handle_client(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        client_task = asyncio.current_task()
        if client_task is not None:
            self._client_tasks.add(client_task)
        peer = writer.get_extra_info("peername") or ("0.0.0.0", 0)
        addr = (str(peer[0]), int(peer[1]))
        self._writers[addr] = writer
        tx = SipTcpWriter(writer, label=f"listener {addr[0]}:{addr[1]}")
        self._tcp_writers[addr] = tx

        def _send(data: bytes, _addr) -> bool:
            return tx.send_nowait(data)

        endpoint = SipUdpEndpoint(
            local_ip=self.local_ip,
            local_sip_port=self.port,
            local_rtp_port=self.local_rtp_port,
            supported_formats=self.supported_formats,
            supported_send_formats=self.supported_send_formats,
            supported_recv_formats=self.supported_recv_formats,
            on_invite=self.on_invite,
            on_terminated=self.on_terminated,
            on_register=self.on_register,
            on_info=self.on_info,
            send_override=_send,
            signaling_transport="TCP",
            enable_video=self.enable_video,
            enable_video_transcoding=self.enable_video_transcoding,
            prefer_browser_video_send=self.prefer_browser_video_send,
        )
        self.endpoints.add(endpoint)
        try:
            while not reader.at_eof():
                raw = await _read_sip_stream_message(reader)
                if raw is None:
                    break
                try:
                    msg = sip.parse_message(raw)
                except Exception:
                    msg = None
                if msg is not None:
                    queue = self._dialog_queues.get((addr, msg.header("Call-ID")))
                    if queue is not None:
                        if put_drop_oldest(queue, raw):
                            _LOGGER.debug("SIP TCP dialog queue full for %s; dropped oldest message", addr)
                        continue
                endpoint.submit_datagram(raw, addr)
        finally:
            disconnected_calls = set(endpoint.pending_invites) | set(endpoint.active_dialogs)
            endpoint.cancel_request_tasks()
            await endpoint.wait_closed()
            endpoint.pending_invites.clear()
            endpoint.completed_invites.clear()
            endpoint.active_dialogs.clear()
            endpoint.completed_byes.clear()
            if endpoint.on_terminated is not None:
                for call_id in disconnected_calls:
                    with contextlib.suppress(Exception):
                        await endpoint.on_terminated(call_id, "transport_closed")
            self.endpoints.discard(endpoint)
            self._writers.pop(addr, None)
            self._tcp_writers.pop(addr, None)
            for key in [key for key in self._dialog_queues if key[0] == addr]:
                self._dialog_queues.pop(key, None)
            await tx.close()
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
            if client_task is not None:
                self._client_tasks.discard(client_task)

    def open_reused_dialog(
        self,
        addr: tuple[str, int],
        call_id: str,
    ) -> tuple[TcpDialogSender, asyncio.Queue[bytes]] | None:
        writer = self._writers.get(addr)
        tx = self._tcp_writers.get(addr)
        if writer is None or writer.is_closing() or tx is None:
            return None
        queue: asyncio.Queue[bytes] = asyncio.Queue(maxsize=64)
        key = (addr, call_id)
        self._dialog_queues[key] = queue

        def _send(data: bytes) -> bool:
            return tx.send_nowait(data)

        return _send, queue

    def close_reused_dialog(self, addr: tuple[str, int], call_id: str) -> None:
        self._dialog_queues.pop((addr, call_id), None)

    def send_final_response(
        self,
        call_id: str,
        status: int,
        reason: str,
        *,
        answer_sdp: str = "",
        decline_reason: str = "",
    ) -> bool:
        for endpoint in tuple(self.endpoints):
            if endpoint.send_final_response(
                call_id,
                status,
                reason,
                answer_sdp=answer_sdp,
                decline_reason=decline_reason,
            ):
                return True
        return False

    def send_bye(self, call_id: str = "") -> bool:
        for endpoint in tuple(self.endpoints):
            if endpoint.send_bye(call_id):
                return True
        return False

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            await self.server.wait_closed()
            self.server = None
        for tx in tuple(self._tcp_writers.values()):
            await tx.close()
        for writer in tuple(self._writers.values()):
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()
        current = asyncio.current_task()
        client_tasks = tuple(task for task in self._client_tasks if task is not current)
        if client_tasks:
            await asyncio.gather(*client_tasks, return_exceptions=True)
        self.endpoints.clear()
        self._writers.clear()
        self._tcp_writers.clear()
        self._dialog_queues.clear()
        self._client_tasks.clear()
