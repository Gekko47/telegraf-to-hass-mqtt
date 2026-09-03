"""Phase 9 translation-key and category resolution for telegraf_mqtt.

The parser/registry never produces a user-facing string directly. Every
descriptor carries a ``translation_key`` and a ``translation_placeholders``
mapping; the entity layer reads them, the ``en.json`` translator
formats them, and the user sees the localised result. This module is
the *only* place that turns a ``(measurement, field, tags)`` triple into
a translation key.

Phase 10: per-entity category overrides. The options flow can store
``{unique_key: "config" | "diagnostic" | None}`` to flip the auto-derived
category for one specific metric. Keys may be exact ``unique_key``
strings or glob patterns (e.g. ``mem_*``) matched with ``fnmatchcase``;
the override is consulted by ``apply_category_override`` after the
heuristic resolves.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from fnmatch import fnmatchcase

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
    """Return ``(translation_key, placeholders)`` for a metric.

    Composed of single-responsibility helpers, one per measurement,
    so the top-level function reads as a dispatch table. New
    measurements add a new helper (and one row in the table), not a
    new branch in a long if/elif chain.
    """
    measurement_lower = measurement.lower()
    display_field = _display_field(field)
    tag_map = {str(key).lower(): str(value) for key, value in tags.items()}
    # Per-measurement dispatch. Returning ``None`` from a handler
    # means "no special case here, fall through to the generic key".
    handler = _TRANSLATION_DISPATCH.get(measurement_lower)
    if handler is not None:
        result = handler(tag_map, display_field)
        if result is not None:
            return result
    return TK_GENERIC_FIELD, {"field": display_field}


def _translation_disk(
    tag_map: dict[str, str], display_field: str
) -> tuple[str, dict[str, str]] | None:
    """Disk measurements: root path is a special template."""
    path = tag_map.get("path")
    if path == "/":
        return TK_DISK_ROOT_FIELD, {"field": display_field}
    return TK_DISK_FIELD, {"field": display_field}


def _translation_sensors(
    tag_map: dict[str, str], display_field: str
) -> tuple[str, dict[str, str]] | None:
    """Sensors: coretemp package_id_0 is the CPU package temperature."""
    chip = tag_map.get("chip", "").lower()
    feature = tag_map.get("feature", "").lower()
    if "coretemp" in chip and "package_id_0" in feature:
        return TK_CPU_PACKAGE_TEMPERATURE, {}
    return TK_SENSOR_FIELD, {"field": display_field}


def _translation_net(
    tag_map: dict[str, str], display_field: str
) -> tuple[str, dict[str, str]] | None:
    """Network measurements: include the interface tag when present."""
    interface = tag_map.get("interface", "")
    if interface:
        return TK_NETWORK_FIELD, {
            "field": display_field,
            "interface": _network_token(interface),
        }
    return TK_NETWORK_FIELD, {"field": display_field, "interface": ""}


def _translation_simple(
    translation_key: str, display_field: str
) -> tuple[str, dict[str, str]]:
    """Measurements whose template is just the field name."""
    return translation_key, {"field": display_field}


# Per-measurement translation dispatch. Measurements not in the
# table fall through to the generic key. Each handler is a
# ``(tag_map, display_field) -> (key, placeholders) | None``
# callable; ``None`` is the explicit "fall through" signal for
# measurements that have a generic variant.
_TRANSLATION_DISPATCH: dict[str, Callable[[dict[str, str], str], tuple[str, dict[str, str]] | None]] = {
    "cpu": lambda tags, field: _translation_simple(TK_CPU_FIELD, field),
    "mem": lambda tags, field: _translation_simple(TK_MEMORY_FIELD, field),
    "disk": _translation_disk,
    "net": _translation_net,
    "sensors": _translation_sensors,
    "nvidia_gpu": lambda tags, field: _translation_simple(TK_GPU_FIELD, field),
    "battery": lambda tags, field: _translation_simple(TK_BATTERY_FIELD, field),
}


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


def match_category_override_key(
    unique_key: str,
    overrides: Mapping[str, str | None],
) -> str | None:
    """Return the override key that applies to ``unique_key``, or ``None``.

    An exact key always wins over a glob. Glob keys (keys containing
    ``*``, ``?`` or ``[``) are evaluated in insertion order and the
    first match is used. Matching is case-sensitive via
    ``fnmatchcase`` so behavior is identical on every host OS (plain
    ``fnmatch`` would fold case on Windows and make option semantics
    platform-dependent).
    """
    if unique_key in overrides:
        return unique_key
    for key in overrides:
        if any(char in key for char in "*?[") and fnmatchcase(unique_key, key):
            return key
    return None


def apply_category_override(
    measurement: str,
    field: str,
    unique_key: str,
    overrides: Mapping[str, str | None] | None,
) -> str | None:
    """Layer a per-entity category override on top of the heuristic.

    ``overrides`` is a ``{unique_key: category}`` map stored in
    ``entry.options[CATEGORY_OVERRIDES]``. Keys may be exact
    ``unique_key`` strings or glob patterns (e.g. ``mem_*``) matched
    against the metric's unique key via ``match_category_override_key``
    (exact key wins, then the first matching glob in insertion order).
    Values accepted:

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
    matched = match_category_override_key(unique_key, overrides)
    if matched is None:
        return heuristic
    raw = overrides[matched]
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
