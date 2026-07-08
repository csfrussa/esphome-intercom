"""Local SIP registrar for standard SIP endpoints registered to Home Assistant."""

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
REALM = "voip_stack"
NONCE_TTL = 600.0


@dataclass(frozen=True, slots=True)
class SipAccount:
    username: str
    display_name: str
    password: str
    enabled: bool = True
    extension: str = ""
    conference_group: str = ""
    conference_ring: bool = False
    ring_group: str = ""

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
        extension=str(raw.get("extension") or "").strip(),
        conference_group=str(raw.get("conference_group") or "").strip(),
        conference_ring=bool(raw.get("conference_ring", False)),
        ring_group=str(raw.get("ring_group") or "").strip(),
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


def _parse_expires(raw: str, default: int = 3600) -> int:
    try:
        return max(0, min(int(raw), 86400))
    except (TypeError, ValueError):
        return default


def _register_contacts(request: sip.SipMessage) -> list[tuple[str, int, str]]:
    default_expires = _parse_expires(request.header("Expires") or "3600")
    contacts: list[tuple[str, int, str]] = []
    for raw in request.header_values("Contact"):
        header = raw.strip()
        if not header:
            continue
        if header == "*":
            contacts.append(("*", 0, header))
            continue
        uri = _extract_uri(header)
        if not uri:
            continue
        contact_expires = _header_param(header, "expires")
        expires = _parse_expires(contact_expires, default_expires) if contact_expires else default_expires
        contacts.append((uri, expires, header))
    return contacts


def _same_contact(left: str, right: str) -> bool:
    return _extract_uri(left).strip().lower() == _extract_uri(right).strip().lower()


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

        contacts = _register_contacts(request)
        if not contacts:
            return self._result(400, "Bad Request")
        active_contacts = [contact for contact in contacts if contact[0] != "*" and contact[1] > 0]
        remove_contacts = [contact for contact in contacts if contact[0] == "*" or contact[1] <= 0]

        if active_contacts:
            contact_uri, expires, raw_contact = active_contacts[-1]
            self.registrations[account.username] = SipRegistration(
                username=account.username,
                contact_uri=contact_uri,
                source_host=addr[0],
                source_port=int(addr[1]),
                transport=transport,
                expires_at=time.time() + expires,
                user_agent=request.header("User-Agent"),
            )
            _LOGGER.info(
                "SIP registrar registered user=%s transport=%s expires=%ss contact=%s contacts=%s",
                account.username,
                transport.upper(),
                expires,
                contact_uri,
                len(contacts),
            )
            return self._result(200, "OK", (("Expires", str(expires)), ("Contact", raw_contact)))

        if remove_contacts:
            current = self.registrations.get(account.username)
            contact_uri, _expires_value, _raw_contact = remove_contacts[-1]
            if contact_uri == "*" or (
                current is not None and contact_uri and _same_contact(contact_uri, current.contact_uri)
            ):
                self.registrations.pop(account.username, None)
                _LOGGER.info(
                    "SIP registrar unregistered user=%s transport=%s contact=%s contacts=%s",
                    account.username,
                    transport.upper(),
                    contact_uri or "*",
                    len(contacts),
                )
            else:
                _LOGGER.info(
                    "SIP registrar ignored unregister for stale contact user=%s transport=%s contact=%s active=%s",
                    account.username,
                    transport.upper(),
                    contact_uri or "-",
                    current.contact_uri if current is not None else "-",
                )
            return self._result(200, "OK", (("Expires", "0"),))
        return self._result(400, "Bad Request")

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
        return self.registered_roster_entries()

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
                    sip_uri=registration.contact_uri,
                    extension=account.extension,
                    metadata={
                        "sip_transport": registration.transport.lower(),
                        "registered": True,
                        "user_agent": registration.user_agent,
                        "conference_group": account.conference_group,
                        "conference_ring": bool(account.conference_ring),
                        "ring_group": account.ring_group,
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
