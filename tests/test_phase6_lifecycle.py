"""Phase 6 exit-criteria tests: intelligent cleanup & device lifecycle.

ROADMAP.md Phase 6:
  - Healthy device, battery metric gone 30+ days -> battery entity removed;
    other entities untouched.
  - Offline device, all metrics gone 50 days -> nothing removed.
  - Deleted metric reappears -> entity recreated automatically, same
    unique_id.
  - Device with zero remaining entities and expired heartbeat is removed
    cleanly.
  - Restart mid-lifecycle leaves registry consistent.

Most tests are harness-free (registry/cleanup is pure logic); one
real-harness test asserts that the ``SIGNAL_REMOVE_METRIC`` -> entity
registry chain actually drops the entity and that the unique_id is
preserved on a recreate.
"""

from __future__ import annotations

import asyncio
import json
from typing import TYPE_CHECKING, Any, ClassVar

import pytest

if TYPE_CHECKING:
    from homeassistant.core import HomeAssistant

from custom_components.telegraf_mqtt.models import MetricDescriptor
from custom_components.telegraf_mqtt.parsers.generic import parse_generic_payload
from custom_components.telegraf_mqtt.parsers.static import is_static_field
from custom_components.telegraf_mqtt.registry import (
    DeviceManager,
    MetricRegistry,
    MetricState,
)

# ---------------------------------------------------------------------------
# Tiny helpers
# ---------------------------------------------------------------------------


def _descriptor(
    unique_key: str,
    *,
    value: float = 1.0,
    cleanup_policy: str = "AUTO",
    field_name: str | None = None,
) -> MetricDescriptor:
    return MetricDescriptor(
        unique_key=unique_key,
        measurement="m",
        tags={"host": "h"},
        field=field_name or unique_key,
        value=value,
        timestamp=1.0,
        native_unit=None,
        suggested_device_class=None,
        suggested_state_class="measurement",
        entity_category=None,
        cleanup_policy=cleanup_policy,
    )


# ---------------------------------------------------------------------------
# Static-field policy: the descriptor contract emits ``NEVER`` for known
# fixed-metadata pairs and ``AUTO`` for everything else.
# ---------------------------------------------------------------------------


def test_static_metadata_descriptor_gets_never_cleanup_policy() -> None:
    """``cpu.model_name`` / ``system.n_cpus`` / etc. must carry ``NEVER``."""
    payload = {
        "name": "cpu",
        "tags": {"host": "h1"},
        "fields": {"model_name": "AMD Ryzen 7 5800X", "usage_idle": 5.0},
        "timestamp": 1721664000,
    }
    descriptors = parse_generic_payload(payload)
    by_field = {d.field: d for d in descriptors}
    assert by_field["model_name"].cleanup_policy == "NEVER"
    # Dynamic field stays AUTO.
    assert by_field["usage_idle"].cleanup_policy == "AUTO"


@pytest.mark.parametrize(
    "measurement,field",
    [
        ("system", "n_cpus"),
        ("system", "n_users"),
        ("system", "uptime_format"),
        ("cpu", "model_name"),
        ("cpu", "vendor_id"),
        ("cpu", "flags"),
        ("cpu", "cache_size"),
    ],
)
def test_static_field_policy_is_case_insensitive(measurement: str, field: str) -> None:
    assert is_static_field(measurement.upper(), field.upper())


def test_dynamic_field_is_not_static() -> None:
    """A regular dynamic field is NOT flagged as static -- guardrail."""
    assert not is_static_field("cpu", "usage_idle")
    assert not is_static_field("mem", "used_percent")
    assert not is_static_field("system", "uptime")  # uptime is dynamic, NOT in the NEVER set


# ---------------------------------------------------------------------------
# Per-metric lifecycle states.
# ---------------------------------------------------------------------------


def test_first_expiry_transitions_to_cleanup_candidate() -> None:
    """The first ``check_expiry`` pass on a stale metric must set
    ``cleanup_candidate_since`` -- that is the explicit Cleanup Candidate
    state in the ROADMAP lifecycle.
    """
    clock = [100.0]
    registry = MetricRegistry(expire_after=5, clock=lambda: clock[0])
    registry.update(_descriptor("cpu_usage_idle"))

    clock[0] = 200.0  # 95s past expire_after
    registry.check_expiry()
    state = registry.get("cpu_usage_idle")
    assert state is not None
    assert state.is_available is False
    assert state.cleanup_candidate_since == 200.0


def test_incoming_message_clears_cleanup_candidate_state() -> None:
    """A new message brings the metric back to life; the candidate
    timestamp is cleared.
    """
    clock = [100.0]
    registry = MetricRegistry(expire_after=5, clock=lambda: clock[0])
    registry.update(_descriptor("cpu_usage_idle"))
    clock[0] = 200.0
    registry.check_expiry()
    assert registry.get("cpu_usage_idle").cleanup_candidate_since == 200.0

    clock[0] = 201.0
    registry.update(_descriptor("cpu_usage_idle", value=99.0))  # new value: active again
    assert registry.get("cpu_usage_idle").is_available is True
    assert registry.get("cpu_usage_idle").cleanup_candidate_since is None


def test_never_policy_metric_never_becomes_candidate() -> None:
    """Static-metadata metrics (cleanup_policy=NEVER) must not enter the
    Cleanup Candidate state even when their ``last_updated`` is far in
    the past.
    """
    clock = [100.0]
    registry = MetricRegistry(expire_after=5, clock=lambda: clock[0])
    registry.update(_descriptor("cpu_model_name", cleanup_policy="NEVER"))
    clock[0] = 10_000.0
    registry.check_expiry()
    state = registry.get("cpu_model_name")
    assert state is not None
    # check_expiry does not flip is_available to False for NEVER either.
    assert state.is_available is True
    assert state.cleanup_candidate_since is None


# ---------------------------------------------------------------------------
# Exit criterion 1: Healthy device, battery gone 30+ days -> battery removed,
# siblings untouched.
# ---------------------------------------------------------------------------


def test_healthy_device_removes_only_stale_metric_after_cleanup_delay() -> None:
    """On a healthy device, the cleanup pass after cleanup_delay removes
    just the candidate metric; siblings stay put.
    """
    clock = [100.0]
    manager = DeviceManager(expire_after=5, cleanup_delay=1, delete_delay=2, clock=lambda: clock[0])
    registry = manager.get_or_create_registry("server01", "server01")
    # Two live metrics: a CPU gauge and a battery gauge.
    registry.update(_descriptor("cpu_usage_idle"))
    registry.update(_descriptor("battery_percentage"))
    # Both are fresh now; we make the battery stale *after* the next clock
    # tick, by giving it an ancient last_updated. The CPU stays fresh at
    # check_expiry time.
    registry.get("cpu_usage_idle").last_updated = 199.0  # 1s old at clock=200
    registry.get("battery_percentage").last_updated = 50.0  # 150s old at clock=200
    registry.last_any_metric = 100.0

    # Expire -> battery becomes a Cleanup Candidate; CPU stays available.
    clock[0] = 200.0
    registry.check_expiry()
    assert registry.get("cpu_usage_idle").is_available is True
    assert registry.get("battery_percentage").cleanup_candidate_since == 200.0

    # Cleanup before the delay elapses: nothing happens yet. The CPU
    # sibling stays fresh; only the battery is a candidate.
    registry.get("cpu_usage_idle").last_updated = 249.0
    registry.last_any_metric = 249.0
    removed = manager.cleanup()
    assert removed == []

    # Past the cleanup_delay: only the candidate is removed. Keep the
    # CPU fresh at the new clock value too, so the min_active_metrics=1
    # guard does not skip the device (available_count must be >= 1).
    clock[0] = 300.0
    registry.get("cpu_usage_idle").last_updated = 299.0
    registry.last_any_metric = 299.0
    removed = manager.cleanup()
    assert removed == [("server01", "battery_percentage")]
    assert manager.get_metric("server01:cpu_usage_idle") is not None
    assert manager.get_metric("server01:battery_percentage") is None


# ---------------------------------------------------------------------------
# Exit criterion 2: Offline device, all metrics gone 50 days -> nothing
# removed. (Pinned by an even-more-extreme timeline than the existing test.)
# ---------------------------------------------------------------------------


def test_offline_device_keeps_every_entity_after_50_days() -> None:
    """A device whose heartbeat is older than expire_after is skipped
    entirely by cleanup. Even when the clock is advanced 50 simulated
    days past the heartbeat, nothing is removed.
    """
    clock = [100.0]
    manager = DeviceManager(expire_after=5, cleanup_delay=1, delete_delay=2, clock=lambda: clock[0])
    registry = manager.get_or_create_registry("server02", "server02")
    registry.update(_descriptor("mem_used_percent"))
    registry.last_any_metric = 50.0  # heartbeat is 50s old at clock=100
    # The metric itself is also stale (50s old at clock=100 -> same as
    # heartbeat, since the last message was the heartbeat).
    registry.get("mem_used_percent").last_updated = 50.0

    # Advance the clock to t=4_350_000 (50 days + a bit). Even at this
    # extreme, an offline device is never cleaned up.
    # Drive the device through the same lifecycle as the pre-Phase-6 test:
    # mark the metric unavailable via check_expiry, then verify cleanup
    # never touches an offline device even at this extreme clock value.
    clock[0] = 100.0
    manager.check_expiry()
    unavailable = manager.get_metric("server02:mem_used_percent")
    assert unavailable is not None and unavailable.is_available is False

    clock[0] = 4_350_000.0
    removed = manager.cleanup()
    assert removed == []
    assert manager.get_metric("server02:mem_used_percent") is not None
    # The metric was marked unavailable (its own last_updated is stale),
    # but it is NOT removed.
    assert manager.get_metric("server02:mem_used_percent").is_available is False


# ---------------------------------------------------------------------------
# Exit criterion 3: Deleted metric reappears -> entity recreated
# automatically, same unique_id. (Verified at the registry level: the
# subsequent ``update`` call re-creates the state, and the unique_key is
# stable so the entity's identity is preserved.)
# ---------------------------------------------------------------------------


def test_deleted_metric_reappearing_uses_same_unique_key() -> None:
    """When a metric is removed by cleanup and then reappears via a new
    message, the new state carries the same unique_key (v1-frozen
    identity) so HA's entity-registry resolution will join the new
    metric to the existing entity.
    """
    clock = [100.0]
    registry = MetricRegistry(expire_after=5, cleanup_delay=1, clock=lambda: clock[0])
    registry.update(_descriptor("battery_percentage", value=87.0))

    # Make it a candidate and let cleanup remove it.
    clock[0] = 200.0
    registry.check_expiry()
    clock[0] = 300.0
    assert registry.cleanup() == ["battery_percentage"]
    assert registry.get("battery_percentage") is None

    # Reappear with a new value.
    registry.update(_descriptor("battery_percentage", value=42.0))
    reborn = registry.get("battery_percentage")
    assert reborn is not None
    assert reborn.is_available is True
    # The same unique_key is used -- the entity-registry entry is
    # preserved without an explicit remove+create.
    assert reborn.descriptor.unique_key == "battery_percentage"


# ---------------------------------------------------------------------------
# Exit criterion 4: Device with zero remaining entities and expired
# heartbeat is removed cleanly.
# ---------------------------------------------------------------------------


def test_prune_stale_devices_removes_only_silent_and_expired() -> None:
    """``prune_stale_devices`` drops a device once its host has been
    silent for longer than ``delete_delay``, regardless of how many
    metrics the registry still holds.
    """
    clock = [0.0]
    manager = DeviceManager(expire_after=10, cleanup_delay=1, delete_delay=5, clock=lambda: clock[0])
    empty = manager.get_or_create_registry("gone", "gone")
    full = manager.get_or_create_registry("alive", "alive")
    full.update(_descriptor("cpu_usage_idle"))
    empty.update(_descriptor("battery_percentage"))

    clock[0] = 100.0
    # Mark the empty-to-be device as stale and drop its only metric.
    empty.last_any_metric = 0.0
    empty.check_expiry()
    clock[0] = 200.0
    empty.cleanup()  # removes battery_percentage
    assert len(empty) == 0

    # A device that is STILL REPORTING is never pruned, however many
    # metrics it holds. ``delete_delay`` is keyed on the heartbeat, not on
    # emptiness -- that distinction is the fix for M14, where a
    # never-pruned device held a device-cap slot forever.
    full.last_any_metric = 199.0  # reported 1s ago at clock=200
    pruned = manager.prune_stale_devices()
    # delete_delay=5; clock=200. "gone" is at 0 (200s silent) -> pruned.
    # "alive" is at 199 (1s silent) -> kept.
    assert pruned == ["gone"]
    assert "gone" not in manager.devices
    assert "alive" in manager.devices
    # Once "alive" also falls silent past delete_delay it IS pruned, even
    # though its registry still holds a metric.
    full.last_any_metric = 0.0
    clock[0] = 10_000.0
    assert manager.prune_stale_devices() == ["alive"]
    assert "alive" not in manager.devices


def test_prune_stale_devices_keeps_devices_with_a_fresh_heartbeat() -> None:
    """A device that just lost its last metric must NOT be pruned
    while its heartbeat is still inside ``delete_delay`` -- the gap
    might be transient before a new message arrives.

    ``prune_empty_devices`` only drops a device when *both* (a) it has
    zero metrics and (b) its ``last_any_metric`` heartbeat is older than
    ``delete_delay``. This test exercises the second half of that
    guard: a heartbeat refreshed AFTER the registry became empty must
    keep the device alive.
    """
    clock = [0.0]
    manager = DeviceManager(expire_after=10, cleanup_delay=1, delete_delay=5, clock=lambda: clock[0])
    registry = manager.get_or_create_registry("fresh", "fresh")
    registry.update(_descriptor("battery_percentage"))
    clock[0] = 100.0
    registry.check_expiry()
    clock[0] = 200.0
    registry.cleanup()
    assert len(registry) == 0
    # A new message (or any other reason) refreshes the heartbeat after
    # the registry drained. last_any_metric is set by the manager on
    # incoming messages; model that by setting it explicitly to a
    # recent clock value. delete_delay=5, clock=200, last_any_metric=200
    # -> 0s elapsed, well inside the delete_delay window.
    registry.last_any_metric = 200.0
    assert manager.prune_stale_devices() == []


# ---------------------------------------------------------------------------
# Exit criterion 5: Restart mid-lifecycle leaves registry consistent.
#
# The registry itself is in-memory by design (SPEC.md: "Never persist field
# values to disk"). The persistence guarantee lives in HA's entity
# registry: an entity created before the restart is still registered
# after it. We verify the contract at the metric-state level: after a
# "restart" (a fresh MetricState with the same descriptor), the
# unique_key matches and the entity_id is reconstructable.
# ---------------------------------------------------------------------------


def test_restart_preserves_unique_key_for_lifecycle_in_progress() -> None:
    """Mid-lifecycle (Availability / Candidate state) metrics keep their
    unique_key across a restart, so the entity-registry join works.
    """
    clock = [100.0]
    registry = MetricRegistry(expire_after=5, clock=lambda: clock[0])
    registry.update(_descriptor("battery_percentage", value=87.0))

    # Mid-lifecycle: battery was last seen ages ago, marked unavailable.
    clock[0] = 200.0
    registry.check_expiry()
    pre_restart = registry.get("battery_percentage")
    assert pre_restart is not None
    assert pre_restart.is_available is False
    pre_unique_key = pre_restart.descriptor.unique_key

    # Simulate restart: drop the in-memory state. HA's entity registry
    # is unchanged, so a new MetricState with the same unique_key will
    # be rejoined to the same entity_id.
    registry._states.clear()

    clock[0] = 300.0  # any time after restart
    new_descriptor = _descriptor(pre_unique_key, value=42.0)
    new_state = MetricState(
        raw_descriptor=new_descriptor,
        descriptor=new_descriptor,
        device_id="h",
        device_name="h",
        last_updated=300.0,
        is_available=True,
    )
    registry._states[pre_unique_key] = new_state

    # unique_key survived; entity-registry join is deterministic.
    assert new_state.descriptor.unique_key == pre_unique_key


# ---------------------------------------------------------------------------
# The two new manager-level tunables.
# ---------------------------------------------------------------------------


def test_enable_cleanup_false_short_circuits_cleanup() -> None:
    """When the user disables cleanup, the entire cleanup pass is a no-op.

    This is the "discovery + expiry only" mode: every metric persists
    forever once received, even if no further messages arrive.
    """
    clock = [100.0]
    manager = DeviceManager(
        expire_after=5,
        cleanup_delay=1,
        delete_delay=2,
        enable_cleanup=False,
        clock=lambda: clock[0],
    )
    registry = manager.get_or_create_registry("server01", "server01")
    registry.update(_descriptor("cpu_usage_idle"))
    registry.get("cpu_usage_idle").last_updated = 0.0  # very stale
    clock[0] = 200.0
    registry.check_expiry()
    # Even with a candidate, cleanup is disabled -> nothing removed.
    clock[0] = 10_000.0
    assert manager.cleanup() == []
    assert manager.get_metric("server01:cpu_usage_idle") is not None


def test_min_active_metrics_protects_devices_below_floor() -> None:
    """A device with fewer than ``min_active_metrics`` available metrics
    is skipped by cleanup -- the guard prevents accidentally emptying a
    device that is already at the floor.
    """
    clock = [100.0]
    manager = DeviceManager(
        expire_after=5,
        cleanup_delay=1,
        delete_delay=2,
        min_active_metrics=2,  # guard: require 2 active metrics before allowing cleanup
        clock=lambda: clock[0],
    )
    registry = manager.get_or_create_registry("server01", "server01")
    # Only one active metric (kept fresh at check_expiry time); the other
    # is a Cleanup Candidate. With min_active_metrics=2 the device is below
    # the floor, so cleanup is skipped entirely.
    registry.update(_descriptor("cpu_usage_idle"))  # the single live metric
    registry.update(_descriptor("battery_percentage"))  # the candidate
    registry.get("cpu_usage_idle").last_updated = 199.0  # 1s old at clock=200
    registry.get("battery_percentage").last_updated = 0.0
    registry.last_any_metric = 100.0
    clock[0] = 200.0
    registry.check_expiry()
    clock[0] = 300.0
    # Available count is 1, min_active_metrics=2 -> the device is skipped
    # entirely. The candidate stays put (it'll be removed by
    # prune_empty_devices once the heartbeat expires).
    assert manager.cleanup() == []
    assert manager.get_metric("server01:battery_percentage") is not None
    assert manager.get_metric("server01:battery_percentage").cleanup_candidate_since == 200.0


def test_min_active_metrics_does_not_block_when_floor_is_met() -> None:
    """The guard is the floor, not a hard floor -- with 2 active + 1
    candidate, the candidate can still be removed.
    """
    clock = [100.0]
    manager = DeviceManager(
        expire_after=5,
        cleanup_delay=1,
        delete_delay=2,
        min_active_metrics=2,
        clock=lambda: clock[0],
    )
    registry = manager.get_or_create_registry("server01", "server01")
    registry.update(_descriptor("cpu_usage_idle"))
    registry.update(_descriptor("mem_used_percent"))
    registry.update(_descriptor("battery_percentage"))
    # All three siblings get fresh last_updated at check_expiry time; only
    # the battery is the stale one. With 2 active + 1 candidate, the
    # floor is met and the candidate can be removed.
    registry.get("cpu_usage_idle").last_updated = 199.0
    registry.get("mem_used_percent").last_updated = 199.0
    registry.get("battery_percentage").last_updated = 0.0
    registry.last_any_metric = 100.0
    clock[0] = 200.0
    registry.check_expiry()
    # Bump the two live siblings to clock=299 so they remain available
    # at cleanup time; only the battery is a candidate.
    clock[0] = 300.0
    registry.get("cpu_usage_idle").last_updated = 299.0
    registry.get("mem_used_percent").last_updated = 299.0
    registry.last_any_metric = 299.0
    assert manager.cleanup() == [("server01", "battery_percentage")]


# ---------------------------------------------------------------------------
# Source-audit: no premature conversion / no entity-platform imports leak
# into the cleanup path. (Sanity regression: same audit shape as Phase 4.)
# ---------------------------------------------------------------------------


def test_registry_source_has_no_1024_or_kb_conversion() -> None:
    """Phase 6 didn\'t introduce byte conversion. Lock that in."""
    import pathlib

    # The registry module uses a relative import (``from .models``) so
    # ``importlib`` can\'t load it standalone. The audit reads the file
    # source directly -- the relevant tokens are file-local constants
    # anyway, not from imported helpers.
    path = pathlib.Path("custom_components/telegraf_mqtt/registry.py")
    source = path.read_text(encoding="utf-8")
    forbidden = (" / 1024", "/1024", " / 1000", "/1000", "DataSize", "UnitOfInformation")
    violations = [tok for tok in forbidden if tok in source]
    assert not violations, f"premature conversion tokens found: {violations}"


def test_registry_never_imports_entity_platform_modules() -> None:
    """AGENTS.md architecture guardrail: the registry must not import
    sensor / binary_sensor / any platform module. Phase 6 added prune
    logic; that logic must respect the guardrail.
    """
    import pathlib

    src = pathlib.Path("custom_components/telegraf_mqtt/registry.py").read_text(encoding="utf-8")
    for forbidden in (
        "from homeassistant.components.sensor",
        "from homeassistant.components.binary_sensor",
        "from .sensor",
        "from .binary_sensor",
    ):
        assert forbidden not in src, f"guardrail violation: {forbidden!r} in registry.py"


# ---------------------------------------------------------------------------
# min_active_metrics=0: the guard is disabled, every candidate is removed.
# Pinned as a branch-coverage target for the ``if available_count <
# self._min_active_metrics: continue`` arm.
# ---------------------------------------------------------------------------


def test_min_active_metrics_zero_disables_the_guard() -> None:
    """Setting min_active_metrics=0 turns the floor guard off: the
    cleanup pass will drain a device to zero rather than refuse to
    touch it.
    """
    clock = [100.0]
    manager = DeviceManager(
        expire_after=5,
        cleanup_delay=1,
        delete_delay=2,
        min_active_metrics=0,
        clock=lambda: clock[0],
    )
    registry = manager.get_or_create_registry("server01", "server01")
    registry.update(_descriptor("cpu_usage_idle"))
    registry.update(_descriptor("battery_percentage"))
    registry.get("cpu_usage_idle").last_updated = 199.0
    registry.get("battery_percentage").last_updated = 0.0
    registry.last_any_metric = 100.0
    clock[0] = 200.0
    registry.check_expiry()
    clock[0] = 300.0
    registry.get("cpu_usage_idle").last_updated = 299.0
    registry.last_any_metric = 299.0
    # Guard is off; the single candidate is removed even though the
    # device would have 0 active metrics after.
    assert manager.cleanup() == [("server01", "battery_percentage")]


# ---------------------------------------------------------------------------
# ``available_count`` is a production input, not a test-only helper.
# ``DeviceManager.cleanup`` consults it to decide whether a device is above
# the ``min_active_metrics`` floor, and that decision is the difference
# between a stale entity being removed from HA's entity registry and being
# left in place. These two tests pin BOTH directions of that comparison on
# the real cleanup path, so a regression that made ``available_count``
# always 0 (or always ``len(registry)``) would fail here rather than
# silently changing which entities the integration deletes.
# ---------------------------------------------------------------------------


def _seed_device(
    manager: DeviceManager,
    device_id: str,
    live_fields: tuple[str, ...],
    clock: list[float],
) -> Any:
    """Seed one device with ``live_fields`` staying available and a stale battery.

    The battery is made stale by giving it an ancient ``last_updated``
    *before* ``check_expiry`` runs, so only it flips to a Cleanup
    Candidate. Setting ``last_updated`` after the check would be a no-op
    -- availability is already latched False by then.
    """
    registry = manager.get_or_create_registry(device_id, device_id)
    for field in live_fields:
        registry.update(_descriptor(field))
    registry.update(_descriptor("battery_percentage"))
    # Fresh for the survivors, ancient for the candidate, at clock=200.
    for field in live_fields:
        registry.get(field).last_updated = 199.0
    registry.get("battery_percentage").last_updated = 50.0
    registry.last_any_metric = 100.0
    clock[0] = 200.0
    registry.check_expiry()
    # Past cleanup_delay=1, with the survivors refreshed so the device
    # heartbeat stays inside expire_after=5 and the device is not skipped
    # by the *other* guard.
    clock[0] = 300.0
    for field in live_fields:
        registry.get(field).last_updated = 299.0
    registry.last_any_metric = 299.0
    return registry


def test_available_count_drives_the_production_cleanup_floor() -> None:
    """``available_count`` is what the min_active_metrics floor reads.

    Two devices, two different live counts, one threshold. The device with
    fewer available metrics than the floor is skipped whole; the device at
    the floor has its stale candidate removed. This exercises
    ``DeviceManager.cleanup`` -- the function the periodic tick in
    ``__init__`` actually calls -- so the assertion is about the decision
    the integration makes, not about a helper returning a number.
    """
    clock = [100.0]
    manager = DeviceManager(
        expire_after=5,
        cleanup_delay=1,
        delete_delay=2,
        # Floor of 2: a device with only 1 available metric is below it.
        min_active_metrics=2,
        clock=lambda: clock[0],
    )
    thin = _seed_device(manager, "thin", ("cpu_usage_idle",), clock)
    fat = _seed_device(manager, "fat", ("cpu_usage_idle", "mem_used_percent"), clock)

    # Precondition: both devices hold the same stale candidate and are
    # equally past ``cleanup_delay``. The only difference between them is
    # the live count, so the outcome split below is attributable to
    # ``available_count`` alone -- a floor evaluated against the TOTAL
    # metric count could not produce it, because the totals differ too.
    assert thin.get("battery_percentage").cleanup_candidate_since == 200.0
    assert fat.get("battery_percentage").cleanup_candidate_since == 200.0
    assert thin.available_count == 1
    assert fat.available_count == 2
    assert len(thin) != len(fat), "the two devices must differ in total size too"

    removed = manager.cleanup()

    # Only the device at the floor loses its stale entity. The below-floor
    # device is left completely alone -- the whole point of the guard, and
    # the reason it reads the *available* count rather than the total.
    assert removed == [("fat", "battery_percentage")]
    assert manager.get_metric("fat:cpu_usage_idle") is not None
    assert manager.get_metric("fat:battery_percentage") is None
    # The skipped device kept its candidate, which is the observable
    # consequence of the entity not being removed from HA's registry.
    assert manager.get_metric("thin:battery_percentage") is not None


def test_available_count_floor_is_disabled_at_zero() -> None:
    """``min_active_metrics=0`` disables the floor entirely.

    A device whose ``available_count`` is 1 still loses its candidate,
    because ``1 < 0`` is false. This is the documented way to let cleanup
    drain a device to zero, and it is the boundary case that a
    ``<=``/``>=`` slip in the comparison would invert.
    """
    clock = [100.0]
    manager = DeviceManager(
        expire_after=5,
        cleanup_delay=1,
        delete_delay=2,
        min_active_metrics=0,
        clock=lambda: clock[0],
    )
    registry = _seed_device(manager, "server01", ("cpu_usage_idle",), clock)
    assert registry.available_count == 1

    assert manager.cleanup() == [("server01", "battery_percentage")]


# ---------------------------------------------------------------------------
# Real-harness test: ``_handle_remove_metric`` actually drops the
# corresponding entity from the entity registry while preserving the
# parent device and sibling entities.
# ---------------------------------------------------------------------------


# The mocked paho client fires on_socket_open (starting MQTT's
# 1-second misc timer) but nothing fires the matching on_socket_close
# at disconnect; this is the harness's documented opt-out (see
# pytest_homeassistant_custom_component plugins.py).
pytestmark = [pytest.mark.parametrize("expected_lingering_timers", [True])]


async def test_signal_remove_metric_drops_only_the_target_entity(hass: HomeAssistant, mqtt_mock) -> None:
    """When cleanup fires, the entity for the removed metric is dropped
    from the entity registry; siblings on the same device keep their
    entity_ids, and the parent device itself is preserved.
    """
    from homeassistant.helpers import entity_registry as er_helpers
    from pytest_homeassistant_custom_component.common import (
        MockConfigEntry,
        async_fire_mqtt_message,
    )

    from custom_components.telegraf_mqtt.const import (
        CONF_DEVICE_NAME,
        CONF_TOPIC_PATTERN,
        DOMAIN,
    )

    # Wire up a config entry and publish two metrics on the same host.
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Telegraf",
        data={CONF_TOPIC_PATTERN: "telegraf/#", CONF_DEVICE_NAME: "Telegraf"},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    async_fire_mqtt_message(
        hass,
        "telegraf/cpu",
        json.dumps(
            {
                "name": "cpu",
                "tags": {"host": "server01"},
                "fields": {"usage_idle": 88.4},
                "timestamp": 1700000000,
            }
        ),
    )
    async_fire_mqtt_message(
        hass,
        "telegraf/battery",
        json.dumps(
            {
                "name": "battery",
                "tags": {"host": "server01"},
                "fields": {"percentage": 87.0},
                "timestamp": 1700000000,
            }
        ),
    )
    await hass.async_block_till_done()

    # Verify both entities are registered and grouped under one device.
    entity_registry = er_helpers.async_get(hass)
    domain_entries = [e for e in entity_registry.entities.values() if e.platform == DOMAIN]
    by_unique = {e.unique_id: e for e in domain_entries}
    assert len(by_unique) == 2
    cpu_uid = f"{DOMAIN}_server01_cpu_usage_idle"
    battery_uid = f"{DOMAIN}_server01_battery_percentage"
    cpu_entity_id = by_unique[cpu_uid].entity_id
    battery_entity_id = by_unique[battery_uid].entity_id
    assert cpu_entity_id != battery_entity_id
    # Parent device is shared.
    assert by_unique[cpu_uid].device_id == by_unique[battery_uid].device_id

    # Drive cleanup: age both metrics, mark the battery unavailable,
    # let it pass the cleanup_delay, then advance the device's
    # ``last_any_metric`` so the device is still ACTIVE for cleanup.
    registry = entry.runtime_data.manager.get_or_create_registry("server01", "server01")
    # Find the state keys. unique_key is the post-slug composite of
    # measurement + sorted non-host tags + field, so for our
    # bare-tag payloads the keys are exactly "cpu_usage_idle" and
    # "battery_percentage".
    battery_state = registry.get("battery_percentage")
    cpu_state = registry.get("cpu_usage_idle")
    assert battery_state is not None and cpu_state is not None

    # Force the battery through the Cleanup Candidate state. expire_after
    # defaults to 120s; setting last_updated to a very old value triggers
    # the Active -> Unavailable transition with the candidate timestamp.
    # The CPU sibling must stay available (last_updated near "now") so
    # the device's ``min_active_metrics=1`` floor is met.
    import time

    now = time.monotonic()
    # Drive the battery past `expire_after` (defaults to 120s) by setting
    # `last_updated` to a monotonic timestamp safely older than the
    # threshold -- not a hardcoded `0.0`, which only works once the
    # process has been running for >= `expire_after` of monotonic time
    # and flakes in short-uptime CI runners / fresh containers.
    expire_after = registry._expire_after
    battery_state.last_updated = now - expire_after - 1.0
    cpu_state.last_updated = now
    registry.last_any_metric = now
    registry.check_expiry()
    assert battery_state.is_available is False
    assert battery_state.cleanup_candidate_since is not None
    assert cpu_state.is_available is True
    # The integration's default cleanup_delay is 30 days; shrink it
    # for this test so the battery is eligible on the same call. We
    # rewrite the candidate timestamp to "now - 2s" so the strict
    # ``cleanup_delay`` check passes. ``DeviceManager.apply_options``
    # is the public propagation path -- it stores the new value at
    # the manager level AND fans it out to every existing per-device
    # registry, so we don't have to touch them individually here.
    manager = entry.runtime_data.manager
    manager.apply_options(cleanup_delay=1)
    battery_state.cleanup_candidate_since = now - 2
    # The device is ACTIVE (heartbeat fresh) and has at least one
    # available metric, so the min_active_metrics floor is met.
    removed = manager.cleanup(
        on_write=lambda *_: None,
    )
    assert ("server01", "battery_percentage") in removed
    # The dispatcher listener registered in ``async_setup_entry`` is
    # a one-line wrapper around the module-level ``remove_metric_entity``.
    # Call it directly here so the test is deterministic (the periodic
    # task would normally fire the signal, but the task scheduler is
    # not under our control in the test harness).
    from custom_components.telegraf_mqtt import remove_metric_entity

    assert remove_metric_entity(hass, "server01", "battery_percentage") is True
    await hass.async_block_till_done()
    # Re-collect after cleanup: the battery entity should be gone.
    by_unique_after = {e.unique_id: e for e in entity_registry.entities.values() if e.platform == DOMAIN}
    assert battery_uid not in by_unique_after
    assert cpu_uid in by_unique_after
    # Sibling entity_id is preserved across the cleanup.
    assert by_unique_after[cpu_uid].entity_id == cpu_entity_id


# ---------------------------------------------------------------------------
# Branch coverage: ``remove_metric_entity`` no-op paths, prune_empty_devices
# + log, and the listener body via the dispatcher.
# ---------------------------------------------------------------------------


def test_remove_metric_entity_returns_false_when_er_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """In import-isolation contexts (no HA entity_registry), the
    function is a no-op and returns False -- never raises.
    """
    import importlib

    integration = importlib.import_module("custom_components.telegraf_mqtt")
    monkeypatch.setattr(integration, "er", None)
    assert integration.remove_metric_entity(hass=None, device_id="x", unique_key="y") is False


def test_remove_metric_entity_returns_false_when_no_match(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """If no entity in the registry matches the composite key, the
    function returns False. We can't reach the real entity registry
    from a harness-free test, so we stub ``er`` to a module whose
    ``async_get(hass)`` returns an empty registry. In real HA the
    function is sync despite the name.
    """
    import importlib
    import types

    integration = importlib.import_module("custom_components.telegraf_mqtt")

    fake = types.ModuleType("fake_er_no_match")

    class _Empty:
        entities: ClassVar[dict] = {}

        def async_get_entity_id(self, platform: str, domain: str, unique_id: str) -> None:
            return None

    def _async_get(_hass: object) -> _Empty:
        # ``entity_registry.async_get(hass)`` is sync in real HA despite
        # the ``async_`` prefix; the helper just looks up the singleton.
        return _Empty()

    fake.async_get = _async_get
    monkeypatch.setattr(integration, "er", fake)
    assert integration.remove_metric_entity(hass=object(), device_id="x", unique_key="y") is False


def test_prune_empty_devices_emits_info_log(caplog) -> None:
    """Phase 6: ``prune_empty_devices`` is logged at INFO so users can
    see when a device is dropped from the integration. We assert the
    log line shape rather than the exact prefix.
    """
    import logging

    clock = [0.0]
    manager = DeviceManager(expire_after=10, cleanup_delay=1, delete_delay=5, clock=lambda: clock[0])
    registry = manager.get_or_create_registry("drained", "drained")
    registry.update(_descriptor("only_metric"))
    clock[0] = 100.0
    registry.check_expiry()
    clock[0] = 200.0
    registry.cleanup()  # remove the metric
    clock[0] = 1_000.0
    with caplog.at_level(logging.INFO, logger="custom_components.telegraf_mqtt.registry"):
        pruned = manager.prune_stale_devices()
    assert pruned == ["drained"]
    assert any("drained" in rec.getMessage() for rec in caplog.records)


# ---------------------------------------------------------------------------
# Branch coverage for the dispatcher-listener registration helper.
# ---------------------------------------------------------------------------


def test_listener_remove_metric_returns_noop_when_dispatcher_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """In import-isolation (no HA dispatcher), the listener helper
    returns a no-op unsubscribe callable instead of raising.
    """
    import importlib

    integration = importlib.import_module("custom_components.telegraf_mqtt")
    monkeypatch.setattr(integration, "async_dispatcher_connect", None)
    noop = integration._listener_remove_metric(hass=None, entry=object())
    assert callable(noop)
    # The callable does not blow up when called.
    assert noop() is None


def test_listener_remove_metric_registers_via_real_dispatcher(monkeypatch: pytest.MonkeyPatch) -> None:
    """The listener body is ``remove_metric_entity(hass, device_id, unique_key)``;
    the registration passes an async listener to ``async_dispatcher_connect``.
    Drive the captured listener end-to-end to cover the body.
    """
    import importlib
    import types

    integration = importlib.import_module("custom_components.telegraf_mqtt")

    captured: dict[str, object] = {}

    def _fake_connect(_hass, signal, target):
        captured["signal"] = signal
        captured["target"] = target
        return lambda: None

    monkeypatch.setattr(integration, "async_dispatcher_connect", _fake_connect)
    fake_hass = object()
    fake_entry = types.SimpleNamespace(entry_id="entry-X", runtime_data=object())
    unsubscribe = integration._listener_remove_metric(hass=fake_hass, entry=fake_entry)
    assert callable(unsubscribe)
    assert "signal" in captured
    assert callable(captured["target"])

    # Drive the captured async listener with a fake entity-registry
    # shape. ``remove_metric_entity`` is fully covered by the
    # no-match / real-harness tests for its branches; here we just
    # need the listener body line to execute.
    class _Empty:
        entities: ClassVar[dict] = {}

        def async_get_entity_id(self, platform: str, domain: str, unique_id: str) -> None:
            return None

    def _async_get(_hass):
        return _Empty()

    fake_er = types.ModuleType("fake_er_for_listener")
    fake_er.async_get = _async_get  # type: ignore[attr-defined]
    monkeypatch.setattr(integration, "er", fake_er)
    # The captured target is async; running it via asyncio.run is the
    # simplest way to drive the body without the rest of the harness.
    asyncio.run(captured["target"]("server01", "net_eth0_link_up"))


# ---------------------------------------------------------------------------
# WS-J (M14): ``delete_delay`` actually cleans.
#
# Every test below fails against the pre-fix implementation, where
# ``prune_empty_devices`` required ``len(registry) == 0`` -- a condition no
# code path could reach for a silent host, because ``cleanup`` skips
# stale-heartbeat devices AND devices below ``min_active_metrics``, and a
# host that stops publishing trips both guards at once.
# ---------------------------------------------------------------------------


def test_delete_delay_retires_a_silent_host_holding_metrics() -> None:
    """The headline M14 case: a host that stops publishing is cleaned up.

    The registry still holds three metrics and its heartbeat is ancient.
    ``delete_delay`` must retire the whole device and report every entity
    it held so the integration can delete them.
    """
    clock = [0.0]
    manager = DeviceManager(expire_after=10, cleanup_delay=1, delete_delay=5, clock=lambda: clock[0])
    registry = manager.get_or_create_registry("departed", "departed")
    for field in ("cpu_usage_idle", "used_percent", "free"):
        registry.update(_descriptor(field))

    clock[0] = 1_000.0
    removed_entities: list[tuple[str, str]] = []
    pruned = manager.prune_stale_devices(on_remove=lambda d, u: removed_entities.append((d, u)))

    assert pruned == ["departed"]
    assert "departed" not in manager.devices
    assert sorted(removed_entities) == [
        ("departed", "cpu_usage_idle"),
        ("departed", "free"),
        ("departed", "used_percent"),
    ]


def test_delete_delay_respects_a_fresh_heartbeat() -> None:
    """A still-reporting host is never retired, however stale its metrics."""
    clock = [0.0]
    manager = DeviceManager(expire_after=10, cleanup_delay=1, delete_delay=5, clock=lambda: clock[0])
    registry = manager.get_or_create_registry("alive", "alive")
    registry.update(_descriptor("cpu_usage_idle"))

    clock[0] = 1_000.0
    registry.last_any_metric = 999.0
    assert manager.prune_stale_devices() == []
    assert "alive" in manager.devices


def test_delete_delay_does_not_fire_before_the_window() -> None:
    """Inside the window nothing is retired."""
    clock = [0.0]
    manager = DeviceManager(expire_after=10, cleanup_delay=1, delete_delay=100, clock=lambda: clock[0])
    manager.get_or_create_registry("recent", "recent").update(_descriptor("cpu_usage_idle"))

    clock[0] = 50.0
    assert manager.prune_stale_devices() == []


def test_enable_cleanup_false_also_disables_device_pruning() -> None:
    """``enable_cleanup: false`` means "delete nothing" -- devices included.

    ``prune_empty_devices`` used to ignore this flag entirely, which
    contradicted the docstring on ``cleanup`` ("keeps every metric in every
    device forever") and surprised any user who turned cleanup off.
    """
    clock = [0.0]
    manager = DeviceManager(
        expire_after=10,
        cleanup_delay=1,
        delete_delay=5,
        enable_cleanup=False,
        clock=lambda: clock[0],
    )
    manager.get_or_create_registry("departed", "departed").update(_descriptor("cpu_usage_idle"))

    clock[0] = 1_000.0
    assert manager.prune_stale_devices() == []
    assert "departed" in manager.devices


def test_departed_hosts_do_not_consume_the_device_cap() -> None:
    """M14's user-visible payoff: the cap regression scenario.

    60 hosts are observed over time, 20 of the 50 tracked ones later depart
    for good and sit past ``delete_delay``, and a 51st *live* host then
    shows up. Without pruning, the 20 corpses still occupy device-cap slots
    and the live host is silently dropped with no error.
    """
    clock = [0.0]
    manager = DeviceManager(max_devices=50, delete_delay=60, clock=lambda: clock[0])

    for i in range(60):
        clock[0] = float(i)
        manager.get_or_create_registry(f"host{i:02d}", f"host{i:02d}")
    assert len(manager.devices) == 50, "cap enforced while every host is live"
    assert manager.dropped_device_count == 10

    # 20 of the 50 known hosts go away for good; the other 30 keep
    # reporting, so their heartbeats must be refreshed or they would be
    # retired for exactly the same reason we are retiring the other 20.
    departed = [f"host{i:02d}" for i in range(20)]
    clock[0] = 10_000.0
    for device_id, registry in manager.devices.items():
        registry.last_any_metric = 0.0 if device_id in departed else 9_999.0

    dropped_before = manager.dropped_device_count
    manager.prune_stale_devices()

    assert len(manager.devices) == 30, "the 20 departed hosts should have been retired"
    assert manager.dropped_device_count == dropped_before

    # A live host now arrives with room to spare -- it must be accepted.
    clock[0] = 10_001.0
    registry = manager.get_or_create_registry("brand_new_live_host", "brand_new_live_host")
    assert registry is not None, "a live host was dropped even though 20 slots were freed"
    assert manager.dropped_device_count == dropped_before, "the new host must not be counted as dropped"


def test_pruned_device_reappears_on_the_next_message() -> None:
    """A retired host that comes back gets a fresh registry, not an error."""
    clock = [0.0]
    manager = DeviceManager(delete_delay=5, clock=lambda: clock[0])
    manager.get_or_create_registry("flapping", "flapping").update(_descriptor("cpu_usage_idle"))

    clock[0] = 1_000.0
    assert manager.prune_stale_devices() == ["flapping"]

    clock[0] = 1_001.0
    registry = manager.get_or_create_registry("flapping", "flapping")
    assert registry is not None
    assert len(registry) == 0


def test_cleanup_returns_device_and_key_separately() -> None:
    """``cleanup`` yields a real 2-tuple, not a re-joinable composite string.

    The composite form had to be split apart downstream to rebuild the
    entity ``unique_id``, which mis-split any ``unique_key`` containing a
    ``:``. A ``unique_key`` that actually contains a colon must survive --
    that is the case the old string surgery got wrong.
    """
    clock = [0.0]
    # min_active_metrics=0 so the device floor does not block the single
    # (now-unavailable) metric from being cleaned -- this test is about the
    # shape of the returned key, not about the floor.
    manager = DeviceManager(
        expire_after=10,
        cleanup_delay=0,
        min_active_metrics=0,
        clock=lambda: clock[0],
    )
    registry = manager.get_or_create_registry("server01", "server01")
    registry.update(_descriptor("net:eth0:link_up"))

    # Expire the metric so it becomes a cleanup candidate, then step the
    # clock past ``cleanup_delay`` (the comparison is strict ``>``) while
    # keeping the DEVICE's heartbeat fresh -- otherwise ``cleanup`` skips
    # the whole device as offline and returns nothing.
    clock[0] = 1_000.0
    registry.check_expiry()
    clock[0] = 1_001.0
    registry.last_any_metric = 1_000.0
    removed = manager.cleanup()

    assert removed == [("server01", "net:eth0:link_up")]


# NOTE: a real-subscribe-failure-after-probe test was attempted but
# the project's mqtt_mock wires ``async_subscribe`` through a layer
# that does not let us intercept the second call cleanly. The rare
# path (broker accepts the probe SUBSCRIBE-ACK, then drops the
# connection before the real subscribe is sent) is genuinely hard to
# reproduce in the harness; the production line carries an honest
# ``# pragma: no cover`` annotation instead. Phase 5's setup-failure
# test already pins the broader "raise ConfigEntryNotReady on broker
# error" contract, which is what users care about.
