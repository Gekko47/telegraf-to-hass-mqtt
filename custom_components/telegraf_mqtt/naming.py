"""Phase 3 naming and alias resolution for telegraf_mqtt."""

from __future__ import annotations

from collections.abc import Mapping

from .heuristics import (
    ENTITY_CATEGORY_DIAGNOSTIC,
    FIELD_ALIASES,
    MEASUREMENT_PROFILES,
    TAG_ALIASES,
)

# System-metadata fields that are diagnostic rather than user-facing metrics
# (SPEC.md "Entity categories": uptime, boot_time, process counts, load averages).
_DIAGNOSTIC_FIELDS: frozenset[str] = frozenset({"uptime", "boot_time"})
_LOAD_AVERAGE_FIELDS: frozenset[str] = frozenset({"load1", "load5", "load15"})
_PROCESS_FIELD_PREFIX = "processes_"


def resolve_name(measurement: str, tags: Mapping[str, str], field: str) -> str:
    """Resolve a stable, clean name using the Phase 3 override order."""
    measurement = measurement.lower()
    tag_map = {str(key).lower(): str(value) for key, value in tags.items()}

    if measurement == "sensors":
        chip = tag_map.get("chip", "").lower()
        feature = tag_map.get("feature", "").lower()
        if "coretemp" in chip and "package_id_0" in feature:
            return "CPU Package Temperature"

    if measurement == "disk":
        path = tag_map.get("path")
        if path == "/":
            return f"Disk Root {_titleize(FIELD_ALIASES.get(field, field))}"

    tag_bits = []
    for key, value in sorted(tag_map.items()):
        if key == "host":
            continue
        normalized_value = str(value).strip().lower()
        if not normalized_value:
            continue
        token = TAG_ALIASES.get(normalized_value, normalized_value)
        if token not in tag_bits:
            tag_bits.append(token)

    field_bit = FIELD_ALIASES.get(field, field)

    parts: list[str] = []
    parts.extend(tag_bits)
    parts.append(field_bit)
    return " ".join(_format_part(part) for part in parts if part)


def resolve_entity_category(measurement: str, field: str) -> str | None:
    """Resolve the entity category from measurement profile heuristics."""
    profile = MEASUREMENT_PROFILES.get(measurement, {})
    if profile.get("entity_category") == ENTITY_CATEGORY_DIAGNOSTIC:
        return ENTITY_CATEGORY_DIAGNOSTIC
    if measurement.lower() == "disk":
        return ENTITY_CATEGORY_DIAGNOSTIC

    # Field-level diagnostics: lifecycle metadata, load averages, process counts.
    field_lower = field.lower()
    if (
        field_lower in _DIAGNOSTIC_FIELDS
        or field_lower in _LOAD_AVERAGE_FIELDS
        or field_lower.startswith(_PROCESS_FIELD_PREFIX)
    ):
        return ENTITY_CATEGORY_DIAGNOSTIC
    return None


def _titleize(value: str) -> str:
    return " ".join(word.capitalize() for word in value.replace("_", " ").split() if word)


def _format_part(value: str) -> str:
    """Preserve explicit alias display labels while title-casing raw token values."""
    if value in TAG_ALIASES.values() or value in FIELD_ALIASES.values():
        return value
    return _titleize(value)
