"""Config flow for telegraf_mqtt."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry, ConfigFlowResult
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_AUTO_DISCOVER,
    CONF_CATEGORY_OVERRIDES,
    CONF_CLEANUP_DELAY,
    CONF_DELETE_DELAY,
    CONF_DEVICE_ID_STRATEGY,
    CONF_DEVICE_NAME,
    CONF_ENABLE_CLEANUP,
    CONF_EXCLUDE_PATTERNS,
    CONF_EXPIRE_AFTER,
    CONF_FIELD_OVERRIDES,
    CONF_MIN_ACTIVE_METRICS,
    CONF_MODEL,
    CONF_SW_VERSION,
    CONF_TOPIC_PATTERN,
    DEFAULT_AUTO_DISCOVER,
    DEFAULT_CLEANUP_DELAY,
    DEFAULT_DELETE_DELAY,
    DEFAULT_DEVICE_ID_STRATEGY,
    DEFAULT_DEVICE_NAME,
    DEFAULT_ENABLE_CLEANUP,
    DEFAULT_EXPIRE_AFTER,
    DEFAULT_MIN_ACTIVE_METRICS,
    DEFAULT_TOPIC_PATTERN,
    DOMAIN,
    VALID_DEVICE_ID_STRATEGIES,
    VALID_PLATFORM_HINTS,
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


def _strategy_label(strategy: str) -> str:
    """Human-readable label for a ``device_id_strategy`` value."""
    return {
        "host": "Host tag (default)",
        "host_topic": "Host tag, then topic segment",
        "topic_only": "Topic tree only",
    }.get(strategy, strategy)


def _build_options_schema(current_options: Mapping[str, Any]) -> vol.Schema:
    """Build the Phase 10 multi-section options schema.

    Sections:
    - Discovery: enable the post-setup snoop listener and pick a
      ``device_id_strategy`` for resolving ``host`` collisions.
    - Cleanup lifecycle.
    - Filter / override: ``exclude_patterns``, ``field_overrides``,
      and the per-entity ``category_overrides`` map.
    """
    enable_cleanup = current_options.get(CONF_ENABLE_CLEANUP, DEFAULT_ENABLE_CLEANUP)
    cleanup_delay = current_options.get(CONF_CLEANUP_DELAY, DEFAULT_CLEANUP_DELAY)
    delete_delay = current_options.get(CONF_DELETE_DELAY, DEFAULT_DELETE_DELAY)
    min_active_metrics = current_options.get(CONF_MIN_ACTIVE_METRICS, DEFAULT_MIN_ACTIVE_METRICS)
    auto_discover = current_options.get(CONF_AUTO_DISCOVER, DEFAULT_AUTO_DISCOVER)
    device_id_strategy = current_options.get(CONF_DEVICE_ID_STRATEGY, DEFAULT_DEVICE_ID_STRATEGY)
    category_overrides = current_options.get(CONF_CATEGORY_OVERRIDES, {})
    non_negative_int = vol.All(int, vol.Range(min=0))

    return vol.Schema(
        {
            vol.Optional(CONF_AUTO_DISCOVER, default=auto_discover): bool,
            vol.Optional(CONF_DEVICE_ID_STRATEGY, default=device_id_strategy): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        selector.SelectOptionDict(value=v, label=_strategy_label(v)) for v in VALID_DEVICE_ID_STRATEGIES
                    ],
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Optional(CONF_EXPIRE_AFTER, default=DEFAULT_EXPIRE_AFTER): vol.All(int, vol.Range(min=1)),
            vol.Optional(CONF_ENABLE_CLEANUP, default=enable_cleanup): bool,
            vol.Optional(CONF_CLEANUP_DELAY, default=cleanup_delay): non_negative_int,
            vol.Optional(CONF_DELETE_DELAY, default=delete_delay): non_negative_int,
            vol.Optional(CONF_MIN_ACTIVE_METRICS, default=min_active_metrics): non_negative_int,
            vol.Optional(CONF_EXCLUDE_PATTERNS, default=[]): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[],
                    custom_value=True,
                    multiple=True,
                    mode=selector.SelectSelectorMode.LIST,
                )
            ),
            vol.Optional(CONF_FIELD_OVERRIDES, default={}): selector.ObjectSelector(),
            vol.Optional(CONF_CATEGORY_OVERRIDES, default=category_overrides): selector.ObjectSelector(),
        }
    )


def _clean_options(user_input: dict[str, Any]) -> dict[str, Any]:
    """Normalize user input from the options flow before persisting.

    Phase 10: ``CONF_CATEGORY_OVERRIDES`` is an ObjectSelector result;
    the user can leave it as ``{}``, so we coerce the empty form value
    back to an empty dict.
    """
    return {
        CONF_AUTO_DISCOVER: bool(user_input.get(CONF_AUTO_DISCOVER, DEFAULT_AUTO_DISCOVER)),
        CONF_DEVICE_ID_STRATEGY: str(user_input.get(CONF_DEVICE_ID_STRATEGY, DEFAULT_DEVICE_ID_STRATEGY)),
        CONF_EXPIRE_AFTER: int(user_input.get(CONF_EXPIRE_AFTER, DEFAULT_EXPIRE_AFTER)),
        CONF_ENABLE_CLEANUP: bool(user_input.get(CONF_ENABLE_CLEANUP, DEFAULT_ENABLE_CLEANUP)),
        CONF_CLEANUP_DELAY: int(user_input.get(CONF_CLEANUP_DELAY, DEFAULT_CLEANUP_DELAY)),
        CONF_DELETE_DELAY: int(user_input.get(CONF_DELETE_DELAY, DEFAULT_DELETE_DELAY)),
        CONF_MIN_ACTIVE_METRICS: int(user_input.get(CONF_MIN_ACTIVE_METRICS, DEFAULT_MIN_ACTIVE_METRICS)),
        CONF_EXCLUDE_PATTERNS: list(user_input.get(CONF_EXCLUDE_PATTERNS, [])),
        CONF_FIELD_OVERRIDES: dict(user_input.get(CONF_FIELD_OVERRIDES, {})),
        CONF_CATEGORY_OVERRIDES: dict(user_input.get(CONF_CATEGORY_OVERRIDES, {})),
    }


class TelegrafMqttOptionsFlow(config_entries.OptionsFlow):
    """Handle telegraf_mqtt options changes.

    Phase 10: the single ``init`` step grew to cover discovery settings
    and per-entity category overrides. Each option is documented in
    ``strings.json``; the schema is built by ``_build_options_schema``
    so the field set is in one place.
    """

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Show the multi-section options form."""
        if user_input is not None:
            return self.async_create_entry(title="", data=_clean_options(user_input))

        return self.async_show_form(
            step_id="init",
            data_schema=_build_options_schema(self.config_entry.options),
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

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
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

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
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
        entry: ConfigEntry = self.hass.config_entries.async_get_known_entry(self.context["entry_id"])
        return entry


# Re-export so the ``__init__`` setup can validate the runtime strategy
# at startup without importing ``.const`` separately.
__all__ = [
    "VALID_PLATFORM_HINTS",
    "TelegrafMqttConfigFlow",
    "TelegrafMqttOptionsFlow",
]
