"""Edge-case tests for parser.py and parsers/generic.py branch coverage."""

from __future__ import annotations

import json

from custom_components.telegraf_mqtt.parser import TelegrafParser
from custom_components.telegraf_mqtt.parsers import base
from custom_components.telegraf_mqtt.parsers.generic import (
    build_fallback_name,
    build_unique_key,
    infer_device_class,
    infer_native_unit,
    infer_state_class,
    parse_generic_payload,
)


def test_payload_parser_protocol_is_importable() -> None:
    """parsers/base.py defines the protocol every measurement parser satisfies."""
    assert callable(base.PayloadParser.__call__)


def test_parser_rejects_non_dict_json_and_missing_measurement() -> None:
    parser = TelegrafParser()

    assert parser.parse("[]") == []  # valid JSON, wrong shape
    assert parser.parse("null") == []
    assert parser.parse(json.dumps({"fields": {}, "timestamp": 1})) == []  # no name
    assert parser.parse(json.dumps({"name": 7, "fields": {}, "timestamp": 1})) == []


def test_generic_parser_rejects_incomplete_or_mistyped_envelopes() -> None:
    assert parse_generic_payload({"name": "x", "tags": {}, "timestamp": 1}) == []
    assert parse_generic_payload({"name": "x", "tags": "bad", "fields": {}, "timestamp": 1}) == []
    assert parse_generic_payload({"name": "x", "tags": {}, "fields": 5, "timestamp": 1}) == []
    assert parse_generic_payload({"name": "x", "tags": {}, "fields": {}, "timestamp": "abc"}) == []
    assert parse_generic_payload({"name": "x", "tags": {}, "fields": {}, "timestamp": None}) == []


def test_generic_parser_drops_non_string_field_names() -> None:
    descriptors = parse_generic_payload({"name": "x", "tags": {"host": "h"}, "fields": {1: 5, "ok": 6}, "timestamp": 1})

    assert [descriptor.field for descriptor in descriptors] == ["ok"]


def test_build_fallback_name_titleizes_measurement_tags_and_field() -> None:
    """Raw fallback naming titleizes slugs; alias polish arrives in Phase 3."""
    assert build_fallback_name("net", {"interface": "wlan0"}, "bytes_recv") == "Net Wlan0 Bytes Recv"
    assert build_fallback_name("disk", {"path": "/"}, "free") == "Disk Free"
    assert build_fallback_name("weird!!name", {}, "field_x") == "Weird Name Field X"


def test_slugify_maps_root_path_and_blank_values() -> None:
    assert build_unique_key("disk", {"path": "/"}, "free") == "disk_root_free"
    assert build_unique_key("m", {"t": "###"}, "f") == "m_unknown_f"


def test_infer_native_unit_covers_every_branch() -> None:
    cases = {
        "usage_percent": "%",
        "percentage": "%",
        "temp_input": "°C",
        "temp": "°C",
        "bytes_recv": "B",
        "bytes_sent": "B",
        "fan_input": "RPM",
        "voltage": "V",
        "energy_rate": "W",
        "energy": "Wh",
        "time_to_empty": "h",
        "uptime": "s",
        "something_else": None,
    }
    assert {field: infer_native_unit(field) for field in cases} == cases


def test_infer_device_class_covers_every_branch() -> None:
    assert infer_device_class("sensors", "temp_input") == "temperature"
    assert infer_device_class("battery", "voltage") == "voltage"
    assert infer_device_class("ups", "energy_rate") == "power"
    assert infer_device_class("ups", "energy") == "energy"
    assert infer_device_class("battery", "percentage") == "battery"
    assert infer_device_class("cpu", "usage_idle") is None


def test_infer_state_class_shape_rules() -> None:
    assert infer_state_class("uptime", 500) == "total_increasing"
    assert infer_state_class("usage_idle", 88.4) == "measurement"
    assert infer_state_class("link_up", True) is None
    assert infer_state_class("label", "wifi") is None
