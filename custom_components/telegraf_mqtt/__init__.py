"""The telegraf_mqtt integration."""

from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

try:
    from homeassistant.components import mqtt
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.const import Platform
    from homeassistant.core import HomeAssistant
    from homeassistant.helpers.dispatcher import async_dispatcher_send
    from homeassistant.helpers.event import async_track_time_interval
except ModuleNotFoundError:  # pragma: no cover - exercised only in unit-test import isolation
    ConfigEntry = object
    Platform = None
    HomeAssistant = object
    mqtt = None
    async_dispatcher_send = None
    async_track_time_interval = None

from .const import (
    CONF_EXCLUDE_PATTERNS,
    CONF_EXPIRE_AFTER,
    CONF_FIELD_OVERRIDES,
    CONF_TOPIC_PATTERN,
    DEFAULT_EXPIRE_AFTER,
    SIGNAL_METRIC_UPDATED,
    SIGNAL_NEW_DEVICE,
    SIGNAL_NEW_METRIC,
)
from .parser import TelegrafParser
from .registry import DeviceManager

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR] if Platform is not None else []


@dataclass
class TelegrafMqttRuntimeData:
    """Runtime state for a config entry."""

    manager: DeviceManager
    parser: TelegrafParser
    manufacturer: str | None
    model: str | None
    unsubscribe: Callable[[], None] | None = None
    cancel_expiry: Callable[[], None] | None = None


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up telegraf_mqtt from a config entry."""
    options = _options_from_entry(entry)
    parser = TelegrafParser()
    manager = DeviceManager(
        expire_after=options.expire_after,
        exclude_patterns=options.exclude_patterns,
        field_overrides=options.field_overrides,
        parser=parser,
    )
    entry.runtime_data = TelegrafMqttRuntimeData(
        manager=manager,
        parser=parser,
        manufacturer=entry.data.get("manufacturer"),
        model=entry.data.get("model"),
    )

    if Platform is not None:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    if mqtt is not None:
        topic_pattern = entry.data[CONF_TOPIC_PATTERN]

        manager.set_callbacks(
            on_write=lambda metric_key, available, value: _dispatch_metric_updated(hass, entry, metric_key),
            on_discovered=lambda metric_key: _dispatch_new_metric(hass, entry, metric_key),
            on_new_device=_make_new_device_callback(hass, entry),
        )

        async def message_received(message: Any) -> None:
            manager.process_message(message.topic, message.payload)

        entry.runtime_data.unsubscribe = await mqtt.async_subscribe(hass, topic_pattern, message_received)
        _LOGGER.info("Subscribed to Telegraf MQTT topic pattern %s", topic_pattern)

    if async_track_time_interval is not None:
        _schedule_expiry_check(hass, entry)

    if hasattr(entry, "async_on_unload") and hasattr(entry, "add_update_listener"):
        entry.async_on_unload(entry.add_update_listener(_async_options_updated))

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
    if unload_ok and runtime_data.cancel_expiry is not None:
        runtime_data.cancel_expiry()
        runtime_data.cancel_expiry = None
    return unload_ok


@dataclass(frozen=True)
class TelegrafMqttOptions:
    """Normalized runtime options."""

    expire_after: int
    exclude_patterns: tuple[str, ...]
    field_overrides: dict[str, dict[str, Any]]


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Apply options live without reloading the config entry."""
    options = _options_from_entry(entry)
    entry.runtime_data.manager.apply_options(
        expire_after=options.expire_after,
        exclude_patterns=options.exclude_patterns,
        field_overrides=options.field_overrides,
        on_write=lambda unique_key, available, value: _dispatch_metric_updated(hass, entry, unique_key),
    )
    _schedule_expiry_check(hass, entry)


def _options_from_entry(entry: ConfigEntry) -> TelegrafMqttOptions:
    """Normalize config entry options into registry settings."""
    raw_options = getattr(entry, "options", {}) or {}
    return TelegrafMqttOptions(
        expire_after=max(1, int(raw_options.get(CONF_EXPIRE_AFTER, DEFAULT_EXPIRE_AFTER))),
        exclude_patterns=tuple(str(pattern) for pattern in raw_options.get(CONF_EXCLUDE_PATTERNS, [])),
        field_overrides=dict(raw_options.get(CONF_FIELD_OVERRIDES, {})),
    )


def _schedule_expiry_check(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Schedule or replace the periodic registry expiry check."""
    if async_track_time_interval is None:
        return

    runtime_data = entry.runtime_data
    if runtime_data.cancel_expiry is not None:
        runtime_data.cancel_expiry()

    interval_seconds = max(1, min(_options_from_entry(entry).expire_after, 30))

    def check_expiry(now: Any) -> None:
        runtime_data.manager.check_expiry(
            on_write=lambda metric_key, available, value: _dispatch_metric_updated(hass, entry, metric_key)
        )
        runtime_data.manager.cleanup(
            on_write=lambda metric_key, available, value: _dispatch_metric_updated(hass, entry, metric_key)
        )

    runtime_data.cancel_expiry = async_track_time_interval(hass, check_expiry, timedelta(seconds=interval_seconds))


def _dispatch_metric_updated(hass: HomeAssistant, entry: ConfigEntry, unique_key: str) -> None:
    """Dispatch a registry update signal for one metric."""
    if async_dispatcher_send is not None:
        async_dispatcher_send(
            hass,
            SIGNAL_METRIC_UPDATED.format(entry_id=entry.entry_id),
            unique_key,
        )


def _dispatch_new_metric(hass: HomeAssistant, entry: ConfigEntry, metric_key: str) -> None:
    """Dispatch the new-metric signal so platforms add an entity for it."""
    if async_dispatcher_send is not None:
        async_dispatcher_send(
            hass,
            SIGNAL_NEW_METRIC.format(entry_id=entry.entry_id),
            metric_key,
        )


def _make_new_device_callback(hass: HomeAssistant, entry: ConfigEntry):
    """Build the device-discovery callback that announces a newly seen host."""

    def on_new_device(device_id: str, device_name: str) -> None:
        _LOGGER.info("Discovered new Telegraf device %s (%s)", device_name, device_id)
        if async_dispatcher_send is not None:
            async_dispatcher_send(
                hass,
                SIGNAL_NEW_DEVICE.format(entry_id=entry.entry_id),
                device_id,
                device_name,
            )

    return on_new_device
