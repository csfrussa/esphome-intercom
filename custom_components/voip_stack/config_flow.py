"""Config flow for VoIP Stack."""

from collections.abc import Mapping
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import SOURCE_RECONFIGURE, ConfigEntry, ConfigFlow
from homeassistant.helpers.selector import (
    BooleanSelector,
    AssistPipelineSelector,
    NumberSelector,
    NumberSelectorConfig,
    SelectSelector,
    SelectSelectorConfig,
    TextSelector,
)

from .config_validation import extension_conflicts
from .const import (
    CONF_ASSIST_ENDPOINT_ENABLED,
    CONF_ASSIST_EXTENSION,
    CONF_ASSIST_PIPELINE,
    CONF_ASSIST_INTENTS,
    CONF_AUTOMATION_ROUTING_ENABLED,
    CONF_DEBUG_MODE,
    CONF_EXPERIMENTAL_VIDEO,
    CONF_VIDEO_CAMERA_SEND,
    CONF_VIDEO_TRANSCODING,
    CONF_PHONEBOOK_CONTACTS,
    CONF_REGISTRAR_ENABLED,
    CONF_TRUNK_AUTH_USERNAME,
    CONF_TRUNK_DOMAIN,
    CONF_TRUNK_DTMF_ENABLED,
    CONF_TRUNK_DTMF_TERMINATOR,
    CONF_TRUNK_DTMF_TIMEOUT_MS,
    CONF_TRUNK_ENABLED,
    CONF_TRUNK_EXPIRES,
    CONF_TRUNK_INBOUND_DEFAULT_TARGET,
    CONF_TRUNK_INBOUND_MODE,
    CONF_TRUNK_OUTBOUND_PROXY,
    CONF_TRUNK_PASSWORD,
    CONF_TRUNK_PORT,
    CONF_TRUNK_SERVER,
    CONF_TRUNK_TRANSPORT,
    CONF_TRUNK_USERNAME,
    DOMAIN,
    TRUNK_INBOUND_MODE_DIRECT,
    TRUNK_INBOUND_MODE_DTMF,
    VOIP_STACK_RTP_PORT,
    VOIP_STACK_SIP_PORT,
)
def _port_selector():
    return NumberSelector(NumberSelectorConfig(min=1, max=65535, step=1, mode="box"))


def _dtmf_timeout_seconds(value: object) -> int:
    raw = 3000 if value in (None, "") else int(value)
    if 0 <= raw <= 10:
        return raw
    return max(0, min(10, round(raw / 1000)))


def _disabled_trunk_data(data: dict, existing: Mapping[str, Any]) -> dict:
    """Return entry data with trunk explicitly disabled."""
    data.update(
        {
            CONF_TRUNK_ENABLED: False,
            CONF_TRUNK_TRANSPORT: existing.get(CONF_TRUNK_TRANSPORT, "udp"),
            CONF_TRUNK_SERVER: existing.get(CONF_TRUNK_SERVER, ""),
            CONF_TRUNK_PORT: existing.get(CONF_TRUNK_PORT, VOIP_STACK_SIP_PORT),
            CONF_TRUNK_DOMAIN: existing.get(CONF_TRUNK_DOMAIN, ""),
            CONF_TRUNK_USERNAME: existing.get(CONF_TRUNK_USERNAME, ""),
            CONF_TRUNK_AUTH_USERNAME: existing.get(CONF_TRUNK_AUTH_USERNAME, ""),
            CONF_TRUNK_PASSWORD: existing.get(CONF_TRUNK_PASSWORD, ""),
            CONF_TRUNK_EXPIRES: existing.get(CONF_TRUNK_EXPIRES, 300),
            CONF_TRUNK_OUTBOUND_PROXY: existing.get(CONF_TRUNK_OUTBOUND_PROXY, ""),
            CONF_TRUNK_INBOUND_DEFAULT_TARGET: existing.get(CONF_TRUNK_INBOUND_DEFAULT_TARGET, "HA"),
            CONF_TRUNK_INBOUND_MODE: existing.get(CONF_TRUNK_INBOUND_MODE, TRUNK_INBOUND_MODE_DIRECT),
            CONF_AUTOMATION_ROUTING_ENABLED: existing.get(CONF_AUTOMATION_ROUTING_ENABLED, False),
            CONF_TRUNK_DTMF_ENABLED: existing.get(CONF_TRUNK_DTMF_ENABLED, False),
            CONF_TRUNK_DTMF_TIMEOUT_MS: int(existing.get(CONF_TRUNK_DTMF_TIMEOUT_MS, 3000)),
            CONF_TRUNK_DTMF_TERMINATOR: existing.get(CONF_TRUNK_DTMF_TERMINATOR, ""),
            "sip_accounts": existing.get("sip_accounts", []),
            CONF_PHONEBOOK_CONTACTS: existing.get(CONF_PHONEBOOK_CONTACTS, []),
        }
    )
    return data


class VoipStackConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for VoIP Stack."""

    VERSION = 2
    _base_input: dict | None = None

    def _current_entry(self) -> ConfigEntry | None:
        if self.source == SOURCE_RECONFIGURE:
            return self._get_reconfigure_entry()
        return next(iter(self._async_current_entries()), None)

    def _current_entry_data(self) -> tuple[ConfigEntry | None, Mapping[str, Any]]:
        current_entry = self._current_entry()
        return current_entry, (current_entry.data if current_entry else {})

    def _store_entry(self, data: dict):
        current_entry, _existing = self._current_entry_data()
        if current_entry is not None:
            return self.async_update_reload_and_abort(
                current_entry,
                data=data,
                reason="reconfigure_successful",
            )
        return self.async_create_entry(title="VoIP Stack", data=data)

    async def async_step_reconfigure(self, user_input=None):
        """Reconfigure the existing VoIP Stack entry from HA's native menu."""
        return await self.async_step_user(user_input)

    async def async_step_user(self, user_input=None):
        """Handle install/reconfigure of the single VoIP Stack entry.

        SIP is the only HA call-control protocol. HA is always both a
        softphone and SIP router/B2BUA, and it listens on both SIP/UDP and
        SIP/TCP like a normal SIP endpoint.
        """
        _current_entry, existing = self._current_entry_data()
        defaults = {
            "sip_port": existing.get("sip_port", VOIP_STACK_SIP_PORT),
            "rtp_port": existing.get("rtp_port", VOIP_STACK_RTP_PORT),
            "advertise_host": existing.get("advertise_host", ""),
            CONF_ASSIST_INTENTS: existing.get(CONF_ASSIST_INTENTS, False),
            CONF_ASSIST_ENDPOINT_ENABLED: existing.get(CONF_ASSIST_ENDPOINT_ENABLED, False),
            CONF_DEBUG_MODE: existing.get(CONF_DEBUG_MODE, False),
            CONF_EXPERIMENTAL_VIDEO: existing.get(CONF_EXPERIMENTAL_VIDEO, False),
            CONF_REGISTRAR_ENABLED: existing.get(CONF_REGISTRAR_ENABLED, False),
            CONF_TRUNK_ENABLED: existing.get(CONF_TRUNK_ENABLED, False),
        }
        schema = vol.Schema(
            {
                vol.Required("sip_port", default=defaults["sip_port"]): _port_selector(),
                vol.Required("rtp_port", default=defaults["rtp_port"]): _port_selector(),
                vol.Optional("advertise_host", default=defaults["advertise_host"]): str,
                vol.Required(
                    CONF_ASSIST_INTENTS,
                    default=defaults[CONF_ASSIST_INTENTS],
                ): BooleanSelector(),
                vol.Required(CONF_ASSIST_ENDPOINT_ENABLED, default=defaults[CONF_ASSIST_ENDPOINT_ENABLED]): BooleanSelector(),
                vol.Required(CONF_DEBUG_MODE, default=defaults[CONF_DEBUG_MODE]): BooleanSelector(),
                vol.Required(
                    CONF_EXPERIMENTAL_VIDEO,
                    default=defaults[CONF_EXPERIMENTAL_VIDEO],
                ): BooleanSelector(),
                vol.Required(CONF_REGISTRAR_ENABLED, default=defaults[CONF_REGISTRAR_ENABLED]): BooleanSelector(),
                vol.Required(CONF_TRUNK_ENABLED, default=defaults[CONF_TRUNK_ENABLED]): BooleanSelector(),
            }
        )
        errors: dict[str, str] = {}

        if user_input is not None:
            # Number selectors hand floats; coerce to int once on the boundary.
            for k in ("sip_port", "rtp_port"):
                user_input[k] = int(user_input[k])
            for k in ("advertise_host",):
                user_input[k] = (user_input.get(k) or "").strip()
            if user_input["sip_port"] == user_input["rtp_port"]:
                errors["base"] = "sip_rtp_port_conflict"

            if not errors:
                self._base_input = dict(user_input)
                if user_input[CONF_EXPERIMENTAL_VIDEO]:
                    return await self.async_step_video()
                self._base_input[CONF_VIDEO_TRANSCODING] = False
                self._base_input[CONF_VIDEO_CAMERA_SEND] = False
                if user_input[CONF_ASSIST_ENDPOINT_ENABLED]:
                    return await self.async_step_assist()
                self._base_input[CONF_ASSIST_EXTENSION] = str(existing.get(CONF_ASSIST_EXTENSION) or "").strip()
                self._base_input[CONF_ASSIST_PIPELINE] = str(existing.get(CONF_ASSIST_PIPELINE) or "").strip()
                if user_input[CONF_TRUNK_ENABLED]:
                    return await self.async_step_trunk()
                data = dict(user_input)
                _disabled_trunk_data(data, existing)
                current_entry, _existing = self._current_entry_data()
                if current_entry is None:
                    await self.async_set_unique_id(DOMAIN)
                    self._abort_if_unique_id_configured()
                return self._store_entry(data)

        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)

    async def async_step_video(self, user_input=None):
        """Configure optional browser video features without affecting audio."""

        _current_entry, existing = self._current_entry_data()
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_VIDEO_TRANSCODING,
                    default=bool(existing.get(CONF_VIDEO_TRANSCODING, False)),
                ): BooleanSelector(),
                vol.Required(
                    CONF_VIDEO_CAMERA_SEND,
                    default=bool(existing.get(CONF_VIDEO_CAMERA_SEND, False)),
                ): BooleanSelector(),
            }
        )
        if user_input is not None:
            assert self._base_input is not None
            self._base_input[CONF_VIDEO_TRANSCODING] = bool(user_input[CONF_VIDEO_TRANSCODING])
            self._base_input[CONF_VIDEO_CAMERA_SEND] = bool(user_input[CONF_VIDEO_CAMERA_SEND])
            if self._base_input[CONF_ASSIST_ENDPOINT_ENABLED]:
                return await self.async_step_assist()
            self._base_input[CONF_ASSIST_EXTENSION] = str(existing.get(CONF_ASSIST_EXTENSION) or "").strip()
            self._base_input[CONF_ASSIST_PIPELINE] = str(existing.get(CONF_ASSIST_PIPELINE) or "").strip()
            if self._base_input[CONF_TRUNK_ENABLED]:
                return await self.async_step_trunk()
            data = dict(self._base_input)
            _disabled_trunk_data(data, existing)
            current_entry, _existing = self._current_entry_data()
            if current_entry is None:
                await self.async_set_unique_id(DOMAIN)
                self._abort_if_unique_id_configured()
            return self._store_entry(data)
        return self.async_show_form(step_id="video", data_schema=schema)

    async def async_step_assist(self, user_input=None):
        """Configure the optional local Assist pipeline extension."""
        _current_entry, existing = self._current_entry_data()
        suggested = str(existing.get(CONF_ASSIST_EXTENSION) or "").strip()
        pipeline = str(existing.get(CONF_ASSIST_PIPELINE) or "").strip()
        extension_key = vol.Required(CONF_ASSIST_EXTENSION, default=suggested) if suggested else vol.Required(CONF_ASSIST_EXTENSION)
        # HA's native selector represents "Preferred assistant" as an empty
        # selection, while named pipelines carry their concrete pipeline ID.
        pipeline_key = (
            vol.Optional(CONF_ASSIST_PIPELINE, description={"suggested_value": pipeline})
            if pipeline and pipeline != "preferred"
            else vol.Optional(CONF_ASSIST_PIPELINE)
        )
        schema = vol.Schema({
            extension_key: TextSelector(),
            pipeline_key: AssistPipelineSelector(),
        })
        errors: dict[str, str] = {}
        if user_input is not None:
            extension = str(user_input.get(CONF_ASSIST_EXTENSION) or "").strip()
            if not extension.isdigit() or not 1 <= len(extension) <= 8:
                errors["base"] = "assist_extension_invalid"
            elif extension != suggested and extension_conflicts(extension, existing):
                errors["base"] = "assist_extension_conflict"
            if not errors:
                assert self._base_input is not None
                self._base_input[CONF_ASSIST_EXTENSION] = extension
                self._base_input[CONF_ASSIST_PIPELINE] = str(user_input.get(CONF_ASSIST_PIPELINE) or "preferred").strip()
                if self._base_input[CONF_TRUNK_ENABLED]:
                    return await self.async_step_trunk()
                data = dict(self._base_input)
                _disabled_trunk_data(data, existing)
                current_entry, _existing = self._current_entry_data()
                if current_entry is None:
                    await self.async_set_unique_id(DOMAIN)
                    self._abort_if_unique_id_configured()
                return self._store_entry(data)
        return self.async_show_form(step_id="assist", data_schema=schema, errors=errors)

    async def async_step_trunk(self, user_input=None):
        _current_entry, existing = self._current_entry_data()
        legacy_timeout = _dtmf_timeout_seconds(existing.get(CONF_TRUNK_DTMF_TIMEOUT_MS, 3000))
        legacy_mode = (
            TRUNK_INBOUND_MODE_DTMF
            if existing.get(CONF_TRUNK_DTMF_ENABLED, True) and legacy_timeout > 0
            else TRUNK_INBOUND_MODE_DIRECT
        )
        defaults = {
            CONF_TRUNK_TRANSPORT: existing.get(CONF_TRUNK_TRANSPORT, "udp"),
            CONF_TRUNK_SERVER: existing.get(CONF_TRUNK_SERVER, ""),
            CONF_TRUNK_PORT: existing.get(CONF_TRUNK_PORT, VOIP_STACK_SIP_PORT),
            CONF_TRUNK_DOMAIN: existing.get(CONF_TRUNK_DOMAIN, ""),
            CONF_TRUNK_USERNAME: existing.get(CONF_TRUNK_USERNAME, ""),
            CONF_TRUNK_AUTH_USERNAME: existing.get(CONF_TRUNK_AUTH_USERNAME, ""),
            CONF_TRUNK_PASSWORD: existing.get(CONF_TRUNK_PASSWORD, ""),
            CONF_TRUNK_EXPIRES: existing.get(CONF_TRUNK_EXPIRES, 300),
            CONF_TRUNK_OUTBOUND_PROXY: existing.get(CONF_TRUNK_OUTBOUND_PROXY, ""),
            CONF_TRUNK_INBOUND_DEFAULT_TARGET: existing.get(CONF_TRUNK_INBOUND_DEFAULT_TARGET, "HA"),
            CONF_TRUNK_INBOUND_MODE: existing.get(CONF_TRUNK_INBOUND_MODE, legacy_mode),
            CONF_AUTOMATION_ROUTING_ENABLED: existing.get(CONF_AUTOMATION_ROUTING_ENABLED, False),
            CONF_TRUNK_DTMF_TIMEOUT_MS: legacy_timeout,
            CONF_TRUNK_DTMF_TERMINATOR: existing.get(CONF_TRUNK_DTMF_TERMINATOR, ""),
        }
        schema = vol.Schema(
            {
                vol.Required(CONF_TRUNK_TRANSPORT, default=defaults[CONF_TRUNK_TRANSPORT]): SelectSelector(
                    SelectSelectorConfig(options=["udp", "tcp"])
                ),
                vol.Required(CONF_TRUNK_SERVER, default=defaults[CONF_TRUNK_SERVER]): TextSelector(),
                vol.Required(CONF_TRUNK_PORT, default=defaults[CONF_TRUNK_PORT]): _port_selector(),
                vol.Optional(CONF_TRUNK_DOMAIN, default=defaults[CONF_TRUNK_DOMAIN]): TextSelector(),
                vol.Required(CONF_TRUNK_USERNAME, default=defaults[CONF_TRUNK_USERNAME]): TextSelector(),
                vol.Optional(CONF_TRUNK_AUTH_USERNAME, default=defaults[CONF_TRUNK_AUTH_USERNAME]): TextSelector(),
                vol.Required(CONF_TRUNK_PASSWORD, default=defaults[CONF_TRUNK_PASSWORD]): TextSelector(),
                vol.Required(CONF_TRUNK_EXPIRES, default=defaults[CONF_TRUNK_EXPIRES]): NumberSelector(
                    NumberSelectorConfig(min=60, max=3600, step=30, mode="box")
                ),
                vol.Optional(CONF_TRUNK_OUTBOUND_PROXY, default=defaults[CONF_TRUNK_OUTBOUND_PROXY]): TextSelector(),
                vol.Optional(
                    CONF_TRUNK_INBOUND_DEFAULT_TARGET,
                    default=defaults[CONF_TRUNK_INBOUND_DEFAULT_TARGET],
                ): TextSelector(),
                vol.Required(CONF_TRUNK_INBOUND_MODE, default=defaults[CONF_TRUNK_INBOUND_MODE]): SelectSelector(
                    SelectSelectorConfig(options=[TRUNK_INBOUND_MODE_DIRECT, TRUNK_INBOUND_MODE_DTMF], translation_key="trunk_inbound_mode")
                ),
                vol.Required(
                    CONF_AUTOMATION_ROUTING_ENABLED,
                    default=defaults[CONF_AUTOMATION_ROUTING_ENABLED],
                ): BooleanSelector(),
                vol.Required(CONF_TRUNK_DTMF_TIMEOUT_MS, default=defaults[CONF_TRUNK_DTMF_TIMEOUT_MS]): NumberSelector(
                    NumberSelectorConfig(min=0, max=10, step=1, mode="box")
                ),
                vol.Optional(CONF_TRUNK_DTMF_TERMINATOR, default=defaults[CONF_TRUNK_DTMF_TERMINATOR]): TextSelector(),
            }
        )
        errors: dict[str, str] = {}
        if user_input is not None:
            for k in (CONF_TRUNK_PORT, CONF_TRUNK_EXPIRES, CONF_TRUNK_DTMF_TIMEOUT_MS):
                user_input[k] = int(user_input[k])
            user_input[CONF_TRUNK_DTMF_TIMEOUT_MS] = max(0, min(10, user_input[CONF_TRUNK_DTMF_TIMEOUT_MS])) * 1000
            inbound_mode = str(user_input.get(CONF_TRUNK_INBOUND_MODE) or TRUNK_INBOUND_MODE_DIRECT)
            user_input[CONF_TRUNK_DTMF_ENABLED] = (
                inbound_mode == TRUNK_INBOUND_MODE_DTMF
                and user_input[CONF_TRUNK_DTMF_TIMEOUT_MS] > 0
            )
            for k in (
                CONF_TRUNK_SERVER,
                CONF_TRUNK_DOMAIN,
                CONF_TRUNK_USERNAME,
                CONF_TRUNK_AUTH_USERNAME,
                CONF_TRUNK_OUTBOUND_PROXY,
                CONF_TRUNK_INBOUND_DEFAULT_TARGET,
                CONF_TRUNK_DTMF_TERMINATOR,
            ):
                user_input[k] = (user_input.get(k) or "").strip()
            # SIP digest credentials are opaque. Leading/trailing whitespace is
            # uncommon but valid and must survive a reconfigure unchanged.
            user_input[CONF_TRUNK_PASSWORD] = str(user_input.get(CONF_TRUNK_PASSWORD) or "")
            trunk_fields_empty = not any(
                user_input.get(k)
                for k in (
                    CONF_TRUNK_SERVER,
                    CONF_TRUNK_USERNAME,
                    CONF_TRUNK_AUTH_USERNAME,
                    CONF_TRUNK_PASSWORD,
                    CONF_TRUNK_DOMAIN,
                    CONF_TRUNK_OUTBOUND_PROXY,
                )
            )
            if trunk_fields_empty:
                data = dict(
                    self._base_input
                    or {
                        "sip_port": int(existing.get("sip_port", VOIP_STACK_SIP_PORT)),
                        "rtp_port": int(existing.get("rtp_port", VOIP_STACK_RTP_PORT)),
                        "advertise_host": str(existing.get("advertise_host", "") or "").strip(),
                        CONF_ASSIST_INTENTS: bool(existing.get(CONF_ASSIST_INTENTS, False)),
                        CONF_DEBUG_MODE: bool(existing.get(CONF_DEBUG_MODE, False)),
                        CONF_EXPERIMENTAL_VIDEO: bool(existing.get(CONF_EXPERIMENTAL_VIDEO, False)),
                        CONF_VIDEO_TRANSCODING: bool(existing.get(CONF_VIDEO_TRANSCODING, False)),
                        CONF_VIDEO_CAMERA_SEND: bool(existing.get(CONF_VIDEO_CAMERA_SEND, False)),
                        CONF_REGISTRAR_ENABLED: bool(existing.get(CONF_REGISTRAR_ENABLED, False)),
                    }
                )
                return self._store_entry(_disabled_trunk_data(data, existing))
            if not user_input[CONF_TRUNK_SERVER]:
                errors["base"] = "trunk_server_required"
            elif not user_input[CONF_TRUNK_USERNAME]:
                errors["base"] = "trunk_username_required"
            elif not user_input[CONF_TRUNK_PASSWORD]:
                errors["base"] = "trunk_password_required"
            if not errors:
                data = dict(
                    self._base_input
                    or {
                        "sip_port": int(existing.get("sip_port", VOIP_STACK_SIP_PORT)),
                        "rtp_port": int(existing.get("rtp_port", VOIP_STACK_RTP_PORT)),
                        "advertise_host": str(existing.get("advertise_host", "") or "").strip(),
                        CONF_ASSIST_INTENTS: bool(existing.get(CONF_ASSIST_INTENTS, False)),
                        CONF_DEBUG_MODE: bool(existing.get(CONF_DEBUG_MODE, False)),
                        CONF_EXPERIMENTAL_VIDEO: bool(existing.get(CONF_EXPERIMENTAL_VIDEO, False)),
                        CONF_VIDEO_TRANSCODING: bool(existing.get(CONF_VIDEO_TRANSCODING, False)),
                        CONF_VIDEO_CAMERA_SEND: bool(existing.get(CONF_VIDEO_CAMERA_SEND, False)),
                        CONF_REGISTRAR_ENABLED: bool(existing.get(CONF_REGISTRAR_ENABLED, False)),
                    }
                )
                data[CONF_TRUNK_ENABLED] = True
                data.setdefault("sip_accounts", existing.get("sip_accounts", []))
                data.setdefault(CONF_PHONEBOOK_CONTACTS, existing.get(CONF_PHONEBOOK_CONTACTS, []))
                data.update(user_input)
                current_entry, _existing = self._current_entry_data()
                if current_entry is None:
                    await self.async_set_unique_id(DOMAIN)
                    self._abort_if_unique_id_configured()
                return self._store_entry(data)
        return self.async_show_form(step_id="trunk", data_schema=schema, errors=errors)
