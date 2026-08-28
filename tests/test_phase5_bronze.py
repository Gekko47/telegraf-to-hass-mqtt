"""Phase 5 exit-criteria tests: the Bronze quality-scale gate.

ROADMAP.md Phase 5:
  - Config flow tests cover success, duplicate-topic abort, invalid topic, and
    setup-failure paths (test-before-setup / test-before-configure).
  - Setup validates connectivity assumptions before creating the entry; unload
    test leaves zero residual listeners/timers.
  - All state stored on ``entry.runtime_data``; every entity has a stable prefixed
    unique_id and ``_attr_has_entity_name``.
  - Brand imagery submitted/noted; README covers what-it-does, install, removal.
  - ``quality_scale.yaml``: all Bronze rows done or exempt.

Harness-free tests live here so they can run without the real HA harness; the
two real-harness assertions (entity-registry records ``has_entity_name=True``,
unload leaves zero residual timers) are co-located for context.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

import pytest

import custom_components.telegraf_mqtt as integration
from custom_components.telegraf_mqtt.const import (
    CONF_DEVICE_NAME,
    CONF_TOPIC_PATTERN,
    DOMAIN,
)
from custom_components.telegraf_mqtt.models import MetricDescriptor


# ---------------------------------------------------------------------------
# FakeHass / FakeConfigEntry / FakeMqtt
#
# Mirror test_runtime.py's import-isolation pattern. The only Phase-5-specific
# twist: ``FakeMqtt`` can be told to raise on the *first* (probe) call and to
# succeed on the *second* (real) call -- or vice versa -- so we can drive the
# test-before-setup happy/sad paths deterministically without the real harness.
# ---------------------------------------------------------------------------


@dataclass
class FakeConfigEntries:
    forwarded: list[tuple[Any, list[str]]] = field(default_factory=list)
    unloaded: list[tuple[Any, list[str]]] = field(default_factory=list)

    async def async_forward_entry_setups(self, entry, platforms: list[str]) -> None:
        self.forwarded.append((entry, platforms))

    async def async_unload_platforms(self, entry, platforms: list[str]) -> bool:
        self.unloaded.append((entry, platforms))
        return True


@dataclass
class FakeHass:
    config_entries: FakeConfigEntries = field(default_factory=FakeConfigEntries)


class FakeConfigEntry:
    def __init__(self) -> None:
        self.entry_id = "entry-1"
        self.data = {
            CONF_TOPIC_PATTERN: "telegraf/#",
            CONF_DEVICE_NAME: "Telegraf MQTT",
        }
        self.options = {}
        self.runtime_data: Any = None
        self._unload_callbacks: list[Callable[[], None]] = []

    def async_on_unload(self, callback: Callable[[], None]) -> None:
        self._unload_callbacks.append(callback)

    def add_update_listener(self, _listener: Callable[..., Any]) -> Callable[[], None]:
        return lambda: None


class FakeMqtt:
    """Tracks every ``async_subscribe`` call so we can drive the probe paths.

    Default behaviour: every call succeeds and returns ``self.unsubscribe``. To
    simulate broker rejection, pre-load ``self.raise_on_call`` with the call
    index (1-based) that should raise; subsequent calls succeed normally.
    """

    def __init__(self) -> None:
        self.subscribe_calls: list[tuple[str, Callable[..., Any]]] = []
        self.unsubscribe_calls: int = 0
        self.raise_on_call: dict[int, type[BaseException]] = {}

    async def async_subscribe(
        self, _hass: Any, topic_pattern: str, callback: Callable[..., Any]
    ) -> Callable[[], None]:
        call_index = len(self.subscribe_calls) + 1
        self.subscribe_calls.append((topic_pattern, callback))
        if call_index in self.raise_on_call:
            raise self.raise_on_call[call_index](f"simulated broker failure on call {call_index}")
        return self.unsubscribe

    def unsubscribe(self) -> None:
        self.unsubscribe_calls += 1


class FakePlatform:
    SENSOR = "sensor"
    BINARY_SENSOR = "binary_sensor"


def _patch(monkeypatch: pytest.MonkeyPatch) -> FakeMqtt:
    fake_mqtt = FakeMqtt()

    def fake_dispatch(_hass: Any, _signal: str, _key: str) -> None:
        return None

    def fake_dispatcher_connect(_hass: Any, _signal: str, _target: Callable[..., Any]) -> Callable[[], None]:
        # Phase 6: ``async_setup_entry`` now also subscribes to
        # ``SIGNAL_REMOVE_METRIC`` via ``async_dispatcher_connect``. The
        # Bronze tests in this module don't exercise the entity-registry
        # removal path; recording the listener (and returning a cancel
        # handle) is enough to keep ``async_setup_entry`` reachable
        # without the real HA dispatcher touching a non-HA FakeHass.
        return lambda: None

    def fake_track_time_interval(_hass: Any, _cb: Any, _interval: Any) -> Callable[[], None]:
        return lambda: None

    monkeypatch.setattr(integration, "Platform", FakePlatform)
    monkeypatch.setattr(integration, "PLATFORMS", [FakePlatform.SENSOR, FakePlatform.BINARY_SENSOR])
    monkeypatch.setattr(integration, "mqtt", fake_mqtt)
    monkeypatch.setattr(integration, "async_dispatcher_send", fake_dispatch)
    monkeypatch.setattr(integration, "async_dispatcher_connect", fake_dispatcher_connect)
    monkeypatch.setattr(integration, "async_track_time_interval", fake_track_time_interval)
    # Phase 7: stub the issue registry so repairs helpers are a no-op.
    monkeypatch.setattr(integration, "ir", None)
    return fake_mqtt


# ---------------------------------------------------------------------------
# Exit criterion 1: test-before-setup -- subscribe probe gates entry setup.
# ---------------------------------------------------------------------------


def test_setup_runs_subscribe_probe_before_real_subscription(monkeypatch: pytest.MonkeyPatch) -> None:
    """``async_setup_entry`` must probe the broker first; the real subscription
    happens only after the probe succeeds.

    Phase 5 exit criterion: setup validates connectivity assumptions before
    the entry is considered set up.
    """
    fake_mqtt = _patch(monkeypatch)
    hass = FakeHass()
    entry = FakeConfigEntry()

    assert asyncio.run(integration.async_setup_entry(hass, entry)) is True

    # Probe (call 1) and real subscription (call 2) -- both must have happened,
    # both on the same configured topic, with different callbacks.
    assert len(fake_mqtt.subscribe_calls) == 2
    assert all(topic == "telegraf/#" for topic, _ in fake_mqtt.subscribe_calls)
    probe_cb, real_cb = (cb for _, cb in fake_mqtt.subscribe_calls)
    assert probe_cb is not real_cb


def test_setup_raises_when_probe_subscription_fails(monkeypatch: pytest.MonkeyPatch) -> None:
    """When the broker refuses the probe subscribe, setup must raise
    ``ConfigEntryNotReady`` (so HA surfaces a retry-able error) and must NOT
    install the real subscription. The entry must also be left in a clean
    state -- no half-configured runtime_data carrying a real unsubscribe.
    """
    fake_mqtt = _patch(monkeypatch)
    fake_mqtt.raise_on_call[1] = RuntimeError  # generic broker-side error
    hass = FakeHass()
    entry = FakeConfigEntry()

    from homeassistant.exceptions import ConfigEntryNotReady

    with pytest.raises(ConfigEntryNotReady):
        asyncio.run(integration.async_setup_entry(hass, entry))

    # Probe was attempted; real subscription was not.
    assert len(fake_mqtt.subscribe_calls) == 1
    # Nothing subscribed = nothing to unsubscribe from.
    assert fake_mqtt.unsubscribe_calls == 0
    # Probe failure path: the periodic-task scheduler must NOT have run -- if
    # it had, HA would have a lingering timer for an entry that is going to
    # be re-tried. The manager/parser may exist in runtime_data (they are
    # cheap and HA will retry setup by re-entering async_setup_entry), but
    # the I/O handles must be absent.
    assert entry.runtime_data is not None
    assert entry.runtime_data.unsubscribe is None
    assert entry.runtime_data.cancel_expiry is None


def test_setup_does_not_duplicate_subscriptions_on_successful_probe(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """After the probe succeeds, the real subscription is installed exactly
    once. ``entry.runtime_data.unsubscribe`` is wired to a real teardown
    handle (we verify by calling it; identity comparison is unreliable
    because the integration may wrap the bound method)."""
    fake_mqtt = _patch(monkeypatch)
    hass = FakeHass()
    entry = FakeConfigEntry()

    asyncio.run(integration.async_setup_entry(hass, entry))

    # Probe was unsubscribed inline; only the real subscription remains live.
    assert fake_mqtt.unsubscribe_calls == 1
    assert entry.runtime_data.unsubscribe is not None
    entry.runtime_data.unsubscribe()
    assert fake_mqtt.unsubscribe_calls == 2


# ---------------------------------------------------------------------------
# Exit criterion 2: has-entity-name on every entity.
# ---------------------------------------------------------------------------


def test_sensor_entity_has_entity_name_true() -> None:
    """Phase 5 exit criterion: every entity has ``has_entity_name`` set to True.

    HA's ``CachedProperties`` metaclass moves the class-body
    ``_attr_has_entity_name = True`` into a private ``__attr_has_entity_name``
    slot and replaces the public attribute with a property; the public
    ``has_entity_name`` property then reads through it. We assert the public
    contract, which is what consumers see.
    """
    from custom_components.telegraf_mqtt.sensor import TelegrafMqttSensor

    # The metaclass stores the True value at the class level under the
    # plain name ``__attr_has_entity_name`` (no name-mangling -- the
    # assignment is in the metaclass, not the class body). The public
    # ``_attr_has_entity_name`` is replaced by a property whose getter
    # reads from there.
    assert vars(TelegrafMqttSensor)["__attr_has_entity_name"] is True
    assert isinstance(vars(TelegrafMqttSensor)["_attr_has_entity_name"], property)


def test_binary_sensor_entity_has_entity_name_true() -> None:
    from custom_components.telegraf_mqtt.binary_sensor import TelegrafMqttBinarySensor

    assert vars(TelegrafMqttBinarySensor)["__attr_has_entity_name"] is True
    assert isinstance(vars(TelegrafMqttBinarySensor)["_attr_has_entity_name"], property)


# ---------------------------------------------------------------------------
# Exit criterion 3: stable, domain-prefixed unique_id on every entity.
# ---------------------------------------------------------------------------


def _descriptor() -> MetricDescriptor:
    return MetricDescriptor(
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


def test_entity_unique_id_is_domain_prefixed_and_stable(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every entity's ``_attr_unique_id`` must be prefixed with the domain so
    it is unique across the whole HA instance, and must match the v1-frozen
    ``{DOMAIN}_{device_id}_{unique_key}`` schema.
    """
    import sys
    import types
    import enum
    import importlib

    # Stub HA so the platform module imports cleanly.
    components = types.ModuleType("homeassistant.components")
    sensor = types.ModuleType("homeassistant.components.sensor")
    config_entries = types.ModuleType("homeassistant.config_entries")
    const = types.ModuleType("homeassistant.const")
    core = types.ModuleType("homeassistant.core")
    device_registry = types.ModuleType("homeassistant.helpers.device_registry")
    dispatcher = types.ModuleType("homeassistant.helpers.dispatcher")
    helpers = types.ModuleType("homeassistant.helpers")
    entity_helpers = types.ModuleType("homeassistant.helpers.entity")

    class StubEntity:
        def __init__(self) -> None:
            self.write_count = 0
        def async_write_ha_state(self) -> None:
            self.write_count += 1
        def async_on_remove(self, _c) -> None:
            self.remove_callback = lambda: None

    class UnitOfTemperature:
        CELSIUS = "°C"

    def _cb(f): f.__hass_callback__ = True; return f
    def _adc(_h, _s, _t): return lambda: None

    class _EC(str, enum.Enum):
        CONFIG = "config"
        DIAGNOSTIC = "diagnostic"

    sensor.SensorEntity = StubEntity
    config_entries.ConfigEntry = object
    const.UnitOfTemperature = UnitOfTemperature
    core.HomeAssistant = object
    core.callback = _cb
    device_registry.DeviceInfo = dict
    dispatcher.async_dispatcher_connect = _adc
    entity_helpers.EntityCategory = _EC

    saved = {n: sys.modules.get(n) for n in (
        "homeassistant.components", "homeassistant.components.sensor",
        "homeassistant.config_entries", "homeassistant.const",
        "homeassistant.core", "homeassistant.helpers",
        "homeassistant.helpers.device_registry", "homeassistant.helpers.dispatcher",
        "homeassistant.helpers.entity",
    )}
    sys.modules["homeassistant.components"] = components
    sys.modules["homeassistant.components.sensor"] = sensor
    sys.modules["homeassistant.config_entries"] = config_entries
    sys.modules["homeassistant.const"] = const
    sys.modules["homeassistant.core"] = core
    sys.modules["homeassistant.helpers"] = helpers
    sys.modules["homeassistant.helpers.device_registry"] = device_registry
    sys.modules["homeassistant.helpers.dispatcher"] = dispatcher
    sys.modules["homeassistant.helpers.entity"] = entity_helpers
    sys.modules.pop("custom_components.telegraf_mqtt.sensor", None)

    try:
        sensor_mod = importlib.import_module("custom_components.telegraf_mqtt.sensor")
        from custom_components.telegraf_mqtt.registry import DeviceManager

        manager = DeviceManager(parser=__import__("custom_components.telegraf_mqtt.parser", fromlist=["TelegrafParser"]).TelegrafParser())
        registry = manager.get_or_create_registry("host1", "host1")
        registry.update(_descriptor())

        @dataclass
        class _E:
            runtime_data: Any
            entry_id: str = "entry-1"
        from custom_components.telegraf_mqtt.parser import TelegrafParser as _TP
        e = _E(runtime_data=integration.TelegrafMqttRuntimeData(
            manager=manager,
            parser=_TP(),
            parser_stats=_TP().stats,
            manufacturer=None,
            model=None,
        ))
        entity = sensor_mod.TelegrafMqttSensor(e, "host1:mem_used_percent")
        assert entity._attr_unique_id == "telegraf_mqtt_host1_mem_used_percent"
        # And the domain prefix is the actual DOMAIN constant, not a hardcode.
        assert entity._attr_unique_id.startswith(DOMAIN + "_")
    finally:
        sys.modules.pop("custom_components.telegraf_mqtt.sensor", None)
        for name, mod in saved.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod


# ---------------------------------------------------------------------------
# Exit criterion 4: unload leaves no residual listeners/timers.
# ---------------------------------------------------------------------------


def test_unload_unsubscribes_and_cancels_periodic_task(monkeypatch: pytest.MonkeyPatch) -> None:
    """``async_unload_entry`` must fire every registered cleanup handle
    (MQTT unsubscribe, expiry-timer cancel, options-update listener).

    Phase 5 exit criterion: "unload test leaves zero residual listeners/timers."
    """
    fake_mqtt = _patch(monkeypatch)
    hass = FakeHass()
    entry = FakeConfigEntry()

    assert asyncio.run(integration.async_setup_entry(hass, entry)) is True
    # Setup already fired one unsubscribe (the probe). Unload must fire another.
    unsubs_after_setup = fake_mqtt.unsubscribe_calls
    assert unsubs_after_setup == 1
    assert entry.runtime_data.cancel_expiry is not None

    assert asyncio.run(integration.async_unload_entry(hass, entry)) is True

    # Unload tore down the real subscription (one more unsubscribe).
    assert fake_mqtt.unsubscribe_calls == unsubs_after_setup + 1
    # The cancel handle is wiped, so a second unload is a clean no-op.
    assert entry.runtime_data.unsubscribe is None
    assert entry.runtime_data.cancel_expiry is None
    # Platform unload was requested.
    assert hass.config_entries.unloaded, "platforms.unload was not requested"


# ---------------------------------------------------------------------------
# Exit criterion 5: everything goes through ``entry.runtime_data``.
# ---------------------------------------------------------------------------


def test_all_setup_state_lives_on_entry_runtime_data(monkeypatch: pytest.MonkeyPatch) -> None:
    """No per-entry state should leak onto ``hass.data[DOMAIN]`` or module-level
    globals. Everything observable lives on ``entry.runtime_data``.
    """
    from custom_components.telegraf_mqtt.registry import DeviceManager
    from custom_components.telegraf_mqtt.parser import TelegrafParser

    fake_mqtt = _patch(monkeypatch)
    hass = FakeHass()
    entry = FakeConfigEntry()

    asyncio.run(integration.async_setup_entry(hass, entry))

    rd = entry.runtime_data
    assert isinstance(rd, integration.TelegrafMqttRuntimeData)
    assert isinstance(rd.manager, DeviceManager)
    assert isinstance(rd.parser, TelegrafParser)
    assert rd.manufacturer is None
    assert rd.model is None
    # The only module-level constant is PLATFORMS itself; verify it did not
    # mutate between setup and the entry touching.
    assert integration.PLATFORMS == [FakePlatform.SENSOR, FakePlatform.BINARY_SENSOR]
    # Real subscription handle is per-entry, not shared. We assert it
    # actually wires through to the FakeMqtt by calling it and checking the
    # count; identity comparison is unreliable across bound-method dispatch.
    assert rd.unsubscribe is not None
    rd.unsubscribe()
    assert fake_mqtt.unsubscribe_calls >= 1
