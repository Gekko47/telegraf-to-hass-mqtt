from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from time import monotonic
from typing import Any

import custom_components.telegraf_mqtt as integration
from custom_components.telegraf_mqtt.const import (
    CONF_AUTO_DISCOVER,
    CONF_CLEANUP_DELAY,
    CONF_DELETE_DELAY,
    CONF_DEVICE_NAME,
    CONF_EXCLUDE_PATTERNS,
    CONF_EXPIRE_AFTER,
    CONF_FIELD_OVERRIDES,
    CONF_TOPIC_PATTERN,
    DEFAULT_DEVICE_ID_STRATEGY,
    MAX_EXPIRY_TICK_SECONDS,
    MIN_EXPIRY_TICK_SECONDS,
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
        # Real HA records every listener and fires them all; this fake
        # previously kept only the last one, which broke when Phase 10
        # registered a second listener (``_async_options_maybe_reload``).
        self._update_listeners: list[Callable[[FakeHass, FakeConfigEntry], Any]] = []

    def async_on_unload(self, callback: Callable[[], None]) -> None:
        self._unload_callbacks.append(callback)

    def add_update_listener(self, listener: Callable[[FakeHass, FakeConfigEntry], Any]) -> Callable[[], None]:
        self._update_listeners.append(listener)
        return lambda: None

    async def _fire_update_listeners(self, hass: FakeHass) -> None:
        for listener in self._update_listeners:
            await listener(hass, self)


class FakeMqtt:
    def __init__(self) -> None:
        self.unsubscribe_called = False
        self.unsubscribe_calls = 0
        self.topic_pattern: str | None = None
        self.message_callback: Callable[[Any], Any] | None = None
        # Every subscribe attempt in order -- the live auto-discover
        # toggle tests count main + snoop subscriptions explicitly.
        self.subscribe_calls: list[tuple[str, Callable[[Any], Any]]] = []

    async def async_subscribe(
        self, hass: FakeHass, topic_pattern: str, callback: Callable[[Any], Any]
    ) -> Callable[[], None]:
        self.subscribe_calls.append((topic_pattern, callback))
        self.topic_pattern = topic_pattern
        self.message_callback = callback
        return self.unsubscribe

    def unsubscribe(self) -> None:
        self.unsubscribe_called = True
        self.unsubscribe_calls += 1


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
    assert entry._update_listeners


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
    asyncio.run(entry._fire_update_listeners(hass))

    state = registry.get("mem_used_percent")
    assert state.is_available is False
    assert state.descriptor.native_unit == "%"
    assert dispatched[-1] == (
        SIGNAL_METRIC_UPDATED.format(entry_id=entry.entry_id),
        "host1:mem_used_percent",
    )


def test_options_update_reruns_device_id_repairs_immediately(monkeypatch) -> None:
    """A live options update re-runs the device-id collision/conflict
    Repairs checks right away instead of waiting for the next periodic
    tick (or, for a ``device_id_strategy`` change, the post-reload setup)."""
    _patch_runtime(monkeypatch)
    hass = FakeHass()
    entry = FakeConfigEntry(options={CONF_EXPIRE_AFTER: 5})
    calls: list[str] = []
    monkeypatch.setattr(
        integration,
        "check_device_id_collision",
        lambda hass_, entry_: calls.append("collision"),
    )
    monkeypatch.setattr(
        integration,
        "check_device_id_conflict",
        lambda hass_, entry_: calls.append("conflict"),
    )

    asyncio.run(integration.async_setup_entry(hass, entry))
    # Setup itself runs both checks once.
    assert calls == ["collision", "conflict"]

    entry.options = {CONF_EXPIRE_AFTER: 7}
    asyncio.run(entry._fire_update_listeners(hass))

    # The live options-update listener ran both checks again, in order,
    # before the (no-op) strategy reload listener.
    assert calls == ["collision", "conflict", "collision", "conflict"]


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
    entry = FakeConfigEntry(options={CONF_CLEANUP_DELAY: 30, CONF_DELETE_DELAY: 60})

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
    asyncio.run(entry._fire_update_listeners(hass))

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
    entry.options = {CONF_EXPIRE_AFTER: "abc", CONF_CLEANUP_DELAY: None}
    asyncio.run(entry._fire_update_listeners(hass))

    assert entry.runtime_data.manager._expire_after == DEFAULT_EXPIRE_AFTER
    assert entry.runtime_data.manager._cleanup_delay == DEFAULT_CLEANUP_DELAY
    assert registry._expire_after == DEFAULT_EXPIRE_AFTER
    # Expiry rescheduling consumed the normalized value (capped at
    # MAX_EXPIRY_TICK_SECONDS).
    assert hass.expiry_interval.total_seconds() == min(DEFAULT_EXPIRE_AFTER, MAX_EXPIRY_TICK_SECONDS)


def test_options_update_starts_snoop_live(monkeypatch) -> None:
    """Toggling ``auto_discover`` on through the live options-update
    listener starts the snoop listener immediately -- no reload required.
    The pre-fix behaviour silently did nothing until the entry was
    reloaded, leaving the user with a feature they believed was active."""
    fake_mqtt, _dispatched = _patch_runtime(monkeypatch)
    hass = FakeHass()
    entry = FakeConfigEntry(options={CONF_AUTO_DISCOVER: True})

    assert asyncio.run(integration.async_setup_entry(hass, entry)) is True

    # Setup opted in: real subscription + snoop (probe == the pattern).
    assert len(fake_mqtt.subscribe_calls) == 2
    assert fake_mqtt.subscribe_calls[0][0] == "telegraf/#"
    assert fake_mqtt.subscribe_calls[1][0] == "telegraf/#"
    assert entry.runtime_data.unsubscribe_snoop is not None

    # Re-submitting the same toggle is idempotent: no second snoop.
    entry.options = {CONF_AUTO_DISCOVER: True}
    asyncio.run(entry._fire_update_listeners(hass))
    assert len(fake_mqtt.subscribe_calls) == 2


def test_options_update_stops_snoop_live_and_restarts(monkeypatch) -> None:
    """Toggling ``auto_discover`` off through the live options-update
    listener tears the long-lived snoop subscription down immediately
    (previously it kept running and dispatching until the next
    reload/unload), and toggling it back on starts a fresh listener."""
    fake_mqtt, _dispatched = _patch_runtime(monkeypatch)
    hass = FakeHass()
    entry = FakeConfigEntry(options={CONF_AUTO_DISCOVER: True})

    assert asyncio.run(integration.async_setup_entry(hass, entry)) is True
    assert len(fake_mqtt.subscribe_calls) == 2

    entry.options = {CONF_AUTO_DISCOVER: False}
    asyncio.run(entry._fire_update_listeners(hass))
    # The snoop's unsubscribe ran; the handle is wiped so a later
    # reload/unload cannot double-tear it down.
    assert fake_mqtt.unsubscribe_calls == 1
    assert entry.runtime_data.unsubscribe_snoop is None

    # Toggling back on starts a fresh listener.
    entry.options = {CONF_AUTO_DISCOVER: True}
    asyncio.run(entry._fire_update_listeners(hass))
    assert len(fake_mqtt.subscribe_calls) == 3
    assert entry.runtime_data.unsubscribe_snoop is not None


def test_options_update_snoop_start_failure_is_non_fatal(monkeypatch) -> None:
    """A failing snoop start on the live path logs and moves on: the main
    subscription stays, no teardown handle is parked, and the entry keeps
    working (a later options update can retry the start)."""
    fake_mqtt, _dispatched = _patch_runtime(monkeypatch)
    hass = FakeHass()
    entry = FakeConfigEntry()

    assert asyncio.run(integration.async_setup_entry(hass, entry)) is True
    assert len(fake_mqtt.subscribe_calls) == 1

    original_subscribe = fake_mqtt.async_subscribe

    async def failing_subscribe(
        hass: FakeHass, topic_pattern: str, callback: Callable[[Any], Any]
    ) -> Callable[[], None]:
        if fake_mqtt.subscribe_calls:
            # Any call after the main subscription (i.e. the snoop) fails.
            raise RuntimeError("broker gone")
        return await original_subscribe(hass, topic_pattern, callback)

    monkeypatch.setattr(fake_mqtt, "async_subscribe", failing_subscribe)

    entry.options = {CONF_AUTO_DISCOVER: True}
    asyncio.run(entry._fire_update_listeners(hass))

    # The start attempt happened, failed, and was absorbed: no handle,
    # no crash, and the main subscription is untouched.
    assert len(fake_mqtt.subscribe_calls) == 1
    assert entry.runtime_data.unsubscribe_snoop is None


def test_options_update_auto_discover_is_a_noop_without_mqtt(monkeypatch) -> None:
    """Import-isolation guard: the live auto-discover toggle is a no-op
    when the MQTT component is absent (harness-free environments) -- no
    listener is started and the entry still applies its other options."""
    _patch_runtime(monkeypatch)
    monkeypatch.setattr(integration, "mqtt", None)
    hass = FakeHass()
    entry = FakeConfigEntry(options={CONF_AUTO_DISCOVER: True})

    assert asyncio.run(integration.async_setup_entry(hass, entry)) is True

    entry.options = {CONF_AUTO_DISCOVER: True}
    asyncio.run(entry._fire_update_listeners(hass))

    # No MQTT component -> no subscription possible, no teardown handle.
    assert entry.runtime_data.unsubscribe_snoop is None


def test_expiry_tick_interval_is_floored_and_capped(monkeypatch) -> None:
    """The synchronous full-registry scan's cadence is floored above 1s
    no matter how small ``expire_after`` is, and capped at
    ``MAX_EXPIRY_TICK_SECONDS``: a once-per-second loop-thread scan
    would add event-loop latency at fleet scale. Values inside the
    window pass through unchanged."""
    for expire_after, expected_seconds in (
        (1, MIN_EXPIRY_TICK_SECONDS),  # floored
        (7, 7),  # passthrough
        (120, MAX_EXPIRY_TICK_SECONDS),  # capped
    ):
        _patch_runtime(monkeypatch)
        hass = FakeHass()
        entry = FakeConfigEntry(options={CONF_EXPIRE_AFTER: expire_after})

        assert asyncio.run(integration.async_setup_entry(hass, entry)) is True
        assert hass.expiry_interval.total_seconds() == expected_seconds


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


def test_setup_loads_pre_phase_10_entry_without_crashing(monkeypatch) -> None:
    """A config entry created before Phase 10 -- whose options dict has
    none of the Phase-10 additions -- must still set up cleanly.

    Trip wire for the audit's "migration safety" item: a user who
    installed the integration pre-Phase-10 keeps their existing
    ``.storage/`` blob across the upgrade. ``async_setup_entry`` runs
    through ``_normalize_options`` which falls back to documented
    defaults via the ``_coerce_*`` helpers -- this test pins the
    contract so a future refactor that breaks the default-fallback path
    (e.g. a ``raw_options[CONF_DEVICE_ID_STRATEGY]`` that crashes when
    the key is absent) fails CI instead of crashing the user's entry
    in production.

    Three Phase-10 options are explicitly absent from the options dict
    below: ``device_id_strategy``, ``category_overrides``, and
    ``auto_discover``. The setup must:
      1. Return ``True`` (no exception, no entry failure).
      2. Build a manager that carries the documented defaults for the
         missing fields -- the user should not silently get a strategy
         flip, a category override, or a snoop listener they never
         opted into.
      3. Fire no dispatcher signals (entity-churn guard: setup must
         not pretend a brand-new metric appeared just because the
         options dict was migrated).
    """
    _fake_mqtt, dispatched = _patch_runtime(monkeypatch)
    hass = FakeHass()
    # Note: this is the exact options shape a user who last opened the
    # options flow in v1.1.x would have -- the three Phase-10 keys
    # below are *deliberately* missing to simulate a pre-10 entry.
    pre_phase_10_options = {
        CONF_EXPIRE_AFTER: 7,
        CONF_EXCLUDE_PATTERNS: ["mem_*"],
        CONF_FIELD_OVERRIDES: {"used_percent": {"native_unit": "%"}},
        CONF_CLEANUP_DELAY: 30,
        CONF_DELETE_DELAY: 60,
    }
    entry = FakeConfigEntry(options=pre_phase_10_options)

    assert asyncio.run(integration.async_setup_entry(hass, entry)) is True

    # Default-fallback contract: the manager must carry the documented
    # Phase-10 defaults, not crash and not pick up an arbitrary value.
    manager = entry.runtime_data.manager
    assert manager is not None
    assert manager._device_id_strategy == DEFAULT_DEVICE_ID_STRATEGY
    assert manager._category_overrides == {}

    # Entity-churn guard: setup must not pretend anything changed.
    # ``_patch_runtime`` collects every ``async_dispatcher_send`` call;
    # an empty list means no spurious new-metric / new-device signal
    # fired as a side effect of the migration.
    assert dispatched == []

    # Snoop listener is opt-in via ``auto_discover``; a pre-10 entry
    # never had that key, so the teardown handle must stay ``None``.
    assert entry.runtime_data.unsubscribe_snoop is None
