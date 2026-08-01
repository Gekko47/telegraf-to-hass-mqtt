"""Sensor platform for telegraf_mqtt."""

from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import SensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect

from .const import DOMAIN, SIGNAL_METRIC_UPDATED, SIGNAL_NEW_METRIC
from .registry import MetricRegistry


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities) -> None:
    """Set up sensor entities from a config entry."""
    runtime_data = entry.runtime_data
    registry: MetricRegistry = runtime_data.registry
    added: set[str] = set()

    def add_metric(unique_key: str) -> None:
        state = registry.get(unique_key)
        if state is None or isinstance(state.value, bool) or unique_key in added:
            return
        added.add(unique_key)
        async_add_entities([TelegrafMqttSensor(entry, unique_key)])

    for unique_key in registry.keys():
        add_metric(unique_key)

    entry.async_on_unload(
        async_dispatcher_connect(
            hass,
            SIGNAL_NEW_METRIC.format(entry_id=entry.entry_id),
            add_metric,
        )
    )


class TelegrafMqttSensor(SensorEntity):
    """Sensor backed by the Telegraf metric registry."""

    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, unique_key: str) -> None:
        self._entry = entry
        self._unique_key = unique_key
        self._refresh_descriptor_attributes()

    def _refresh_descriptor_attributes(self) -> None:
        state = self._entry.runtime_data.registry.get(self._unique_key)
        descriptor = state.descriptor
        self._attr_unique_id = f"{DOMAIN}_{self._unique_key}"
        self._attr_name = descriptor.name
        self._attr_native_unit_of_measurement = _normalize_native_unit(descriptor.native_unit)
        self._attr_device_class = descriptor.suggested_device_class
        self._attr_state_class = descriptor.suggested_state_class
        self._attr_entity_category = descriptor.entity_category
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, self._entry.runtime_data.device_id)},
            name=self._entry.runtime_data.device_name,
            manufacturer=self._entry.runtime_data.manufacturer,
            model=self._entry.runtime_data.model,
        )

    async def async_added_to_hass(self) -> None:
        """Subscribe to registry updates for this metric."""
        self.async_on_remove(
            async_dispatcher_connect(
                self.hass,
                SIGNAL_METRIC_UPDATED.format(entry_id=self._entry.entry_id),
                self._handle_metric_updated,
            )
        )

    @callback
    def _handle_metric_updated(self, unique_key: str) -> None:
        """Write HA state when this metric changes."""
        if unique_key == self._unique_key:
            self._refresh_descriptor_attributes()
            self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Return whether this metric is currently available."""
        state = self._entry.runtime_data.registry.get(self._unique_key)
        return state is not None and state.is_available

    @property
    def native_value(self) -> Any:
        """Return the current registry value."""
        state = self._entry.runtime_data.registry.get(self._unique_key)
        if state is None:
            return None
        return state.value

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Expose Telegraf identity details for troubleshooting."""
        state = self._entry.runtime_data.registry.get(self._unique_key)
        if state is None:
            return None
        descriptor = state.descriptor
        return {
            "measurement": descriptor.measurement,
            "field": descriptor.field,
            "tags": dict(descriptor.tags),
            "timestamp": descriptor.timestamp,
        }


def _normalize_native_unit(native_unit: str | None) -> str | None:
    if native_unit == "°C":
        return UnitOfTemperature.CELSIUS
    return native_unit
