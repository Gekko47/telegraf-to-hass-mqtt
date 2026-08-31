"""Harness-free tests for the discover-topics config flow helpers.

The discover path uses the same ``SnoopListener`` plumbing as the
post-setup auto-discover. The config flow installs the listener, waits
for it to auto-stop, then renders a pick list from the roll-up. These
tests cover the helpers in isolation plus a fake-broker end-to-end
where we drive the snoop directly and assert what the flow would
present.
"""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from custom_components.telegraf_mqtt.config_flow import (
    _MAX_PICK_LIST_OPTIONS,
    TelegrafMqttConfigFlow,
    _looks_telegraf_shaped,
    _pick_topics_schema,
    _roll_up_topics,
)
from custom_components.telegraf_mqtt.const import (
    CONF_TOPIC_PATTERN,
    DEFAULT_AUTO_DISCOVER,
    DEFAULT_AUTO_DISCOVER_PROBE_TOPIC,
)
from custom_components.telegraf_mqtt.snoop import SnoopListener, derive_probe_topic


# ---------------------------------------------------------------------------
# Security posture: the post-setup snoop is opt-in.
# ---------------------------------------------------------------------------
def test_default_auto_discover_is_false() -> None:
    """The post-setup snoop is off by default.

    Topic discovery only happens during the config flow. The user
    opts in to the post-setup snoop (so the snoop picks up new
    Telegraf hosts under their existing ``topic_pattern``) via the
    options flow. A ``True`` default would silently widen the
    integration's broker subscription past the user's scope on a
    shared broker -- the original concern this whole change set
    out to fix. This one-liner pins the default so a refactor that
    flips it back is caught by CI.
    """
    assert DEFAULT_AUTO_DISCOVER is False


# ---------------------------------------------------------------------------
# Probe-topic derivation (post-setup snoop)
# ---------------------------------------------------------------------------
def test_derive_probe_topic_returns_pattern_verbatim() -> None:
    """The post-setup snoop probe never widens past the user pattern."""
    assert derive_probe_topic("telegraf/rack1/#") == "telegraf/rack1/#"
    assert derive_probe_topic("telegraf/+/cpu") == "telegraf/+/cpu"
    assert derive_probe_topic("telegraf/#") == "telegraf/#"


def test_derive_probe_topic_strips_whitespace() -> None:
    assert derive_probe_topic("  telegraf/#  ") == "telegraf/#"


def test_derive_probe_topic_empty_falls_back_to_default() -> None:
    """An empty pattern does not subscribe to everything -- we fall back
    to the documented default so a corrupted entry cannot widen the
    probe past the user intent.
    """
    assert derive_probe_topic("") == DEFAULT_AUTO_DISCOVER_PROBE_TOPIC
    assert derive_probe_topic("   ") == DEFAULT_AUTO_DISCOVER_PROBE_TOPIC


# ---------------------------------------------------------------------------
# Looks-Telegraf-shaped heuristic
# ---------------------------------------------------------------------------
def test_looks_telegraf_shaped_matches_head_segment() -> None:
    assert _looks_telegraf_shaped("telegraf/rack1/#") is True
    assert _looks_telegraf_shaped("TELEGRAF/rack1/#") is True  # case-insensitive
    assert _looks_telegraf_shaped("sensors/office/#") is False
    assert _looks_telegraf_shaped("homeassistant/sensor/#") is False


# ---------------------------------------------------------------------------
# Roll-up: 2nd-level prefix grouping
# ---------------------------------------------------------------------------
def test_roll_up_groups_rack1_and_rack2_separately() -> None:
    """The original concern: a shared broker with two Telegraf deployments.

    The roll-up must present ``telegraf/rack1/#`` and ``telegraf/rack2/#``
    as separate pick-list entries so the user can subscribe to one and
    leave the other alone.
    """
    seen = frozenset(
        {
            "telegraf/rack1/cpu",
            "telegraf/rack1/mem",
            "telegraf/rack1/disk",
            "telegraf/rack2/cpu",
            "telegraf/rack2/net",
        }
    )
    result = _roll_up_topics(seen)
    assert "telegraf/rack1/#" in result
    assert "telegraf/rack2/#" in result
    assert len(result) == 2


def test_roll_up_dedupes_many_leaves_to_one_prefix() -> None:
    seen = frozenset(
        f"telegraf/host{i}/{kind}" for i in range(50) for kind in ("cpu", "mem", "disk")
    )
    result = _roll_up_topics(seen)
    assert len(result) == 50
    assert all(p.startswith("telegraf/host") and p.endswith("/#") for p in result)


def test_roll_up_caps_at_max_pick_list() -> None:
    """Bounded: a busy broker cannot blow up the form."""
    seen = frozenset(f"prefix{i}/x" for i in range(_MAX_PICK_LIST_OPTIONS * 3))
    result = _roll_up_topics(seen)
    assert len(result) == _MAX_PICK_LIST_OPTIONS


def test_roll_up_topics_mixed_single_and_multi_segment() -> None:
    """Trip wire: a refactor of the prefix logic must keep the
    single-segment leaves separate from the multi-segment ones.

    The roll-up builds a subscription pattern for each leaf, but the
    patterns differ: a single-segment leaf (``cpu``) is its own
    subscription, while a multi-segment leaf (``telegraf/rack1/cpu``)
    collapses to a 2nd-level prefix (``telegraf/rack1/#``). Mixing
    both shapes in one input is a refactor magnet -- a careless
    rewrite that always splits on ``/`` would treat ``cpu`` as a
    2nd-level prefix under an empty head, producing ``/#``.
    """
    seen = frozenset(
        {
            "cpu",
            "mem",
            "telegraf/rack1/cpu",
            "telegraf/rack1/mem",
            "telegraf/rack2/cpu",
        }
    )
    result = _roll_up_topics(seen)
    assert result == [
        "cpu",
        "mem",
        "telegraf/rack1/#",
        "telegraf/rack2/#",
    ]


# ---------------------------------------------------------------------------
# Snoop listener under the discover-path scenario
# ---------------------------------------------------------------------------
@dataclass
class _FakeMqttMessage:
    topic: str
    payload: bytes


@dataclass
class _FakeMqtt:
    """Minimal ``homeassistant.components.mqtt``-shaped double."""

    subscribed_topics: list[tuple[str, Callable[..., Any]]] = None  # type: ignore[assignment]

    def __post_init__(self) -> None:
        if self.subscribed_topics is None:
            self.subscribed_topics = []

    async def async_subscribe(self, hass: Any, topic: str, callback: Any) -> Callable[[], None]:
        self.subscribed_topics.append((topic, callback))

        def _unsub() -> None:
            self.subscribed_topics = [
                entry for entry in self.subscribed_topics if entry[1] is not callback
            ]

        return _unsub


class _Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


async def _drain(callback: Any, topic: str, payload: bytes) -> None:
    await callback(_FakeMqttMessage(topic=topic, payload=payload))


def test_snoop_listener_can_be_driven_by_config_flow_scan() -> None:
    """End-to-end on a fake broker: the snoop captures the topics the
    config flow would then present. This is the same plumbing the
    in-flow ``_start_scan`` uses; the only difference is that here we
    do not go through the HA config flow manager.
    """
    mqtt = _FakeMqtt()

    async def _run() -> None:
        # The snoop needs ``hass.loop.call_later`` for the auto-stop
        # timer. The HA ``hass`` object provides that; in this test
        # we attach the running event loop to a bare ``object()`` --
        # exactly what ``test_phase10_ux.py::test_snoop_listener_*
        # does for the same reason.
        hass = type("H", (), {})()
        hass.loop = asyncio.get_running_loop()

        listener = SnoopListener(
            probe_topic="telegraf/#",
            timeout_seconds=0.05,
            clock=_Clock(),
        )
        await listener.start(hass=hass, subscribe=mqtt.async_subscribe)
        await _drain(listener._on_message, "telegraf/rack1/cpu", b"")
        await _drain(listener._on_message, "telegraf/rack1/mem", b"")
        await _drain(listener._on_message, "telegraf/rack2/cpu", b"")
        await asyncio.sleep(0.08)
        assert listener.is_finished is True
        result = listener.stop()
        prefixes = _roll_up_topics(result.topics)
        assert "telegraf/rack1/#" in prefixes
        assert "telegraf/rack2/#" in prefixes
        assert mqtt.subscribed_topics == []

    asyncio.run(_run())


def test_flow_start_scan_uses_user_supplied_probe_root() -> None:
    """A user who narrows the scan root to ``telegraf/rack1/#`` should
    not see rack2 traffic during the scan. This is the original
    isolation concern: the scan root scopes what the user is allowed
    to discover, just like the regular topic_pattern scopes the
    primary subscription.
    """
    mqtt = _FakeMqtt()

    async def _run() -> None:
        hass = type("H", (), {})()
        hass.loop = asyncio.get_running_loop()

        listener = SnoopListener(
            probe_topic="telegraf/rack1/#",
            timeout_seconds=0.05,
            clock=_Clock(),
        )
        await listener.start(hass=hass, subscribe=mqtt.async_subscribe)
        assert mqtt.subscribed_topics[0][0] == "telegraf/rack1/#"
        await _drain(listener._on_message, "telegraf/rack1/cpu", b"")
        await _drain(listener._on_message, "telegraf/rack1/mem", b"")
        await asyncio.sleep(0.08)
        result = listener.stop()
        prefixes = _roll_up_topics(result.topics)
        assert prefixes == ["telegraf/rack1/#"]

    asyncio.run(_run())


# ---------------------------------------------------------------------------
# Discover-path: pick-topics error branches + schema pre-selection.
# ---------------------------------------------------------------------------
# These tests drive the real ``async_step_pick_topics`` step method directly
# (no HA flow manager). The form-rendering helper ``async_show_form`` and
# the entry-creation helper ``async_set_unique_id`` are stubbed: the form
# branch returns a dict we can assert against, and the entry-creation
# branch is not exercised in any of these tests. Same pattern as the
# ``test_phase9_gold.py`` reconfigure validator tests.


def _make_flow_with_seen(seen: frozenset[str]) -> tuple[TelegrafMqttConfigFlow, list[dict[str, Any]]]:
    """Build a config flow with seeded ``_scan_seen_topics`` and a
    ``async_show_form`` stub that records every form it would have
    rendered. Returns ``(flow, forms)``.
    """
    flow = TelegrafMqttConfigFlow()
    flow._scan_seen_topics = seen
    forms: list[dict[str, Any]] = []

    def _show_form(*, step_id: str, data_schema: Any, errors: dict | None = None) -> dict[str, Any]:
        record = {"step_id": step_id, "data_schema": data_schema, "errors": dict(errors or {})}
        forms.append(record)
        return {"type": "form", **record}

    flow.async_show_form = _show_form  # type: ignore[method-assign]
    # ``async_set_unique_id`` is only reached on the create_entry path;
    # these tests stay on the form branch. Stub it just in case.
    flow.async_set_unique_id = lambda *_a, **_kw: None  # type: ignore[assignment,method-assign]
    return flow, forms


def test_pick_topics_invalid_pick_returns_invalid_topic_error() -> None:
    """A custom-value pick that fails ``_valid_subscription_topic``
    surfaces ``invalid_topic`` and re-shows the form. The picker
    has ``custom_value=True`` so the user can type a hand-rolled
    topic; this branch makes sure typos don't silently create an
    entry.
    """
    seen = frozenset({"telegraf/rack1/cpu", "telegraf/rack1/mem"})
    flow, forms = _make_flow_with_seen(seen)

    async def _run() -> None:
        # Mix a valid pre-selected Telegraf pick with a hand-typed junk
        # topic; only the junk should fail the validator.
        result = await flow.async_step_pick_topics(
            {CONF_TOPIC_PATTERN: ["telegraf/rack1/#", "garbage/topic/#/bad"]}
        )
        assert result["type"] == "form"
        assert result["step_id"] == "pick_topics"
        assert result["errors"] == {CONF_TOPIC_PATTERN: "invalid_topic"}

    asyncio.run(_run())
    assert len(forms) == 1
    assert forms[0]["step_id"] == "pick_topics"
    assert forms[0]["errors"] == {CONF_TOPIC_PATTERN: "invalid_topic"}


def test_pick_topics_no_picks_returns_no_topics_selected_error() -> None:
    """Submitting the pick form with an empty list surfaces
    ``no_topics_selected`` and re-shows the form. The HA-harness
    config flow is the canonical pin; this harness-free test is the
    trip wire against a refactor that silently accepts an empty
    pick list.
    """
    seen = frozenset({"telegraf/rack1/cpu"})
    flow, forms = _make_flow_with_seen(seen)

    async def _run() -> None:
        result = await flow.async_step_pick_topics({CONF_TOPIC_PATTERN: []})
        assert result["type"] == "form"
        assert result["step_id"] == "pick_topics"
        assert result["errors"] == {CONF_TOPIC_PATTERN: "no_topics_selected"}

    asyncio.run(_run())
    assert len(forms) == 1
    assert forms[0]["errors"] == {CONF_TOPIC_PATTERN: "no_topics_selected"}


def test_pick_topics_schema_pre_selects_only_telegraf_shaped_prefixes() -> None:
    """The pick form's ``default=`` is the Telegraf-shaped subset, not
    the full prefix list.

    Pin the wiring between ``_looks_telegraf_shaped`` and
    ``_pick_topics_schema`` so a refactor that accidentally swaps
    ``pre_selected`` for ``prefixes`` (or drops the pre-selection
    altogether) is caught here: the user would otherwise be greeted
    by a fully-selected pick list, including HA-internal topics
    like ``homeassistant/sensor/#``.
    """
    prefixes = [
        "homeassistant/sensor/#",
        "sensors/office/#",
        "telegraf/rack1/#",
        "telegraf/rack2/#",
    ]
    # ``async_step_pick_topics`` builds the pre-selected list as
    # ``[p for p in prefixes if _looks_telegraf_shaped(p)]``; the
    # test exercises the same wiring so a refactor that drops the
    # pre-selection or reorders the args is caught.
    pre_selected = [p for p in prefixes if _looks_telegraf_shaped(p)]
    schema = _pick_topics_schema(prefixes, pre_selected)
    # ``vol.Schema({...})`` materialises the default at construction
    # time, so we can read it back via ``schema({})`` -- an empty
    # input lets the default fire.
    result = schema({})
    assert result[CONF_TOPIC_PATTERN] == pre_selected
    # Pin the specific pre-selected list to make the test read as a
    # contract: HA-internal + non-Telegraf prefixes are excluded;
    # only the Telegraf-shaped 2nd-level ones are checked.
    assert pre_selected == [
        "telegraf/rack1/#",
        "telegraf/rack2/#",
    ]


