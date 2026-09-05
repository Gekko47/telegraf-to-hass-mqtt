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
import importlib
import inspect
import json
from dataclasses import dataclass

import pytest
from conftest import (
    _install_binary_sensor_stubs_and_reload,
    _install_sensor_stubs_and_reload,
    _pop_integration_modules,
    _restore_ha_stubs,
)

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
        # Phase 11 -- byte counters with the data_size device_class. HA's
        # recorder accepts ``data_size`` + ``total_increasing`` (see
        # ``SensorDeviceClass.DATA_SIZE`` in HA core). Drives the
        # "7.38 GB" rendering for ``mem.used`` / ``diskio.read_bytes`` /
        # ``swap.used`` / etc.
        ("data_size", "total_increasing", "B"),
        # Phase 11 -- byte gauges (e.g. ``mem.used``, ``disk.free``) carry
        # the same data_size device class but the state class is
        # ``measurement`` (gauge) because the value is a current snapshot,
        # not a counter.
        ("data_size", "measurement", "B"),
        # Phase 11 -- uptime rendered as a duration. Lets the recorder /
        # UI render the seconds-as-duration form for ``system.uptime``.
        ("duration", "total_increasing", "s"),
        # Phase 11 -- ms-precision duration fields (ping, net_response,
        # http_response). These are gauges, not counters, so the state
        # class is "measurement".
        ("duration", "measurement", "ms"),
        # Phase 11 -- wireless signal_strength (dBm). Gauges.
        ("signal_strength", "measurement", "dBm"),
        # Bug 1 fix: diskio duration fields (ms) are gauges with duration device_class
        ("duration", "measurement", "ms"),
        # Bug 1 fix: diskio io_util is a percentage gauge
        (None, "measurement", "%"),
        # Bug 2 fix: wireless packet counters are dimensionless total_increasing
        (None, "total_increasing", None),
        # Bug 3 fix: ipmi_sensor with unit tag mapping
        ("temperature", "measurement", "°C"),
        (None, "measurement", "RPM"),
        ("voltage", "measurement", "V"),
        ("power", "measurement", "W"),
        ("current", "measurement", "A"),
    }
)


# Real-world representative Telegraf measurements (sourced from common plugin
# sets, plus the 7 SPEC reference payloads). Each entry is a
# (measurement, field, probe_value) tuple: ``probe_value`` is the value fed to
# ``infer_state_class`` -- ``True`` for boolean fields so the boolean branch
# returns None, ``1`` for numerics so the default "measurement" path is
# taken. The (measurement, field, probe_value) triple is fed through
# ``infer_*`` to derive the actual (device_class, state_class, native_unit)
# the pipeline will assign; the parametrized test below asserts that
# combination lives in ALLOWED_COMBOS.
REPRESENTATIVE_FIELDS: list[tuple[str, str, float | int | bool]] = [
    # From the 7 SPEC.md reference payloads.
    ("cpu", "usage_idle", 1),
    ("mem", "used_percent", 1),
    ("mem", "used", 1),
    ("disk", "used_percent", 1),
    ("disk", "free", 1),
    ("net", "bytes_recv", 1),
    ("net", "bytes_sent", 1),
    ("sensors", "temp_input", 1),
    ("nvidia_gpu", "gpu_util", 1),
    ("nvidia_gpu", "temp", 1),
    ("nvidia_gpu", "mem_used", 1),
    ("battery", "percentage", 1),
    ("battery", "voltage", 1),
    # Lifecycle/load/process fields (DIAGNOSTIC per SPEC.md).
    ("system", "uptime", 1),
    ("system", "load1", 1),
    ("system", "load5", 1),
    ("system", "load15", 1),
    ("system", "processes_forked", 1),
    ("system", "n_users", 1),
    # Other Telegraf outputs commonly seen in the wild.
    ("diskio", "read_bytes", 1),
    ("diskio", "write_bytes", 1),
    ("swap", "used_percent", 1),
    ("swap", "used", 1),
    ("mem", "available", 1),
    ("mem", "total", 1),
    ("processes", "total", 1),
    ("processes", "running", 1),
    ("cpu", "usage_user", 1),
    # Boolean -- confirmed by infer_state_class returning None; the
    # (device_class, state_class, native_unit) tuple will be (None, None, None)
    # and the allowlist must permit it (binary sensors don't have a state_class).
    # The probe value is stored alongside the (measurement, field) tuple so
    # the boolean branch of infer_state_class is exercised directly.
    ("net", "link_up", True),
    # More unit branches from ``infer_native_unit`` not in SPEC reference set.
    ("ups", "battery_runtime", 1),
    ("sensors", "fan1_input", 1),
    ("smart", "energy_rate", 1),
    # Bug 1 fix: diskio duration and io_util fields
    ("diskio", "read_time", 1),
    ("diskio", "write_time", 1),
    ("diskio", "io_time", 1),
    ("diskio", "weighted_io_time", 1),
    ("diskio", "io_util", 1),
    # Bug 2 fix: wireless packet counters
    ("wireless", "nwid", 1),
    ("wireless", "crypt", 1),
    ("wireless", "frag", 1),
    ("wireless", "retry", 1),
    ("wireless", "misc", 1),
    ("wireless", "missed_beacon", 1),
    # Bug 3 fix: ipmi_sensor with various unit tags
    ("ipmi_sensor", "value", 1),  # unit tag handled by tag mapping
]


def _combination_for(
    measurement: str, field: str, value: float | int | bool = 1
) -> tuple[str | None, str | None, str | None]:
    """Resolve the (device_class, state_class, native_unit) combination the
    pipeline will assign for a (measurement, field) pair.

    Phase 11: ``infer_native_unit`` takes the measurement so the
    per-measurement byte override (``mem.used`` -> ``B``) kicks in here
    too, mirroring the parser's call site.
    """
    return (
        infer_device_class(measurement, field),
        infer_state_class(field, value),
        infer_native_unit(field, measurement),
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
        "custom_components.telegraf_mqtt.units",
        # Phase 11 -- the new per-measurement parsers all delegate to
        # ``parse_generic_payload`` (or are pure functions), so the
        # audit applies to them identically.
        "custom_components.telegraf_mqtt.parsers.system",
        "custom_components.telegraf_mqtt.parsers.kernel",
        "custom_components.telegraf_mqtt.parsers.kernel_vmstat",
        "custom_components.telegraf_mqtt.parsers.processes",
        "custom_components.telegraf_mqtt.parsers.swap",
        "custom_components.telegraf_mqtt.parsers.diskio",
        "custom_components.telegraf_mqtt.parsers.ping",
        "custom_components.telegraf_mqtt.parsers.smart",
        "custom_components.telegraf_mqtt.parsers.docker",
        "custom_components.telegraf_mqtt.parsers.wireless",
        "custom_components.telegraf_mqtt.parsers.zfs",
        "custom_components.telegraf_mqtt.parsers.net_response",
        "custom_components.telegraf_mqtt.parsers.http_response",
        "custom_components.telegraf_mqtt.parsers.ipmi_sensor",
        "custom_components.telegraf_mqtt.parsers.interrupts",
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
    "measurement,field,value",
    REPRESENTATIVE_FIELDS,
    ids=[f"{m}.{f}" for m, f, _v in REPRESENTATIVE_FIELDS],
)
def test_combination_is_recorder_valid(measurement: str, field: str, value: float | int | bool) -> None:
    """Every combination the pipeline can assign must be in ALLOWED_COMBOS.

    Phase 4 exit criterion 2: verified valid for long-term statistics by test.
    """
    # The probe ``value`` is supplied by REPRESENTATIVE_FIELDS: booleans are
    # passed as ``True`` so ``infer_state_class`` takes the boolean branch
    # (returning None), numeric fields as ``1`` so they fall through to the
    # default "measurement" path. No field-name branching here.
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
        {
            "name": "mem",
            "tags": {"host": "h"},
            "fields": {"used_percent": 41.2, "used": 8589934592},
            "timestamp": 1721664000,
        },
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
        {
            "name": "nvidia_gpu",
            "tags": {"host": "h"},
            "fields": {"gpu_util": 12, "temp": 45.0, "mem_used": 1024},
            "timestamp": 1721664000,
        },
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
# pattern; the HA stub installers themselves live in tests/conftest.py so the
# Bronze test in test_phase5_bronze.py can reuse the same scaffolding).
# ---------------------------------------------------------------------------


@dataclass
class _RuntimeData:
    manager: DeviceManager
    manufacturer: str | None = None
    model: str | None = None
    sw_version: str | None = None


@dataclass
class _Entry:
    runtime_data: _RuntimeData
    entry_id: str = "entry-1"

    def __post_init__(self) -> None:
        self._unload_callbacks: list = []

    def async_on_unload(self, callback) -> None:
        self._unload_callbacks.append(callback)


def _setup_sensor_platform(sensor_module, manager, entry: _Entry, added: list) -> None:
    """Drive the platform's async_setup_entry the way HA would at startup."""
    # ``manager`` is carried via entry.runtime_data; kept as an explicit
    # argument so call sites read like the production wiring they mirror.
    asyncio.run(sensor_module.async_setup_entry(object(), entry, added.extend))


def _setup_binary_platform(binary_module, manager, entry: _Entry, added: list) -> None:
    asyncio.run(binary_module.async_setup_entry(object(), entry, added.extend))
