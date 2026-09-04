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
import contextlib
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

import pytest

from custom_components.telegraf_mqtt.config_flow import (
    _MAX_PICK_LIST_OPTIONS,
    TelegrafMqttConfigFlow,
    _looks_telegraf_shaped,
    _pick_topics_schema,
    _roll_up_topics,
)
from custom_components.telegraf_mqtt.const import (
    CONF_SCAN_DURATION_SECONDS,
    CONF_SCAN_ROOT_TOPIC,
    CONF_SETUP_MODE,
    CONF_TOPIC_PATTERN,
    DEFAULT_AUTO_DISCOVER,
    DEFAULT_AUTO_DISCOVER_PROBE_TOPIC,
    SETUP_MODE_DISCOVER,
)
from custom_components.telegraf_mqtt.snoop import SnoopListener, SnoopResult, derive_probe_topic


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
    seen = frozenset(f"telegraf/host{i}/{kind}" for i in range(50) for kind in ("cpu", "mem", "disk"))
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
            self.subscribed_topics = [entry for entry in self.subscribed_topics if entry[1] is not callback]

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
        # A hand-typed junk topic must fail the validator so the typo
        # cannot silently create an entry.
        result = await flow.async_step_pick_topics({CONF_TOPIC_PATTERN: "garbage/topic/#/bad"})
        assert result["type"] == "form"
        assert result["step_id"] == "pick_topics"
        assert result["errors"] == {CONF_TOPIC_PATTERN: "invalid_topic"}

    asyncio.run(_run())
    assert len(forms) == 1
    assert forms[0]["step_id"] == "pick_topics"
    assert forms[0]["errors"] == {CONF_TOPIC_PATTERN: "invalid_topic"}


def test_pick_topics_no_picks_returns_no_topics_selected_error() -> None:
    """Submitting the pick form without a pick -- the key omitted
    entirely (an untouched optional select) or an explicitly empty
    value -- surfaces ``no_topics_selected`` and re-shows the form.
    The HA-harness config flow is the canonical pin; this harness-free
    test is the trip wire against a refactor that silently accepts an
    empty pick.
    """
    seen = frozenset({"telegraf/rack1/cpu"})
    flow, forms = _make_flow_with_seen(seen)

    async def _run() -> None:
        result = await flow.async_step_pick_topics({})
        assert result["type"] == "form"
        assert result["step_id"] == "pick_topics"
        assert result["errors"] == {CONF_TOPIC_PATTERN: "no_topics_selected"}

        result = await flow.async_step_pick_topics({CONF_TOPIC_PATTERN: ""})
        assert result["errors"] == {CONF_TOPIC_PATTERN: "no_topics_selected"}

    asyncio.run(_run())
    assert len(forms) == 2
    assert all(form["step_id"] == "pick_topics" for form in forms)
    assert forms[0]["errors"] == {CONF_TOPIC_PATTERN: "no_topics_selected"}
    assert forms[1]["errors"] == {CONF_TOPIC_PATTERN: "no_topics_selected"}


def test_pick_topics_schema_pre_selects_only_telegraf_shaped_prefixes() -> None:
    """The pick form's ``default=`` is the first Telegraf-shaped
    prefix, never a non-Telegraf one.

    Pin the wiring between ``_looks_telegraf_shaped`` and
    ``_pick_topics_schema`` so a refactor that accidentally swaps
    ``pre_selected`` for ``prefixes`` (or drops the pre-selection
    altogether) is caught here: the user would otherwise be greeted
    by a pre-selected HA-internal topic like
    ``homeassistant/sensor/#``.
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
    # pre-selection or reorders the args is caught. ``_roll_up_topics``
    # returns a sorted list, so ``pre_selected[0]`` is deterministic.
    pre_selected = [p for p in prefixes if _looks_telegraf_shaped(p)]
    # Pin the specific pre-selected list to make the test read as a
    # contract: HA-internal + non-Telegraf prefixes are excluded;
    # only the Telegraf-shaped 2nd-level ones are checked.
    assert pre_selected == [
        "telegraf/rack1/#",
        "telegraf/rack2/#",
    ]
    schema = _pick_topics_schema(prefixes, pre_selected)
    # ``vol.Schema({...})`` materialises the default at construction
    # time, so we can read it back via ``schema({})`` -- an empty
    # input lets the default fire. The single-select form defaults to
    # the FIRST Telegraf-shaped prefix.
    result = schema({})
    assert result[CONF_TOPIC_PATTERN] == "telegraf/rack1/#"
    # With nothing Telegraf-shaped, the field has no default: the key
    # stays absent and the step owns the friendly
    # ``no_topics_selected`` error (voluptuous validates defaults, so
    # a ``default=None`` sentinel is not an option here).
    empty_schema = _pick_topics_schema(prefixes, [])
    assert CONF_TOPIC_PATTERN not in empty_schema({})


# ---------------------------------------------------------------------------
# Discover-path: the full scan pipeline (settings -> running -> pick).
# ---------------------------------------------------------------------------
# The v1.3.0 release shipped the discover runtime path in ``config_flow.py``
# (scan-settings submit, the scan window, the pick-submit create path, and
# ``_start_scan``) without any test driving it, which silently broke the
# repo's 100% coverage gate: ``config_flow.py`` sat at ~80% in every run.
# These tests restore that coverage harness-free -- the REAL step methods
# run against a fake broker and a flow whose ``hass.loop.call_later`` parks
# the snoop's auto-stop timer, so each test fires the stop deterministically
# instead of sleeping out the scan window.


class _ParkedTimer:
    """``TimerHandle``-shaped double returned by ``_FakeFlowLoop.call_later``."""

    def __init__(self) -> None:
        self.cancelled = False

    def cancel(self) -> None:
        self.cancelled = True


class _FakeFlowLoop:
    """Event-loop double: ``call_later`` parks the snoop's auto-stop timer.

    The real loop would fire the timer after the full scan window (>= 5 s
    of wall time); parking it lets each test inject traffic and then fire
    the stop synchronously and instantly.
    """

    def __init__(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self.timers: list[_ParkedTimer] = []

    def call_later(self, _delay: float, _callback: Callable[..., None], *_args: Any) -> _ParkedTimer:
        timer = _ParkedTimer()
        self.timers.append(timer)
        return timer

    def __getattr__(self, name: str) -> Any:
        # Everything else (call_soon, call_at, time, ...) delegates to the
        # real running loop.
        return getattr(self._loop, name)


def _make_scan_flow() -> tuple[TelegrafMqttConfigFlow, list[dict[str, Any]], list[Callable[[], None]]]:
    """Build a config flow ready for the scan-pipeline tests.

    Stubs the FlowHandler result-builders (a bare instance carries no
    ``flow_id``, so the real methods cannot run outside HA's flow manager
    -- same pattern as ``_make_flow_with_seen``) and arms an
    ``async_on_unload`` recorder so ``_start_scan``'s teardown registration
    is observable regardless of the installed HA version. Returns
    ``(flow, forms, unload_hooks)``.

    Also stubs the progress-step API (``async_show_progress``,
    ``async_show_progress_done``, ``async_update_progress``) and the
    ``hass.async_create_task`` runner so the progress-based scan flow
    can be exercised without a real HA flow manager. The progress
    events are recorded on ``flow._progress_events`` for assertion.
    """
    flow = TelegrafMqttConfigFlow()
    forms: list[dict[str, Any]] = []
    progress_events: list[dict[str, Any]] = []

    def _show_form(*, step_id: str, data_schema: Any = None, errors: dict | None = None, **_kw: Any) -> dict[str, Any]:
        record = {"type": "form", "step_id": step_id, "data_schema": data_schema, "errors": dict(errors or {})}
        forms.append(record)
        return record

    def _create_entry(*, title: str | None = None, data: Any = None, **_kw: Any) -> dict[str, Any]:
        return {"type": "create_entry", "title": title, "data": dict(data or {})}

    def _show_progress(
        *,
        step_id: str,
        progress_action: str,
        description_placeholders: dict | None = None,
        progress_task: Any = None,
        **_kw: Any,
    ) -> dict[str, Any]:
        record = {
            "type": "progress",
            "step_id": step_id,
            "progress_action": progress_action,
            "description_placeholders": dict(description_placeholders or {}),
            "progress_task": progress_task,
        }
        forms.append(record)
        return record

    def _show_progress_done(*, next_step_id: str, **_kw: Any) -> dict[str, Any]:
        record = {"type": "progress_done", "next_step_id": next_step_id}
        forms.append(record)
        return record

    def _update_progress(progress: float) -> None:
        progress_events.append({"progress": progress})

    async def _set_unique_id(*_a: Any, **_kw: Any) -> None:
        return None

    flow.async_show_form = _show_form  # type: ignore[method-assign]
    flow.async_create_entry = _create_entry  # type: ignore[method-assign]
    flow.async_set_unique_id = _set_unique_id  # type: ignore[method-assign,assignment]
    flow._abort_if_unique_id_configured = lambda: None  # type: ignore[method-assign]
    flow.async_show_progress = _show_progress  # type: ignore[method-assign]
    flow.async_show_progress_done = _show_progress_done  # type: ignore[method-assign]
    flow.async_update_progress = _update_progress  # type: ignore[method-assign]
    flow._progress_events = progress_events  # type: ignore[attr-defined]
    unload_hooks: list[Callable[[], None]] = []
    flow.async_on_unload = unload_hooks.append  # type: ignore[method-assign]
    # ``hass.loop`` is deliberately not armed here: ``get_running_loop()``
    # only works inside a coroutine, so the scan-pipeline tests arm the
    # fake loop inside their ``_run()`` before starting the step task.
    flow.hass = type("H", (), {})()  # type: ignore[assignment]

    # ``async_create_task`` runs the coroutine on the running loop and
    # returns a task. We wrap it so the test can introspect the task
    # and force it to completion.
    def _create_task(coro: Any) -> asyncio.Task[Any]:
        return asyncio.ensure_future(coro)

    flow.hass.async_create_task = _create_task  # type: ignore[attr-defined]
    return flow, forms, unload_hooks


def test_scan_settings_invalid_submit_rerenders_with_errors() -> None:
    """A bad probe root and an out-of-range duration rerender the form.

    Covers the submit branch of ``async_step_scan_settings`` including the
    schema rebuild for the error render.
    """
    flow, forms, _hooks = _make_scan_flow()

    async def _run() -> dict[str, Any]:
        return await flow.async_step_scan_settings(
            {CONF_SCAN_ROOT_TOPIC: "telegraf/#/bad", CONF_SCAN_DURATION_SECONDS: 1}
        )

    result = asyncio.run(_run())
    assert result["type"] == "form"
    assert result["step_id"] == "scan_settings"
    assert result["errors"] == {
        CONF_SCAN_ROOT_TOPIC: "invalid_topic",
        CONF_SCAN_DURATION_SECONDS: "invalid_duration",
    }
    assert forms and forms[-1]["step_id"] == "scan_settings"


def test_user_menu_discover_branch_shows_scan_settings() -> None:
    """Choosing the discover mode routes ``async_step_user`` to the
    scan-settings form (the manual branch is pinned by the harness
    tests)."""
    flow, forms, _hooks = _make_scan_flow()

    async def _run() -> dict[str, Any]:
        return await flow.async_step_user({CONF_SETUP_MODE: SETUP_MODE_DISCOVER})

    result = asyncio.run(_run())
    assert result["type"] == "form"
    assert result["step_id"] == "scan_settings"
    assert forms and forms[-1]["step_id"] == "scan_settings"


def test_scan_pipeline_valid_settings_reaches_pick_topics(monkeypatch: pytest.MonkeyPatch) -> None:
    """Settings submit -> live snoop scan -> pick list, end to end.

    The fake loop parks the snoop's auto-stop timer: the test injects
    rack1 traffic while the scan is running and then fires the parked
    stop, so the whole scan window elapses instantly. Pins the
    ``_start_scan`` teardown contract: the snoop's ``stop`` is registered
    on the flow's unload hooks (so a user cancelling the flow mid-scan
    cannot leak the broker subscription), and ``stop()`` cancels the
    parked timer and unsubscribes.

    The progress-based scan flow returns a ``progress`` result from
    ``async_step_scan_settings`` (which chains to
    ``async_step_scan_running``), and the flow's progress task must
    complete before the manager re-invokes the step and chains to
    ``async_step_pick_topics`` via ``async_show_progress_done``.
    """
    mqtt = _FakeMqtt()
    monkeypatch.setattr("homeassistant.components.mqtt.async_subscribe", mqtt.async_subscribe)
    flow, forms, unload_hooks = _make_scan_flow()

    async def _run() -> dict[str, Any]:
        flow.hass.loop = _FakeFlowLoop(asyncio.get_running_loop())
        result = await flow.async_step_scan_settings(
            {CONF_SCAN_ROOT_TOPIC: "telegraf/#", CONF_SCAN_DURATION_SECONDS: 5}
        )
        # First call returns the progress result.
        assert result["type"] == "progress"
        assert result["step_id"] == "scan_running"
        assert result["progress_action"] == "scan_running"
        # Wait for the scan subscription to be installed on the broker.
        while not mqtt.subscribed_topics:
            await asyncio.sleep(0)
        assert mqtt.subscribed_topics[0][0] == "telegraf/#"
        # Traffic during the scan window: only rack1 shows up.
        await _drain(mqtt.subscribed_topics[0][1], "telegraf/rack1/cpu", b"")
        await _drain(mqtt.subscribed_topics[0][1], "telegraf/rack1/mem", b"")
        while flow._scan_snoop is None:
            await asyncio.sleep(0)
        # The unload hook is the snoop's own stop; firing the parked
        # auto-stop timer cancels the timer handle and unsubscribes.
        assert unload_hooks and unload_hooks[0].__self__ is flow._scan_snoop
        # Wait for the background _wait_for_scan task, then fire the
        # snoop's auto-stop to let it complete.
        scan_task = flow._scan_task
        assert scan_task is not None
        flow._scan_snoop._on_timeout()
        await asyncio.wait_for(scan_task, 5)
        assert flow.hass.loop.timers and flow.hass.loop.timers[0].cancelled
        assert mqtt.subscribed_topics == []
        # Re-invoking the step after the task completes chains to pick_topics.
        result = await flow.async_step_scan_running()
        assert result["type"] == "progress_done"
        assert result["next_step_id"] == "pick_topics"
        # The flow manager would then invoke async_step_pick_topics.
        result = await flow.async_step_pick_topics()
        return result

    result = asyncio.run(_run())
    assert result["type"] == "form"
    assert result["step_id"] == "pick_topics"
    assert flow._scan_seen_topics == {"telegraf/rack1/cpu", "telegraf/rack1/mem"}
    assert forms and forms[-1]["step_id"] == "pick_topics"
    # Progress events were emitted (at least one integer percentage).
    assert flow._progress_events, "no progress events were emitted"
    assert all(0.0 <= e["progress"] <= 1.0 for e in flow._progress_events)


def test_scan_pipeline_no_traffic_returns_to_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    """A scan window with zero traffic chains to the scan_no_traffic
    step which re-shows the settings form with a clear error, and the
    subscription is still torn down."""
    mqtt = _FakeMqtt()
    monkeypatch.setattr("homeassistant.components.mqtt.async_subscribe", mqtt.async_subscribe)
    flow, forms, _hooks = _make_scan_flow()

    async def _run() -> dict[str, Any]:
        flow.hass.loop = _FakeFlowLoop(asyncio.get_running_loop())
        result = await flow.async_step_scan_settings(
            {CONF_SCAN_ROOT_TOPIC: "telegraf/#", CONF_SCAN_DURATION_SECONDS: 5}
        )
        assert result["type"] == "progress"
        while not mqtt.subscribed_topics:
            await asyncio.sleep(0)
        while flow._scan_snoop is None:
            await asyncio.sleep(0)
        scan_task = flow._scan_task
        assert scan_task is not None
        flow._scan_snoop._on_timeout()
        await asyncio.wait_for(scan_task, 5)
        # Re-invoke: scan completed with no traffic -> chains to scan_no_traffic.
        result = await flow.async_step_scan_running()
        assert result["type"] == "progress_done"
        assert result["next_step_id"] == "scan_no_traffic"
        # The scan_no_traffic step re-shows the settings form with the error.
        result = await flow.async_step_scan_no_traffic()
        return result

    result = asyncio.run(_run())
    assert result["type"] == "form"
    assert result["step_id"] == "scan_settings"
    assert result["errors"] == {"base": "no_traffic_on_scan_root"}
    assert mqtt.subscribed_topics == []
    # The scan_no_traffic step was recorded in the forms log.
    scan_no_traffic_records = [f for f in forms if f.get("step_id") == "scan_settings"]
    assert scan_no_traffic_records
    assert scan_no_traffic_records[-1]["errors"] == {"base": "no_traffic_on_scan_root"}


def test_scan_pipeline_deadline_force_stops_snoop(monkeypatch: pytest.MonkeyPatch) -> None:
    """A snoop that never finishes is force-stopped at the deadline.

    Guards the leak path the scan relies on: if the auto-stop timer
    misfires (``is_finished`` stays False), the step's own
    ``asyncio.timeout`` deadline must stop the snoop explicitly -- the
    broker subscription cannot outlive the flow -- and the flow must
    still continue with whatever the snoop managed to capture.

    The progress task wraps the wait loop, so the deadline fires inside
    ``_wait_for_scan`` and the task completes with the snoop's
    captured topics. Re-invoking the step after the task completes
    chains to ``pick_topics`` with the captured data.
    """
    stop_calls = 0

    class _StuckSnoop:
        """Snoop double whose auto-stop never flips ``is_finished``."""

        @property
        def is_finished(self) -> bool:
            return False

        def stop(self) -> SnoopResult:
            nonlocal stop_calls
            stop_calls += 1
            return SnoopResult(
                hosts=frozenset({"rack1"}),
                topics=frozenset({"telegraf/rack1/cpu"}),
                duration_seconds=0.0,
            )

    async def _fake_start_scan(_self: Any, _probe: str, _duration: float) -> _StuckSnoop:
        return _StuckSnoop()

    @contextlib.asynccontextmanager
    async def _instant_timeout(_deadline: float) -> Any:
        raise TimeoutError
        yield  # pragma: no cover

    monkeypatch.setattr("custom_components.telegraf_mqtt.config_flow.asyncio.timeout", _instant_timeout)
    monkeypatch.setattr(TelegrafMqttConfigFlow, "_start_scan", _fake_start_scan)
    monkeypatch.setattr(TelegrafMqttConfigFlow, "_scan_duration", None)
    flow, forms, _hooks = _make_scan_flow()

    async def _run() -> dict[str, Any]:
        # First call: launches the background task and returns progress.
        result = await flow.async_step_scan_running()
        assert result["type"] == "progress"
        # The wait task should complete immediately (asyncio.timeout raised).
        scan_task = flow._scan_task
        assert scan_task is not None
        await asyncio.wait_for(scan_task, 5)
        # Re-invoking the step after the task completes chains to pick_topics.
        result = await flow.async_step_scan_running()
        assert result["type"] == "progress_done"
        assert result["next_step_id"] == "pick_topics"
        # The flow manager would then invoke async_step_pick_topics.
        result = await flow.async_step_pick_topics()
        return result

    result = asyncio.run(_run())
    # Force-stopped (not left running), then read once more for the result.
    assert stop_calls >= 1
    assert result["type"] == "form"
    assert result["step_id"] == "pick_topics"
    assert flow._scan_seen_topics == {"telegraf/rack1/cpu"}
    assert forms and forms[-1]["step_id"] == "pick_topics"


def test_scan_progress_emits_initial_event_before_snoop_finishes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The progress step emits an initial progress event (0.0) so the
    frontend renders the bar immediately, even when the snoop finishes
    on the first check (e.g. a parked auto-stop timer in tests)."""
    mqtt = _FakeMqtt()
    monkeypatch.setattr("homeassistant.components.mqtt.async_subscribe", mqtt.async_subscribe)
    flow, forms, _hooks = _make_scan_flow()

    async def _run() -> dict[str, Any]:
        flow.hass.loop = _FakeFlowLoop(asyncio.get_running_loop())
        result = await flow.async_step_scan_settings(
            {CONF_SCAN_ROOT_TOPIC: "telegraf/#", CONF_SCAN_DURATION_SECONDS: 5}
        )
        assert result["type"] == "progress"
        while flow._scan_snoop is None:
            await asyncio.sleep(0)
        # Fire the parked auto-stop to let the snoop finish immediately.
        flow._scan_snoop._on_timeout()
        scan_task = flow._scan_task
        assert scan_task is not None
        await asyncio.wait_for(scan_task, 5)
        return result

    asyncio.run(_run())
    # The initial progress event was emitted even though the snoop
    # finished on the first check (the parked timer never elapsed).
    assert flow._progress_events, "no initial progress event was emitted"
    assert flow._progress_events[0]["progress"] == 0.0
    # The forms log records the progress step with the expected
    # placeholders so the frontend can render the description.
    progress_records = [f for f in forms if f.get("type") == "progress"]
    assert progress_records
    assert progress_records[0]["description_placeholders"]["probe"] == "telegraf/#"
    assert progress_records[0]["description_placeholders"]["duration"] == "5"


def test_scan_progress_emits_increasing_fractions_during_scan(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The progress loop emits increasing fractions (0.0, then a value
    between 0 and 1) as the scan window elapses, not just the initial
    0.0 event. The snoop becomes finished only after a few ticks so the
    loop body executes."""
    mqtt = _FakeMqtt()
    monkeypatch.setattr("homeassistant.components.mqtt.async_subscribe", mqtt.async_subscribe)
    flow, forms, _hooks = _make_scan_flow()

    tick_count = [0]

    class _SlowSnoop:
        """Snoop double that finishes after ~3 ticks of the progress loop."""

        @property
        def is_finished(self) -> bool:
            tick_count[0] += 1
            return tick_count[0] >= 3

        def stop(self) -> SnoopResult:
            return SnoopResult(
                hosts=frozenset({"rack1"}),
                topics=frozenset({"telegraf/rack1/cpu"}),
                duration_seconds=0.0,
            )

    async def _fake_start_scan(self: Any, probe: str, duration: float) -> _SlowSnoop:
        return _SlowSnoop()

    monkeypatch.setattr(TelegrafMqttConfigFlow, "_start_scan", _fake_start_scan)

    async def _run() -> dict[str, Any]:
        flow.hass.loop = _FakeFlowLoop(asyncio.get_running_loop())
        # Set the scan params (normally done by async_step_scan_settings).
        flow._scan_root = "telegraf/#"
        flow._scan_duration = 1
        # Call the running step directly: the first entry launches the
        # background task and returns the progress result.
        result = await flow.async_step_scan_running()
        assert result["type"] == "progress"
        scan_task = flow._scan_task
        assert scan_task is not None
        # While the task is still running, re-invoking the step should
        # re-show progress (the not-done branch).
        result = await flow.async_step_scan_running()
        assert result["type"] == "progress"
        # Wait for the background task to complete.
        await asyncio.wait_for(scan_task, 5)
        # After the task completes, the step chains to the next one.
        result = await flow.async_step_scan_running()
        assert result["type"] == "progress_done"
        assert result["next_step_id"] == "pick_topics"
        # The flow manager would then invoke async_step_pick_topics.
        result = await flow.async_step_pick_topics()
        return result

    result = asyncio.run(_run())
    assert result["type"] == "form"
    assert result["step_id"] == "pick_topics"
    # The progress loop fired more than once: the initial 0.0 event plus
    # at least one mid-scan event (the snoop became finished after ~3
    # ticks, so the loop iterated at least twice before exiting).
    assert len(flow._progress_events) >= 2
    # Progress fractions are monotonically non-decreasing.
    fractions = [e["progress"] for e in flow._progress_events]
    assert fractions == sorted(fractions)
    assert fractions[0] == 0.0
    # The first re-show (not-done) branch is exercised: the forms log
    # records two progress entries (the initial launch and the re-show
    # while the task was still running).
    progress_records = [f for f in forms if f.get("type") == "progress"]
    assert len(progress_records) >= 2


def test_pick_topics_submit_creates_entry() -> None:
    """Submitting the (single) pick creates the entry with that exact
    pattern. The picker is single-select -- the runtime subscription
    supports exactly one pattern per entry -- so the submission is a
    single string and the device title derives from it."""
    flow, _forms, _hooks = _make_scan_flow()
    flow._scan_seen_topics = frozenset({"telegraf/rack1/cpu", "telegraf/rack2/cpu"})
    unique_ids: list[str] = []

    async def _set_unique_id(unique_id: str | None) -> None:
        unique_ids.append(unique_id or "")

    flow.async_set_unique_id = _set_unique_id  # type: ignore[method-assign,assignment]

    async def _run() -> dict[str, Any]:
        return await flow.async_step_pick_topics({CONF_TOPIC_PATTERN: "telegraf/rack2/#"})

    result = asyncio.run(_run())
    assert result["type"] == "create_entry"
    # The picked pattern is stored verbatim; the device title derives from it.
    assert result["data"][CONF_TOPIC_PATTERN] == "telegraf/rack2/#"
    assert result["title"] == "Telegraf"
    assert unique_ids == ["telegraf/rack2/#"]
