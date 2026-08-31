import enum
import importlib
import json
import sys
import types
from dataclasses import dataclass
from pathlib import Path
from typing import ClassVar

from custom_components.telegraf_mqtt.models import MetricDescriptor
from custom_components.telegraf_mqtt.registry import DeviceManager


def _install_fake_homeassistant(monkeypatch) -> None:
    """Install the minimal HA stubs needed for import-time package resolution."""

    homeassistant = types.ModuleType("homeassistant")
    config_entries = types.ModuleType("homeassistant.config_entries")
    data_entry_flow = types.ModuleType("homeassistant.data_entry_flow")
    core = types.ModuleType("homeassistant.core")
    const = types.ModuleType("homeassistant.const")
    voluptuous = types.ModuleType("voluptuous")

    class ConfigEntry:
        def __init__(self) -> None:
            self.runtime_data = None
            self.entry_id = "test-entry"

    class FakeOptionsFlow:
        def __init__(self, config_entry=None) -> None:
            self.config_entry = config_entry

        def async_show_form(self, step_id: str, data_schema, errors=None) -> dict[str, object]:
            return {"type": "form", "step_id": step_id, "data_schema": data_schema, "errors": errors or {}}

        def async_create_entry(self, title: str, data: dict[str, object]) -> dict[str, object]:
            return {"type": "create_entry", "title": title, "data": data}

    class FakeConfigFlow:
        VERSION = 1
        _configured_ids: ClassVar[set[str]] = set()

        def __init_subclass__(cls, **kwargs) -> None:
            return super().__init_subclass__()

        def __init__(self, *args, **kwargs) -> None:
            self._unique_id = None

        async def async_set_unique_id(self, unique_id: str) -> None:
            self._unique_id = unique_id

        def _abort_if_unique_id_configured(self) -> None:
            if self._unique_id in self.__class__._configured_ids:
                raise RuntimeError("already_configured")

        def async_create_entry(self, title: str, data: dict[str, str]) -> dict[str, object]:
            return {"type": "create_entry", "title": title, "data": data}

        def async_show_form(self, step_id: str, data_schema, errors=None) -> dict[str, object]:
            return {"type": "form", "step_id": step_id, "data_schema": data_schema, "errors": errors or {}}

        def add_suggested_values_to_schema(self, schema, values):
            return schema

    class _Schema(dict):
        def __init__(self, schema: dict) -> None:
            super().__init__(schema)
            self.schema = schema

    class _Required:
        def __init__(self, key: str, default: str | None = None) -> None:
            self.key = key
            self.default = default

    class _Platform:
        SENSOR = "sensor"
        BINARY_SENSOR = "binary_sensor"

    class HomeAssistant:
        pass

    def callback(func):
        return func

    def _required(key: str, default: str | None = None):
        return _Required(key, default)

    def _optional(key: str, default=None):
        return _Required(key, default)

    voluptuous.Required = _required
    voluptuous.Optional = _optional
    voluptuous.Schema = _Schema
    const.Platform = _Platform
    core.HomeAssistant = HomeAssistant
    core.callback = callback
    config_entries.ConfigEntry = ConfigEntry
    config_entries.ConfigFlow = FakeConfigFlow
    config_entries.OptionsFlow = FakeOptionsFlow
    data_entry_flow.FlowResult = dict

    homeassistant.config_entries = config_entries
    homeassistant.const = const
    homeassistant.core = core

    monkeypatch.setitem(sys.modules, "homeassistant", homeassistant)
    monkeypatch.setitem(sys.modules, "homeassistant.config_entries", config_entries)
    monkeypatch.setitem(sys.modules, "homeassistant.data_entry_flow", data_entry_flow)
    monkeypatch.setitem(sys.modules, "homeassistant.core", core)
    monkeypatch.setitem(sys.modules, "homeassistant.const", const)
    monkeypatch.setitem(sys.modules, "voluptuous", voluptuous)


def _install_binary_sensor_homeassistant_stubs(monkeypatch) -> None:
    components = types.ModuleType("homeassistant.components")
    binary_sensor = types.ModuleType("homeassistant.components.binary_sensor")
    sensor = types.ModuleType("homeassistant.components.sensor")
    config_entries = types.ModuleType("homeassistant.config_entries")
    const = types.ModuleType("homeassistant.const")
    core = types.ModuleType("homeassistant.core")
    device_registry = types.ModuleType("homeassistant.helpers.device_registry")
    dispatcher = types.ModuleType("homeassistant.helpers.dispatcher")
    entity_platform = types.ModuleType("homeassistant.helpers.entity_platform")
    helpers = types.ModuleType("homeassistant.helpers")

    class BinarySensorEntity:
        def __init__(self) -> None:
            self._attr_is_on = None

    class SensorEntity:
        pass

    class ConfigEntry:
        pass

    class HomeAssistant:
        pass

    class DeviceInfo(dict):
        pass

    class UnitOfTemperature:
        CELSIUS = "°C"

    class StubEntityCategory(enum.StrEnum):
        CONFIG = "config"
        DIAGNOSTIC = "diagnostic"

    def callback(func):
        return func

    def async_dispatcher_connect(hass, signal, target):
        return lambda: None

    def add_entities(entities) -> None:
        return None

    binary_sensor.BinarySensorEntity = BinarySensorEntity
    sensor.SensorEntity = SensorEntity
    config_entries.ConfigEntry = ConfigEntry
    const.UnitOfTemperature = UnitOfTemperature
    const.EntityCategory = StubEntityCategory
    core.HomeAssistant = HomeAssistant
    core.callback = callback
    device_registry.DeviceInfo = DeviceInfo
    dispatcher.async_dispatcher_connect = async_dispatcher_connect
    entity_platform.AddEntitiesCallback = add_entities

    monkeypatch.setitem(sys.modules, "homeassistant.components", components)
    monkeypatch.setitem(sys.modules, "homeassistant.components.binary_sensor", binary_sensor)
    monkeypatch.setitem(sys.modules, "homeassistant.components.sensor", sensor)
    monkeypatch.setitem(sys.modules, "homeassistant.config_entries", config_entries)
    monkeypatch.setitem(sys.modules, "homeassistant.const", const)
    monkeypatch.setitem(sys.modules, "homeassistant.core", core)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers", helpers)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.device_registry", device_registry)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.dispatcher", dispatcher)
    monkeypatch.setitem(sys.modules, "homeassistant.helpers.entity_platform", entity_platform)


@dataclass
class RuntimeData:
    manager: DeviceManager
    manufacturer: str | None = None
    model: str | None = None
    sw_version: str | None = None


@dataclass
class Entry:
    runtime_data: RuntimeData
    entry_id: str = "entry-1"


def test_boolean_metric_is_exposed_as_binary_sensor(monkeypatch) -> None:
    _install_fake_homeassistant(monkeypatch)
    _install_binary_sensor_homeassistant_stubs(monkeypatch)

    binary_sensor_module = importlib.import_module("custom_components.telegraf_mqtt.binary_sensor")
    manager = DeviceManager()
    registry = manager.get_or_create_registry("host1", "host1")
    registry.update(
        MetricDescriptor(
            unique_key="link_up",
            measurement="net",
            tags={"host": "host1", "interface": "wlan0"},
            field="link_up",
            value=True,
            timestamp=1721664000,
            native_unit=None,
            suggested_device_class=None,
            suggested_state_class=None,
            entity_category=None,
        )
    )
    entry = Entry(RuntimeData(manager=manager))
    entity = binary_sensor_module.TelegrafMqttBinarySensor(entry, "host1:link_up")

    assert entity.is_on is True
    assert entity.available is True
    assert entity._attr_unique_id == "telegraf_mqtt_host1_link_up"


def test_manifest_and_translations_are_release_ready() -> None:
    manifest_path = Path("custom_components/telegraf_mqtt/manifest.json")
    strings_path = Path("custom_components/telegraf_mqtt/strings.json")
    translations_path = Path("custom_components/telegraf_mqtt/translations/en.json")

    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    strings = json.loads(strings_path.read_text(encoding="utf-8"))
    translations = json.loads(translations_path.read_text(encoding="utf-8"))

    assert manifest["domain"] == "telegraf_mqtt"
    assert manifest["name"] == "Telegraf MQTT"
    assert manifest["codeowners"] == ["@Gekko47"]
    assert manifest["documentation"].endswith("telegraf-to-hass-mqtt")
    assert manifest["issue_tracker"].endswith("issues")
    assert "homeassistant" not in manifest
    # The device-metadata fields moved to ``manual_topic`` when the two-path
    # config flow landed; ``user`` is now the mode picker. Both are pinned.
    assert strings["config"]["step"]["manual_topic"]["data"]["topic_pattern"] == "MQTT topic pattern"
    assert strings["config"]["step"]["user"]["data"]["setup_mode"] == "Setup mode"
    assert translations["config"]["step"]["options"]["title"] == "Configure options"


def test_hacs_packaging_metadata_is_release_ready() -> None:
    hacs_path = Path("hacs.json")
    manifest_path = Path("custom_components/telegraf_mqtt/manifest.json")

    hacs = json.loads(hacs_path.read_text(encoding="utf-8"))
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))

    assert hacs["name"] == "Telegraf MQTT"
    assert hacs["homeassistant"] == "2026.6.0"
    assert hacs["content_in_root"] is False
    assert manifest["version"] != "0.0.0"


def test_readme_documents_install_and_configuration() -> None:
    readme = Path("README.md").read_text(encoding="utf-8")

    assert "HACS" in readme
    assert "MQTT" in readme
    assert "telegraf_mqtt" in readme
    assert "Install" in readme
    assert "Configure" in readme


def test_repository_has_diagnostics_and_changelog() -> None:
    assert Path("custom_components/telegraf_mqtt/diagnostics.py").exists()
    assert Path("CHANGELOG.md").exists()


def test_repository_has_hacs_branding_assets() -> None:
    assert Path("icon.png").exists()
    assert Path("logo.png").exists()
    assert Path("custom_components/telegraf_mqtt/brand/icon.png").exists()
    assert Path("custom_components/telegraf_mqtt/brand/logo.png").exists()


def test_readme_documents_xdist_fast_path() -> None:
    """README must point local devs at the pytest-xdist fast invocation.

    Phase 10 cost-cut: 390 tests, sequential + coverage = ~2:05 on the dev
    box; with ``-n auto`` that drops to ~0:52 (≈2.5x). The CI gate keeps the
    sequential run for deterministic coverage output, so the README is the
    only place this discovery surface lives.
    """
    readme = Path("README.md").read_text(encoding="utf-8")
    assert "pytest -n auto" in readme
    assert "pytest-xdist" in readme


def test_pyproject_documents_xdist_invocation() -> None:
    """``pyproject.toml`` must keep the recommended invocations in sync.

    The ``[tool.pytest.ini_options]`` table is the canonical place future
    maintainers look when investigating a slow CI run. If the section
    silently drops the xdist example, this test fails.
    """
    text = Path("pyproject.toml").read_text(encoding="utf-8")
    assert "[tool.pytest.ini_options]" in text
    assert "pytest -n auto" in text
    assert 'asyncio_mode = "auto"' in text


def test_placeholder_binary_sensor_stubs_are_self_contained(monkeypatch) -> None:
    """Regression: the placeholder HA stubs must not depend on sibling tests.

    Under sequential ``pytest`` the ``homeassistant.const`` module picks up
    ``EntityCategory`` from whichever test ran first. Under ``pytest -n auto``
    the modules are isolated per worker, so any missing name in
    ``_install_binary_sensor_homeassistant_stubs`` surfaces as an
    ``ImportError`` only on sharded runs. This test pins the
    self-contained shape so the bug never regresses.
    """
    # Run the placeholder helper against an isolated ``sys.modules`` slice so
    # the assertions below observe exactly what it registered, not whatever
    # an earlier sibling test (or the conftest helper) left behind. We
    # snapshot every ``homeassistant.*`` name the helper might touch,
    # actively evict them from ``sys.modules`` for the duration of the test,
    # and restore them on exit so we never leak stubs into harness-based
    # tests that run afterwards. Without the eviction, names the real HA
    # package already populated (entity_platform, const, ...) would mask a
    # missing ``monkeypatch.setitem`` in the helper.
    expected_module_names = (
        "homeassistant.components",
        "homeassistant.components.binary_sensor",
        "homeassistant.components.sensor",
        "homeassistant.config_entries",
        "homeassistant.const",
        "homeassistant.core",
        "homeassistant.helpers",
        "homeassistant.helpers.device_registry",
        "homeassistant.helpers.dispatcher",
        "homeassistant.helpers.entity_platform",
    )
    saved_modules = {name: sys.modules.pop(name, None) for name in expected_module_names}
    try:
        _install_binary_sensor_homeassistant_stubs(monkeypatch)

        for name in expected_module_names:
            assert name in sys.modules, (
                f"_install_binary_sensor_homeassistant_stubs() failed to "
                f"register {name!r}; re-add the monkeypatch.setitem call so "
                f"the suite stays xdist-safe."
            )

        # The specific attributes that broke when xdist sharded the suite:
        # the helper must define them on the modules it registers, not rely
        # on a previous test (or the conftest helper) having populated them.
        const_module = sys.modules["homeassistant.const"]
        entity_platform_module = sys.modules["homeassistant.helpers.entity_platform"]
        assert hasattr(const_module, "EntityCategory"), (
            "homeassistant.const is missing EntityCategory; "
            "_install_binary_sensor_homeassistant_stubs() must set it so "
            "binary_sensor.py can resolve the import under pytest-xdist."
        )
        assert hasattr(entity_platform_module, "AddEntitiesCallback"), (
            "homeassistant.helpers.entity_platform is missing "
            "AddEntitiesCallback; _install_binary_sensor_homeassistant_stubs() "
            "must set it so binary_sensor.py can resolve the import under "
            "pytest-xdist."
        )

        # Secondary guard: the canonical conftest helper is the reference
        # for everything else sensor.py / binary_sensor.py import. Keep the
        # cross-check so adding a name to the placeholder helper without
        # updating conftest still surfaces.
        from conftest import _build_ha_stub_modules  # type: ignore[import-not-found]

        placeholder_names = set(expected_module_names)
        canonical = _build_ha_stub_modules()
        missing = placeholder_names - set(canonical)
        assert not missing, (
            f"Placeholder test_placeholder.py binary-sensor stubs are missing "
            f"these modules from the canonical conftest helper: {sorted(missing)}. "
            f"Re-add them so the suite stays xdist-safe."
        )
    finally:
        # Restore the pre-test sys.modules state so we never leak the
        # placeholder stubs into harness-based tests that run afterwards.
        for name, saved in saved_modules.items():
            if saved is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = saved
