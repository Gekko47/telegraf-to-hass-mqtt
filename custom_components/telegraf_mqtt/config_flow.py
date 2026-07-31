"""Config flow for telegraf_mqtt."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult

from .const import DOMAIN


class TelegrafMqttOptionsFlow(config_entries.OptionsFlow):
    """Handle telegraf_mqtt options changes."""

    def __init__(self, config_entry: Any) -> None:
        self.config_entry = config_entry

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Show a minimal options form for the registry controls."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional("exclude_patterns", default=[]): list,
                    vol.Optional("field_overrides", default={}): dict,
                    vol.Optional("expire_after", default=120): int,
                }
            ),
        )


class TelegrafMqttConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for telegraf_mqtt."""

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: Any) -> TelegrafMqttOptionsFlow:
        """Return the options flow handler for an existing entry."""
        return TelegrafMqttOptionsFlow(config_entry)

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Handle the initial step."""
        if user_input is not None:
            await self.async_set_unique_id(user_input["topic_pattern"])
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=user_input["device_name"],
                data=user_input,
            )

        schema = vol.Schema(
            {
                vol.Required("topic_pattern", default="telegraf/#"): str,
                vol.Required("device_name", default="Telegraf MQTT"): str,
            }
        )
        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(
                schema,
                {
                    "topic_pattern": "telegraf/#",
                    "device_name": "Telegraf MQTT",
                },
            ),
        )
