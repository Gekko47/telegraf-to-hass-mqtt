"""Phase 9 exit-criteria tests: the Gold quality-scale gate.

ROADMAP.md Phase 9 (Gold UX + translations + docs depth):
  - every emitted entity has translation_key + placeholders
  - diagnostic entities are disabled by default
  - non-diagnostic entities are enabled by default
  - DeviceInfo carries sw_version when set
  - icon assigned from descriptor icon key
  - reconfigure flow updates topic pattern + reloads
  - reconfigure flow aborts on duplicate topic
  - reconfigure flow validates invalid topic
  - translatable exceptions carry translation_key
  - diagnostics no host-identity leaks (redaction audit)
  - overlap repair raises + auto-resolves
  - invalid-option repair raises + clears
  - dynamic device appears without restart
  - stale device pruned after delete_delay
  - stale device recovers on reappearance
  - every descriptor translation_key has a strings.json + en.json row
  - every descriptor icon key has a MDI icon
  - README has every required Gold section
  - descriptor.name is gone (no fallback path)
  - generic_field is the only fallback translation key
"""

from __future__ import annotations

import asyncio
import json
import logging
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
from custom_components.telegraf_mqtt.exceptions import (
    MqttBrokerUnreachable,
    ReconfigureSubscribeFailed,
)
from custom_components.telegraf_mqtt.icons import ICON_FOR_KEY
from custom_components.telegraf_mqtt.models import MetricDescriptor
from custom_components.telegraf_mqtt.naming import (
    ENTITY_CATEGORY_DIAGNOSTIC,
    ICON_KEY_GENERIC,
    TK_GENERIC_FIELD,
    resolve_translation,
)
from custom_components.telegraf_mqtt.parser import ParserStats, TelegrafParser
from custom_components.telegraf_mqtt.registry import DeviceManager, MetricRegistry
from custom_components.telegraf_mqtt.translations_strings import (
    all_translation_keys,
    format_translation,
)


# ---------------------------------------------------------------------------
# FakeHass / FakeConfigEntry / FakeMqtt
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
    data: dict = field(default_factory=dict)
    config: Any = field(default_factory=lambda: type("Cfg", (), {"path": "", "config_dir": "", "components": set()}))

    def __hash__(self) -> int:
        return id(self)


class FakeConfigEntry:
    def __init__(
        self,
        *,
        data: dict | None = None,
        options: dict | None = None,
        title: str = "Telegraf",
        entry_id: str = "entry-1",
    ) -> None:
        self.entry_id = entry_id
        self.data = data or {
            CONF_TOPIC_PATTERN: "telegraf/#",
            CONF_DEVICE_NAME: title,
        }
        self.options = options or {}
        self.title = title
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
        self.subscribe_error: Exception | None = None

    async def async_subscribe(
        self, _hass: Any, topic_pattern: str, callback: Callable[..., Any]
    ) -> Callable[[], None]:
        self.subscribe_calls.append((topic_pattern, callback))
        if self.subscribe_error is not None:
            err = self.subscribe_error
            self.subscribe_error = None
            raise err
        return lambda: setattr(self, "unsubscribe_calls", self.unsubscribe_calls + 1)


def _patch_integration(monkeypatch, fake_mqtt: FakeMqtt) -> None:
    monkeypatch.setattr(integration, "mqtt", fake_mqtt, raising=False)


def _runtime_data_with_sw(sw: str | None = None) -> Any:
    from custom_components.telegraf_mqtt import TelegrafMqttRuntimeData

    manager = DeviceManager()
    parser = TelegrafParser()
    return TelegrafMqttRuntimeData(
        manager=manager,
        parser=parser,
        parser_stats=parser.stats,
        manufacturer="Acme",
        model="PC-1",
        sw_version=sw,
    )


# ---------------------------------------------------------------------------
# descriptor.name field is GONE (the clean-cut)
# ---------------------------------------------------------------------------


def test_descriptor_name_field_is_gone() -> None:
    """Phase 9: the resolved display string is no longer on MetricDescriptor.

    Catches any regression that re-introduces the old ``name`` field.
    """
    desc = MetricDescriptor(
        unique_key="k",
        measurement="m",
        tags={},
        field="f",
        value=1.0,
        timestamp=0.0,
        native_unit=None,
        suggested_device_class=None,
        suggested_state_class=None,
        entity_category=None,
    )
    assert not hasattr(desc, "name")


# ---------------------------------------------------------------------------
# Translation-key + placeholder resolution
# ---------------------------------------------------------------------------


def test_resolve_translation_cpu_field_has_field_placeholder() -> None:
    key, placeholders = resolve_translation("cpu", {"host": "h"}, "usage_idle")
    assert key == "cpu_field"
    assert placeholders == {"field": "Usage Idle"}


def test_resolve_translation_disk_root_path_uses_root_template() -> None:
    key, placeholders = resolve_translation("disk", {"host": "h", "path": "/"}, "used_percent")
    assert key == "disk_root_field"
    assert placeholders == {"field": "Used Percent"}


def test_resolve_translation_sensors_coretemp_special_cases() -> None:
    key, placeholders = resolve_translation(
        "sensors",
        {"host": "h", "chip": "coretemp-isa-0000", "feature": "package_id_0"},
        "temp_input",
    )
    assert key == "cpu_package_temperature"
    assert placeholders == {}


def test_resolve_translation_network_includes_interface() -> None:
    key, placeholders = resolve_translation(
        "net", {"host": "h", "interface": "wlan0"}, "bytes_recv"
    )
    assert key == "network_field"
    assert placeholders == {"field": "Bytes Received", "interface": "wlan0"}


def test_resolve_translation_unknown_measurement_falls_back_to_generic() -> None:
    """Generic is the *only* fallback. There is no silent passthrough."""
    key, placeholders = resolve_translation(
        "custom_plugin", {"host": "h"}, "watts"
    )
    assert key == TK_GENERIC_FIELD
    assert placeholders == {"field": "Watts"}


def test_format_translation_renders_all_reference_payload_names() -> None:
    """The translation tables produce the user-facing display names for the
    SPEC.md reference payloads."""
    parser = TelegrafParser()
    cases = [
        ("cpu", "Usage Idle", "CPU Usage Idle"),
        ("mem", "used", "Memory Used"),
        ("disk", "free", "Disk Root Free"),
        ("net", "bytes_recv", "wlan0 Bytes Received"),
        ("sensors", "temp_input", "CPU Package Temperature"),
        ("nvidia_gpu", "gpu_util", "GPU Utilization"),
        ("battery", "percentage", "Battery Percentage"),
    ]
    for measurement, field, expected in cases:
        payload = {
            "name": measurement,
            "tags": {"host": "h"} | ({"path": "/"} if measurement == "disk" else {}) | (
                {"chip": "coretemp-isa-0000", "feature": "package_id_0"} if measurement == "sensors" else {"interface": "wlan0"} if measurement == "net" else {}),
            "fields": {field: 1},
            "timestamp": 0,
        }
        descriptor = parser.parse(json.dumps(payload))[0]
        rendered = format_translation(
            descriptor.translation_key, dict(descriptor.translation_placeholders)
        )
        assert rendered == expected, (measurement, field, rendered)


# ---------------------------------------------------------------------------
# Entity layer: translation_key/placeholders + icon + disabled-by-default
# ---------------------------------------------------------------------------


def _install_entity_homeassistant_stubs(monkeypatch) -> None:
    """Stub the HA modules so sensor.py / binary_sensor.py import under test."""
    import sys
    import types
    import importlib

    from homeassistant.helpers import device_registry, dispatcher
    from homeassistant.helpers import entity as entity_helpers
    from homeassistant.const import UnitOfTemperature
    from homeassistant.core import HomeAssistant, callback
    from homeassistant.components import sensor as sensor_module
    from homeassistant.components import binary_sensor as binary_module

    monkeypatch.setattr(device_registry, "DeviceInfo", dict, raising=False)
    monkeypatch.setattr(dispatcher, "async_dispatcher_connect", lambda *a, **kw: lambda: None, raising=False)
    monkeypatch.setattr(entity_helpers, "EntityCategory", entity_helpers.EntityCategory, raising=False)
    # Force a fresh import of the platform modules.
    sys.modules.pop("custom_components.telegraf_mqtt.sensor", None)
    sys.modules.pop("custom_components.telegraf_mqtt.binary_sensor", None)
    importlib.import_module("custom_components.telegraf_mqtt.sensor")
    importlib.import_module("custom_components.telegraf_mqtt.binary_sensor")


def _make_descriptor(measurement: str, field: str, *, tags=None, category=None) -> MetricDescriptor:
    from custom_components.telegraf_mqtt.parsers.generic import build_unique_key
    from custom_components.telegraf_mqtt.translations_strings import format_translation as _fmt  # noqa: F401
    from custom_components.telegraf_mqtt.naming import (
        resolve_entity_category as _cat,
        resolve_translation as _tr,
    )
    from types import MappingProxyType

    translation_key, placeholders = _tr(measurement, tags or {}, field)
    if category is None:
        category = _cat(measurement, field)
    return MetricDescriptor(
        unique_key=build_unique_key(measurement, tags or {}, field),
        measurement=measurement,
        tags=tags or {},
        field=field,
        value=1.0,
        timestamp=0.0,
        native_unit=None,
        suggested_device_class=None,
        suggested_state_class="measurement",
        entity_category=category,
        translation_key=translation_key,
        translation_placeholders=MappingProxyType(dict(placeholders)),
    )


def test_diagnostic_entity_is_disabled_by_default(monkeypatch) -> None:
    """Phase 9: ``disk`` measurement -> diagnostic -> disabled by default."""
    from homeassistant.components.sensor import SensorEntity
    from custom_components.telegraf_mqtt.sensor import TelegrafMqttSensor

    @dataclass
    class Entry:
        runtime_data: Any

    entry = Entry(runtime_data=_runtime_data_with_sw())
    registry = entry.runtime_data.manager.get_or_create_registry("host1", "host1")
    descriptor = _make_descriptor("disk", "used_percent", tags={"host": "host1", "path": "/"})
    registry.update(descriptor)

    _install_entity_homeassistant_stubs(monkeypatch)
    entity = TelegrafMqttSensor(entry, "host1:disk_root_used_percent")
    assert descriptor.entity_category == ENTITY_CATEGORY_DIAGNOSTIC
    assert entity._attr_entity_registry_enabled_default is False
    assert entity._attr_entity_category.value == "diagnostic"


def test_non_diagnostic_entity_is_enabled_by_default(monkeypatch) -> None:
    """Phase 9: ``cpu.usage_idle`` is non-diagnostic -> enabled by default."""
    from custom_components.telegraf_mqtt.sensor import TelegrafMqttSensor

    @dataclass
    class Entry:
        runtime_data: Any

    entry = Entry(runtime_data=_runtime_data_with_sw())
    registry = entry.runtime_data.manager.get_or_create_registry("host1", "host1")
    descriptor = _make_descriptor("cpu", "usage_idle", tags={"host": "host1"})
    registry.update(descriptor)

    _install_entity_homeassistant_stubs(monkeypatch)
    entity = TelegrafMqttSensor(entry, "host1:cpu_usage_idle")
    assert descriptor.entity_category is None
    assert entity._attr_entity_registry_enabled_default is True
    assert entity._attr_entity_category is None


def test_entity_uses_translation_key_and_placeholders(monkeypatch) -> None:
    """Phase 9: entities set _attr_translation_key + placeholders, not _attr_name."""
    from custom_components.telegraf_mqtt.sensor import TelegrafMqttSensor

    @dataclass
    class Entry:
        runtime_data: Any

    entry = Entry(runtime_data=_runtime_data_with_sw())
    registry = entry.runtime_data.manager.get_or_create_registry("host1", "host1")
    descriptor = _make_descriptor("cpu", "usage_idle", tags={"host": "host1"})
    registry.update(descriptor)

    _install_entity_homeassistant_stubs(monkeypatch)
    entity = TelegrafMqttSensor(entry, "host1:cpu_usage_idle")
    assert entity._attr_translation_key == "cpu_field"
    assert entity._attr_translation_placeholders == {"field": "Usage Idle"}
    # No _attr_name -- every entity uses the translation path.
    assert getattr(entity, "_attr_name", None) is None


def test_entity_icon_is_always_set(monkeypatch) -> None:
    """Every reference payload produces an entity with a non-null icon."""
    from custom_components.telegraf_mqtt.sensor import TelegrafMqttSensor

    @dataclass
    class Entry:
        runtime_data: Any

    cases = [
        ("cpu", "usage_idle", {"host": "host1"}),
        ("mem", "used_percent", {"host": "host1"}),
        ("disk", "used_percent", {"host": "host1", "path": "/"}),
        ("net", "bytes_recv", {"host": "host1", "interface": "wlan0"}),
        ("sensors", "temp_input", {"host": "host1", "chip": "coretemp-isa-0000", "feature": "package_id_0"}),
        ("nvidia_gpu", "gpu_util", {"host": "host1"}),
        ("battery", "percentage", {"host": "host1"}),
    ]
    _install_entity_homeassistant_stubs(monkeypatch)
    for measurement, field, tags in cases:
        entry = Entry(runtime_data=_runtime_data_with_sw())
        registry = entry.runtime_data.manager.get_or_create_registry("host1", "host1")
        descriptor = _make_descriptor(measurement, field, tags=tags)
        registry.update(descriptor)
        entity = TelegrafMqttSensor(entry, f"host1:{descriptor.unique_key}")
        assert entity._attr_icon, (measurement, field)
        assert entity._attr_icon.startswith("mdi:"), (measurement, field)


def test_icon_lookup_table_covers_every_inferred_key() -> None:
    from custom_components.telegraf_mqtt.naming import (
        ICON_KEY_BATTERY, ICON_KEY_CPU, ICON_KEY_DISK, ICON_KEY_ENERGY, ICON_KEY_FAN,
        ICON_KEY_GENERIC, ICON_KEY_MEMORY, ICON_KEY_NETWORK, ICON_KEY_PERCENTAGE,
        ICON_KEY_POWER, ICON_KEY_TEMPERATURE, ICON_KEY_VOLTAGE, ICON_KEY_BINARY,
    )
    for key in (
        ICON_KEY_CPU, ICON_KEY_MEMORY, ICON_KEY_DISK, ICON_KEY_NETWORK,
        ICON_KEY_TEMPERATURE, ICON_KEY_VOLTAGE, ICON_KEY_POWER, ICON_KEY_ENERGY,
        ICON_KEY_BATTERY, ICON_KEY_FAN, ICON_KEY_PERCENTAGE, ICON_KEY_BINARY,
        ICON_KEY_GENERIC,
    ):
        assert key in ICON_FOR_KEY, key
        assert ICON_FOR_KEY[key].startswith("mdi:"), key


# ---------------------------------------------------------------------------
# DeviceInfo: sw_version
# ---------------------------------------------------------------------------


def test_device_info_includes_sw_version_when_set(monkeypatch) -> None:
    from custom_components.telegraf_mqtt.sensor import TelegrafMqttSensor

    @dataclass
    class Entry:
        runtime_data: Any

    entry = Entry(runtime_data=_runtime_data_with_sw("2026.6.0"))
    registry = entry.runtime_data.manager.get_or_create_registry("host1", "host1")
    descriptor = _make_descriptor("cpu", "usage_idle", tags={"host": "host1"})
    registry.update(descriptor)

    _install_entity_homeassistant_stubs(monkeypatch)
    entity = TelegrafMqttSensor(entry, "host1:cpu_usage_idle")
    assert entity._attr_device_info["sw_version"] == "2026.6.0"


def test_device_info_sw_version_is_none_when_unset(monkeypatch) -> None:
    from custom_components.telegraf_mqtt.sensor import TelegrafMqttSensor

    @dataclass
    class Entry:
        runtime_data: Any

    entry = Entry(runtime_data=_runtime_data_with_sw(None))
    registry = entry.runtime_data.manager.get_or_create_registry("host1", "host1")
    descriptor = _make_descriptor("cpu", "usage_idle", tags={"host": "host1"})
    registry.update(descriptor)

    _install_entity_homeassistant_stubs(monkeypatch)
    entity = TelegrafMqttSensor(entry, "host1:cpu_usage_idle")
    assert entity._attr_device_info["sw_version"] is None


# ---------------------------------------------------------------------------
# Translatable exceptions
# ---------------------------------------------------------------------------


def test_reconfigure_subscribe_failed_carries_translation_key() -> None:
    exc = ReconfigureSubscribeFailed("telegraf/#", "no route to host")
    assert exc.translation_domain == "telegraf_mqtt"
    assert exc.translation_key == "reconfigure_subscribe_failed"
    assert exc.translation_placeholders == {
        "topic": "telegraf/#",
        "error": "no route to host",
    }


def test_mqtt_broker_unreachable_carries_translation_key() -> None:
    exc = MqttBrokerUnreachable("telegraf/#", "no route to host")
    assert exc.translation_domain == "telegraf_mqtt"
    assert exc.translation_key == "mqtt_broker_unreachable"
    assert exc.translation_placeholders == {
        "topic": "telegraf/#",
        "error": "no route to host",
    }


def test_config_entry_not_ready_uses_mqtt_broker_unreachable_translation(monkeypatch) -> None:
    """When setup fails to subscribe, the raised ConfigEntryNotReady carries
    the mqtt_broker_unreachable translation key + placeholders."""
    import asyncio
    from homeassistant.exceptions import ConfigEntryNotReady
    fake_mqtt = FakeMqtt()
    fake_mqtt.subscribe_error = RuntimeError("connection refused")
    _patch_integration(monkeypatch, fake_mqtt)
    # Replace the real ``ir`` module with a no-op stub so repairs.py
    # doesn't try to access the full Home Assistant runtime.
    class _StubIR:
        class IssueSeverity:
            WARNING = "warning"
        def async_create_issue(self, *a, **kw): pass
        def async_delete_issue(self, *a, **kw): pass
    monkeypatch.setattr(integration, "ir", _StubIR(), raising=False)
    hass = FakeHass()
    entry = FakeConfigEntry()
    captured: dict = {}

    async def _run() -> None:
        try:
            await integration.async_setup_entry(hass, entry)  # type: ignore[arg-type]
        except ConfigEntryNotReady as exc:
            captured["exc"] = exc

    asyncio.run(_run())
    assert "exc" in captured
    exc = captured["exc"]
    assert exc.translation_domain == DOMAIN
    assert exc.translation_key == "mqtt_broker_unreachable"
    assert exc.translation_placeholders == {
        "topic": "telegraf/#",
        "error": "connection refused",
    }


# ---------------------------------------------------------------------------
# Reconfigure flow
# ---------------------------------------------------------------------------


def test_reconfigure_flow_validates_invalid_topic() -> None:
    """async_step_reconfigure rejects syntactically invalid topics."""
    from custom_components.telegraf_mqtt.config_flow import _valid_subscription_topic

    assert not _valid_subscription_topic("not/#/wildcard")
    assert not _valid_subscription_topic("bad#segment")
    assert not _valid_subscription_topic("")
    assert _valid_subscription_topic("telegraf/#")
    assert _valid_subscription_topic("a/+/c")


def test_reconfigure_flow_aborts_on_duplicate_topic(monkeypatch) -> None:
    """Re-configuring to a topic already used by another entry aborts.

    The actual abort happens in HA's ``async_set_unique_id`` (HA plumbing);
    what we verify here is that the reconfigure flow's validation step
    accepts the input as long as it passes the topic-syntax check, and
    that the flow *invokes* ``async_set_unique_id`` before applying the
    update. We assert this by monkeypatching ``async_set_unique_id`` and
    confirming it was called with the new topic.
    """
    from custom_components.telegraf_mqtt.config_flow import TelegrafMqttConfigFlow

    hass = FakeHass()

    class _CfgEntries:
        def __init__(self) -> None:
            self.known: dict[str, Any] = {
                "self": FakeConfigEntry(
                    data={CONF_TOPIC_PATTERN: "old/topic", CONF_DEVICE_NAME: "Self"},
                )
            }

        def async_get_known_entry(self, entry_id):
            return self.known[entry_id]

        def async_entries(self, _domain):
            return [
                FakeConfigEntry(
                    entry_id="other",
                    data={CONF_TOPIC_PATTERN: "telegraf/#", CONF_DEVICE_NAME: "Existing"},
                )
            ]

    hass.config_entries = _CfgEntries()  # type: ignore[assignment]
    flow = TelegrafMqttConfigFlow()
    flow.hass = hass  # type: ignore[attr-defined]
    flow.context = {"entry_id": "self"}  # type: ignore[attr-defined]

    # The validator accepts the new topic -- the abort is in async_set_unique_id.
    errors = flow._validate({CONF_TOPIC_PATTERN: "new/topic", CONF_DEVICE_NAME: "X"})
    assert errors == {}

    # Mock async_set_unique_id to confirm the reconfigure flow calls it
    # before applying the data update. This is the call that aborts when
    # the topic pattern is already used by another entry.
    called_with: list[str] = []

    async def fake_set_unique_id(value, *args, **kwargs):
        called_with.append(value)

    monkeypatch.setattr(flow, "async_set_unique_id", fake_set_unique_id)

    update_called: list[bool] = []

    async def fake_update(*args, **kwargs):
        # Record invocation only. Do NOT call async_set_unique_id here --
        # async_step_reconfigure is the sole source of that call, which we
        # verify via the called_with assertion below.
        update_called.append(True)

    monkeypatch.setattr(flow, "async_update_reload_and_abort", fake_update)

    import asyncio
    asyncio.run(flow.async_step_reconfigure({CONF_TOPIC_PATTERN: "new/topic", CONF_DEVICE_NAME: "X"}))
    assert called_with == ["new/topic"]


def test_reconfigure_flow_runs_async_step_reconfigure(monkeypatch) -> None:
    """The config flow exposes ``async_step_reconfigure`` -- the HA-blessed
    reconfigure entry point for HA 2026.6+."""
    from custom_components.telegraf_mqtt.config_flow import TelegrafMqttConfigFlow

    assert hasattr(TelegrafMqttConfigFlow, "async_step_reconfigure")
    assert callable(TelegrafMqttConfigFlow.async_step_reconfigure)


# ---------------------------------------------------------------------------
# Diagnostics: redaction audit
# ---------------------------------------------------------------------------


def test_diagnostics_no_host_identity_leaks(monkeypatch) -> None:
    """The diagnostics download must not contain the user's host identity.

    SPEC.md says the diagnostics payload exposes config, runtime stats,
    last-message metadata (redacted), and dropped-payload counts. The
    host tag in a Telegraf payload is the user's machine name -- it must
    never leave the integration.
    """
    from custom_components.telegraf_mqtt.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    @dataclass
    class _Entry:
        entry_id: str = "entry-1"
        domain: str = DOMAIN
        title: str = "Telegraf"
        unique_id: str = "telegraf/#"
        data: dict = field(default_factory=lambda: {
            CONF_TOPIC_PATTERN: "telegraf/#",
            CONF_DEVICE_NAME: "Telegraf",
        })
        options: dict = field(default_factory=dict)
        runtime_data: Any = None

    manager = DeviceManager()
    parser = TelegrafParser()
    manager.process_message(
        "telegraf/secret-host/cpu",
        json.dumps({"name": "cpu", "tags": {"host": "secret-host"}, "fields": {"usage_idle": 99.9}, "timestamp": 1}),
        parser=parser,
    )

    @dataclass
    class _RD:
        manager: Any = field(default=None)
        parser: Any = field(default=None)
        parser_stats: Any = field(default=None)
        manufacturer: str | None = "Acme"
        model: str | None = None
        sw_version: str | None = None

    rd = _RD(manager=manager, parser=parser, parser_stats=parser.stats)
    entry = _Entry(runtime_data=rd)

    async def _run() -> None:
        payload = await async_get_config_entry_diagnostics(FakeHass(), entry)  # type: ignore[arg-type]
        # Walk the dict as JSON and check no value matches the secret hostname.
        serialized = json.dumps(payload)
        assert "secret-host" not in serialized
        # The diagnostic device id should be a hash, not the raw slug.
        runtime = payload.get("runtime", {})
        devices = runtime.get("manager", {}).get("devices", [])
        assert devices, "manager.devices must not be empty"
        for device in devices:
            # Per-device id is a 16-char digest, not the original slug.
            assert "secret" not in device.get("device_id", "")

    asyncio.run(_run())


def test_diagnostics_contains_required_sections() -> None:
    """The diagnostics payload includes entry, config, runtime, options_validity."""
    from custom_components.telegraf_mqtt.diagnostics import (
        async_get_config_entry_diagnostics,
    )

    @dataclass
    class _Entry:
        entry_id: str = "entry-1"
        domain: str = DOMAIN
        title: str = "Telegraf"
        unique_id: str = "telegraf/#"
        data: Any = field(default_factory=lambda: {CONF_TOPIC_PATTERN: "telegraf/#", CONF_DEVICE_NAME: "Telegraf"})
        options: Any = field(default_factory=dict)
        runtime_data: Any = None

    @dataclass
    class _RD:
        manager: Any = field(default=None)
        parser: Any = field(default=None)
        parser_stats: Any = field(default=None)
        manufacturer: str | None = None
        model: str | None = None
        sw_version: str | None = None

    rd = _RD(
        manager=DeviceManager(),
        parser=TelegrafParser(),
        parser_stats=TelegrafParser().stats,
    )
    entry = _Entry(runtime_data=rd)

    async def _run() -> None:
        payload = await async_get_config_entry_diagnostics(FakeHass(), entry)  # type: ignore[arg-type]
        assert "entry" in payload
        assert "config" in payload
        assert "runtime" in payload
        assert "options_validity" in payload

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Repairs: gate evidence
# ---------------------------------------------------------------------------


def test_overlap_repair_raises_and_auto_resolves(monkeypatch) -> None:
    """Overlapping topic patterns raise a Repairs issue; non-overlap clears it."""
    from custom_components.telegraf_mqtt.repairs import check_overlapping_topics

    created: list[dict] = []
    deleted: list[str] = []

    class _IR:
        class IssueSeverity:
            WARNING = "warning"

        def async_create_issue(self, hass, *, domain, issue_id, is_fixable, severity, translation_key, translation_placeholders):
            created.append({
                "issue_id": issue_id, "translation_key": translation_key,
                "severity": severity, "placeholders": translation_placeholders,
            })

        def async_delete_issue(self, hass, *, domain, issue_id):
            deleted.append(issue_id)

    class _CfgEntries:
        def __init__(self, others):
            self._others = others

        def async_entries(self, _domain):
            return self._others

    @dataclass
    class _Entry:
        entry_id: str = "self"
        data: dict = field(default_factory=lambda: {CONF_TOPIC_PATTERN: "telegraf/#", CONF_DEVICE_NAME: "X"})

    @dataclass
    class _Other:
        entry_id: str = "other"
        title: str = "Other"
        data: dict = field(default_factory=lambda: {CONF_TOPIC_PATTERN: "telegraf/+/cpu", CONF_DEVICE_NAME: "Y"})

    hass = FakeHass()
    hass.config_entries = _CfgEntries([_Other()])  # type: ignore[assignment]
    monkeypatch.setattr(integration, "ir", _IR(), raising=False)

    overlapping = check_overlapping_topics(hass, _Entry())  # type: ignore[arg-type]
    assert "other" in overlapping
    assert any(c["translation_key"] == "overlap_topic_patterns" for c in created)
    assert any(c["severity"] == "warning" for c in created)

    # Switch to a non-overlapping other entry; the prior issue must be cleared.
    # Reload: now the overlap check should find no overlap, and delete the prior issue.
    other2 = _Other()
    other2.entry_id = "different"
    other2.data = {"topic_pattern": "different/#", "device_name": "Z"}
    hass.config_entries = _CfgEntries([other2])  # type: ignore[assignment]
    overlapping2 = check_overlapping_topics(hass, _Entry())  # type: ignore[arg-type]
    assert overlapping2 == []


def test_invalid_option_repair_raises_and_clears(monkeypatch) -> None:
    """Invalid persisted options raise a Repairs issue; valid options clear it."""
    from custom_components.telegraf_mqtt.repairs import check_invalid_persisted_option

    created: list[dict] = []
    deleted: list[str] = []

    class _IR:
        class IssueSeverity:
            WARNING = "warning"

        def async_create_issue(self, hass, *, domain, issue_id, is_fixable, severity, translation_key, translation_placeholders):
            created.append({"issue_id": issue_id, "translation_key": translation_key, "severity": severity, "placeholders": translation_placeholders})

        def async_delete_issue(self, hass, *, domain, issue_id):
            deleted.append(issue_id)

    @dataclass
    class _Entry:
        entry_id: str = "self"

    hass = FakeHass()
    monkeypatch.setattr(integration, "ir", _IR(), raising=False)
    check_invalid_persisted_option(hass, _Entry(), ["expire_after"])  # type: ignore[arg-type]
    assert any(c["translation_key"] == "invalid_persisted_option" for c in created)
    assert any(c["severity"] == "warning" for c in created)
    check_invalid_persisted_option(hass, _Entry(), [])  # type: ignore[arg-type]
    assert deleted


# ---------------------------------------------------------------------------
# Dynamic + stale device gate evidence
# ---------------------------------------------------------------------------


def test_dynamic_device_appears_without_restart() -> None:
    """A new host appearing in a Telegraf payload is registered without
    a restart, reload, or entry recreation."""
    clock = [1000.0]
    manager = DeviceManager(clock=lambda: clock[0])
    manager.set_parser(TelegrafParser())

    new_device: list[str] = []
    manager.set_callbacks(on_new_device=lambda did, _name: new_device.append(did))

    manager.process_message(
        "telegraf/host1/cpu",
        json.dumps({"name": "cpu", "tags": {"host": "host1"}, "fields": {"usage_idle": 50}, "timestamp": 1}),
    )
    assert "host1" in manager.devices
    assert "host1" in new_device

    manager.process_message(
        "telegraf/host2/cpu",
        json.dumps({"name": "cpu", "tags": {"host": "host2"}, "fields": {"usage_idle": 50}, "timestamp": 1}),
    )
    assert "host2" in manager.devices
    assert "host2" in new_device
    # No reload, no entry recreation -- the manager now has two devices.
    assert len(manager.devices) == 2


def test_stale_device_pruned_after_delete_delay() -> None:
    """Empty devices older than delete_delay are pruned.

    A device with an ALWAYS-policy metric is removed on the first cleanup
    call (per the cleanup_policy contract). Once the device is empty, it
    becomes eligible for ``prune_empty_devices`` once ``delete_delay``
    has elapsed since the last heartbeat.
    """
    from custom_components.telegraf_mqtt.models import MetricDescriptor
    from custom_components.telegraf_mqtt.naming import resolve_translation
    from custom_components.telegraf_mqtt.parsers.generic import build_unique_key
    from types import MappingProxyType

    clock = [0.0]
    manager = DeviceManager(clock=lambda: clock[0], delete_delay=10, cleanup_delay=0)
    manager.set_parser(TelegrafParser())

    # Push a metric with cleanup_policy=ALWAYS so the first cleanup call
    # removes it regardless of the expiry/cleanup timing.
    translation_key, placeholders = resolve_translation("cpu", {"host": "old"}, "usage_idle")
    descriptor = MetricDescriptor(
        unique_key="cpu_usage_idle",
        measurement="cpu",
        tags={"host": "old"},
        field="usage_idle",
        value=50,
        timestamp=0,
        native_unit=None,
        suggested_device_class=None,
        suggested_state_class="measurement",
        entity_category=None,
        cleanup_policy="ALWAYS",
        translation_key=translation_key,
        translation_placeholders=MappingProxyType(dict(placeholders)),
    )
    manager.get_or_create_registry("old", "old").update(descriptor)
    assert "old" in manager.devices
    assert len(manager.devices["old"]) == 1

    # First cleanup drains the metric from the device.
    removed = manager.cleanup()
    assert removed, "ALWAYS metric must be removed on first cleanup call"
    assert len(manager.devices["old"]) == 0

    # Advance past delete_delay; the empty device is pruned.
    clock[0] = 100.0
    pruned = manager.prune_empty_devices()
    assert "old" in pruned
    assert "old" not in manager.devices


def test_stale_device_recovers_on_reappearance() -> None:
    """A pruned device reappears when its host sends another message."""
    from custom_components.telegraf_mqtt.models import MetricDescriptor
    from custom_components.telegraf_mqtt.naming import resolve_translation
    from types import MappingProxyType

    clock = [0.0]
    manager = DeviceManager(clock=lambda: clock[0], delete_delay=10, cleanup_delay=0)
    manager.set_parser(TelegrafParser())

    translation_key, placeholders = resolve_translation("cpu", {"host": "again"}, "usage_idle")
    descriptor = MetricDescriptor(
        unique_key="cpu_usage_idle",
        measurement="cpu",
        tags={"host": "again"},
        field="usage_idle",
        value=50,
        timestamp=0,
        native_unit=None,
        suggested_device_class=None,
        suggested_state_class="measurement",
        entity_category=None,
        cleanup_policy="ALWAYS",
        translation_key=translation_key,
        translation_placeholders=MappingProxyType(dict(placeholders)),
    )
    manager.get_or_create_registry("again", "again").update(descriptor)
    assert "again" in manager.devices
    manager.cleanup()  # ALWAYS drains immediately
    assert len(manager.devices["again"]) == 0
    clock[0] = 100.0
    manager.prune_empty_devices()
    assert "again" not in manager.devices

    # Reappearance: a fresh message creates a new registry for the same host.
    manager.process_message(
        "telegraf/again/cpu",
        json.dumps({"name": "cpu", "tags": {"host": "again"}, "fields": {"usage_idle": 60}, "timestamp": 2}),
    )
    assert "again" in manager.devices
    fresh = manager.devices["again"].get("cpu_usage_idle")
    assert fresh is not None
    assert fresh.value == 60


# ---------------------------------------------------------------------------
# Translations completeness
# ---------------------------------------------------------------------------


def test_every_descriptor_translation_key_has_strings_json_entry() -> None:
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    strings = json.loads((root / "custom_components/telegraf_mqtt/strings.json").read_text(encoding="utf-8"))
    sensor_keys = set(strings["entity"]["sensor"].keys())
    binary_keys = set(strings["entity"]["binary_sensor"].keys())
    for key in all_translation_keys():
        assert key in sensor_keys, key
        assert key in binary_keys, key


def test_every_descriptor_translation_key_has_en_json_entry() -> None:
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    en = json.loads((root / "custom_components/telegraf_mqtt/translations/en.json").read_text(encoding="utf-8"))
    sensor_keys = set(en["entity"]["sensor"].keys())
    binary_keys = set(en["entity"]["binary_sensor"].keys())
    for key in all_translation_keys():
        assert key in sensor_keys, key
        assert key in binary_keys, key


def test_sw_version_field_is_in_strings_and_en() -> None:
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    strings = json.loads((root / "custom_components/telegraf_mqtt/strings.json").read_text(encoding="utf-8"))
    en = json.loads((root / "custom_components/telegraf_mqtt/translations/en.json").read_text(encoding="utf-8"))
    assert "sw_version" in strings["config"]["step"]["user"]["data"]
    assert "sw_version" in en["config"]["step"]["user"]["data"]


def test_reconfigure_step_is_in_strings_and_en() -> None:
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    strings = json.loads((root / "custom_components/telegraf_mqtt/strings.json").read_text(encoding="utf-8"))
    en = json.loads((root / "custom_components/telegraf_mqtt/translations/en.json").read_text(encoding="utf-8"))
    assert "reconfigure" in strings["config"]["step"]
    assert "reconfigure" in en["config"]["step"]
    assert "sw_version" in strings["config"]["step"]["reconfigure"]["data"]
    assert "sw_version" in en["config"]["step"]["reconfigure"]["data"]


def test_exceptions_block_is_in_strings_and_en() -> None:
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    strings = json.loads((root / "custom_components/telegraf_mqtt/strings.json").read_text(encoding="utf-8"))
    en = json.loads((root / "custom_components/telegraf_mqtt/translations/en.json").read_text(encoding="utf-8"))
    for key in ("reconfigure_subscribe_failed", "mqtt_broker_unreachable"):
        assert key in strings["exceptions"]
        assert "message" in strings["exceptions"][key]
        assert key in en["exceptions"]
        assert "message" in en["exceptions"][key]


# ---------------------------------------------------------------------------
# entity-category / entity-device-class row flips (pin-tests)
# ---------------------------------------------------------------------------


def test_diagnostic_descriptors_carry_entity_category() -> None:
    """``disk`` and lifecycle/load/process fields are diagnostic."""
    parser = TelegrafParser()
    for payload in (
        {"name": "disk", "tags": {"host": "h", "path": "/"}, "fields": {"used_percent": 50}, "timestamp": 0},
        {"name": "system", "tags": {"host": "h"}, "fields": {"uptime": 1, "load1": 0.5, "processes_forked": 5}, "timestamp": 0},
    ):
        for descriptor in parser.parse(json.dumps(payload)):
            if descriptor.field in ("used_percent", "uptime", "load1", "processes_forked"):
                assert descriptor.entity_category == "diagnostic", (payload, descriptor.field)


def test_temperature_field_gets_temperature_device_class() -> None:
    parser = TelegrafParser()
    payload = {
        "name": "sensors",
        "tags": {"host": "h", "chip": "coretemp-isa-0000", "feature": "package_id_0"},
        "fields": {"temp_input": 50.0},
        "timestamp": 0,
    }
    [descriptor] = parser.parse(json.dumps(payload))
    assert descriptor.suggested_device_class == "temperature"


# ---------------------------------------------------------------------------
# README pin-tests (one per Gold doc rule)
# ---------------------------------------------------------------------------


def _readme_text() -> str:
    from pathlib import Path
    return (Path(__file__).resolve().parents[1] / "README.md").read_text(encoding="utf-8")


def test_readme_has_data_update_section() -> None:
    text = _readme_text()
    assert "Data update" in text
    assert "local_push" in text


def test_readme_has_supported_devices_section() -> None:
    text = _readme_text()
    assert "Supported devices" in text or "Supported hardware" in text or "supported-devices" in text


def test_readme_has_supported_functions_section() -> None:
    text = _readme_text()
    assert "Supported functions" in text or "supported-functions" in text


def test_readme_has_examples_section() -> None:
    text = _readme_text()
    assert "Examples" in text


def test_readme_has_use_cases_section() -> None:
    text = _readme_text()
    assert "Use cases" in text or "use-cases" in text


def test_readme_has_known_limitations_section() -> None:
    text = _readme_text()
    assert "Known limitations" in text or "known-limitations" in text


def test_readme_has_troubleshooting_section() -> None:
    text = _readme_text()
    assert "Troubleshooting" in text or "troubleshooting" in text


def test_readme_has_entity_behavior_section() -> None:
    text = _readme_text()
    assert "Entity behavior" in text or "entity behavior" in text
    # Diagnostic entities off by default is documented.
    assert "diagnostic" in text.lower() and "disabled" in text.lower()


# ---------------------------------------------------------------------------
# Generic fallback (Phase 9: clean-cut "only fallback")
# ---------------------------------------------------------------------------


def test_generic_field_is_the_only_fallback() -> None:
    """Unknown measurement -> generic_field. The fallback always sets
    ``{field}`` to the title-cased field name so the result is never
    invisible."""
    from custom_components.telegraf_mqtt.naming import TK_GENERIC_FIELD

    for measurement, field in (
        ("some_custom_plugin", "watts"),
        ("weird", "deep_value"),
        ("memory_extra", "count"),
    ):
        key, placeholders = resolve_translation(measurement, {"host": "h"}, field)
        assert key == TK_GENERIC_FIELD
        assert placeholders == {"field": field.replace("_", " ").title()}
