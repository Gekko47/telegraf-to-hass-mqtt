"""The telegraf_mqtt integration."""

from __future__ import annotations

try:
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.const import Platform
    from homeassistant.core import HomeAssistant
except ModuleNotFoundError:  # pragma: no cover - exercised only in unit-test import isolation
    ConfigEntry = object
    Platform = None
    HomeAssistant = object

from .const import DOMAIN

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR] if Platform is not None else []


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up telegraf_mqtt from a config entry."""
    entry.runtime_data = {}
    if Platform is not None:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a telegraf_mqtt config entry."""
    if Platform is None:
        return True
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
