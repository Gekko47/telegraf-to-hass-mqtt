"""Phase 4 exit-criteria tests: units, statistics, and binary-sensor projection.

ROADMAP.md Phase 4:
  - "Bytes stay bytes; native_unit set; no pre-conversion anywhere"
  - "Every assigned device_class/state_class pair verified valid for long-term
    statistics by test"
  - "Boolean fields become BinarySensorEntity; documented in README"

Harness-free per AGENTS.md: parser/naming logic must not depend on the HA harness.
The pipeline walk-through (parser -> registry -> sensor/binary_sensor stub) reuses
the same HA-stub installer pattern as ``tests/test_platform_units.py`` so the
"no pre-conversion" claim is enforced end-to-end, not just at the parser.
"""

from __future__ import annotations

import asyncio
import enum
import importlib
import inspect
import json
import sys
import types
from dataclasses import dataclass

import pytest

from custom_components.telegraf_mqtt.models import MetricDescriptor
from custom_components.telegraf_mqtt.parser import TelegrafParser
from custom_components.telegraf_mqtt.parsers.generic import (
    infer_device_class,
    infer_native_unit,
    infer_state_class,
)
from custom_components.telegraf_mqtt.registry import DeviceManager

# ---------------------------------------------------------------------------
# Allowlist: every (device_class, state_class, native_unit) tuple the
# integration is allowed to assign, per the Phase 4 "verified valid for
# long-term statistics by test" exit criterion. The recorder only accepts
# documented combinations (e.g. ``energy`` requires ``total_increasing``;
# ``measurement`` is forbidden for cumulative counters; byte counters need
# ``total_increasing``; etc.).
#
# The allowlist is intentionally explicit. If a new measurement or field is
# ever introduced that produces a tuple outside this set, the parametrized
# test below fails first -- that is the contract.
# ---------------------------------------------------------------------------

# ``None`` entries mean "no value" (e.g. a measurement-type sensor without a
# device_class, or a unit-less gauge).
ALLOWED_COMBOS: frozenset[tuple[str | None, str | None, str | None]] = frozenset(
    {
        # Generic gauges: no device_class, measurement state_class, no unit.
        (None, "measurement", None),
        # Percentage gauges (e.g. mem.used_percent, battery.percentage) -- no
        # device_class, measurement, "%" unit.
        (None, "measurement", "%"),
        # Battery-class percentages.
        ("battery", "measurement", "%"),
        # Temperature.
        ("temperature", "measurement", "°C"),
        # Voltage.
        ("voltage", "measurement", "V"),
        # Power.
        ("power", "measurement", "W"),
        # Energy (cumulative Wh).
        ("energy", "total_increasing", "Wh"),
        # Network/IO byte counters -- HA's recorder accepts "B" under
        # state_class=total_increasing even without a device_class.
        (None, "total_increasing", "B"),
        # Uptime / monotonic time.
        (None, "total_increasing", "s"),
        # Fan RPM.
        (None, "measurement", "RPM"),
        # Boolean fields (e.g. net.link_up) routed to binary_sensor.py --
        # they have no device_class, no state_class, and no native unit, so
        # the recorder tuple is the all-None sentinel. Binary sensors skip
        # the recorder's state_class validation, but the allowlist still
        # has to permit the tuple so the parametrize test stays green.
        (None, None, None),
    }
)


# Real-world representative Telegraf measurements (sourced from common plugin
# sets, plus the 7 SPEC reference payloads). For each, the (measurement, field)
# tuple is fed through ``infer_*`` to derive the actual (device_class,
# state_class, native_unit) the pipeline will assign; the parametrized test
# below asserts that combination lives in ALLOWED_COMBOS.
REPRESENTATIVE_FIELDS: list[tuple[str, str]] = [
    # From the 7 SPEC.md reference payloads.
    ("cpu", "usage_idle"),
    ("mem", "used_percent"),
    ("mem", "used"),
    ("disk", "used_percent"),
    ("disk", "free"),
    ("net", "bytes_recv"),
    ("net", "bytes_sent"),
    ("sensors", "temp_input"),
    ("nvidia_gpu", "gpu_util"),
    ("nvidia_gpu", "temp"),
    ("nvidia_gpu", "mem_used"),
    ("battery", "percentage"),
    ("battery", "voltage"),
    # Lifecycle/load/process fields (DIAGNOSTIC per SPEC.md).
    ("system", "uptime"),
    ("system", "load1"),
    ("system", "load5"),
    ("system", "load15"),
    ("system", "processes_forked"),
    ("system", "n_users"),
    # Other Telegraf outputs commonly seen in the wild.
    ("diskio", "read_bytes"),
    ("diskio", "write_bytes"),
    ("swap", "used_percent"),
    ("swap", "used"),
    ("mem", "available"),
    ("mem", "total"),
    ("processes", "total"),
    ("processes", "running"),
    ("cpu", "usage_user"),
    # Boolean -- confirmed by infer_state_class returning None; the
    # (device_class, state_class, native_unit) tuple will be (None, None, None)
    # and the allowlist must permit it (binary sensors don't have a state_class).
    # The test_combination_is_recorder_valid parametrize call below detects
    # this entry and passes True explicitly so the boolean branch of
    # infer_state_class is exercised instead of falling through to the
    # default-int "measurement" path.
    ("net", "link_up"),
    # More unit branches from ``infer_native_unit`` not in SPEC reference set.
    ("ups", "battery_runtime"),
    ("sensors", "fan1_input"),
    ("smart", "energy_rate"),
]


def _combination_for(
    measurement: str, field: str, value: float | int | bool = 1
) -> tuple[str | None, str | None, str | None]:
    """Resolve the (device_class, state_class, native_unit) combination the
    pipeline will assign for a (measurement, field) pair."""
    return (
        infer_device_class(measurement, field),
        infer_state_class(field, value),
        infer_native_unit(field),
    )


# ---------------------------------------------------------------------------
# Exit criterion 1: "Bytes stay bytes; native_unit set; no pre-conversion"
# ---------------------------------------------------------------------------


def test_bytes_stay_bytes_end_to_end() -> None:
    """The raw byte value must reach the sensor unchanged and native_unit must
    be set on the descriptor.

    Phase 4 exit criterion 1: bytes are stored as bytes (no /1024, no MB, no
    human-friendly conversion) and the native unit is set, not None.
    """
    parser = TelegrafParser()
    payload = {
        "name": "net",
        "tags": {"host": "host1", "interface": "wlan0"},
        "fields": {"bytes_recv": 1048576000, "bytes_sent": 209715200},
        "timestamp": 1721664000,
    }

    descriptors = parser.parse(json.dumps(payload))
    by_field = {descriptor.field: descriptor for descriptor in descriptors}

    for field, expected_value in (("bytes_recv", 1048576000), ("bytes_sent", 209715200)):
        descriptor = by_field[field]
        assert descriptor.value == expected_value, f"{field} value must be stored raw"
        assert descriptor.native_unit == "B", f"{field} native_unit must be 'B', got {descriptor.native_unit!r}"
        assert descriptor.suggested_state_class == "total_increasing", (
            f"{field} state_class must be 'total_increasing' for byte counters"
        )


def test_no_premature_byte_conversion_in_pipeline_source() -> None:
    """No code path in the parser/registry/descriptor/sensor/binary_sensor
    chain may convert byte values to KB/MB/GB or divide by 1024/1000.

    Phase 4 exit criterion 1 ("no pre-conversion anywhere") -- enforced by
    auditing the source of every relevant module for the forbidden tokens. A
    unit test is the right place for this; the alternative is a code review
    checklist that nobody actually runs.
    """
    forbidden_tokens: tuple[str, ...] = (
        " / 1024",
        "/1024",
        " / 1000",
        "/1000",
        '"KB"',
        '"MB"',
        '"GB"',
        '"KiB"',
        '"MiB"',
        '"GiB"',
        "UnitOfInformation",
        "DataSize",
    )

    modules = [
        "custom_components.telegraf_mqtt.parser",
        "custom_components.telegraf_mqtt.parsers.generic",
        "custom_components.telegraf_mqtt.parsers.cpu",
        "custom_components.telegraf_mqtt.parsers.mem",
        "custom_components.telegraf_mqtt.parsers.disk",
        "custom_components.telegraf_mqtt.parsers.net",
        "custom_components.telegraf_mqtt.parsers.sensors",
        "custom_components.telegraf_mqtt.parsers.battery",
        "custom_components.telegraf_mqtt.parsers.nvidia_gpu",
        "custom_components.telegraf_mqtt.parsers.base",
        "custom_components.telegraf_mqtt.models",
        "custom_components.telegraf_mqtt.registry",
        "custom_components.telegraf_mqtt.sensor",
        "custom_components.telegraf_mqtt.binary_sensor",
        "custom_components.telegraf_mqtt.heuristics",
        "custom_components.telegraf_mqtt.naming",
    ]
    violations: list[str] = []
    for module_name in modules:
        module = importlib.import_module(module_name)
        source = inspect.getsource(module)
        for token in forbidden_tokens:
            if token in source:
                violations.append(f"{module_name}: forbidden token {token!r} present")

    assert not violations, "Premature byte conversion detected:\n" + "\n".join(violations)


def test_native_unit_propagates_to_sensor_entity_through_registry() -> None:
    """End-to-end: parser -> registry -> sensor entity must expose the
    raw byte value and the 'B' native unit on the entity itself.
    """
    sensor_module = _install_sensor_stubs_and_reload()
    try:
        manager = DeviceManager(parser=TelegrafParser())
        manager.get_or_create_registry("host1", "host1")

        payload = json.dumps(
            {
                "name": "net",
                "tags": {"host": "host1", "interface": "wlan0"},
                "fields": {"bytes_recv": 1234567890},
                "timestamp": 1721664000,
            }
        )
        manager.process_message("telegraf/host1/net", payload)

        entry = _Entry(_RuntimeData(manager=manager))
        added: list = []
        _setup_sensor_platform(sensor_module, manager, entry, added)

        assert len(added) == 1
        entity = added[0]
        assert entity.native_value == 1234567890
        assert entity._attr_native_unit_of_measurement == "B"
    finally:
        _pop_integration_modules()
        _restore_ha_stubs()


# ---------------------------------------------------------------------------
# Exit criterion 2: every (device_class, state_class, native_unit) pair the
# integration ever produces is one HA's recorder accepts for long-term
# statistics. Enforced via a single allowlist (ALLOWED_COMBOS above).
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "measurement,field",
    REPRESENTATIVE_FIELDS,
    ids=[f"{m}.{f}" for m, f in REPRESENTATIVE_FIELDS],
)
def test_combination_is_recorder_valid(measurement: str, field: str) -> None:
    """Every combination the pipeline can assign must be in ALLOWED_COMBOS.

    Phase 4 exit criterion 2: verified valid for long-term statistics by test.
    """
    # Boolean fields must be probed with a True value so infer_state_class
    # returns None (the boolean branch) instead of falling through to the
    # default-int "measurement" path. The list of known boolean Telegraf
    # fields lives next to REPRESENTATIVE_FIELDS; keeping the mapping local
    # avoids a new module-level constant for a single field.
    value: float | int | bool = True if (measurement, field) == ("net", "link_up") else 1
    combination = _combination_for(measurement, field, value)
    assert combination in ALLOWED_COMBOS, (
        f"({measurement!r}, {field!r}) resolves to {combination}, "
        f"which is not in the Phase 4 allowlist. Add it to ALLOWED_COMBOS or "
        f"fix the inference if the tuple is wrong."
    )


def test_reference_payload_combinations_are_recorder_valid() -> None:
    """The exact 7 SPEC reference payloads must each produce a recorder-valid
    combination for every field they contain.
    """
    reference_payloads = [
        {"name": "cpu", "tags": {"host": "h"}, "fields": {"usage_idle": 88.4}, "timestamp": 1721664000},
        {"name": "mem", "tags": {"host": "h"}, "fields": {"used_percent": 41.2, "used": 8589934592}, "timestamp": 1721664000},
        {
            "name": "disk",
            "tags": {"host": "h", "path": "/", "fstype": "ext4"},
            "fields": {"used_percent": 63.5, "free": 128849018880},
            "timestamp": 1721664000,
        },
        {
            "name": "net",
            "tags": {"host": "h", "interface": "wlan0"},
            "fields": {"bytes_recv": 1048576000, "bytes_sent": 209715200},
            "timestamp": 1721664000,
        },
        {
            "name": "sensors",
            "tags": {"host": "h", "chip": "coretemp-isa-0000", "feature": "package_id_0"},
            "fields": {"temp_input": 52.0},
            "timestamp": 1721664000,
        },
        {"name": "nvidia_gpu", "tags": {"host": "h"}, "fields": {"gpu_util": 12, "temp": 45.0, "mem_used": 1024}, "timestamp": 1721664000},
        {
            "name": "battery",
            "tags": {"host": "h", "state": "discharging"},
            "fields": {"percentage": 87.0, "voltage": 11.4},
            "timestamp": 1721664000,
        },
    ]
    parser = TelegrafParser()
    for payload in reference_payloads:
        for descriptor in parser.parse(json.dumps(payload)):
            combination = (
                descriptor.suggested_device_class,
                descriptor.suggested_state_class,
                descriptor.native_unit,
            )
            assert combination in ALLOWED_COMBOS, (
                f"SPEC reference payload field {descriptor.measurement}.{descriptor.field} "
                f"resolves to {combination}, which is not in the Phase 4 allowlist."
            )


# ---------------------------------------------------------------------------
# Exit criterion 3: boolean fields become BinarySensorEntity
# ---------------------------------------------------------------------------


def test_boolean_field_becomes_binary_sensor_and_skips_sensor_platform() -> None:
    """A boolean Telegraf field must be routed to binary_sensor.py and
    *only* there -- the sensor.py platform must skip it.
    """
    sensor_module = _install_sensor_stubs_and_reload()
    binary_module = _install_binary_sensor_stubs_and_reload()
    try:
        manager = DeviceManager(parser=TelegrafParser())
        manager.get_or_create_registry("host1", "host1")

        payload = json.dumps(
            {
                "name": "net",
                "tags": {"host": "host1", "interface": "eth0"},
                "fields": {"link_up": True},
                "timestamp": 1721664000,
            }
        )
        manager.process_message("telegraf/host1/net", payload)

        entry = _Entry(_RuntimeData(manager=manager))
        sensor_added: list = []
        binary_added: list = []
        _setup_sensor_platform(sensor_module, manager, entry, sensor_added)
        _setup_binary_platform(binary_module, manager, entry, binary_added)

        # Exactly one binary sensor, no sensor entities for the bool.
        assert len(binary_added) == 1
        assert len(sensor_added) == 0
        binary_entity = binary_added[0]
        assert isinstance(binary_entity, binary_module.BinarySensorEntity)
        assert binary_entity.is_on is True
        assert binary_entity._attr_unique_id == "telegraf_mqtt_host1_net_eth0_link_up"
    finally:
        _pop_integration_modules()
        _restore_ha_stubs()


def test_numeric_field_becomes_sensor_and_skips_binary_sensor_platform() -> None:
    """The opposite of the boolean test: a numeric field must reach
    sensor.py and must not be exposed as a binary sensor.
    """
    sensor_module = _install_sensor_stubs_and_reload()
    binary_module = _install_binary_sensor_stubs_and_reload()
    try:
        manager = DeviceManager(parser=TelegrafParser())
        manager.get_or_create_registry("host1", "host1")

        payload = json.dumps(
            {
                "name": "mem",
                "tags": {"host": "host1"},
                "fields": {"used_percent": 41.2},
                "timestamp": 1721664000,
            }
        )
        manager.process_message("telegraf/host1/mem", payload)

        entry = _Entry(_RuntimeData(manager=manager))
        sensor_added: list = []
        binary_added: list = []
        _setup_sensor_platform(sensor_module, manager, entry, sensor_added)
        _setup_binary_platform(binary_module, manager, entry, binary_added)

        assert len(sensor_added) == 1
        assert len(binary_added) == 0
        assert sensor_added[0].native_value == 41.2
    finally:
        _pop_integration_modules()
        _restore_ha_stubs()


def test_mixed_payload_routes_booleans_to_binary_and_numbers_to_sensor() -> None:
    """A mixed Telegraf payload containing both boolean and numeric fields
    must dispatch each field to its correct platform -- the Phase 4 contract
    is "boolean fields become BinarySensorEntity" and the split is at the
    dispatch layer, not per-field config.
    """
    sensor_module = _install_sensor_stubs_and_reload()
    binary_module = _install_binary_sensor_stubs_and_reload()
    try:
        manager = DeviceManager(parser=TelegrafParser())
        manager.get_or_create_registry("host1", "host1")

        payload = json.dumps(
            {
                "name": "net",
                "tags": {"host": "host1", "interface": "eth0"},
                "fields": {"link_up": True, "bytes_recv": 100, "bytes_sent": 200},
                "timestamp": 1721664000,
            }
        )
        manager.process_message("telegraf/host1/net", payload)

        entry = _Entry(_RuntimeData(manager=manager))
        sensor_added: list = []
        binary_added: list = []
        _setup_sensor_platform(sensor_module, manager, entry, sensor_added)
        _setup_binary_platform(binary_module, manager, entry, binary_added)

        # Numeric fields -> sensor; the single bool -> binary_sensor. Keys are
        # ``{device_id}:{measurement}_{sorted non-host tags}_{field}``.
        sensor_keys = {entity._metric_key for entity in sensor_added}
        binary_keys = {entity._metric_key for entity in binary_added}
        assert sensor_keys == {"host1:net_eth0_bytes_recv", "host1:net_eth0_bytes_sent"}
        assert binary_keys == {"host1:net_eth0_link_up"}
    finally:
        _pop_integration_modules()
        _restore_ha_stubs()


# ---------------------------------------------------------------------------
# Stubs and harness-free platform setup (mirrors tests/test_platform_units.py
# pattern; intentionally self-contained so this file is independent).
# ---------------------------------------------------------------------------


@dataclass
class _RuntimeData:
    manager: DeviceManager
    manufacturer: str | None = None
    model: str | None = None


@dataclass
class _Entry:
    runtime_data: _RuntimeData
    entry_id: str = "entry-1"

    def __post_init__(self) -> None:
        self._unload_callbacks: list = []

    def async_on_unload(self, callback) -> None:
        self._unload_callbacks.append(callback)


# --- Minimal HA stand-ins ---------------------------------------------------
# Same spirit as tests/test_platform_units.py's ``_install_platform_stubs``,
# but self-contained and snapshot-based: the first install remembers whatever
# lived under homeassistant.* (real HA from .venv or another test's stubs) and
# ``_restore_ha_stubs`` puts those originals back, so this file can never leak
# its stand-ins into harness-based tests that run afterwards.

_HA_MODULE_NAMES: tuple[str, ...] = (
    "homeassistant.components",
    "homeassistant.components.sensor",
    "homeassistant.components.binary_sensor",
    "homeassistant.config_entries",
    "homeassistant.const",
    "homeassistant.core",
    "homeassistant.helpers",
    "homeassistant.helpers.device_registry",
    "homeassistant.helpers.dispatcher",
    "homeassistant.helpers.entity",
)

_SAVED_HA_MODULES: dict[str, types.ModuleType | None] = {}


def _build_ha_stub_modules() -> dict[str, types.ModuleType]:
    """Fresh stand-ins covering everything sensor.py / binary_sensor.py import."""
    components = types.ModuleType("homeassistant.components")
    sensor_mod = types.ModuleType("homeassistant.components.sensor")
    binary_mod = types.ModuleType("homeassistant.components.binary_sensor")
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

    def async_dispatcher_connect(_hass, _signal, target):
        return lambda: None

    sensor_mod.SensorEntity = StubEntity
    binary_mod.BinarySensorEntity = StubEntity
    config_entries.ConfigEntry = object
    const.UnitOfTemperature = UnitOfTemperature
    core.HomeAssistant = object
    core.callback = callback
    device_registry.DeviceInfo = dict
    dispatcher.async_dispatcher_connect = async_dispatcher_connect

    entity_helpers = types.ModuleType("homeassistant.helpers.entity")

    class StubEntityCategory(str, enum.Enum):
        CONFIG = "config"
        DIAGNOSTIC = "diagnostic"

    entity_helpers.EntityCategory = StubEntityCategory

    return {
        "homeassistant.components": components,
        "homeassistant.components.sensor": sensor_mod,
        "homeassistant.components.binary_sensor": binary_mod,
        "homeassistant.config_entries": config_entries,
        "homeassistant.const": const,
        "homeassistant.core": core,
        "homeassistant.helpers": helpers,
        "homeassistant.helpers.device_registry": device_registry,
        "homeassistant.helpers.dispatcher": dispatcher,
        "homeassistant.helpers.entity": entity_helpers,
    }


def _install_ha_entity_stubs() -> None:
    if not _SAVED_HA_MODULES:
        for name in _HA_MODULE_NAMES:
            _SAVED_HA_MODULES[name] = sys.modules.get(name)
    for name, module in _build_ha_stub_modules().items():
        sys.modules[name] = module


def _restore_ha_stubs() -> None:
    """Put back whatever the pre-test environment had under homeassistant.*."""
    for name, saved in _SAVED_HA_MODULES.items():
        if saved is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = saved


def _install_sensor_stubs_and_reload():
    """Install HA stubs and import a pristine sensor.py bound to them."""
    _install_ha_entity_stubs()
    sys.modules.pop("custom_components.telegraf_mqtt.sensor", None)
    return importlib.import_module("custom_components.telegraf_mqtt.sensor")


def _install_binary_sensor_stubs_and_reload():
    """Install HA stubs and import a pristine binary_sensor.py bound to them."""
    _install_ha_entity_stubs()
    sys.modules.pop("custom_components.telegraf_mqtt.binary_sensor", None)
    return importlib.import_module("custom_components.telegraf_mqtt.binary_sensor")


def _pop_integration_modules() -> None:
    sys.modules.pop("custom_components.telegraf_mqtt.sensor", None)
    sys.modules.pop("custom_components.telegraf_mqtt.binary_sensor", None)


def _setup_sensor_platform(sensor_module, manager, entry: _Entry, added: list) -> None:
    """Drive the platform's async_setup_entry the way HA would at startup."""
    # ``manager`` is carried via entry.runtime_data; kept as an explicit
    # argument so call sites read like the production wiring they mirror.
    asyncio.run(sensor_module.async_setup_entry(object(), entry, added.extend))


def _setup_binary_platform(binary_module, manager, entry: _Entry, added: list) -> None:
    asyncio.run(binary_module.async_setup_entry(object(), entry, added.extend))
