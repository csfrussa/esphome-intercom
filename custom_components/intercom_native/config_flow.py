"""Config flow for Intercom Native."""

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow
from homeassistant.helpers.selector import BooleanSelector, NumberSelector, NumberSelectorConfig

from .const import (
    CONF_ASSIST_INTENTS,
    CONF_SIP_TCP_ENABLED,
    CONF_SIP_UDP_ENABLED,
    DOMAIN,
    INTERCOM_RTP_PORT,
    INTERCOM_SIP_PORT,
)


def _port_selector():
    return NumberSelector(NumberSelectorConfig(min=1, max=65535, step=1, mode="box"))


class IntercomNativeConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Intercom Native."""

    VERSION = 1

    async def async_step_user(self, user_input=None):
        """Handle install/reconfigure of the single Intercom Native entry.

        SIP is the only HA call-control protocol. HA is always both a
        softphone and SIP router/B2BUA; these toggles only choose which SIP
        signaling transports HA listens on.
        """
        current_entry = next(iter(self._async_current_entries()), None)
        existing = current_entry.data if current_entry else {}
        defaults = {
            CONF_SIP_TCP_ENABLED: existing.get(CONF_SIP_TCP_ENABLED, True),
            CONF_SIP_UDP_ENABLED: existing.get(CONF_SIP_UDP_ENABLED, False),
            "sip_port": existing.get("sip_port", INTERCOM_SIP_PORT),
            "rtp_port": existing.get("rtp_port", INTERCOM_RTP_PORT),
            "advertise_host": existing.get("advertise_host", ""),
            CONF_ASSIST_INTENTS: existing.get(CONF_ASSIST_INTENTS, False),
        }
        schema = vol.Schema(
            {
                vol.Required(
                    CONF_SIP_TCP_ENABLED,
                    default=defaults[CONF_SIP_TCP_ENABLED],
                ): BooleanSelector(),
                vol.Required(
                    CONF_SIP_UDP_ENABLED,
                    default=defaults[CONF_SIP_UDP_ENABLED],
                ): BooleanSelector(),
                vol.Required("sip_port", default=defaults["sip_port"]): _port_selector(),
                vol.Required("rtp_port", default=defaults["rtp_port"]): _port_selector(),
                vol.Optional("advertise_host", default=defaults["advertise_host"]): str,
                vol.Required(
                    CONF_ASSIST_INTENTS,
                    default=defaults[CONF_ASSIST_INTENTS],
                ): BooleanSelector(),
            }
        )
        errors: dict[str, str] = {}

        if user_input is not None:
            # Number selectors hand floats; coerce to int once on the boundary.
            for k in ("sip_port", "rtp_port"):
                user_input[k] = int(user_input[k])
            user_input["advertise_host"] = (user_input.get("advertise_host") or "").strip()

            if not (user_input[CONF_SIP_TCP_ENABLED] or user_input[CONF_SIP_UDP_ENABLED]):
                errors["base"] = "sip_transport_required"
            elif current_entry is not None:
                return self.async_update_reload_and_abort(
                    current_entry,
                    data=user_input,
                    reason="reconfigure_successful",
                )
            else:
                await self.async_set_unique_id(DOMAIN)
                self._abort_if_unique_id_configured()
                return self.async_create_entry(title="Intercom Native", data=user_input)

        return self.async_show_form(step_id="user", data_schema=schema, errors=errors)
