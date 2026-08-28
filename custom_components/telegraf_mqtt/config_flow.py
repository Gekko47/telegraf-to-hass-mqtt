"""Config flow for telegraf_mqtt."""

from __future__ import annotations

from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import callback
from homeassistant.data_entry_flow import FlowResult

from .const import (
    CONF_CLEANUP_DELAY,
    CONF_DELETE_DELAY,
    CONF_DEVICE_NAME,
    CONF_ENABLE_CLEANUP,
    CONF_EXCLUDE_PATTERNS,
    CONF_EXPIRE_AFTER,
    CONF_FIELD_OVERRIDES,
    CONF_MIN_ACTIVE_METRICS,
    CONF_MODEL,
    CONF_SW_VERSION,
    CONF_TOPIC_PATTERN,
    DEFAULT_CLEANUP_DELAY,
    DEFAULT_DELETE_DELAY,
    DEFAULT_DEVICE_NAME,
    DEFAULT_ENABLE_CLEANUP,
    DEFAULT_EXPIRE_AFTER,
    DEFAULT_MIN_ACTIVE_METRICS,
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


def _config_schema(defaults: dict[str, Any]) -> vol.Schema:
    """Build the config / reconfigure schema with the given defaults."""
    return vol.Schema(
        {
            vol.Required(CONF_TOPIC_PATTERN, default=defaults[CONF_TOPIC_PATTERN]): str,
            vol.Required(CONF_DEVICE_NAME, default=defaults[CONF_DEVICE_NAME]): str,
            vol.Optional(CONF_MODEL, default=defaults.get(CONF_MODEL, "")): str,
            vol.Optional("manufacturer", default=defaults.get("manufacturer", "")): str,
            vol.Optional(CONF_SW_VERSION, default=defaults.get(CONF_SW_VERSION, "")): str,
        }
    )


def _clean(value: Any) -> str | None:
    """Return None for empty/whitespace strings, otherwise the stripped value."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


class TelegrafMqttOptionsFlow(config_entries.OptionsFlow):
    """Handle telegraf_mqtt options changes."""

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Show a minimal options form for the registry controls."""
        if user_input is not None:
            return self.async_create_entry(title="", data=user_input)

        current_options = self.config_entry.options
        enable_cleanup = current_options.get(CONF_ENABLE_CLEANUP, DEFAULT_ENABLE_CLEANUP)
        cleanup_delay = current_options.get(CONF_CLEANUP_DELAY, DEFAULT_CLEANUP_DELAY)
        delete_delay = current_options.get(CONF_DELETE_DELAY, DEFAULT_DELETE_DELAY)
        min_active_metrics = current_options.get(
            CONF_MIN_ACTIVE_METRICS, DEFAULT_MIN_ACTIVE_METRICS
        )
        non_negative_int = vol.All(int, vol.Range(min=0))

        return self.async_show_form(
            step_id="init",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_EXCLUDE_PATTERNS, default=[]): list,
                    vol.Optional(CONF_FIELD_OVERRIDES, default={}): dict,
                    vol.Optional(CONF_EXPIRE_AFTER, default=DEFAULT_EXPIRE_AFTER): int,
                    vol.Optional(CONF_ENABLE_CLEANUP, default=enable_cleanup): bool,
                    vol.Optional(CONF_CLEANUP_DELAY, default=cleanup_delay): non_negative_int,
                    vol.Optional(CONF_DELETE_DELAY, default=delete_delay): non_negative_int,
                    vol.Optional(
                        CONF_MIN_ACTIVE_METRICS, default=min_active_metrics
                    ): non_negative_int,
                }
            ),
        )


class TelegrafMqttConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for telegraf_mqtt.

    Phase 9: this flow also handles reconfigure (topic pattern, device
    name, manufacturer, model, sw_version). A reconfigure that changes
    the topic pattern triggers a reload of the config entry, which is
    the HA-blessed way to swap the MQTT subscription. The previous
    subscription is unsubscribed cleanly by ``async_unload_entry``.
    """

    VERSION = 1

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: Any) -> TelegrafMqttOptionsFlow:
        """Return the options flow handler for an existing entry."""
        return TelegrafMqttOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> FlowResult:
        """Handle the initial step."""
        if user_input is not None:
            errors = self._validate(user_input)
            if errors:
                return self.async_show_form(
                    step_id="user",
                    data_schema=_config_schema(
                        {
                            CONF_TOPIC_PATTERN: user_input.get(CONF_TOPIC_PATTERN, DEFAULT_TOPIC_PATTERN),
                            CONF_DEVICE_NAME: user_input.get(CONF_DEVICE_NAME, DEFAULT_DEVICE_NAME),
                            CONF_MODEL: user_input.get(CONF_MODEL, ""),
                            "manufacturer": user_input.get("manufacturer", ""),
                            CONF_SW_VERSION: user_input.get(CONF_SW_VERSION, ""),
                        }
                    ),
                    errors=errors,
                )

            topic_pattern = user_input[CONF_TOPIC_PATTERN]
            device_name = _clean(user_input[CONF_DEVICE_NAME])
            assert device_name is not None  # guarded by _validate above
            await self.async_set_unique_id(topic_pattern)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=device_name,
                data={
                    CONF_TOPIC_PATTERN: topic_pattern,
                    CONF_DEVICE_NAME: device_name,
                    "manufacturer": _clean(user_input.get("manufacturer")),
                    CONF_MODEL: _clean(user_input.get(CONF_MODEL)),
                    CONF_SW_VERSION: _clean(user_input.get(CONF_SW_VERSION)),
                },
            )

        return self.async_show_form(
            step_id="user",
            data_schema=_config_schema(
                {
                    CONF_TOPIC_PATTERN: DEFAULT_TOPIC_PATTERN,
                    CONF_DEVICE_NAME: _default_device_name(DEFAULT_TOPIC_PATTERN),
                    CONF_MODEL: "",
                    "manufacturer": "",
                    CONF_SW_VERSION: "",
                }
            ),
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> FlowResult:
        """Phase 9: handle the reconfigure step.

        A reconfigure that changes the topic pattern triggers a reload of
        the config entry, which swaps the MQTT subscription cleanly via
        ``async_unload_entry`` + ``async_setup_entry``. Metadata-only
        reconfigures (device_name, manufacturer, model, sw_version) also
        reload because the DeviceInfo carried on every entity is built
        from ``entry.data`` -- a reload is the simplest way to refresh
        the visible device metadata.
        """
        entry = self._get_entry()
        if user_input is not None:
            errors = self._validate(user_input)
            if errors:
                return self.async_show_form(
                    step_id="reconfigure",
                    data_schema=_config_schema(
                        {
                            CONF_TOPIC_PATTERN: user_input.get(CONF_TOPIC_PATTERN, ""),
                            CONF_DEVICE_NAME: user_input.get(CONF_DEVICE_NAME, ""),
                            CONF_MODEL: user_input.get(CONF_MODEL, ""),
                            "manufacturer": user_input.get("manufacturer", ""),
                            CONF_SW_VERSION: user_input.get(CONF_SW_VERSION, ""),
                        }
                    ),
                    errors=errors,
                )

            topic = user_input[CONF_TOPIC_PATTERN]
            device_name = _clean(user_input[CONF_DEVICE_NAME])
            assert device_name is not None  # guarded by _validate above
            await self.async_set_unique_id(topic)
            self._abort_if_unique_id_configured()
            return self.async_update_reload_and_abort(
                entry,
                data_updates={
                    CONF_TOPIC_PATTERN: topic,
                    CONF_DEVICE_NAME: device_name,
                    "manufacturer": _clean(user_input.get("manufacturer")),
                    CONF_MODEL: _clean(user_input.get(CONF_MODEL)),
                    CONF_SW_VERSION: _clean(user_input.get(CONF_SW_VERSION)),
                },
            )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_config_schema(
                {
                    CONF_TOPIC_PATTERN: entry.data.get(CONF_TOPIC_PATTERN, DEFAULT_TOPIC_PATTERN),
                    CONF_DEVICE_NAME: entry.data.get(CONF_DEVICE_NAME, DEFAULT_DEVICE_NAME),
                    CONF_MODEL: entry.data.get(CONF_MODEL) or "",
                    "manufacturer": entry.data.get("manufacturer") or "",
                    CONF_SW_VERSION: entry.data.get(CONF_SW_VERSION) or "",
                }
            ),
        )

    def _validate(self, user_input: dict[str, Any]) -> dict[str, str]:
        """Return a per-field error dict, or {} when the input is valid."""
        errors: dict[str, str] = {}
        topic = user_input.get(CONF_TOPIC_PATTERN, "")
        if not _valid_subscription_topic(topic):
            errors[CONF_TOPIC_PATTERN] = "invalid_topic"
        # Normalize CONF_DEVICE_NAME with _clean so whitespace-only values
        # are rejected the same way as an empty string, and the stripped
        # value is the one stored on the entry (see async_step_user and
        # async_step_reconfigure for the matching create-entry path).
        if _clean(user_input.get(CONF_DEVICE_NAME)) is None:
            errors[CONF_DEVICE_NAME] = "required"
        return errors

    def _get_entry(self) -> ConfigEntry:
        """Return the entry being reconfigured."""
        entry: ConfigEntry = self.hass.config_entries.async_get_known_entry(
            self.context["entry_id"]
        )
        return entry
