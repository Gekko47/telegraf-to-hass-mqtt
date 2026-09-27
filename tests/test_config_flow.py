"""Config flow tests for telegraf_mqtt.

Pure helper tests stay harness-free; flow-level tests run under the real HA
harness so the duplicate-topic abort is asserted against Home Assistant's own
flow manager.
"""

from __future__ import annotations

import copy

import pytest
from homeassistant.config_entries import SOURCE_USER
from homeassistant.data_entry_flow import FlowResultType
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.telegraf_mqtt.config_flow import (
    TelegrafMqttConfigFlow,
    _build_options_schema,
    _clean_options,
    _default_device_name,
    _roll_up_topics,
    _valid_subscription_topic,
)
from custom_components.telegraf_mqtt.const import (
    CONF_AUTO_DISCOVER,
    CONF_AUTO_DISCOVER_SCOPE,
    CONF_CATEGORY_OVERRIDES,
    CONF_CLEANUP_DELAY,
    CONF_DELETE_DELAY,
    CONF_DEVICE_ID_STRATEGY,
    CONF_DEVICE_NAME,
    CONF_ENABLE_CLEANUP,
    CONF_EXCLUDE_PATTERNS,
    CONF_EXPIRE_AFTER,
    CONF_FIELD_OVERRIDES,
    CONF_MIN_ACTIVE_METRICS,
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
    seen = frozenset(f"telegraf/host{i}/{kind}" for i in range(5) for kind in ("cpu", "mem"))
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
    errors = flow._validate_scan_settings({CONF_SCAN_ROOT_TOPIC: "telegraf/#/bad", CONF_SCAN_DURATION_SECONDS: 30})
    assert errors == {CONF_SCAN_ROOT_TOPIC: "invalid_topic"}

    # Non-integer duration: caught before the range check.
    for bad in (None, "abc", 1.5):
        errors = flow._validate_scan_settings({CONF_SCAN_ROOT_TOPIC: "telegraf/#", CONF_SCAN_DURATION_SECONDS: bad})
        assert errors == {CONF_SCAN_DURATION_SECONDS: "invalid_duration"}, (
            f"expected invalid_duration for {bad!r}, got {errors!r}"
        )

    # Out-of-range integer duration: caught by the range check.
    for bad in (MIN_SCAN_DURATION_SECONDS - 1, MAX_SCAN_DURATION_SECONDS + 1):
        errors = flow._validate_scan_settings({CONF_SCAN_ROOT_TOPIC: "telegraf/#", CONF_SCAN_DURATION_SECONDS: bad})
        assert errors == {CONF_SCAN_DURATION_SECONDS: "invalid_duration"}, (
            f"expected invalid_duration for {bad!r}, got {errors!r}"
        )

    # Happy path: returns an empty dict.
    errors = flow._validate_scan_settings({CONF_SCAN_ROOT_TOPIC: "telegraf/#", CONF_SCAN_DURATION_SECONDS: 30})
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


async def test_manual_topic_blank_device_name_returns_a_form_error(hass) -> None:
    """A whitespace-only device name is a form error, not an empty title.

    ``_clean`` strips the value to ``None``, and ``_validate`` is the
    single place that turns that into ``{CONF_DEVICE_NAME: "required"}``.
    The step must therefore re-render the form with that error and must
    NOT proceed to ``async_create_entry`` with a ``None`` title, which
    would surface as an opaque ``TypeError`` naming a Home Assistant
    internal rather than a form the user can fix.

    Driven with a whitespace-only name rather than ``""`` on purpose: the
    two are the same after ``_clean``, and this pins that the *stripped*
    value is the one validated.
    """
    flow = TelegrafMqttConfigFlow()
    result = await flow.async_step_manual_topic(
        {CONF_TOPIC_PATTERN: "telegraf/#", CONF_DEVICE_NAME: "   "},
    )
    assert result["type"] == FlowResultType.FORM
    assert result["step_id"] == "manual_topic"
    assert result["errors"] == {CONF_DEVICE_NAME: "required"}


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


# ---------------------------------------------------------------------------
# WS-B (C2): the options dialog must never reset a setting the user did not
# touch. ``_clean_options`` persists whatever the form returns, so a schema
# field that defaults to a CONSTANT instead of the CURRENT value silently
# discards the user's configuration on any unrelated save.
# ---------------------------------------------------------------------------


_STORED_OPTIONS = {
    CONF_EXPIRE_AFTER: 900,
    CONF_EXCLUDE_PATTERNS: ["disk_*", "swap_*"],
    CONF_FIELD_OVERRIDES: {"usage_idle": {"platform": "sensor"}},
}


def test_clean_options_preserves_untouched_settings() -> None:
    """Submitting a single changed field must not reset the other three."""
    cleaned = _clean_options({CONF_AUTO_DISCOVER: True}, _STORED_OPTIONS)

    assert cleaned[CONF_AUTO_DISCOVER] is True
    assert cleaned[CONF_EXPIRE_AFTER] == 900
    assert cleaned[CONF_EXCLUDE_PATTERNS] == ["disk_*", "swap_*"]
    assert cleaned[CONF_FIELD_OVERRIDES] == {"usage_idle": {"platform": "sensor"}}


def test_clean_options_defaults_only_when_nothing_is_stored() -> None:
    """With no stored options, an omitted key falls back to the documented default."""
    cleaned = _clean_options({}, {})

    assert cleaned[CONF_EXPIRE_AFTER] == 120
    assert cleaned[CONF_EXCLUDE_PATTERNS] == []
    assert cleaned[CONF_FIELD_OVERRIDES] == {}


def _schema_defaults(current: dict) -> dict:
    """Return ``{key: default}`` for every marker in an options schema."""
    schema = _build_options_schema(current)
    return {str(marker): marker.default() for marker in schema.schema}


def test_options_schema_prefills_every_field_from_current_options() -> None:
    """Every option in the form carries the CURRENT value as its default.

    This is the schema-level tripwire for C2: the three fields that used to
    hardcode a constant default (``expire_after``, ``exclude_patterns``,
    ``field_overrides``) now read from ``current_options``, so an untouched
    field round-trips to the value the user already saved.
    """
    defaults = _schema_defaults(_STORED_OPTIONS)

    assert defaults[CONF_EXPIRE_AFTER] == 900
    assert defaults[CONF_EXCLUDE_PATTERNS] == ["disk_*", "swap_*"]
    assert defaults[CONF_FIELD_OVERRIDES] == {"usage_idle": {"platform": "sensor"}}


async def test_options_flow_round_trip_preserves_stored_options(hass) -> None:
    """End-to-end: open the dialog, change one field, save -- the rest survive."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Telegraf",
        data={CONF_TOPIC_PATTERN: "telegraf/#", CONF_DEVICE_NAME: "Telegraf"},
        options=dict(_STORED_OPTIONS),
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == FlowResultType.FORM

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        user_input={CONF_AUTO_DISCOVER: True},
    )
    await hass.async_block_till_done()

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_EXPIRE_AFTER] == 900
    assert entry.options[CONF_EXCLUDE_PATTERNS] == ["disk_*", "swap_*"]
    assert entry.options[CONF_FIELD_OVERRIDES] == {"usage_idle": {"platform": "sensor"}}
    assert entry.options[CONF_AUTO_DISCOVER] is True


# ---------------------------------------------------------------------------
# AC6: "Saving the options dialog with no field edited leaves entry.options
# byte-identical", and by extension changing exactly one field must leave
# every other field byte-for-byte as it was.
#
# The prior assertions spot-check three keys. This one diffs the WHOLE
# dict, so an option added to the form later is covered automatically
# rather than needing its own assertion. It also seeds deliberately
# awkward values -- empty containers, a zero, a non-default strategy --
# because those are exactly the values a ``_get`` fallback written as
# ``stored.get(key) or DEFAULT`` would silently discard.
# ---------------------------------------------------------------------------

_AC6_STORED: dict = {
    CONF_EXPIRE_AFTER: 900,
    CONF_EXCLUDE_PATTERNS: ["disk_*", "swap_*"],
    CONF_FIELD_OVERRIDES: {"usage_idle": {"platform": "sensor"}},
    CONF_CATEGORY_OVERRIDES: {"mem_used": "config"},
    CONF_ENABLE_CLEANUP: False,
    CONF_CLEANUP_DELAY: 0,
    CONF_DELETE_DELAY: 86_400,
    CONF_MIN_ACTIVE_METRICS: 0,
    CONF_DEVICE_ID_STRATEGY: "topic_only",
    CONF_AUTO_DISCOVER: True,
    CONF_AUTO_DISCOVER_SCOPE: "telegraf/mine/#",
}


def _edited_value_for(key: str) -> object:
    """A plausible *new* value for ``key``, of the same shape it already has.

    The real form always submits a well-typed value for every field, so a
    generic sentinel string would be testing ``_clean_options``'s coercion
    (``list("x")`` raises) rather than its preservation. Editing means
    changing one field to a DIFFERENT value of its own type.
    """
    current = _AC6_STORED[key]
    if isinstance(current, bool):
        return not current
    if isinstance(current, int):
        return current + 4242
    if isinstance(current, list):
        return [*current, "brand_new_*"]
    if isinstance(current, dict):
        return {**current, "brand_new_field": {"native_unit": "%"}}
    return "telegraf/other/#"


@pytest.mark.parametrize("changed_key", sorted(_AC6_STORED))
def test_changing_one_option_leaves_every_other_option_byte_identical(changed_key: str) -> None:
    """One edited field in, exactly one field out.

    Everything the user did not touch must come back out of
    ``_clean_options`` equal to what was stored -- same value, same type,
    and for the container options the same emptiness. A field the user
    legitimately set to ``0``, ``False`` or ``[]`` must survive, because
    those are meaningful values, not "unset".
    """
    new_value = _edited_value_for(changed_key)

    cleaned = _clean_options({changed_key: new_value}, _AC6_STORED)

    for key, stored_value in _AC6_STORED.items():
        if key == changed_key:
            assert cleaned[key] == new_value, f"{key} should have been updated"
        else:
            assert cleaned[key] == stored_value, f"{key} was clobbered by an unrelated edit"
            # Type identity too: ``900`` and ``900.0`` compare equal, and a
            # bool/0 mixup would too. A "preserved" value of the wrong
            # type is not preserved.
            assert type(cleaned[key]) is type(stored_value), f"{key} changed type"

    # No key invented, none dropped.
    assert set(cleaned) == set(_AC6_STORED)


def test_empty_containers_and_falsy_scalars_survive_untouched() -> None:
    """Empty containers and falsy scalars are values, not absences.

    Seeded with ``[]``/``{}``/``0``/``False`` and edited on an unrelated
    key. This is the specific shape of bug a truthiness-based fallback
    (``stored.get(key) or DEFAULT``) produces, and the case the previous
    three-key spot check could not see.
    """
    stored: dict = {
        CONF_EXCLUDE_PATTERNS: [],
        CONF_FIELD_OVERRIDES: {},
        CONF_CATEGORY_OVERRIDES: {},
        CONF_CLEANUP_DELAY: 0,
        CONF_MIN_ACTIVE_METRICS: 0,
        CONF_ENABLE_CLEANUP: False,
        CONF_AUTO_DISCOVER: False,
    }

    cleaned = _clean_options({CONF_EXPIRE_AFTER: 300}, stored)

    assert cleaned[CONF_EXPIRE_AFTER] == 300
    assert cleaned[CONF_EXCLUDE_PATTERNS] == []
    assert cleaned[CONF_FIELD_OVERRIDES] == {}
    assert cleaned[CONF_CATEGORY_OVERRIDES] == {}
    assert cleaned[CONF_CLEANUP_DELAY] == 0
    assert cleaned[CONF_MIN_ACTIVE_METRICS] == 0
    assert cleaned[CONF_ENABLE_CLEANUP] is False
    assert cleaned[CONF_AUTO_DISCOVER] is False


async def test_options_flow_saving_one_field_preserves_the_rest_end_to_end(hass) -> None:
    """The AC6 contract through the real flow, not just the helper.

    Opens the options dialog, submits a single changed field, and diffs
    the persisted ``entry.options`` against the previous dict. This is
    the user-facing version of the unit test above: it also covers the
    schema pre-fill, the voluptuous validation, and the actual
    ``async_create_entry`` write.
    """
    entry = MockConfigEntry(
        domain=DOMAIN,
        title="Telegraf",
        data={CONF_TOPIC_PATTERN: "telegraf/#", CONF_DEVICE_NAME: "Telegraf"},
        options=copy.deepcopy(_AC6_STORED),
    )
    entry.add_to_hass(hass)
    # ``entry.options`` is a MappingProxyType, which ``copy.deepcopy``
    # cannot pickle, so snapshot it by hand. A shallow dict() would be
    # enough for the scalars but would alias the two nested containers,
    # letting a mutation hide from the comparison below.
    before = {
        k: (dict(v) if isinstance(v, dict) else list(v) if isinstance(v, list) else v) for k, v in entry.options.items()
    }

    result = await hass.config_entries.options.async_init(entry.entry_id)
    assert result["type"] == FlowResultType.FORM

    result = await hass.config_entries.options.async_configure(
        result["flow_id"],
        # The real form submits every field, so this is what the client
        # actually sends: the pre-filled values for everything else.
        user_input={**_schema_defaults(_AC6_STORED), CONF_EXPIRE_AFTER: 1800},
    )
    await hass.async_block_till_done()

    assert result["type"] == FlowResultType.CREATE_ENTRY
    assert entry.options[CONF_EXPIRE_AFTER] == 1800
    for key, value in before.items():
        if key == CONF_EXPIRE_AFTER:
            continue
        assert entry.options[key] == value, f"{key} was clobbered by the expire_after edit"
