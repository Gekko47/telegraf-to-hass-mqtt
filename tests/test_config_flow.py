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
    TelegrafMqttConfigFlow,
    _default_device_name,
    _roll_up_topics,
    _valid_subscription_topic,
)
from custom_components.telegraf_mqtt.const import (
    CONF_DEVICE_NAME,
    CONF_SCAN_DURATION_SECONDS,
    CONF_SCAN_ROOT_TOPIC,
    CONF_SETUP_MODE,
    CONF_TOPIC_PATTERN,
    DOMAIN,
    MAX_SCAN_DURATION_SECONDS,
    MIN_SCAN_DURATION_SECONDS,
    SETUP_MODE_MANUAL,
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


def test_default_device_name_handles_dashes_and_unicode() -> None:
    """Pins the title-casing contract for mixed and non-ASCII inputs.

    ``_default_device_name`` replaces both ``_`` and ``-`` with
    spaces and then title-cases the segment. A refactor that drops
    either replace loses human-readable device names for the common
    Telegraf convention of kebab-case or snake_case hostnames. The
    unicode case pins the title-case behaviour on non-ASCII (the
    first character is already uppercase, so ``title()`` leaves it
    alone -- a refactor that upper-cases the first character
    instead of title-casing the whole segment would flip the
    behaviour here).
    """
    # Dashes only.
    assert _default_device_name("my-host/#") == "My Host"
    # Mixed underscores and dashes in the same segment.
    assert _default_device_name("some_host-name/#") == "Some Host Name"
    # Multiple segments -- the function returns on the first static
    # segment, so the second is irrelevant.
    assert _default_device_name("first-thing/irrelevant/leaf") == "First Thing"
    # Non-ASCII: Greek alpha + beta. Python's ``str.title()``
    # upper-cases the first character of every word. The

    # literal as deliberate test data; ruff's ambiguous-unicode
    # rule would otherwise flag them as lookalikes of Latin letters.
    assert _default_device_name("alpha-βeta/#") == "Alpha Βeta"  # noqa: RUF001


def test_roll_up_topics_groups_to_second_level_prefix() -> None:
    """2nd-level prefix grouping: rack1's leaves collapse to one pick."""
    seen = frozenset(
        {
            "telegraf/rack1/cpu",
            "telegraf/rack1/mem",
            "telegraf/rack2/cpu",
            "sensors/office/temp",
        }
    )
    result = _roll_up_topics(seen)
    assert result == [
        "sensors/office/#",
        "telegraf/rack1/#",
        "telegraf/rack2/#",
    ]


def test_roll_up_topics_handles_single_segment() -> None:
    """A leaf with one segment is grouped under itself."""
    assert _roll_up_topics(frozenset({"cpu", "mem"})) == ["cpu", "mem"]


def test_roll_up_topics_is_sorted_and_deduped() -> None:
    """Many leaves under the same prefix collapse to one pick; the
    result is sorted for stable UI rendering."""
    seen = frozenset(
        f"telegraf/host{i}/{kind}" for i in range(5) for kind in ("cpu", "mem")
    )
    result = _roll_up_topics(seen)
    assert result == [
        "telegraf/host0/#",
        "telegraf/host1/#",
        "telegraf/host2/#",
        "telegraf/host3/#",
        "telegraf/host4/#",
    ]


def test_validate_scan_settings_rejects_invalid_inputs() -> None:
    """The scan-settings form validator pins all four error branches.

    The validator is the gate that decides whether the user moves on
    to the running step. A refactor that drops or mis-routes any
    branch surfaces here. The cases:

    * ``telegraf/#/bad`` is syntactically invalid -> ``invalid_topic``
    * ``None`` / ``"abc"`` for the duration field is not castable
      to int -> ``invalid_duration`` (no range check, since the cast
      failed)
    * ``1`` and ``500`` for the duration field cast fine but are out
      of the documented 5-300 range -> ``invalid_duration``
    """
    flow = TelegrafMqttConfigFlow()

    # Bad probe root: same rule as the manual topic.
    errors = flow._validate_scan_settings(
        {CONF_SCAN_ROOT_TOPIC: "telegraf/#/bad", CONF_SCAN_DURATION_SECONDS: 30}
    )
    assert errors == {CONF_SCAN_ROOT_TOPIC: "invalid_topic"}

    # Non-integer duration: caught before the range check.
    for bad in (None, "abc", 1.5):
        errors = flow._validate_scan_settings(
            {CONF_SCAN_ROOT_TOPIC: "telegraf/#", CONF_SCAN_DURATION_SECONDS: bad}
        )
        assert errors == {CONF_SCAN_DURATION_SECONDS: "invalid_duration"}, (
            f"expected invalid_duration for {bad!r}, got {errors!r}"
        )

    # Out-of-range integer duration: caught by the range check.
    for bad in (MIN_SCAN_DURATION_SECONDS - 1, MAX_SCAN_DURATION_SECONDS + 1):
        errors = flow._validate_scan_settings(
            {CONF_SCAN_ROOT_TOPIC: "telegraf/#", CONF_SCAN_DURATION_SECONDS: bad}
        )
        assert errors == {CONF_SCAN_DURATION_SECONDS: "invalid_duration"}, (
            f"expected invalid_duration for {bad!r}, got {errors!r}"
        )

    # Happy path: returns an empty dict.
    errors = flow._validate_scan_settings(
        {CONF_SCAN_ROOT_TOPIC: "telegraf/#", CONF_SCAN_DURATION_SECONDS: 30}
    )
    assert errors == {}


async def test_config_flow_creates_entry(hass) -> None:
    """Manual path: user picks manual -> enters topic -> entry created."""
    result = await hass.config_entries.flow.async_init(DOMAIN, context={"source": SOURCE_USER})
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "user"

    # Choose manual mode.
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_SETUP_MODE: SETUP_MODE_MANUAL},
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "manual_topic"

    # Submit topic + device metadata.
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
        {CONF_SETUP_MODE: SETUP_MODE_MANUAL},
    )
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
        {CONF_SETUP_MODE: SETUP_MODE_MANUAL},
    )
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
        {CONF_SETUP_MODE: SETUP_MODE_MANUAL},
    )
    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_TOPIC_PATTERN: "telegraf/#", CONF_DEVICE_NAME: ""},
    )

    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {CONF_DEVICE_NAME: "required"}


async def test_reconfigure_flow_rejects_invalid_topic(hass) -> None:
    """The reconfigure step surfaces a form with ``invalid_topic`` error
    when the user submits a syntactically invalid MQTT topic pattern."""
    from homeassistant.config_entries import SOURCE_RECONFIGURE  # type: ignore[attr-defined]

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Telegraf",
        data={CONF_TOPIC_PATTERN: "telegraf/#", CONF_DEVICE_NAME: "Telegraf"},
        unique_id="telegraf/#",
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "reconfigure"

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_TOPIC_PATTERN: "telegraf/#/bad", CONF_DEVICE_NAME: "Telegraf"},
    )
    assert result["type"] == FlowResultType.FORM
    assert result["errors"] == {CONF_TOPIC_PATTERN: "invalid_topic"}


async def test_reconfigure_flow_requires_device_name(hass) -> None:
    """The reconfigure step surfaces a form with ``required`` error
    when the user submits an empty device name."""
    from homeassistant.config_entries import SOURCE_RECONFIGURE  # type: ignore[attr-defined]

    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Telegraf",
        data={CONF_TOPIC_PATTERN: "telegraf/#", CONF_DEVICE_NAME: "Telegraf"},
        unique_id="telegraf/#",
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_RECONFIGURE, "entry_id": entry.entry_id},
    )
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
