"""Local SIP registrar for standard SIP endpoints registered to Home Assistant."""

from __future__ import annotations

from dataclasses import dataclass, asdict
import hmac
import logging
from secrets import token_hex, token_urlsafe
import time
from typing import Any

from . import sip
from .sip_auth import parse_digest_challenge, sip_digest_md5
from .roster import RosterEntry


_LOGGER = logging.getLogger(__name__)
REALM = "voip_stack"
NONCE_TTL = 600.0
MAX_ACTIVE_NONCES = 256
MAX_NONCE_USE_RECORDS = 2048


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
    advertised_contact_uri: str = ""
    user_agent: str = ""

    def snapshot(self) -> dict[str, Any]:
        return {
            "username": self.username,
            "contact_uri": self.contact_uri,
            "advertised_contact_uri": self.advertised_contact_uri,
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
        try:
            uri = str(sip.parse_sip_uri(uri))
        except (TypeError, ValueError, sip.SipError):
            continue
        contact_expires = _header_param(header, "expires")
        expires = _parse_expires(contact_expires, default_expires) if contact_expires else default_expires
        contacts.append((uri, expires, header))
    return contacts


def _same_contact(left: str, right: str) -> bool:
    return _extract_uri(left).strip().lower() == _extract_uri(right).strip().lower()


def _contact_for_source_flow(
    contact_uri: str,
    addr: tuple[str, int],
    transport: str,
) -> str:
    """Pin a REGISTER binding to the authenticated signaling flow.

    The Contact user and non-transport URI parameters remain intact, while an
    endpoint cannot turn its authenticated account into an arbitrary network
    target.  This is also the NAT-friendly behavior expected for a registrar
    that receives REGISTER directly rather than through a trusted edge proxy.
    """

    advertised = sip.parse_sip_uri(contact_uri)
    source_host = str(addr[0] or "").strip()
    if ":" in source_host and not source_host.startswith("["):
        source_host = f"[{source_host}]"
    source_port = int(addr[1])
    if not source_host or not 1 <= source_port <= 65535:
        raise sip.SipError("REGISTER source flow is invalid")
    actual_transport = str(transport or "UDP").strip().lower()
    had_transport = any(
        key.lower() == "transport" for key, _value in advertised.params
    )
    params = tuple(
        (key, value)
        for key, value in advertised.params
        if key.lower() != "transport"
    )
    if had_transport or actual_transport != "udp":
        params += (("transport", actual_transport),)
    return str(
        sip.SipUri(
            user=advertised.user,
            host=source_host,
            port=source_port,
            params=params,
        )
    )


class SipRegistrar:
    def __init__(self, *, enabled: bool, accounts: list[SipAccount], local_ip: str, local_sip_port: int) -> None:
        self.enabled = bool(enabled)
        self.local_ip = local_ip
        self.local_sip_port = int(local_sip_port)
        self.accounts = {account.username.lower(): account for account in accounts}
        self.registrations: dict[str, SipRegistration] = {}
        self.nonces: dict[str, float] = {}
        self.nonce_uses: dict[
            tuple[str, str, str],
            tuple[int, tuple[Any, ...]],
        ] = {}
        self.source_nonces: dict[str, str] = {}
        self.last_sip_event = ""
        self.last_sip_status_code = 0
        self.last_sip_reason = ""

    def update_accounts(self, accounts: list[SipAccount]) -> None:
        previous_accounts = self.accounts
        self.accounts = {account.username.lower(): account for account in accounts}
        registrations: dict[str, SipRegistration] = {}
        for registration in self.registrations.values():
            key = registration.username.lower()
            account = self.accounts.get(key)
            previous = previous_accounts.get(key)
            if (
                account is None
                or not account.enabled
                or previous is None
                or not hmac.compare_digest(previous.password, account.password)
            ):
                continue
            registration.username = account.username
            registrations[account.username] = registration
        self.registrations = registrations

    def _registration(self, username: str) -> SipRegistration | None:
        wanted = str(username or "").lower()
        return next(
            (
                registration
                for key, registration in self.registrations.items()
                if key.lower() == wanted or registration.username.lower() == wanted
            ),
            None,
        )

    def remove_registration(self, username: str) -> None:
        wanted = str(username or "").lower()
        for key in list(self.registrations):
            registration = self.registrations[key]
            if key.lower() == wanted or registration.username.lower() == wanted:
                self.registrations.pop(key, None)

    def registration_matches_source(
        self,
        username: str,
        host: str,
        port: int,
        transport: str,
    ) -> bool:
        """Authenticate an in-dialog origin against its live REGISTER flow."""

        self.expire()
        registration = self._registration(username)
        return bool(
            registration is not None
            and registration.source_host == str(host or "")
            and int(registration.source_port) == int(port)
            and registration.transport.upper() == str(transport or "").upper()
        )

    def _prune_nonces(self) -> None:
        now = time.time()
        self.nonces = {key: exp for key, exp in self.nonces.items() if exp > now}
        active = set(self.nonces)
        self.nonce_uses = {
            key: value for key, value in self.nonce_uses.items() if key[0] in active
        }
        self.source_nonces = {
            source: nonce
            for source, nonce in self.source_nonces.items()
            if nonce in active
        }

    def _challenge(self, source: str = "") -> tuple[str, str]:
        self._prune_nonces()
        cached_nonce = self.source_nonces.get(source) if source else None
        if cached_nonce and cached_nonce in self.nonces:
            return cached_nonce, (
                f'Digest realm="{REALM}", nonce="{cached_nonce}", '
                'algorithm=MD5, qop="auth"'
            )
        while len(self.nonces) >= MAX_ACTIVE_NONCES:
            expired_nonce = next(iter(self.nonces))
            self.nonces.pop(expired_nonce)
            self.nonce_uses = {
                key: value
                for key, value in self.nonce_uses.items()
                if key[0] != expired_nonce
            }
            self.source_nonces = {
                key: value
                for key, value in self.source_nonces.items()
                if value != expired_nonce
            }
        nonce = token_hex(16)
        self.nonces[nonce] = time.time() + NONCE_TTL
        if source:
            self.source_nonces[source] = nonce
        return nonce, f'Digest realm="{REALM}", nonce="{nonce}", algorithm=MD5, qop="auth"'

    def _valid_nonce(self, nonce: str) -> bool:
        self._prune_nonces()
        return bool(nonce and nonce in self.nonces)

    @staticmethod
    def _register_fingerprint(
        request: sip.SipMessage,
        addr: tuple[str, int],
        transport: str,
    ) -> tuple[Any, ...]:
        return (
            request.method,
            request.uri,
            request.header("Call-ID"),
            request.header("CSeq"),
            request.header_values("Via")[:1],
            request.header_values("Contact"),
            request.header("Expires"),
            str(addr[0]),
            int(addr[1]),
            str(transport or "").upper(),
        )

    def _check_authorization(
        self,
        request: sip.SipMessage,
        account: SipAccount,
        addr: tuple[str, int],
        transport: str,
    ) -> bool:
        params = parse_digest_challenge(request.header("Authorization"))
        nonce = params.get("nonce", "")
        if not self._valid_nonce(nonce):
            return False
        username = params.get("username", "")
        if username.lower() != account.username.lower():
            return False
        realm = params.get("realm", REALM)
        uri = params.get("uri", request.uri)
        qop = params.get("qop", "").lower()
        algorithm = params.get("algorithm", "MD5").upper()
        cnonce = params.get("cnonce", "")
        nc = params.get("nc", "")
        if (
            realm != REALM
            or uri != request.uri
            or qop != "auth"
            or algorithm != "MD5"
            or not cnonce
            or len(cnonce) > 128
            or len(nc) != 8
        ):
            return False
        try:
            nonce_count = int(nc, 16)
        except ValueError:
            return False
        if nonce_count <= 0:
            return False
        ha1 = sip_digest_md5(f"{account.username}:{realm}:{account.password}")
        ha2 = sip_digest_md5(f"REGISTER:{uri}")
        expected = sip_digest_md5(
            f"{ha1}:{nonce}:{nc}:{cnonce}:{qop}:{ha2}"
        )
        if not hmac.compare_digest(expected, params.get("response", "")):
            return False
        use_key = (nonce, account.username.lower(), cnonce)
        fingerprint = self._register_fingerprint(request, addr, transport)
        previous = self.nonce_uses.get(use_key)
        if previous is not None:
            previous_count, previous_fingerprint = previous
            if nonce_count < previous_count:
                return False
            if nonce_count == previous_count:
                return hmac.compare_digest(
                    repr(fingerprint),
                    repr(previous_fingerprint),
                )
        if use_key not in self.nonce_uses:
            while len(self.nonce_uses) >= MAX_NONCE_USE_RECORDS:
                self.nonce_uses.pop(next(iter(self.nonce_uses)))
        self.nonce_uses[use_key] = (nonce_count, fingerprint)
        return True

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
        source_key = f"{str(transport or '').upper()}:{addr[0]}"
        if (
            account is None
            or not account.enabled
            or not self._check_authorization(request, account, addr, transport)
        ):
            _nonce, challenge = self._challenge(source_key)
            return self._result(401, "Unauthorized", (("WWW-Authenticate", challenge),))

        contacts = _register_contacts(request)
        if not contacts:
            return self._result(400, "Bad Request")
        active_contacts = [contact for contact in contacts if contact[0] != "*" and contact[1] > 0]
        remove_contacts = [contact for contact in contacts if contact[0] == "*" or contact[1] <= 0]

        if active_contacts:
            advertised_contact_uri, expires, raw_contact = active_contacts[-1]
            try:
                contact_uri = _contact_for_source_flow(
                    advertised_contact_uri,
                    addr,
                    transport,
                )
            except (TypeError, ValueError, sip.SipError):
                return self._result(400, "Bad Request")
            self.remove_registration(account.username)
            self.registrations[account.username] = SipRegistration(
                username=account.username,
                contact_uri=contact_uri,
                source_host=addr[0],
                source_port=int(addr[1]),
                transport=transport,
                expires_at=time.time() + expires,
                advertised_contact_uri=advertised_contact_uri,
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
            current = self._registration(account.username)
            contact_uri, _expires_value, _raw_contact = remove_contacts[-1]
            if contact_uri == "*" or (
                current is not None
                and contact_uri
                and _same_contact(
                    contact_uri,
                    current.advertised_contact_uri or current.contact_uri,
                )
            ):
                self.remove_registration(account.username)
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
