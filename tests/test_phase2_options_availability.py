"""HA-harness tests for Phase 2: options & availability exit criteria.

Uses the real pytest-homeassistant-custom-component harness because these exit
criteria concern Home Assistant entity states and the config-entry update
listener path. Metrics flow through the real MQTT transport
(``async_fire_mqtt_message``); expiry runs on the production periodic timer.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
)

from custom_components.telegraf_mqtt.const import (
    CONF_DEVICE_NAME,
    CONF_EXCLUDE_PATTERNS,
    CONF_EXPIRE_AFTER,
    CONF_TOPIC_PATTERN,
    DOMAIN,
)

# Harness-environmental only: see tests/test_harness_devices.py — the plugin's
# mocked paho client leaks its misc timer past teardown for any test that opens
# an MQTT subscription; this parametrization is the sanctioned opt-out.
pytestmark = [pytest.mark.parametrize("expected_lingering_timers", [True])]


def _payload(host: str, measurement: str, fields: dict) -> str:
    return json.dumps(
        {
            "name": measurement,
            "tags": {"host": host},
            "fields": fields,
            "timestamp": 1700000000,
        }
    )


def _unique_id(host: str, unique_key: str) -> str:
    return f"{DOMAIN}_{host}_{unique_key}"


async def _setup_entry(hass: HomeAssistant, mqtt_mock, options: dict | None = None) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Telegraf",
        data={
            CONF_TOPIC_PATTERN: "telegraf/#",
            CONF_DEVICE_NAME: "Telegraf",
        },
        options=options or {},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _feed(hass: HomeAssistant, host: str, measurement: str, fields: dict) -> None:
    """Publish one Telegraf message through the real MQTT transport and settle."""
    async_fire_mqtt_message(hass, "telegraf/data", _payload(host, measurement, fields))
    await hass.async_block_till_done()


def _entity_id_for(hass: HomeAssistant, unique_id: str) -> str:
    entity_registry = er.async_get(hass)
    return next(e.entity_id for e in entity_registry.entities.values() if e.unique_id == unique_id)


def _domain_entity_ids(hass: HomeAssistant) -> set[str]:
    return {e.entity_id for e in er.async_get(hass).entities.values() if e.platform == DOMAIN}


async def test_exclude_pattern_option_prevents_entity_creation(hass, mqtt_mock) -> None:
    """Exit criterion: excluded metrics never become entities; others still do."""
    await _setup_entry(hass, mqtt_mock, options={CONF_EXCLUDE_PATTERNS: ["mem_*"]})

    await _feed(hass, "server07", "mem", {"used_percent": 41.2})
    assert _domain_entity_ids(hass) == set(), "excluded metric must not create an entity"

    await _feed(hass, "server07", "cpu", {"usage_idle": 88.4})
    expected = _unique_id("server07", "cpu_usage_idle")
    assert _domain_entity_ids(hass) == {_entity_id_for(hass, expected)}
    assert hass.states.get(_entity_id_for(hass, expected)).state == "88.4"


async def test_entity_expires_unavailable_then_recovers_on_next_message(hass, mqtt_mock) -> None:
    """Exit criterion: unavailable after expire_after; immediate recovery on next message."""
    await _setup_entry(hass, mqtt_mock, options={CONF_EXPIRE_AFTER: 1})
    await _feed(hass, "server08", "mem", {"used_percent": 41.2})

    entity_id = _entity_id_for(hass, _unique_id("server08", "mem_used_percent"))
    assert hass.states.get(entity_id).state == "41.2"

    # Production expiry loop ticks every min(expire_after, 30)s = 1s here; wait
    # past two ticks so the monotonic-based last_updated genuinely ages out.
    await asyncio.sleep(2.2)

    assert hass.states.get(entity_id).state == "unavailable"

    await _feed(hass, "server08", "mem", {"used_percent": 42.0})
    assert hass.states.get(entity_id).state == "42.0"


async def test_options_apply_live_without_reload(hass, mqtt_mock) -> None:
    """Exit criterion: option changes take effect immediately, never deleting entities."""
    entry = await _setup_entry(hass, mqtt_mock)
    await _feed(hass, "server09", "mem", {"used_percent": 41.2})
    entity_id = _entity_id_for(hass, _unique_id("server09", "mem_used_percent"))
    assert hass.states.get(entity_id).state == "41.2"

    # Live change: excluding an existing metric marks it unavailable — but the
    # entity itself stays registered (never deleted).
    hass.config_entries.async_update_entry(
        entry,
        options={CONF_EXCLUDE_PATTERNS: ["mem_*"], CONF_EXPIRE_AFTER: 30},
    )
    await hass.async_block_till_done()

    assert hass.states.get(entity_id).state == "unavailable"
    assert er.async_get(hass).async_get(entity_id) is not None

    # Lifting the exclusion is equally live; the next message recovers the value.
    hass.config_entries.async_update_entry(entry, options={})
    await hass.async_block_till_done()
    await _feed(hass, "server09", "mem", {"used_percent": 44.0})

    assert hass.states.get(entity_id).state == "44.0"
