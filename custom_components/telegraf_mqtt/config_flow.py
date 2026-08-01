"""Config flow for telegraf_mqtt."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult

from .const import (
    CONF_DEVICE_NAME,
    CONF_EXCLUDE_PATTERNS,
    CONF_EXPIRE_AFTER,
    CONF_FIELD_OVERRIDES,
    CONF_TOPIC_PATTERN,
    DEFAULT_DEVICE_NAME,
    DEFAULT_EXPIRE_AFTER,
    DEFAULT_TOPIC_PATTERN,
    DOMAIN,
)


def _valid_subscription_topic(topic: str) -> bool:
    """Return whether a string is a syntactically valid MQTT subscription topic."""
    if not topic:
        return False
    parts = topic.split("/")
    for index, part in enumerate(parts):
        if "#" in part and (part != "#" or index != len(parts) - 1):
            return False
        if "+" in part and part != "+":
            return False
    return True


def _default_device_name(topic: str) -> str:
    """Build a default device name from the first static topic segment."""
    for part in topic.split("/"):
        if part and part not in {"+", "#"}:
            return part.replace("_", " ").replace("-", " ").title()
    return DEFAULT_DEVICE_NAME


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
                    vol.Optional(CONF_EXCLUDE_PATTERNS, default=[]): list,
                    vol.Optional(CONF_FIELD_OVERRIDES, default={}): dict,
                    vol.Optional(CONF_EXPIRE_AFTER, default=DEFAULT_EXPIRE_AFTER): int,
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
            errors: dict[str, str] = {}
            topic_pattern = user_input[CONF_TOPIC_PATTERN]
            if not _valid_subscription_topic(topic_pattern):
                errors[CONF_TOPIC_PATTERN] = "invalid_topic"
            if not user_input[CONF_DEVICE_NAME]:
                errors[CONF_DEVICE_NAME] = "required"
            if errors:
                return self.async_show_form(
                    step_id="user",
                    data_schema=_schema(topic_pattern),
                    errors=errors,
                )

            await self.async_set_unique_id(topic_pattern)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=user_input[CONF_DEVICE_NAME],
                data=user_input,
            )

        return self.async_show_form(
            step_id="user",
            data_schema=self.add_suggested_values_to_schema(_schema(DEFAULT_TOPIC_PATTERN), {}),
        )


def _schema(topic_pattern: str) -> vol.Schema:
    return vol.Schema(
        {
            vol.Required(CONF_TOPIC_PATTERN, default=topic_pattern): str,
            vol.Required(CONF_DEVICE_NAME, default=_default_device_name(topic_pattern)): str,
            vol.Optional("manufacturer"): str,
            vol.Optional("model"): str,
        }
    )
