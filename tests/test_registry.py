from custom_components.telegraf_mqtt.registry import MetricDescriptor, MetricRegistry


def test_registry_expiry_and_recovery() -> None:
    clock = [100.0]
    registry = MetricRegistry(expire_after=5, clock=lambda: clock[0])

    descriptor = MetricDescriptor(
        unique_key="cpu_cpu-total_usage_idle",
        measurement="cpu",
        tags={"host": "host1", "cpu": "cpu-total"},
        field="usage_idle",
        value=88.4,
        name="CPU Usage Idle",
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
            name="CPU Usage Idle",
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
        name="Memory Used Percent",
    )

    assert registry.update(descriptor) is False
    assert registry.get("mem_used_percent") is None

    descriptor = MetricDescriptor(
        unique_key="cpu_usage_idle",
        measurement="cpu",
        tags={"host": "host1"},
        field="used_percent",
        value=41.2,
        name="CPU Usage Idle",
    )
    assert registry.update(descriptor) is True
    state = registry.get("cpu_usage_idle")
    assert state is not None
    assert state.descriptor.native_unit == "%"
    assert state.descriptor.state_class == "measurement"


def test_registry_only_writes_state_on_real_change() -> None:
    calls: list[tuple[bool, bool]] = []
    registry = MetricRegistry(expire_after=5)

    descriptor = MetricDescriptor(
        unique_key="mem_used_percent",
        measurement="mem",
        tags={"host": "host1"},
        field="used_percent",
        value=41.2,
        name="Memory Used Percent",
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
            name="Memory Used Percent",
        ),
        on_write=on_write,
    )
    assert len(calls) == 2
