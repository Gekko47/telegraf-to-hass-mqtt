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
from custom_components.telegraf_mqtt.translations_strings import format_translation

REFERENCE_PAYLOADS: list[dict] = [
    {"name": "cpu", "tags": {"host": "host-a"}, "fields": {"usage_idle": 88.4}, "timestamp": 1721664000},
    {
        "name": "mem",
        "tags": {"host": "host-a"},
        "fields": {"used_percent": 41.2, "used": 8589934592},
        "timestamp": 1721664000,
    },
    {
        "name": "disk",
        "tags": {"host": "host-a", "path": "/", "fstype": "ext4"},
        "fields": {"used_percent": 63.5, "free": 128849018880},
        "timestamp": 1721664000,
    },
    {
        "name": "net",
        "tags": {"host": "host-a", "interface": "wlan0"},
        "fields": {"bytes_recv": 1048576000, "bytes_sent": 209715200},
        "timestamp": 1721664000,
    },
    {
        "name": "sensors",
        "tags": {"host": "host-a", "chip": "coretemp-isa-0000", "feature": "package_id_0"},
        "fields": {"temp_input": 52.0},
        "timestamp": 1721664000,
    },
    {
        "name": "nvidia_gpu",
        "tags": {"host": "host-a"},
        "fields": {"gpu_util": 12, "temp": 45.0, "mem_used": 1024},
        "timestamp": 1721664000,
    },
    {
        "name": "battery",
        "tags": {"host": "host-a", "state": "discharging"},
        "fields": {"percentage": 87.0, "voltage": 11.4},
        "timestamp": 1721664000,
    },
]

# (field, expected_name, native_unit, suggested_device_class, entity_category) per reference payload.
# Names are produced by translations_strings.format_translation, which mirrors en.json.
EXPECTED_NAMING: dict[str, list[tuple[str, str, str | None, str | None, str | None]]] = {
    "cpu": [("usage_idle", "CPU Usage Idle", None, None, None)],
    "mem": [
        ("used_percent", "Memory Used Percent", "%", None, None),
        # Phase 11: ``mem.used`` is documented as bytes in Telegraf's
        # mem plugin, so the per-measurement byte override assigns
        # ``data_size`` / ``B`` to it.
        ("used", "Memory Used", "B", "data_size", None),
    ],
    "disk": [
        ("used_percent", "Disk Root Used Percent", "%", None, "diagnostic"),
        # Phase 11: same per-measurement byte override applies to disk.
        ("free", "Disk Root Free", "B", "data_size", "diagnostic"),
    ],
    "net": [
        # Phase 11: byte counters now also resolve to ``data_size`` so HA
        # picks the right unit-conversion + sparkline colours.
        ("bytes_recv", "wlan0 Bytes Received", "B", "data_size", None),
        ("bytes_sent", "wlan0 Bytes Sent", "B", "data_size", None),
    ],
    "sensors": [("temp_input", "CPU Package Temperature", "\u00b0C", "temperature", None)],
    "nvidia_gpu": [
        ("gpu_util", "GPU Utilization", None, None, None),
        ("temp", "GPU Temperature", "\u00b0C", "temperature", None),
        ("mem_used", "GPU Memory Used", None, None, None),
    ],
    "battery": [
        ("percentage", "Battery Percentage", "%", "battery", None),
        ("voltage", "Battery Voltage", "V", "voltage", None),
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
        rendered = format_translation(descriptor.translation_key, dict(descriptor.translation_placeholders))
        assert rendered == name, field
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

    assert [
        (d.field, d.value, format_translation(d.translation_key, dict(d.translation_placeholders))) for d in descriptors
    ] == [("watts", 12.5, "Watts")]
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
