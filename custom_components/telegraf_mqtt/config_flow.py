"""Config flow for telegraf_mqtt."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.data_entry_flow import FlowResult

from .const import DOMAIN


class TelegrafMqttConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for telegraf_mqtt."""

    VERSION = 1

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
