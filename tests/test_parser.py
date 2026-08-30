from __future__ import annotations

import json
from types import MappingProxyType

from custom_components.telegraf_mqtt.models import MetricDescriptor
from custom_components.telegraf_mqtt.parser import TelegrafParser
from custom_components.telegraf_mqtt.parsers.generic import build_unique_key
from custom_components.telegraf_mqtt.translations_strings import format_translation

REFERENCE_PAYLOADS = [
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


def test_all_reference_payloads_produce_descriptors() -> None:
    parser = TelegrafParser()

    descriptors = [descriptor for payload in REFERENCE_PAYLOADS for descriptor in parser.parse(json.dumps(payload))]

    assert len(descriptors) == 13
    assert all(isinstance(descriptor, MetricDescriptor) for descriptor in descriptors)
    assert {descriptor.measurement for descriptor in descriptors} == {
        "battery",
        "cpu",
        "disk",
        "mem",
        "net",
        "nvidia_gpu",
        "sensors",
    }


def test_unique_key_excludes_host_and_sorts_other_tags() -> None:
    assert (
        build_unique_key(
            "disk",
            {"host": "one", "path": "/", "fstype": "ext4"},
            "used_percent",
        )
        == "disk_ext4_root_used_percent"
    )


def test_descriptor_generation_sets_units_state_classes_and_immutable_tags() -> None:
    parser = TelegrafParser()
    descriptors = parser.parse(
        json.dumps(
            {
                "name": "net",
                "tags": {"host": "host-a", "interface": "wlan0"},
                "fields": {"bytes_recv": 1048576000, "link_up": True, "label": "wifi"},
                "timestamp": 1721664000,
            }
        )
    )

    by_field = {descriptor.field: descriptor for descriptor in descriptors}
    assert by_field["bytes_recv"].native_unit == "B"
    assert by_field["bytes_recv"].suggested_state_class == "total_increasing"
    assert by_field["link_up"].suggested_state_class is None
    assert by_field["label"].suggested_state_class is None
    assert isinstance(by_field["bytes_recv"].tags, MappingProxyType)


def test_measurement_specific_naming_and_profile_categories() -> None:
    parser = TelegrafParser()

    sensors_descriptors = parser.parse(
        json.dumps(
            {
                "name": "sensors",
                "tags": {"host": "host-a", "chip": "coretemp-isa-0000", "feature": "package_id_0"},
                "fields": {"temp_input": 52.0},
                "timestamp": 1721664000,
            }
        )
    )
    disk_descriptors = parser.parse(
        json.dumps(
            {
                "name": "disk",
                "tags": {"host": "host-a", "path": "/", "fstype": "ext4"},
                "fields": {"used_percent": 63.5},
                "timestamp": 1721664000,
            }
        )
    )

    assert [format_translation(d.translation_key, dict(d.translation_placeholders)) for d in sensors_descriptors] == [
        "CPU Package Temperature"
    ]
    assert sensors_descriptors[0].entity_category is None
    assert disk_descriptors[0].entity_category == "diagnostic"


def test_name_resolution_excludes_host_leakage_and_resolves_field_aliases() -> None:
    parser = TelegrafParser()

    cpu_descriptors = parser.parse(
        json.dumps(
            {
                "name": "cpu",
                "tags": {"host": "host-a"},
                "fields": {"usage_idle": 88.4},
                "timestamp": 1721664000,
            }
        )
    )
    mem_descriptors = parser.parse(
        json.dumps(
            {
                "name": "mem",
                "tags": {"host": "host-a"},
                "fields": {"used_percent": 41.2, "used": 8589934592},
                "timestamp": 1721664000,
            }
        )
    )

    assert [format_translation(d.translation_key, dict(d.translation_placeholders)) for d in cpu_descriptors] == [
        "CPU Usage Idle"
    ]
    assert [format_translation(d.translation_key, dict(d.translation_placeholders)) for d in mem_descriptors] == [
        "Memory Used Percent",
        "Memory Used",
    ]


def test_name_resolution_normalizes_tag_value_whitespace_and_case_for_alias_lookup() -> None:
    parser = TelegrafParser()

    cpu_descriptors = parser.parse(
        json.dumps(
            {
                "name": "cpu",
                "tags": {"host": "host-a", "cpu": " CPU-total "},
                "fields": {"usage_idle": 88.4},
                "timestamp": 1721664000,
            }
        )
    )

    assert [format_translation(d.translation_key, dict(d.translation_placeholders)) for d in cpu_descriptors] == [
        "CPU Usage Idle"
    ]


def test_parser_drops_invalid_json_and_unsupported_field_shapes() -> None:
    parser = TelegrafParser()

    assert parser.parse("{") == []
    descriptors = parser.parse(
        json.dumps(
            {
                "name": "custom",
                "tags": {"host": "host-a"},
                "fields": {"ok": 1, "nested": {"bad": True}, "items": [1, 2]},
                "timestamp": 1721664000,
            }
        )
    )

    assert [descriptor.field for descriptor in descriptors] == ["ok"]
