"""Binary sensor platform for telegraf_mqtt."""

from __future__ import annotations

from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect
from homeassistant.helpers.entity import EntityCategory

from .const import DOMAIN, SIGNAL_METRIC_UPDATED, SIGNAL_NEW_METRIC


def _entity_category(value: str | None) -> EntityCategory | None:
    """Coerce a resolved category string into HA's enum at the platform boundary."""
    return EntityCategory(value) if value else None


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities) -> None:
    """Set up binary sensor entities from a config entry."""
    manager = entry.runtime_data.manager
    added: set[str] = set()

    @callback
    def add_metric(metric_key: str) -> None:
        state = manager.get_metric(metric_key)
        if state is None or not isinstance(state.value, bool) or metric_key in added:
            return
        added.add(metric_key)
        async_add_entities([TelegrafMqttBinarySensor(entry, metric_key)])

    for metric_key in manager:
        add_metric(metric_key)

    entry.async_on_unload(
        async_dispatcher_connect(
            hass,
            SIGNAL_NEW_METRIC.format(entry_id=entry.entry_id),
            add_metric,
        )
    )


class TelegrafMqttBinarySensor(BinarySensorEntity):
    """Binary sensor backed by one Telegraf metric on one discovered device."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(self, entry: ConfigEntry, metric_key: str) -> None:
        self._entry = entry
        self._metric_key = metric_key
        self._refresh_descriptor_attributes()

    @property
    def _manager(self):
        return self._entry.runtime_data.manager

    def _refresh_descriptor_attributes(self) -> None:
        state = self._manager.get_metric(self._metric_key)
        if state is None:
            return
        descriptor = state.descriptor
        self._attr_unique_id = f"{DOMAIN}_{state.device_id}_{descriptor.unique_key}"
        self._attr_name = descriptor.name
        self._attr_entity_category = _entity_category(descriptor.entity_category)
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, state.device_id)},
            name=state.device_name,
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
    def _handle_metric_updated(self, metric_key: str) -> None:
        """Write HA state when this metric changes."""
        if metric_key == self._metric_key:
            self._refresh_descriptor_attributes()
            self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Return whether this metric is currently available."""
        state = self._manager.get_metric(self._metric_key)
        return state is not None and state.is_available

    @property
    def is_on(self) -> bool | None:
        """Return the current boolean metric state."""
        state = self._manager.get_metric(self._metric_key)
        if state is None:
            return None
        return state.value if isinstance(state.value, bool) else None

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Expose Telegraf identity details for troubleshooting."""
        state = self._manager.get_metric(self._metric_key)
        if state is None:
            return None
        descriptor = state.descriptor
        return {
            "measurement": descriptor.measurement,
            "field": descriptor.field,
            "tags": dict(descriptor.tags),
            "timestamp": descriptor.timestamp,
        }
