"""Local SIP registrar for softphones registered to Home Assistant."""

from __future__ import annotations

from dataclasses import dataclass, asdict
import hashlib
import hmac
import logging
from secrets import token_hex, token_urlsafe
import time
from typing import Any

from . import sip
from .sip_auth import parse_digest_challenge
from .roster import RosterEntry


_LOGGER = logging.getLogger(__name__)
REALM = "intercom_native"
NONCE_TTL = 600.0


@dataclass(frozen=True, slots=True)
class SipAccount:
    username: str
    display_name: str
    password: str
    enabled: bool = True

    @property
    def roster_name(self) -> str:
        return self.display_name or self.username


@dataclass(slots=True)
class SipRegistration:
    username: str
    contact_uri: str
    source_host: str
    source_port: int
    transport: str
    expires_at: float
    user_agent: str = ""

    def snapshot(self) -> dict[str, Any]:
        return {
            "username": self.username,
            "contact_uri": self.contact_uri,
            "source_host": self.source_host,
            "source_port": self.source_port,
            "transport": self.transport.lower(),
            "expires_at": self.expires_at,
            "user_agent": self.user_agent,
        }


@dataclass(frozen=True, slots=True)
class SipRegisterResult:
    status: int
    reason: str
    headers: tuple[tuple[str, str], ...] = ()


def generate_password() -> str:
    return token_urlsafe(18)


def normalize_username(value: str) -> str:
    cleaned = (value or "").strip()
    if not cleaned:
        raise ValueError("username is required")
    if not all(ch.isalnum() or ch in {"_", "-", "."} for ch in cleaned):
        raise ValueError("username must contain only letters, numbers, _, - or .")
    return cleaned


def account_from_mapping(raw: dict[str, Any]) -> SipAccount:
    username = normalize_username(str(raw.get("username") or raw.get("id") or ""))
    return SipAccount(
        username=username,
        display_name=str(raw.get("display_name") or raw.get("name") or username).strip(),
        password=str(raw.get("password") or ""),
        enabled=bool(raw.get("enabled", True)),
    )


def dump_account(account: SipAccount) -> dict[str, Any]:
    return asdict(account)


def _extract_uri(header: str) -> str:
    value = (header or "").strip()
    if "<" in value and ">" in value:
        value = value[value.index("<") + 1:value.index(">")]
    return value.strip()


def _extract_register_username(request: sip.SipMessage) -> str:
    candidates: list[str] = []
    for raw_uri in (request.uri, _extract_uri(request.header("To")), _extract_uri(request.header("From"))):
        if not raw_uri:
            continue
        try:
            parsed = sip.parse_sip_uri(raw_uri)
        except Exception:
            continue
        if parsed.user:
            candidates.append(parsed.user)
    if not candidates:
        auth_username = parse_digest_challenge(request.header("Authorization")).get("username", "")
        if auth_username:
            candidates.append(auth_username)
    return normalize_username(candidates[0])


def _header_param(header: str, name: str) -> str:
    wanted = name.lower()
    for part in (header or "").split(";")[1:]:
        if "=" not in part:
            continue
        key, value = part.split("=", 1)
        if key.strip().lower() == wanted:
            return value.strip()
    return ""


def _expires(request: sip.SipMessage) -> int:
    contact_expires = _header_param(request.header("Contact"), "expires")
    raw = contact_expires or request.header("Expires") or "3600"
    try:
        return max(0, min(int(raw), 86400))
    except ValueError:
        return 3600


class SipRegistrar:
    def __init__(self, *, enabled: bool, accounts: list[SipAccount], local_ip: str, local_sip_port: int) -> None:
        self.enabled = bool(enabled)
        self.local_ip = local_ip
        self.local_sip_port = int(local_sip_port)
        self.accounts = {account.username.lower(): account for account in accounts}
        self.registrations: dict[str, SipRegistration] = {}
        self.nonces: dict[str, float] = {}
        self.last_sip_event = ""
        self.last_sip_status_code = 0
        self.last_sip_reason = ""

    def update_accounts(self, accounts: list[SipAccount]) -> None:
        self.accounts = {account.username.lower(): account for account in accounts}
        for username in list(self.registrations):
            account = self.accounts.get(username.lower())
            if account is None or not account.enabled:
                self.registrations.pop(username, None)

    def _challenge(self) -> tuple[str, str]:
        nonce = token_hex(16)
        self.nonces[nonce] = time.time() + NONCE_TTL
        return nonce, f'Digest realm="{REALM}", nonce="{nonce}", algorithm=MD5, qop="auth"'

    def _valid_nonce(self, nonce: str) -> bool:
        now = time.time()
        self.nonces = {key: exp for key, exp in self.nonces.items() if exp > now}
        return bool(nonce and self.nonces.get(nonce, 0) > now)

    def _check_authorization(self, request: sip.SipMessage, account: SipAccount) -> bool:
        params = parse_digest_challenge(request.header("Authorization"))
        nonce = params.get("nonce", "")
        if not self._valid_nonce(nonce):
            return False
        username = params.get("username", "")
        if username.lower() != account.username.lower():
            return False
        realm = params.get("realm", REALM)
        uri = params.get("uri", request.uri)
        qop = params.get("qop", "")
        ha1 = hashlib.md5(f"{account.username}:{realm}:{account.password}".encode()).hexdigest()
        ha2 = hashlib.md5(f"REGISTER:{uri}".encode()).hexdigest()
        if qop:
            expected = hashlib.md5(
                f"{ha1}:{nonce}:{params.get('nc', '')}:{params.get('cnonce', '')}:{qop}:{ha2}".encode()
            ).hexdigest()
        else:
            expected = hashlib.md5(f"{ha1}:{nonce}:{ha2}".encode()).hexdigest()
        return hmac.compare_digest(expected, params.get("response", ""))

    async def handle_register(self, request: sip.SipMessage, addr: tuple[str, int], transport: str) -> SipRegisterResult:
        self.last_sip_event = "REGISTER"
        if not self.enabled:
            return self._result(405, "Method Not Allowed")
        try:
            sip.parse_sip_uri(request.uri)
            username = _extract_register_username(request)
        except Exception:
            return self._result(400, "Bad Request")
        account = self.accounts.get(username.lower())
        if account is None or not account.enabled:
            return self._result(403, "Forbidden")
        if not self._check_authorization(request, account):
            _nonce, challenge = self._challenge()
            return self._result(401, "Unauthorized", (("WWW-Authenticate", challenge),))

        expires = _expires(request)
        if expires <= 0:
            self.registrations.pop(account.username, None)
            _LOGGER.info("SIP registrar unregistered user=%s transport=%s", account.username, transport.upper())
            return self._result(200, "OK", (("Expires", "0"),))
        contact = _extract_uri(request.header("Contact"))
        if not contact:
            return self._result(400, "Bad Request")
        self.registrations[account.username] = SipRegistration(
            username=account.username,
            contact_uri=contact,
            source_host=addr[0],
            source_port=int(addr[1]),
            transport=transport,
            expires_at=time.time() + expires,
            user_agent=request.header("User-Agent"),
        )
        _LOGGER.info("SIP registrar registered user=%s transport=%s expires=%ss", account.username, transport.upper(), expires)
        return self._result(200, "OK", (("Expires", str(expires)), ("Contact", request.header("Contact"))))

    def _result(self, status: int, reason: str, headers: tuple[tuple[str, str], ...] = ()) -> SipRegisterResult:
        self.last_sip_status_code = int(status)
        self.last_sip_reason = reason
        return SipRegisterResult(status, reason, headers)

    def expire(self) -> bool:
        now = time.time()
        old = set(self.registrations)
        self.registrations = {key: reg for key, reg in self.registrations.items() if reg.expires_at > now}
        expired = old - set(self.registrations)
        for username in expired:
            _LOGGER.info("SIP registrar expired user=%s", username)
        return bool(expired)

    def roster_entries(self) -> list[RosterEntry]:
        self.expire()
        entries: list[RosterEntry] = []
        for username, account in sorted(self.accounts.items()):
            if not account.enabled:
                continue
            registration = self.registrations.get(account.username)
            metadata = {
                "registered": bool(registration),
            }
            sip_uri = ""
            if registration is not None:
                sip_uri = registration.contact_uri
                metadata.update(
                    {
                        "sip_transport": registration.transport.lower(),
                        "user_agent": registration.user_agent,
                    }
                )
            entries.append(
                RosterEntry(
                    id=account.username,
                    name=account.roster_name,
                    kind="softphone",
                    sip_uri=sip_uri,
                    metadata=metadata,
                )
            )
        return entries

    def registered_roster_entries(self) -> list[RosterEntry]:
        self.expire()
        entries: list[RosterEntry] = []
        for username, registration in sorted(self.registrations.items()):
            account = self.accounts.get(username.lower())
            if account is None or not account.enabled:
                continue
            entries.append(
                RosterEntry(
                    id=account.username,
                    name=account.roster_name,
                    kind="softphone",
                    sip_uri=registration.contact_uri,
                    metadata={
                        "sip_transport": registration.transport.lower(),
                        "registered": True,
                        "user_agent": registration.user_agent,
                    },
                )
            )
        return entries

    def snapshot(self) -> dict[str, Any]:
        self.expire()
        return {
            "registrar_enabled": self.enabled,
            "registrar_accounts": len(self.accounts),
            "registrar_registered": len(self.registrations),
            "registrar_bindings": [reg.snapshot() for reg in self.registrations.values()],
            "registrar_last_sip_event": self.last_sip_event,
            "registrar_last_sip_status_code": self.last_sip_status_code,
            "registrar_last_sip_reason": self.last_sip_reason,
        }
