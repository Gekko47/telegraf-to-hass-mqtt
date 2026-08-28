"""Phase 8 exit-criteria tests: the Silver quality-scale gate.

ROADMAP.md Phase 8 (Silver + reliability hardening):
  - Reload/unload cycles leak nothing; errors recover without log spam
  - Entities going unavailable log once per transition, at INFO or below
  - Performance target met without excessive ``async_write_ha_state()``
  - >=90% coverage on parsers/, registry.py, naming.py, config_flow.py, platforms;
    suite-wide floor documented (see tests/test_phase8_performance.py for the
    performance harness)
  - CODEOWNERS/ownership recorded; options documented in README
  - ``quality_scale.yaml``: all Silver rows done or exempt

Malformed-payload / transition-logging / error-path tests are harness-free
(registry + parser stay importable without HA, per AGENTS.md). The
unload/reload and broker-reconnect cycles use the real MQTT subscription
callback so we prove the integration tolerates a subscribe/drop cycle without
reimplementing HA's reconnect logic.
"""

from __future__ import annotations

import asyncio
import json
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
from custom_components.telegraf_mqtt.parser import ParserStats, TelegrafParser
from custom_components.telegraf_mqtt.registry import DeviceManager, MetricRegistry


# ---------------------------------------------------------------------------
# FakeHass / FakeConfigEntry / FakeMqtt / _patch
#
# Import-isolation fakes mirroring test_phase5_bronze.py, so the unload/reload
# and reconnect cycle tests can run without the real HA harness.
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
    """Tracks every ``async_subscribe`` call so callbacks can be re-driven."""

    def __init__(self) -> None:
        self.subscribe_calls: list[tuple[str, Callable[..., Any]]] = []
        self.unsubscribe_calls: int = 0

    async def async_subscribe(
        self, _hass: Any, topic_pattern: str, callback: Callable[..., Any]
    ) -> Callable[[], None]:
        self.subscribe_calls.append((topic_pattern, callback))
        return self.unsubscribe

    def unsubscribe(self) -> None:
        self.unsubscribe_calls += 1


class FakePlatform:
    SENSOR = "sensor"
    BINARY_SENSOR = "binary_sensor"


def _patch(monkeypatch: pytest.MonkeyPatch) -> FakeMqtt:
    fake_mqtt = FakeMqtt()

    def fake_dispatch(_hass: Any, _signal: str, *_args: Any) -> None:
        # ``async_dispatcher_send`` is called with a variable-length payload
        # (2-4 args): metric key for updates, and device_id+device_name for the
        # new-device signal. The fake must accept them all.
        return None

    def fake_dispatcher_connect(_hass: Any, _signal: str, _target: Callable[..., Any]) -> Callable[[], None]:
        return lambda: None

    def fake_track_time_interval(_hass: Any, _cb: Any, _interval: Any) -> Callable[[], None]:
        return lambda: None

    monkeypatch.setattr(integration, "Platform", FakePlatform)
    monkeypatch.setattr(integration, "PLATFORMS", [FakePlatform.SENSOR, FakePlatform.BINARY_SENSOR])
    monkeypatch.setattr(integration, "mqtt", fake_mqtt)
    monkeypatch.setattr(integration, "async_dispatcher_send", fake_dispatch)
    monkeypatch.setattr(integration, "async_dispatcher_connect", fake_dispatcher_connect)
    monkeypatch.setattr(integration, "async_track_time_interval", fake_track_time_interval)
    monkeypatch.setattr(integration, "ir", None)
    return fake_mqtt


def _descriptor(unique_key: str, value: Any = 1.0, measurement: str = "mem") -> MetricDescriptor:
    return MetricDescriptor(
        unique_key=unique_key,
        measurement=measurement,
        tags={"host": "host1"},
        field="used_percent",
        value=value,
        timestamp=1721664000,
        name="Mem Used Percent",
        native_unit=None,
        suggested_device_class=None,
        suggested_state_class="measurement",
        entity_category=None,
        cleanup_policy="AUTO",
    )


# ---------------------------------------------------------------------------
# log-when-unavailable: edge-triggered transition logging (no spam)
# ---------------------------------------------------------------------------


def test_unavailable_transition_logs_once(caplog: pytest.LogCaptureFixture) -> None:
    """The Active->Unavailable transition emits exactly one log line,
    at DEBUG (INFO or below), naming the metric and device."""
    clock = [100.0]
    registry = MetricRegistry(expire_after=5, clock=lambda: clock[0], device_name="Host One")
    registry.update(_descriptor("mem_host1_used_percent"))

    with caplog.at_level("DEBUG", logger="custom_components.telegraf_mqtt.registry"):
        clock[0] = 106.0
        registry.check_expiry()  # transition
        registry.check_expiry()  # already unavailable -> no new log
        registry.check_expiry()  # still unavailable -> no new log

    matches = [r for r in caplog.records if "went unavailable" in (r.getMessage() or "")]
    assert len(matches) == 1
    assert "mem_host1_used_percent" in matches[0].getMessage()
    assert "Host One" in matches[0].getMessage()
    assert matches[0].levelno == 10  # logging.DEBUG


def test_unavailable_recovery_logs_once(caplog: pytest.LogCaptureFixture) -> None:
    """Recovery (unavailable -> available) emits exactly one log line at DEBUG."""
    clock = [100.0]
    registry = MetricRegistry(expire_after=5, clock=lambda: clock[0], device_name="Host One")
    registry.update(_descriptor("mem_host1_used_percent", value=1.0))
    clock[0] = 106.0
    registry.check_expiry()
    assert registry.get("mem_host1_used_percent").is_available is False

    with caplog.at_level("DEBUG", logger="custom_components.telegraf_mqtt.registry"):
        registry.update(_descriptor("mem_host1_used_percent", value=2.0))  # recovery
        # A second update with no value/availability change must not re-log.
        registry.update(_descriptor("mem_host1_used_percent", value=2.0))

    matches = [r for r in caplog.records if "is available again" in (r.getMessage() or "")]
    assert len(matches) == 1
    assert "mem_host1_used_percent" in matches[0].getMessage()
    assert "Host One" in matches[0].getMessage()
    assert matches[0].levelno == 10  # DEBUG


def test_transition_logs_via_device_manager(caplog: pytest.LogCaptureFixture) -> None:
    """The DeviceManager (the production path) routes messages and fires the
    same transition logging through the per-device registries."""
    clock = [100.0]
    manager = DeviceManager(
        expire_after=5, clock=lambda: clock[0], device_name="Telegraf MQTT"
    )
    manager.set_parser(TelegrafParser())
    manager.process_message(
        "telegraf/host1/mem",
        json.dumps(
            {
                "name": "mem",
                "tags": {"host": "host1"},
                "fields": {"used_percent": 42.0},
                "timestamp": 1700000000,
            }
        ),
    )
    with caplog.at_level("DEBUG", logger="custom_components.telegraf_mqtt.registry"):
        clock[0] = 106.0
        # Advance the clock and run the DeviceManager-level expiry pass so we
        # exercise the manager's delegation to per-device registries.
        manager.check_expiry()

    matches = [r for r in caplog.records if "went unavailable" in (r.getMessage() or "")]
    assert len(matches) == 1
    # DeviceManager routes the message to a per-device MetricRegistry whose
    # unique_key is the bare measurement+field and whose device_name is the
    # host derived from the topic/tags.
    assert "mem_used_percent" in matches[0].getMessage()
    assert "host1" in matches[0].getMessage()
    assert matches[0].levelno == 10  # logging.DEBUG


def test_device_manager_unchanged_value_does_not_log_unavailable(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A no-op refresh through the DeviceManager (same value, clock unchanged)
    does not produce a 'went unavailable' log -- the metric stays available
    because ``update`` keeps ``last_updated`` current."""
    clock = [100.0]
    manager = DeviceManager(
        expire_after=5, clock=lambda: clock[0], device_name="Telegraf MQTT"
    )
    manager.set_parser(TelegrafParser())
    payload = json.dumps(
        {
            "name": "mem",
            "tags": {"host": "host1"},
            "fields": {"used_percent": 42.0},
            "timestamp": 1700000000,
        }
    )
    manager.process_message("telegraf/host1/mem", payload)

    with caplog.at_level("DEBUG", logger="custom_components.telegraf_mqtt.registry"):
        # Same value, clock not advanced: no expiry, no transition -> no log.
        manager.process_message("telegraf/host1/mem", payload)

    assert not [r for r in caplog.records if "went unavailable" in (r.getMessage() or "")]


# ---------------------------------------------------------------------------
# Error-path hardening: malformed payload flood
# ---------------------------------------------------------------------------


def test_malformed_payload_flood_no_exception_and_counts() -> None:
    """A burst of malformed payloads (bad JSON, wrong shape, nested values)
    must never raise and must update the parser-stats counters correctly while
    the valid message in the stream still lands in the registry."""
    stats = ParserStats()
    parser = TelegrafParser(stats=stats)
    manager = DeviceManager(expire_after=120)
    manager.set_parser(parser)

    messages = [
        ("telegraf/host1/cpu", "not json at all"),
        ("telegraf/host1/cpu", "{broken"),
        ("telegraf/host1/cpu", json.dumps("a string not a dict")),
        ("telegraf/host1/cpu", json.dumps({"fields": {"x": 1}})),  # no name
        ("telegraf/host1/cpu", json.dumps({"name": 7, "fields": {"x": 1}})),
        ("telegraf/host1/cpu", json.dumps({"name": "cpu", "fields": {"x": {}}})),  # nested -> dropped
        (
            "telegraf/host1/mem",
            json.dumps(
                {
                    "name": "mem",
                    "tags": {"host": "host1"},
                    "fields": {"used_percent": 42.0},
                    "timestamp": 1700000000,
                }
            ),
        ),
    ]
    for topic, payload in messages:
        manager.process_message(topic, payload)  # must never raise

    assert stats.received == len(messages)
    assert stats.dropped_invalid_json == 2
    assert stats.dropped_unsupported_shape >= 3
    # Two messages reach the measurement dispatch stage: the ``cpu`` one
    # (whose nested field is dropped, but the measurement itself parsed) and
    # the valid ``mem`` one. Unknown/shape-broken payloads never count as
    # parsed -- only measurements that dispatched successfully do.
    assert stats.parsed == 2
    # The valid ``mem`` message was parsed and stored in the registry.
    # The composite key excludes the ``host`` tag from the unique_key.
    assert manager.get_metric("host1:mem_used_percent") is not None


def test_parser_stats_survive_full_flood_cycle() -> None:
    """Parser stats are the single source of truth; after a flood the counters
    and the last-message snapshot are intact and redacted (no raw bytes)."""
    stats = ParserStats()
    parser = TelegrafParser(stats=stats)
    parser.parse(b"garbage")
    parser.parse(json.dumps({"name": "mem", "fields": {"x": 1}, "timestamp": 1}).encode())

    assert stats.last_message is not None
    assert "garbage" not in json.dumps(stats.last_message)
    assert stats.dropped_invalid_json == 1


# ---------------------------------------------------------------------------
# config-entry-unloading: reload cycles + double-unload (no leaks)
# ---------------------------------------------------------------------------


def test_unload_then_reload_clean_cycle(monkeypatch: pytest.MonkeyPatch) -> None:
    """A full reload (setup -> unload -> setup) leaks nothing: each setup
    installs a probe + real subscription, each unload tears the real one down
    and wipes the expiry handle. A second reload succeeds again."""
    fake_mqtt = _patch(monkeypatch)
    hass = FakeHass()
    entry = FakeConfigEntry()

    for cycle in (1, 2):
        assert asyncio.run(integration.async_setup_entry(hass, entry)) is True
        assert entry.runtime_data.unsubscribe is not None
        # Setup = probe (1) + real (1) subscribe for this cycle.
        assert len(fake_mqtt.subscribe_calls) == 2 * cycle
        # The expiry timer handle was registered for this cycle.
        assert entry.runtime_data.cancel_expiry is not None

        assert asyncio.run(integration.async_unload_entry(hass, entry)) is True
        # Unload tore down the real subscription and wiped the handle.
        assert entry.runtime_data.unsubscribe is None
        assert entry.runtime_data.cancel_expiry is None
        # Platform unload was requested for this cycle.
        assert len(hass.config_entries.unloaded) == cycle

    # Each setup/unload round pumped exactly one real-subscription unsubscribe.
    assert fake_mqtt.unsubscribe_calls >= 2


def test_double_unload_is_clean_noop(monkeypatch: pytest.MonkeyPatch) -> None:
    """Calling unload twice must not double-unsubscribe or raise."""
    fake_mqtt = _patch(monkeypatch)
    hass = FakeHass()
    entry = FakeConfigEntry()
    assert asyncio.run(integration.async_setup_entry(hass, entry)) is True

    assert asyncio.run(integration.async_unload_entry(hass, entry)) is True
    unsubs_after_first = fake_mqtt.unsubscribe_calls
    assert asyncio.run(integration.async_unload_entry(hass, entry)) is True
    assert fake_mqtt.unsubscribe_calls == unsubs_after_first  # no second unwire


def test_reload_restores_fresh_runtime_and_parser_stats(monkeypatch: pytest.MonkeyPatch) -> None:
    """After unload + re-setup the runtime_data and ParserStats are fresh
    (a reconnect does not carry stale state from a previous life)."""
    fake_mqtt = _patch(monkeypatch)
    hass = FakeHass()
    entry = FakeConfigEntry()

    asyncio.run(integration.async_setup_entry(hass, entry))
    first_parser_stats = entry.runtime_data.parser_stats
    asyncio.run(integration.async_unload_entry(hass, entry))

    asyncio.run(integration.async_setup_entry(hass, entry))
    assert entry.runtime_data.parser_stats is not first_parser_stats
    assert entry.runtime_data.parser_stats.received == 0
    assert entry.runtime_data.parser_stats is entry.runtime_data.parser.stats


# ---------------------------------------------------------------------------
# Broker-reconnect simulation: the integration survives a subscribe/drop cycle
# ---------------------------------------------------------------------------


def test_broker_resubscribe_tolerates_drop_and_processes_messages(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """HA's mqtt component owns broker reconnects by re-running the subscribe
    callback. We simulate that lifecycle: setup -> drop (unload) -> re-setup,
    then verify a message through the *new* real-subscription callback still
    parses, lands in the registry, and updates the ParserStats."""
    fake_mqtt = _patch(monkeypatch)
    hass = FakeHass()
    entry = FakeConfigEntry()

    asyncio.run(integration.async_setup_entry(hass, entry))

    def _payload(name: str) -> Any:
        return {
            "name": name,
            "tags": {"host": "host1"},
            "fields": {"used_percent": 69.0},
            "timestamp": 1700000000,
        }

    class _Msg:
        def __init__(self, topic: str, payload: str) -> None:
            self.topic = topic
            self.payload = payload

    # First life: fire through the real subscription callback (call 2).
    # ``message_received`` is async, so drive it to completion with asyncio.run.
    real_cb = fake_mqtt.subscribe_calls[1][1]
    asyncio.run(real_cb(_Msg("telegraf/host1/mem", json.dumps(_payload("mem")))))
    assert entry.runtime_data.parser_stats.received == 1

    # Simulate a broker drop: unload. HA will resubscribe on reconnect, which
    # we model as a fresh setup. The integration must not raise on the drop.
    asyncio.run(integration.async_unload_entry(hass, entry))

    # Reconnect: a new setup installs a fresh real subscription (call 4).
    asyncio.run(integration.async_setup_entry(hass, entry))
    new_real_cb = fake_mqtt.subscribe_calls[3][1]
    assert new_real_cb is not real_cb
    asyncio.run(new_real_cb(_Msg("telegraf/host1/mem", json.dumps(_payload("mem")))))
    # The fresh ParserStats is back to a clean slate after reconnect.
    assert entry.runtime_data.parser_stats.received == 1
    # The registry produced a metric from the reconnected stream.
    assert entry.runtime_data.manager.get_metric("host1:mem_used_percent") is not None


# ---------------------------------------------------------------------------
# integration-owner: CODEOWNERS recorded; docs cover install + options
# ---------------------------------------------------------------------------


def test_codeowners_file_exists_and_names_owner() -> None:
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    codeowners = root / "CODEOWNERS"
    assert codeowners.exists(), "a CODEOWNERS file must exist for the integration-owner rule"
    text = codeowners.read_text(encoding="utf-8")
    assert "@Gekko47" in text
    assert "* @Gekko47" in text


def test_manifest_lists_codeowners() -> None:
    import json
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    manifest = root / "custom_components" / "telegraf_mqtt" / "manifest.json"
    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert data.get("codeowners") == ["@Gekko47"]


def test_readme_documents_install_variants() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    readme = (root / "README.md").read_text(encoding="utf-8")
    assert "HACS" in readme
    assert "Manual installation" in readme
    assert "2026.6" in readme


def test_readme_documents_every_option() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    readme = (root / "README.md").read_text(encoding="utf-8")
    for option in (
        "exclude_patterns",
        "field_overrides",
        "expire_after",
        "enable_cleanup",
        "cleanup_delay",
        "delete_delay",
        "min_active_metrics",
    ):
        assert option in readme, f"README must document the {option} option"


def test_readme_documents_coverage_floor() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    readme = (root / "README.md").read_text(encoding="utf-8")
    assert "100%" in readme
    assert "coverage" in readme.lower()
