"""Phase 3 naming and alias resolution for telegraf_mqtt."""

from __future__ import annotations

from collections.abc import Mapping

from .heuristics import FIELD_ALIASES, MEASUREMENT_PROFILES, TAG_ALIASES


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

    tag_bits = [TAG_ALIASES.get(str(value), value) for value in tags.values() if value not in {"host"}]
    field_bit = FIELD_ALIASES.get(field, field)

    parts: list[str] = []
    for token in tag_bits:
        if token not in parts:
            parts.append(token)
    parts.append(field_bit)
    return " ".join(_titleize(part) for part in parts if part)


def resolve_entity_category(measurement: str, field: str) -> str | None:
    """Resolve the entity category from measurement profile heuristics."""
    profile = MEASUREMENT_PROFILES.get(measurement, {})
    if profile.get("entity_category") == "diagnostic":
        return "diagnostic"
    if measurement.lower() == "disk":
        return "diagnostic"
    if field.lower() in {"uptime", "boot_time"}:
        return "diagnostic"
    return None


def _titleize(value: str) -> str:
    return " ".join(word.capitalize() for word in value.replace("_", " ").split() if word)
