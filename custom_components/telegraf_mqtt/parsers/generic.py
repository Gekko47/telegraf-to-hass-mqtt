"""Generic Telegraf JSON parser."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

from ..models import MetricDescriptor, MetricValue, frozen_tags
from ..naming import (
    resolve_entity_category,
    resolve_translation,
)
from .static import static_cleanup_policy

_LOGGER = logging.getLogger(__name__)

_SLUG_RE = re.compile(r"[^a-z0-9]+")

# Field names whose values are monotonically increasing counters. The
# recorder's ``state_class=total_increasing`` contract is that the value
# only ever grows (or wraps on a reset) -- these are the field-name
# substrings we accept as that shape. Anything else falls through to the
# default ``measurement`` (gauge) class.
_TOTAL_INCREASING_FIELDS: frozenset[str] = frozenset(
    {
        # Legacy Phase-4 entries: net bytes in / out, system uptime.
        "bytes_recv",
        "bytes_sent",
        "uptime",
        # diskio read/write byte counters.
        "read_bytes",
        "write_bytes",
        # Generic IO byte counters used by docker, net, etc.
        "bytes_read",
        "bytes_written",
        "rx_bytes",
        "tx_bytes",
        # Swap in / out (bytes since boot).
        "in",
        "out",
        # wireless packet counters (total_increasing).
        "nwid",
        "crypt",
        "frag",
        "retry",
        "misc",
        "missed_beacon",
    }
)

# Substring markers that mark a field as a byte counter regardless of the
# exact suffix (``_bytes``, ``bytes_*``, ``_bytes_*``).
_BYTE_FIELD_MARKERS: tuple[str, ...] = (
    "_bytes",
    "bytes_",
)

# Substring markers for time / duration fields expressed in milliseconds.
_MS_FIELD_MARKERS: tuple[str, ...] = (
    "response_ms",
    "_time_ms",
    "latency_ms",
    "duration_ms",
    # diskio time fields (Telegraf uses bare names without _ms suffix)
    "read_time",
    "write_time",
    "io_time",
    "weighted_io_time",
)

# Substring markers for fields expressed in seconds (durations).
_SECONDS_FIELD_MARKERS: tuple[str, ...] = (
    "response_time",
    "uptime",
    "duration_s",
)

# Measurements whose every field is treated as a byte counter unless a
# non-byte marker is present. Covers the memory / disk / swap / diskio /
# docker / net_io / nvidia_gpu / smart family. The heuristic is "if the
# measurement is one of these and the field is numeric, treat as a byte
# counter" because every field in that family is documented as bytes in
# Telegraf's input-plugin docs.
_BYTE_MEASUREMENT_OVERRIDES: frozenset[str] = frozenset(
    {
        "mem",
        "swap",
        "disk",
        "diskio",
        "docker_container_mem",
        "docker_container_blkio",
        "nvidia_gpu",
        "zfs_pool",
        "zfs_dataset",
    }
)

# Per-measurement hints that override the field-name-based device class.
_PERCENT_MEASUREMENT_OVERRIDES: frozenset[str] = frozenset({"mem", "swap", "battery"})

# Field names on a byte-measurement that are bytes (the rest are percent
# or string fields and stay out of the byte branch). Telegraf's mem /
# swap / disk plugins document every field in this list as bytes; the
# percent / string fields (used_percent, available_percent) are excluded
# by the explicit exclude set below.
_BYTE_MEASUREMENT_INCLUDE: frozenset[str] = frozenset(
    {
        "used",
        "free",
        "total",
        "available",
        "cached",
        "buffered",
        "shared",
        "high_free",
        "high_total",
        "low_free",
        "low_total",
        "in",
        "out",
    }
)

# Field names on a byte-measurement that are *not* bytes (e.g. percent).
_BYTE_MEASUREMENT_EXCLUDE: frozenset[str] = frozenset(
    {
        "used_percent",
        "available_percent",
    }
)

# --------------------------------------------------------------------------
# Tag-based unit/device_class mapping registry (Option A).
#
# Some Telegraf plugins (e.g. ipmi_sensor) put the unit in a tag rather
# than the field name. This registry allows per-measurement tag-value
# mappings to override the field-name-based inference.
#
# Structure: measurement_name -> { tag_name -> { tag_value -> (unit, device_class) } }
# --------------------------------------------------------------------------
_TAG_UNIT_MAPPINGS: dict[str, dict[str, dict[str, tuple[str | None, str | None]]]] = {
    "ipmi_sensor": {
        "unit": {
            "degrees_c": ("\u00b0C", "temperature"),
            "degrees_celsius": ("\u00b0C", "temperature"),
            "celsius": ("\u00b0C", "temperature"),
            "rpm": ("RPM", None),  # HA has no fan device_class
            "volts": ("V", "voltage"),
            "watts": ("W", "power"),
            "amps": ("A", "current"),
            "percent": ("%", None),
        }
    },
    # Future measurements with tag-based units can be added here
}


def _apply_tag_unit_mapping(
    measurement: str,
    field: str,
    tags: Mapping[str, str] | None,
    native_unit: str | None,
    device_class: str | None,
) -> tuple[str | None, str | None]:
    """Apply tag-based unit/device_class mapping if available for this measurement."""
    if not tags or measurement not in _TAG_UNIT_MAPPINGS:
        return native_unit, device_class

    mapping = _TAG_UNIT_MAPPINGS[measurement]
    for tag_name, value_map in mapping.items():
        tag_value = tags.get(tag_name, "").lower()
        if tag_value in value_map:
            mapped_unit, mapped_dc = value_map[tag_value]
            # Tag mapping takes precedence over field-name inference
            return mapped_unit, mapped_dc

    return native_unit, device_class


def parse_generic_payload(payload: Mapping[str, Any]) -> list[MetricDescriptor]:
    """Parse a Telegraf JSON payload using generic fallback rules.

    Phase 9: the resolved display ``name`` is no longer stored on the
    descriptor. The parser sets ``translation_key`` and
    ``translation_placeholders``; the entity layer reads them and HA
    formats them via translations/en.json.

    Phase 10: every descriptor carries ``cleanup_policy`` (a ``Literal``)
    and ``platform_hint`` (also a ``Literal``). The platform hint defaults
    to ``"auto"`` and is overridden by ``field_overrides[platform]`` in
    the registry's ``_apply_overrides`` step. Setting it to ``"none"``
    marks a field as excluded and the registry drops it before entity
    routing; setting it to ``"sensor"`` or ``"binary_sensor"`` forces
    the platform split regardless of the value's Python type.
    """
    measurement = payload.get("name")
    tags = payload.get("tags")
    fields = payload.get("fields")
    timestamp = payload.get("timestamp")

    if not isinstance(measurement, str) or not isinstance(tags, Mapping) or not isinstance(fields, Mapping):
        _LOGGER.debug("Unsupported Telegraf payload shape")
        return []

    if timestamp is None:
        _LOGGER.debug("Unsupported Telegraf payload shape")
        return []

    try:
        timestamp_float = float(timestamp)
    except (TypeError, ValueError):  # fmt: skip
        _LOGGER.debug("Unsupported Telegraf payload shape")
        return []

    clean_tags = {str(key): str(value) for key, value in tags.items()}
    descriptors: list[MetricDescriptor] = []
    for field, value in fields.items():
        if not isinstance(field, str):
            _LOGGER.debug("Dropping Telegraf field with unsupported name: %r", field)
            continue

        if not isinstance(value, (int, float, str, bool)) or value is None:
            _LOGGER.debug("Dropping unsupported Telegraf field %s.%s", measurement, field)
            continue

        translation_key, placeholders = resolve_translation(measurement, clean_tags, field)
        # Infer base unit/device_class from field name
        native_unit = infer_native_unit(field, measurement)
        device_class = infer_device_class(measurement, field)
        # Apply tag-based override if available (e.g. ipmi_sensor.unit tag)
        native_unit, device_class = _apply_tag_unit_mapping(measurement, field, clean_tags, native_unit, device_class)
        descriptors.append(
            MetricDescriptor(
                unique_key=build_unique_key(measurement, clean_tags, field),
                measurement=measurement,
                tags=frozen_tags(clean_tags),
                field=field,
                value=value,
                timestamp=timestamp_float,
                native_unit=native_unit,
                suggested_device_class=device_class,
                suggested_state_class=infer_state_class(field, value),
                entity_category=resolve_entity_category(measurement, field),
                # Phase 6: static system metadata (CPU model, vendor id, n_cpus,
                # uptime_format, ...) is never cleaned up; everything else is AUTO.
                # The literal strings come from const.py; static.py is the
                # single place that decides the policy.
                cleanup_policy=static_cleanup_policy(measurement, field),  # type: ignore[arg-type]
                device_id=clean_tags.get("host", ""),
                translation_key=translation_key,
                translation_placeholders=MappingProxyType(dict(placeholders)),
                # Phase 10: platform_hint is "auto" by default; the registry
                # applies the user's field_override (if any) at update time.
                platform_hint="auto",
            )
        )

    return descriptors


def build_unique_key(measurement: str, tags: Mapping[str, str], field: str) -> str:
    """Build the deterministic parser-level unique key for a metric field."""
    parts = [measurement]
    parts.extend(value for key, value in sorted(tags.items()) if key != "host")
    parts.append(field)
    return "_".join(_slugify(part) for part in parts if part)


def infer_native_unit(field: str, measurement: str | None = None) -> str | None:
    """Infer a basic native unit from a field name.

    Phase 11: extended to cover the full Telegraf surface area so the
    per-type auto-formatting rules in ``units.py`` can pick the right
    precision and HA's unit conversion can pick the right suffix.

    When ``measurement`` is provided, the per-measurement byte override
    kicks in: e.g. ``mem.used`` -> ``B`` even though the field name does
    not contain a literal byte marker. The legacy single-argument form
    (no measurement) stays supported for backwards compatibility with
    the test suite.
    """
    field_lower = field.lower()
    if "percent" in field_lower or field_lower == "percentage":
        return "%"
    # diskio io_util is documented as a 0-1 fraction (not percent)
    # but represents utilization percentage
    if measurement == "diskio" and field_lower == "io_util":
        return "%"
    if "temp_input" in field_lower or "temp" in field_lower or field_lower == "temperature":
        return "\u00b0C"
    if _is_byte_field(measurement or "", field):
        return "B"
    if any(marker in field_lower for marker in _MS_FIELD_MARKERS):
        return "ms"
    if any(marker in field_lower for marker in _SECONDS_FIELD_MARKERS):
        return "s"
    if "fan_input" in field_lower or field_lower == "rpm":
        return "RPM"
    if "voltage" in field_lower:
        return "V"
    if "energy_rate" in field_lower or field_lower == "power":
        return "W"
    if "energy" in field_lower:
        return "Wh"
    if "time_to_empty" in field_lower or "time_to_full" in field_lower:
        return "h"
    if field_lower in {"level", "noise", "signal", "signal_dbm"}:
        return "dBm"
    if field_lower in {"link", "link_quality"}:
        return "%"
    if field_lower == "bitrate":
        return "Mbit/s"
    if field_lower == "frequency":
        return "MHz"
    if field_lower == "power_on_hours":
        return "h"
    if field_lower == "frequency_hz":
        return "Hz"
    return None


def infer_device_class(measurement: str, field: str) -> str | None:
    """Infer a basic suggested device class from a field name.

    Phase 11: extended with ``data_size`` for byte counters, ``duration``
    for time fields, ``signal_strength`` for wireless, and ``power`` for
    the ``power``-named field used by some IPMI / PSU sensors.
    """
    field_lower = field.lower()
    if "temp_input" in field_lower or "temp" in field_lower or field_lower == "temperature":
        return "temperature"
    if "voltage" in field_lower:
        return "voltage"
    if "energy_rate" in field_lower or field_lower == "power":
        return "power"
    if "energy" in field_lower:
        return "energy"
    if _is_byte_field(measurement, field):
        return "data_size"
    if _is_duration_field(measurement, field):
        return "duration"
    if field_lower in {"level", "noise", "signal", "signal_dbm"}:
        return "signal_strength"
    if measurement == "battery" and field_lower == "percentage":
        return "battery"
    return None


def infer_state_class(field: str, value: MetricValue) -> str | None:
    """Infer a basic suggested state class from field/value shape.

    Phase 11: byte counters and second-precision durations are
    monotonically increasing, so they map to ``total_increasing``.
    Millisecond durations (latency) are gauges, not counters. Everything
    numeric falls through to ``measurement`` (gauge).
    """
    if isinstance(value, (bool, str)):
        return None
    field_lower = field.lower()
    if field in _TOTAL_INCREASING_FIELDS:
        return "total_increasing"
    if any(marker in field_lower for marker in _BYTE_FIELD_MARKERS):
        return "total_increasing"
    if any(marker in field_lower for marker in _MS_FIELD_MARKERS):
        return "measurement"
    if any(marker in field_lower for marker in _SECONDS_FIELD_MARKERS):
        return "total_increasing"
    return "measurement"


def _is_byte_field(measurement: str, field: str) -> bool:
    """Return True when a field is documented as a byte counter.

    Two routes qualify: the field name carries a byte marker
    (``_bytes`` / ``bytes_`` / ``bytes_recv`` / ...) or the field is on
    a known-byte measurement and its name is in the include list
    (``mem.used`` / ``swap.used`` / ``disk.free`` / ...). The second
    route is what catches fields like ``mem.used`` that don't carry a
    literal byte marker but are documented as bytes in Telegraf's
    input-plugin docs.
    """
    field_lower = field.lower()
    if any(marker in field_lower for marker in _BYTE_FIELD_MARKERS):
        return True
    return (
        measurement in _BYTE_MEASUREMENT_OVERRIDES
        and field_lower in _BYTE_MEASUREMENT_INCLUDE
        and field_lower not in _BYTE_MEASUREMENT_EXCLUDE
    )


def _is_duration_field(measurement: str, field: str) -> bool:
    """Return True when a field is a duration / latency."""
    field_lower = field.lower()
    return any(marker in field_lower for marker in (*_MS_FIELD_MARKERS, *_SECONDS_FIELD_MARKERS))


def _slugify(value: str) -> str:
    if value == "/":
        return "root"
    slug = _SLUG_RE.sub("_", value.lower()).strip("_")
    return slug or "unknown"
