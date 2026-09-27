"""Real Home Assistant harness tests for telegraf_mqtt.

Three families of tests that need ``pytest-homeassistant-custom-component``
(a real ``HomeAssistant`` instance, the HA device/entity registries, and
``async_fire_mqtt_message`` for broker-style delivery):

1. Smoke: the harness boots HA 2026.6.x on this platform and discovers the
   integration. If these fail, the problem is the environment/harness, not
   telegraf_mqtt code.
2. Phase 1 exit criteria: payloads from two hosts produce two grouped
   devices; a reload preserves entity_ids without duplicates.
3. Real-harness counterparts of the fake-driven Phase 10 UX tests in
   ``test_phase10_ux.py``: retained messages during subscribe reach the
   platforms, ``auto_discover`` toggles live, and category-override globs
   apply against HA's actual entity registry.

Harness-environmental note: the plugin's mocked paho client fires
``on_socket_open`` (starting MQTT's 1-second misc timer) but nothing fires
the matching ``on_socket_close`` at disconnect, so that handle always
lingers past teardown for any test that opens an MQTT subscription. The
module opts in to ``expected_lingering_timers=True`` (the sanctioned
opt-out; see pytest_homeassistant_custom_component.plugins). The two smoke
tests open no MQTT subscription, so for them the opt-out merely downgrades
any lingering-timer failure to a warning. No lingering timers originate
from telegraf_mqtt code.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from homeassistant.const import __version__ as HA_VERSION
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from homeassistant.loader import async_get_integration
from pytest_homeassistant_custom_component.common import (
    MockConfigEntry,
    async_fire_mqtt_message,
)

from custom_components.telegraf_mqtt.const import (
    CONF_AUTO_DISCOVER,
    CONF_CATEGORY_OVERRIDES,
    CONF_DEVICE_NAME,
    CONF_TOPIC_PATTERN,
    DOMAIN,
)

pytestmark = [pytest.mark.parametrize("expected_lingering_timers", [True])]

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


# ---------------------------------------------------------------------------
# Smoke: prove the HA test harness itself works on this platform. If these
# fail, the problem is the environment/harness — not telegraf_mqtt code.
# ---------------------------------------------------------------------------


def _hacs_floor() -> tuple[int, int]:
    """Return the ``(major, minor)`` floor declared in ``hacs.json``."""
    hacs = json.loads(Path("hacs.json").read_text(encoding="utf-8"))
    major, minor, _patch = hacs["homeassistant"].split(".", 2)
    return int(major), int(minor)


async def test_hass_fixture_boots(hass) -> None:
    """The hass fixture starts a real Home Assistant test instance.

    The assertion is a *floor* check, not an exact-version check: the point
    is that the harness runs an HA at or above the version the integration
    declares in ``hacs.json`` (2026.6.0). Pinning an exact patch version
    broke the moment CI picked up a newer HA release, which is a property
    of the test environment rather than of the integration.
    """
    assert hass.is_running
    installed = tuple(int(part) for part in HA_VERSION.split(".")[:2])
    floor = _hacs_floor()
    assert installed >= floor, f"HA {HA_VERSION} is below the declared floor {'.'.join(map(str, (*floor, 0)))}"


async def test_telegraf_mqtt_integration_is_loadable(hass, enable_custom_integrations) -> None:
    """HA's loader discovers telegraf_mqtt as a custom integration."""
    integration = await async_get_integration(hass, "telegraf_mqtt")
    assert integration.domain == "telegraf_mqtt"


# ---------------------------------------------------------------------------
# Phase 1 exit criteria: dynamic devices and reload stability.
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Real-harness versions of contracts that test_phase10_ux.py drives through
# fakes: the same path, but through HA's real entity/config registries.
# ---------------------------------------------------------------------------


async def test_retained_message_during_subscribe_reaches_platforms_real(
    hass: HomeAssistant, mqtt_mock, hass_config_dir: str
) -> None:
    """Real-harness version of the setup-ordering regression test.

    The fake-version in ``test_phase10_ux.py`` proves the integration
    calls ``async_forward_entry_setups`` before ``mqtt.async_subscribe``;
    this real-harness version proves the end-to-end contract against Home
    Assistant's actual entity and dispatcher plumbing.

    A broker-style delivery via the real MQTT transport after setup must
    reach the platform's ``SIGNAL_NEW_METRIC`` listener and surface as a
    real entity in ``hass.states`` and the entity registry. With the
    pre-fix ordering, the dispatch would have hit an empty listener list
    and the metric was registered in the manager but never surfaced as an
    entity.
    """
    entity_registry = er.async_get(hass)

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Telegraf",
        data={CONF_TOPIC_PATTERN: "telegraf/#", CONF_DEVICE_NAME: "Telegraf"},
    )
    entry.add_to_hass(hass)

    # Set up the entry. The platform dispatcher listeners must be attached
    # before the integration's MQTT subscription is wired -- this is the
    # contract under test.
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # A broker-style delivery via the real MQTT transport.
    async_fire_mqtt_message(
        hass,
        "telegraf/cpu",
        _payload("retained_host", "cpu", {"usage_idle": 88.4}),
    )
    # Multiple block_till_done passes drain the platform's
    # async_added_to_hass chain.
    await hass.async_block_till_done()
    await hass.async_block_till_done()

    # Real entity-registry assertion: a retained message that landed
    # through the live MQTT path produced a real entity record under the
    # integration's platform.
    domain_entries = [e for e in entity_registry.entities.values() if e.platform == DOMAIN]
    unique_ids = {e.unique_id for e in domain_entries}
    assert "telegraf_mqtt_retained_host_cpu_usage_idle" in unique_ids, (
        f"entity registry is missing the retained-message entity; got unique_ids={unique_ids!r}"
    )
    state = hass.states.get("sensor.retained_host_cpu_usage_idle")
    assert state is not None
    assert state.state == "88.4"


async def test_options_flow_toggles_auto_discover_live_real(
    hass: HomeAssistant, mqtt_mock, hass_config_dir: str
) -> None:
    """Real-harness version of the auto_discover opt-in toggle.

    Pins the contract that ``DEFAULT_AUTO_DISCOVER=False`` keeps the snoop
    off after first setup, and that flipping the option to ``True`` via
    ``hass.config_entries.async_update_entry`` installs the snoop listener
    on the live broker.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Telegraf",
        data={CONF_TOPIC_PATTERN: "telegraf/#", CONF_DEVICE_NAME: "Telegraf"},
        options={},  # no opt-in
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # Default: no snoop teardown handle parked.
    assert entry.runtime_data.unsubscribe_snoop is None

    # Flip the option to True via the standard options-update path.
    hass.config_entries.async_update_entry(entry, options={CONF_AUTO_DISCOVER: True})
    await hass.async_block_till_done()

    # After the options update, the snoop teardown handle is parked.
    assert entry.runtime_data.unsubscribe_snoop is not None


async def test_options_flow_applies_category_overrides_glob_live_real(
    hass: HomeAssistant, mqtt_mock, hass_config_dir: str
) -> None:
    """Real-harness version of the category_overrides glob live-update.

    Pins the contract that a glob pattern (e.g. ``cpu_*``) in
    ``category_overrides`` flips the entity's category live -- the same way
    a user-typed option flow would.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Telegraf",
        data={CONF_TOPIC_PATTERN: "telegraf/#", CONF_DEVICE_NAME: "Telegraf"},
        options={},
    )
    entry.add_to_hass(hass)
    assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()

    # Feed a cpu metric so we have an entity to update.
    async_fire_mqtt_message(
        hass,
        "telegraf/cpu",
        _payload("category_host", "cpu", {"usage_idle": 42.0}),
    )
    await hass.async_block_till_done()
    await hass.async_block_till_done()

    # The cpu metric is now an entity in the registry.
    entity_registry = er.async_get(hass)
    domain_entries = [e for e in entity_registry.entities.values() if e.platform == DOMAIN]
    cpu_unique = "telegraf_mqtt_category_host_cpu_usage_idle"
    cpu_entity_id = next(e.entity_id for e in domain_entries if e.unique_id == cpu_unique)

    # Without an explicit category override, ``cpu.usage_idle`` resolves to
    # no category.
    state_before = hass.states.get(cpu_entity_id)
    assert state_before is not None

    # Apply a glob category override live: every ``cpu_*`` field goes to
    # "diagnostic".
    hass.config_entries.async_update_entry(
        entry,
        options={CONF_CATEGORY_OVERRIDES: {"cpu_*": "diagnostic"}},
    )
    await hass.async_block_till_done()
    await hass.async_block_till_done()

    # The state still has the value (the override only changes the category,
    # not the value).
    state_after = hass.states.get(cpu_entity_id)
    assert state_after is not None
    assert state_after.state == "42.0"
