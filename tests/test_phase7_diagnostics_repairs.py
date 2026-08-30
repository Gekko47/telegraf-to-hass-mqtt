"""Phase 7 exit-criteria tests: diagnostics + repairs.

ROADMAP.md Phase 7:
  - Diagnostics download contains everything SPEC.md lists, redacted.
  - Overlapping topic patterns raise a Repair issue, not just a log line.
  - Repair issues resolve/clear automatically when fixed.

These tests are harness-free: the diagnostic payload is a plain dict
that any test can build; the Repairs helpers are exercised against a
fake ``ir`` module that records the calls. The real-HA test of the
diagnostics download lives in ``tests/test_diagnostics.py``.
"""

from __future__ import annotations

import json
import types
from dataclasses import dataclass, field
from typing import Any

import pytest

from custom_components.telegraf_mqtt.const import (
    CONF_AUTO_DISCOVER,
    CONF_DEVICE_ID_STRATEGY,
    DEFAULT_AUTO_DISCOVER,
    DEFAULT_DEVICE_ID_STRATEGY,
    VALID_DEVICE_ID_STRATEGIES,
)
from custom_components.telegraf_mqtt.parser import ParserStats, TelegrafParser
from custom_components.telegraf_mqtt.repairs import (
    _patterns_overlap,
    check_invalid_persisted_option,
    check_overlapping_topics,
)

# ---------------------------------------------------------------------------
# Parser stats: counters + last_message snapshot
# ---------------------------------------------------------------------------


def test_parser_stats_count_received_for_every_parse() -> None:
    stats = ParserStats()
    parser = TelegrafParser(stats=stats)
    parser.parse(b'{"name": "cpu", "tags": {}, "fields": {"x": 1}, "timestamp": 1}')
    assert stats.received == 1
    assert stats.parsed == 1
    assert stats.dropped_invalid_json == 0


def test_parser_stats_note_received_records_metadata() -> None:
    """``ParserStats.note_received`` is the public surface that
    populates ``last_message`` *before* a parse outcome is known.
    Tests pin the field set so a future refactor cannot accidentally
    widen it (e.g. including raw bytes).
    """
    stats = ParserStats()
    stats.note_received(topic="telegraf/host1/cpu", byte_length=42)
    assert stats.received == 1
    assert stats.last_message is not None
    # The set of keys is closed; a refactor that adds "raw" / "payload"
    # / "value_bytes" / "host" would break the redaction contract.
    assert set(stats.last_message) == {
        "topic",
        "byte_length",
        "dropped_reason",
        "measurement",
    }
    assert stats.last_message["topic"] == "telegraf/host1/cpu"
    assert stats.last_message["byte_length"] == 42
    assert stats.last_message["dropped_reason"] is None
    assert stats.last_message["measurement"] is None


def test_parser_stats_count_dropped_invalid_json() -> None:
    stats = ParserStats()
    parser = TelegrafParser(stats=stats)
    result = parser.parse(b"not json at all")
    assert result == []
    assert stats.received == 1
    assert stats.dropped_invalid_json == 1
    assert stats.parsed == 0
    assert stats.last_message is not None
    assert stats.last_message["dropped_reason"] == "invalid_json"


def test_parser_stats_count_dropped_unsupported_shape() -> None:
    stats = ParserStats()
    parser = TelegrafParser(stats=stats)
    # Valid JSON, not a dict.
    parser.parse(b'"just a string"')
    assert stats.dropped_unsupported_shape == 1
    # Valid JSON, dict, but no "name" key.
    parser.parse(b'{"tags": {}, "fields": {}, "timestamp": 1}')
    assert stats.dropped_unsupported_shape == 2
    # Valid JSON, dict, but "name" is not a string.
    parser.parse(b'{"name": 7, "tags": {}, "fields": {}, "timestamp": 1}')
    assert stats.dropped_unsupported_shape == 3


def test_parser_handler_dispatch_is_fault_isolated() -> None:
    """A handler that raises must not crash the parser.

    Phase 10 follow-on: per-measurement handlers are the only place in
    the pipeline that isn't already wrapped in a defensive try/except.
    We pin the contract that ``KeyError``, ``TypeError``,
    ``AttributeError``, and ``ValueError`` raised inside a handler are
    caught, counted in ``dropped_parser_error``, logged at DEBUG, and
    surface as ``[]`` -- and that the next message is still processed.
    """
    import json

    from custom_components.telegraf_mqtt.parser import ParserStats, TelegrafParser

    def _raising_handler(payload):  # type: ignore[no-untyped-def]
        # Simulate the shape of bug a real handler might have:
        # ``payload["fields"]["x"]`` against a missing key.
        value = payload["fields"]["x"]
        return [f"used={value}"]

    original_cpu = TelegrafParser._PARSERS["cpu"]
    TelegrafParser._PARSERS["cpu"] = _raising_handler  # type: ignore[assignment]
    try:
        stats = ParserStats()
        parser = TelegrafParser(stats=stats)
        result = parser.parse(
            json.dumps({"name": "cpu", "tags": {"host": "h1"}, "fields": {}, "timestamp": 1}),
            topic="t/boom",
        )
        assert result == []
        assert stats.dropped_parser_error == 1
        assert stats.parsed == 0
        assert stats.received == 1
        assert stats.last_message is not None
        assert stats.last_message["topic"] == "t/boom"
        assert stats.last_message["measurement"] == "cpu"
        assert stats.last_message["dropped_reason"] == "parser_error"
    finally:
        TelegrafParser._PARSERS["cpu"] = original_cpu  # type: ignore[assignment]


def test_parser_handler_dispatch_propagates_unexpected_exceptions() -> None:
    """The narrow catch must NOT suppress unrelated exceptions.

    ``KeyboardInterrupt``, ``SystemExit``, ``MemoryError``, and
    ``asyncio.CancelledError`` should propagate as designed -- they
    are integration-level signals, not handler bugs. A blanket
    ``except Exception`` would silently break HA's cancel path; this
    test pins the contract that the wrap is narrow.
    """
    import json

    import pytest

    from custom_components.telegraf_mqtt.parser import ParserStats, TelegrafParser

    def _raising_handler(_payload):  # type: ignore[no-untyped-def]
        raise SystemExit("handler designed to crash")

    original_cpu = TelegrafParser._PARSERS["cpu"]
    TelegrafParser._PARSERS["cpu"] = _raising_handler  # type: ignore[assignment]
    try:
        parser = TelegrafParser(stats=ParserStats())
        with pytest.raises(SystemExit):
            parser.parse(
                json.dumps({"name": "cpu", "tags": {"host": "h1"}, "fields": {"x": 1}, "timestamp": 1}),
                topic="t/sys",
            )
    finally:
        TelegrafParser._PARSERS["cpu"] = original_cpu  # type: ignore[assignment]


def test_parser_handler_dispatch_handles_each_caught_exception() -> None:
    """Every exception in the narrow catch list is handled identically.

    KeyError / TypeError / AttributeError / ValueError are the four
    exception families a future per-measurement handler is most likely
    to raise from a bad assumption. Each one must be caught and
    counted; this test pins that.
    """
    import json

    from custom_components.telegraf_mqtt.parser import ParserStats, TelegrafParser

    def _make_handler(exc: BaseException):  # type: ignore[no-untyped-def]
        def _handler(_payload):  # type: ignore[no-untyped-def]
            raise exc

        return _handler

    original_cpu = TelegrafParser._PARSERS["cpu"]
    try:
        for exc in (
            KeyError("fields"),
            TypeError("bad coercion"),
            AttributeError("'NoneType' has no attribute 'get'"),
            ValueError("not a number"),
        ):
            TelegrafParser._PARSERS["cpu"] = _make_handler(exc)  # type: ignore[assignment]
            stats = ParserStats()
            parser = TelegrafParser(stats=stats)
            result = parser.parse(
                json.dumps({"name": "cpu", "tags": {"host": "h1"}, "fields": {"x": 1}, "timestamp": 1}),
                topic="t/" + type(exc).__name__,
            )
            assert result == []
            assert stats.dropped_parser_error == 1
            assert stats.last_message is not None
            assert stats.last_message["dropped_reason"] == "parser_error"
            assert stats.last_message["measurement"] == "cpu"
    finally:
        TelegrafParser._PARSERS["cpu"] = original_cpu  # type: ignore[assignment]


def test_parser_stats_note_parser_error_seeds_last_message() -> None:
    """``note_parser_error`` populates ``last_message`` with the
    measurement that failed.

    The ``last_message`` schema is shared between the three note_*
    helpers; this test pins the schema field set so a future refactor
    cannot accidentally drop the measurement name from a parser_error
    event.
    """
    from custom_components.telegraf_mqtt.parser import ParserStats

    stats = ParserStats()
    stats.note_parser_error(topic="t/cpu", byte_length=42, measurement="cpu")
    assert stats.received == 1
    assert stats.dropped_parser_error == 1
    assert stats.last_message is not None
    assert set(stats.last_message) == {
        "topic",
        "byte_length",
        "dropped_reason",
        "measurement",
    }
    assert stats.last_message["topic"] == "t/cpu"
    assert stats.last_message["byte_length"] == 42
    assert stats.last_message["dropped_reason"] == "parser_error"
    assert stats.last_message["measurement"] == "cpu"


def test_parser_stats_count_unknown_measurement_fallback() -> None:
    stats = ParserStats()
    parser = TelegrafParser(stats=stats)
    # "custom_plugin" is not in the parser dispatch table -> generic fallback.
    parser.parse(b'{"name": "custom_plugin", "tags": {"host": "h1"}, "fields": {"x": 1.0}, "timestamp": 1}')
    assert stats.unknown_measurement_fallbacks == 1
    assert stats.parsed == 1


def test_parser_last_message_captures_relevant_metadata() -> None:
    """The single-slot ring buffer records enough to debug a stuck
    integration: topic, byte length, drop reason, measurement name.
    The raw payload is never captured.
    """
    stats = ParserStats()
    parser = TelegrafParser(stats=stats)
    parser.parse(
        b'{"name": "cpu", "tags": {"host": "h1"}, "fields": {"usage_idle": 88.4}, "timestamp": 1}',
        topic="telegraf/host1/cpu",
    )
    assert stats.last_message is not None
    assert stats.last_message["topic"] == "telegraf/host1/cpu"
    assert stats.last_message["byte_length"] > 0
    assert stats.last_message["dropped_reason"] is None
    assert stats.last_message["measurement"] == "cpu"
    # The raw payload is not captured.
    assert "raw" not in stats.last_message
    assert "payload" not in stats.last_message
    assert "fields" not in stats.last_message
    # And the serialised form contains no host data either.
    text = json.dumps(stats.last_message)
    assert "h1" not in text
    assert "88.4" not in text


def test_parser_last_message_is_singular() -> None:
    """The buffer keeps only the most recent message."""
    stats = ParserStats()
    parser = TelegrafParser(stats=stats)
    parser.parse(
        b'{"name": "cpu", "tags": {}, "fields": {"x": 1}, "timestamp": 1}',
        topic="first",
    )
    parser.parse(
        b'{"name": "mem", "tags": {}, "fields": {"y": 2}, "timestamp": 1}',
        topic="second",
    )
    assert stats.last_message["topic"] == "second"


# ---------------------------------------------------------------------------
# Repairs: _patterns_overlap unit tests
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "a,b,expected",
    [
        ("telegraf/#", "telegraf/#", True),  # exact match
        ("telegraf/#", "telegraf/+/cpu", True),  # wildcard absorbs literal
        ("telegraf/+/cpu", "telegraf/#", True),  # symmetric
        ("telegraf/+/cpu", "telegraf/+/cpu", True),
        ("telegraf/+/cpu", "telegraf/+/mem", False),  # disjoint leaves
        ("a/b/c", "a/b/c/d", False),  # literal cannot absorb sub-topic
        ("a/b", "a/b/#", True),  # parent-level match: trailing ``#``
        ("a/b/#", "a/b", True),  # symmetric
        ("a/b/c", "a/b/c/#", True),  # parent-level match, deeper
        ("a/b/c/#", "a/b/c", True),  # symmetric
        ("x/y", "a/b", False),
        ("#", "anything/at/all", True),  # bare ``#`` matches everything
        ("a/b/c", "a/b/#", True),  # literal absorbed by ``#`` tail
        ("a/#", "a/b/c", True),
        ("a/b/+", "a/b/c", True),  # ``+`` matches any single segment
        # MQTT 3.1.1 §4.7 ``$``-topic rule: a leading-wildcard filter
        # (``#`` or ``+``) does not match ``$``-prefixed topics.
        ("#", "$SYS/broker/version", False),
        ("$SYS/broker/version", "#", False),
        ("+", "$SYS/foo", False),
        ("+/foo", "$SYS/foo", False),
        ("$SYS/foo", "+/foo", False),
        ("$SYS/#", "+/foo", False),
        # Within ``$``-prefixed topics, the usual overlap rules apply
        # (no exclusion because no leading wildcard on either side).
        ("$SYS/#", "$SYS/+/info", True),
        ("$SYS/+/info", "$SYS/#", True),
        ("$SYS/+", "$SYS/foo", True),
    ],
)
def test_patterns_overlap(a: str, b: str, expected: bool) -> None:
    assert _patterns_overlap(a, b) == expected


# ---------------------------------------------------------------------------
# Repairs: _patterns_overlap regression tests (parent + $-topic)
# ---------------------------------------------------------------------------


def test_patterns_overlap_trailing_hash_matches_parent() -> None:
    """Regression: a trailing ``#`` is treated as also matching its
    parent topic, so an exact-topic filter and a subtree filter under
    the same parent must be reported as overlapping.

    Without the parent-level rule, ``a/b`` and ``a/b/#`` would be
    reported as disjoint even though the subtree filter obviously
    covers the parent's hierarchy and the user almost certainly
    intended them as related.
    """
    assert _patterns_overlap("a/b", "a/b/#") is True
    assert _patterns_overlap("a/b/#", "a/b") is True
    # Same rule at a deeper level.
    assert _patterns_overlap("home/+/sensors", "home/+/sensors/#") is True
    # The rule is specifically about a trailing ``#``; a literal
    # sub-topic (no ``#``) is still disjoint from its parent.
    assert _patterns_overlap("a/b/c", "a/b") is False


def test_patterns_overlap_dollar_topics_excluded_for_leading_wildcard() -> None:
    """Regression: a leading-wildcard filter (``#`` or ``+``) does
    not overlap ``$``-prefixed filters per MQTT 3.1.1 §4.7.

    The broker never delivers ``$SYS/...`` to a subscriber whose
    first level is a wildcard, so two config entries respectively
    subscribed to ``#`` and ``$SYS/#`` cannot share any real topic
    and must not be reported as overlapping. Same for ``+`` vs
    ``$SYS/...`` and ``+/foo`` vs ``$SYS/foo``.
    """
    # Bare ``#`` does not overlap any ``$``-prefixed filter.
    assert _patterns_overlap("#", "$SYS/#") is False
    assert _patterns_overlap("$SYS/#", "#") is False
    assert _patterns_overlap("#", "$SYS/broker/version") is False
    # ``+`` (single-level wildcard) does not overlap ``$``-prefixed.
    assert _patterns_overlap("+", "$SYS/foo") is False
    assert _patterns_overlap("$SYS/foo", "+") is False
    # ``+/foo`` does not overlap ``$SYS/foo`` (the leading ``+``
    # would have to match ``$SYS``, which it does not).
    assert _patterns_overlap("+/foo", "$SYS/foo") is False
    assert _patterns_overlap("$SYS/foo", "+/foo") is False
    # Sanity: when no leading-wildcard side exists, the usual
    # ``$``-internal overlap rules still apply.
    assert _patterns_overlap("$SYS/#", "$SYS/+/info") is True
    assert _patterns_overlap("$SYS/+/info", "$SYS/+/info") is True


# ---------------------------------------------------------------------------
# Repairs: check_overlapping_topics with a fake ir
# ---------------------------------------------------------------------------


@dataclass
class _FakeIrCall:
    domain: str
    issue_id: str
    kwargs: dict[str, Any] = field(default_factory=dict)


@dataclass
class _FakeIr:
    """A minimal IssueRegistry fake that records create/delete calls.

    The real ``ir`` exposes ``IssueSeverity`` (a class with WARNING /
    ERROR constants). We replicate that class attribute here so the
    real ``ir.IssueSeverity.WARNING`` access in ``repairs.py`` works
    without the integration code being aware of the test fake.
    """

    IssueSeverity: type = field(default_factory=lambda: types.SimpleNamespace(WARNING="warning"))
    created: list[_FakeIrCall] = field(default_factory=list)
    deleted: list[_FakeIrCall] = field(default_factory=list)

    def async_create_issue(self, hass, domain, issue_id, **kwargs):
        self.created.append(_FakeIrCall(domain=domain, issue_id=issue_id, kwargs=kwargs))
        return "ignored"

    def async_delete_issue(self, hass, domain, issue_id):
        self.deleted.append(_FakeIrCall(domain=domain, issue_id=issue_id))


@dataclass
class _FakeEntry:
    entry_id: str
    data: dict
    title: str = ""


@dataclass
class _FakeConfigEntries:
    entries: list[_FakeEntry] = field(default_factory=list)

    def async_entries(self, domain):
        return [e for e in self.entries if getattr(e, "entry_id", None) is not None]


@dataclass
class _FakeHass:
    config_entries: _FakeConfigEntries = field(default_factory=_FakeConfigEntries)


def _patch_ir(monkeypatch, fake_ir):
    monkeypatch.setattr("custom_components.telegraf_mqtt.ir", fake_ir)


def test_overlap_detection_raises_repair_issue(monkeypatch) -> None:
    """When two entries have overlapping patterns, the check raises a
    Repairs issue for each pair.
    """
    fake_ir = _FakeIr()
    _patch_ir(monkeypatch, fake_ir)
    hass = _FakeHass(
        config_entries=_FakeConfigEntries(
            entries=[
                _FakeEntry(entry_id="A", data={"topic_pattern": "telegraf/#"}),
                _FakeEntry(entry_id="B", data={"topic_pattern": "telegraf/+/cpu"}),
            ]
        )
    )
    entry = _FakeEntry(entry_id="A", data={"topic_pattern": "telegraf/#"})

    overlapping = check_overlapping_topics(hass, entry)
    assert overlapping == ["B"]
    # An issue was created for the B entry's overlap with A.
    assert any(c.issue_id == "overlap_topic_patterns_A_B" for c in fake_ir.created)


def test_overlap_detection_no_match_creates_no_issue(monkeypatch) -> None:
    fake_ir = _FakeIr()
    _patch_ir(monkeypatch, fake_ir)
    hass = _FakeConfigEntries(
        entries=[
            _FakeEntry(entry_id="A", data={"topic_pattern": "telegraf/#"}),
            _FakeEntry(entry_id="B", data={"topic_pattern": "sensors/+/temp"}),
        ]
    )
    from types import SimpleNamespace

    hass_obj = SimpleNamespace(config_entries=hass)
    entry = _FakeEntry(entry_id="A", data={"topic_pattern": "telegraf/#"})

    overlapping = check_overlapping_topics(hass_obj, entry)
    assert overlapping == []
    assert fake_ir.created == []


def test_overlap_auto_resolves_when_fixed(monkeypatch) -> None:
    """When the overlap is gone on a subsequent run, the prior issue
    is deleted (not just left dangling).
    """
    fake_ir = _FakeIr()
    _patch_ir(monkeypatch, fake_ir)
    from types import SimpleNamespace

    # First run: A overlaps with B -> issue created.
    hass_obj = SimpleNamespace(
        config_entries=_FakeConfigEntries(
            entries=[
                _FakeEntry(entry_id="A", data={"topic_pattern": "telegraf/#"}),
                _FakeEntry(entry_id="B", data={"topic_pattern": "telegraf/+/cpu"}),
            ]
        )
    )
    entry = _FakeEntry(entry_id="A", data={"topic_pattern": "telegraf/#"})
    check_overlapping_topics(hass_obj, entry)
    assert any(c.issue_id == "overlap_topic_patterns_A_B" for c in fake_ir.created)

    # Second run: B has been changed to a non-overlapping pattern.
    fake_ir.created.clear()
    hass_obj = SimpleNamespace(
        config_entries=_FakeConfigEntries(
            entries=[
                _FakeEntry(entry_id="A", data={"topic_pattern": "telegraf/#"}),
                _FakeEntry(entry_id="B", data={"topic_pattern": "other/+/cpu"}),
            ]
        )
    )
    check_overlapping_topics(hass_obj, entry)
    # No new issue created; the prior one is deleted.
    assert fake_ir.created == []
    assert any(d.issue_id == "overlap_topic_patterns_A_B" for d in fake_ir.deleted)


def test_overlap_detection_uses_ir_none_returns_silently(monkeypatch) -> None:
    """When ``ir`` is None (import-isolation), the check is a no-op."""
    monkeypatch.setattr("custom_components.telegraf_mqtt.ir", None)
    hass = _FakeHass(
        config_entries=_FakeConfigEntries(
            entries=[
                _FakeEntry(entry_id="A", data={"topic_pattern": "telegraf/#"}),
                _FakeEntry(entry_id="B", data={"topic_pattern": "telegraf/+/cpu"}),
            ]
        )
    )
    entry = _FakeEntry(entry_id="A", data={"topic_pattern": "telegraf/#"})
    assert check_overlapping_topics(hass, entry) == []


def test_overlap_detection_ignores_entries_without_pattern(monkeypatch) -> None:
    """Entries with no ``topic_pattern`` data (legacy / pre-repair)
    do not produce overlap issues and themselves are skipped.
    """
    fake_ir = _FakeIr()
    _patch_ir(monkeypatch, fake_ir)
    hass = _FakeHass(
        config_entries=_FakeConfigEntries(
            entries=[
                _FakeEntry(entry_id="A", data={"topic_pattern": "telegraf/#"}),
                _FakeEntry(entry_id="B", data={}),  # no pattern
                _FakeEntry(entry_id="C", data={"topic_pattern": ""}),  # empty
            ]
        )
    )
    entry = _FakeEntry(entry_id="A", data={"topic_pattern": "telegraf/#"})
    overlapping = check_overlapping_topics(hass, entry)
    assert overlapping == []
    assert fake_ir.created == []


def test_overlap_detection_no_own_pattern_returns_empty(monkeypatch) -> None:
    """If the calling entry itself has no topic pattern, the
    function is a no-op (a pre-config entry is not the integration's
    concern).
    """
    fake_ir = _FakeIr()
    _patch_ir(monkeypatch, fake_ir)
    hass = _FakeHass(
        config_entries=_FakeConfigEntries(
            entries=[
                _FakeEntry(entry_id="A", data={}),  # no own pattern
                _FakeEntry(entry_id="B", data={"topic_pattern": "telegraf/#"}),
            ]
        )
    )
    entry = _FakeEntry(entry_id="A", data={})
    assert check_overlapping_topics(hass, entry) == []
    assert fake_ir.created == []


# ---------------------------------------------------------------------------
# Repairs: check_invalid_persisted_option
# ---------------------------------------------------------------------------


def test_invalid_persisted_option_raises_repair(monkeypatch) -> None:
    fake_ir = _FakeIr()
    _patch_ir(monkeypatch, fake_ir)
    hass = object()  # any object: only the fake_ir uses it
    entry = _FakeEntry(entry_id="E", data={})
    check_invalid_persisted_option(hass, entry, ["expire_after", "enable_cleanup"])
    assert len(fake_ir.created) == 1
    issue = fake_ir.created[0]
    assert issue.issue_id == "invalid_persisted_option_E"
    assert "expire_after" in issue.kwargs["translation_placeholders"]["options"]
    assert "enable_cleanup" in issue.kwargs["translation_placeholders"]["options"]


def test_invalid_persisted_option_no_keys_clears_issue(monkeypatch) -> None:
    """An empty invalid list deletes the prior issue."""
    fake_ir = _FakeIr()
    _patch_ir(monkeypatch, fake_ir)
    hass = object()
    entry = _FakeEntry(entry_id="E", data={})
    check_invalid_persisted_option(hass, entry, [])
    assert fake_ir.created == []
    assert any(d.issue_id == "invalid_persisted_option_E" for d in fake_ir.deleted)


def test_invalid_persisted_option_uses_defaults_in_message(monkeypatch) -> None:
    """The Repair message shows the default that was used, so the user
    can see what to change to.
    """
    fake_ir = _FakeIr()
    _patch_ir(monkeypatch, fake_ir)
    check_invalid_persisted_option(object(), _FakeEntry("E", {}), ["expire_after"])
    msg = fake_ir.created[0].kwargs["translation_placeholders"]["defaults"]
    assert "expire_after=120" in msg


# ---------------------------------------------------------------------------
# Diagnostics end-to-end via real-harness entity registry / config flow
# (covered by tests/test_diagnostics.py). This module exercises the
# parser + repairs side which is harness-free by design.
# ---------------------------------------------------------------------------


def test_diagnostics_no_topic_in_options_validity_when_absent() -> None:
    """If the user did not override CONF_TOPIC_PATTERN in options (it
    is a config-field, not an options-field), the validity map
    still works.
    """
    from custom_components.telegraf_mqtt.diagnostics import _options_validity

    assert "topic_pattern" not in _options_validity({})
    assert _options_validity({"expire_after": 30})["expire_after"] is True
    assert _options_validity({"expire_after": "abc"})["expire_after"] is False


def test_diagnostics_options_validity_marks_invalid_topic_and_overrides() -> None:
    """Topic must be a non-empty string without embedded NUL bytes;
    field overrides must be a dict of ``str -> dict``.
    """
    from custom_components.telegraf_mqtt.diagnostics import _options_validity

    v = _options_validity({"topic_pattern": "abc\x00def", "field_overrides": {"f": "not-a-dict"}})
    assert v["topic_pattern"] is False
    assert v["field_overrides"] is False

    v2 = _options_validity({"topic_pattern": "ok/topic", "field_overrides": {"f": {"unit": "%"}}})
    assert v2["topic_pattern"] is True
    assert v2["field_overrides"] is True


def test_diagnostics_options_validity_marks_invalid_enable_cleanup() -> None:
    """``enable_cleanup`` must be a JSON-bool; a string or int is invalid."""
    from custom_components.telegraf_mqtt.diagnostics import _options_validity

    assert _options_validity({"enable_cleanup": "yes"})["enable_cleanup"] is False
    assert _options_validity({"enable_cleanup": 1})["enable_cleanup"] is False
    assert _options_validity({"enable_cleanup": True})["enable_cleanup"] is True
    assert _options_validity({})["enable_cleanup"] is True


# --------------------------------------------------------------------------
# _coerce_int_option / _coerce_bool_option / _options_from_entry_with_repair
# direct coverage. These helpers are the recovery path; without tests
# here the line-coverage gate would fail.
# --------------------------------------------------------------------------


def test_coerce_int_option_marks_uncoercible_and_below_minimum() -> None:
    # Import the package (not its __init__ submodule) so the helpers
    # resolve through the same module object that ``_patch_ir`` mutates
    # (``custom_components.telegraf_mqtt.ir``).
    from custom_components.telegraf_mqtt import _coerce_int_option

    # Uncoercible strings -> default + invalid.
    value, bad = _coerce_int_option({"x": "abc"}, "x", 10)
    assert value == 10 and bad is True
    # None and other non-numeric types too.
    value, bad = _coerce_int_option({"x": None}, "x", 10)
    assert value == 10 and bad is True
    # Floats that *can* be coerced -> ok.
    value, bad = _coerce_int_option({"x": 3.0}, "x", 10)
    assert value == 3 and bad is False
    # Boolean and non-integral fractional values are rejected rather than
    # silently coerced (int(True)==1, int(3.7)==3) so a Repair issue is raised.
    value, bad = _coerce_int_option({"x": True}, "x", 10)
    assert value == 10 and bad is True
    value, bad = _coerce_int_option({"x": 3.7}, "x", 10)
    assert value == 10 and bad is True
    # Below the minimum -> default + invalid.
    value, bad = _coerce_int_option({"x": 0}, "x", 10, minimum=1)
    assert value == 10 and bad is True
    # Absent key -> default + ok.
    value, bad = _coerce_int_option({}, "x", 10)
    assert value == 10 and bad is False


def test_coerce_bool_option_marks_non_bool() -> None:
    # Import the package (not its __init__ submodule) so the helpers
    # resolve through the same module object that ``_patch_ir`` mutates
    # (``custom_components.telegraf_mqtt.ir``).
    from custom_components.telegraf_mqtt import _coerce_bool_option

    # A string is not a JSON-bool.
    value, bad = _coerce_bool_option({"x": "yes"}, "x", True)
    assert value is True and bad is True
    # An int is not a bool either (Python True is 1 trap).
    value, bad = _coerce_bool_option({"x": 1}, "x", False)
    assert value is False and bad is True
    # Real bools -> ok.
    value, bad = _coerce_bool_option({"x": True}, "x", False)
    assert value is True and bad is False


def test_options_from_entry_with_repair_collects_invalid_keys(monkeypatch) -> None:
    """Integration: a config entry with multiple invalid options
    surfaces all of them in a single Repairs issue and uses defaults
    for the rest."""
    # Import the package (not its __init__ submodule) so the helper
    # resolves through the same module object that ``_patch_ir`` mutates
    # (``custom_components.telegraf_mqtt.ir``).
    from custom_components.telegraf_mqtt import _options_from_entry_with_repair

    fake_ir = _FakeIr()
    _patch_ir(monkeypatch, fake_ir)

    @dataclass
    class _E:
        entry_id: str = "E"
        options: dict = field(
            default_factory=lambda: {
                "expire_after": "abc",
                "cleanup_delay": -1,
                "delete_delay": "nope",
                "min_active_metrics": -5,
                "enable_cleanup": "yes",
            }
        )

    options, invalid = _options_from_entry_with_repair(object(), _E())
    assert set(invalid) == {
        "expire_after",
        "cleanup_delay",
        "delete_delay",
        "min_active_metrics",
        "enable_cleanup",
    }
    # Defaults were substituted for the invalid fields.
    assert options.expire_after == 120
    # cleanup_delay=-1 falls back to the documented default.
    from custom_components.telegraf_mqtt.const import (
        DEFAULT_CLEANUP_DELAY,
        DEFAULT_DELETE_DELAY,
        DEFAULT_ENABLE_CLEANUP,
        DEFAULT_MIN_ACTIVE_METRICS,
    )

    assert options.cleanup_delay == DEFAULT_CLEANUP_DELAY
    assert options.delete_delay == DEFAULT_DELETE_DELAY
    assert options.min_active_metrics == DEFAULT_MIN_ACTIVE_METRICS
    assert options.enable_cleanup is DEFAULT_ENABLE_CLEANUP
    # The repairs helper was called with the right set.
    assert len(fake_ir.created) == 1
    placeholders = fake_ir.created[0].kwargs["translation_placeholders"]
    for key in ("expire_after", "cleanup_delay", "enable_cleanup"):
        assert key in placeholders["options"]


def test_options_from_entry_with_repair_no_invalid_clears_issue(monkeypatch) -> None:
    """When all options are valid, the prior invalid_persisted_option
    issue is deleted."""
    import importlib

    integration_module = importlib.import_module("custom_components.telegraf_mqtt.__init__")

    fake_ir = _FakeIr()
    _patch_ir(monkeypatch, fake_ir)

    @dataclass
    class _E:
        entry_id: str = "E"
        options: dict = field(default_factory=lambda: {"expire_after": 60, "cleanup_delay": 10})

    options, invalid = integration_module._options_from_entry_with_repair(object(), _E())
    assert invalid == []
    assert options.expire_after == 60
    assert fake_ir.created == []
    assert any(d.issue_id == "invalid_persisted_option_E" for d in fake_ir.deleted)


def test_options_from_entry_recovers_invalid_persisted_values() -> None:
    """``_options_from_entry`` (live-update / expiry-scheduling path)
    shares setup's safe normalization: corrupted values such as
    ``expire_after='abc'`` or ``cleanup_delay=None`` substitute the
    documented defaults instead of raising, so coercion errors can
    never escape setup or the live listeners."""
    from custom_components.telegraf_mqtt import _options_from_entry
    from custom_components.telegraf_mqtt.const import (
        DEFAULT_CLEANUP_DELAY,
        DEFAULT_DELETE_DELAY,
        DEFAULT_ENABLE_CLEANUP,
        DEFAULT_EXPIRE_AFTER,
        DEFAULT_MIN_ACTIVE_METRICS,
    )

    @dataclass
    class _E:
        options: dict = field(
            default_factory=lambda: {
                "expire_after": "abc",  # int('abc') used to raise ValueError
                "cleanup_delay": None,  # int(None) used to raise TypeError
                "delete_delay": "nope",
                "min_active_metrics": -5,
                "enable_cleanup": "yes",
            }
        )

    options = _options_from_entry(_E())
    assert options.expire_after == DEFAULT_EXPIRE_AFTER
    assert options.cleanup_delay == DEFAULT_CLEANUP_DELAY
    assert options.delete_delay == DEFAULT_DELETE_DELAY
    assert options.min_active_metrics == DEFAULT_MIN_ACTIVE_METRICS
    assert options.enable_cleanup is DEFAULT_ENABLE_CLEANUP


def test_normalize_options_marks_unknown_device_id_strategy() -> None:
    """An unknown ``device_id_strategy`` must fall back to the default and
    surface ``CONF_DEVICE_ID_STRATEGY`` in the invalid-keys list (so a
    Repair issue is raised) -- otherwise a corrupted persisted value
    would silently downgrade to the default without the user ever
    seeing a Repairs hint.
    """
    # Import the package (not its ``__init__`` submodule) so the helper
    # resolves through the same module object that ``_patch_ir`` mutates.
    from custom_components.telegraf_mqtt import _normalize_options

    # Unknown strategy (a typo, empty string, or older/missing entry
    # would all land here): invalid + default fallback.
    options, invalid = _normalize_options({CONF_DEVICE_ID_STRATEGY: "unknown_strategy"})
    assert CONF_DEVICE_ID_STRATEGY in invalid
    assert options.device_id_strategy == DEFAULT_DEVICE_ID_STRATEGY

    # Empty string is also not a known strategy.
    options, invalid = _normalize_options({CONF_DEVICE_ID_STRATEGY: ""})
    assert CONF_DEVICE_ID_STRATEGY in invalid
    assert options.device_id_strategy == DEFAULT_DEVICE_ID_STRATEGY

    # Each known strategy passes through verbatim and is not flagged.
    for strategy in VALID_DEVICE_ID_STRATEGIES:
        options, invalid = _normalize_options({CONF_DEVICE_ID_STRATEGY: strategy})
        assert CONF_DEVICE_ID_STRATEGY not in invalid
        assert options.device_id_strategy == strategy

    # Absent key -> default + ok.
    options, invalid = _normalize_options({})
    assert CONF_DEVICE_ID_STRATEGY not in invalid
    assert options.device_id_strategy == DEFAULT_DEVICE_ID_STRATEGY


def test_normalize_options_marks_non_bool_auto_discover() -> None:
    """A non-bool ``auto_discover`` value must fall back to the default
    and surface ``CONF_AUTO_DISCOVER`` in the invalid-keys list.
    """
    # Import the package (not its ``__init__`` submodule) so the helper
    # resolves through the same module object that ``_patch_ir`` mutates.
    from custom_components.telegraf_mqtt import _normalize_options

    # String is not a JSON-bool.
    options, invalid = _normalize_options({CONF_AUTO_DISCOVER: "yes"})
    assert CONF_AUTO_DISCOVER in invalid
    assert options.auto_discover is DEFAULT_AUTO_DISCOVER

    # An int is not a bool either (Python ``True is 1`` trap).
    options, invalid = _normalize_options({CONF_AUTO_DISCOVER: 1})
    assert CONF_AUTO_DISCOVER in invalid
    assert options.auto_discover is DEFAULT_AUTO_DISCOVER

    # Real bools pass through verbatim and are not flagged (both polarities).
    options, invalid = _normalize_options({CONF_AUTO_DISCOVER: True})
    assert CONF_AUTO_DISCOVER not in invalid
    assert options.auto_discover is True
    options, invalid = _normalize_options({CONF_AUTO_DISCOVER: False})
    assert CONF_AUTO_DISCOVER not in invalid
    assert options.auto_discover is False

    # Absent key -> default + ok.
    options, invalid = _normalize_options({})
    assert CONF_AUTO_DISCOVER not in invalid
    assert options.auto_discover is DEFAULT_AUTO_DISCOVER
