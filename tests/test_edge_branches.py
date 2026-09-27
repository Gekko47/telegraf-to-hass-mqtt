"""Branch coverage for the production-hardening workstreams.

The project enforces 100% line coverage, and this file closes the
branches that the workstream tests left unexercised. Each test is named
for the *behaviour* it pins, not for the line it covers, because a line
number is not a reason -- a defensive branch that nothing can reach is a
branch to delete, and one that something can reach is a branch to test.
"""

from __future__ import annotations

import asyncio
import dataclasses
import types
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any
from unittest.mock import patch

import pytest

import custom_components.telegraf_mqtt as integration
from custom_components.telegraf_mqtt import diagnostics as diag
from custom_components.telegraf_mqtt.const import (
    CLEANUP_POLICY_ALWAYS,
    CONF_TOPIC_PATTERN,
    MAX_SEEN_HOSTS,
    MAX_SEEN_TOPICS,
    PLATFORM_HINT_NONE,
)
from custom_components.telegraf_mqtt.models import MetricDescriptor
from custom_components.telegraf_mqtt.parser import TelegrafParser
from custom_components.telegraf_mqtt.registry import DeviceManager, MetricRegistry
from custom_components.telegraf_mqtt.topics import mqtt_filter_covers


def _descriptor(field: str = "x", value: Any = 1.0, *, hint: str = "auto") -> MetricDescriptor:
    return MetricDescriptor(
        unique_key=f"cpu_{field}",
        measurement="cpu",
        tags={"host": "h"},
        field=field,
        value=value,
        timestamp=0.0,
        native_unit=None,
        suggested_device_class=None,
        suggested_state_class=None,
        entity_category=None,
        platform_hint=hint,  # type: ignore[arg-type]
    )


def _manager(**kwargs: Any) -> DeviceManager:
    clock = kwargs.pop("clock", None)
    manager = DeviceManager(clock=clock, **kwargs)
    manager.set_parser(TelegrafParser())
    return manager


# ---------------------------------------------------------------------------
# topics.mqtt_filter_covers -- outer shorter than inner, no wildcard
# ---------------------------------------------------------------------------


def test_covers_rejects_a_shorter_outer_without_a_trailing_hash() -> None:
    """``a/b`` is not a prefix filter, so it cannot cover ``a/b/c``.

    The distinct case from the ``#`` branch: both filters are pure
    literals and the outer simply runs out of levels, which is the one way
    coverage can fail while every compared level has matched so far.
    """
    assert mqtt_filter_covers("a/b", "a/b/c/d") is False


# ---------------------------------------------------------------------------
# config_flow: scope validation and the pre-flight message sink
# ---------------------------------------------------------------------------


def test_omitted_scope_key_is_accepted_and_falls_back_to_stored() -> None:
    """A client that renders a partial form omits the field entirely.

    That is not a validation error -- ``_clean_options`` falls back to the
    stored value -- so the step must save rather than bounce the user back
    to a form they already filled in correctly.
    """
    from custom_components.telegraf_mqtt.config_flow import TelegrafMqttOptionsFlow

    flow = TelegrafMqttOptionsFlow()
    assert flow._validate_scope({}) is None
    assert flow._validate_scope({"auto_discover": True}) is None


def test_preflight_sink_discards_a_retained_message_without_raising() -> None:
    """A retained message on the candidate topic can arrive during the
    pre-flight subscribe. The sink must accept it silently -- raising here
    would fail the user's reconfigure for a topic that is working
    perfectly well.
    """
    from custom_components.telegraf_mqtt.config_flow import _preflight_message_sink

    assert asyncio.run(_preflight_message_sink(types.SimpleNamespace(topic="a/b", payload=b"x"))) is None


# ---------------------------------------------------------------------------
# diagnostics._redact_topic edge shapes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", [None, 42, b"telegraf/a", ""])
def test_redact_topic_returns_none_for_anything_without_a_topic_string(value: Any) -> None:
    """No usable string means nothing to redact *or* to publish.

    A missing topic is the common case (a fresh entry that has received
    no message), so this must not raise and must not publish a sentinel.
    """
    assert diag._redact_topic(value) is None


def test_redact_topic_passes_a_single_segment_through() -> None:
    """``telegraf`` carries no host information -- there is nothing after
    the root to hide, and hashing it would destroy the one field that says
    which topic root the entry is watching.
    """
    assert diag._redact_topic("telegraf") == "telegraf"


def test_redact_topic_keeps_the_root_and_hides_only_the_tail() -> None:
    redacted = diag._redact_topic("telegraf/server01/cpu")
    assert redacted is not None
    assert redacted.startswith("telegraf/")
    assert "server01" not in redacted
    # Stable: two downloads of the same topic correlate.
    assert redacted == diag._redact_topic("telegraf/server01/cpu")


# ---------------------------------------------------------------------------
# diagnostics: runtime with a manager that is None (mid-unload)
# ---------------------------------------------------------------------------


@dataclass
class _StubRuntime:
    manager: Any
    parser: Any = None
    parser_stats: Any = None
    manufacturer: str | None = None
    model: str | None = None
    sw_version: str | None = None


@dataclass
class _StubEntry:
    entry_id: str = "entry-1"
    domain: str = "telegraf_mqtt"
    title: str = "Telegraf"
    unique_id: str = "telegraf/#"
    data: dict = field(default_factory=dict)
    options: dict = field(default_factory=dict)
    runtime_data: Any = None


def test_diagnostics_survives_a_manager_none_runtime() -> None:
    """``runtime_data.manager`` is ``| None`` for the unload window.

    Diagnostics is the one surface a user reaches for *after* something is
    already broken, so a mid-unload entry must produce an empty-but-valid
    runtime block rather than an ``AttributeError`` traceback that replaces
    the information they were trying to get.
    """
    entry = _StubEntry(runtime_data=_StubRuntime(manager=None))
    payload = asyncio.run(diag.async_get_config_entry_diagnostics(None, entry))  # type: ignore[arg-type]
    assert payload["runtime"] == {"manager": None, "devices": [], "device_count": 0}


# ---------------------------------------------------------------------------
# registry: the on_remove push-down and the platform-none removal
# ---------------------------------------------------------------------------


def test_platform_none_on_an_existing_metric_fires_on_remove() -> None:
    """M7/WS-C: flipping a live metric to ``platform: none`` must reach the
    entity registry, not just flip availability.

    The ``on_write(False, ...)`` that the registry also emits is not
    enough -- both platforms' routing listeners return early once
    ``get_metric`` yields ``None``, so an availability flip alone produces
    no removal signal and the entity lingers as a permanent ``unavailable``
    shell that only a reload clears.
    """
    removed: list[tuple[str, str]] = []
    registry = MetricRegistry(clock=lambda: 0.0, device_id="h1", on_remove=lambda d, k: removed.append((d, k)))
    registry.update(_descriptor("a"))
    assert removed == []

    registry._field_overrides = {"a": {"platform": PLATFORM_HINT_NONE}}
    writes: list[tuple[str, bool]] = []
    registry.update(_descriptor("a"), on_write=lambda key, avail, _v: writes.append((key, avail)))

    assert removed == [("h1", "cpu_a")]
    assert writes == [("cpu_a", False)]


def test_set_callbacks_pushes_on_remove_into_existing_registries() -> None:
    """A registry created before ``set_callbacks`` captured ``None``.

    Without the push-down that registry would keep a dead ``None`` and a
    later ``platform: none`` flip would silently orphan the entity -- the
    exact defect the callback exists to fix, just on the second device.
    """
    manager = _manager()
    manager.get_or_create_registry("h1", "h1")
    manager.get_or_create_registry("h2", "h2")
    assert all(registry._on_remove is None for registry in manager.devices.values())

    removed: list[tuple[str, str]] = []
    manager.set_callbacks(on_remove=lambda d, k: removed.append((d, k)))

    assert all(registry._on_remove is not None for registry in manager.devices.values())

    # Both devices take a live metric first, so the later flip is a
    # transition on an EXISTING metric -- the case that needs on_remove.
    for device_id in ("h1", "h2"):
        manager.devices[device_id].update(_descriptor(f"x_{device_id}"))
    assert removed == []

    for device_id in ("h1", "h2"):
        manager.devices[device_id]._field_overrides = {f"x_{device_id}": {"platform": PLATFORM_HINT_NONE}}
        manager.devices[device_id].update(_descriptor(f"x_{device_id}"))
    assert sorted(removed) == [("h1", "cpu_x_h1"), ("h2", "cpu_x_h2")]


# ---------------------------------------------------------------------------
# registry: bounded seen-sets
# ---------------------------------------------------------------------------


def test_seen_hosts_are_capped() -> None:
    """A broker with unbounded host cardinality must not grow the set for
    the life of the entry. ``check_no_traffic`` only asks "have we seen
    anything", so eviction is safe.
    """
    manager = _manager()
    for index in range(MAX_SEEN_HOSTS + 50):
        manager.record_seen_host(f"host-{index}", f"telegraf/host-{index}")
    assert len(manager.seen_hosts) <= MAX_SEEN_HOSTS
    assert manager.has_received_messages() is True


def test_seen_topics_are_capped() -> None:
    """Same ceiling for topics: the Repairs preview reads the first few,
    never all of them, so completeness buys nothing here.
    """
    manager = _manager()
    for index in range(MAX_SEEN_TOPICS + 50):
        manager.record_seen_host(f"host-{index}", f"telegraf/{index}/cpu")
    assert len(manager.seen_topics) <= MAX_SEEN_TOPICS


# ---------------------------------------------------------------------------
# registry: the seconds_since_* accessors
# ---------------------------------------------------------------------------


def test_seconds_since_first_message_is_none_before_any_traffic() -> None:
    """A brand-new entry has no first message; the caller must be able to
    tell that apart from "the first message was just now" (0.0).
    """
    manager = _manager(clock=lambda: 500.0)
    assert manager.seconds_since_first_message() is None


def test_seconds_since_first_message_counts_from_the_first_message() -> None:
    """Distinct from ``seconds_since_last_message``: this one measures the
    entry's total lifetime with traffic, which is the "has this entry been
    useful at all" signal.
    """
    now = [100.0]
    manager = _manager(clock=lambda: now[0])
    manager.record_seen_host("h", "t")
    now[0] = 400.0
    manager.record_seen_host("h", "t")
    # First at t=100, last at t=400, now 400.
    assert manager.seconds_since_first_message() == 300.0
    assert manager.seconds_since_last_message() == 0.0


# ---------------------------------------------------------------------------
# registry: the min_active_metrics floor inside cleanup
# ---------------------------------------------------------------------------


def test_cleanup_skips_a_device_below_the_minimum_active_floor() -> None:
    """A host that is still reporting must not be stripped from under the
    user just because it went quiet on most of its fields.

    The floor is the per-device guard; whole devices are retired by
    ``prune_stale_devices`` once the host itself stops reporting.
    """
    now = [10_000.0]
    # ``expire_after`` is wide so the device's heartbeat still counts as
    # live: the point of this test is the SECOND guard, ``min_active_metrics``.
    manager = _manager(clock=lambda: now[0], min_active_metrics=2, cleanup_delay=0, expire_after=10**9)
    registry = manager.get_or_create_registry("h1", "h1")
    assert registry is not None
    registry.update(dataclasses.replace(_descriptor("a"), cleanup_policy=CLEANUP_POLICY_ALWAYS))
    registry.last_any_metric = now[0]

    # One available metric, floor of 2 -> the device is protected.
    assert manager.cleanup() == []

    # Lift the floor and the SAME device is cleaned on the next call.
    manager.apply_options(min_active_metrics=1)
    assert manager.cleanup() == [("h1", "cpu_a")]


def test_cleanup_still_runs_when_the_floor_is_met() -> None:
    """The floor is a floor, not a blanket skip -- a device at or above it
    is cleaned on schedule.
    """
    now = [10_000.0]
    manager = _manager(clock=lambda: now[0], min_active_metrics=1, cleanup_delay=0, expire_after=10**9)
    registry = manager.get_or_create_registry("h1", "h1")
    assert registry is not None
    registry.update(dataclasses.replace(_descriptor("a"), cleanup_policy=CLEANUP_POLICY_ALWAYS))
    registry.last_any_metric = now[0]
    assert manager.cleanup() == [("h1", "cpu_a")]


# ---------------------------------------------------------------------------
# platforms: forget_metric releases the dedup key
# ---------------------------------------------------------------------------


@dataclass
class _PlatformHass:
    """Dispatcher double that records the signal each listener was
    registered against, so a test can drive the removal listener."""

    listeners: dict[str, Callable[..., Any]] = field(default_factory=dict)


def _platform_entry(manager: DeviceManager) -> Any:
    @dataclass
    class _E:
        entry_id: str = "entry-1"
        runtime_data: Any = None

        def async_on_unload(self, _cb: Callable[[], None]) -> None:
            return None

    runtime = integration.TelegrafMqttRuntimeData(
        manager=manager,
        parser=TelegrafParser(),
        parser_stats=None,
        manufacturer=None,
        model=None,
    )
    return _E(runtime_data=runtime)


@pytest.mark.parametrize(
    ("platform_name", "value", "override"),
    [
        # Each platform owns a different value shape, so the setup must
        # admit the metric to the platform under test.
        ("sensor", 1.0, {}),
        ("binary_sensor", 1, {"flag": {"platform": "binary_sensor"}}),
    ],
)
def test_removal_listener_releases_the_dedup_key(platform_name: str, value: Any, override: dict) -> None:
    """A removed metric must be re-adoptable when the host republishes.

    Without the release, ``added`` still claims the key, ``add_metric``
    returns early on its dedup guard, and the entity never comes back until
    the config entry is reloaded -- the documented
    ``Active -> Unavailable -> Cleanup Candidate -> Deleted`` lifecycle
    silently failing to reverse itself.
    """
    import importlib

    module = importlib.import_module(f"custom_components.telegraf_mqtt.{platform_name}")

    manager = _manager(field_overrides=override)
    registry = manager.get_or_create_registry("h1", "h1")
    assert registry is not None
    registry.update(_descriptor("flag", value))

    hass = _PlatformHass()
    entry = _platform_entry(manager)
    added: list[Any] = []

    def _fake_connect(_h: Any, signal: str, target: Callable[..., Any]) -> Callable[[], None]:
        hass.listeners[signal] = target
        return lambda: None

    with patch.object(module, "async_dispatcher_connect", _fake_connect):
        asyncio.run(module.async_setup_entry(hass, entry, lambda entities: added.extend(entities)))  # type: ignore[arg-type]

    assert len(added) == 1, "the metric should be adopted on setup"
    before = len(added)

    # The metric leaves the registry (cleanup, or a platform:none flip) and
    # the integration removes the entity.
    remove_signal = next(s for s in hass.listeners if s.endswith("_remove_metric_entry-1"))
    hass.listeners[remove_signal]("h1", "cpu_flag")
    assert len(added) == before, "removal must not add anything"

    # It comes back.
    registry.update(_descriptor("flag", value))
    new_signal = next(s for s in hass.listeners if s.endswith("_new_metric_entry-1"))
    hass.listeners[new_signal]("h1:cpu_flag")
    assert len(added) == before + 1, "the key must be re-adoptable after removal"


@pytest.mark.parametrize("platform_name", ["sensor", "binary_sensor"])
def test_entity_unregisters_itself_when_ha_removes_it(platform_name: str) -> None:
    """WS-E3: the platform table must not outlive the entities in it.

    ``forget_metric`` covers removal the *integration* initiates. Every
    other path -- a config-entry reload, the platform being torn down, HA
    dropping an entity for its own reasons -- goes through
    ``async_will_remove_from_hass``. Without it the table keeps a strong
    reference to a dead entity and its key, and ``add_metric`` then
    returns early on the dedup guard for the rest of the entry's life: the
    metric would silently never come back.

    The entity is driven directly here because the stub ``Entity`` base
    has no removal machinery of its own.
    """
    import importlib

    module = importlib.import_module(f"custom_components.telegraf_mqtt.{platform_name}")
    manager = _manager()
    entry = _platform_entry(manager)
    table: dict[str, Any] = {}
    entity = module.__dict__["TelegrafMqttSensor" if platform_name == "sensor" else "TelegrafMqttBinarySensor"](
        entry, "host1:cpu_x", table
    )

    table["host1:cpu_x"] = entity
    assert "host1:cpu_x" in table

    asyncio.run(entity.async_will_remove_from_hass())
    assert table == {}

    # Idempotent: HA may call the hook more than once across a reload.
    asyncio.run(entity.async_will_remove_from_hass())
    assert table == {}


def test_entity_without_a_platform_table_tolerates_removal() -> None:
    """An entity constructed without a table (the shape several unit tests
    use) must not raise on removal rather than failing inside HA's
    teardown, which would mask whatever was actually being torn down."""
    import importlib

    sensor_module = importlib.import_module("custom_components.telegraf_mqtt.sensor")
    manager = _manager()
    entity = sensor_module.TelegrafMqttSensor(_platform_entry(manager), "host1:cpu_x")
    asyncio.run(entity.async_will_remove_from_hass())


# ---------------------------------------------------------------------------
# diagnostics: the pending-cleanup block (M13)
# ---------------------------------------------------------------------------


@dataclass
class _CleanupEntry:
    entry_id: str = "entry-1"
    domain: str = "telegraf_mqtt"
    title: str = "Telegraf"
    unique_id: str = "telegraf/#"
    data: dict = field(default_factory=lambda: {CONF_TOPIC_PATTERN: "telegraf/#"})
    options: dict = field(default_factory=dict)
    runtime_data: Any = None


def _diagnostics_for(manager: DeviceManager) -> dict[str, Any]:
    runtime = integration.TelegrafMqttRuntimeData(
        manager=manager,
        parser=TelegrafParser(),
        parser_stats=None,
        manufacturer=None,
        model=None,
    )
    entry = _CleanupEntry(runtime_data=runtime)
    return asyncio.run(diag.async_get_config_entry_diagnostics(None, entry))  # type: ignore[arg-type]


def test_diagnostics_reports_metrics_queued_for_removal() -> None:
    """The Cleanup-Candidate state is otherwise invisible.

    A metric sits there for ``cleanup_delay`` (30 days by default) before
    it disappears, and nothing in the payload said so -- a user who
    believed "the integration is deleting my entities" had no way to
    confirm it from a download.
    """
    now = [0.0]
    manager = _manager(clock=lambda: now[0], expire_after=5, cleanup_delay=9999)
    registry = manager.get_or_create_registry("host-a", "host-a")
    assert registry is not None
    registry.update(_descriptor("a"))
    registry.update(_descriptor("b"))
    now[0] = 100.0
    manager.check_expiry()

    block = _diagnostics_for(manager)["runtime"]["pending_cleanup"]
    assert block["total"] == 2
    assert block["truncated"] is False
    keys = {item["unique_key"] for item in block["metrics"]}
    assert keys == {"cpu_a", "cpu_b"}
    # The device id is hashed, exactly as everywhere else in this module.
    for item in block["metrics"]:
        assert "host" not in item["device_id"]


def test_diagnostics_pending_cleanup_truncates_and_says_so() -> None:
    """A truncated list that does not admit it is worse than no list: the
    user concludes there is nothing queued."""
    from custom_components.telegraf_mqtt import const as const_mod

    manager = _manager(clock=lambda: 100.0, expire_after=5, cleanup_delay=9999)
    for device in range(3):
        registry = manager.get_or_create_registry(f"host-{device}", f"host-{device}")
        assert registry is not None
        for index in range(const_mod.DIAGNOSTICS_PENDING_CLEANUP_LIMIT):
            registry.update(_descriptor(f"x{index}"))
    # Force every metric into the candidate state, then let the expiry
    # pass confirm it.
    for registry in manager.devices.values():
        for state in registry._states.values():
            state.cleanup_candidate_since = 0.0
    manager.check_expiry()

    block = _diagnostics_for(manager)["runtime"]["pending_cleanup"]
    assert block["truncated"] is True
    assert len(block["metrics"]) == const_mod.DIAGNOSTICS_PENDING_CLEANUP_LIMIT
    assert block["total"] == 3 * const_mod.DIAGNOSTICS_PENDING_CLEANUP_LIMIT


def test_pending_cleanup_is_empty_when_nothing_is_queued() -> None:
    """A healthy entry must not report a scary-looking block of nothing."""
    manager = _manager(clock=lambda: 0.0)
    manager.get_or_create_registry("host-a", "host-a")
    block = _diagnostics_for(manager)["runtime"]["pending_cleanup"]
    assert block == {"total": 0, "truncated": False, "metrics": []}


# ---------------------------------------------------------------------------
# platforms: an unknown entity category must not fail entity construction (N2)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("platform_name", ["sensor", "binary_sensor"])
def test_unknown_entity_category_degrades_instead_of_raising(platform_name: str, caplog: Any) -> None:
    """``category_overrides`` is free text the user types, so an
    unrecognised value is a configuration mistake, not a bug.

    HA would raise ``ValueError`` from inside ``Entity`` construction,
    which takes down the *whole platform* rather than the one field, and
    the user sees an error naming an internal module rather than the
    option they mistyped.
    """
    import importlib

    module = importlib.import_module(f"custom_components.telegraf_mqtt.{platform_name}")
    with caplog.at_level("WARNING"):
        assert module._entity_category(None) is None
        assert module._entity_category("") is None
        assert module._entity_category("config") is not None
        assert module._entity_category("diagnostic") is not None
        assert module._entity_category("nonsense") is None
    assert "nonsense" in caplog.text
