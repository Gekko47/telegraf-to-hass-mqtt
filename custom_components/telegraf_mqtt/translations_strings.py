"""In-process English translation strings for telegraf_mqtt (Phase 9).

This module is the *only* place the in-code English strings live. The
JSON files in translations/ and strings.json are the source of truth for
the HA UI; this module mirrors them so test code, diagnostics output, and
log lines can render a label without going through the entity layer.
"""

from __future__ import annotations

# Translation tables for entity.sensor.* and entity.binary_sensor.*.
# Kept in sync with strings.json / translations/en.json; tests assert this.
_ENTITY_TRANSLATIONS: dict[str, dict[str, str]] = {
    "sensor": {
        "cpu_package_temperature": "CPU Package Temperature",
        "cpu_field": "CPU {field}",
        "memory_field": "Memory {field}",
        "disk_root_field": "Disk Root {field}",
        "disk_field": "Disk {field}",
        "network_field": "{interface} {field}",
        "sensor_field": "Sensor {field}",
        "gpu_field": "GPU {field}",
        "battery_field": "Battery {field}",
        "generic_field": "{field}",
    },
    "binary_sensor": {
        "cpu_package_temperature": "CPU Package Temperature",
        "cpu_field": "CPU {field}",
        "memory_field": "Memory {field}",
        "disk_root_field": "Disk Root {field}",
        "disk_field": "Disk {field}",
        "network_field": "{interface} {field}",
        "sensor_field": "Sensor {field}",
        "gpu_field": "GPU {field}",
        "battery_field": "Battery {field}",
        "generic_field": "{field}",
    },
}


def format_translation(translation_key: str, placeholders: dict[str, str]) -> str:
    """Format a translation key with placeholders using the in-code English strings.

    Used by diagnostics, log lines, and tests. The actual entity layer uses
    HA's JSON-driven translator; this is the in-process mirror that lets
    non-entity code produce a user-visible label.

    The ``network_field`` template is ``"{interface} {field}"`` -- when
    the interface placeholder is empty the leading space would render
    as a leading-space user-facing string, which is awkward. We strip
    a leading separator in that case.
    """
    template = _ENTITY_TRANSLATIONS["sensor"][translation_key]
    rendered = template.format(**placeholders)
    if translation_key == "network_field":
        rendered = rendered.lstrip()
    return rendered


def all_translation_keys() -> set[str]:
    """Return the set of every translation key the integration can emit."""
    return set(_ENTITY_TRANSLATIONS["sensor"].keys())
