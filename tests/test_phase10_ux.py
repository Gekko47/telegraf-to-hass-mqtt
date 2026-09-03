"""Phase 10 UX polish + strict typing gate."""

from __future__ import annotations

import asyncio
import types
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any

import pytest

import custom_components.telegraf_mqtt as integration
from custom_components.telegraf_mqtt.const import (
    CLEANUP_POLICY_AUTO,
    CLEANUP_POLICY_NEVER,
    CONF_AUTO_DISCOVER,
    CONF_TOPIC_PATTERN,
    PLATFORM_HINT_AUTO,
    PLATFORM_HINT_BINARY_SENSOR,
    PLATFORM_HINT_NONE,
    PLATFORM_HINT_SENSOR,
)
from custom_components.telegraf_mqtt.models import (
    MetricDescriptor,
    coerce_to_bool,
    is_bool_metric,
    is_numeric_metric,
    is_string_metric,
)
from custom_components.telegraf_mqtt.naming import apply_category_override
from custom_components.telegraf_mqtt.parsers.static import static_cleanup_policy


def _descriptor(field: str = "x", value: Any = 1.0) -> MetricDescriptor:
    return MetricDescriptor(
        unique_key=f"cpu_{field}",
        measurement="cpu",
        tags=MappingProxyType({"host": "h"}),
        field=field,
        value=value,
        timestamp=0.0,
        native_unit=None,
        suggested_device_class=None,
        suggested_state_class=None,
        entity_category=None,
    )


@dataclass
class _Clock:
    now: float = 0.0

    def __call__(self) -> float:
        return self.now


def _clock_fn() -> Any:
    return _Clock().__call__


# ---------------------------------------------------------------------------
# MetricValue TypeGuards
# ---------------------------------------------------------------------------


def test_is_bool_metric_recognises_true_false_not_int() -> None:
    assert is_bool_metric(True) is True
    assert is_bool_metric(False) is True
    # bool is a subclass of int in Python; the typeguard must exclude it
    # from the numeric branch.
    assert is_numeric_metric(True) is False
    assert is_numeric_metric(0) is True
    assert is_numeric_metric(0.0) is True
    assert is_numeric_metric("hi") is False
    assert is_string_metric("hi") is True
    assert is_string_metric(0) is False


def test_is_string_metric_excludes_bool_and_numbers() -> None:
    assert is_string_metric("ok") is True
    assert is_string_metric("") is True
    assert is_string_metric(0) is False
    assert is_string_metric(0.0) is False
    assert is_string_metric(True) is False


# ---------------------------------------------------------------------------
# coerce_to_bool
# ---------------------------------------------------------------------------


def test_coerce_to_bool_accepts_native_bool() -> None:
    assert coerce_to_bool(True) is True
    assert coerce_to_bool(False) is False


def test_coerce_to_bool_treats_zero_as_false() -> None:
    assert coerce_to_bool(0) is False
    assert coerce_to_bool(0.0) is False


def test_coerce_to_bool_treats_nonzero_as_true() -> None:
    assert coerce_to_bool(1) is True
    assert coerce_to_bool(42) is True
    assert coerce_to_bool(0.5) is True
    assert coerce_to_bool(-1) is True


def test_coerce_to_bool_recognises_string_truthy() -> None:
    assert coerce_to_bool("true") is True
    assert coerce_to_bool("TRUE") is True
    assert coerce_to_bool("1") is True
    assert coerce_to_bool("yes") is True
    assert coerce_to_bool("on") is True
    assert coerce_to_bool("false") is False
    assert coerce_to_bool("0") is False
    assert coerce_to_bool("") is False


# ---------------------------------------------------------------------------
# Descriptor Literal types
# ---------------------------------------------------------------------------


def test_cleanup_policy_literal_defaults_to_auto() -> None:
    """The descriptor default cleanup_policy is the AUTO Literal."""
    descriptor = _descriptor()
    assert descriptor.cleanup_policy == CLEANUP_POLICY_AUTO


def test_platform_hint_literal_defaults_to_auto() -> None:
    """The descriptor default platform_hint is the auto Literal."""
    descriptor = _descriptor()
    assert descriptor.platform_hint == PLATFORM_HINT_AUTO


# ---------------------------------------------------------------------------
# static_cleanup_policy
# ---------------------------------------------------------------------------


def test_static_cleanup_policy_marks_static_fields_never() -> None:
    assert static_cleanup_policy("system", "n_cpus") == CLEANUP_POLICY_NEVER
    assert static_cleanup_policy("cpu", "model_name") == CLEANUP_POLICY_NEVER
    assert static_cleanup_policy("system", "uptime_format") == CLEANUP_POLICY_NEVER


def test_static_cleanup_policy_default_is_auto() -> None:
    assert static_cleanup_policy("cpu", "usage_idle") == CLEANUP_POLICY_AUTO
    assert static_cleanup_policy("mem", "used_percent") == CLEANUP_POLICY_AUTO
    assert static_cleanup_policy("disk", "used_percent") == CLEANUP_POLICY_AUTO


def test_static_cleanup_policy_is_case_insensitive() -> None:
    assert static_cleanup_policy("CPU", "Model_Name") == CLEANUP_POLICY_NEVER
    assert static_cleanup_policy("System", "Uptime_Format") == CLEANUP_POLICY_NEVER


# ---------------------------------------------------------------------------
# apply_category_override
# ---------------------------------------------------------------------------


def test_category_override_returns_heuristic_when_no_override_present() -> None:
    assert apply_category_override("disk", "used_percent", "k", None) == "diagnostic"
    assert apply_category_override("cpu", "uptime", "k", None) == "diagnostic"
    assert apply_category_override("cpu", "usage_idle", "k", None) is None


def test_category_override_returns_heuristic_for_unknown_key() -> None:
    overrides = {"other_key": None}
    assert apply_category_override("disk", "used_percent", "k", overrides) == "diagnostic"
    assert apply_category_override("cpu", "usage_idle", "k", overrides) is None


def test_category_override_can_force_diagnostic() -> None:
    overrides = {"cpu_usage_idle": "diagnostic"}
    assert apply_category_override("cpu", "usage_idle", "cpu_usage_idle", overrides) == "diagnostic"


def test_category_override_can_force_config() -> None:
    overrides = {"k": "config"}
    assert apply_category_override("cpu", "usage_idle", "k", overrides) == "config"


def test_category_override_can_clear_category() -> None:
    overrides_none = {"disk_used_percent": None}
    overrides_empty = {"disk_used_percent": ""}
    assert apply_category_override("disk", "used_percent", "disk_used_percent", overrides_none) is None
    assert apply_category_override("disk", "used_percent", "disk_used_percent", overrides_empty) is None


def test_category_override_ignores_unknown_value() -> None:
    overrides = {"k": "garbage-value"}
    assert apply_category_override("disk", "used_percent", "k", overrides) == "diagnostic"
    assert apply_category_override("cpu", "usage_idle", "k", overrides) is None


# ---------------------------------------------------------------------------
# Registry: category_overrides, platform_hint, pending_cleanup
# ---------------------------------------------------------------------------


def _registry(**kwargs: Any) -> Any:
    from custom_components.telegraf_mqtt.registry import MetricRegistry

    return MetricRegistry(clock=_clock_fn(), **kwargs)


def test_registry_init_accepts_category_overrides() -> None:
    overrides = {"cpu_usage_idle": "config"}
    registry = _registry(category_overrides=overrides)
    assert registry._category_overrides == overrides


def test_registry_init_accepts_device_id_strategy() -> None:
    registry = _registry(device_id_strategy="topic_only")
    assert registry._device_id_strategy == "topic_only"


def test_registry_init_falls_back_to_default_for_invalid_strategy() -> None:
    registry = _registry(device_id_strategy="not-a-strategy")
    assert registry._device_id_strategy == "host"


def test_registry_apply_options_propagates_category_overrides() -> None:
    registry = _registry()
    registry.apply_options(category_overrides={"k": "config"})
    assert registry._category_overrides == {"k": "config"}


# --- Phase 10: category_overrides accept globs (parity with exclude_patterns)


def test_category_override_matcher_prefers_exact_over_glob() -> None:
    from custom_components.telegraf_mqtt.naming import match_category_override_key

    overrides = {"cpu_x": None, "cpu_*": "config"}
    assert match_category_override_key("cpu_x", overrides) == "cpu_x"


def test_category_override_matcher_first_glob_in_insertion_order_wins() -> None:
    from custom_components.telegraf_mqtt.naming import match_category_override_key

    # Non-overlapping globs: "cpu_usage*" cannot match "cpu_used_percent".
    overrides = {"cpu_usage*": "config", "cpu_*": "diagnostic"}
    assert match_category_override_key("cpu_usage_idle", overrides) == "cpu_usage*"
    assert match_category_override_key("cpu_used_percent", overrides) == "cpu_*"


def test_category_override_matcher_no_match_returns_none() -> None:
    from custom_components.telegraf_mqtt.naming import match_category_override_key

    assert match_category_override_key("mem_used", {"cpu_*": "config"}) is None
    # A key without glob characters stays exact-only: it can never
    # pattern-match a different unique_key.
    assert match_category_override_key("mem_used", {"mem": "config"}) is None


def test_apply_category_override_glob_pattern_applies() -> None:
    assert apply_category_override("cpu", "x", "cpu_x", {"cpu_*": "diagnostic"}) == "diagnostic"
    # A glob whose value clears the category behaves like the exact form.
    assert apply_category_override("cpu", "x", "cpu_x", {"cpu_*": None}) is None


def test_apply_category_override_glob_no_match_keeps_heuristic() -> None:
    # ("mem", "used_percent") resolves to no category; a non-matching
    # glob must not change that.
    assert apply_category_override("mem", "used_percent", "mem_used_percent", {"cpu_*": "config"}) is None


def test_registry_glob_category_override_resolves_live() -> None:
    """A glob category override re-resolves existing states and emits a
    write when the resolved category actually changes."""
    registry = _registry()
    registry.update(_descriptor("x", 1.0))
    writes: list[tuple[str, bool]] = []
    registry.apply_options(
        category_overrides={"cpu_*": "diagnostic"},
        on_write=lambda key, available, value: writes.append((key, available)),
    )
    assert writes == [("cpu_x", True)]
    assert registry.get("cpu_x").descriptor.entity_category == "diagnostic"


def test_registry_exact_category_override_still_beats_glob_live() -> None:
    registry = _registry()
    registry.update(_descriptor("x", 1.0))
    registry.apply_options(category_overrides={"cpu_x": None, "cpu_*": "diagnostic"})
    # Exact key clears the category; the glob never gets a say. The
    # resolved value equals the descriptor's baseline category, so no
    # re-write is emitted either.
    assert registry.get("cpu_x").descriptor.entity_category is None


def test_registry_apply_options_propagates_device_id_strategy() -> None:
    registry = _registry()
    registry.apply_options(device_id_strategy="host_topic")
    assert registry._device_id_strategy == "host_topic"


def test_registry_apply_options_rejects_invalid_strategy() -> None:
    registry = _registry(device_id_strategy="host")
    registry.apply_options(device_id_strategy="garbage")
    assert registry._device_id_strategy == "host"


def test_field_override_platform_none_drops_existing_state() -> None:
    registry = _registry()
    registry.update(_descriptor("x", 1.0))
    assert len(registry) == 1
    registry._field_overrides = {"x": {"platform": "none"}}
    registry.update(_descriptor("x", 2.0))
    assert len(registry) == 0


def test_field_override_platform_none_skips_discovery() -> None:
    discovered: list[str] = []

    def _on_discovered(key: str) -> None:
        discovered.append(key)

    registry = _registry()
    registry._field_overrides = {"x": {"platform": "none"}}
    registry.update(_descriptor("x", 1.0), on_discovered=_on_discovered)
    assert discovered == []


def test_field_override_platform_binary_sensor_coerces_int_to_bool() -> None:
    registry = _registry()
    registry._field_overrides = {"x": {"platform": "binary_sensor"}}
    registry.update(_descriptor("x", 1))
    state = registry.get("cpu_x")
    assert state is not None
    assert state.descriptor.value is True
    assert state.descriptor.platform_hint == "binary_sensor"


def test_field_override_platform_sensor_preserves_value() -> None:
    registry = _registry()
    registry._field_overrides = {"x": {"platform": "sensor"}}
    registry.update(_descriptor("x", 42))
    state = registry.get("cpu_x")
    assert state is not None
    assert state.descriptor.value == 42
    assert state.descriptor.platform_hint == "sensor"


def test_field_override_unknown_platform_falls_back_to_auto() -> None:
    registry = _registry()
    registry._field_overrides = {"x": {"platform": "garbage"}}
    registry.update(_descriptor("x", 1.0))
    state = registry.get("cpu_x")
    assert state is not None
    assert state.descriptor.platform_hint == "auto"


def test_field_override_no_platform_key_keeps_auto() -> None:
    registry = _registry()
    registry._field_overrides = {"x": {"native_unit": "B"}}
    registry.update(_descriptor("x", 1.0))
    state = registry.get("cpu_x")
    assert state is not None
    assert state.descriptor.platform_hint == "auto"


def test_category_override_layers_onto_existing_field_state() -> None:
    registry = _registry(category_overrides={"cpu_x": None})
    registry.update(_descriptor("x", 1.0))
    state = registry.get("cpu_x")
    assert state is not None
    assert state.descriptor.entity_category is None


def test_registry_category_override_can_force_diagnostic() -> None:
    registry = _registry(category_overrides={"cpu_x": "diagnostic"})
    registry.update(_descriptor("x", 1.0))
    state = registry.get("cpu_x")
    assert state is not None
    assert state.descriptor.entity_category == "diagnostic"


def test_registry_category_override_can_force_config() -> None:
    registry = _registry(category_overrides={"cpu_x": "config"})
    registry.update(_descriptor("x", 1.0))
    state = registry.get("cpu_x")
    assert state is not None
    assert state.descriptor.entity_category == "config"


def test_apply_options_with_category_overrides_emits_write_for_changed_state() -> None:
    writes: list[tuple[str, bool, Any]] = []
    registry = _registry()
    registry.update(_descriptor("x", 1.0))
    registry.apply_options(
        category_overrides={"cpu_x": "diagnostic"},
        on_write=lambda key, available, value: writes.append((key, available, value)),
    )
    assert len(writes) == 1
    assert writes[0][0] == "cpu_x"


# ---------------------------------------------------------------------------
# pending_cleanup
# ---------------------------------------------------------------------------


def test_pending_cleanup_empty_when_nothing_expired() -> None:
    registry = _registry(expire_after=120, cleanup_delay=300)
    registry.update(_descriptor("x", 1.0))
    assert registry.pending_cleanup() == []


def test_pending_cleanup_lists_metrics_in_candidate_state() -> None:
    clock = _Clock()
    from custom_components.telegraf_mqtt.registry import MetricRegistry

    registry = MetricRegistry(clock=clock, expire_after=10, cleanup_delay=300)
    registry.update(_descriptor("x", 1.0))
    clock.now = 100.0
    registry.check_expiry()
    candidates = registry.pending_cleanup()
    assert len(candidates) == 1
    entry = candidates[0]
    assert entry["unique_key"] == "cpu_x"
    assert entry["cleanup_candidate_since"] == 100.0
    assert entry["seconds_until_removal"] == 300.0


def test_pending_cleanup_excludes_never_policy() -> None:
    clock = _Clock()
    from custom_components.telegraf_mqtt.registry import MetricRegistry

    registry = MetricRegistry(clock=clock, expire_after=10, cleanup_delay=300)
    descriptor = MetricDescriptor(
        unique_key="cpu_model_name",
        measurement="cpu",
        tags=MappingProxyType({"host": "h"}),
        field="model_name",
        value="Intel",
        timestamp=0.0,
        native_unit=None,
        suggested_device_class=None,
        suggested_state_class=None,
        entity_category=None,
        cleanup_policy="NEVER",
    )
    registry.update(descriptor)
    clock.now = 100.0
    registry.check_expiry()
    assert registry.pending_cleanup() == []


def test_pending_cleanup_is_sorted_oldest_first() -> None:
    clock = _Clock()
    from custom_components.telegraf_mqtt.registry import MetricRegistry

    registry = MetricRegistry(clock=clock, expire_after=5, cleanup_delay=300)
    registry.update(_descriptor("a", 1.0))
    clock.now = 100.0
    registry.update(_descriptor("b", 2.0))
    clock.now = 200.0
    registry.update(_descriptor("c", 3.0))
    clock.now = 1000.0
    registry.check_expiry()
    candidates = registry.pending_cleanup()
    assert [c["unique_key"] for c in candidates] == ["cpu_a", "cpu_b", "cpu_c"]


# ---------------------------------------------------------------------------
# DeviceManager: seen_hosts, seen_topics, record_seen_host, pending_cleanup_all
# ---------------------------------------------------------------------------


def _manager(**kwargs: Any) -> Any:
    from custom_components.telegraf_mqtt.parser import TelegrafParser
    from custom_components.telegraf_mqtt.registry import DeviceManager

    manager = DeviceManager(clock=_clock_fn(), **kwargs)
    manager.set_parser(TelegrafParser())
    return manager


def test_device_manager_starts_with_no_seen_hosts() -> None:
    manager = _manager()
    assert manager.seen_hosts == frozenset()
    assert manager.seen_topics == frozenset()
    assert manager.has_received_messages() is False


def test_record_seen_host_records_host_and_topic() -> None:
    manager = _manager()
    manager.record_seen_host("host-a", "telegraf/host-a/cpu")
    assert manager.seen_hosts == frozenset({"host-a"})
    assert manager.seen_topics == frozenset({"telegraf/host-a/cpu"})
    assert manager.has_received_messages() is True
    assert manager.first_message_at is not None
    assert manager.last_message_at is not None


def test_record_seen_host_records_first_and_last_timestamps() -> None:
    clock = _Clock()
    from custom_components.telegraf_mqtt.registry import DeviceManager

    manager = DeviceManager(clock=clock)
    manager.record_seen_host("h1", "t1")
    clock.now = 100.0
    manager.record_seen_host("h2", "t2")
    assert manager.first_message_at == 0.0
    assert manager.last_message_at == 100.0


def test_process_message_records_seen_host_from_descriptor_tag() -> None:
    manager = _manager()
    payload = b'{"name":"cpu","tags":{"host":"host-a"},"fields":{"usage_idle":12.3},"timestamp":1721664000}'
    manager.process_message("telegraf/host-a/cpu", payload)
    assert "host-a" in manager.seen_hosts
    assert "telegraf/host-a/cpu" in manager.seen_topics


def test_process_message_records_seen_topic_even_when_parser_drops() -> None:
    manager = _manager()
    manager.process_message("telegraf/host-a/garbage", b"not json")
    assert "telegraf/host-a/garbage" in manager.seen_topics


def test_pending_cleanup_all_aggregates_across_devices() -> None:
    clock = _Clock()
    from custom_components.telegraf_mqtt.parser import TelegrafParser
    from custom_components.telegraf_mqtt.registry import DeviceManager

    manager = DeviceManager(clock=clock, expire_after=5, cleanup_delay=300)
    manager.set_parser(TelegrafParser())
    payload_a = b'{"name":"cpu","tags":{"host":"host-a"},"fields":{"usage_idle":12.3},"timestamp":1721664000}'
    payload_b = b'{"name":"mem","tags":{"host":"host-b"},"fields":{"used_percent":50.0},"timestamp":1721664000}'
    manager.process_message("telegraf/host-a/cpu", payload_a)
    manager.process_message("telegraf/host-b/mem", payload_b)
    clock.now = 100.0
    manager.check_expiry()
    result = manager.pending_cleanup_all()
    # Device keys include a SHA digest suffix when the host tag contains
    # characters that the slugifier strips (the ``-`` here), so we look up
    # the per-device entries by prefix rather than exact key.
    assert len(result) == 2
    host_a_key = next(k for k in result if k.startswith("host_a"))
    host_b_key = next(k for k in result if k.startswith("host_b"))
    assert len(result[host_a_key]) == 1
    assert len(result[host_b_key]) == 1


def test_pending_cleanup_all_omits_empty_registries() -> None:
    manager = _manager()
    manager.process_message(
        "telegraf/host-a/cpu",
        b'{"name":"cpu","tags":{"host":"host-a"},"fields":{"usage_idle":12.3},"timestamp":1721664000}',
    )
    assert manager.pending_cleanup_all() == {}


# ---------------------------------------------------------------------------
# device_id_strategy
# ---------------------------------------------------------------------------


def test_device_id_strategy_host_is_default() -> None:
    manager = _manager()
    assert manager._device_id_strategy == "host"


def test_device_id_strategy_topic_only_uses_topic_root() -> None:
    manager = _manager(device_id_strategy="topic_only")
    payload = b'{"name":"cpu","tags":{"host":"ignored"},"fields":{"usage_idle":12.3},"timestamp":1721664000}'
    # The first non-wildcard topic segment is the device id seed under
    # ``topic_only`` -- not the second. The host tag is intentionally
    # set to a different value to prove the strategy ignores it.
    manager.process_message("telegraf/host-x/cpu", payload)
    devices = list(manager.devices.keys())
    assert devices == ["telegraf"]


def test_device_id_strategy_host_topic_appends_second_segment() -> None:
    manager = _manager(device_id_strategy="host_topic")
    payload = b'{"name":"cpu","tags":{},"fields":{"usage_idle":12.3},"timestamp":1721664000}'
    manager.process_message("telegraf/server01/cpu", payload)
    devices = list(manager.devices.keys())
    assert devices == ["telegraf_server01"]


def test_device_id_strategy_host_topic_falls_back_to_first_when_only_one_segment() -> None:
    manager = _manager(device_id_strategy="host_topic")
    payload = b'{"name":"cpu","tags":{},"fields":{"usage_idle":12.3},"timestamp":1721664000}'
    # Single non-wildcard segment after ``/``-stripping -- there is no
    # second segment, so the device id collapses to the first segment.
    manager.process_message("telegraf", payload)
    devices = list(manager.devices.keys())
    assert devices == ["telegraf"]


def test_device_id_strategy_host_topic_prefers_host_tag() -> None:
    manager = _manager(device_id_strategy="host_topic")
    payload = b'{"name":"cpu","tags":{"host":"real-host"},"fields":{"usage_idle":12.3},"timestamp":1721664000}'
    manager.process_message("telegraf/anything/cpu", payload)
    devices = list(manager.devices.keys())
    assert len(devices) == 1
    assert devices[0].startswith("real_host")


def test_device_id_strategy_host_topic_disambiguates_degenerate_host_via_topic() -> None:
    """The bug report's flagship scenario: two distinct real hosts both
    publishing ``host=localhost`` must end up on different devices when
    ``device_id_strategy="host_topic"`` is selected.

    The integration cannot trust the host tag under this strategy -- that
    is precisely why the user picked it. Each payload's second-level
    topic segment is the only per-machine signal the broker carries, so
    the device id has to incorporate it.
    """
    manager = _manager(device_id_strategy="host_topic")
    payload = b'{"name":"cpu","tags":{"host":"localhost"},"fields":{"usage_idle":12.3},"timestamp":1721664000}'
    manager.process_message("telegraf/server01/cpu", payload)
    manager.process_message("telegraf/server02/cpu", payload)
    devices = sorted(manager.devices.keys())
    assert devices == ["telegraf_server01", "telegraf_server02"]


def test_device_id_strategy_host_topic_degenerate_host_match_is_case_insensitive() -> None:
    """``LOCALHOST`` and ``127.0.0.1`` are degenerate too; comparison must
    be lowercased so a Telegraf agent publishing ``host=LOCALHOST`` does
    not silently re-introduce the collision the option was meant to fix.
    """
    manager = _manager(device_id_strategy="host_topic")
    payload_upper = b'{"name":"cpu","tags":{"host":"LOCALHOST"},"fields":{"usage_idle":1.0},"timestamp":1}'
    payload_loopback = b'{"name":"cpu","tags":{"host":"127.0.0.1"},"fields":{"usage_idle":2.0},"timestamp":2}'
    manager.process_message("telegraf/server03/cpu", payload_upper)
    manager.process_message("telegraf/server04/cpu", payload_loopback)
    devices = sorted(manager.devices.keys())
    assert devices == ["telegraf_server03", "telegraf_server04"]


def test_device_id_strategy_host_topic_treats_non_string_host_as_degenerate() -> None:
    """A non-string host tag is treated as degenerate under ``host_topic``.

    The bundled parsers stringify every tag, but ``DeviceManager`` is a
    public surface that accepts descriptors from any parser: a malformed
    host tag (e.g. a numeric value) must be treated as untrustworthy --
    the same as ``localhost`` -- so the topic tree still anchors the
    device id instead of the manager stringifying and trusting it.
    """
    manager = _manager(device_id_strategy="host_topic")
    descriptor = replace(
        _descriptor("usage_idle", 1.0),
        tags=MappingProxyType({"host": 5}),
    )
    assert manager._derive_device_id("telegraf/server05/cpu", descriptor) == "telegraf_server05"


def test_device_id_strategy_default_host_still_uses_localhost_when_topic_irrelevant() -> None:
    """Regression guard: changing the ``host_topic`` fallback must not
    touch the default ``"host"`` strategy. A user who has not opted in
    to ``host_topic`` continues to trust the host tag verbatim, even
    when that tag is the well-known degenerate ``localhost`` -- they
    get a single device either way.
    """
    manager = _manager()  # default strategy is "host"
    payload = b'{"name":"cpu","tags":{"host":"localhost"},"fields":{"usage_idle":1.0},"timestamp":1}'
    manager.process_message("telegraf/server01/cpu", payload)
    manager.process_message("telegraf/server02/cpu", payload)
    devices = list(manager.devices.keys())
    assert devices == ["localhost"]


# ---------------------------------------------------------------------------
# device_id_strategy reload listener
# ---------------------------------------------------------------------------


@dataclass
class _ListenerHass:
    """Minimal hass double: records ``async_reload`` calls."""

    config_entries: Any = None

    def __post_init__(self) -> None:
        if self.config_entries is None:
            self.config_entries = _ListenerConfigEntries()


@dataclass
class _ListenerConfigEntries:
    reload_calls: list[str] = field(default_factory=list)

    async def async_reload(self, entry_id: str) -> None:
        self.reload_calls.append(entry_id)


@dataclass
class _ListenerEntry:
    """Minimal entry double exposing only what the reload listener reads."""

    entry_id: str = "entry-1"
    options: dict = field(default_factory=dict)
    runtime_data: Any = None


def test_device_id_strategy_change_triggers_config_entry_reload() -> None:
    """The ``_async_options_maybe_reload`` listener fires a config-entry
    reload when the user picks a new strategy, because the existing
    ``DeviceManager.devices`` dict is keyed by the old strategy's slugs
    and a live apply would leave them orphaned while new traffic
    creates a parallel set of registries.

    Same-strategy updates must NOT trigger a reload.
    """
    from custom_components.telegraf_mqtt.registry import DeviceManager

    # Build a manager as the live update listener would, then assert the
    # public read-only accessor matches the private slot.
    manager = DeviceManager(device_id_strategy="host")
    assert manager.device_id_strategy == "host"

    hass = _ListenerHass()
    entry = _ListenerEntry(
        options={},
        runtime_data=integration.TelegrafMqttRuntimeData(
            manager=manager,
            parser=None,
            parser_stats=None,
            manufacturer=None,
            model=None,
        ),
    )

    async def _run() -> None:
        # No-op: same strategy the manager already has.
        await integration._async_options_maybe_reload(hass, entry)
        assert hass.config_entries.reload_calls == []

        # Strategy changed: must trigger exactly one reload.
        entry.options = {"device_id_strategy": "topic_only"}
        await integration._async_options_maybe_reload(hass, entry)
        assert hass.config_entries.reload_calls == [entry.entry_id]

    asyncio.run(_run())


def test_device_id_strategy_reload_listener_is_a_noop_when_runtime_missing() -> None:
    """Defensive guard: a config entry without ``runtime_data`` (e.g.
    mid-unload) must not raise, and must not fire a reload."""
    hass = _ListenerHass()
    entry = _ListenerEntry(options={"device_id_strategy": "topic_only"}, runtime_data=None)

    async def _run() -> None:
        await integration._async_options_maybe_reload(hass, entry)

    asyncio.run(_run())
    assert hass.config_entries.reload_calls == []


def test_device_id_strategy_reload_listener_is_a_noop_when_manager_missing() -> None:
    """``runtime_data.manager`` is typed ``DeviceManager | None`` for
    the unload path; the listener must tolerate ``None`` and skip the
    reload without raising."""
    entry = _ListenerEntry(
        options={"device_id_strategy": "topic_only"},
        runtime_data=integration.TelegrafMqttRuntimeData(
            manager=None,
            parser=None,
            parser_stats=None,
            manufacturer=None,
            model=None,
        ),
    )
    hass = _ListenerHass()

    async def _run() -> None:
        await integration._async_options_maybe_reload(hass, entry)

    asyncio.run(_run())
    assert hass.config_entries.reload_calls == []


# ---------------------------------------------------------------------------
# Manager apply_options propagation
# ---------------------------------------------------------------------------


def test_manager_apply_options_propagates_category_overrides_to_existing_registry() -> None:
    manager = _manager()
    manager.process_message(
        "telegraf/host-a/cpu",
        b'{"name":"cpu","tags":{"host":"host-a"},"fields":{"usage_idle":12.3},"timestamp":1721664000}',
    )
    # unique_key for the published field is ``cpu_usage_idle``; the
    # override map must use the same key.
    manager.apply_options(category_overrides={"cpu_usage_idle": "diagnostic"})
    # The composite key is the slugified device id + the unique key.
    device_keys = list(manager.devices.keys())
    state = manager.get_metric(f"{device_keys[0]}:cpu_usage_idle")
    assert state is not None
    assert state.descriptor.entity_category == "diagnostic"


def test_manager_apply_options_propagates_strategy_to_new_registries() -> None:
    manager = _manager()
    manager.apply_options(device_id_strategy="topic_only")
    assert manager._device_id_strategy == "topic_only"


def test_manager_apply_options_rejects_invalid_strategy() -> None:
    manager = _manager()
    manager.apply_options(device_id_strategy="garbage")
    assert manager._device_id_strategy == "host"


# ---------------------------------------------------------------------------
# Coverage pinpoints
# ---------------------------------------------------------------------------


def test_apply_options_with_unchanged_category_does_not_emit() -> None:
    """When the override doesn't change the category, no write is emitted."""
    writes: list[tuple[str, bool, Any]] = []
    registry = _registry(category_overrides={"cpu_x": "diagnostic"})
    registry.update(_descriptor("x", 1.0))
    # First update already produced the category. Re-applying the same
    # override should not re-emit a write because the descriptor is
    # already in its post-override state.
    writes.clear()
    registry.apply_options(
        category_overrides={"cpu_x": "diagnostic"},
        on_write=lambda key, available, value: writes.append((key, available, value)),
    )
    assert writes == []


def test_field_override_platform_none_with_existing_state_emits_write() -> None:
    """Transitioning an existing state to platform_hint=none emits a write."""
    writes: list[tuple[str, bool, Any]] = []
    registry = _registry()
    registry.update(_descriptor("x", 42))
    writes.clear()
    registry._field_overrides = {"x": {"platform": "none"}}
    registry.update(_descriptor("x", 43), on_write=lambda key, available, value: writes.append((key, available, value)))
    assert len(writes) == 1
    assert writes[0] == ("cpu_x", False, 42)


def test_manager_apply_options_stores_category_overrides_at_manager_level() -> None:
    """The manager stores category_overrides in its own slot."""
    manager = _manager()
    manager.apply_options(category_overrides={"k": "config"})
    assert manager._category_overrides == {"k": "config"}


def test_manager_apply_options_stores_device_id_strategy_at_manager_level() -> None:
    """The manager stores a valid device_id_strategy in its own slot."""
    manager = _manager()
    manager.apply_options(device_id_strategy="topic_only")
    assert manager._device_id_strategy == "topic_only"


def test_coerce_to_bool_falls_back_to_false_for_unrecognised_type() -> None:
    """``coerce_to_bool`` returns False for types outside the MetricValue union."""
    # Pass a list to bypass the MetricValue static type -- the runtime
    # defensive path is line 58 of models.py. The function must not raise.
    result = coerce_to_bool([1, 2, 3])  # type: ignore[arg-type]
    assert result is False


# ---------------------------------------------------------------------------
# SnoopListener
# ---------------------------------------------------------------------------


def _payload(name: str = "cpu", host: str = "host-a") -> bytes:
    import json

    return json.dumps(
        {
            "name": name,
            "tags": {"host": host},
            "fields": {"usage_idle": 12.3},
            "timestamp": 1721664000,
        }
    ).encode()


@dataclass
class _FakeMqttMessage:
    topic: str
    payload: bytes


@dataclass
class _FakeMqtt:
    """Minimal ``homeassistant.components.mqtt``-shaped double for snoop tests."""

    subscribed_topics: Any = None

    def __post_init__(self) -> None:
        if self.subscribed_topics is None:
            self.subscribed_topics = []

    async def async_subscribe(self, hass: Any, topic: str, callback: Any) -> Callable[[], None]:
        self.subscribed_topics.append((topic, callback))

        def _unsub() -> None:
            self.subscribed_topics = [entry for entry in self.subscribed_topics if entry[1] is not callback]

        return _unsub


async def _drain(callback: Any, topic: str, payload: bytes) -> None:
    await callback(_FakeMqttMessage(topic=topic, payload=payload))


def test_snoop_listener_records_hosts_and_topics() -> None:
    from custom_components.telegraf_mqtt.snoop import SnoopListener

    class _Clock:
        def __init__(self) -> None:
            self.now = 0.0

        def __call__(self) -> float:
            return self.now

    clock = _Clock()
    listener = SnoopListener(probe_topic="telegraf/#", timeout_seconds=1.0, clock=clock)
    mqtt = _FakeMqtt()
    asyncio.run(listener.start(hass=None, subscribe=mqtt.async_subscribe))
    asyncio.run(_drain(listener._on_message, "telegraf/cpu", _payload("cpu", "h1")))
    asyncio.run(_drain(listener._on_message, "telegraf/mem", _payload("mem", "h2")))
    clock.now = 0.5
    result = listener.stop()
    assert result.hosts == frozenset({"h1", "h2"})
    assert result.topics == frozenset({"telegraf/cpu", "telegraf/mem"})
    assert result.duration_seconds == 0.5
    assert listener.is_finished is True
    assert mqtt.subscribed_topics == []


def test_snoop_listener_handles_malformed_payload() -> None:
    from custom_components.telegraf_mqtt.snoop import SnoopListener

    listener = SnoopListener(probe_topic="telegraf/#", timeout_seconds=0.0)
    mqtt = _FakeMqtt()
    asyncio.run(listener.start(hass=None, subscribe=mqtt.async_subscribe))
    asyncio.run(_drain(listener._on_message, "telegraf/cpu", b"not json"))
    result = listener.stop()
    assert result.hosts == frozenset()
    assert result.topics == frozenset({"telegraf/cpu"})


def test_snoop_listener_ignores_payload_without_host_tag() -> None:
    import json

    from custom_components.telegraf_mqtt.snoop import SnoopListener

    listener = SnoopListener(probe_topic="telegraf/#", timeout_seconds=0.0)
    mqtt = _FakeMqtt()
    asyncio.run(listener.start(hass=None, subscribe=mqtt.async_subscribe))
    payload = json.dumps({"name": "cpu", "fields": {"x": 1}}).encode()
    asyncio.run(_drain(listener._on_message, "telegraf/cpu", payload))
    result = listener.stop()
    assert result.hosts == frozenset()
    assert result.topics == frozenset({"telegraf/cpu"})


def test_snoop_listener_extracts_host_from_string_payload() -> None:
    from custom_components.telegraf_mqtt.snoop import _extract_host

    assert _extract_host(b'{"tags":{"host":"alpha"}}') == "alpha"
    assert _extract_host('{"tags":{"host":"beta"}}') == "beta"
    assert _extract_host(b"not json") == ""
    assert _extract_host(b'{"host": "with space"}') == "with space"
    assert _extract_host(b'{"host"  :   "spaced"}') == "spaced"
    assert _extract_host(b'{"host"}') == ""
    # Unclosed-quote: the scan walks to end-of-string and returns the tail.
    assert _extract_host(b'{"host": "unclosed') == "unclosed"
    # Non-string or absent value: no opening quote after the colon -> empty.
    assert _extract_host(b'{"host": 42}') == ""
    assert _extract_host(b'{"host":') == ""


def test_snoop_listener_deduplicates_hosts() -> None:
    from custom_components.telegraf_mqtt.snoop import SnoopListener

    listener = SnoopListener(probe_topic="telegraf/#", timeout_seconds=0.0)
    mqtt = _FakeMqtt()
    asyncio.run(listener.start(hass=None, subscribe=mqtt.async_subscribe))
    for _ in range(3):
        asyncio.run(_drain(listener._on_message, "telegraf/cpu", _payload("cpu", "h1")))
    result = listener.stop()
    assert result.hosts == frozenset({"h1"})


def test_snoop_listener_duration_is_zero_when_stopped_immediately() -> None:
    from custom_components.telegraf_mqtt.snoop import SnoopListener

    listener = SnoopListener(probe_topic="telegraf/#", timeout_seconds=0.0)
    result = listener.stop()
    assert result.duration_seconds == 0.0


def test_snoop_listener_auto_stops_when_timeout_expires() -> None:
    """The timer fires ``stop()`` so the diagnostics probe path gets a
    one-shot snapshot without the caller having to call ``stop()``
    explicitly. ``loop.call_later`` is on the real event loop, so a
    short timeout + a real ``asyncio.sleep`` is enough to drive the
    callback. The ``SnoopResult`` returned by the explicit ``stop()``
    call (made *after* the timer fires) reflects the captured traffic.
    """
    import asyncio

    from custom_components.telegraf_mqtt.snoop import SnoopListener

    class _Hass:
        pass

    mqtt = _FakeMqtt()

    async def _run() -> None:
        listener = SnoopListener(probe_topic="telegraf/#", timeout_seconds=0.05)
        hass = _Hass()
        hass.loop = asyncio.get_running_loop()
        await listener.start(hass, mqtt.async_subscribe)
        await _drain(listener._on_message, "telegraf/cpu", _payload("cpu", "h1"))
        # Wait past the configured timeout so ``_on_timeout`` fires.
        await asyncio.sleep(0.08)
        assert listener.is_finished is True
        # ``stop()`` after a timer fire is idempotent and still returns
        # the captured snapshot.
        result = listener.stop()
        assert result.hosts == frozenset({"h1"})
        assert result.topics == frozenset({"telegraf/cpu"})

    asyncio.run(_run())
    # The broker-side unsubscribe ran exactly once -- either by the
    # timer callback or by the explicit stop, never twice.
    assert mqtt.subscribed_topics == []


def test_snoop_listener_explicit_stop_cancels_pending_timer() -> None:
    """``stop()`` must cancel the scheduled timer so it cannot fire
    after the caller has already torn the listener down. We assert
    this by stopping the listener *before* the timer window elapses
    and checking the cancel handle was invoked (captured below)."""
    from custom_components.telegraf_mqtt.snoop import SnoopListener

    cancelled: list[bool] = []

    class _Loop:
        def __init__(self) -> None:
            self.call_later_called = 0

        def call_later(self, _delay: float, callback: Callable[[], None]) -> Callable[[], None]:
            self.call_later_called += 1

            def _cancel() -> None:
                cancelled.append(True)

            return _cancel

    class _Hass:
        loop = _Loop()

    listener = SnoopListener(probe_topic="telegraf/#", timeout_seconds=10.0)
    mqtt = _FakeMqtt()

    async def _run() -> None:
        await listener.start(_Hass(), mqtt.async_subscribe)

    asyncio.run(_run())
    # Explicit stop cancels the timer.
    listener.stop()
    assert cancelled == [True]


def test_extract_host_handles_bytes_decode_error() -> None:
    from custom_components.telegraf_mqtt.snoop import _extract_host

    result = _extract_host(b"\x80\x81\x82\x83")
    assert result == ""


def test_extract_host_handles_object_without_topic() -> None:
    from custom_components.telegraf_mqtt.snoop import _extract_host

    # Object is not a string/bytes/bytearray: returns empty string without raising.
    assert _extract_host(object()) == ""  # type: ignore[arg-type]


def test_extract_host_handles_bytearray_payload() -> None:
    from custom_components.telegraf_mqtt.snoop import _extract_host

    result = _extract_host(bytearray(b'{"host":"x"}'))
    assert result == "x"


def test_extract_host_handles_bytes_with_escape() -> None:
    """Escape handling is best-effort; the snoop is a hint, not a parser.

    The snoop is read-only telemetry -- a host tag with an embedded
    quote is rare in real Telegraf output (hostnames don't contain
    ``"``). The implementation walks until the next ``"`` without
    JSON-escape awareness. This test pins the current behaviour so a
    future change is intentional.
    """
    from custom_components.telegraf_mqtt.snoop import _extract_host

    # ``a\\"b`` in the source is the 3-char payload ``a\`` then ``"`` then ``b``.
    # The scan stops at the ``"``, so the returned value is just the
    # leading backslash. This is *not* the right host tag for the
    # payload, but the snoop is best-effort and a real Telegraf host
    # never contains a backslash followed by a quote.
    result = _extract_host(b'{"host":"a\\"b"}')
    assert result == "a\\"


def test_extract_host_anchors_on_key_not_substring() -> None:
    """Trip wire for the audit's ``_extract_host`` substring bug.

    The previous hand-rolled scanner searched for the bare string
    ``"host"`` and took the first match -- which is wrong on any
    payload where ``"host"`` appears earlier as a substring. The
    fixed implementation anchors on the JSON *key* position
    (``"host":``), so a sibling tag named ``"hostname"`` (whose
    value would otherwise be returned instead of the real host tag)
    or a string field whose value happens to contain the literal
    ``"host":`` sequence does not get returned.

    Each case below would return the *wrong* value under the old
    scanner. They all return the right value under the new one.

    Scope note: a payload where a *nested object* carries a
    ``"host"`` key before the real one, or where the first
    ``"host"`` key is followed by a non-string value while a later
    one is the real string host tag, is intentionally not asserted
    here. The audit's own suggested anchor (``^{.*"host"\\s*:``) is
    no stronger than the regex below on those adversarial shapes,
    and Telegraf's actual wire format never produces them. The
    substring cases (1, 2 below) are the realistic risks this test
    pins.
    """
    from custom_components.telegraf_mqtt.snoop import _extract_host

    # 1. The audit's flagship failure mode: a sibling tag whose name
    # starts with ``"host"`` (``"hostname"``) precedes the real
    # ``"host"`` tag. The old scanner would return ``"x"`` (the
    # value of ``hostname``); the anchored scanner skips over the
    # substring and finds the real key.
    assert _extract_host(b'{"tags":{"hostname":"x","host":"real"}}') == "real"

    # 2. A string value containing the literal ``"host"`` (without the
    # trailing ``":`` that would make it a key) must not be treated
    # as the key position. The old scanner would land inside the
    # string and walk into the value. The anchored scanner requires
    # the substring to be followed by ``:`` after the closing quote,
    # which the string value does not provide.
    assert _extract_host(b'{"path":"/some/host/file","host":"real"}}') == "real"

    # 3. The actual Telegraf payload shape must still work -- the
    # anchored search is a strict improvement, not a regression.
    assert _extract_host(b'{"name":"cpu","tags":{"host":"alpha","dc":"east"},"fields":{}}') == "alpha"

    # 4. The no-host-tag case still returns ``""``: when the only
    # ``"host"``-prefixed token is a sibling tag, the anchored
    # search finds nothing.
    assert _extract_host(b'{"tags":{"hostname":"only"}}') == ""


# ---------------------------------------------------------------------------
# Setup-level Phase 10 arms: broker-wait precheck, snoop failure tolerance,
# and the scheduled snoop stop timer.
# ---------------------------------------------------------------------------


class _FakePlatform:
    SENSOR = "sensor"
    BINARY_SENSOR = "binary_sensor"


class _FakeConfigEntries:
    def __init__(self) -> None:
        self.forwarded: list[tuple[Any, list[str]]] = []

    async def async_forward_entry_setups(self, entry: Any, platforms: list[str]) -> None:
        self.forwarded.append((entry, platforms))

    async def async_unload_platforms(self, entry: Any, platforms: list[str]) -> bool:
        # The snoop-unload tests care only that the call returns
        # truthy so ``async_unload_entry`` proceeds to the teardown
        # branches that release the runtime handles.
        return True


class _FakeSetupHass:
    """Minimal hass double for ``async_setup_entry``."""

    def __init__(self) -> None:
        self.config_entries = _FakeConfigEntries()
        self.data: dict = {}


class _FakeSetupEntry:
    """Minimal config-entry double for ``async_setup_entry``."""

    def __init__(self) -> None:
        self.entry_id = "entry-1"
        self.data = {CONF_TOPIC_PATTERN: "telegraf/#"}
        # Tests in this module were written against the previous
        # default of ``auto_discover=True`` (the post-setup snoop
        # running by default). The production default is now ``False``
        # so the snoop no longer silently widens past the user's
        # scope; tests that exercise snoop behaviour opt in here so
        # they keep their original coverage of the snoop code path.
        self.options: dict = {CONF_AUTO_DISCOVER: True}
        self.title = "Telegraf"
        self.runtime_data: Any = None
        self._unload_callbacks: list[Callable[[], None]] = []

    def async_on_unload(self, callback: Callable[[], None]) -> None:
        self._unload_callbacks.append(callback)

    def add_update_listener(self, _listener: Callable[..., Any]) -> Callable[[], None]:
        return lambda: None


class _SetupFakeMqtt:
    """MQTT double exposing the Phase 10 ``async_wait_for_mqtt_client`` precheck."""

    def __init__(self, wait_error: Exception | None = None) -> None:
        self.wait_error = wait_error
        self.subscribe_calls: list[tuple[str, Callable[..., Any]]] = []
        self.unsubscribe_calls = 0

    async def async_wait_for_mqtt_client(self, _hass: Any) -> None:
        if self.wait_error is not None:
            raise self.wait_error

    async def async_subscribe(self, _hass: Any, topic: str, cb: Callable[..., Any]) -> Callable[[], None]:
        self.subscribe_calls.append((topic, cb))
        return self._unsubscribe

    def _unsubscribe(self) -> None:
        self.unsubscribe_calls += 1


def _patch_setup(monkeypatch: pytest.MonkeyPatch, fake_mqtt: _SetupFakeMqtt) -> None:
    """Install the standard setup doubles (mirrors test_phase8_silver._patch)."""
    monkeypatch.setattr(integration, "Platform", _FakePlatform)
    monkeypatch.setattr(integration, "PLATFORMS", [_FakePlatform.SENSOR, _FakePlatform.BINARY_SENSOR])
    monkeypatch.setattr(integration, "mqtt", fake_mqtt, raising=False)
    monkeypatch.setattr(integration, "async_dispatcher_send", lambda *_a: None)
    monkeypatch.setattr(integration, "async_dispatcher_connect", lambda *_a: lambda: None)
    monkeypatch.setattr(integration, "async_track_time_interval", lambda *_a: lambda: None)
    monkeypatch.setattr(integration, "ir", None)


def test_setup_wait_for_mqtt_client_failure_raises_not_ready(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed ``async_wait_for_mqtt_client`` precheck raises
    ``ConfigEntryNotReady`` (so HA retries) and never attempts the real
    subscription."""
    from homeassistant.exceptions import ConfigEntryNotReady

    fake_mqtt = _SetupFakeMqtt(wait_error=RuntimeError("broker not connected"))
    _patch_setup(monkeypatch, fake_mqtt)
    hass = _FakeSetupHass()
    entry = _FakeSetupEntry()
    captured: dict = {}

    async def _run() -> None:
        try:
            await integration.async_setup_entry(hass, entry)  # type: ignore[arg-type]
        except ConfigEntryNotReady as exc:
            captured["exc"] = exc

    asyncio.run(_run())
    assert "exc" in captured
    assert getattr(captured["exc"], "translation_key", None) == "mqtt_broker_unreachable"
    # The precheck short-circuits -- no subscription was attempted.
    assert fake_mqtt.subscribe_calls == []


def test_setup_snoop_failure_is_non_fatal(monkeypatch: pytest.MonkeyPatch) -> None:
    """A failing snoop must not fail setup: the snoop is stopped, the real
    subscription stays, and a message through it still lands in the parser."""
    fake_mqtt = _SetupFakeMqtt()
    _patch_setup(monkeypatch, fake_mqtt)

    class _BrokenSnoop:
        def __init__(self) -> None:
            self.stopped = False

        async def start(self, hass: Any, subscribe: Any) -> None:
            raise RuntimeError("snoop exploded")

        def stop(self) -> Any:
            self.stopped = True
            return None

    broken = _BrokenSnoop()

    def _factory(*_args: Any, **_kwargs: Any) -> Any:
        return broken

    monkeypatch.setattr(integration, "SnoopListener", _factory)

    hass = _FakeSetupHass()
    entry = _FakeSetupEntry()

    async def _run() -> None:
        await integration.async_setup_entry(hass, entry)  # type: ignore[arg-type]

    asyncio.run(_run())
    assert broken.stopped is True


def test_setup_entry_with_default_options_does_not_install_snoop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The post-setup snoop is off by default; setup must not install it.

    Topic discovery only happens during the config flow. The user
    opts in to the snoop via the options flow. This is the
    integration-level pin on the security default -- a refactor that
    flips ``DEFAULT_AUTO_DISCOVER`` back to ``True`` is caught here
    because the snoop subscription never appears on the broker.
    """
    fake_mqtt = _SetupFakeMqtt()
    _patch_setup(monkeypatch, fake_mqtt)

    hass = _FakeSetupHass()
    # Default options: no ``auto_discover`` opt-in. ``_FakeSetupEntry``
    # would otherwise set it to True to preserve the snoop tests'
    # original coverage; we override here to test the no-opt-in path.
    entry = _FakeSetupEntry()
    entry.options = {}

    async def _run() -> None:
        await integration.async_setup_entry(hass, entry)  # type: ignore[arg-type]

    asyncio.run(_run())

    # Exactly one subscription: the real one on the user's
    # topic_pattern. The snoop is not installed because the user
    # has not opted in.
    assert len(fake_mqtt.subscribe_calls) == 1
    assert fake_mqtt.subscribe_calls[0][0] == "telegraf/#"
    # No snoop teardown handle -- the runtime never parked one.
    assert entry.runtime_data.unsubscribe_snoop is None


def test_options_flow_enables_snoop_on_reload(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The opt-in path still wires the snoop.

    The user toggles ``auto_discover`` to True in the options flow;
    the update listener reloads the entry. After reload, the snoop
    subscription is installed. This is the complement of
    ``test_setup_entry_with_default_options_does_not_install_snoop``:
    the no-opt-in path stays off, the opt-in path turns on.
    """
    fake_mqtt = _SetupFakeMqtt()
    _patch_setup(monkeypatch, fake_mqtt)

    hass = _FakeSetupHass()
    entry = _FakeSetupEntry()
    # Start with the no-opt-in default.
    entry.options = {}

    async def _run() -> None:
        # First cycle: no opt-in -> no snoop.
        await integration.async_setup_entry(hass, entry)  # type: ignore[arg-type]
        # The user toggles ``auto_discover`` to True via the options flow.
        # The update listener then reloads the entry.
        entry.options = {CONF_AUTO_DISCOVER: True}
        await integration.async_unload_entry(hass, entry)  # type: ignore[arg-type]
        await integration.async_setup_entry(hass, entry)  # type: ignore[arg-type]

    asyncio.run(_run())

    # Cycle 1 (no opt-in): 1 subscription, the real one. Cycle 2
    # (opt-in): 1 more real + 1 snoop. The snoop is the last
    # subscription on the broker; both cycle-2 subscriptions match
    # the user's topic_pattern because the probe is derived from it.
    assert len(fake_mqtt.subscribe_calls) == 3
    assert fake_mqtt.subscribe_calls[0][0] == "telegraf/#"  # cycle 1 real
    assert fake_mqtt.subscribe_calls[1][0] == "telegraf/#"  # cycle 2 real
    assert fake_mqtt.subscribe_calls[2][0] == "telegraf/#"  # cycle 2 snoop
    # The runtime parked a snoop teardown handle on the second cycle.
    assert entry.runtime_data.unsubscribe_snoop is not None


def test_setup_entry_rack1_topic_runs_snoop_on_rack1_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The snoop's probe topic is the user's ``topic_pattern``, not
    the global ``telegraf/#`` fallback.

    Production-wiring end-to-end: a user who configures
    ``telegraf/rack1/#`` and opts in to ``auto_discover`` must have
    the snoop subscribe on ``telegraf/rack1/#`` only. MQTT broker
    filtering is what enforces the scope -- the snoop itself
    doesn't filter messages -- so a wiring bug here would let the
    snoop see rack2 traffic on a shared broker and auto-create
    devices the user never opted into. This is the production
    integration of the rack1/rack2 isolation invariant; the
    unit-level version lives in
    ``test_discover_topics.py::test_flow_start_scan_uses_user_supplied_probe_root``.
    """
    fake_mqtt = _SetupFakeMqtt()
    _patch_setup(monkeypatch, fake_mqtt)

    hass = _FakeSetupHass()
    entry = _FakeSetupEntry()
    # The user is on rack1 only -- the snoop must respect that scope.
    entry.data = {CONF_TOPIC_PATTERN: "telegraf/rack1/#"}
    entry.options = {CONF_AUTO_DISCOVER: True}

    async def _run() -> None:
        await integration.async_setup_entry(hass, entry)  # type: ignore[arg-type]

    asyncio.run(_run())

    # Real subscription is on the user's pattern. The snoop is the
    # second subscription and is bound to the SAME pattern -- the
    # broker is what filters, not the snoop, so the binding has to
    # match. A refactor that hardcodes ``telegraf/#`` (or a wider
    # default) in __init__.py is caught here.
    assert len(fake_mqtt.subscribe_calls) == 2
    assert fake_mqtt.subscribe_calls[0][0] == "telegraf/rack1/#"  # real
    assert fake_mqtt.subscribe_calls[1][0] == "telegraf/rack1/#"  # snoop

    # The snoop is wired with a dispatcher -- the rack1 isolation
    # is enforced at the broker level, not by the snoop code. If
    # the broker ever delivered a rack2 message, it would land in
    # the manager. We don't test that path (the broker doesn't
    # deliver out-of-pattern messages in production); we test
    # the binding.
    snoop_cb = fake_mqtt.subscribe_calls[1][1]
    snoop = snoop_cb.__self__
    assert snoop._dispatcher is not None


def test_setup_snoop_is_long_lived_and_stored_on_runtime_data(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The snoop is long-lived (no timer); ``runtime_data.unsubscribe_snoop``
    carries the teardown handle so ``async_unload_entry`` can release it."""
    fake_mqtt = _SetupFakeMqtt()
    _patch_setup(monkeypatch, fake_mqtt)

    hass = _FakeSetupHass()
    hass.loop = object()  # any sentinel; the new code no longer inspects it
    entry = _FakeSetupEntry()

    async def _run() -> None:
        await integration.async_setup_entry(hass, entry)  # type: ignore[arg-type]

    asyncio.run(_run())
    # Real subscription (call 0) + snoop (call 1).
    assert len(fake_mqtt.subscribe_calls) == 2
    assert fake_mqtt.subscribe_calls[0][0] == "telegraf/#"
    snoop = fake_mqtt.subscribe_calls[1][1].__self__
    assert snoop.is_finished is False
    # The snoop's teardown handle is parked on the runtime data so the
    # unload path can release it. ``snoop.stop`` is a bound method; the
    # ``__func__`` comparison is identity-stable across attribute lookups.
    assert entry.runtime_data.unsubscribe_snoop.__func__ is snoop.stop.__func__  # type: ignore[attr-defined]
    # Traffic through the snoop before unload is recorded.
    snoop_cb = fake_mqtt.subscribe_calls[1][1]
    asyncio.run(snoop_cb(_FakeMqttMessage(topic="telegraf/h1/mem", payload=_payload("mem", "h1"))))
    assert fake_mqtt.unsubscribe_calls == 0
    # Unload fires the teardown: unsubscribes the snoop and finishes it.
    assert asyncio.run(integration.async_unload_entry(hass, entry)) is True
    assert snoop.is_finished is True
    # Two unsubscribes total -- one for the real sub, one for the snoop.
    assert fake_mqtt.unsubscribe_calls == 2
    # After unload the handle is wiped so a second unload is a no-op.
    assert entry.runtime_data.unsubscribe_snoop is None


def test_snoop_dispatcher_creates_devices_and_metrics(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """When the snoop's ``dispatcher`` is wired to ``manager.process_message``,
    a captured message becomes a real device and metric in the registry.
    This is the user-facing "auto-add entities" behaviour: a new Telegraf
    host appearing under ``telegraf/#`` becomes a real device the user did
    not have to add by hand."""
    fake_mqtt = _SetupFakeMqtt()
    _patch_setup(monkeypatch, fake_mqtt)

    hass = _FakeSetupHass()
    hass.loop = object()
    entry = _FakeSetupEntry()

    async def _run() -> None:
        await integration.async_setup_entry(hass, entry)  # type: ignore[arg-type]

    asyncio.run(_run())
    # The snoop is the second subscription.
    snoop_cb = fake_mqtt.subscribe_calls[1][1]
    snoop = snoop_cb.__self__
    # Sanity: the snoop was constructed with a dispatcher.
    assert snoop._dispatcher is not None
    # A message on a topic the *user's* primary pattern (``telegraf/#``)
    # would also match, but a unique host lands the device in the
    # registry through the dispatcher.
    asyncio.run(
        snoop_cb(
            _FakeMqttMessage(
                topic="telegraf/snoop-host/cpu",
                payload=_payload("cpu", "snoop-host"),
            )
        )
    )
    # Dispatched count incremented exactly once.
    assert snoop.dispatched_count == 1
    # The manager now has a device + metric for the new host. The
    # device id is the ``host`` tag slugified with a collision
    # suffix, so we look it up via the registry's view of the
    # device's device_name (which preserves the raw ``host``).
    manager = entry.runtime_data.manager
    assert len(manager.devices) == 1
    device_id, registry = next(iter(manager.devices.items()))
    assert registry.device_name == "snoop-host"
    assert manager.get_metric(f"{device_id}:cpu_usage_idle") is not None
    # Tidy up: unload tears the snoop down.
    asyncio.run(integration.async_unload_entry(hass, entry))


def test_snoop_record_only_does_not_create_devices(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A snoop with no ``dispatcher`` records hosts/topics but does not
    touch the registry -- the previous contract is preserved when callers
    explicitly opt out of auto-dispatch (e.g. the diagnostics probe)."""
    from custom_components.telegraf_mqtt.snoop import SnoopListener

    captured: list[tuple[str, Any]] = []

    async def _fake_subscribe(_hass: Any, topic: str, cb: Any) -> Callable[[], None]:
        return lambda: None

    async def _run() -> None:
        snoop = SnoopListener(
            probe_topic="telegraf/#",
            timeout_seconds=0.0,
        )
        await snoop.start(object(), _fake_subscribe)

        class _Msg:
            def __init__(self, topic: str, payload: Any) -> None:
                self.topic = topic
                self.payload = payload

        await snoop._on_message(_Msg("telegraf/host-only/cpu", _payload("cpu", "host-only")))
        captured.append(("hosts", snoop._seen_hosts))
        captured.append(("dispatched", snoop.dispatched_count))
        snoop.stop()

    asyncio.run(_run())
    assert captured[0][1] == frozenset({"host-only"})
    assert captured[1][1] == 0


def test_snoop_dispatcher_errors_are_counted_not_swallowed_silently() -> None:
    """A dispatcher that raises on some messages must not stop the snoop.

    The snoop listener already catches any exception raised by the
    dispatcher so a bad payload can't break the broker-side
    subscription. The Phase 10 follow-on is to bump a counter so the
    user can see that the dispatcher is sick -- the alternative would
    be an integration that's silently broken on the auto-discover
    path, indistinguishable from one that's working.
    """
    import json

    from custom_components.telegraf_mqtt.snoop import SnoopListener

    good_calls: list[tuple[str, Any]] = []
    boom_calls: list[str] = []

    def _flaky_dispatcher(topic: str, payload: Any) -> None:
        # Every other call raises; the rest succeed.
        if topic.endswith("/boom"):
            boom_calls.append(topic)
            raise RuntimeError("downstream is on fire")
        good_calls.append((topic, payload))

    async def _fake_subscribe(_hass: Any, _topic: str, _cb: Any) -> Callable[[], None]:
        return lambda: None

    async def _run() -> None:
        snoop = SnoopListener(
            probe_topic="telegraf/#",
            timeout_seconds=0.0,
            dispatcher=_flaky_dispatcher,
        )
        await snoop.start(object(), _fake_subscribe)

        class _Msg:
            def __init__(self, topic: str, payload: Any) -> None:
                self.topic = topic
                self.payload = payload

        # 3 OK + 2 boom. Order matters: alternation tests that one
        # failure does not break the next message.
        for i in range(5):
            t = f"telegraf/h{i}/cpu" if i % 2 == 0 else f"telegraf/h{i}/boom"
            body = json.dumps({"name": "cpu", "tags": {"host": f"h{i}"}, "fields": {"x": 1}, "timestamp": 1}).encode()
            await snoop._on_message(_Msg(t, body))
        return snoop.stop()

    result = asyncio.run(_run())
    assert len(good_calls) == 3
    assert len(boom_calls) == 2
    assert result.dispatched_count == 3
    assert result.dispatcher_errors == 2


# ---------------------------------------------------------------------------
# Platform routing on descriptor.platform_hint (Phase 10)
# ---------------------------------------------------------------------------


def _runtime_data_for(manager: Any) -> Any:
    from custom_components.telegraf_mqtt import TelegrafMqttRuntimeData
    from custom_components.telegraf_mqtt.parser import TelegrafParser

    parser = TelegrafParser()
    return TelegrafMqttRuntimeData(
        manager=manager,
        parser=parser,
        parser_stats=parser.stats,
        manufacturer="Acme",
        model="PC-1",
        sw_version=None,
    )


def _platform_entities(monkeypatch: pytest.MonkeyPatch, platform_module: Any, manager: Any) -> list[Any]:
    """Drive a platform's ``async_setup_entry`` and return the entities it added."""
    monkeypatch.setattr(platform_module, "async_dispatcher_connect", lambda *a, **kw: lambda: None)
    hass = _FakeSetupHass()
    entry = _FakeSetupEntry()
    entry.runtime_data = _runtime_data_for(manager)
    entities: list[Any] = []
    asyncio.run(platform_module.async_setup_entry(hass, entry, entities.extend))
    return entities


def _registry_for(manager: Any) -> Any:
    registry = manager.get_or_create_registry("h1", "h1")
    return registry


def test_sensor_platform_skips_bool_value_with_auto_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A bool metric with the default ``auto`` hint belongs on the
    binary_sensor platform; the sensor platform must not duplicate it."""
    from custom_components.telegraf_mqtt import sensor as sensor_platform

    manager = _manager()
    _registry_for(manager).update(_descriptor("flag", True))

    assert _platform_entities(monkeypatch, sensor_platform, manager) == []


def test_sensor_platform_admits_bool_value_with_sensor_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An explicit ``platform_hint="sensor"`` field override pulls a bool
    field onto the sensor platform."""
    from custom_components.telegraf_mqtt import sensor as sensor_platform

    manager = _manager()
    _registry_for(manager).update(replace(_descriptor("flag", True), platform_hint=PLATFORM_HINT_SENSOR))

    entities = _platform_entities(monkeypatch, sensor_platform, manager)
    assert len(entities) == 1
    assert entities[0]._attr_unique_id == "telegraf_mqtt_h1_cpu_flag"
    assert entities[0].native_value is True


def test_binary_sensor_platform_excludes_sensor_hinted_bool(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A field the user forced onto the sensor platform must not also be
    created by the binary_sensor platform."""
    from custom_components.telegraf_mqtt import binary_sensor as binary_platform

    manager = _manager()
    _registry_for(manager).update(replace(_descriptor("flag", True), platform_hint=PLATFORM_HINT_SENSOR))

    assert _platform_entities(monkeypatch, binary_platform, manager) == []


def test_binary_sensor_platform_admits_registry_coerced_hint(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A numeric field with a ``binary_sensor`` override is coerced to a
    bool by the registry and lands only on the binary_sensor platform."""
    from custom_components.telegraf_mqtt import binary_sensor as binary_platform
    from custom_components.telegraf_mqtt import sensor as sensor_platform

    manager = _manager(field_overrides={"flag": {"platform": PLATFORM_HINT_BINARY_SENSOR}})
    assert _registry_for(manager).update(_descriptor("flag", 1)) is True

    binary_entities = _platform_entities(monkeypatch, binary_platform, manager)
    sensor_entities = _platform_entities(monkeypatch, sensor_platform, manager)
    assert len(binary_entities) == 1
    assert binary_entities[0].is_on is True
    assert sensor_entities == []


def test_none_hint_field_never_reaches_either_platform(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A ``none`` hint drops the field at the registry -- neither platform
    ever sees it."""
    from custom_components.telegraf_mqtt import binary_sensor as binary_platform
    from custom_components.telegraf_mqtt import sensor as sensor_platform

    manager = _manager(field_overrides={"x": {"platform": PLATFORM_HINT_NONE}})
    assert _registry_for(manager).update(_descriptor("x", 1.0)) is False

    assert _platform_entities(monkeypatch, sensor_platform, manager) == []
    assert _platform_entities(monkeypatch, binary_platform, manager) == []


# ---------------------------------------------------------------------------
# DeviceManager.find_device_id_collisions
# ---------------------------------------------------------------------------


def test_find_device_id_collisions_empty_when_only_one_host_per_device() -> None:
    """One host -> one device_id: no collisions."""
    manager = _manager()
    manager.process_message(
        "telegraf/host-a/cpu",
        b'{"name":"cpu","tags":{"host":"host-a"},"fields":{"usage_idle":12.3},"timestamp":1721664000}',
    )
    assert manager.find_device_id_collisions() == {}


def test_find_device_id_collisions_detects_two_hosts_same_slug() -> None:
    """Two distinct host tags that slugify to the same device_id form a
    collision. Common case: two Telegraf containers both using
    ``host=localhost``."""
    manager = _manager()
    # First host: 'localhost' -> 'localhost'
    manager.process_message(
        "telegraf/localhost/cpu",
        b'{"name":"cpu","tags":{"host":"localhost"},"fields":{"usage_idle":1.0},"timestamp":1}',
    )
    # Second host: 'LOCALHOST' slugs to the same 'localhost'
    manager.process_message(
        "telegraf/LOCALHOST/cpu",
        b'{"name":"cpu","tags":{"host":"LOCALHOST"},"fields":{"usage_idle":2.0},"timestamp":2}',
    )
    collisions = manager.find_device_id_collisions()
    assert "localhost" in collisions
    assert sorted(collisions["localhost"]) == ["LOCALHOST", "localhost"]


# ---------------------------------------------------------------------------
# Repairs: check_no_traffic, check_device_id_collision, check_device_id_conflict
# ---------------------------------------------------------------------------


@dataclass
class _FakeIrCall:
    domain: str
    issue_id: str
    kwargs: dict = field(default_factory=dict)


@dataclass
class _FakeIr:
    """IssueRegistry stub that records create/delete calls."""

    IssueSeverity: type = field(default_factory=lambda: types.SimpleNamespace(WARNING="warning"))
    created: list[_FakeIrCall] = field(default_factory=list)
    deleted: list[_FakeIrCall] = field(default_factory=list)

    def async_create_issue(self, hass, domain, issue_id, **kwargs):
        self.created.append(_FakeIrCall(domain=domain, issue_id=issue_id, kwargs=kwargs))
        return "ignored"

    def async_delete_issue(self, hass, domain, issue_id):
        self.deleted.append(_FakeIrCall(domain=domain, issue_id=issue_id))


@dataclass
class _FakeEntry:
    entry_id: str
    data: dict
    title: str = "Telegraf"
    runtime_data: Any = None


@dataclass
class _FakeConfigEntriesForRepairs:
    entries: list[_FakeEntry] = field(default_factory=list)

    def async_entries(self, domain):
        return [e for e in self.entries if getattr(e, "entry_id", None) is not None]


@dataclass
class _FakeHassForRepairs:
    config_entries: _FakeConfigEntriesForRepairs = field(default_factory=_FakeConfigEntriesForRepairs)


def _patch_ir(monkeypatch, fake_ir):
    monkeypatch.setattr("custom_components.telegraf_mqtt.ir", fake_ir)


def test_check_no_traffic_raises_when_no_messages(monkeypatch) -> None:
    """If the manager has not received any messages by the time the
    Repairs check runs, the issue is raised with a preview of the
    configured topic pattern."""
    from custom_components.telegraf_mqtt.repairs import check_no_traffic

    fake_ir = _FakeIr()
    _patch_ir(monkeypatch, fake_ir)
    manager = _manager()
    entry = _FakeEntry(
        entry_id="A",
        data={CONF_TOPIC_PATTERN: "telegraf/host-a/#"},
        runtime_data=_runtime_data_for(manager),
    )
    hass = _FakeHassForRepairs()

    check_no_traffic(hass, entry)
    # Exactly one create call for no_traffic_on_topic.
    assert len(fake_ir.created) == 1
    call = fake_ir.created[0]
    assert call.issue_id == "no_traffic_on_topic_A"
    assert call.kwargs["translation_key"] == "no_traffic_on_topic"
    placeholders = call.kwargs["translation_placeholders"]
    assert placeholders["configured_topic"] == "telegraf/host-a/#"
    assert placeholders["seen_topics"] == "(none)"


def test_check_no_traffic_auto_resolves_when_messages_arrive(monkeypatch) -> None:
    """Once messages have arrived, the next call deletes the issue."""
    from custom_components.telegraf_mqtt.repairs import check_no_traffic

    fake_ir = _FakeIr()
    _patch_ir(monkeypatch, fake_ir)
    manager = _manager()
    entry = _FakeEntry(
        entry_id="A",
        data={CONF_TOPIC_PATTERN: "telegraf/#"},
        runtime_data=_runtime_data_for(manager),
    )
    hass = _FakeHassForRepairs()

    check_no_traffic(hass, entry)
    assert len(fake_ir.created) == 1
    # A message arrives.
    manager.record_seen_host("host-a", "telegraf/host-a/cpu")
    fake_ir.created.clear()
    fake_ir.deleted.clear()
    check_no_traffic(hass, entry)
    assert fake_ir.created == []
    assert any(c.issue_id == "no_traffic_on_topic_A" for c in fake_ir.deleted)


def test_check_no_traffic_noop_when_ir_unavailable(monkeypatch) -> None:
    """If the issue registry is not importable, the check is a no-op."""
    from custom_components.telegraf_mqtt.repairs import check_no_traffic

    monkeypatch.setattr("custom_components.telegraf_mqtt.ir", None)
    manager = _manager()
    entry = _FakeEntry(
        entry_id="A",
        data={CONF_TOPIC_PATTERN: "telegraf/#"},
        runtime_data=_runtime_data_for(manager),
    )
    hass = _FakeHassForRepairs()
    # Must not raise.
    check_no_traffic(hass, entry)


def test_check_no_traffic_noop_when_runtime_data_missing(monkeypatch) -> None:
    """A pre-setup entry has no runtime_data; the check is a no-op."""
    from custom_components.telegraf_mqtt.repairs import check_no_traffic

    fake_ir = _FakeIr()
    _patch_ir(monkeypatch, fake_ir)
    entry = _FakeEntry(entry_id="A", data={CONF_TOPIC_PATTERN: "telegraf/#"}, runtime_data=None)
    hass = _FakeHassForRepairs()
    check_no_traffic(hass, entry)
    assert fake_ir.created == []


def test_check_device_id_collision_raises(monkeypatch) -> None:
    """Two distinct host tags collapsing onto one device_id slug raises
    a Repairs issue listing both hosts."""
    from custom_components.telegraf_mqtt.repairs import check_device_id_collision

    fake_ir = _FakeIr()
    _patch_ir(monkeypatch, fake_ir)
    manager = _manager()
    manager.process_message(
        "telegraf/localhost/cpu",
        b'{"name":"cpu","tags":{"host":"localhost"},"fields":{"usage_idle":1.0},"timestamp":1}',
    )
    manager.process_message(
        "telegraf/LOCALHOST/cpu",
        b'{"name":"cpu","tags":{"host":"LOCALHOST"},"fields":{"usage_idle":2.0},"timestamp":2}',
    )
    entry = _FakeEntry(
        entry_id="A",
        data={CONF_TOPIC_PATTERN: "telegraf/#"},
        runtime_data=_runtime_data_for(manager),
    )
    hass = _FakeHassForRepairs()

    check_device_id_collision(hass, entry)
    assert len(fake_ir.created) == 1
    call = fake_ir.created[0]
    assert call.issue_id == "device_id_collision_A"
    assert call.kwargs["translation_key"] == "device_id_collision"
    desc = call.kwargs["translation_placeholders"]["description"]
    assert "localhost" in desc
    assert "LOCALHOST" in desc


def test_check_device_id_collision_auto_resolves(monkeypatch) -> None:
    """A clean state deletes any prior collision issue."""
    from custom_components.telegraf_mqtt.repairs import check_device_id_collision

    fake_ir = _FakeIr()
    _patch_ir(monkeypatch, fake_ir)
    manager = _manager()  # no collisions
    entry = _FakeEntry(
        entry_id="A",
        data={CONF_TOPIC_PATTERN: "telegraf/#"},
        runtime_data=_runtime_data_for(manager),
    )
    hass = _FakeHassForRepairs()
    check_device_id_collision(hass, entry)
    assert fake_ir.created == []
    assert any(c.issue_id == "device_id_collision_A" for c in fake_ir.deleted)


def test_check_device_id_conflict_raises_on_cross_entry_overlap(
    monkeypatch,
) -> None:
    """Two entries producing the same device_id from different topic
    patterns raise a cross-entry conflict issue."""
    from custom_components.telegraf_mqtt.repairs import check_device_id_conflict

    fake_ir = _FakeIr()
    _patch_ir(monkeypatch, fake_ir)
    manager_a = _manager()
    manager_b = _manager()
    # Both managers produced the 'host-a' device.
    manager_a.process_message(
        "telegraf/host-a/cpu",
        b'{"name":"cpu","tags":{"host":"host-a"},"fields":{"usage_idle":1.0},"timestamp":1}',
    )
    manager_b.process_message(
        "other/host-a/cpu",
        b'{"name":"cpu","tags":{"host":"host-a"},"fields":{"usage_idle":1.0},"timestamp":1}',
    )
    entry_a = _FakeEntry(
        entry_id="A",
        data={CONF_TOPIC_PATTERN: "telegraf/#"},
        title="First",
        runtime_data=_runtime_data_for(manager_a),
    )
    entry_b = _FakeEntry(
        entry_id="B",
        data={CONF_TOPIC_PATTERN: "other/#"},
        title="Second",
        runtime_data=_runtime_data_for(manager_b),
    )
    hass = _FakeHassForRepairs(config_entries=_FakeConfigEntriesForRepairs(entries=[entry_a, entry_b]))

    check_device_id_conflict(hass, entry_a)
    assert any(
        c.issue_id == "device_id_conflict_A" and c.kwargs.get("translation_key") == "device_id_conflict"
        for c in fake_ir.created
    )


def test_check_device_id_conflict_auto_resolves(monkeypatch) -> None:
    """No overlap -> the prior conflict issue is deleted."""
    from custom_components.telegraf_mqtt.repairs import check_device_id_conflict

    fake_ir = _FakeIr()
    _patch_ir(monkeypatch, fake_ir)
    manager = _manager()  # no devices
    entry = _FakeEntry(
        entry_id="A",
        data={CONF_TOPIC_PATTERN: "telegraf/#"},
        runtime_data=_runtime_data_for(manager),
    )
    hass = _FakeHassForRepairs()
    check_device_id_conflict(hass, entry)
    assert fake_ir.created == []


def test_check_device_id_conflict_noop_when_ir_none(monkeypatch) -> None:
    from custom_components.telegraf_mqtt.repairs import check_device_id_conflict

    monkeypatch.setattr("custom_components.telegraf_mqtt.ir", None)
    manager = _manager()
    entry = _FakeEntry(
        entry_id="A",
        data={CONF_TOPIC_PATTERN: "telegraf/#"},
        runtime_data=_runtime_data_for(manager),
    )
    hass = _FakeHassForRepairs()
    # Must not raise.
    check_device_id_conflict(hass, entry)
    check_device_id_conflict(hass, entry)


# ---------------------------------------------------------------------------
# Defensive guards: missing manager / missing runtime_data / overlapping entries
# ---------------------------------------------------------------------------


def test_check_no_traffic_noop_when_manager_missing(monkeypatch) -> None:
    """``runtime_data`` is set but has no ``manager`` attribute -> no-op."""
    from custom_components.telegraf_mqtt.repairs import check_no_traffic

    fake_ir = _FakeIr()
    _patch_ir(monkeypatch, fake_ir)
    from custom_components.telegraf_mqtt import TelegrafMqttRuntimeData

    runtime = TelegrafMqttRuntimeData(
        manager=None,  # type: ignore[arg-type]
        parser=None,  # type: ignore[arg-type]
        parser_stats=None,
        manufacturer=None,
        model=None,
        sw_version=None,
    )
    entry = _FakeEntry(entry_id="A", data={CONF_TOPIC_PATTERN: "telegraf/#"}, runtime_data=runtime)
    hass = _FakeHassForRepairs()
    # Must not raise.
    check_no_traffic(hass, entry)
    assert fake_ir.created == []


def test_check_device_id_collision_noop_when_runtime_data_missing(
    monkeypatch,
) -> None:
    """Pre-setup entry has no ``runtime_data`` -> no-op."""
    from custom_components.telegraf_mqtt.repairs import check_device_id_collision

    fake_ir = _FakeIr()
    _patch_ir(monkeypatch, fake_ir)
    entry = _FakeEntry(entry_id="A", data={CONF_TOPIC_PATTERN: "telegraf/#"}, runtime_data=None)
    hass = _FakeHassForRepairs()
    check_device_id_collision(hass, entry)
    assert fake_ir.created == []


def test_check_device_id_collision_noop_when_manager_missing(monkeypatch) -> None:
    """``runtime_data.manager`` is None -> no-op."""
    from custom_components.telegraf_mqtt.repairs import check_device_id_collision

    fake_ir = _FakeIr()
    _patch_ir(monkeypatch, fake_ir)
    from custom_components.telegraf_mqtt import TelegrafMqttRuntimeData

    runtime = TelegrafMqttRuntimeData(
        manager=None,  # type: ignore[arg-type]
        parser=None,  # type: ignore[arg-type]
        parser_stats=None,
        manufacturer=None,
        model=None,
        sw_version=None,
    )
    entry = _FakeEntry(entry_id="A", data={CONF_TOPIC_PATTERN: "telegraf/#"}, runtime_data=runtime)
    hass = _FakeHassForRepairs()
    check_device_id_collision(hass, entry)
    assert fake_ir.created == []


def test_check_device_cap_raises_when_dropped(monkeypatch) -> None:
    """A ``dropped_device_count > 0`` raises a Repairs issue with the count."""
    from custom_components.telegraf_mqtt.repairs import check_device_cap

    fake_ir = _FakeIr()
    _patch_ir(monkeypatch, fake_ir)
    manager = _manager()
    manager.dropped_device_count = 5
    entry = _FakeEntry(
        entry_id="A",
        data={CONF_TOPIC_PATTERN: "telegraf/#"},
        runtime_data=_runtime_data_for(manager),
    )
    hass = _FakeHassForRepairs()

    check_device_cap(hass, entry)
    assert len(fake_ir.created) == 1
    call = fake_ir.created[0]
    assert call.issue_id == "device_cap_reached_A"
    assert call.kwargs["translation_key"] == "device_cap_reached"
    placeholders = call.kwargs["translation_placeholders"]
    assert placeholders["dropped"] == "5"
    assert placeholders["max_devices"] == "30"
    assert placeholders["configured_topic"] == "telegraf/#"


def test_check_device_cap_auto_resolves_when_clean(monkeypatch) -> None:
    """A ``dropped_device_count == 0`` deletes any prior cap issue."""
    from custom_components.telegraf_mqtt.repairs import check_device_cap

    fake_ir = _FakeIr()
    _patch_ir(monkeypatch, fake_ir)
    manager = _manager()
    manager.dropped_device_count = 0
    entry = _FakeEntry(
        entry_id="A",
        data={CONF_TOPIC_PATTERN: "telegraf/#"},
        runtime_data=_runtime_data_for(manager),
    )
    hass = _FakeHassForRepairs()

    check_device_cap(hass, entry)
    assert fake_ir.created == []
    assert any(c.issue_id == "device_cap_reached_A" for c in fake_ir.deleted)


def test_check_device_cap_noop_when_ir_unavailable(monkeypatch) -> None:
    """No issue registry -> no-op."""
    from custom_components.telegraf_mqtt.repairs import check_device_cap

    monkeypatch.setattr(
        "custom_components.telegraf_mqtt.repairs._ir",
        lambda _h: None,
    )
    manager = _manager()
    manager.dropped_device_count = 5
    entry = _FakeEntry(
        entry_id="A",
        data={CONF_TOPIC_PATTERN: "telegraf/#"},
        runtime_data=_runtime_data_for(manager),
    )
    hass = _FakeHassForRepairs()
    check_device_cap(hass, entry)  # should not raise


def test_check_metric_cap_raises_when_dropped(monkeypatch) -> None:
    """A ``dropped_metric_count > 0`` raises a Repairs issue with the count."""
    from custom_components.telegraf_mqtt.repairs import check_metric_cap

    fake_ir = _FakeIr()
    _patch_ir(monkeypatch, fake_ir)
    manager = _manager()
    manager.dropped_metric_count = 12
    entry = _FakeEntry(
        entry_id="B",
        data={CONF_TOPIC_PATTERN: "telegraf/rack1/#"},
        runtime_data=_runtime_data_for(manager),
    )
    hass = _FakeHassForRepairs()

    check_metric_cap(hass, entry)
    assert len(fake_ir.created) == 1
    call = fake_ir.created[0]
    assert call.issue_id == "metric_cap_reached_B"
    assert call.kwargs["translation_key"] == "metric_cap_reached"
    placeholders = call.kwargs["translation_placeholders"]
    assert placeholders["dropped"] == "12"
    assert placeholders["max_metrics_per_device"] == "50"
    assert placeholders["configured_topic"] == "telegraf/rack1/#"


def test_check_metric_cap_auto_resolves_when_clean(monkeypatch) -> None:
    """A ``dropped_metric_count == 0`` deletes any prior cap issue."""
    from custom_components.telegraf_mqtt.repairs import check_metric_cap

    fake_ir = _FakeIr()
    _patch_ir(monkeypatch, fake_ir)
    manager = _manager()
    manager.dropped_metric_count = 0
    entry = _FakeEntry(
        entry_id="B",
        data={CONF_TOPIC_PATTERN: "telegraf/rack1/#"},
        runtime_data=_runtime_data_for(manager),
    )
    hass = _FakeHassForRepairs()

    check_metric_cap(hass, entry)
    assert fake_ir.created == []
    assert any(c.issue_id == "metric_cap_reached_B" for c in fake_ir.deleted)


def test_check_metric_cap_noop_when_ir_unavailable(monkeypatch) -> None:
    """No issue registry -> no-op."""
    from custom_components.telegraf_mqtt.repairs import check_metric_cap

    monkeypatch.setattr(
        "custom_components.telegraf_mqtt.repairs._ir",
        lambda _h: None,
    )
    manager = _manager()
    manager.dropped_metric_count = 12
    entry = _FakeEntry(
        entry_id="B",
        data={CONF_TOPIC_PATTERN: "telegraf/rack1/#"},
        runtime_data=_runtime_data_for(manager),
    )
    hass = _FakeHassForRepairs()
    check_metric_cap(hass, entry)  # should not raise


def test_check_device_cap_noop_when_runtime_data_missing(monkeypatch) -> None:
    """Pre-setup entry has no ``runtime_data`` -> no-op."""
    from custom_components.telegraf_mqtt.repairs import check_device_cap

    fake_ir = _FakeIr()
    _patch_ir(monkeypatch, fake_ir)
    entry = _FakeEntry(
        entry_id="A",
        data={CONF_TOPIC_PATTERN: "telegraf/#"},
        runtime_data=None,
    )
    hass = _FakeHassForRepairs()
    check_device_cap(hass, entry)
    assert fake_ir.created == []


def test_check_device_cap_noop_when_manager_missing(monkeypatch) -> None:
    """``runtime_data.manager`` is None -> no-op."""
    from custom_components.telegraf_mqtt.repairs import check_device_cap

    fake_ir = _FakeIr()
    _patch_ir(monkeypatch, fake_ir)
    from custom_components.telegraf_mqtt import TelegrafMqttRuntimeData

    runtime = TelegrafMqttRuntimeData(
        manager=None,  # type: ignore[arg-type]
        parser=None,  # type: ignore[arg-type]
        parser_stats=None,
        manufacturer=None,
        model=None,
        sw_version=None,
    )
    entry = _FakeEntry(entry_id="A", data={CONF_TOPIC_PATTERN: "telegraf/#"}, runtime_data=runtime)
    hass = _FakeHassForRepairs()
    check_device_cap(hass, entry)
    assert fake_ir.created == []


def test_check_metric_cap_noop_when_runtime_data_missing(monkeypatch) -> None:
    """Pre-setup entry has no ``runtime_data`` -> no-op."""
    from custom_components.telegraf_mqtt.repairs import check_metric_cap

    fake_ir = _FakeIr()
    _patch_ir(monkeypatch, fake_ir)
    entry = _FakeEntry(
        entry_id="B",
        data={CONF_TOPIC_PATTERN: "telegraf/rack1/#"},
        runtime_data=None,
    )
    hass = _FakeHassForRepairs()
    check_metric_cap(hass, entry)
    assert fake_ir.created == []


def test_check_metric_cap_noop_when_manager_missing(monkeypatch) -> None:
    """``runtime_data.manager`` is None -> no-op."""
    from custom_components.telegraf_mqtt.repairs import check_metric_cap

    fake_ir = _FakeIr()
    _patch_ir(monkeypatch, fake_ir)
    from custom_components.telegraf_mqtt import TelegrafMqttRuntimeData

    runtime = TelegrafMqttRuntimeData(
        manager=None,  # type: ignore[arg-type]
        parser=None,  # type: ignore[arg-type]
        parser_stats=None,
        manufacturer=None,
        model=None,
        sw_version=None,
    )
    entry = _FakeEntry(entry_id="B", data={CONF_TOPIC_PATTERN: "telegraf/rack1/#"}, runtime_data=runtime)
    hass = _FakeHassForRepairs()
    check_metric_cap(hass, entry)
    assert fake_ir.created == []


def test_check_device_id_conflict_noop_when_runtime_data_missing(
    monkeypatch,
) -> None:
    """Pre-setup entry has no ``runtime_data`` -> no-op."""
    from custom_components.telegraf_mqtt.repairs import check_device_id_conflict

    fake_ir = _FakeIr()
    _patch_ir(monkeypatch, fake_ir)
    entry = _FakeEntry(entry_id="A", data={CONF_TOPIC_PATTERN: "telegraf/#"}, runtime_data=None)
    hass = _FakeHassForRepairs()
    check_device_id_conflict(hass, entry)
    assert fake_ir.created == []


def test_check_device_id_conflict_noop_when_manager_missing(monkeypatch) -> None:
    """``runtime_data.manager`` is None -> no-op."""
    from custom_components.telegraf_mqtt.repairs import check_device_id_conflict

    fake_ir = _FakeIr()
    _patch_ir(monkeypatch, fake_ir)
    from custom_components.telegraf_mqtt import TelegrafMqttRuntimeData

    runtime = TelegrafMqttRuntimeData(
        manager=None,  # type: ignore[arg-type]
        parser=None,  # type: ignore[arg-type]
        parser_stats=None,
        manufacturer=None,
        model=None,
        sw_version=None,
    )
    entry = _FakeEntry(entry_id="A", data={CONF_TOPIC_PATTERN: "telegraf/#"}, runtime_data=runtime)
    hass = _FakeHassForRepairs()
    check_device_id_conflict(hass, entry)
    assert fake_ir.created == []


def test_check_device_id_conflict_skips_entries_without_runtime_data(
    monkeypatch,
) -> None:
    """Other entries that are mid-reload (no ``runtime_data`` yet) are skipped
    rather than crashing the conflict scan."""
    from custom_components.telegraf_mqtt.repairs import check_device_id_conflict

    fake_ir = _FakeIr()
    _patch_ir(monkeypatch, fake_ir)
    manager = _manager()
    # A real device for entry A.
    manager.process_message(
        "telegraf/host-a/cpu",
        b'{"name":"cpu","tags":{"host":"host-a"},"fields":{"usage_idle":1.0},"timestamp":1}',
    )
    entry_a = _FakeEntry(
        entry_id="A",
        data={CONF_TOPIC_PATTERN: "telegraf/#"},
        title="First",
        runtime_data=_runtime_data_for(manager),
    )
    # Entry B is mid-reload (no runtime_data, no manager).
    entry_b = _FakeEntry(
        entry_id="B",
        data={CONF_TOPIC_PATTERN: "other/#"},
        title="Second",
        runtime_data=None,
    )
    hass = _FakeHassForRepairs(config_entries=_FakeConfigEntriesForRepairs(entries=[entry_a, entry_b]))
    # Must not raise. The mid-reload entry contributes nothing to the scan.
    check_device_id_conflict(hass, entry_a)
    assert fake_ir.created == []


def test_check_device_id_conflict_resolves_when_other_overlap_clears(
    monkeypatch,
) -> None:
    """If entry A has devices but no other entry's devices overlap, the
    conflict issue is auto-resolved (deleted)."""
    from custom_components.telegraf_mqtt.repairs import check_device_id_conflict

    fake_ir = _FakeIr()
    _patch_ir(monkeypatch, fake_ir)
    manager_a = _manager()
    manager_b = _manager()
    # Different host names -> different device_id slugs, so no overlap.
    manager_a.process_message(
        "telegraf/host-a/cpu",
        b'{"name":"cpu","tags":{"host":"host-a"},"fields":{"usage_idle":1.0},"timestamp":1}',
    )
    manager_b.process_message(
        "other/host-b/cpu",
        b'{"name":"cpu","tags":{"host":"host-b"},"fields":{"usage_idle":1.0},"timestamp":1}',
    )
    entry_a = _FakeEntry(
        entry_id="A",
        data={CONF_TOPIC_PATTERN: "telegraf/#"},
        title="First",
        runtime_data=_runtime_data_for(manager_a),
    )
    entry_b = _FakeEntry(
        entry_id="B",
        data={CONF_TOPIC_PATTERN: "other/#"},
        title="Second",
        runtime_data=_runtime_data_for(manager_b),
    )
    hass = _FakeHassForRepairs(config_entries=_FakeConfigEntriesForRepairs(entries=[entry_a, entry_b]))
    check_device_id_conflict(hass, entry_a)
    # No overlap -> no issue created, and any prior issue is removed.
    assert fake_ir.created == []
    assert any(c.issue_id == "device_id_conflict_A" for c in fake_ir.deleted)
