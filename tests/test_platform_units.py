"""Platform-entity unit tests for sensor.py / binary_sensor.py branch coverage.

Stub-based (harness-free) per AGENTS.md's platform-independence goal. Modules are
re-imported fresh against the stubs and dropped from sys.modules afterwards so
execution order can never pollute the real-HA harness tests.
"""

from __future__ import annotations

import asyncio
import enum
import importlib
import sys
import types
from dataclasses import dataclass, field

import pytest

from custom_components.telegraf_mqtt.const import (
    PLATFORM_HINT_BINARY_SENSOR,
    PLATFORM_HINT_NONE,
    PLATFORM_HINT_SENSOR,
    SIGNAL_METRIC_UPDATED,
    SIGNAL_NEW_METRIC,
    SIGNAL_REMOVE_METRIC,
)
from custom_components.telegraf_mqtt.models import MetricDescriptor
from custom_components.telegraf_mqtt.registry import DeviceManager

ENTRY_ID = "entry-1"
_TARGETS_HOLDER: dict[str, list] = {"targets": {}}
_SENDS_HOLDER: dict[str, list] = {"sends": []}


def _install_platform_stubs(monkeypatch) -> dict[str, list]:
    """Minimal HA stand-ins; records dispatcher targets by signal for later dispatch.

    Kept in lockstep with the platforms' import surface (see conftest's
    ``_build_ha_stub_modules``): a missing name pulls the real HA package
    into the stub window and poisons later harness tests.
    """
    components = types.ModuleType("homeassistant.components")
    sensor = types.ModuleType("homeassistant.components.sensor")
    binary_sensor = types.ModuleType("homeassistant.components.binary_sensor")
    config_entries = types.ModuleType("homeassistant.config_entries")
    const = types.ModuleType("homeassistant.const")
    core = types.ModuleType("homeassistant.core")
    device_registry = types.ModuleType("homeassistant.helpers.device_registry")
    dispatcher = types.ModuleType("homeassistant.helpers.dispatcher")
    entity_platform = types.ModuleType("homeassistant.helpers.entity_platform")
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

    # Phase 10 re-routing fires ``SIGNAL_REMOVE_METRIC`` from the
    # platform's ``reevaluate_routing`` listener; record every send so
    # the rerouting tests can assert the exact signal payloads.
    sends = _SENDS_HOLDER["sends"]

    def async_dispatcher_send(_hass, signal, *args):
        sends.append((signal, *args))

    sensor.SensorEntity = StubEntity
    binary_sensor.BinarySensorEntity = StubEntity
    config_entries.ConfigEntry = object
    const.UnitOfTemperature = UnitOfTemperature
    core.HomeAssistant = object
    core.callback = callback
    device_registry.DeviceInfo = dict
    dispatcher.async_dispatcher_connect = async_dispatcher_connect
    dispatcher.async_dispatcher_send = async_dispatcher_send
    entity_helpers = types.ModuleType("homeassistant.helpers.entity")

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

    entity_helpers.EntityCategory = StubEntityCategory

    sensor.SensorDeviceClass = SensorDeviceClass
    sensor.SensorStateClass = SensorStateClass
    const.EntityCategory = StubEntityCategory
    entity_platform.AddEntitiesCallback = object

    monkeypatch.setitem(sys.modules, "homeassistant.components", components)
    monkeypatch.setitem(sys.modules, "homeassistant.components.sensor", sensor)
    monkeypatch.setitem(sys.modules, "homeassistant.components.binary_sensor", binary_sensor)
    monkeypatch.setitem(sys.modules, "homeassistant.config_entries", config_entries)
    monkeypatch.setitem(sys.modules, "homeassistant.const", const)
    monkeypatch.setitem(sys.modules, "homeassistant.core", core)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers", helpers)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.device_registry", device_registry)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.dispatcher", dispatcher)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.entity", entity_helpers)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.entity_platform", entity_platform)
    return targets


@dataclass
class RuntimeData:
    manager: DeviceManager
    manufacturer: str | None = None
    model: str | None = None
    sw_version: str | None = None


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
    # Per-test isolation: the holders are module-level so the stub
    # closures can reach them; stale targets/sends must never leak
    # between tests.
    _TARGETS_HOLDER["targets"] = {}
    _SENDS_HOLDER["sends"] = []
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


def test_binary_sensor_setup_ignores_unknown_signals(platform_env) -> None:
    """The binary_sensor platform's ``add_metric`` guard must return without
    adding an entity when (a) the manager has no record of the metric key
    (``state is None``) or (b) the metric was already added. Mirrors the
    sensor-side test in ``test_sensor_setup_adds_existing_metrics_and_ignores_unknown_signals``
    so both platform branches are exercised in lock-step -- if one drifts the
    coverage report surfaces the gap immediately."""
    binary_module = _fresh_module("binary_sensor")
    manager = DeviceManager()
    registry = manager.get_or_create_registry("host1", "host1")
    registry.update(_descriptor("link_up", True))

    added, targets, _entry = _setup_platform(binary_module, manager)

    assert len(added) == 1  # the one boolean metric that exists
    assert added[0]._attr_unique_id == "telegraf_mqtt_host1_link_up"

    new_metric_signal = SIGNAL_NEW_METRIC.format(entry_id=ENTRY_ID)
    assert targets[new_metric_signal], "setup must subscribe to the new-metric signal"

    # Unknown metric key: guard returns without adding (state is None).
    for target in targets[new_metric_signal]:
        target("bogus:key")
    assert len(added) == 1

    # Known-but-already-added key: dedup guard returns as well.
    for target in targets[new_metric_signal]:
        target("host1:link_up")
    assert len(added) == 1


def test_diagnostic_descriptors_carry_entity_category_on_sensor(platform_env) -> None:
    sensor_module = _fresh_module("sensor")
    manager = DeviceManager()
    registry = manager.get_or_create_registry("host1", "host1")
    registry.update(_descriptor("used_percent", 63.5, entity_category="diagnostic"))

    added, _targets, _entry = _setup_platform(sensor_module, manager)

    stub_ec = sys.modules["homeassistant.helpers.entity"].EntityCategory
    assert added[0]._attr_entity_category is stub_ec.DIAGNOSTIC


def test_non_diagnostic_descriptors_leave_entity_category_none(platform_env) -> None:
    sensor_module = _fresh_module("sensor")
    binary_module = _fresh_module("binary_sensor")
    manager = DeviceManager()
    registry = manager.get_or_create_registry("host1", "host1")
    registry.update(_descriptor("usage_idle", 12.5))
    registry.update(_descriptor("link_up", True))

    sensor_added, _targets, _entry = _setup_platform(sensor_module, manager)
    binary_added, _btargets, _bentry = _setup_platform(binary_module, manager)

    assert sensor_added[0]._attr_entity_category is None
    assert binary_added[0]._attr_entity_category is None


# --- Phase 10 live platform re-routing --------------------------------------
#
# ``apply_options`` re-applies ``field_overrides`` and fires
# ``SIGNAL_METRIC_UPDATED`` (through ``__init__.py``'s ``on_write`` wiring)
# for every changed state; both platforms must reconcile their routing on
# that tick: the loser fires ``SIGNAL_REMOVE_METRIC`` (``__init__.py``'s
# ``_listener_remove_metric`` drops the entity), the winner re-adds it.


def _bridge(targets: dict[str, list], signal: str):
    """Mirror ``__init__.py``'s options-update ``on_write`` wiring: a registry
    write fans out as a ``SIGNAL_METRIC_UPDATED`` dispatch to every platform
    target subscribed by ``async_setup_entry``."""

    def on_write(metric_key: str, _available: bool, _value: object) -> None:
        for target in targets[signal]:
            target(metric_key)

    return on_write


def test_sensor_hint_flip_to_binary_sensor_reroutes_live(platform_env) -> None:
    """``platform`` flipped sensor -> binary_sensor: the sensor platform
    drops its entity and fires ``SIGNAL_REMOVE_METRIC`` on the update tick,
    while the binary_sensor platform re-adds the metric on the same tick."""
    sensor_module = _fresh_module("sensor")
    binary_module = _fresh_module("binary_sensor")
    manager = DeviceManager()
    registry = manager.get_or_create_registry("host1", "host1")
    registry.update(_descriptor("link_up", True, platform_hint=PLATFORM_HINT_SENSOR))

    sensor_added, targets, _entry = _setup_platform(sensor_module, manager)
    binary_added, _btargets, _bentry = _setup_platform(binary_module, manager)

    assert len(sensor_added) == 1  # the bool was pinned to the sensor platform
    assert binary_added == []  # ... so binary_sensor skipped it

    updated_signal = SIGNAL_METRIC_UPDATED.format(entry_id=ENTRY_ID)
    remove_signal = SIGNAL_REMOVE_METRIC.format(entry_id=ENTRY_ID)
    manager.apply_options(
        field_overrides={"link_up": {"platform": PLATFORM_HINT_BINARY_SENSOR}},
        on_write=_bridge(targets, updated_signal),
    )

    # Exactly one remove signal, carrying the composite metric key.
    assert _SENDS_HOLDER["sends"] == [(remove_signal, "host1:link_up")]
    # The binary_sensor platform picked the metric up without a reload.
    assert len(binary_added) == 1
    assert binary_added[0]._attr_unique_id == "telegraf_mqtt_host1_link_up"

    # Re-firing the tick is idempotent: the metric is no longer in the
    # sensor platform's ``added`` set and the binary platform dedups.
    for target in targets[updated_signal]:
        target("host1:link_up")
    assert _SENDS_HOLDER["sends"] == [(remove_signal, "host1:link_up")]

    # Unknown metric keys are guarded off before any dispatch.
    for target in targets[updated_signal]:
        target("host1:bogus")
    assert _SENDS_HOLDER["sends"] == [(remove_signal, "host1:link_up")]


def test_binary_sensor_hint_flip_to_sensor_reroutes_live(platform_env) -> None:
    """``platform`` flipped binary_sensor -> sensor: the binary_sensor platform
    fires ``SIGNAL_REMOVE_METRIC`` on the update tick and the sensor platform
    picks the metric back up. A value change while still auto-routed must not
    dispatch anything (the metric stays put)."""
    sensor_module = _fresh_module("sensor")
    binary_module = _fresh_module("binary_sensor")
    manager = DeviceManager()
    registry = manager.get_or_create_registry("host1", "host1")
    registry.update(_descriptor("link_up", True))

    sensor_added, targets, _entry = _setup_platform(sensor_module, manager)
    binary_added, _btargets, _bentry = _setup_platform(binary_module, manager)

    assert sensor_added == []  # auto-routed bools stay off the sensor platform

    updated_signal = SIGNAL_METRIC_UPDATED.format(entry_id=ENTRY_ID)
    remove_signal = SIGNAL_REMOVE_METRIC.format(entry_id=ENTRY_ID)

    def registry_bridge(metric_key: str, _available: bool, _value: object) -> None:
        # Direct ``registry.update`` calls emit registry-local keys; the
        # manager's dispatcher wiring prefixes the device id.
        for target in targets[updated_signal]:
            target(f"host1:{metric_key}")

    # Value flip while still auto-routed: binary keeps the entity, the
    # sensor platform's re-evaluator ignores the bool it does not own.
    registry.update(_descriptor("link_up", False), on_write=registry_bridge)
    assert _SENDS_HOLDER["sends"] == []
    assert len(binary_added) == 1
    assert binary_added[0].is_on is False

    manager.apply_options(
        field_overrides={"link_up": {"platform": PLATFORM_HINT_SENSOR}},
        on_write=_bridge(targets, updated_signal),
    )

    assert _SENDS_HOLDER["sends"] == [(remove_signal, "host1:link_up")]
    assert len(sensor_added) == 1
    assert sensor_added[0]._attr_unique_id == "telegraf_mqtt_host1_link_up"


def test_hint_none_flip_removes_entity_and_lands_nowhere(platform_env) -> None:
    """``platform=none`` excludes the field everywhere: the owning platform
    fires ``SIGNAL_REMOVE_METRIC`` and neither platform re-adds it -- not on
    the update tick, and not on a later new-metric tick."""
    sensor_module = _fresh_module("sensor")
    binary_module = _fresh_module("binary_sensor")
    manager = DeviceManager()
    registry = manager.get_or_create_registry("host1", "host1")
    registry.update(_descriptor("link_up", True, platform_hint=PLATFORM_HINT_SENSOR))

    _sensor_added, targets, _entry = _setup_platform(sensor_module, manager)
    binary_added, _btargets, _bentry = _setup_platform(binary_module, manager)

    updated_signal = SIGNAL_METRIC_UPDATED.format(entry_id=ENTRY_ID)
    new_metric_signal = SIGNAL_NEW_METRIC.format(entry_id=ENTRY_ID)
    remove_signal = SIGNAL_REMOVE_METRIC.format(entry_id=ENTRY_ID)

    manager.apply_options(
        field_overrides={"link_up": {"platform": PLATFORM_HINT_NONE}},
        on_write=_bridge(targets, updated_signal),
    )

    assert _SENDS_HOLDER["sends"] == [(remove_signal, "host1:link_up")]
    assert binary_added == []

    # Even a fresh new-metric tick must not resurrect the excluded field:
    # add_metric rejects sensor-pinned and excluded hints on both platforms.
    for target in targets[new_metric_signal]:
        target("host1:link_up")
    assert binary_added == []
    assert _SENDS_HOLDER["sends"] == [(remove_signal, "host1:link_up")]
    assert registry.get("link_up") is not None  # state lingers until the next publish drops it


def test_value_type_change_reroutes_between_platforms(platform_env) -> None:
    """A bool metric that stops being boolean (Telegraf starts sending 0/1
    ints after a config change) leaves the binary_sensor platform and lands
    on the sensor platform without a reload."""
    sensor_module = _fresh_module("sensor")
    binary_module = _fresh_module("binary_sensor")
    manager = DeviceManager()
    registry = manager.get_or_create_registry("host1", "host1")
    registry.update(_descriptor("link_up", True))

    sensor_added, targets, _entry = _setup_platform(sensor_module, manager)
    binary_added, _btargets, _bentry = _setup_platform(binary_module, manager)

    assert len(binary_added) == 1

    updated_signal = SIGNAL_METRIC_UPDATED.format(entry_id=ENTRY_ID)
    remove_signal = SIGNAL_REMOVE_METRIC.format(entry_id=ENTRY_ID)

    def registry_bridge(metric_key: str, _available: bool, _value: object) -> None:
        for target in targets[updated_signal]:
            target(f"host1:{metric_key}")

    registry.update(_descriptor("link_up", 7), on_write=registry_bridge)

    assert _SENDS_HOLDER["sends"] == [(remove_signal, "host1:link_up")]
    assert len(sensor_added) == 1
    assert sensor_added[0].native_value == 7
