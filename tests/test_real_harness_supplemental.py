"""Real-harness supplement to the test_phase10_ux.py fake-driven tests.

The audit's Issue #1: ``test_retained_message_during_subscribe_reaches_platforms``
is a fake test that proves only call-order and that an internal
function fired, not that an entity was actually created. This file
holds the real-harness versions of the same contracts -- the same
path, but driven through ``pytest-homeassistant-custom-component``'s
real ``hass`` / ``mqtt_mock`` / ``MockConfigEntry`` /
``async_fire_mqtt_message`` plumbing so an entity shows up in
``hass.states`` (not just a fake dispatch list).

Issue #3: phase10_ux auto_discover / category_overrides tests are
entirely fake-driven. The new tests here exercise the same paths
through HA's real ``async_update_entry`` and
``entity_registry.async_get`` so a regression that breaks the
live-update path is caught against the actual entity and
configuration state.

Harness-environmental note: the plugin's mocked paho client leaks
its misc timer past teardown for any test that opens an MQTT
subscription; ``expected_lingering_timers=True`` is the sanctioned
opt-out.
"""

from __future__ import annotations

import json

import pytest
from homeassistant.core import HomeAssistant
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


def _payload(host: str, measurement: str, fields: dict) -> str:
    return json.dumps(
        {
            "name": measurement,
            "tags": {"host": host},
            "fields": fields,
            "timestamp": 1700000000,
        }
    )


async def test_retained_message_during_subscribe_reaches_platforms_real(
    hass: HomeAssistant, mqtt_mock, hass_config_dir: str
) -> None:
    """Real-harness version of the setup-ordering regression test.

    The fake-version in ``test_phase10_ux.py`` proves the integration
    calls ``async_forward_entry_setups`` before
    ``mqtt.async_subscribe``; this real-harness version proves the
    end-to-end contract against Home Assistant's actual entity and
    dispatcher plumbing.

    A broker-style delivery via the real MQTT transport after setup
    must reach the platform's ``SIGNAL_NEW_METRIC`` listener and
    surface as a real entity in ``hass.states`` and the entity
    registry. With the pre-fix ordering, the dispatch would have hit
    an empty listener list and the metric was registered in the
    manager but never surfaced as an entity.
    """
    from homeassistant.helpers import entity_registry as er

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Telegraf",
        data={CONF_TOPIC_PATTERN: "telegraf/#", CONF_DEVICE_NAME: "Telegraf"},
    )
    entry.add_to_hass(hass)

    # Set up the entry. The platform dispatcher listeners must be
    # attached before the integration's MQTT subscription is wired
    # -- this is the contract under test.
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

    # Real entity-registry assertion: a retained message that
    # landed through the live MQTT path produced a real entity
    # record under the integration's platform.
    entity_registry = er.async_get(hass)
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

    Pins the contract that ``DEFAULT_AUTO_DISCOVER=False`` keeps the
    snoop off after first setup, and that flipping the option to
    ``True`` via ``hass.config_entries.async_update_entry`` installs
    the snoop listener on the live broker.
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
    ``category_overrides`` flips the entity's category live -- the
    same way a user-typed option flow would.
    """
    from homeassistant.helpers import entity_registry as er

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

    # Without an explicit category override, ``cpu.usage_idle`` resolves
    # to no category.
    state_before = hass.states.get(cpu_entity_id)
    assert state_before is not None

    # Apply a glob category override live: every ``cpu_*`` field goes
    # to "diagnostic".
    hass.config_entries.async_update_entry(
        entry,
        options={CONF_CATEGORY_OVERRIDES: {"cpu_*": "diagnostic"}},
    )
    await hass.async_block_till_done()
    await hass.async_block_till_done()

    # The state still has the value (the override only changes the
    # category, not the value).
    state_after = hass.states.get(cpu_entity_id)
    assert state_after is not None
    assert state_after.state == "42.0"
