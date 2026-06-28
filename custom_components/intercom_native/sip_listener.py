"""Async SIP/UDP endpoint for the phase-1 VoIP intercom profile."""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
import logging
import re
from typing import Any, Awaitable, Callable

from .audio_format import AudioFormat, HA_SIP_PCM_FORMATS
from .const import INTERCOM_RTP_PORT
from . import sdp, sip


_LOGGER = logging.getLogger(__name__)
_TAG_RE = re.compile(r"(?:^|;)tag=([^;>\s]+)")


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


@dataclass(slots=True)
class _ActiveDialog:
    request: sip.SipMessage
    addr: tuple[str, int]
    to_tag: str
    cseq: int
    transport: str


InviteHandler = Callable[[SipInvite], Awaitable[SipInviteResult]]
TerminateHandler = Callable[[str, str], Awaitable[None]]
RegisterHandler = Callable[[sip.SipMessage, tuple[str, int], str], Awaitable[Any]]
SendHandler = Callable[[bytes, tuple[str, int]], None]
TcpDialogSender = Callable[[bytes], None]


def _extract_tag(header: str) -> str:
    match = _TAG_RE.search(header or "")
    return match.group(1) if match else ""


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
    via_value = _response_via_header(request, addr) if addr is not None else request.header("Via")
    if via_value:
        headers.append(("Via", via_value))
    for name in ("From", "Call-ID", "CSeq"):
        value = request.header(name)
        if value:
            headers.append((name, value))
    to_value = request.header("To")
    if to_value and to_tag and "tag=" not in to_value:
        to_value = f"{to_value};tag={to_tag}"
    if to_value:
        headers.insert(2, ("To", to_value))
    return headers


def _response_contact_uri(request: sip.SipMessage, *, local_ip: str, local_sip_port: int, transport: str) -> str:
    try:
        request_uri = sip.parse_sip_uri(request.uri)
        user = request_uri.user or "intercom"
    except Exception:
        user = "intercom"
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
        send_override: SendHandler | None = None,
        signaling_transport: str = "UDP",
    ) -> None:
        self.local_ip = local_ip
        self.local_sip_port = int(local_sip_port or 5060)
        self.local_rtp_port = local_rtp_port or INTERCOM_RTP_PORT
        base_formats = supported_formats or list(HA_SIP_PCM_FORMATS)
        self.supported_send_formats = supported_send_formats or base_formats
        self.supported_recv_formats = supported_recv_formats or base_formats
        self.on_invite = on_invite
        self.on_terminated = on_terminated
        self.on_register = on_register
        self.send_override = send_override
        self.signaling_transport = (signaling_transport or "UDP").upper()
        self.transport: asyncio.DatagramTransport | None = None
        self.pending_invites: dict[str, _PendingInvite] = {}
        self.active_dialogs: dict[str, _ActiveDialog] = {}
        self._logged_incompatible_invites: set[str] = set()
        self.last_sip_event = ""
        self.last_sip_status_code = 0
        self.last_sip_reason = ""

    def _mark_sip_event(self, event: str, status: int = 0, reason: str = "") -> None:
        self.last_sip_event = event
        if status:
            self.last_sip_status_code = int(status)
            self.last_sip_reason = reason or ""

    def connection_made(self, transport: asyncio.BaseTransport) -> None:
        self.transport = transport  # type: ignore[assignment]
        _LOGGER.info("SIP UDP listener ready on %s", transport.get_extra_info("sockname"))

    def connection_lost(self, exc: Exception | None) -> None:
        if exc:
            _LOGGER.warning("SIP UDP listener closed with error: %s", exc)
        else:
            _LOGGER.info("SIP UDP listener closed")
        self.transport = None

    def datagram_received(self, data: bytes, addr) -> None:
        asyncio.create_task(self._handle_datagram(data, addr))

    def error_received(self, exc: Exception) -> None:
        _LOGGER.warning("SIP UDP socket error: %s", exc)

    def _send(self, data: bytes, addr) -> None:
        if self.send_override is not None:
            self.send_override(data, addr)
            return
        if self.transport is not None:
            self.transport.sendto(data, addr)

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
    ) -> None:
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
            headers.append(("Reason", f'X-Intercom;cause={int(status)};text="{quoted}"'))
            headers.append(("X-Intercom-Decline-Reason", clean_reason))
        raw = sip.build_response(status, reason, headers, body)
        _LOGGER.info("SIP TX %s %s to %s:%s", status, reason, addr[0], addr[1])
        self._mark_sip_event("SIP_RESPONSE", int(status), reason)
        self._send(raw, addr)

    async def _handle_datagram(self, data: bytes, addr) -> None:
        try:
            request = sip.parse_message(data)
        except Exception as err:
            _LOGGER.info("SIP RX malformed from %s:%s: %s", addr[0], addr[1], err)
            return
        if not request.is_request:
            _LOGGER.info("SIP RX response ignored from %s:%s", addr[0], addr[1])
            self._mark_sip_event("SIP_RESPONSE", int(request.status_code or 0), request.reason)
            return

        _LOGGER.info("SIP RX %s %s from %s:%s", request.method, request.uri, addr[0], addr[1])
        self._mark_sip_event(request.method or "SIP_REQUEST")
        if request.method not in sip.SUPPORTED_METHODS:
            status, reason = _unsupported_method_response(request.method or "")
            self._send_response(request, addr, status, reason)
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
                _LOGGER.info("SIP TX %s %s to %s:%s", result.status, result.reason, addr[0], addr[1])
                self._mark_sip_event("SIP_RESPONSE", int(result.status), str(result.reason))
                self._send(raw, addr)
                return
            self._send_response(request, addr, 405, "Method Not Allowed")
            return
        if request.method == "INFO":
            self._send_response(request, addr, 200, "OK")
            return
        if request.method in {"CANCEL", "BYE"}:
            call_id = request.header("Call-ID")
            terminal_reason = "cancelled" if request.method == "CANCEL" else "remote_hangup"
            pending = self.pending_invites.pop(call_id, None)
            self.active_dialogs.pop(call_id, None)
            self._send_response(request, addr, 200, "OK")
            if pending is not None:
                self._send_response(
                    pending.request,
                    pending.addr,
                    487,
                    "Request Terminated",
                    to_tag=pending.to_tag,
                    decline_reason=terminal_reason,
                )
            if self.on_terminated is not None:
                await self.on_terminated(call_id, terminal_reason)
            return
        if request.method == "ACK":
            return
        self._send_response(request, addr, 100, "Trying")
        invite = self._parse_invite(request, addr)
        if invite is None:
            self._send_response(request, addr, 488, "Not Acceptable Here", to_tag=sip.make_tag())
            return

        result = await self.on_invite(invite)
        to_tag = result.to_tag or sip.make_tag()
        if result.defer_final:
            self.pending_invites[invite.call_id] = _PendingInvite(
                request,
                addr,
                to_tag,
                self.signaling_transport,
            )
            self._send_response(request, addr, result.status, result.reason, to_tag=to_tag)
            return

        body = result.answer_sdp.encode("utf-8") if result.answer_sdp else b""
        self._send_response(
            request,
            addr,
            result.status,
            result.reason,
            body=body,
            to_tag=to_tag,
            decline_reason=result.decline_reason,
        )
        if 200 <= int(result.status) < 300:
            self.active_dialogs[invite.call_id] = _ActiveDialog(
                request=request,
                addr=addr,
                to_tag=to_tag,
                cseq=_cseq_number(request.header("CSeq")) + 1,
                transport=self.signaling_transport,
            )

    def send_final_response(
        self,
        call_id: str,
        status: int,
        reason: str,
        *,
        answer_sdp: str = "",
        decline_reason: str = "",
    ) -> bool:
        pending = self.pending_invites.pop(call_id, None)
        if pending is None:
            return False
        body = answer_sdp.encode("utf-8") if answer_sdp else b""
        self._send_response(
            pending.request,
            pending.addr,
            status,
            reason,
            body=body,
            to_tag=pending.to_tag,
            decline_reason=decline_reason,
        )
        if 200 <= int(status) < 300:
            self.active_dialogs[call_id] = _ActiveDialog(
                request=pending.request,
                addr=pending.addr,
                to_tag=pending.to_tag,
                cseq=_cseq_number(pending.request.header("CSeq")) + 1,
                transport=pending.transport,
            )
        return True

    def send_bye(self, call_id: str = "") -> bool:
        if not call_id and len(self.active_dialogs) == 1:
            call_id = next(iter(self.active_dialogs))
        dialog = self.active_dialogs.pop(call_id, None) if call_id else None
        if dialog is None:
            return False
        remote_uri = _uri_text_from_header(dialog.request.header("Contact")) or _uri_text_from_header(dialog.request.header("From"))
        local_uri = _uri_text_from_header(dialog.request.header("To"))
        if not remote_uri or not local_uri:
            return False
        remote_tag = _extract_tag(dialog.request.header("From"))
        ids = sip.SipDialogIds(
            call_id=call_id,
            local_tag=dialog.to_tag,
            remote_tag=remote_tag,
            cseq=dialog.cseq,
            branch=sip.make_branch(),
        )
        headers = sip.dialog_headers(
            request_uri=remote_uri,
            local_uri=local_uri,
            remote_uri=remote_uri,
            dialog=ids,
            method="BYE",
            contact_uri=local_uri,
            transport=dialog.transport,
        )
        raw = sip.build_request("BYE", remote_uri, headers, b"")
        _LOGGER.info("SIP TX BYE call_id=%s to %s:%s", call_id, dialog.addr[0], dialog.addr[1])
        self._mark_sip_event("BYE")
        self._send(raw, dialog.addr)
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
            caller = _identity_header(request.header("X-Intercom-Caller-Name"))
            target = _identity_header(request.header("X-Intercom-Dest-Name"))
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
                    signaling_transport="UDP",
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
            self.transport.close()
            self.transport = None
        self.endpoint = None


async def _read_sip_stream_message(reader: asyncio.StreamReader) -> bytes | None:
    try:
        head = await reader.readuntil(b"\r\n\r\n")
    except (asyncio.IncompleteReadError, asyncio.LimitOverrunError):
        return None
    content_length = 0
    try:
        text = head.decode("utf-8", errors="strict")
    except UnicodeDecodeError:
        return None
    for line in text.split("\r\n")[1:]:
        if ":" not in line:
            continue
        key, value = line.split(":", 1)
        if key.strip().lower() == "content-length":
            try:
                content_length = int(value.strip())
            except ValueError:
                return None
    if content_length < 0 or content_length > sip.MAX_SIP_BODY_BYTES:
        return None
    body = await reader.readexactly(content_length) if content_length else b""
    return head + body


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
        self.server: asyncio.AbstractServer | None = None
        self.endpoints: set[SipUdpEndpoint] = set()
        self._writers: dict[tuple[str, int], asyncio.StreamWriter] = {}
        self._dialog_queues: dict[tuple[tuple[str, int], str], asyncio.Queue[bytes]] = {}

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
        peer = writer.get_extra_info("peername") or ("0.0.0.0", 0)
        addr = (str(peer[0]), int(peer[1]))
        self._writers[addr] = writer

        def _send(data: bytes, _addr) -> None:
            writer.write(data)
            asyncio.create_task(writer.drain())

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
            send_override=_send,
            signaling_transport="TCP",
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
                        await queue.put(raw)
                        continue
                await endpoint._handle_datagram(raw, addr)
        finally:
            self.endpoints.discard(endpoint)
            self._writers.pop(addr, None)
            for key in [key for key in self._dialog_queues if key[0] == addr]:
                self._dialog_queues.pop(key, None)
            writer.close()
            with contextlib.suppress(Exception):
                await writer.wait_closed()

    def open_reused_dialog(
        self,
        addr: tuple[str, int],
        call_id: str,
    ) -> tuple[TcpDialogSender, asyncio.Queue[bytes]] | None:
        writer = self._writers.get(addr)
        if writer is None or writer.is_closing():
            return None
        queue: asyncio.Queue[bytes] = asyncio.Queue()
        key = (addr, call_id)
        self._dialog_queues[key] = queue

        def _send(data: bytes) -> None:
            writer.write(data)

            async def _drain() -> None:
                try:
                    await writer.drain()
                except (ConnectionError, RuntimeError, OSError):
                    _LOGGER.debug("SIP TCP reused dialog write drain failed", exc_info=True)

            try:
                asyncio.get_running_loop().create_task(_drain())
            except RuntimeError:
                pass

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
        for writer in tuple(self._writers.values()):
            writer.close()
        self.endpoints.clear()
        self._writers.clear()
        self._dialog_queues.clear()
