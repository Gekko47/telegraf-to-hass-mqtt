"""Phase 9 translation-key and category resolution for telegraf_mqtt.

The parser/registry never produces a user-facing string directly. Every
descriptor carries a ``translation_key`` and a ``translation_placeholders``
mapping; the entity layer reads them, the ``en.json`` translator
formats them, and the user sees the localised result. This module is
the *only* place that turns a ``(measurement, field, tags)`` triple into
a translation key.

Phase 10: per-entity category overrides. The options flow can store
``{unique_key: "config" | "diagnostic" | None}`` to flip the auto-derived
category for one specific metric. The override is consulted by
``apply_category_override`` after the heuristic resolves.
"""

from __future__ import annotations

from collections.abc import Mapping

from .heuristics import (
    ENTITY_CATEGORY_DIAGNOSTIC,
    FIELD_ALIASES,
    TAG_ALIASES,
)

# Translation keys. Every key has a matching row in en.json + strings.json.
TK_CPU_PACKAGE_TEMPERATURE = "cpu_package_temperature"
TK_CPU_FIELD = "cpu_field"
TK_MEMORY_FIELD = "memory_field"
TK_DISK_ROOT_FIELD = "disk_root_field"
TK_DISK_FIELD = "disk_field"
TK_NETWORK_FIELD = "network_field"
TK_SENSOR_FIELD = "sensor_field"
TK_GPU_FIELD = "gpu_field"
TK_BATTERY_FIELD = "battery_field"
TK_GENERIC_FIELD = "generic_field"

# Icon-key constants.
ICON_KEY_CPU = "cpu"
ICON_KEY_MEMORY = "memory"
ICON_KEY_DISK = "disk"
ICON_KEY_NETWORK = "network"
ICON_KEY_TEMPERATURE = "temperature"
ICON_KEY_VOLTAGE = "voltage"
ICON_KEY_POWER = "power"
ICON_KEY_ENERGY = "energy"
ICON_KEY_BATTERY = "battery"
ICON_KEY_FAN = "fan"
ICON_KEY_PERCENTAGE = "percentage"
ICON_KEY_BINARY = "binary"
ICON_KEY_GENERIC = "generic"

# Entity-category literals. Mirror the values HA's EntityCategory enum accepts.
ENTITY_CATEGORY_CONFIG = "config"
ENTITY_CATEGORY_NONE = ""  # sentinel: an override that explicitly clears the category

VALID_ENTITY_CATEGORIES = (
    ENTITY_CATEGORY_CONFIG,
    ENTITY_CATEGORY_DIAGNOSTIC,
    None,  # explicit "no category"
)

_DIAGNOSTIC_FIELDS: frozenset[str] = frozenset({"uptime", "boot_time"})
_LOAD_AVERAGE_FIELDS: frozenset[str] = frozenset({"load1", "load5", "load15"})
_PROCESS_FIELD_PREFIX = "processes_"
_DIAGNOSTIC_MEASUREMENTS: frozenset[str] = frozenset({"disk"})


def _titleize(value: str) -> str:
    return " ".join(word.capitalize() for word in value.replace("_", " ").split() if word)


def _display_field(field: str) -> str:
    return FIELD_ALIASES.get(field, _titleize(field))


def _network_token(tag_value: str) -> str:
    return TAG_ALIASES.get(tag_value.lower(), tag_value)


def resolve_translation(measurement: str, tags: Mapping[str, str], field: str) -> tuple[str, dict[str, str]]:
    """Return ``(translation_key, placeholders)`` for a metric."""
    measurement_lower = measurement.lower()
    display_field = _display_field(field)
    tag_map = {str(key).lower(): str(value) for key, value in tags.items()}

    if measurement_lower == "sensors":
        chip = tag_map.get("chip", "").lower()
        feature = tag_map.get("feature", "").lower()
        if "coretemp" in chip and "package_id_0" in feature:
            return TK_CPU_PACKAGE_TEMPERATURE, {}

    if measurement_lower == "disk":
        path = tag_map.get("path")
        if path == "/":
            return TK_DISK_ROOT_FIELD, {"field": display_field}

    if measurement_lower == "cpu":
        return TK_CPU_FIELD, {"field": display_field}
    if measurement_lower == "mem":
        return TK_MEMORY_FIELD, {"field": display_field}
    if measurement_lower == "disk":
        return TK_DISK_FIELD, {"field": display_field}
    if measurement_lower == "net":
        interface = tag_map.get("interface", "")
        if interface:
            return TK_NETWORK_FIELD, {
                "field": display_field,
                "interface": _network_token(interface),
            }
        return TK_NETWORK_FIELD, {"field": display_field, "interface": ""}
    if measurement_lower == "sensors":
        return TK_SENSOR_FIELD, {"field": display_field}
    if measurement_lower == "nvidia_gpu":
        return TK_GPU_FIELD, {"field": display_field}
    if measurement_lower == "battery":
        return TK_BATTERY_FIELD, {"field": display_field}

    return TK_GENERIC_FIELD, {"field": display_field}


def resolve_entity_category(measurement: str, field: str) -> str | None:
    """Resolve the entity category from measurement + field heuristics.

    The return value is a string category, ``None`` for "no category".
    The Phase 10 per-entity override map is layered on top of this in
    ``apply_category_override`` (called by the registry); this function
    is the heuristic baseline.
    """
    measurement_lower = measurement.lower()
    if measurement_lower in _DIAGNOSTIC_MEASUREMENTS:
        return ENTITY_CATEGORY_DIAGNOSTIC

    field_lower = field.lower()
    if (
        field_lower in _DIAGNOSTIC_FIELDS
        or field_lower in _LOAD_AVERAGE_FIELDS
        or field_lower.startswith(_PROCESS_FIELD_PREFIX)
    ):
        return ENTITY_CATEGORY_DIAGNOSTIC

    return None


def apply_category_override(
    measurement: str,
    field: str,
    unique_key: str,
    overrides: Mapping[str, str | None] | None,
) -> str | None:
    """Layer a per-entity category override on top of the heuristic.

    ``overrides`` is a ``{unique_key: category}`` map stored in
    ``entry.options[CATEGORY_OVERRIDES]``. Values accepted:

    - ``"config"`` -> forced to the config category.
    - ``"diagnostic"`` -> forced to diagnostic.
    - ``""`` or ``None`` -> clear the auto-derived category (entity
      appears in the primary list).
    - any other value -> the override is ignored and the heuristic
      result is returned.

    Missing / invalid override keys return the heuristic result
    unchanged. The function is total over its inputs.
    """
    heuristic = resolve_entity_category(measurement, field)
    if not overrides:
        return heuristic
    if unique_key not in overrides:
        return heuristic
    raw = overrides[unique_key]
    if raw in (None, ENTITY_CATEGORY_NONE):
        return None
    if raw == ENTITY_CATEGORY_CONFIG:
        return ENTITY_CATEGORY_CONFIG
    if raw == ENTITY_CATEGORY_DIAGNOSTIC:
        return ENTITY_CATEGORY_DIAGNOSTIC
    return heuristic


def infer_icon_key(measurement: str, field: str) -> str:
    """Return the icon-table key for a metric."""
    field_lower = field.lower()
    if "temp_input" in field_lower or field_lower == "temp" or "temperature" in field_lower:
        return ICON_KEY_TEMPERATURE
    if "voltage" in field_lower:
        return ICON_KEY_VOLTAGE
    if "energy_rate" in field_lower or "power" in field_lower:
        return ICON_KEY_POWER
    if "energy" in field_lower:
        return ICON_KEY_ENERGY
    if "fan_input" in field_lower or field_lower == "rpm":
        return ICON_KEY_FAN
    if "percent" in field_lower or field_lower == "percentage":
        return ICON_KEY_PERCENTAGE

    measurement_lower = measurement.lower()
    if measurement_lower == "cpu":
        return ICON_KEY_CPU
    if measurement_lower == "mem":
        return ICON_KEY_MEMORY
    if measurement_lower == "disk":
        return ICON_KEY_DISK
    if measurement_lower == "net":
        return ICON_KEY_NETWORK
    if measurement_lower == "battery":
        return ICON_KEY_BATTERY

    return ICON_KEY_GENERIC
