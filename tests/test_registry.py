from custom_components.telegraf_mqtt.models import MetricDescriptor
from custom_components.telegraf_mqtt.registry import MetricRegistry


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

    assert registry.update(descriptor, on_write=lambda key, available, value: writes.append(key), on_discovered=discovered.append)
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
