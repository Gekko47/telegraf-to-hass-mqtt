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

import asyncio
import enum
import json
from collections.abc import Callable
from typing import Any

from custom_components.telegraf_mqtt.const import SIGNAL_METRIC_UPDATED
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


class _CountingDispatcher:
    """A minimal HA-dispatcher stand-in that counts listener INVOCATIONS.

    The other tests in this file count ``on_write``, which is the number of
    *state writes*. That number is a property of the registry's
    change-detection and is exactly what the parallel-updates claim is
    about -- which is also why it is structurally blind to the O(N^2)
    dispatcher fan-out (H2): the fan-out was all no-op callbacks that
    never reached an ``on_write``.

    This counter is the other side of the ledger. HA's dispatcher is a flat
    list, so a per-entity subscription means every dispatch walks every
    listener; counting them is the only way to see that cost.
    """

    def __init__(self) -> None:
        # Per-signal, exactly like HA: the three signals this integration
        # uses are independent fan-out lists, and only one of them is the
        # hot path.
        self.by_signal: dict[str, list[Callable[[str], None]]] = {}
        self.invocations: int = 0

    def connect(self, signal: str, target: Callable[[str], None]) -> Callable[[], None]:
        self.by_signal.setdefault(signal, []).append(target)
        return lambda: None

    def count(self, signal: str) -> int:
        return len(self.by_signal.get(signal, []))

    def send(self, signal: str, metric_key: str) -> None:
        for target in self.by_signal.get(signal, []):
            self.invocations += 1
            target(metric_key)


def _counting_platform(module_name: str, dispatcher: _CountingDispatcher) -> Any:
    """Drive a platform's ``async_setup_entry`` against the counting dispatcher.

    Returns ``(manager, add_entities)`` so a test can publish real payloads
    and observe the invocation count, with the real entity objects
    (against stubbed Home Assistant bases, so no event loop is needed).
    """
    import importlib
    import sys
    import types
    from dataclasses import dataclass

    class StubEntity:
        def async_write_ha_state(self) -> None:
            self.write_count = getattr(self, "write_count", 0) + 1

        def async_on_remove(self, remove_callback: Any) -> None:
            self.remove_callback = remove_callback

    class UnitOfTemperature:
        CELSIUS = "°C"

    class StubEntityCategory(enum.StrEnum):
        CONFIG = "config"
        DIAGNOSTIC = "diagnostic"

    def _callback(func: Any) -> Any:
        return func

    components = types.ModuleType("homeassistant.components")
    sensor = types.ModuleType("homeassistant.components.sensor")
    binary_sensor = types.ModuleType("homeassistant.components.binary_sensor")
    config_entries = types.ModuleType("homeassistant.config_entries")
    const = types.ModuleType("homeassistant.const")
    core = types.ModuleType("homeassistant.core")
    device_registry = types.ModuleType("homeassistant.helpers.device_registry")
    dispatcher_mod = types.ModuleType("homeassistant.helpers.dispatcher")
    entity_platform = types.ModuleType("homeassistant.helpers.entity_platform")
    helpers = types.ModuleType("homeassistant.helpers")
    entity_helpers = types.ModuleType("homeassistant.helpers.entity")

    class SensorDeviceClass(enum.StrEnum):
        TEMPERATURE = "temperature"
        POWER = "power"
        ENERGY = "energy"

    class SensorStateClass(enum.StrEnum):
        MEASUREMENT = "measurement"
        TOTAL = "total"
        TOTAL_INCREASING = "total_increasing"

    sensor.SensorEntity = StubEntity
    binary_sensor.BinarySensorEntity = StubEntity
    config_entries.ConfigEntry = object
    const.UnitOfTemperature = UnitOfTemperature
    const.EntityCategory = StubEntityCategory
    core.HomeAssistant = object
    core.callback = _callback
    device_registry.DeviceInfo = dict
    dispatcher_mod.async_dispatcher_connect = lambda _h, signal, target: dispatcher.connect(signal, target)
    dispatcher_mod.async_dispatcher_send = lambda _h, signal, *args: dispatcher.send(signal, *args)
    entity_platform.AddEntitiesCallback = object
    entity_helpers.EntityCategory = StubEntityCategory
    sensor.SensorDeviceClass = SensorDeviceClass
    sensor.SensorStateClass = SensorStateClass

    saved = {
        name: sys.modules.get(name)
        for name in (
            "homeassistant.components",
            "homeassistant.components.sensor",
            "homeassistant.components.binary_sensor",
            "homeassistant.config_entries",
            "homeassistant.const",
            "homeassistant.core",
            "homeassistant.helpers",
            "homeassistant.helpers.device_registry",
            "homeassistant.helpers.dispatcher",
            "homeassistant.helpers.entity",
            "homeassistant.helpers.entity_platform",
        )
    }
    sys.modules.update(
        {
            "homeassistant.components": components,
            "homeassistant.components.sensor": sensor,
            "homeassistant.components.binary_sensor": binary_sensor,
            "homeassistant.config_entries": config_entries,
            "homeassistant.const": const,
            "homeassistant.core": core,
            "homeassistant.helpers": helpers,
            "homeassistant.helpers.device_registry": device_registry,
            "homeassistant.helpers.dispatcher": dispatcher_mod,
            "homeassistant.helpers.entity": entity_helpers,
            "homeassistant.helpers.entity_platform": entity_platform,
        }
    )

    @dataclass
    class RuntimeData:
        manager: Any
        manufacturer: str | None = None
        model: str | None = None
        sw_version: str | None = None

    @dataclass
    class Entry:
        runtime_data: Any
        entry_id: str = "entry-1"

        def async_on_unload(self, _cb: Any) -> None:
            return None

    try:
        sys.modules.pop(f"custom_components.telegraf_mqtt.{module_name}", None)
        module = importlib.import_module(f"custom_components.telegraf_mqtt.{module_name}")
        manager = DeviceManager()
        manager.set_parser(TelegrafParser())
        # Mirror ``__init__.py``: the manager's on_write fans out as a
        # SIGNAL_METRIC_UPDATED dispatch. Wiring it AFTER setup is what
        # production does, and it is what makes the count meaningful.
        updated_signal = SIGNAL_METRIC_UPDATED.format(entry_id="entry-1")
        manager.set_callbacks(
            on_write=lambda key, _available, _value: dispatcher.send(updated_signal, key),
        )
        added: list[Any] = []
        entry = Entry(runtime_data=RuntimeData(manager=manager))
        asyncio.run(module.async_setup_entry(object(), entry, added.extend))
        return manager, added, updated_signal
    finally:
        for name, original in saved.items():
            if original is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = original
        sys.modules.pop(f"custom_components.telegraf_mqtt.{module_name}", None)


def test_dispatcher_fanout_is_linear_not_quadratic() -> None:
    """H2: one listener per PLATFORM, not one per entity (WS-E3).

    Before: every entity connected to the entry-wide
    ``SIGNAL_METRIC_UPDATED`` in its own ``async_added_to_hass``, so HA's
    flat dispatcher invoked all N listeners for each of the M changed
    fields in a single message -- N x M invocations for N entities.
    After: one listener per platform, so the count is 1 per dispatch,
    independent of N.

    The assertion is on the *ratio*, not an absolute number, because the
    absolute count scales with the fleet size and would need re-tuning for
    every cap change. What must not change is that adding entities does
    not multiply the per-message cost.
    """
    dispatcher = _CountingDispatcher()
    manager, added, updated_signal = _counting_platform("sensor", dispatcher)

    devices = ["host-a", "host-b", "host-c", "host-d"]
    # Populate the registry so the platform adopts a real entity per key.
    for device in devices:
        for index in range(5):
            topic, payload = _message(device, "mem", f"used_percent_{index}", float(index))
            manager.process_message(topic, payload)
    # 4 devices x 5 fields = 20 live entities on this platform.
    assert len(added) == 20
    # Each of the 20 registry writes dispatched once at setup adoption time.
    dispatcher.invocations = 0
    listeners = dispatcher.count(updated_signal)

    # Now publish one multi-field message. Pre-WS-E3 the 20 live entities
    # would each have been a listener, so this single message would have
    # cost 5 dispatches x 20 listeners = 100 invocations.
    topic, payload = _message("host-a", "mem", "used_percent_0", 999.0)
    manager.process_message(topic, payload)
    after_single = dispatcher.invocations
    manager.process_message(*_message("host-a", "mem", "used_percent_1", 998.0))

    assert listeners == 1, f"expected one platform-level listener, got {listeners}"
    # Exactly one changed field -> exactly one dispatch -> one invocation.
    assert after_single == 1, (
        f"one changed field produced {after_single} listener invocations; the fan-out is still per-entity"
    )
    # ...and it does not grow with the number of entities the platform owns.
    assert dispatcher.invocations == 2


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
            for i, _metric in enumerate(metrics):
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
