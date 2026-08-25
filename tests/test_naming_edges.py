"""Direct unit tests for naming.py resolution branches."""

from __future__ import annotations

from custom_components.telegraf_mqtt.naming import resolve_entity_category, resolve_name


def test_resolve_name_skips_empty_tag_values() -> None:
    assert resolve_name("mem", {"host": "h", "empty_tag": ""}, "used_percent") == "Used Percent"


def test_disk_measurement_resolves_diagnostic_regardless_of_case() -> None:
    """Profile lookup is exact-match, so 'Disk' exercises the lowercase fallback."""
    assert resolve_entity_category("disk", "used_percent") == "diagnostic"
    assert resolve_entity_category("Disk", "used_percent") == "diagnostic"


def test_lifecycle_fields_resolve_diagnostic_on_any_measurement() -> None:
    assert resolve_entity_category("system", "uptime") == "diagnostic"
    assert resolve_entity_category("system", "boot_time") == "diagnostic"
    assert resolve_entity_category("system", "load1") is None
