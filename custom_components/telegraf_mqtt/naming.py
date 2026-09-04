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
TK_SYSTEM_FIELD = "system_field"
TK_KERNEL_FIELD = "kernel_field"
TK_PROCESSES_FIELD = "processes_field"
TK_SWAP_FIELD = "swap_field"
TK_DISKIO_FIELD = "diskio_field"
TK_PING_FIELD = "ping_field"
TK_SMART_FIELD = "smart_field"
TK_WIRELESS_FIELD = "wireless_field"
TK_DOCKER_FIELD = "docker_field"
TK_ZFS_FIELD = "zfs_field"
TK_NET_RESPONSE_FIELD = "net_response_field"
TK_HTTP_RESPONSE_FIELD = "http_response_field"
TK_INTERRUPTS_FIELD = "interrupts_field"
TK_IPMI_FIELD = "ipmi_field"
# Phase 11 -- one translation key per new measurement. The template is
# ``"{measurement} {field}"`` for the simple ones; per-device / per-
# container / per-pool variants layer an extra placeholder.
TK_SYSTEM_FIELD = "system_field"
TK_KERNEL_FIELD = "kernel_field"
TK_PROCESSES_FIELD = "processes_field"
TK_SWAP_FIELD = "swap_field"
TK_DISKIO_FIELD = "diskio_field"  # "{device} {field}"
TK_PING_FIELD = "ping_field"  # "{url} {field}"
TK_SMART_FIELD = "smart_field"  # "{device} {field}"
TK_WIRELESS_FIELD = "wireless_field"  # "{interface} {field}"
TK_DOCKER_FIELD = "docker_field"  # "{container} {field}"
TK_ZFS_FIELD = "zfs_field"  # "{pool} {field}" or "{dataset} {field}"
TK_NET_RESPONSE_FIELD = "net_response_field"  # "{server}:{port} {field}"
TK_HTTP_RESPONSE_FIELD = "http_response_field"  # "{method} {server} {field}"
TK_INTERRUPTS_FIELD = "interrupts_field"  # "{irq} {field}"
TK_IPMI_FIELD = "ipmi_field"  # "{name} {field}"

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
# Phase 11 -- per-measurement icon keys for the new parsers.
ICON_KEY_SYSTEM = "system"
ICON_KEY_KERNEL = "kernel"
ICON_KEY_PROCESSES = "processes"
ICON_KEY_SWAP = "swap"
ICON_KEY_DISKIO = "diskio"
ICON_KEY_PING = "ping"
ICON_KEY_SMART = "smart"
ICON_KEY_WIRELESS = "wireless"
ICON_KEY_DOCKER = "docker"
ICON_KEY_ZFS = "zfs"
ICON_KEY_NET_RESPONSE = "net_response"
ICON_KEY_HTTP_RESPONSE = "http_response"
ICON_KEY_INTERRUPTS = "interrupts"
ICON_KEY_IPMI = "ipmi"

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


def _translation_disk(tag_map: dict[str, str], display_field: str) -> tuple[str, dict[str, str]] | None:
    """Disk measurements: root path is a special template."""
    path = tag_map.get("path")
    if path == "/":
        return TK_DISK_ROOT_FIELD, {"field": display_field}
    return TK_DISK_FIELD, {"field": display_field}


def _translation_sensors(tag_map: dict[str, str], display_field: str) -> tuple[str, dict[str, str]] | None:
    """Sensors: coretemp package_id_0 is the CPU package temperature."""
    chip = tag_map.get("chip", "").lower()
    feature = tag_map.get("feature", "").lower()
    if "coretemp" in chip and "package_id_0" in feature:
        return TK_CPU_PACKAGE_TEMPERATURE, {}
    return TK_SENSOR_FIELD, {"field": display_field}


def _translation_net(tag_map: dict[str, str], display_field: str) -> tuple[str, dict[str, str]] | None:
    """Network measurements: include the interface tag when present."""
    interface = tag_map.get("interface", "")
    if interface:
        return TK_NETWORK_FIELD, {
            "field": display_field,
            "interface": _network_token(interface),
        }
    return TK_NETWORK_FIELD, {"field": display_field, "interface": ""}


def _translation_simple(translation_key: str, display_field: str) -> tuple[str, dict[str, str]]:
    """Measurements whose template is just the field name."""
    return translation_key, {"field": display_field}


def _translation_tagged(
    tag_name: str,
    placeholder_name: str,
    translation_key: str,
    tag_map: dict[str, str],
    display_field: str,
) -> tuple[str, dict[str, str]]:
    """Per-device / per-container / per-pool template helper.

    Reads ``tag_name`` from the tag map (e.g. ``name`` for diskio,
    ``container_name`` for docker), defaults to an empty string if
    missing, and returns
    ``(translation_key, {placeholder_name: value, "field": display_field})``.

    ``placeholder_name`` is the template placeholder to use, so a
    measurement whose template is ``"{container} {field}"`` returns
    ``{"container": ..., "field": ...}``. For most measurements
    ``placeholder_name == "device"`` is fine (matching
    ``{device} {field}``), but docker uses ``{container}``, interrupts
    uses ``{irq}``, ipmi uses ``{name}``, etc.

    The translation template renders the empty leading segment as
    nothing in :func:`format_translation` (mirroring the ``network_field``
    leading-space trim), so a missing tag produces a clean
    ``"Read Bytes"`` instead of ``" Read Bytes"``.
    """
    raw = tag_map.get(tag_name, "")
    return translation_key, {placeholder_name: raw, "field": display_field}


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
    "system": lambda tags, field: _translation_simple(TK_SYSTEM_FIELD, field),
    "kernel": lambda tags, field: _translation_simple(TK_KERNEL_FIELD, field),
    "kernel_vmstat": lambda tags, field: _translation_simple(TK_KERNEL_FIELD, field),
    "processes": lambda tags, field: _translation_simple(TK_PROCESSES_FIELD, field),
    "swap": lambda tags, field: _translation_simple(TK_SWAP_FIELD, field),
    "diskio": lambda tags, field: _translation_tagged("name", "device", TK_DISKIO_FIELD, tags, field),
    "ping": lambda tags, field: _translation_tagged("url", "url", TK_PING_FIELD, tags, field),
    "smart": lambda tags, field: _translation_tagged("device", "device", TK_SMART_FIELD, tags, field),
    "wireless": lambda tags, field: _translation_tagged("interface", "interface", TK_WIRELESS_FIELD, tags, field),
    "docker": lambda tags, field: _translation_tagged("container_name", "container", TK_DOCKER_FIELD, tags, field),
    "docker_container_cpu": lambda tags, field: _translation_tagged(
        "container_name", "container", TK_DOCKER_FIELD, tags, field
    ),
    "docker_container_mem": lambda tags, field: _translation_tagged(
        "container_name", "container", TK_DOCKER_FIELD, tags, field
    ),
    "docker_container_net": lambda tags, field: _translation_tagged(
        "container_name", "container", TK_DOCKER_FIELD, tags, field
    ),
    "docker_container_blkio": lambda tags, field: _translation_tagged(
        "container_name", "container", TK_DOCKER_FIELD, tags, field
    ),
    "docker_container_status": lambda tags, field: _translation_tagged(
        "container_name", "container", TK_DOCKER_FIELD, tags, field
    ),
    "zfs": lambda tags, field: _translation_tagged("pools", "pool", TK_ZFS_FIELD, tags, field),
    "zfs_pool": lambda tags, field: _translation_tagged("pool", "pool", TK_ZFS_FIELD, tags, field),
    "zfs_dataset": lambda tags, field: _translation_tagged("dataset", "pool", TK_ZFS_FIELD, tags, field),
    "net_response": lambda tags, field: (
        TK_NET_RESPONSE_FIELD,
        {"server": tags.get("server", ""), "port": tags.get("port", ""), "field": field},
    ),
    "http_response": lambda tags, field: (
        TK_HTTP_RESPONSE_FIELD,
        {"method": tags.get("method", ""), "server": tags.get("server", ""), "field": field},
    ),
    "interrupts": lambda tags, field: _translation_tagged("irq", "irq", TK_INTERRUPTS_FIELD, tags, field),
    "soft_interrupts": lambda tags, field: _translation_tagged("irq", "irq", TK_INTERRUPTS_FIELD, tags, field),
    "ipmi_sensor": lambda tags, field: _translation_tagged("name", "name", TK_IPMI_FIELD, tags, field),
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
    # Phase 11 -- one icon key per new measurement. The order matters
    # only when a measurement shares a prefix with another (``docker``
    # must come before a hypothetical ``docker_x``); for the existing
    # names no overlap exists, so a flat if-chain is fine.
    if measurement_lower == "system":
        return ICON_KEY_SYSTEM
    if measurement_lower in {"kernel", "kernel_vmstat"}:
        return ICON_KEY_KERNEL
    if measurement_lower == "processes":
        return ICON_KEY_PROCESSES
    if measurement_lower == "swap":
        return ICON_KEY_SWAP
    if measurement_lower == "diskio":
        return ICON_KEY_DISKIO
    if measurement_lower == "ping":
        return ICON_KEY_PING
    if measurement_lower == "smart":
        return ICON_KEY_SMART
    if measurement_lower == "wireless":
        return ICON_KEY_WIRELESS
    if measurement_lower.startswith("docker"):
        return ICON_KEY_DOCKER
    if measurement_lower in {"zfs", "zfs_pool", "zfs_dataset"}:
        return ICON_KEY_ZFS
    if measurement_lower == "net_response":
        return ICON_KEY_NET_RESPONSE
    if measurement_lower == "http_response":
        return ICON_KEY_HTTP_RESPONSE
    if measurement_lower in {"interrupts", "soft_interrupts"}:
        return ICON_KEY_INTERRUPTS
    if measurement_lower == "ipmi_sensor":
        return ICON_KEY_IPMI

    return ICON_KEY_GENERIC
