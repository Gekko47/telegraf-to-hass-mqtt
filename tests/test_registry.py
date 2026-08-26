from __future__ import annotations

import json

import pytest

from custom_components.telegraf_mqtt.models import MetricDescriptor
from custom_components.telegraf_mqtt.parser import TelegrafParser
from custom_components.telegraf_mqtt.registry import DeviceManager, MetricRegistry


def test_registry_expiry_and_recovery() -> None:
    clock = [100.0]
    registry = MetricRegistry(expire_after=5, clock=lambda: clock[0])

    descriptor = MetricDescriptor(
        unique_key="cpu_cpu-total_usage_idle",
        measurement="cpu",
        tags={"host": "host1", "cpu": "cpu-total"},
        field="usage_idle",
        value=88.4,
        timestamp=1721664000,
        name="CPU Usage Idle",
        native_unit=None,
        suggested_device_class=None,
        suggested_state_class="measurement",
        entity_category=None,
    )

    registry.update(descriptor)
    assert registry.get("cpu_cpu-total_usage_idle").is_available is True

    clock[0] = 106.0
    registry.check_expiry()
    assert registry.get("cpu_cpu-total_usage_idle").is_available is False

    registry.update(
        MetricDescriptor(
            unique_key="cpu_cpu-total_usage_idle",
            measurement="cpu",
            tags={"host": "host1", "cpu": "cpu-total"},
            field="usage_idle",
            value=89.0,
            timestamp=1721664001,
            name="CPU Usage Idle",
            native_unit=None,
            suggested_device_class=None,
            suggested_state_class="measurement",
            entity_category=None,
        )
    )
    assert registry.get("cpu_cpu-total_usage_idle").is_available is True


def test_registry_exclude_patterns_and_field_overrides() -> None:
    registry = MetricRegistry(
        expire_after=5,
        exclude_patterns=("mem_*",),
        field_overrides={"used_percent": {"native_unit": "%", "state_class": "measurement"}},
    )
    descriptor = MetricDescriptor(
        unique_key="mem_used_percent",
        measurement="mem",
        tags={"host": "host1"},
        field="used_percent",
        value=41.2,
        timestamp=1721664000,
        name="Memory Used Percent",
        native_unit=None,
        suggested_device_class=None,
        suggested_state_class="measurement",
        entity_category=None,
    )

    assert registry.update(descriptor) is False
    assert registry.get("mem_used_percent") is None

    descriptor = MetricDescriptor(
        unique_key="cpu_usage_idle",
        measurement="cpu",
        tags={"host": "host1"},
        field="used_percent",
        value=41.2,
        timestamp=1721664000,
        name="CPU Usage Idle",
        native_unit=None,
        suggested_device_class=None,
        suggested_state_class="measurement",
        entity_category=None,
    )
    assert registry.update(descriptor) is True
    state = registry.get("cpu_usage_idle")
    assert state is not None
    assert state.descriptor.native_unit == "%"
    assert state.descriptor.suggested_state_class == "measurement"


def test_registry_apply_options_live() -> None:
    registry = MetricRegistry(expire_after=5)
    registry.apply_options(
        expire_after=1,
        exclude_patterns=("mem_*",),
        field_overrides={"used_percent": {"native_unit": "%", "state_class": "measurement"}},
    )

    descriptor = MetricDescriptor(
        unique_key="mem_used_percent",
        measurement="mem",
        tags={"host": "host1"},
        field="used_percent",
        value=41.2,
        timestamp=1721664000,
        name="Memory Used Percent",
        native_unit=None,
        suggested_device_class=None,
        suggested_state_class="measurement",
        entity_category=None,
    )

    assert registry.update(descriptor) is False
    assert registry.get("mem_used_percent") is None
    assert registry._expire_after == 1


def test_registry_only_writes_state_on_real_change() -> None:
    calls: list[tuple[bool, bool]] = []
    registry = MetricRegistry(expire_after=5)

    descriptor = MetricDescriptor(
        unique_key="mem_used_percent",
        measurement="mem",
        tags={"host": "host1"},
        field="used_percent",
        value=41.2,
        timestamp=1721664000,
        name="Memory Used Percent",
        native_unit=None,
        suggested_device_class=None,
        suggested_state_class="measurement",
        entity_category=None,
    )

    def on_write(key: str, available: bool, value: object) -> None:
        calls.append((available, value == registry.get(key).value))

    registry.update(descriptor, on_write=on_write)
    registry.update(descriptor, on_write=on_write)
    assert len(calls) == 1

    registry.update(
        MetricDescriptor(
            unique_key="mem_used_percent",
            measurement="mem",
            tags={"host": "host1"},
            field="used_percent",
            value=41.3,
            timestamp=1721664002,
            name="Memory Used Percent",
            native_unit=None,
            suggested_device_class=None,
            suggested_state_class="measurement",
            entity_category=None,
        ),
        on_write=on_write,
    )
    assert len(calls) == 2


def test_registry_discovers_once_and_does_not_write_for_timestamp_only_changes() -> None:
    writes: list[str] = []
    discovered: list[str] = []
    registry = MetricRegistry(expire_after=5)

    descriptor = MetricDescriptor(
        unique_key="cpu_usage_idle",
        measurement="cpu",
        tags={"host": "host1"},
        field="usage_idle",
        value=88.4,
        timestamp=1721664000,
        name="CPU Usage Idle",
        native_unit=None,
        suggested_device_class=None,
        suggested_state_class="measurement",
        entity_category=None,
    )

    assert registry.update(
        descriptor,
        on_write=lambda key, available, value: writes.append(key),
        on_discovered=discovered.append,
    )
    assert not registry.update(
        MetricDescriptor(
            unique_key="cpu_usage_idle",
            measurement="cpu",
            tags={"host": "host1"},
            field="usage_idle",
            value=88.4,
            timestamp=1721664001,
            name="CPU Usage Idle",
            native_unit=None,
            suggested_device_class=None,
            suggested_state_class="measurement",
            entity_category=None,
        ),
        on_write=lambda key, available, value: writes.append(key),
        on_discovered=discovered.append,
    )

    assert writes == ["cpu_usage_idle"]
    assert discovered == ["cpu_usage_idle"]


class _FakeParser:
    def __init__(self, descriptors: list[MetricDescriptor]) -> None:
        self._descriptors = descriptors

    def parse(self, payload: str | bytes) -> list[MetricDescriptor]:
        return list(self._descriptors)


def test_device_manager_discovers_multiple_devices_and_isolates_metrics() -> None:
    manager = DeviceManager(expire_after=5, clock=lambda: 100.0)
    parser = _FakeParser(
        [
            MetricDescriptor(
                unique_key="cpu_usage_idle",
                measurement="cpu",
                tags={"host": "server01", "cpu": "cpu-total"},
                field="usage_idle",
                value=88.4,
                timestamp=1721664000,
                name="CPU Usage Idle",
                native_unit=None,
                suggested_device_class=None,
                suggested_state_class="measurement",
                entity_category=None,
            ),
            MetricDescriptor(
                unique_key="mem_used_percent",
                measurement="mem",
                tags={"host": "server02"},
                field="used_percent",
                value=41.2,
                timestamp=1721664000,
                name="Memory Used Percent",
                native_unit=None,
                suggested_device_class=None,
                suggested_state_class="measurement",
                entity_category=None,
            ),
        ]
    )

    manager.process_message("telegraf/server01", "{}", parser=parser)
    manager.process_message("telegraf/server02", "{}", parser=parser)

    assert set(manager.devices) == {"server01", "server02"}
    assert manager.keys() == (
        "server01:cpu_usage_idle",
        "server02:mem_used_percent",
    )


def test_device_manager_cleanup_skips_offline_device_and_removes_only_active_stale_metric() -> None:
    clock = [100.0]
    manager = DeviceManager(expire_after=5, cleanup_delay=1, delete_delay=2, clock=lambda: clock[0])
    active_registry = manager.get_or_create_registry("server01", "server01")
    active_registry.update(
        MetricDescriptor(
            unique_key="cpu_usage_idle",
            measurement="cpu",
            tags={"host": "server01", "cpu": "cpu-total"},
            field="usage_idle",
            value=88.4,
            timestamp=1721664000,
            name="CPU Usage Idle",
            native_unit=None,
            suggested_device_class=None,
            suggested_state_class="measurement",
            entity_category=None,
        )
    )
    clock[0] = 107.0
    active_registry.last_any_metric = 107.0
    stale = active_registry.get("cpu_usage_idle")
    stale.last_updated = 100.0

    offline_registry = manager.get_or_create_registry("server02", "server02")
    offline_registry.last_any_metric = 100.0
    offline_registry.update(
        MetricDescriptor(
            unique_key="mem_used_percent",
            measurement="mem",
            tags={"host": "server02"},
            field="used_percent",
            value=41.2,
            timestamp=1721664000,
            name="Memory Used Percent",
            native_unit=None,
            suggested_device_class=None,
            suggested_state_class="measurement",
            entity_category=None,
        )
    )
    offline_registry.get("mem_used_percent").last_updated = 100.0

    writes: list[tuple[str, bool]] = []
    removed = manager.cleanup(on_write=lambda key, available, value: writes.append((key, available)))

    assert removed == ["server01:cpu_usage_idle"]
    assert writes == [("server01:cpu_usage_idle", False)]
    assert manager.get_metric("server01:cpu_usage_idle") is None
    assert manager.get_metric("server02:mem_used_percent") is not None


# --- helpers for the tests below -------------------------------------------


def _descriptor(
    unique_key: str,
    *,
    field: str | None = None,
    cleanup_policy: str = "AUTO",
) -> MetricDescriptor:
    return MetricDescriptor(
        unique_key=unique_key,
        measurement="m",
        tags={"host": "h"},
        field=field or unique_key,
        value=1,
        timestamp=1,
        name=unique_key,
        native_unit=None,
        suggested_device_class=None,
        suggested_state_class=None,
        entity_category=None,
        cleanup_policy=cleanup_policy,
    )


def test_cleanup_enforces_never_and_always_policies() -> None:
    clock = [0.0]
    registry = MetricRegistry(expire_after=10, cleanup_delay=5, clock=lambda: clock[0])
    registry.update(_descriptor("always_go", cleanup_policy="ALWAYS"))
    registry.update(_descriptor("never_go", cleanup_policy="NEVER"))
    registry.update(_descriptor("auto_go", cleanup_policy="AUTO"))
    assert len(registry) == 3

    clock[0] = 100.0
    removed = registry.cleanup()

    assert removed == ["always_go", "auto_go"]
    assert registry.get("never_go") is not None
    assert len(registry) == 1


def test_check_expiry_leaves_fresh_metrics_available() -> None:
    clock = [100.0]
    registry = MetricRegistry(expire_after=10, clock=lambda: clock[0])
    registry.update(_descriptor("fresh"))
    clock[0] = 105.0

    registry.check_expiry()

    assert registry.get("fresh").is_available is True


def test_apply_options_only_touches_affected_metrics() -> None:
    writes: list[tuple[str, bool]] = []
    registry = MetricRegistry(expire_after=10)
    registry.update(_descriptor("temp_input"))
    registry.update(_descriptor("usage_idle"))

    # Exclusion loop: only the matching metric flips unavailable.
    registry.apply_options(
        exclude_patterns=("temp_*",),
        on_write=lambda key, available, value: writes.append((key, available)),
    )
    assert writes == [("temp_input", False)]
    assert registry.get("usage_idle").is_available is True

    # Override loop: only metrics whose descriptor actually changed emit a write.
    writes.clear()
    registry.apply_options(
        field_overrides={"usage_idle": {"native_unit": "%"}},
        on_write=lambda key, available, value: writes.append((key, available)),
    )
    assert writes == [("usage_idle", True)]
    assert registry.get("usage_idle").descriptor.native_unit == "%"


def test_device_manager_get_scans_registries_and_reports_misses() -> None:
    manager = DeviceManager()
    manager.get_or_create_registry("a", "A").update(_descriptor("k1"))
    manager.get_or_create_registry("b", "B").update(_descriptor("k2"))

    assert manager.get("k1").device_id == "a"
    assert manager.get("k2").device_id == "b"
    assert manager.get("missing") is None
    assert len(manager) == 2


def test_get_metric_handles_legacy_and_unknown_keys() -> None:
    manager = DeviceManager()
    manager.get_or_create_registry("a", "A").update(_descriptor("k1"))

    assert manager.get_metric("k1") is not None  # legacy colon-less key
    assert manager.get_metric("zz:k1") is None  # unknown device id
    assert manager.get_metric("zz:nope") is None


def test_process_message_requires_a_parser() -> None:
    with pytest.raises(ValueError, match="parser is required"):
        DeviceManager().process_message("topic", "{}")


def test_set_parser_persists_for_subsequent_messages() -> None:
    manager = DeviceManager()
    manager.set_parser(TelegrafParser())

    manager.process_message(
        "telegraf/data",
        json.dumps(
            {
                "name": "mem",
                "tags": {"host": "h1"},
                "fields": {"used_percent": 41.2},
                "timestamp": 1700000000,
            }
        ),
    )

    assert manager.get_metric("h1:mem_used_percent") is not None


def test_cleanup_always_policy_notifies_on_write_before_removal() -> None:
    clock = [0.0]
    registry = MetricRegistry(expire_after=10, cleanup_delay=5, clock=lambda: clock[0])
    registry.update(_descriptor("always_go", cleanup_policy="ALWAYS"))
    writes: list[tuple[str, bool]] = []

    def on_write(key: str, available: bool, value: object) -> None:
        assert registry.get(key) is not None
        writes.append((key, available))

    clock[0] = 100.0
    removed = registry.cleanup(on_write=on_write)

    assert removed == ["always_go"]
    assert writes == [("always_go", False)]
    assert registry.get("always_go") is None


def test_fully_stale_device_marks_entities_unavailable_and_never_deletes_them() -> None:
    """Exit criterion: a fully-stale device goes unavailable but keeps every entity."""
    clock = [100.0]
    manager = DeviceManager(expire_after=5, cleanup_delay=1, delete_delay=2, clock=lambda: clock[0])
    registry = manager.get_or_create_registry("server09", "server09")
    registry.update(_descriptor("mem_used_percent"))
    registry.update(_descriptor("cpu_usage_idle"))
    clock[0] = 200.0  # far past expire_after for every metric on the device

    unavailable: list[tuple[str, bool]] = []
    manager.check_expiry(
        on_write=lambda key, available, value: unavailable.append((key, available))
    )

    assert set(unavailable) == {
        ("server09:mem_used_percent", False),
        ("server09:cpu_usage_idle", False),
    }
    assert all(
        manager.get_metric(f"server09:{key}").is_available is False
        for key in ("mem_used_percent", "cpu_usage_idle")
    )

    # Heartbeat long expired -> cleanup skips the whole device.
    writes: list[tuple[str, bool]] = []
    removed = manager.cleanup(
        on_write=lambda key, available, value: writes.append((key, available))
    )
    assert removed == []
    assert writes == []
    assert manager.keys() == ("server09:mem_used_percent", "server09:cpu_usage_idle")


def test_device_heartbeat_separates_offline_device_from_stale_metric() -> None:
    """Per-metric expiry marks availability everywhere; the heartbeat gates cleanup."""
    clock = [100.0]
    manager = DeviceManager(expire_after=5, cleanup_delay=1, delete_delay=2, clock=lambda: clock[0])

    online = manager.get_or_create_registry("server01", "server01")
    online.update(_descriptor("usage_idle"))
    online.last_any_metric = 104.0  # heartbeat fresh: device is ACTIVE at t=107
    online.get("usage_idle").last_updated = 90.0  # this individual metric is stale

    offline = manager.get_or_create_registry("server02", "server02")
    offline.update(_descriptor("used_percent"))
    offline.last_any_metric = 50.0  # nothing received for ages: device OFFLINE
    offline.get("used_percent").last_updated = 50.0

    clock[0] = 107.0
    unavailable: list[str] = []
    manager.check_expiry(on_write=lambda key, available, value: unavailable.append(key))
    assert set(unavailable) == {"server01:usage_idle", "server02:used_percent"}

    # Only the ACTIVE device's stale metric becomes a cleanup candidate...
    assert manager.cleanup() == ["server01:usage_idle"]
    assert manager.get_metric("server01:usage_idle") is None
    # ...the OFFLINE device keeps its entity however long the delay grows.
    clock[0] = 10_000.0
    assert manager.cleanup() == []
    stale_state = manager.get_metric("server02:used_percent")
    assert stale_state is not None
    assert stale_state.is_available is False
