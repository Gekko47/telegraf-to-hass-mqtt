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
        # Phase 11 -- per-measurement templates. The "tagged" variants
        # carry an extra ``{device}`` placeholder that holds the
        # disambiguator (e.g. ``name=sda`` for diskio, ``container_name``
        # for docker, ``url=example.org`` for ping). The leading space
        # is trimmed by ``format_translation`` when the placeholder is
        # empty -- the same contract ``network_field`` already had.
        "system_field": "System {field}",
        "kernel_field": "Kernel {field}",
        "processes_field": "Processes {field}",
        "swap_field": "Swap {field}",
        "diskio_field": "{device} {field}",
        "ping_field": "{url} {field}",
        "smart_field": "{device} {field}",
        "wireless_field": "{interface} {field}",
        "docker_field": "{container} {field}",
        "zfs_field": "{pool} {field}",
        "net_response_field": "{server}:{port} {field}",
        "http_response_field": "{method} {server} {field}",
        "interrupts_field": "{irq} {field}",
        "ipmi_field": "{name} {field}",
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
        "system_field": "System {field}",
        "kernel_field": "Kernel {field}",
        "processes_field": "Processes {field}",
        "swap_field": "Swap {field}",
        "diskio_field": "{device} {field}",
        "ping_field": "{url} {field}",
        "smart_field": "{device} {field}",
        "wireless_field": "{interface} {field}",
        "docker_field": "{container} {field}",
        "zfs_field": "{pool} {field}",
        "net_response_field": "{server}:{port} {field}",
        "http_response_field": "{method} {server} {field}",
        "interrupts_field": "{irq} {field}",
        "ipmi_field": "{name} {field}",
    },
}

# Translation keys whose template is the "tagged" form
# (an empty disambiguator prefix is trimmed to keep the rendered label
# clean). Phase 11 added the per-measurement tagged keys; ``network_field``
# has been in this set since Phase 3.
_TAGGED_TRANSLATION_KEYS: frozenset[str] = frozenset(
    {
        "network_field",
        "diskio_field",
        "ping_field",
        "smart_field",
        "wireless_field",
        "docker_field",
        "zfs_field",
        "net_response_field",
        "http_response_field",
        "interrupts_field",
        "ipmi_field",
    }
)


def format_translation(translation_key: str, placeholders: dict[str, str]) -> str:
    """Format a translation key with placeholders using the in-code English strings.

    Used by diagnostics, log lines, and tests. The actual entity layer uses
    HA's JSON-driven translator; this is the in-process mirror that lets
    non-entity code produce a user-visible label.

    The ``*_field`` templates that carry a disambiguator prefix (e.g.
    ``network_field`` -> ``"{interface} {field}"``) render with a
    leading space when the disambiguator is empty. ``format_translation``
    strips a single leading separator in that case so the user sees
    ``"Bytes Received"`` rather than ``" Bytes Received"``.
    """
    template = _ENTITY_TRANSLATIONS["sensor"][translation_key]
    rendered = template.format(**placeholders)
    if translation_key in _TAGGED_TRANSLATION_KEYS:
        rendered = rendered.lstrip()
    return rendered


def all_translation_keys() -> set[str]:
    """Return the set of every translation key the integration can emit."""
    return set(_ENTITY_TRANSLATIONS["sensor"].keys())
