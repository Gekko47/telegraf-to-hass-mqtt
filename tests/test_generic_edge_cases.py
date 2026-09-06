"""Edge-case tests for parser.py and parsers/generic.py branch coverage."""

from __future__ import annotations

import json

from custom_components.telegraf_mqtt.parser import TelegrafParser
from custom_components.telegraf_mqtt.parsers import base
from custom_components.telegraf_mqtt.parsers.generic import (
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


def test_slugify_maps_root_path_and_blank_values() -> None:
    assert build_unique_key("disk", {"path": "/"}, "free") == "disk_root_free"
    assert build_unique_key("m", {"t": "###"}, "f") == "m_unknown_f"


def test_infer_native_unit_covers_every_branch() -> None:
    cases = {
        "usage_percent": "%",
        "percentage": "%",
        "temp_input": "\u00b0C",
        "temp": "\u00b0C",
        "temperature": "\u00b0C",
        "bytes_recv": "B",
        "bytes_sent": "B",
        "read_bytes": "B",
        "write_bytes": "B",
        "rx_bytes": "B",
        "tx_bytes": "B",
        "fan_input": "RPM",
        "rpm": "RPM",
        "voltage": "V",
        "energy_rate": "W",
        "energy": "Wh",
        "power": "W",
        "time_to_empty": "h",
        "uptime": "s",
        "average_response_ms": "ms",
        "response_time": "s",
        "level": "dBm",
        "noise": "dBm",
        "bitrate": "Mbit/s",
        "frequency": "MHz",
        "power_on_hours": "h",
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
    # Phase 11: byte / duration / signal / power additions.
    assert infer_device_class("mem", "used") == "data_size"
    assert infer_device_class("diskio", "read_bytes") == "data_size"
    assert infer_device_class("net", "bytes_recv") == "data_size"
    assert infer_device_class("swap", "used") == "data_size"
    assert infer_device_class("system", "uptime") == "duration"
    assert infer_device_class("ping", "average_response_ms") == "duration"
    assert infer_device_class("ping", "response_time") == "duration"
    assert infer_device_class("wireless", "level") == "signal_strength"
    assert infer_device_class("smart", "temp_c") == "temperature"
    assert infer_device_class("ipmi_sensor", "value") is None  # unit is a tag, not a field


def test_infer_state_class_shape_rules() -> None:
    assert infer_state_class("uptime", 500) == "total_increasing"
    assert infer_state_class("usage_idle", 88.4) == "measurement"
    assert infer_state_class("link_up", True) is None
    assert infer_state_class("label", "wifi") is None
    # Phase 11: byte counters and second-precision durations are counters.
    assert infer_state_class("read_bytes", 1024) == "total_increasing"
    assert infer_state_class("write_bytes", 1024) == "total_increasing"
    assert infer_state_class("rx_bytes", 1024) == "total_increasing"
    assert infer_state_class("tx_bytes", 1024) == "total_increasing"
    # Millisecond durations (latency) are gauges.
    assert infer_state_class("average_response_ms", 23.0) == "measurement"
    # Swap in/out are byte counters.
    assert infer_state_class("in", 1024) == "total_increasing"
    assert infer_state_class("out", 1024) == "total_increasing"


def test_infer_state_class_string_and_bool_return_none() -> None:
    assert infer_state_class("uptime", "string") is None
    assert infer_state_class("usage_idle", True) is None


def test_infer_native_unit_wireless_branches() -> None:
    """The wireless plugin emits ``link`` (percent) and ``frequency_hz``
    (Hz); both branch through infer_native_unit. Phase 11 added them; the
    edge-case test pins the (previously uncovered) fallback lines.
    """
    assert infer_native_unit("link") == "%"
    assert infer_native_unit("link_quality") == "%"
    assert infer_native_unit("frequency_hz") == "Hz"


def test_infer_state_class_bytes_marker_branches() -> None:
    """Fields whose name carries a byte marker but are not in the
    hard-coded ``_TOTAL_INCREASING_FIELDS`` set still resolve to
    ``total_increasing`` via the substring branch. Same for the
    ``response_time``/``duration_s`` second-precision markers.
    """
    # Bytes-by-marker.
    assert infer_state_class("io_service_bytes_recursive_total", 1) == "total_increasing"
    # Duration-by-marker, seconds branch.
    assert infer_state_class("duration_s", 1) == "total_increasing"


def test_catchall_docker_payload_handler_is_routed() -> None:
    """The ``docker`` catch-all handler is the fallback for any docker_*
    measurement not enumerated as a primary parser (e.g. ``docker_swarm``,
    ``docker_disk_usage``). Pin it so the dispatcher table is exhaustive.
    """
    from custom_components.telegraf_mqtt.parser import TelegrafParser

    handler = TelegrafParser._PARSERS["docker"]
    payload = {
        "name": "docker",
        "tags": {"host": "h1", "container_name": "pihole"},
        "fields": {"tasks_desired": 3},
        "timestamp": 1,
    }
    descriptors = list(handler(payload))
    assert len(descriptors) == 1
    assert descriptors[0].measurement == "docker"
    assert descriptors[0].field == "tasks_desired"


# ---------------------------------------------------------------------------
# Adversarial input suite (Phase 10 follow-on).
#
# The fault-isolation contract: every per-measurement handler must
# return [] for malformed-but-JSON-valid payloads instead of raising.
# Telegraf can't *generate* these inputs in practice, but a misbehaving
# producer or a corrupted buffer can, and a single ``KeyError`` from
# ``payload["fields"]["x"]`` should not take down the MQTT subscription.
# The shapes below are the ones the user explicitly called out:
# numbers as tags, deeply nested fields, huge arrays, NaN / +/-inf
# values, NUL bytes in field names, and unicode whitespace.
# ---------------------------------------------------------------------------


_MEASUREMENT_HANDLERS = (
    "battery",
    "cpu",
    "disk",
    "diskio",
    "docker_container_cpu",
    "docker_container_mem",
    "docker_container_net",
    "docker_container_blkio",
    "docker_container_status",
    "http_response",
    "interrupts",
    "ipmi_sensor",
    "kernel",
    "kernel_vmstat",
    "mem",
    "net",
    "net_response",
    "nvidia_gpu",
    "ping",
    "processes",
    "sensors",
    "smart",
    "soft_interrupts",
    "swap",
    "system",
    "wireless",
    "zfs",
    "zfs_pool",
    "zfs_dataset",
    "generic",  # the fallback path
)


def _parse_for(measurement: str, payload: dict):  # type: ignore[no-untyped-def]
    """Dispatch a payload to the per-measurement handler.

    The parser layer's ``parse()`` method also drops envelopes that
    don't match the top-level shape, but that test belongs to
    test_parser.py. Here we want to hit the *handler* surface
    directly, so we route via the ``TelegrafParser._PARSERS`` table
    (or the generic fallback) just like ``parse()`` does internally.
    """
    from custom_components.telegraf_mqtt.parser import TelegrafParser

    handler = TelegrafParser._PARSERS.get(measurement)
    if handler is None:
        from custom_components.telegraf_mqtt.parsers.generic import parse_generic_payload

        handler = parse_generic_payload
    return list(handler(payload))


def test_every_handler_swallows_numbers_as_tags() -> None:
    """Tags are coerced to ``str`` in ``parse_generic_payload``; a
    numeric or list value must not raise inside the handler.
    """
    payload = {
        "name": "cpu",
        "tags": {"host": "h1", "port": 80, "rate": 1.5, "weird": [1, 2, 3]},
        "fields": {"x": 1.0},
        "timestamp": 1,
    }
    for measurement in _MEASUREMENT_HANDLERS:
        result = _parse_for(measurement, payload)
        # Result is either 0 (host tag isn't routed) or 1 descriptor;
        # the contract is "doesn't raise", not "returns N descriptors".
        assert isinstance(result, list)


def test_every_handler_swallows_deeply_nested_field_values() -> None:
    """A field value that is a nested dict or list is dropped at the
    generic path with a DEBUG log. The handler must not raise trying
    to coerce it.
    """
    payload = {
        "name": "cpu",
        "tags": {"host": "h1"},
        "fields": {
            "x": {"a": {"b": {"c": 1}}},
            "y": [[[[1, 2, 3]]]],
            "z": {"nested": ["list", "of", "things"]},
        },
        "timestamp": 1,
    }
    for measurement in _MEASUREMENT_HANDLERS:
        result = _parse_for(measurement, payload)
        assert isinstance(result, list)
        for descriptor in result:
            assert isinstance(descriptor.value, (int, float, str, bool)) or descriptor.value is None


def test_every_handler_swallows_huge_field_arrays() -> None:
    """A field value that is a 10k-element list exercises the size
    budget without exhausting the test runner.
    """
    huge = list(range(10_000))
    payload = {
        "name": "cpu",
        "tags": {"host": "h1"},
        "fields": {"big": huge, "small": 1.0},
        "timestamp": 1,
    }
    for measurement in _MEASUREMENT_HANDLERS:
        result = _parse_for(measurement, payload)
        assert isinstance(result, list)
        assert any(descriptor.field == "small" for descriptor in result) or not result


def test_every_handler_swallows_nan_and_infinity_values() -> None:
    """NaN and +/-inf are valid JSON values; the handler must not raise
    trying to build a descriptor.
    """
    import math

    payload = {
        "name": "cpu",
        "tags": {"host": "h1"},
        "fields": {
            "nan_value": math.nan,
            "pos_inf": math.inf,
            "neg_inf": -math.inf,
            "normal": 1.0,
        },
        "timestamp": 1,
    }
    for measurement in _MEASUREMENT_HANDLERS:
        result = _parse_for(measurement, payload)
        assert isinstance(result, list)


def test_every_handler_swallows_non_string_field_names() -> None:
    """Integer / None / boolean / list field names are dropped at the
    generic path. A handler must not assume ``field`` is a string.
    """
    payload = {
        "name": "cpu",
        "tags": {"host": "h1"},
        "fields": {
            1: "int key",
            None: "none key",
            "str_only": 1.0,
        },
        "timestamp": 1,
    }
    for measurement in _MEASUREMENT_HANDLERS:
        result = _parse_for(measurement, payload)
        assert isinstance(result, list)
        for descriptor in result:
            assert isinstance(descriptor.field, str)
            assert descriptor.field == "str_only"


def test_every_handler_swallows_unicode_weirdness() -> None:
    """NUL bytes, RTL marks, zero-width spaces, and full-width digits
    in field names. The handler must not raise trying to slugify.
    """
    payload = {
        "name": "cpu",
        "tags": {"host": "h1"},
        "fields": {
            "\x00bad": 1.0,
            "\u200bzerowidth": 1.0,
            "\u202e_rtl": 1.0,
            "\uff11fullwidth_one": 1.0,
            "ok": 1.0,
        },
        "timestamp": 1,
    }
    for measurement in _MEASUREMENT_HANDLERS:
        result = _parse_for(measurement, payload)
        assert isinstance(result, list)



def test_apply_tag_unit_mapping_edge_cases() -> None:
    from custom_components.telegraf_mqtt.parsers.generic import _apply_tag_unit_mapping

    # Case 1: tags is None -> early return
    unit, dc = _apply_tag_unit_mapping('ipmi_sensor', 'value', None, 'ms', 'duration')
    assert unit == 'ms' and dc == 'duration'

    # Case 2: tags is empty dict -> early return
    unit, dc = _apply_tag_unit_mapping('ipmi_sensor', 'value', {}, 'ms', 'duration')
    assert unit == 'ms' and dc == 'duration'

    # Case 3: measurement not in _TAG_UNIT_MAPPINGS -> early return
    unit, dc = _apply_tag_unit_mapping('cpu', 'value', {'unit': 'degrees_c'}, 'ms', 'duration')
    assert unit == 'ms' and dc == 'duration'

    # Case 4: measurement in _TAG_UNIT_MAPPINGS but tag value doesn't match -> falls through
    unit, dc = _apply_tag_unit_mapping('ipmi_sensor', 'value', {'unit': 'unknown_unit'}, 'ms', 'duration')
    assert unit == 'ms' and dc == 'duration'

    # Case 5: tag name exists but value not in value_map -> falls through
    unit, dc = _apply_tag_unit_mapping('ipmi_sensor', 'value', {'unit': 'degrees_c', 'other': 'foo'}, 'ms', 'duration')
    # Should match 'degrees_c' key and return mapped values
    assert unit == '\u00b0C' and dc == 'temperature'

    # Case 6: tag name not 'unit' (only 'unit' tag is mapped for ipmi_sensor)
    unit, dc = _apply_tag_unit_mapping('ipmi_sensor', 'value', {'sensor_type': 'degrees_c'}, 'ms', 'duration')
    # Should not match because only 'unit' tag is in the mapping
    assert unit == 'ms' and dc == 'duration'


def test_binary_sensor_coercion_edge_cases() -> None:
    from custom_components.telegraf_mqtt.models import coerce_to_bool

    # Standard true/false
    assert coerce_to_bool(True) is True
    assert coerce_to_bool(False) is False
    assert coerce_to_bool(1) is True
    assert coerce_to_bool(0) is False
    assert coerce_to_bool(-1) is True
    assert coerce_to_bool(-0.0) is False

    # String true values (case insensitive)
    assert coerce_to_bool('true') is True
    assert coerce_to_bool('True') is True
    assert coerce_to_bool('TRUE') is True
    assert coerce_to_bool('1') is True
    assert coerce_to_bool('yes') is True
    assert coerce_to_bool('on') is True

    # String false values (new - case insensitive)
    assert coerce_to_bool('false') is False
    assert coerce_to_bool('False') is False
    assert coerce_to_bool('FALSE') is False
    assert coerce_to_bool('0') is False
    assert coerce_to_bool('no') is False
    assert coerce_to_bool('off') is False

    # Other strings -> True
    assert coerce_to_bool('something') is True
    assert coerce_to_bool('') is False  # empty string

    # Whitespace-only strings -> False (same as empty after strip)
    assert coerce_to_bool('  ') is False  # whitespace-only is False after strip


def test_infer_native_unit_io_util_diskio() -> None:
    from custom_components.telegraf_mqtt.parsers.generic import infer_native_unit
    # io_util is a special case for diskio measurement
    assert infer_native_unit('io_util', 'diskio') == '%'
    # Other measurements don't get this special case
    assert infer_native_unit('io_util', 'cpu') is None


def test_infer_state_class_wireless_counters() -> None:
    from custom_components.telegraf_mqtt.parsers.generic import infer_state_class
    # Wireless packet counters should be total_increasing
    for field in ['nwid', 'crypt', 'frag', 'retry', 'misc', 'missed_beacon']:
        assert infer_state_class(field, 100.0) == 'total_increasing', f'{field} should be total_increasing'
    # But signal strength fields should be measurement
    assert infer_state_class('level', -45) == 'measurement'
    assert infer_state_class('noise', -90) == 'measurement'

def test_every_handler_swallows_extreme_field_counts() -> None:
    """A payload with 1000 valid fields still returns a list -- no
    recursion limit, no accidental quadratic growth.
    """
    fields = {f"f{i}": float(i) for i in range(1000)}
    payload = {
        "name": "cpu",
        "tags": {"host": "h1"},
        "fields": fields,
        "timestamp": 1,
    }
    for measurement in _MEASUREMENT_HANDLERS:
        result = _parse_for(measurement, payload)
        assert isinstance(result, list)
        assert len(result) == 1000
