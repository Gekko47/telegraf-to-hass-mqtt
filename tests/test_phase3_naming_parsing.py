"""Phase 3 exit-criteria tests: parametrized naming across reference payloads.

Harness-free per AGENTS.md -- parsing/naming logic must not depend on the HA
harness. Covers ROADMAP.md Phase 3:
- every SPEC.md reference payload produces clean names + metadata,
- unknown measurements fall back to generic.py without raising (DEBUG only),
- diagnostic-appropriate fields carry the diagnostic entity category.
"""

from __future__ import annotations

import json
import logging

import pytest

from custom_components.telegraf_mqtt.parser import TelegrafParser

REFERENCE_PAYLOADS: list[dict] = [
    {"name": "cpu", "tags": {"host": "cachyos-gekko"}, "fields": {"usage_idle": 88.4}, "timestamp": 1721664000},
    {
        "name": "mem",
        "tags": {"host": "cachyos-gekko"},
        "fields": {"used_percent": 41.2, "used": 8589934592},
        "timestamp": 1721664000,
    },
    {
        "name": "disk",
        "tags": {"host": "cachyos-gekko", "path": "/", "fstype": "ext4"},
        "fields": {"used_percent": 63.5, "free": 128849018880},
        "timestamp": 1721664000,
    },
    {
        "name": "net",
        "tags": {"host": "cachyos-gekko", "interface": "wlan0"},
        "fields": {"bytes_recv": 1048576000, "bytes_sent": 209715200},
        "timestamp": 1721664000,
    },
    {
        "name": "sensors",
        "tags": {"host": "cachyos-gekko", "chip": "coretemp-isa-0000", "feature": "package_id_0"},
        "fields": {"temp_input": 52.0},
        "timestamp": 1721664000,
    },
    {
        "name": "nvidia_gpu",
        "tags": {"host": "cachyos-gekko"},
        "fields": {"gpu_util": 12, "temp": 45.0, "mem_used": 1024},
        "timestamp": 1721664000,
    },
    {
        "name": "battery",
        "tags": {"host": "cachyos-gekko", "state": "discharging"},
        "fields": {"percentage": 87.0, "voltage": 11.4},
        "timestamp": 1721664000,
    },
]

# (field, name, native_unit, suggested_device_class, entity_category) per reference payload.
EXPECTED_NAMING: dict[str, list[tuple[str, str, str | None, str | None, str | None]]] = {
    # usage_idle has no 'percent' substring; unit-heuristic rework is explicitly Phase 4.
    "cpu": [("usage_idle", "Usage Idle", None, None, None)],
    "mem": [
        ("used_percent", "Used Percent", "%", None, None),
        ("used", "Used", None, None, None),
    ],
    "disk": [
        ("used_percent", "Disk Root Used Percent", "%", None, "diagnostic"),
        ("free", "Disk Root Free", None, None, "diagnostic"),
    ],
    "net": [
        ("bytes_recv", "Wlan0 Bytes Received", "B", None, None),
        ("bytes_sent", "Wlan0 Bytes Sent", "B", None, None),
    ],
    "sensors": [("temp_input", "CPU Package Temperature", "\u00b0C", "temperature", None)],
    "nvidia_gpu": [
        ("gpu_util", "GPU Utilization", None, None, None),
        ("temp", "Temperature", "\u00b0C", "temperature", None),
        ("mem_used", "Memory Used", None, None, None),
    ],
    "battery": [
        ("percentage", "Discharging Percentage", "%", "battery", None),
        ("voltage", "Discharging Voltage", "V", "voltage", None),
    ],
}


@pytest.mark.parametrize("measurement", sorted(EXPECTED_NAMING))
def test_reference_payload_names_and_metadata(measurement: str) -> None:
    """ROADMAP Phase 3: parametrized naming across all 7 reference payloads."""
    parser = TelegrafParser()
    payload = next(p for p in REFERENCE_PAYLOADS if p["name"] == measurement)

    descriptors = parser.parse(json.dumps(payload))

    by_field = {descriptor.field: descriptor for descriptor in descriptors}
    expected_fields = [field for field, *_ in EXPECTED_NAMING[measurement]]
    assert sorted(by_field) == sorted(expected_fields)
    for field, name, unit, device_class, category in EXPECTED_NAMING[measurement]:
        descriptor = by_field[field]
        assert descriptor.name == name, field
        assert descriptor.native_unit == unit, field
        assert descriptor.suggested_device_class == device_class, field
        assert descriptor.entity_category == category, field


def test_unknown_measurement_falls_back_to_generic_without_raising(caplog) -> None:
    """ROADMAP Phase 3: unrecognized measurement falls back quietly (DEBUG)."""
    parser = TelegrafParser()
    payload = {
        "name": "custom_plugin",
        "tags": {"host": "h1"},
        "fields": {"watts": 12.5},
        "timestamp": 1721664000,
    }

    with caplog.at_level(logging.DEBUG, logger="custom_components.telegraf_mqtt.parser"):
        descriptors = parser.parse(json.dumps(payload))

    assert [(d.field, d.value, d.name) for d in descriptors] == [("watts", 12.5, "Watts")]
    fallback_records = [r for r in caplog.records if "custom_plugin" in r.getMessage()]
    assert fallback_records
    assert all(r.levelno == logging.DEBUG for r in fallback_records)
    assert not [r for r in caplog.records if r.levelno >= logging.WARNING]


def test_system_load_and_process_counts_are_diagnostic() -> None:
    """SPEC.md entity categories: load averages/process counts/uptime are DIAGNOSTIC."""
    parser = TelegrafParser()
    payload = {
        "name": "system",
        "tags": {"host": "h1"},
        "fields": {
            "load1": 0.42,
            "load5": 0.31,
            "load15": 0.27,
            "processes_forked": 12345,
            "uptime": 987654,
            "n_users": 2,
        },
        "timestamp": 1721664000,
    }

    by_field = {d.field: d for d in parser.parse(json.dumps(payload))}

    for field in ("load1", "load5", "load15", "processes_forked", "uptime"):
        assert by_field[field].entity_category == "diagnostic", field
    assert by_field["n_users"].entity_category is None
