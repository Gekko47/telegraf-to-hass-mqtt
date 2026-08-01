"""The telegraf_mqtt integration."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable

try:
    from homeassistant.components import mqtt
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.const import Platform
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.dispatcher import async_dispatcher_send
except ModuleNotFoundError:  # pragma: no cover - exercised only in unit-test import isolation
    ConfigEntry = object
    Platform = None
    HomeAssistant = object
    mqtt = None
    async_dispatcher_send = None

from .const import CONF_DEVICE_NAME, CONF_TOPIC_PATTERN, DOMAIN, SIGNAL_METRIC_UPDATED, SIGNAL_NEW_METRIC
from .parser import TelegrafParser
from .registry import MetricRegistry

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR] if Platform is not None else []


@dataclass
class TelegrafMqttRuntimeData:
    """Runtime state for a config entry."""

    device_name: str
    device_id: str
    manufacturer: str | None
    model: str | None
    registry: MetricRegistry
    parser: TelegrafParser
    unsubscribe: Callable[[], None] | None = None


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up telegraf_mqtt from a config entry."""
    registry = MetricRegistry()
    parser = TelegrafParser()
    device_name = entry.data[CONF_DEVICE_NAME]
    entry.runtime_data = TelegrafMqttRuntimeData(
        device_name=device_name,
        device_id=entry.entry_id,
        manufacturer=entry.data.get("manufacturer"),
        model=entry.data.get("model"),
        registry=registry,
        parser=parser,
    )

    if Platform is not None:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    if mqtt is not None:
        topic_pattern = entry.data[CONF_TOPIC_PATTERN]

        async def message_received(message: Any) -> None:
            for descriptor in parser.parse(message.payload):
                registry.update(
                    descriptor,
                    on_discovered=lambda unique_key: async_dispatcher_send(
                        hass,
                        SIGNAL_NEW_METRIC.format(entry_id=entry.entry_id),
                        unique_key,
                    ),
                    on_write=lambda unique_key, available, value: async_dispatcher_send(
                        hass,
                        SIGNAL_METRIC_UPDATED.format(entry_id=entry.entry_id),
                        unique_key,
                    ),
                )

        entry.runtime_data.unsubscribe = await mqtt.async_subscribe(hass, topic_pattern, message_received)
        _LOGGER.info("Subscribed to Telegraf MQTT topic pattern %s", topic_pattern)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a telegraf_mqtt config entry."""
    if Platform is None:
        return True
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    runtime_data = entry.runtime_data
    if unload_ok and runtime_data.unsubscribe is not None:
        runtime_data.unsubscribe()
        runtime_data.unsubscribe = None
    return unload_ok
