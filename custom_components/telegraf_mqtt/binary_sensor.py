"""Binary sensor platform placeholder for telegraf_mqtt."""

from __future__ import annotations

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities) -> None:
    """Set up binary sensor entities from a config entry."""
    async_add_entities([])


class TelegrafMqttBinarySensor(BinarySensorEntity):
    """Minimal binary sensor entity stub."""

    _attr_should_poll = False
