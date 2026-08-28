from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from time import monotonic
from typing import Any

import custom_components.telegraf_mqtt as integration
from custom_components.telegraf_mqtt.const import (
    CONF_CLEANUP_DELAY,
    CONF_DELETE_DELAY,
    CONF_DEVICE_NAME,
    CONF_EXCLUDE_PATTERNS,
    CONF_EXPIRE_AFTER,
    CONF_FIELD_OVERRIDES,
    CONF_TOPIC_PATTERN,
    SIGNAL_METRIC_UPDATED,
    SIGNAL_REMOVE_METRIC,
)
from custom_components.telegraf_mqtt.models import MetricDescriptor


@dataclass
class FakeConfigEntries:
    forwarded: list[tuple[Any, list[str]]] = field(default_factory=list)
    unloaded: list[tuple[Any, list[str]]] = field(default_factory=list)

    async def async_forward_entry_setups(self, entry: FakeConfigEntry, platforms: list[str]) -> None:
        self.forwarded.append((entry, platforms))

    async def async_unload_platforms(self, entry: FakeConfigEntry, platforms: list[str]) -> bool:
        self.unloaded.append((entry, platforms))
        return True


@dataclass
class FakeHass:
    config_entries: FakeConfigEntries = field(default_factory=FakeConfigEntries)


class FakeConfigEntry:
    def __init__(self, *, options: dict[str, Any] | None = None) -> None:
        self.entry_id = "entry-1"
        self.data = {
            CONF_TOPIC_PATTERN: "telegraf/#",
            CONF_DEVICE_NAME: "Telegraf MQTT",
        }
        self.options = options or {}
        self.runtime_data = None
        self._unload_callbacks: list[Callable[[], None]] = []
        self.update_listener: Callable[[FakeHass, FakeConfigEntry], Any] | None = None

    def async_on_unload(self, callback: Callable[[], None]) -> None:
        self._unload_callbacks.append(callback)

    def add_update_listener(self, listener: Callable[[FakeHass, FakeConfigEntry], Any]) -> Callable[[], None]:
        self.update_listener = listener
        return lambda: None


class FakeMqtt:
    def __init__(self) -> None:
        self.unsubscribe_called = False
        self.topic_pattern: str | None = None
        self.message_callback: Callable[[Any], Any] | None = None

    async def async_subscribe(
        self, hass: FakeHass, topic_pattern: str, callback: Callable[[Any], Any]
    ) -> Callable[[], None]:
        self.topic_pattern = topic_pattern
        self.message_callback = callback
        return self.unsubscribe

    def unsubscribe(self) -> None:
        self.unsubscribe_called = True


class FakePlatform:
    SENSOR = "sensor"


def _descriptor(unique_key: str = "mem_used_percent", value: float = 41.2) -> MetricDescriptor:
    return MetricDescriptor(
        unique_key=unique_key,
        measurement="mem",
        tags={"host": "host1"},
        field="used_percent",
        value=value,
        timestamp=1721664000,
        name="Memory Used Percent",
        native_unit=None,
        suggested_device_class=None,
        suggested_state_class="measurement",
        entity_category=None,
    )


def _patch_runtime(monkeypatch) -> tuple[FakeMqtt, list[tuple[str, str]]]:
    fake_mqtt = FakeMqtt()
    dispatched: list[tuple[str, str]] = []

    def fake_dispatch(hass: FakeHass, signal: str, unique_key: str) -> None:
        dispatched.append((signal, unique_key))

    def fake_dispatcher_connect(_hass: FakeHass, _signal: str, _target: Callable[..., Any]) -> Callable[[], None]:
        # Tests in this module don't exercise the entity-registry removal
        # path (that's covered by tests/test_phase6_lifecycle.py under the
        # real HA harness); recording the listener here is enough to keep
        # ``async_setup_entry`` reachable and prevent real HA dispatch from
        # touching the FakeHass' nonexistent ``.data`` attribute.
        return lambda: None

    def fake_track_time_interval(hass: FakeHass, callback: Callable[[Any], None], interval: Any) -> Callable[[], None]:
        hass.expiry_callback = callback
        hass.expiry_interval = interval
        hass.cancelled = False

        def cancel() -> None:
            hass.cancelled = True

        return cancel

    monkeypatch.setattr(integration, "Platform", FakePlatform)
    monkeypatch.setattr(integration, "PLATFORMS", [FakePlatform.SENSOR])
    monkeypatch.setattr(integration, "mqtt", fake_mqtt)
    monkeypatch.setattr(integration, "async_dispatcher_send", fake_dispatch)
    monkeypatch.setattr(integration, "async_dispatcher_connect", fake_dispatcher_connect)
    monkeypatch.setattr(integration, "async_track_time_interval", fake_track_time_interval)
    # Phase 7: stub the issue registry so ``check_overlapping_topics`` and
    # ``check_invalid_persisted_option`` are a no-op in harness-free tests
    # (they would otherwise try to talk to the real HA registry).
    monkeypatch.setattr(integration, "ir", None)
    return fake_mqtt, dispatched


def test_setup_applies_options_and_schedules_expiry(monkeypatch) -> None:
    fake_mqtt, _ = _patch_runtime(monkeypatch)
    hass = FakeHass()
    entry = FakeConfigEntry(
        options={
            CONF_EXPIRE_AFTER: 7,
            CONF_EXCLUDE_PATTERNS: ["mem_*"],
            CONF_FIELD_OVERRIDES: {"used_percent": {"native_unit": "%"}},
        }
    )

    assert asyncio.run(integration.async_setup_entry(hass, entry)) is True

    assert fake_mqtt.topic_pattern == "telegraf/#"
    registry = entry.runtime_data.manager.get_or_create_registry("host1", "host1")
    assert registry.update(_descriptor()) is False
    assert entry.runtime_data.cancel_expiry is not None
    assert entry.update_listener is not None


def test_expiry_dispatches_metric_update(monkeypatch) -> None:
    _fake_mqtt, dispatched = _patch_runtime(monkeypatch)
    hass = FakeHass()
    entry = FakeConfigEntry(options={CONF_EXPIRE_AFTER: 1})

    asyncio.run(integration.async_setup_entry(hass, entry))
    registry = entry.runtime_data.manager.get_or_create_registry("host1", "host1")
    registry.update(_descriptor())
    registry.get("mem_used_percent").last_updated = 0.0

    hass.expiry_callback(None)

    assert registry.get("mem_used_percent").is_available is False
    assert dispatched == [(SIGNAL_METRIC_UPDATED.format(entry_id=entry.entry_id), "host1:mem_used_percent")]


def test_options_update_applies_live_without_reload(monkeypatch) -> None:
    _fake_mqtt, dispatched = _patch_runtime(monkeypatch)
    hass = FakeHass()
    entry = FakeConfigEntry()

    asyncio.run(integration.async_setup_entry(hass, entry))
    registry = entry.runtime_data.manager.get_or_create_registry("host1", "host1")
    registry.update(_descriptor())

    entry.options = {
        CONF_EXCLUDE_PATTERNS: ["mem_*"],
        CONF_FIELD_OVERRIDES: {"used_percent": {"native_unit": "%"}},
        CONF_EXPIRE_AFTER: 5,
    }
    asyncio.run(entry.update_listener(hass, entry))

    state = registry.get("mem_used_percent")
    assert state.is_available is False
    assert state.descriptor.native_unit == "%"
    assert dispatched[-1] == (
        SIGNAL_METRIC_UPDATED.format(entry_id=entry.entry_id),
        "host1:mem_used_percent",
    )


def test_options_update_propagates_cleanup_and_delete_delays_without_reload(
    monkeypatch,
) -> None:
    """``cleanup_delay`` and ``delete_delay`` are wired through the live
    options-update path the same way ``expire_after`` is: changing them
    on a config entry replaces the startup values on both the manager
    and every per-device registry, and the integration is not reloaded.
    """
    _fake_mqtt, _dispatched = _patch_runtime(monkeypatch)
    hass = FakeHass()
    entry = FakeConfigEntry(
        options={CONF_CLEANUP_DELAY: 30, CONF_DELETE_DELAY: 60}
    )

    asyncio.run(integration.async_setup_entry(hass, entry))
    registry = entry.runtime_data.manager.get_or_create_registry("host1", "host1")
    registry.update(_descriptor())

    # Startup values landed where we expect (sanity check before the update).
    assert entry.runtime_data.manager._cleanup_delay == 30
    assert entry.runtime_data.manager._delete_delay == 60
    assert registry._cleanup_delay == 30
    assert registry._delete_delay == 60

    # Live update via the entry's update listener -- the same path
    # ``add_update_listener`` fires when the user changes OptionsFlow values.
    entry.options = {CONF_CLEANUP_DELAY: 5, CONF_DELETE_DELAY: 9}
    asyncio.run(entry.update_listener(hass, entry))

    # Manager-level values replaced.
    assert entry.runtime_data.manager._cleanup_delay == 5
    assert entry.runtime_data.manager._delete_delay == 9
    # Existing per-device registry also picked up the change (not just
    # newly-discovered devices created via ``get_or_create_registry``).
    assert registry._cleanup_delay == 5
    assert registry._delete_delay == 9


def test_live_update_recovers_invalid_persisted_values_without_reload(
    monkeypatch,
) -> None:
    """``_options_from_entry`` shares setup's safe normalization: a
    corrupted persisted value reaching the live-update listener (and the
    expiry reschedule it triggers) falls back to its default instead of
    raising, so no coercion error can escape the recovery path."""
    _fake_mqtt, _dispatched = _patch_runtime(monkeypatch)
    hass = FakeHass()
    entry = FakeConfigEntry(options={CONF_EXPIRE_AFTER: 5})
    asyncio.run(integration.async_setup_entry(hass, entry))
    registry = entry.runtime_data.manager.get_or_create_registry("host1", "host1")
    registry.update(_descriptor())

    from custom_components.telegraf_mqtt.const import (
        DEFAULT_CLEANUP_DELAY,
        DEFAULT_EXPIRE_AFTER,
    )

    # Corrupt the options the way a damaged .storage file would: the
    # update listener then re-normalizes with defaults instead of
    # crashing on int('abc') / int(None).
    entry.options = {CONF_EXPIRE_AFTER: 'abc', CONF_CLEANUP_DELAY: None}
    asyncio.run(entry.update_listener(hass, entry))

    assert entry.runtime_data.manager._expire_after == DEFAULT_EXPIRE_AFTER
    assert entry.runtime_data.manager._cleanup_delay == DEFAULT_CLEANUP_DELAY
    assert registry._expire_after == DEFAULT_EXPIRE_AFTER
    # Expiry rescheduling consumed the normalized value (capped at 30s).
    assert hass.expiry_interval.total_seconds() == min(DEFAULT_EXPIRE_AFTER, 30)


def test_unload_entry_succeeds_without_platform_support(monkeypatch) -> None:
    """Import-isolation guard: unload short-circuits when HA platforms are absent."""
    monkeypatch.setattr(integration, "Platform", None)
    entry = FakeConfigEntry()
    entry.runtime_data = integration.TelegrafMqttRuntimeData(
        manager=None, parser=None, parser_stats=None, manufacturer=None, model=None
    )

    assert asyncio.run(integration.async_unload_entry(FakeHass(), entry)) is True


def test_schedule_expiry_check_is_a_noop_without_time_tracking(monkeypatch) -> None:
    """Import-isolation guard: no periodic task when async_track_time_interval is absent."""
    monkeypatch.setattr(integration, "async_track_time_interval", None)
    hass = FakeHass()
    entry = FakeConfigEntry()

    integration._schedule_expiry_check(hass, entry)

    assert not hasattr(hass, "expiry_callback")


def test_scheduled_cleanup_dispatches_update_for_always_metric(monkeypatch) -> None:
    """Regression: scheduled cleanup notifies listeners when an ALWAYS metric is removed."""
    _fake_mqtt, dispatched = _patch_runtime(monkeypatch)
    hass = FakeHass()
    entry = FakeConfigEntry(options={CONF_EXPIRE_AFTER: 1})

    asyncio.run(integration.async_setup_entry(hass, entry))
    registry = entry.runtime_data.manager.get_or_create_registry("host1", "host1")
    registry.update(replace(_descriptor(), cleanup_policy="ALWAYS"))
    registry.last_any_metric = monotonic()

    hass.expiry_callback(None)

    assert dispatched == [
        (SIGNAL_METRIC_UPDATED.format(entry_id=entry.entry_id), "host1:mem_used_percent"),
        (SIGNAL_REMOVE_METRIC.format(entry_id=entry.entry_id), "host1:mem_used_percent"),
    ]
