from __future__ import annotations

import enum
import importlib
import sys
import types
from dataclasses import dataclass

from custom_components.telegraf_mqtt.models import MetricDescriptor
from custom_components.telegraf_mqtt.registry import DeviceManager


def _install_sensor_homeassistant_stubs(monkeypatch) -> None:
    components = types.ModuleType("homeassistant.components")
    sensor = types.ModuleType("homeassistant.components.sensor")
    config_entries = types.ModuleType("homeassistant.config_entries")
    const = types.ModuleType("homeassistant.const")
    core = types.ModuleType("homeassistant.core")
    device_registry = types.ModuleType("homeassistant.helpers.device_registry")
    dispatcher = types.ModuleType("homeassistant.helpers.dispatcher")
    entity_platform = types.ModuleType("homeassistant.helpers.entity_platform")
    helpers = types.ModuleType("homeassistant.helpers")

    class SensorEntity:
        def __init__(self) -> None:
            self.write_count = 0

        def async_write_ha_state(self) -> None:
            self.write_count = getattr(self, "write_count", 0) + 1

        def async_on_remove(self, callback) -> None:
            self.remove_callback = callback

    class ConfigEntry:
        pass

    class HomeAssistant:
        pass

    class UnitOfTemperature:
        CELSIUS = "°C"

    class StubEntityCategory(enum.StrEnum):
        CONFIG = "config"
        DIAGNOSTIC = "diagnostic"

    class SensorDeviceClass(enum.StrEnum):
        TEMPERATURE = "temperature"
        POWER = "power"
        ENERGY = "energy"

    class SensorStateClass(enum.StrEnum):
        MEASUREMENT = "measurement"
        TOTAL = "total"
        TOTAL_INCREASING = "total_increasing"

    def callback(func):
        return func

    def async_dispatcher_connect(hass, signal, target):
        return lambda: None

    def async_dispatcher_send(hass, signal, *args):
        return None

    sensor.SensorEntity = SensorEntity
    sensor.SensorDeviceClass = SensorDeviceClass
    sensor.SensorStateClass = SensorStateClass
    config_entries.ConfigEntry = ConfigEntry
    const.UnitOfTemperature = UnitOfTemperature
    const.EntityCategory = StubEntityCategory
    core.HomeAssistant = HomeAssistant
    core.callback = callback
    device_registry.DeviceInfo = dict
    dispatcher.async_dispatcher_connect = async_dispatcher_connect
    dispatcher.async_dispatcher_send = async_dispatcher_send
    entity_platform.AddEntitiesCallback = object
    entity_helpers = types.ModuleType("homeassistant.helpers.entity")
    entity_helpers.EntityCategory = StubEntityCategory

    monkeypatch.setitem(sys.modules, "homeassistant.components", components)
    monkeypatch.setitem(sys.modules, "homeassistant.components.sensor", sensor)
    monkeypatch.setitem(sys.modules, "homeassistant.config_entries", config_entries)
    monkeypatch.setitem(sys.modules, "homeassistant.const", const)
    monkeypatch.setitem(sys.modules, "homeassistant.core", core)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers", helpers)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.device_registry", device_registry)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.dispatcher", dispatcher)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.entity", entity_helpers)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.entity_platform", entity_platform)


@dataclass
class RuntimeData:
    manager: DeviceManager
    manufacturer: str | None = None
    model: str | None = None
    sw_version: str | None = None


@dataclass
class Entry:
    runtime_data: RuntimeData
    entry_id: str = "entry-1"


def _descriptor() -> MetricDescriptor:
    return MetricDescriptor(
        unique_key="mem_used_percent",
        measurement="mem",
        tags={"host": "host1"},
        field="used_percent",
        value=41.2,
        timestamp=1721664000,
        native_unit=None,
        suggested_device_class=None,
        suggested_state_class="measurement",
        entity_category=None,
    )


def test_sensor_availability_and_live_override_refresh(monkeypatch) -> None:
    _install_sensor_homeassistant_stubs(monkeypatch)
    # The real harness imports this module under real HA earlier in the suite;
    # drop the cache so the stub-bound version is imported, then drop it again
    # so later tests re-import against whatever environment they provide.
    sys.modules.pop("custom_components.telegraf_mqtt.sensor", None)
    try:
        _run_sensor_assertions(importlib.import_module("custom_components.telegraf_mqtt.sensor"))
    finally:
        sys.modules.pop("custom_components.telegraf_mqtt.sensor", None)


def _run_sensor_assertions(sensor_module) -> None:
    manager = DeviceManager()
    registry = manager.get_or_create_registry("host1", "host1")
    registry.update(_descriptor())
    entry = Entry(RuntimeData(manager=manager))
    entity = sensor_module.TelegrafMqttSensor(entry, "host1:mem_used_percent")

    assert entity.available is True
    assert entity.native_value == 41.2
    assert entity._attr_unique_id == "telegraf_mqtt_host1_mem_used_percent"

    manager.apply_options(
        exclude_patterns=("mem_*",),
        field_overrides={"used_percent": {"native_unit": "%"}},
    )
    entity._handle_metric_updated("host1:mem_used_percent")

    assert entity.available is False
    assert entity.native_value == 41.2
    assert entity._attr_native_unit_of_measurement == "%"
    assert entity.write_count == 1
