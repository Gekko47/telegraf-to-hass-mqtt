"""Phase 8 performance harness: parallel-updates without write pressure.

ROADMAP.md Phase 8 (parallel-updates): the integration must sustain a simulated
high-frequency metric stream (100+ entities at ~1 Hz per device) WITHOUT calling
``async_write_ha_state()`` on every message. The registry's ``on_write`` hook is
the exact boundary that maps to ``async_write_ha_state()`` in the platform code
(``_handle_metric_updated``). If ``on_write`` fires only on *real* value or
availability changes, the entity layer writes are bounded.

Harness-free (AGENTS.md): the registry and parser are exercised directly with a
counting ``on_write``; no HA event loop, no entity objects, no timing flakiness.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from custom_components.telegraf_mqtt.parser import TelegrafParser
from custom_components.telegraf_mqtt.registry import DeviceManager


class _WriteCounter:
    """Counts every ``on_write`` (i.e. every would-be ``async_write_ha_state``)."""

    def __init__(self) -> None:
        self.calls: int = 0
        self.changes: int = 0
        self.last_topic: str | None = None

    def __call__(self, metric_key: str, available: bool, value: Any) -> None:
        self.calls += 1
        if available:
            self.changes += 1


def _build_manager(write_counter: _WriteCounter) -> DeviceManager:
    manager = DeviceManager(expire_after=60)
    manager.set_parser(TelegrafParser())
    manager.set_callbacks(on_write=write_counter)
    return manager


def _message(device: str, measurement: str, field: str, value: float) -> tuple[str, str]:
    topic = f"telegraf/{device}/{measurement}"
    payload = json.dumps(
        {
            "name": measurement,
            "tags": {"host": device},
            "fields": {field: value},
            "timestamp": 1700000000,
        }
    )
    return topic, payload


def test_sustains_100_entities_at_1hz_with_bounded_writes() -> None:
    """Drive 100 metrics across 4 devices for 10 time-steps (~1 Hz simulated),
    varying values so each metric really changes each tick. ``on_write`` (and
    therefore ``async_write_ha_state``) must fire exactly once per real change --
    never more, and never on a no-op refresh.
    """
    counter = _WriteCounter()
    manager = _build_manager(counter)

    devices = ["host-a", "host-b", "host-c", "host-d"]
    metrics = [f"mem_used_percent_{i}" for i in range(25)]  # 25/device * 4 = 100

    for step in range(10):
        for device in devices:
            for i, metric in enumerate(metrics):
                value = float(step * 100 + i)  # unique value per tick -> real change
                topic, payload = _message(device, "mem", f"used_percent_{i}", value)
                manager.process_message(topic, payload)

    # 100 metrics, each written on its first discovery + on 9 further value
    # changes = 100 * 10 writes exactly (1 'write' = 1 real change, no extra).
    expected_writes = 100 * 10
    assert counter.calls == expected_writes
    assert counter.changes == expected_writes


def test_repeated_identical_values_produce_no_extra_writes() -> None:
    """When the same value repeats on consecutive ticks, the registry detects
    the no-op and suppresses the write -- the core of the parallel-updates claim.
    """
    counter = _WriteCounter()
    manager = _build_manager(counter)

    metric = "mem_used_percent_0"
    topic, payload = _message("host-a", "mem", "used_percent_0", 42.0)
    for _ in range(50):
        manager.process_message(topic, payload)  # identical payload, 50 times

    # The very first message discovers the metric (1 write); the other 49
    # identical messages change nothing and must not write.
    assert counter.calls == 1


def test_expiry_transitions_write_sparingly() -> None:
    """Availability transitions write once per change: going unavailable and
    the recovery each write, but many repeated expiry ticks after the transition
    do not add writes (the count stays at discovery + 1 transition)."""
    counter = _WriteCounter()
    clock = [1000.0]
    manager = DeviceManager(expire_after=5, clock=lambda: clock[0], device_name="T")
    manager.set_parser(TelegrafParser())
    manager.set_callbacks(on_write=counter)

    topic, payload = _message("host-a", "mem", "used_percent_0", 42.0)
    manager.process_message(topic, payload)  # 1 write (discovery)

    # Advance the clock far past expire_after and run expiry many times.
    # A single transition must flip availability, and every subsequent tick is
    # a no-op -- so 50 identical expiry ticks write exactly one transition.
    clock[0] = 1006.0
    for _ in range(50):
        manager.check_expiry(on_write=counter)

    assert counter.calls == 2  # 1 discovery + 1 unavailable transition
    # ``host-a`` normalises to a digest-suffixed slug; look the metric up by
    # the actual composite key the manager produced.
    (composite_key,) = manager.keys()
    state = manager.get_metric(composite_key)
    assert state is not None and state.is_available is False

    # Re-sending the same metric flips it back to available: recovery is one
    # edge-triggered write (calls == 3: discovery + unavailable + recovery),
    # and re-sending the identical value a second time is a no-op refresh
    # that must not add a write -- so the count stays at 3.
    manager.process_message(topic, payload)
    assert counter.calls == 3
    state = manager.get_metric(composite_key)
    assert state is not None and state.is_available is True

    manager.process_message(topic, payload)
    assert counter.calls == 3
