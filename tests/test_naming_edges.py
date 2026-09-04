"""Direct unit tests for naming.py resolution branches."""

from __future__ import annotations

from custom_components.telegraf_mqtt.naming import (
    infer_icon_key,
    resolve_entity_category,
    resolve_translation,
)


def test_resolve_translation_cpu_field() -> None:
    key, placeholders = resolve_translation("cpu", {"host": "h"}, "usage_idle")
    assert key == "cpu_field"
    assert placeholders == {"field": "Usage Idle"}


def test_resolve_translation_skips_empty_tag_values() -> None:
    key, placeholders = resolve_translation("mem", {"host": "h", "empty_tag": ""}, "used_percent")
    assert key == "memory_field"
    assert placeholders == {"field": "Used Percent"}


def test_resolve_translation_sensors_coretemp_returns_cpu_package_temperature() -> None:
    key, placeholders = resolve_translation(
        "sensors",
        {"host": "h", "chip": "coretemp-isa-0000", "feature": "package_id_0"},
        "temp_input",
    )
    assert key == "cpu_package_temperature"
    assert placeholders == {}


def test_resolve_translation_disk_root_path() -> None:
    key, placeholders = resolve_translation("disk", {"host": "h", "path": "/"}, "used_percent")
    assert key == "disk_root_field"
    assert placeholders == {"field": "Used Percent"}


def test_resolve_translation_network_includes_interface() -> None:
    key, placeholders = resolve_translation("net", {"host": "h", "interface": "wlan0"}, "bytes_recv")
    assert key == "network_field"
    assert placeholders == {"field": "Bytes Received", "interface": "wlan0"}


def test_resolve_translation_unknown_measurement_falls_back_to_generic() -> None:
    key, placeholders = resolve_translation("custom_plugin", {"host": "h"}, "watts")
    assert key == "generic_field"
    assert placeholders == {"field": "Watts"}


def test_resolve_translation_disk_non_root_path() -> None:
    """A ``disk`` payload without ``path == "/"`` uses the generic disk_field key."""
    key, placeholders = resolve_translation("disk", {"host": "h", "path": "/data"}, "used_percent")
    assert key == "disk_field"
    assert placeholders == {"field": "Used Percent"}


def test_resolve_translation_network_without_interface() -> None:
    """A ``net`` payload without an interface tag uses the network_field key
    with an empty interface placeholder."""
    key, placeholders = resolve_translation("net", {"host": "h"}, "bytes_recv")
    assert key == "network_field"
    assert placeholders == {"field": "Bytes Received", "interface": ""}


def test_resolve_translation_sensors_non_coretemp() -> None:
    """A ``sensors`` payload that's not a coretemp CPU package uses the
    generic sensor_field key."""
    key, placeholders = resolve_translation(
        "sensors", {"host": "h", "chip": "nvme-pci-0008", "feature": "composite"}, "temp_input"
    )
    assert key == "sensor_field"
    assert placeholders == {"field": "Temperature"}


def test_disk_measurement_resolves_diagnostic_regardless_of_case() -> None:
    """Profile lookup is exact-match, so 'Disk' exercises the lowercase fallback."""
    assert resolve_entity_category("disk", "used_percent") == "diagnostic"
    assert resolve_entity_category("Disk", "used_percent") == "diagnostic"


def test_lifecycle_load_and_process_fields_resolve_diagnostic_on_any_measurement() -> None:
    """SPEC.md: uptime/boot_time, load averages and process counts are DIAGNOSTIC."""
    assert resolve_entity_category("system", "uptime") == "diagnostic"
    assert resolve_entity_category("system", "boot_time") == "diagnostic"
    assert resolve_entity_category("system", "load1") == "diagnostic"
    assert resolve_entity_category("system", "load15") == "diagnostic"
    assert resolve_entity_category("system", "processes_forked") == "diagnostic"
    # Plain user-facing counts stay uncategorized.
    assert resolve_entity_category("system", "n_users") is None
    assert resolve_entity_category("system", "load_average") is None


def test_infer_icon_key_physical_classes() -> None:
    assert infer_icon_key("any", "temp_input") == "temperature"
    assert infer_icon_key("any", "voltage") == "voltage"
    assert infer_icon_key("any", "energy_rate") == "power"
    assert infer_icon_key("any", "energy") == "energy"
    assert infer_icon_key("any", "fan_input") == "fan"


def test_infer_icon_key_measurement_fallback() -> None:
    assert infer_icon_key("cpu", "usage_idle") == "cpu"
    assert infer_icon_key("mem", "used") == "memory"
    assert infer_icon_key("disk", "free") == "disk"
    assert infer_icon_key("net", "bytes_recv") == "network"
    assert infer_icon_key("battery", "state") == "battery"
    assert infer_icon_key("custom", "field") == "generic"


def test_infer_icon_key_phase11_per_measurement() -> None:
    """Phase 11 -- one icon key per new measurement so the UI shows the
    right Material Design Icon for every entity.
    """
    # One icon per primary measurement.
    assert infer_icon_key("system", "uptime") == "system"
    assert infer_icon_key("kernel", "interrupts") == "kernel"
    assert infer_icon_key("kernel_vmstat", "pgpgin") == "kernel"
    assert infer_icon_key("processes", "total") == "processes"
    assert infer_icon_key("swap", "used") == "swap"
    assert infer_icon_key("diskio", "read_bytes") == "diskio"
    assert infer_icon_key("ping", "average_response_ms") == "ping"
    assert infer_icon_key("smart", "temp_c") == "smart"
    assert infer_icon_key("wireless", "level") == "wireless"
    assert infer_icon_key("docker_container_cpu", "usage_total") == "docker"
    assert infer_icon_key("docker_container_mem", "usage") == "docker"
    assert infer_icon_key("docker_swarm", "tasks_desired") == "docker"
    assert infer_icon_key("zfs", "arcstats_size") == "zfs"
    assert infer_icon_key("zfs_pool", "allocated") == "zfs"
    assert infer_icon_key("zfs_dataset", "used") == "zfs"
    assert infer_icon_key("net_response", "response_time") == "net_response"
    assert infer_icon_key("http_response", "response_time") == "http_response"
    assert infer_icon_key("interrupts", "count") == "interrupts"
    assert infer_icon_key("soft_interrupts", "count") == "interrupts"
    assert infer_icon_key("ipmi_sensor", "value") == "ipmi"
