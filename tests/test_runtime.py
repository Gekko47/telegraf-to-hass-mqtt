from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import custom_components.telegraf_mqtt as integration
from custom_components.telegraf_mqtt.const import (
    CONF_DEVICE_NAME,
    CONF_EXCLUDE_PATTERNS,
    CONF_EXPIRE_AFTER,
    CONF_FIELD_OVERRIDES,
    CONF_TOPIC_PATTERN,
    SIGNAL_METRIC_UPDATED,
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
    monkeypatch.setattr(integration, "async_track_time_interval", fake_track_time_interval)
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


def test_unload_entry_succeeds_without_platform_support(monkeypatch) -> None:
    """Import-isolation guard: unload short-circuits when HA platforms are absent."""
    monkeypatch.setattr(integration, "Platform", None)
    entry = FakeConfigEntry()
    entry.runtime_data = integration.TelegrafMqttRuntimeData(manager=None, parser=None, manufacturer=None, model=None)

    assert asyncio.run(integration.async_unload_entry(FakeHass(), entry)) is True


def test_schedule_expiry_check_is_a_noop_without_time_tracking(monkeypatch) -> None:
    """Import-isolation guard: no periodic task when async_track_time_interval is absent."""
    monkeypatch.setattr(integration, "async_track_time_interval", None)
    hass = FakeHass()
    entry = FakeConfigEntry()

    integration._schedule_expiry_check(hass, entry)

    assert not hasattr(hass, "expiry_callback")
