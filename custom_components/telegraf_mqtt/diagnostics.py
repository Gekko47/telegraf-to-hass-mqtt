"""Diagnostics support for the Telegraf MQTT integration."""

from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> dict[str, Any]:
    """Return a diagnostics payload that is safe to expose to users.

    The integration keeps its runtime state under the config-entry runtime data,
    so the diagnostics output focuses on the stable, user-facing configuration
    while exposing only a minimal safe snapshot of selected runtime metadata.
    """

    data: dict[str, Any] = {
        "entry_id": entry.entry_id,
        "domain": entry.domain,
        "title": entry.title,
        "unique_id": entry.unique_id,
        "data": dict(entry.data),
        "options": dict(entry.options),
    }

    runtime_data = getattr(entry, "runtime_data", None)
    if runtime_data is not None:
        data["runtime"] = {
            "device_name": runtime_data.device_name,
            "device_id": runtime_data.device_id,
            "manufacturer": runtime_data.manufacturer,
            "model": runtime_data.model,
        }

    return data
