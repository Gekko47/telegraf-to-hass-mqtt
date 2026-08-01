from __future__ import annotations

import importlib
import sys
import types
from dataclasses import dataclass

from custom_components.telegraf_mqtt.models import MetricDescriptor
from custom_components.telegraf_mqtt.registry import MetricRegistry


def _install_sensor_homeassistant_stubs(monkeypatch) -> None:
    components = types.ModuleType("homeassistant.components")
    sensor = types.ModuleType("homeassistant.components.sensor")
    config_entries = types.ModuleType("homeassistant.config_entries")
    const = types.ModuleType("homeassistant.const")
    core = types.ModuleType("homeassistant.core")
    device_registry = types.ModuleType("homeassistant.helpers.device_registry")
    dispatcher = types.ModuleType("homeassistant.helpers.dispatcher")
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

    def callback(func):
        return func

    def async_dispatcher_connect(hass, signal, target):
        return lambda: None

    sensor.SensorEntity = SensorEntity
    config_entries.ConfigEntry = ConfigEntry
    const.UnitOfTemperature = UnitOfTemperature
    core.HomeAssistant = HomeAssistant
    core.callback = callback
    device_registry.DeviceInfo = dict
    dispatcher.async_dispatcher_connect = async_dispatcher_connect

    monkeypatch.setitem(sys.modules, "homeassistant.components", components)
    monkeypatch.setitem(sys.modules, "homeassistant.components.sensor", sensor)
    monkeypatch.setitem(sys.modules, "homeassistant.config_entries", config_entries)
    monkeypatch.setitem(sys.modules, "homeassistant.const", const)
    monkeypatch.setitem(sys.modules, "homeassistant.core", core)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers", helpers)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.device_registry", device_registry)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.dispatcher", dispatcher)


@dataclass
class RuntimeData:
    registry: MetricRegistry
    device_id: str = "entry-1"
    device_name: str = "Telegraf MQTT"
    manufacturer: str | None = None
    model: str | None = None


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
        name="Memory Used Percent",
        native_unit=None,
        suggested_device_class=None,
        suggested_state_class="measurement",
        entity_category=None,
    )


def test_sensor_availability_and_live_override_refresh(monkeypatch) -> None:
    _install_sensor_homeassistant_stubs(monkeypatch)
    sensor_module = importlib.import_module("custom_components.telegraf_mqtt.sensor")

    registry = MetricRegistry()
    registry.update(_descriptor())
    entry = Entry(RuntimeData(registry=registry))
    entity = sensor_module.TelegrafMqttSensor(entry, "mem_used_percent")

    assert entity.available is True
    assert entity.native_value == 41.2

    registry.apply_options(
        exclude_patterns=("mem_*",),
        field_overrides={"used_percent": {"native_unit": "%"}},
    )
    entity._handle_metric_updated("mem_used_percent")

    assert entity.available is False
    assert entity.native_value == 41.2
    assert entity._attr_native_unit_of_measurement == "%"
    assert entity.write_count == 1
