import asyncio
import sys
import types


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
        _configured_ids: set[str] = set()

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


def test_config_flow_rejects_duplicate_topic_pattern(monkeypatch) -> None:
    _install_fake_homeassistant(monkeypatch)

    from custom_components.telegraf_mqtt.config_flow import TelegrafMqttConfigFlow

    first_flow = TelegrafMqttConfigFlow()
    first_result = asyncio.run(
        first_flow.async_step_user(
            {
                "topic_pattern": "telegraf/#",
                "device_name": "Telegraf MQTT",
            }
        )
    )
    assert first_result["type"] == "create_entry"
    TelegrafMqttConfigFlow._configured_ids.add("telegraf/#")

    second_flow = TelegrafMqttConfigFlow()
    try:
        asyncio.run(
            second_flow.async_step_user(
                {
                    "topic_pattern": "telegraf/#",
                    "device_name": "Telegraf MQTT",
                }
            )
        )
    except RuntimeError as exc:
        assert str(exc) == "already_configured"
    else:
        raise AssertionError("duplicate topic pattern should be rejected")

    form_result = asyncio.run(first_flow.async_step_user(None))
    assert form_result["type"] == "form"
    assert form_result["step_id"] == "user"
