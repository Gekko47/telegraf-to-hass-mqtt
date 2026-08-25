"""Platform-entity unit tests for sensor.py / binary_sensor.py branch coverage.

Stub-based (harness-free) per AGENTS.md's platform-independence goal. Modules are
re-imported fresh against the stubs and dropped from sys.modules afterwards so
execution order can never pollute the real-HA harness tests.
"""

from __future__ import annotations

import asyncio
import importlib
import sys
import types
from dataclasses import dataclass, field

import pytest

from custom_components.telegraf_mqtt.const import (
    SIGNAL_METRIC_UPDATED,
    SIGNAL_NEW_METRIC,
)
from custom_components.telegraf_mqtt.models import MetricDescriptor
from custom_components.telegraf_mqtt.registry import DeviceManager

ENTRY_ID = "entry-1"
_TARGETS_HOLDER: dict[str, list] = {"targets": {}}


def _install_platform_stubs(monkeypatch) -> dict[str, list]:
    """Minimal HA stand-ins; records dispatcher targets by signal for later dispatch."""
    components = types.ModuleType("homeassistant.components")
    sensor = types.ModuleType("homeassistant.components.sensor")
    binary_sensor = types.ModuleType("homeassistant.components.binary_sensor")
    config_entries = types.ModuleType("homeassistant.config_entries")
    const = types.ModuleType("homeassistant.const")
    core = types.ModuleType("homeassistant.core")
    device_registry = types.ModuleType("homeassistant.helpers.device_registry")
    dispatcher = types.ModuleType("homeassistant.helpers.dispatcher")
    helpers = types.ModuleType("homeassistant.helpers")

    class StubEntity:
        def __init__(self) -> None:
            self.write_count = 0

        def async_write_ha_state(self) -> None:
            self.write_count += 1

        def async_on_remove(self, remove_callback) -> None:
            self.remove_callback = remove_callback

    class UnitOfTemperature:
        CELSIUS = "°C"

    def callback(func):
        func.__hass_callback__ = True
        return func

    targets = _TARGETS_HOLDER["targets"]

    def async_dispatcher_connect(_hass, signal, target):
        targets.setdefault(signal, []).append(target)
        return lambda: None

    sensor.SensorEntity = StubEntity
    binary_sensor.BinarySensorEntity = StubEntity
    config_entries.ConfigEntry = object
    const.UnitOfTemperature = UnitOfTemperature
    core.HomeAssistant = object
    core.callback = callback
    device_registry.DeviceInfo = dict
    dispatcher.async_dispatcher_connect = async_dispatcher_connect

    monkeypatch.setitem(sys.modules, "homeassistant.components", components)
    monkeypatch.setitem(sys.modules, "homeassistant.components.sensor", sensor)
    monkeypatch.setitem(sys.modules, "homeassistant.components.binary_sensor", binary_sensor)
    monkeypatch.setitem(sys.modules, "homeassistant.config_entries", config_entries)
    monkeypatch.setitem(sys.modules, "homeassistant.const", const)
    monkeypatch.setitem(sys.modules, "homeassistant.core", core)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers", helpers)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.device_registry", device_registry)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.dispatcher", dispatcher)
    return targets


@dataclass
class RuntimeData:
    manager: DeviceManager
    manufacturer: str | None = None
    model: str | None = None


@dataclass
class Entry:
    runtime_data: RuntimeData
    entry_id: str = ENTRY_ID
    unload_callbacks: list = field(default_factory=list)

    def async_on_unload(self, callback) -> None:
        self.unload_callbacks.append(callback)


def _descriptor(unique_key: str, value, **overrides) -> MetricDescriptor:
    kwargs = {
        "unique_key": unique_key,
        "measurement": "net",
        "tags": {"host": "host1"},
        "field": unique_key,
        "value": value,
        "timestamp": 1721664000,
        "name": unique_key.replace("_", " ").title(),
        "native_unit": None,
        "suggested_device_class": None,
        "suggested_state_class": None,
        "entity_category": None,
    }
    kwargs.update(overrides)
    return MetricDescriptor(**kwargs)


def _fresh_module(name: str):
    sys.modules.pop(f"custom_components.telegraf_mqtt.{name}", None)
    try:
        return importlib.import_module(f"custom_components.telegraf_mqtt.{name}")
    finally:
        sys.modules.pop(f"custom_components.telegraf_mqtt.{name}", None)


def _setup_platform(module, manager):
    added: list = []
    entry = Entry(runtime_data=RuntimeData(manager=manager))
    asyncio.run(module.async_setup_entry(object(), entry, added.extend))
    return added, _TARGETS_HOLDER["targets"], entry


@pytest.fixture()
def platform_env(monkeypatch):
    _install_platform_stubs(monkeypatch)
    yield


# --- sensor.py --------------------------------------------------------------


def test_sensor_setup_adds_existing_metrics_and_ignores_unknown_signals(platform_env) -> None:
    sensor_module = _fresh_module("sensor")
    manager = DeviceManager()
    registry = manager.get_or_create_registry("host1", "host1")
    registry.update(_descriptor("bytes_recv", 10))

    added, targets, _entry = _setup_platform(sensor_module, manager)

    assert len(added) == 1  # initial-keys loop
    assert added[0]._attr_unique_id == "telegraf_mqtt_host1_bytes_recv"

    new_metric_signal = SIGNAL_NEW_METRIC.format(entry_id=ENTRY_ID)
    assert targets[new_metric_signal], "setup must subscribe to the new-metric signal"

    # Unknown metric key: guard returns without adding (state is None).
    for target in targets[new_metric_signal]:
        target("bogus:key")
    assert len(added) == 1

    # Known-but-already-added key: dedup guard returns as well.
    for target in targets[new_metric_signal]:
        target("host1:bytes_recv")
    assert len(added) == 1


def test_sensor_entity_guards_when_metric_state_missing(platform_env) -> None:
    sensor_module = _fresh_module("sensor")
    manager = DeviceManager()
    manager.get_or_create_registry("host1", "host1")
    entry = Entry(RuntimeData(manager=manager))

    entity = sensor_module.TelegrafMqttSensor(entry, "zz:none")

    assert entity.available is False
    assert entity.native_value is None
    assert entity.extra_state_attributes is None


def test_celsius_descriptors_use_the_ha_temperature_unit(platform_env) -> None:
    sensor_module = _fresh_module("sensor")
    manager = DeviceManager()
    registry = manager.get_or_create_registry("host1", "host1")
    registry.update(_descriptor("temp_input", 52.0, native_unit="°C"))

    added, _targets, _entry = _setup_platform(sensor_module, manager)

    assert added[0]._attr_native_unit_of_measurement == "°C"


# --- binary_sensor.py -------------------------------------------------------


def test_binary_sensor_routes_only_booleans_and_reflects_state(platform_env) -> None:
    binary_module = _fresh_module("binary_sensor")
    manager = DeviceManager()
    registry = manager.get_or_create_registry("host1", "host1")
    registry.update(_descriptor("link_up", True))
    registry.update(_descriptor("bytes_recv", 10))

    added, targets, entry = _setup_platform(binary_module, manager)

    # Only the boolean metric becomes an entity.
    assert len(added) == 1
    entity = added[0]
    assert entity._attr_unique_id == "telegraf_mqtt_host1_link_up"
    assert entity.is_on is True
    attributes = entity.extra_state_attributes
    assert attributes["measurement"] == "net"
    assert attributes["field"] == "link_up"
    assert attributes["timestamp"] == 1721664000
    assert isinstance(attributes["tags"], dict)

    # Subscribe the entity (as HA would on add) so its update handler registers.
    entity.hass = object()
    asyncio.run(entity.async_added_to_hass())
    assert callable(entity.remove_callback)

    # Update handler: matching key refreshes and writes state.
    # (Stub entities don't run super().__init__, so seed the counter explicitly.)
    entity.write_count = 0
    registry.update(_descriptor("link_up", False))
    updated_signal = SIGNAL_METRIC_UPDATED.format(entry_id=ENTRY_ID)
    for target in targets[updated_signal]:
        target("host1:link_up")
    assert entity.is_on is False
    assert entity.write_count == 1

    # Non-matching key: handler ignores it entirely.
    for target in targets[updated_signal]:
        target("host1:other_key")
    assert entity.write_count == 1

    # A non-boolean metric never reports an on/off value.
    boolless = binary_module.TelegrafMqttBinarySensor(entry, "host1:bytes_recv")
    assert boolless.is_on is None
    assert boolless.available is True


def test_binary_sensor_refresh_guard_without_state(platform_env) -> None:
    binary_module = _fresh_module("binary_sensor")
    manager = DeviceManager()
    manager.get_or_create_registry("host1", "host1")
    entry = Entry(RuntimeData(manager=manager))

    entity = binary_module.TelegrafMqttBinarySensor(entry, "zz:none")

    assert entity.available is False
    assert entity.is_on is None
    assert entity.extra_state_attributes is None
