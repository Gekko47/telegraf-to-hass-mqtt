"""Sensor platform placeholder for telegraf_mqtt."""

from __future__ import annotations

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities) -> None:
    """Set up sensor entities from a config entry."""
    async_add_entities([])


class TelegrafMqttSensor(SensorEntity):
    """Minimal sensor entity stub."""

    _attr_should_poll = False
