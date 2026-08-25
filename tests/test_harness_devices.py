"""HA-harness tests for Phase 1: dynamic devices and reload stability.

These use the real pytest-homeassistant-custom-component harness because the exit
criteria concern Home Assistant's own device/entity registries. Metrics are published
through the real MQTT transport (``async_fire_mqtt_message``) so discovery flows
through the same async MQTT-subscription → dispatcher → platform path as production.
"""

from __future__ import annotations

import json

import pytest
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
)

from custom_components.telegraf_mqtt.const import (
    CONF_DEVICE_NAME,
    CONF_TOPIC_PATTERN,
    DOMAIN,
)

CPU_UNIQUE_ID = "telegraf_mqtt_server01_cpu_usage_idle"
MEM_UNIQUE_ID = "telegraf_mqtt_server02_mem_used_percent"


def _payload(host: str, measurement: str, fields: dict) -> str:
    return json.dumps(
        {
            "name": measurement,
            "tags": {"host": host},
            "fields": fields,
            "timestamp": 1700000000,
        }
    )


async def _setup_entry(hass: HomeAssistant, mqtt_mock) -> MockConfigEntry:
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Telegraf",
        data={
            CONF_TOPIC_PATTERN: "telegraf/#",
            CONF_DEVICE_NAME: "Telegraf",
        },
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    return entry


async def _feed(hass, entry, host: str, measurement: str, fields: dict) -> None:
    """Publish one Telegraf message through the real MQTT transport and settle.

    Uses ``async_fire_mqtt_message`` so discovery flows through the same
    async MQTT-subscription → dispatcher → platform path as production,
    instead of calling into the manager directly from the test coroutine.
    """
    async_fire_mqtt_message(hass, "telegraf/data", _payload(host, measurement, fields))
    await hass.async_block_till_done()


def _domain_entity_entries(hass: HomeAssistant) -> dict:
    entity_registry = er.async_get(hass)
    return {e.unique_id: e for e in entity_registry.entities.values() if e.platform == DOMAIN}


# Harness-environmental only: the plugin's mocked paho client fires on_socket_open
# (starting MQTT's 1-second misc timer) but nothing fires the matching
# on_socket_close at disconnect, so that handle always lingers past teardown.
# See pytest_homeassistant_custom_component plugins.py — this parametrization is
# its sanctioned opt-out. No lingering timers originate from telegraf_mqtt code.
pytestmark = [pytest.mark.parametrize("expected_lingering_timers", [True])]


async def test_two_hosts_produce_two_grouped_devices(hass: HomeAssistant, mqtt_mock) -> None:
    """Exit criterion: payloads from two hosts produce two correctly-grouped devices."""
    entry = await _setup_entry(hass, mqtt_mock)
    await _feed(hass, entry, "server01", "cpu", {"usage_idle": 88.4})
    await _feed(hass, entry, "server02", "mem", {"used_percent": 41.2})

    device_registry = dr.async_get(hass)
    device_a = device_registry.async_get_device(identifiers={(DOMAIN, "server01")})
    device_b = device_registry.async_get_device(identifiers={(DOMAIN, "server02")})
    assert device_a is not None
    assert device_b is not None

    entries = _domain_entity_entries(hass)
    assert set(entries) == {CPU_UNIQUE_ID, MEM_UNIQUE_ID}
    assert entries[CPU_UNIQUE_ID].device_id == device_a.id
    assert entries[MEM_UNIQUE_ID].device_id == device_b.id

    assert hass.states.get(entries[CPU_UNIQUE_ID].entity_id).state == "88.4"
    assert hass.states.get(entries[MEM_UNIQUE_ID].entity_id).state == "41.2"


async def test_reload_preserves_entity_ids_without_duplicates(hass: HomeAssistant, mqtt_mock) -> None:
    """Exit criterion: restarting the entry creates no duplicates and keeps entity_ids."""
    entry = await _setup_entry(hass, mqtt_mock)
    await _feed(hass, entry, "server01", "cpu", {"usage_idle": 1700000000})

    before = {uid: e.entity_id for uid, e in _domain_entity_entries(hass).items()}
    assert CPU_UNIQUE_ID in before

    assert await hass.config_entries.async_reload(entry.entry_id)
    await hass.async_block_till_done()

    # Metrics arrive again after the "restart".
    await _feed(hass, entry, "server01", "cpu", {"usage_idle": 11.5})

    after = {uid: e.entity_id for uid, e in _domain_entity_entries(hass).items()}
    assert set(after) == set(before), "reload must not orphan or duplicate registry entries"
    assert all(after[uid] == entity_id for uid, entity_id in before.items())
    state = hass.states.get(before[CPU_UNIQUE_ID])
    assert state is not None
    assert state.state == "11.5"
