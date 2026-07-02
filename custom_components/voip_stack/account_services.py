"""HA service handlers for local SIP registrar accounts."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
import logging

from homeassistant.components import persistent_notification
from homeassistant.core import ServiceCall
from homeassistant.exceptions import ServiceValidationError

from .const import DOMAIN
from .sip_registrar import SipAccount, dump_account, generate_password, normalize_username
from .store import sip_account_dicts, update_sip_accounts
from .websocket_api import _fire_call_event

_LOGGER = logging.getLogger(__name__)


def build_account_service_handlers(
    refresh_and_push_phonebook: Callable[[object], Awaitable[None]],
) -> dict[str, Callable[[ServiceCall], Awaitable[None]]]:
    """Build account service handlers with the phonebook refresh dependency injected."""

    async def create_account(call: ServiceCall) -> None:
        hass = call.hass
        username = normalize_username(str(call.data["username"]))
        display_name = str(call.data.get("display_name") or username).strip()
        replace_existing = bool(call.data.get("replace", False))
        accounts = sip_account_dicts(hass)
        if any(str(item.get("username") or "").lower() == username.lower() for item in accounts) and not replace_existing:
            raise ServiceValidationError(f"SIP account {username} already exists")
        provided_password = str(call.data.get("password") or "").strip()
        password = provided_password or generate_password()
        account = SipAccount(
            username=username,
            display_name=display_name,
            password=password,
            enabled=bool(call.data.get("enabled", True)),
        )
        accounts = [item for item in accounts if str(item.get("username") or "").lower() != username.lower()]
        accounts.append(dump_account(account))
        update_sip_accounts(hass, accounts)
        await refresh_and_push_phonebook(hass)
        _fire_call_event(
            hass,
            {"state": "sip_account_created", "username": username, "display_name": display_name, "password": password},
            "sip",
        )
        if not provided_password:
            persistent_notification.async_create(
                hass,
                (
                    f"SIP account `{username}` created for `{display_name}`.\n\n"
                    f"Password: `{password}`\n\n"
                    "This generated password is shown only now. Save it in the softphone "
                    "configuration or rotate the account password later."
                ),
                title="VoIP Stack SIP Account",
                notification_id=f"{DOMAIN}_sip_account_{username.lower()}",
            )
        _LOGGER.info("SIP local account created username=%s enabled=%s", username, account.enabled)

    async def remove_account(call: ServiceCall) -> None:
        hass = call.hass
        username = normalize_username(str(call.data["username"]))
        accounts = [
            item for item in sip_account_dicts(hass)
            if str(item.get("username") or "").lower() != username.lower()
        ]
        update_sip_accounts(hass, accounts)
        registrar = hass.data.get(DOMAIN, {}).get("sip_registrar")
        if registrar is not None:
            registrar.registrations.pop(username, None)
        await refresh_and_push_phonebook(hass)
        _LOGGER.info("SIP local account removed username=%s", username)

    async def rotate_account_password(call: ServiceCall) -> None:
        hass = call.hass
        username = normalize_username(str(call.data["username"]))
        password = generate_password()
        found = False
        accounts = []
        for item in sip_account_dicts(hass):
            if str(item.get("username") or "").lower() == username.lower():
                item["password"] = password
                found = True
            accounts.append(item)
        if not found:
            raise ServiceValidationError(f"SIP account {username} does not exist")
        update_sip_accounts(hass, accounts)
        registrar = hass.data.get(DOMAIN, {}).get("sip_registrar")
        if registrar is not None:
            registrar.registrations.pop(username, None)
        await refresh_and_push_phonebook(hass)
        _fire_call_event(hass, {"state": "sip_account_password_rotated", "username": username, "password": password}, "sip")
        _LOGGER.info("SIP local account password rotated username=%s", username)

    async def set_account_enabled(call: ServiceCall, *, enabled: bool) -> None:
        hass = call.hass
        username = normalize_username(str(call.data["username"]))
        found = False
        accounts = []
        for item in sip_account_dicts(hass):
            if str(item.get("username") or "").lower() == username.lower():
                item["enabled"] = enabled
                found = True
            accounts.append(item)
        if not found:
            raise ServiceValidationError(f"SIP account {username} does not exist")
        update_sip_accounts(hass, accounts)
        if not enabled:
            registrar = hass.data.get(DOMAIN, {}).get("sip_registrar")
            if registrar is not None:
                registrar.registrations.pop(username, None)
        await refresh_and_push_phonebook(hass)
        _LOGGER.info("SIP local account %s username=%s", "enabled" if enabled else "disabled", username)

    async def export_accounts(call: ServiceCall) -> None:
        accounts = [
            {
                "username": item.get("username", ""),
                "display_name": item.get("display_name", ""),
                "enabled": bool(item.get("enabled", True)),
            }
            for item in sip_account_dicts(call.hass)
        ]
        _fire_call_event(call.hass, {"state": "export_accounts", "accounts": accounts}, "sip")

    async def enable_account(call: ServiceCall) -> None:
        await set_account_enabled(call, enabled=True)

    async def disable_account(call: ServiceCall) -> None:
        await set_account_enabled(call, enabled=False)

    return {
        "create_account": create_account,
        "remove_account": remove_account,
        "rotate_account_password": rotate_account_password,
        "enable_account": enable_account,
        "disable_account": disable_account,
        "export_accounts": export_accounts,
    }
