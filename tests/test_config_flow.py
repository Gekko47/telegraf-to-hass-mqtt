"""Config flow tests for telegraf_mqtt.

Pure helper tests stay harness-free; flow-level tests run under the real HA
harness so the duplicate-topic abort is asserted against Home Assistant's own
flow manager.
"""

from __future__ import annotations

from homeassistant.config_entries import SOURCE_USER
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.telegraf_mqtt.config_flow import (
    _default_device_name,
    _valid_subscription_topic,
)
from custom_components.telegraf_mqtt.const import (
    CONF_DEVICE_NAME,
    CONF_TOPIC_PATTERN,
    DOMAIN,
)


def test_valid_subscription_topic_accepts_and_rejects() -> None:
    assert _valid_subscription_topic("telegraf/#") is True
    assert _valid_subscription_topic("telegraf/+/cpu") is True
    assert _valid_subscription_topic("") is False
    assert _valid_subscription_topic("telegraf/#/cpu") is False  # hash must be last
    assert _valid_subscription_topic("telegraf/#extra") is False  # hash must be alone
    assert _valid_subscription_topic("telegraf/pl+us") is False  # plus must be alone


def test_default_device_name_from_topic() -> None:
    assert _default_device_name("telegraf_host/#") == "Telegraf Host"
    assert _default_device_name("#") == "Telegraf MQTT"


async def test_config_flow_creates_entry(hass) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_TOPIC_PATTERN: "telegraf/#", CONF_DEVICE_NAME: "Telegraf"},
    )
    await hass.async_block_till_done()

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert result["data"][CONF_TOPIC_PATTERN] == "telegraf/#"


async def test_config_flow_rejects_duplicate_topic_pattern(hass) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Telegraf",
        data={CONF_TOPIC_PATTERN: "telegraf/#", CONF_DEVICE_NAME: "Telegraf"},
        unique_id="telegraf/#",
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_TOPIC_PATTERN: "telegraf/#", CONF_DEVICE_NAME: "Telegraf"},
    )

    assert result["type"] == FlowResultType.ABORT
    assert result["reason"] == "already_configured"


async def test_config_flow_rejects_invalid_topic(hass) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_TOPIC_PATTERN: "telegraf/#/bad", CONF_DEVICE_NAME: "Telegraf"},
    )

    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {CONF_TOPIC_PATTERN: "invalid_topic"}


async def test_config_flow_requires_device_name(hass) -> None:
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_TOPIC_PATTERN: "telegraf/#", CONF_DEVICE_NAME: ""},
    )

    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {CONF_DEVICE_NAME: "required"}


async def test_options_flow_saves_user_input(hass) -> None:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Telegraf",
        data={CONF_TOPIC_PATTERN: "telegraf/#", CONF_DEVICE_NAME: "Telegraf"},
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "init"

    result = await hass.config_entries.options.async_configure(result["flow_id"], user_input={})
    await hass.async_block_till_done()

    assert result["type"] == FlowResultType.CREATE_ENTRY
    # An empty submission persists the schema's default values.
    assert isinstance(result["data"], dict)
