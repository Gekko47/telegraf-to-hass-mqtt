"""Generic Telegraf JSON parser."""

from __future__ import annotations

import logging
import re
from collections.abc import Mapping
from typing import Any

from ..models import MetricDescriptor, MetricValue, frozen_tags
from ..naming import resolve_entity_category, resolve_name

_LOGGER = logging.getLogger(__name__)

_SLUG_RE = re.compile(r"[^a-z0-9]+")

_TOTAL_INCREASING_FIELDS = {"bytes_recv", "bytes_sent", "uptime"}


def parse_generic_payload(payload: Mapping[str, Any]) -> list[MetricDescriptor]:
    """Parse a Telegraf JSON payload using generic fallback rules."""
    measurement = payload.get("name")
    tags = payload.get("tags")
    fields = payload.get("fields")
    timestamp = payload.get("timestamp")

    if not isinstance(measurement, str) or not isinstance(tags, Mapping) or not isinstance(fields, Mapping):
        _LOGGER.debug("Unsupported Telegraf payload shape")
        return []

    try:
        timestamp_float = float(timestamp)
    except (TypeError, ValueError):
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

        descriptors.append(
            MetricDescriptor(
                unique_key=build_unique_key(measurement, clean_tags, field),
                measurement=measurement,
                tags=frozen_tags(clean_tags),
                field=field,
                value=value,
                timestamp=timestamp_float,
                name=resolve_name(measurement, clean_tags, field),
                native_unit=infer_native_unit(field),
                suggested_device_class=infer_device_class(measurement, field),
                suggested_state_class=infer_state_class(field, value),
                entity_category=resolve_entity_category(measurement, field),
            )
        )

    return descriptors


def build_unique_key(measurement: str, tags: Mapping[str, str], field: str) -> str:
    """Build the deterministic parser-level unique key for a metric field."""
    parts = [measurement]
    parts.extend(value for key, value in sorted(tags.items()) if key != "host")
    parts.append(field)
    return "_".join(_slugify(part) for part in parts if part)


def build_fallback_name(measurement: str, tags: Mapping[str, str], field: str) -> str:
    """Build the Phase 1 raw fallback name for a metric field."""
    parts = [measurement]
    parts.extend(value for key, value in sorted(tags.items()) if key != "host")
    parts.append(field)
    return " ".join(_titleize(part) for part in parts if part)


def infer_native_unit(field: str) -> str | None:
    """Infer a basic native unit from a field name."""
    field_lower = field.lower()
    if "percent" in field_lower or field_lower == "percentage":
        return "%"
    if "temp_input" in field_lower or "temp" in field_lower:
        return "°C"
    if field_lower in {"bytes_recv", "bytes_sent"}:
        return "B"
    if "fan_input" in field_lower:
        return "RPM"
    if "voltage" in field_lower:
        return "V"
    if "energy_rate" in field_lower:
        return "W"
    if "energy" in field_lower:
        return "Wh"
    if "time_to_empty" in field_lower:
        return "h"
    if "uptime" in field_lower:
        return "s"
    return None


def infer_device_class(measurement: str, field: str) -> str | None:
    """Infer a basic suggested device class from a field name."""
    field_lower = field.lower()
    if "temp_input" in field_lower or "temp" in field_lower:
        return "temperature"
    if "voltage" in field_lower:
        return "voltage"
    if "energy_rate" in field_lower:
        return "power"
    if "energy" in field_lower:
        return "energy"
    if measurement == "battery" and field_lower == "percentage":
        return "battery"
    return None


def infer_state_class(field: str, value: MetricValue) -> str | None:
    """Infer a basic suggested state class from field/value shape."""
    if isinstance(value, bool) or isinstance(value, str):
        return None
    if field in _TOTAL_INCREASING_FIELDS:
        return "total_increasing"
    return "measurement"


def _slugify(value: str) -> str:
    if value == "/":
        return "root"
    slug = _SLUG_RE.sub("_", value.lower()).strip("_")
    return slug or "unknown"


def _titleize(value: str) -> str:
    return " ".join(word.capitalize() for word in _SLUG_RE.split(value) if word)
