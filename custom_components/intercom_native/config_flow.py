"""Config flow for Intercom Native."""

import voluptuous as vol

from homeassistant.config_entries import ConfigFlow
from homeassistant.helpers.selector import BooleanSelector, NumberSelector, NumberSelectorConfig

from .const import (
    CONF_ASSIST_INTENTS,
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

        SIP is the only HA call-control transport. HA listens as a SIP
        softphone/B2BUA; ESP devices choose SIP UDP or SIP TCP in their own
        `intercom_api.protocol` option.
        """
        current_entry = next(iter(self._async_current_entries()), None)
        existing = current_entry.data if current_entry else {}
        defaults = {
            "use_sip": existing.get("use_sip", True),
            "sip_port": existing.get("sip_port", INTERCOM_SIP_PORT),
            "rtp_port": existing.get("rtp_port", INTERCOM_RTP_PORT),
            "advertise_host": existing.get("advertise_host", ""),
            CONF_ASSIST_INTENTS: existing.get(CONF_ASSIST_INTENTS, False),
        }
        schema = vol.Schema(
            {
                vol.Required("use_sip", default=defaults["use_sip"]): BooleanSelector(),
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

            if not user_input["use_sip"]:
                errors["base"] = "sip_required"
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
