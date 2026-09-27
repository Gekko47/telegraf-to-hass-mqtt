# test_topics.py
from __future__ import annotations

import pytest

from custom_components.telegraf_mqtt.topics import (
    mqtt_filter_covers,
    mqtt_filter_matches,
)

# ---------------------------------------------------------------------------
# mqtt_filter_matches -- MQTT 3.1.1 section 4.7
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("topic", "mqtt_filter"),
    [
        # Literal match.
        ("telegraf/rack1/cpu", "telegraf/rack1/cpu"),
        ("telegraf", "telegraf"),
        # 'a/#' matches the parent level as well as the whole subtree.
        ("telegraf", "telegraf/#"),
        ("telegraf/rack1", "telegraf/#"),
        ("telegraf/rack1/cpu", "telegraf/#"),
        ("telegraf/rack1/cpu/host-a", "telegraf/#"),
        # A bare '#' matches everything except $-prefixed topics.
        ("telegraf", "#"),
        ("anything/at/all", "#"),
        # '+' matches exactly one level, including an EMPTY one.
        ("telegraf/rack1/cpu", "telegraf/+/cpu"),
        ("telegraf//cpu", "telegraf/+/cpu"),
        ("telegraf/rack1", "telegraf/+"),
        # '$' topics are reachable only through a literal first level.
        ("$SYS/broker/uptime", "$SYS/#"),
        ("$SYS/broker/uptime", "$SYS/+/uptime"),
    ],
)
def test_mqtt_filter_matches_positive(topic: str, mqtt_filter: str) -> None:
    assert mqtt_filter_matches(topic, mqtt_filter) is True


@pytest.mark.parametrize(
    ("topic", "mqtt_filter"),
    [
        # Different literal levels.
        ("telegraf/rack1/cpu", "telegraf/rack2/cpu"),
        # A filter without a trailing '#' does not swallow a longer topic.
        ("telegraf/rack1/cpu", "telegraf/rack1"),
        # ...and a longer filter does not match a shorter topic.
        ("telegraf/rack1", "telegraf/rack1/cpu"),
        # '+' spans exactly one level: not zero, not two.
        ("telegraf/cpu", "telegraf/+/cpu"),
        ("telegraf/rack1/sub/cpu", "telegraf/+/cpu"),
        ("telegraf/rack1", "telegraf/+/+"),
        # '$'-prefixed topics are invisible to a wildcard-first filter.
        ("$SYS/broker/uptime", "#"),
        ("$SYS/broker/uptime", "+/broker/uptime"),
        ("$SYS/broker/uptime", "+/#"),
        # An embedded wildcard is a literal level, not a partial wildcard.
        ("sport/tennis", "sp+rt/tennis"),
        ("sp+rt/other", "sp+rt/tennis"),
        # Empty inputs match nothing (a corrupt topic must not match '#').
        ("", "#"),
        ("telegraf/rack1", ""),
        ("", ""),
    ],
)
def test_mqtt_filter_matches_negative(topic: str, mqtt_filter: str) -> None:
    assert mqtt_filter_matches(topic, mqtt_filter) is False


def test_mqtt_filter_matches_is_the_dual_of_itself() -> None:
    """Every topic its own filter matches -- the invariant the snoop
    skip logic depends on (``telegraf/rack1/#`` never skips a message the
    main subscription on that same pattern already handled)."""
    for topic, topic_filter in (
        ("telegraf/rack1/cpu", "telegraf/rack1/cpu"),
        ("telegraf/rack1", "telegraf/#"),
        ("telegraf", "telegraf/#"),
        ("telegraf//cpu", "telegraf/+/cpu"),
    ):
        assert mqtt_filter_matches(topic, topic_filter) is True


# ---------------------------------------------------------------------------
# mqtt_filter_covers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("outer", "inner"),
    [
        # A broader scope covers a narrower pattern...
        ("telegraf/#", "telegraf/rack1/#"),
        ("telegraf/#", "telegraf/rack1/cpu"),
        ("telegraf/#", "telegraf"),
        ("#", "telegraf/rack1/cpu"),
        # ...including a trailing '#' on the outer covering inner's end.
        ("telegraf/#", "telegraf/rack1"),
        # Identity.
        ("telegraf/rack1/#", "telegraf/rack1/#"),
        ("telegraf/rack1/cpu", "telegraf/rack1/cpu"),
        # Literal, level-for-level.
        ("telegraf/rack1/cpu", "telegraf/rack1/cpu"),
    ],
)
def test_mqtt_filter_covers_positive(outer: str, inner: str) -> None:
    assert mqtt_filter_covers(outer, inner) is True


@pytest.mark.parametrize(
    ("outer", "inner"),
    [
        # The direction the Repairs check depends on: a narrow scope does
        # NOT cover the broad pattern the entry already subscribes to.
        ("telegraf/rack1/#", "telegraf/#"),
        ("telegraf/rack1/cpu", "telegraf/rack1/mem"),
        # A sibling subtree is not covered.
        ("telegraf/rack1/#", "telegraf/rack2/#"),
        # Unbounded inner needs an unbounded outer.
        ("telegraf/rack1/cpu", "telegraf/rack1/#"),
        # Asymmetric single-level wildcard: coverage depends on the
        # literal's runtime value, so it is soundly reported as unproven.
        ("telegraf/+/cpu", "telegraf/rack1/cpu"),
        ("telegraf/rack1/cpu", "telegraf/+/cpu"),
        # Empty inputs are not provable coverage.
        ("", "telegraf/#"),
        ("telegraf/#", ""),
    ],
)
def test_mqtt_filter_covers_negative(outer: str, inner: str) -> None:
    assert mqtt_filter_covers(outer, inner) is False


def test_mqtt_filter_covers_is_sound_for_its_own_positives() -> None:
    """A positive result must hold for every concrete topic ``inner``
    matches. This is the safety property the Repairs warning rests on: if
    coverage is reported, skipping really is safe."""
    concrete = ("telegraf", "telegraf/rack1", "telegraf/rack1/cpu", "telegraf/rack1/cpu/host-a")
    for outer, inner in (("telegraf/#", "telegraf/rack1/#"), ("telegraf/#", "telegraf"), ("#", "telegraf/rack1/cpu")):
        assert mqtt_filter_covers(outer, inner) is True
        for topic in concrete:
            if mqtt_filter_matches(topic, inner):
                assert mqtt_filter_matches(topic, outer) is True, f"{outer} claimed to cover {inner} but missed {topic}"


def test_topics_module_never_imports_homeassistant() -> None:
    """``topics.py`` runs on the MQTT message path and is unit-tested
    directly; it must stay dependency-free like ``parser.py``."""
    import ast
    from pathlib import Path

    from custom_components.telegraf_mqtt import topics

    tree = ast.parse(Path(topics.__file__).read_text(encoding="utf-8"))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not {name for name in imported if name.split(".")[0] == "homeassistant"}
