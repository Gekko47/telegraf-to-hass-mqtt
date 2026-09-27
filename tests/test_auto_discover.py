"""WS-I: auto-discover must actually work, on setup, live toggle, and reconfigure.

The feature this file pins was previously a no-op wearing the costume of a
feature. ``derive_probe_topic`` was the identity function, so the snoop
subscribed to exactly the filter the main subscription already held and
re-dispatched every message into ``process_message`` a second time: 2x the
parse, 2x the dispatcher fan-out, and not one additional host.

Three properties are asserted here, and each is a distinct user-visible
contract:

1. **Additive** -- a message the entry's ``topic_pattern`` already covers
   is recorded but not dispatched; one outside it is dispatched and its
   host becomes a device.
2. **Live** -- changing the scope while the snoop is running restarts the
   listener rather than leaving it on the old filter or opening a second
   subscription.
3. **Reconfigured** -- the pattern change a reconfigure commits re-points
   the snoop's ``exclude_filter``, and a pattern the broker rejects never
   reaches ``entry.data`` at all.

Plus the Repairs guard for a scope that can only ever re-see what the
entry already has.
"""

from __future__ import annotations

import asyncio
import types
from collections.abc import Callable
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any

import pytest

import custom_components.telegraf_mqtt as integration
from custom_components.telegraf_mqtt.config_flow import TelegrafMqttConfigFlow
from custom_components.telegraf_mqtt.const import (
    CONF_AUTO_DISCOVER,
    CONF_AUTO_DISCOVER_SCOPE,
    CONF_DEVICE_NAME,
    CONF_TOPIC_PATTERN,
    DEFAULT_AUTO_DISCOVER,
    DEFAULT_AUTO_DISCOVER_SCOPE,
)
from custom_components.telegraf_mqtt.exceptions import ReconfigureSubscribeFailed
from custom_components.telegraf_mqtt.snoop import SnoopListener

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


@dataclass
class _Msg:
    topic: str
    payload: bytes


def _payload(host: str, name: str = "cpu") -> bytes:
    import json

    return json.dumps({"name": name, "tags": {"host": host}, "fields": {"usage_idle": 1.0}, "timestamp": 1}).encode()


class _FakeMqtt:
    """Records every subscribe/unsubscribe so subscription COUNT is assertable."""

    def __init__(self, reject: set[str] | None = None) -> None:
        self.subscribe_calls: list[tuple[str, Callable[..., Any]]] = []
        self.unsubscribe_calls: list[str] = []
        # Topics the broker refuses to subscribe to (an ACL denial, say).
        self.reject = reject or set()

    async def async_wait_for_mqtt_client(self, _hass: Any) -> None:
        return None

    async def async_subscribe(self, _hass: Any, topic: str, cb: Callable[..., Any]) -> Callable[[], None]:
        if topic in self.reject:
            raise RuntimeError(f"ACL denies {topic}")
        self.subscribe_calls.append((topic, cb))

        def _unsub() -> None:
            self.unsubscribe_calls.append(topic)

        return _unsub

    @property
    def live_subscriptions(self) -> list[str]:
        """Topics with a subscribe and no matching unsubscribe."""
        outstanding = list(self.subscribe_calls)
        for topic in self.unsubscribe_calls:
            for index, (subscribed, _) in enumerate(outstanding):
                if subscribed == topic:
                    del outstanding[index]
                    break
        return [topic for topic, _ in outstanding]


@dataclass
class _CfgEntries:
    def __init__(self) -> None:
        self.forwarded: list[Any] = []

    async def async_forward_entry_setups(self, entry: Any, _platforms: list[str]) -> None:
        self.forwarded.append(entry)

    async def async_unload_platforms(self, _entry: Any, _platforms: list[str]) -> bool:
        return True

    def async_entries(self, _domain: str) -> list[Any]:
        # Only the setup-time Repairs checks consume this; this file has a
        # single entry, so there is never an overlap partner.
        return []

    def async_get_known_entry(self, _entry_id: str) -> Any:
        return _FlowEntry()


class _Hass:
    def __init__(self) -> None:
        self.config_entries = _CfgEntries()
        self.data: dict = {}


class _Entry:
    def __init__(self, *, data: dict | None = None, options: dict | None = None) -> None:
        self.entry_id = "entry-1"
        self.data = data if data is not None else {CONF_TOPIC_PATTERN: "telegraf/rack1/#"}
        self.options = options if options is not None else {}
        self.title = "Telegraf"
        self.runtime_data: Any = None
        self._unload: list[Callable[[], None]] = []

    def async_on_unload(self, cb: Callable[[], None]) -> None:
        self._unload.append(cb)

    def add_update_listener(self, _listener: Callable[..., Any]) -> Callable[[], None]:
        return lambda: None


def _patch(monkeypatch: pytest.MonkeyPatch, fake_mqtt: _FakeMqtt, ir: Any = None) -> None:
    class _Platform:
        SENSOR = "sensor"
        BINARY_SENSOR = "binary_sensor"

    monkeypatch.setattr(integration, "Platform", _Platform)
    monkeypatch.setattr(integration, "PLATFORMS", [_Platform.SENSOR, _Platform.BINARY_SENSOR])
    monkeypatch.setattr(integration, "mqtt", fake_mqtt, raising=False)
    monkeypatch.setattr(integration, "async_dispatcher_send", lambda *_a: None)
    monkeypatch.setattr(integration, "async_dispatcher_connect", lambda *_a: lambda: None)
    monkeypatch.setattr(integration, "async_track_time_interval", lambda *_a: lambda: None)
    monkeypatch.setattr(integration, "ir", ir)


def _setup(
    monkeypatch: pytest.MonkeyPatch,
    fake_mqtt: _FakeMqtt,
    entry: _Entry,
    hass: _Hass,
    *,
    ir: Any = None,
) -> None:
    _patch(monkeypatch, fake_mqtt, ir)
    asyncio.run(integration.async_setup_entry(hass, entry))  # type: ignore[arg-type]


def _snoop(fake_mqtt: _FakeMqtt, index: int = 1) -> SnoopListener:
    return fake_mqtt.subscribe_calls[index][1].__self__  # type: ignore[no-any-return]


# ---------------------------------------------------------------------------
# 1. Additive: skip what the main subscription already owns
# ---------------------------------------------------------------------------


def test_out_of_scope_message_is_dispatched_and_creates_a_device(monkeypatch: pytest.MonkeyPatch) -> None:
    """The headline behaviour: a host the entry's pattern cannot see becomes
    a device, without a second config entry."""
    fake_mqtt = _FakeMqtt()
    entry = _Entry(
        data={CONF_TOPIC_PATTERN: "telegraf/rack1/#"},
        options={CONF_AUTO_DISCOVER: True, CONF_AUTO_DISCOVER_SCOPE: "telegraf/#"},
    )
    hass = _Hass()
    _setup(monkeypatch, fake_mqtt, entry, hass)

    snoop_cb = fake_mqtt.subscribe_calls[1][1]
    snoop = _snoop(fake_mqtt)
    asyncio.run(snoop_cb(_Msg("telegraf/rack2/cpu", _payload("rack2-host"))))

    assert snoop.dispatched_count == 1
    manager = entry.runtime_data.manager
    assert len(manager.devices) == 1
    device_id, registry = next(iter(manager.devices.items()))
    assert registry.device_name == "rack2-host"
    assert manager.get_metric(f"{device_id}:cpu_usage_idle") is not None


def test_in_scope_message_is_recorded_but_not_dispatched(monkeypatch: pytest.MonkeyPatch) -> None:
    """The cost half. Without the skip, every in-pattern publish is parsed,
    routed and dispatched twice for a single message."""
    fake_mqtt = _FakeMqtt()
    entry = _Entry(
        data={CONF_TOPIC_PATTERN: "telegraf/rack1/#"},
        options={CONF_AUTO_DISCOVER: True, CONF_AUTO_DISCOVER_SCOPE: "telegraf/#"},
    )
    hass = _Hass()
    _setup(monkeypatch, fake_mqtt, entry, hass)

    snoop_cb = fake_mqtt.subscribe_calls[1][1]
    snoop = _snoop(fake_mqtt)
    asyncio.run(snoop_cb(_Msg("telegraf/rack1/cpu", _payload("rack1-host"))))

    assert snoop.dispatched_count == 0
    # Still recorded: the snoop's snapshot must remain a truthful account of
    # what the broker carried, skipped or not.
    assert "telegraf/rack1/cpu" in snoop._seen_topics
    assert "rack1-host" in snoop._seen_hosts
    # And nothing reached the registry -- the main subscription owns it.
    assert entry.runtime_data.manager.devices == {}
    # The skip is not an error path: the dispatcher was never called, so
    # the error counter must stay at zero.
    assert snoop.stop().dispatcher_errors == 0


def test_skip_honours_single_level_wildcards_in_the_pattern(monkeypatch: pytest.MonkeyPatch) -> None:
    """``exclude_filter`` is evaluated with real MQTT semantics, not a string
    prefix test. A ``+`` in the pattern must not over- or under-skip."""
    fake_mqtt = _FakeMqtt()
    entry = _Entry(
        data={CONF_TOPIC_PATTERN: "telegraf/+/cpu"},
        options={CONF_AUTO_DISCOVER: True, CONF_AUTO_DISCOVER_SCOPE: "telegraf/#"},
    )
    hass = _Hass()
    _setup(monkeypatch, fake_mqtt, entry, hass)
    snoop_cb = fake_mqtt.subscribe_calls[1][1]
    snoop = _snoop(fake_mqtt)

    # Matches ``telegraf/+/cpu`` -> skipped.
    asyncio.run(snoop_cb(_Msg("telegraf/rack1/cpu", _payload("rack1-host"))))
    assert snoop.dispatched_count == 0
    # Two levels deep -> outside the pattern -> dispatched.
    asyncio.run(snoop_cb(_Msg("telegraf/rack1/cpu/sub", _payload("deep-host"))))
    assert snoop.dispatched_count == 1


def test_no_exclude_filter_dispatches_everything(monkeypatch: pytest.MonkeyPatch) -> None:
    """Without an ``exclude_filter`` the listener is a plain forwarder. This
    is the config-flow scan path, which never sets one, and the reason the
    default must be ``None`` rather than ``""`` (an empty filter matches
    nothing and would silence the scan)."""
    dispatched: list[str] = []
    listener = SnoopListener(
        probe_topic="telegraf/#",
        timeout_seconds=0.0,
        dispatcher=lambda topic, _payload: dispatched.append(topic),
    )
    asyncio.run(listener._on_message(_Msg("telegraf/rack1/cpu", _payload("h"))))
    assert dispatched == ["telegraf/rack1/cpu"]


def test_empty_exclude_filter_is_treated_as_no_exclude_filter() -> None:
    """Defensive: a corrupt entry whose pattern normalised to ``""`` must not
    turn the skip into a filter that matches nothing. ``mqtt_filter_matches``
    returns False for an empty filter, so an empty ``exclude_filter``
    behaves as "skip nothing" rather than "skip everything"."""
    dispatched: list[str] = []
    listener = SnoopListener(
        probe_topic="telegraf/#",
        timeout_seconds=0.0,
        dispatcher=lambda topic, _payload: dispatched.append(topic),
        exclude_filter="",
    )
    asyncio.run(listener._on_message(_Msg("telegraf/rack1/cpu", _payload("h"))))
    assert dispatched == ["telegraf/rack1/cpu"]


# ---------------------------------------------------------------------------
# 2. Live toggle and scope restart
# ---------------------------------------------------------------------------


def test_no_opt_in_leaves_exactly_one_subscription(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_mqtt = _FakeMqtt()
    entry = _Entry(options={})
    hass = _Hass()
    _setup(monkeypatch, fake_mqtt, entry, hass)
    assert [t for t, _ in fake_mqtt.subscribe_calls] == ["telegraf/rack1/#"]
    assert entry.runtime_data.unsubscribe_snoop is None
    assert entry.runtime_data.snoop_scope is None


def test_live_toggle_on_subscribes_and_publishes_the_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_mqtt = _FakeMqtt()
    entry = _Entry(options={})
    hass = _Hass()
    _setup(monkeypatch, fake_mqtt, entry, hass)

    entry.options = {CONF_AUTO_DISCOVER: True, CONF_AUTO_DISCOVER_SCOPE: "telegraf/#"}
    asyncio.run(integration._async_options_updated(hass, entry))  # type: ignore[arg-type]

    assert [t for t, _ in fake_mqtt.subscribe_calls] == ["telegraf/rack1/#", "telegraf/#"]
    assert entry.runtime_data.snoop_scope == "telegraf/#"


def test_live_toggle_off_tears_the_snoop_down(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_mqtt = _FakeMqtt()
    entry = _Entry(options={CONF_AUTO_DISCOVER: True})
    hass = _Hass()
    _setup(monkeypatch, fake_mqtt, entry, hass)
    assert entry.runtime_data.unsubscribe_snoop is not None

    entry.options = {CONF_AUTO_DISCOVER: False}
    asyncio.run(integration._async_options_updated(hass, entry))  # type: ignore[arg-type]

    assert entry.runtime_data.unsubscribe_snoop is None
    assert entry.runtime_data.snoop_scope is None
    assert fake_mqtt.live_subscriptions == ["telegraf/rack1/#"]


def test_changing_the_scope_restarts_rather_than_double_subscribes(monkeypatch: pytest.MonkeyPatch) -> None:
    """A scope change cannot be applied by mutating the running listener --
    it reads both filters from a per-message broker callback -- so the
    listener is stopped and replaced. The broker must end up with the main
    subscription plus exactly ONE snoop, not two."""
    fake_mqtt = _FakeMqtt()
    entry = _Entry(
        options={CONF_AUTO_DISCOVER: True, CONF_AUTO_DISCOVER_SCOPE: "telegraf/#"},
    )
    hass = _Hass()
    _setup(monkeypatch, fake_mqtt, entry, hass)
    assert fake_mqtt.live_subscriptions == ["telegraf/rack1/#", "telegraf/#"]

    entry.options = {CONF_AUTO_DISCOVER: True, CONF_AUTO_DISCOVER_SCOPE: "telegraf/rack1/#"}
    asyncio.run(integration._async_options_updated(hass, entry))  # type: ignore[arg-type]

    assert [t for t, _ in fake_mqtt.subscribe_calls] == [
        "telegraf/rack1/#",
        "telegraf/#",
        "telegraf/rack1/#",
    ]
    assert fake_mqtt.live_subscriptions == ["telegraf/rack1/#", "telegraf/rack1/#"]
    assert entry.runtime_data.snoop_scope == "telegraf/rack1/#"


def test_unchanged_scope_does_not_resubscribe(monkeypatch: pytest.MonkeyPatch) -> None:
    """The common case: a user saves the options dialog after changing
    something unrelated. A restart here would churn the broker on every
    save for no reason."""
    fake_mqtt = _FakeMqtt()
    entry = _Entry(options={CONF_AUTO_DISCOVER: True})
    hass = _Hass()
    _setup(monkeypatch, fake_mqtt, entry, hass)
    before = len(fake_mqtt.subscribe_calls)

    for _ in range(3):
        entry.options = dict(entry.options, expire_after=300)
        asyncio.run(integration._async_options_updated(hass, entry))  # type: ignore[arg-type]

    assert len(fake_mqtt.subscribe_calls) == before
    assert fake_mqtt.unsubscribe_calls == []


def test_reconfigure_repoints_the_exclude_filter(monkeypatch: pytest.MonkeyPatch) -> None:
    """The reconfigure reload is what re-points auto-discover.

    ``async_unload_entry`` stops the snoop and ``async_setup_entry`` starts a
    fresh one, so the exclude filter tracks the new pattern automatically.
    This is the path a user takes when they narrow or widen their entry --
    without it, the snoop would keep skipping on the OLD pattern and start
    re-processing traffic the new main subscription already owns.
    """
    fake_mqtt = _FakeMqtt()
    entry = _Entry(
        data={CONF_TOPIC_PATTERN: "telegraf/rack1/#"},
        options={CONF_AUTO_DISCOVER: True, CONF_AUTO_DISCOVER_SCOPE: "telegraf/#"},
    )
    hass = _Hass()
    _setup(monkeypatch, fake_mqtt, entry, hass)

    # Narrow the entry: rack1 -> a single host subtree.
    entry.data = {CONF_TOPIC_PATTERN: "telegraf/rack1/server01/#"}
    assert asyncio.run(integration.async_unload_entry(hass, entry)) is True  # type: ignore[arg-type]
    _setup(monkeypatch, fake_mqtt, entry, hass)

    # Subscriptions so far: [0] main rack1, [1] snoop, [2] main server01,
    # [3] the replacement snoop.
    new_snoop = _snoop(fake_mqtt, index=3)
    assert new_snoop._exclude_filter == "telegraf/rack1/server01/#"
    assert entry.runtime_data.snoop_exclude_filter == "telegraf/rack1/server01/#"
    # ...and the snoop now DISPATCHES rack1 traffic it used to skip,
    # because the main subscription no longer covers all of it.
    snoop_cb = fake_mqtt.subscribe_calls[3][1]
    asyncio.run(snoop_cb(_Msg("telegraf/rack1/server02/cpu", _payload("server02"))))
    assert new_snoop.dispatched_count == 1


# ---------------------------------------------------------------------------
# 3. Reconfigure pre-flight (N3)
# ---------------------------------------------------------------------------


@dataclass
class _FlowEntry:
    entry_id: str = "self"
    data: dict = field(default_factory=lambda: {CONF_TOPIC_PATTERN: "old/topic", CONF_DEVICE_NAME: "Self"})
    options: dict = field(default_factory=dict)
    title: str = "Self"
    runtime_data: Any = None


class _FlowCfgEntries:
    def __init__(self) -> None:
        self.entry = _FlowEntry()

    def async_get_known_entry(self, _entry_id: str) -> _FlowEntry:
        return self.entry

    def async_entries(self, _domain: str) -> list[Any]:
        return [self.entry]


def _flow(monkeypatch: pytest.MonkeyPatch, fake_mqtt: _FakeMqtt) -> tuple[TelegrafMqttConfigFlow, _FlowCfgEntries]:
    import homeassistant.components.mqtt as mqtt_module

    monkeypatch.setattr(mqtt_module, "async_subscribe", fake_mqtt.async_subscribe)
    hass = _Hass()
    hass.config_entries = _FlowCfgEntries()  # type: ignore[assignment]
    flow = TelegrafMqttConfigFlow()
    flow.hass = hass  # type: ignore[attr-defined]
    flow.context = {"entry_id": "self"}  # type: ignore[attr-defined]
    # Skip HA's unique-id plumbing: this file is about the subscribe check.
    monkeypatch.setattr(flow, "async_set_unique_id", lambda *_a, **_k: asyncio.sleep(0))
    monkeypatch.setattr(flow, "_abort_if_unique_id_configured", lambda: None)
    return flow, hass.config_entries  # type: ignore[return-value]


def test_reconfigure_rejects_a_pattern_the_broker_refuses(monkeypatch: pytest.MonkeyPatch) -> None:
    """N3: the failure the old flow could not express. The pattern was
    committed to ``entry.data`` and only then failed the reload, so the user
    saw a success dialog and a silently broken entry."""
    fake_mqtt = _FakeMqtt(reject={"telegraf/denied/#"})
    flow, cfg_entries = _flow(monkeypatch, fake_mqtt)

    result = asyncio.run(
        flow.async_step_reconfigure({CONF_TOPIC_PATTERN: "telegraf/denied/#", CONF_DEVICE_NAME: "New"})
    )

    assert result["type"] == "form"
    assert result["errors"] == {"base": "cannot_connect"}
    # The entry is untouched: still on its old pattern.
    assert cfg_entries.entry.data[CONF_TOPIC_PATTERN] == "old/topic"
    # The failed pre-flight left no subscription behind.
    assert fake_mqtt.subscribe_calls == []
    assert fake_mqtt.live_subscriptions == []


def test_reconfigure_accepts_a_good_pattern_and_releases_the_preflight(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The success path must NOT leak: after the pre-flight the broker holds
    exactly the subscriptions the running entry has, not an extra one."""
    fake_mqtt = _FakeMqtt()
    flow, _cfg = _flow(monkeypatch, fake_mqtt)
    reloaded: list[dict] = []
    monkeypatch.setattr(flow, "async_update_reload_and_abort", lambda _e, data_updates: reloaded.append(data_updates))

    asyncio.run(flow.async_step_reconfigure({CONF_TOPIC_PATTERN: "telegraf/rack2/#", CONF_DEVICE_NAME: "New"}))

    assert [t for t, _ in fake_mqtt.subscribe_calls] == ["telegraf/rack2/#"]
    assert fake_mqtt.unsubscribe_calls == ["telegraf/rack2/#"]
    assert fake_mqtt.live_subscriptions == []
    assert reloaded == [
        {
            CONF_TOPIC_PATTERN: "telegraf/rack2/#",
            CONF_DEVICE_NAME: "New",
            "manufacturer": None,
            "model": None,
            "sw_version": None,
        }
    ]


def test_reconfigure_preflight_exception_carries_the_translation(monkeypatch: pytest.MonkeyPatch) -> None:
    """``ReconfigureSubscribeFailed`` had a translation string and no
    production caller. The pre-flight is that caller: the developer-facing
    log line and the user's toast now come from one message."""
    fake_mqtt = _FakeMqtt(reject={"telegraf/new/#"})
    flow, _cfg = _flow(monkeypatch, fake_mqtt)
    monkeypatch.setattr(flow, "async_update_reload_and_abort", lambda *_a, **_k: {})

    with pytest.raises(ReconfigureSubscribeFailed) as excinfo:
        asyncio.run(flow._can_subscribe("telegraf/new/#"))

    assert excinfo.value.translation_key == "reconfigure_subscribe_failed"
    assert excinfo.value.translation_placeholders == {"topic": "telegraf/new/#", "error": "ACL denies telegraf/new/#"}


def test_reconfigure_preflight_teardown_failure_does_not_block_the_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    """A broker that rejects the UNSUBSCRIBE is a broker problem, not a
    reason to tell the user their reconfigure failed."""
    fake_mqtt = _FakeMqtt()

    async def _subscribe_then_fail_unsub(_hass: Any, topic: str, _cb: Callable[..., Any]) -> Callable[[], None]:
        def _boom() -> None:
            raise RuntimeError("broker said no")

        return _boom

    flow, _cfg = _flow(monkeypatch, fake_mqtt)
    monkeypatch.setattr(flow, "async_update_reload_and_abort", lambda *_a, **_k: {})
    import homeassistant.components.mqtt as mqtt_module

    monkeypatch.setattr(mqtt_module, "async_subscribe", _subscribe_then_fail_unsub)

    # Must not raise.
    asyncio.run(flow._can_subscribe("telegraf/rack3/#"))


# ---------------------------------------------------------------------------
# 4. Redundant-scope Repairs check
# ---------------------------------------------------------------------------


@dataclass
class _Ir:
    IssueSeverity: Any = field(default_factory=lambda: types.SimpleNamespace(WARNING="warning"))
    created: list[Any] = field(default_factory=list)
    deleted: list[Any] = field(default_factory=list)

    def async_create_issue(self, hass, domain, issue_id, **kwargs):
        self.created.append((issue_id, kwargs))
        return "id"

    def async_delete_issue(self, hass, domain, issue_id):
        self.deleted.append(issue_id)


def _check_scope(
    monkeypatch: pytest.MonkeyPatch,
    *,
    pattern: str,
    options: dict,
) -> _Ir:
    from custom_components.telegraf_mqtt.repairs import check_auto_discover_scope

    fake_ir = _Ir()
    monkeypatch.setattr("custom_components.telegraf_mqtt.ir", fake_ir)
    flow_entry = _FlowEntry(data={CONF_TOPIC_PATTERN: pattern}, options=options)
    check_auto_discover_scope(_Hass(), flow_entry)  # type: ignore[arg-type]
    return fake_ir


def test_redundant_scope_raises_a_warning(monkeypatch: pytest.MonkeyPatch) -> None:
    """The scope is a strict subset of the pattern, so every message the
    extra subscription receives is skipped and no host can ever be found."""
    fake_ir = _check_scope(
        monkeypatch,
        pattern="telegraf/#",
        options={CONF_AUTO_DISCOVER: True, CONF_AUTO_DISCOVER_SCOPE: "telegraf/rack1/#"},
    )
    assert len(fake_ir.created) == 1
    issue_id, kwargs = fake_ir.created[0]
    assert issue_id == "auto_discover_scope_redundant_self"
    assert kwargs["translation_key"] == "auto_discover_scope_redundant"
    assert kwargs["severity"] == "warning"
    assert kwargs["translation_placeholders"] == {
        "configured_topic": "telegraf/#",
        "auto_discover_scope": "telegraf/rack1/#",
    }


def test_equal_scope_and_pattern_also_counts_as_redundant(monkeypatch: pytest.MonkeyPatch) -> None:
    fake_ir = _check_scope(
        monkeypatch,
        pattern="telegraf/rack1/#",
        options={CONF_AUTO_DISCOVER: True, CONF_AUTO_DISCOVER_SCOPE: "telegraf/rack1/#"},
    )
    assert len(fake_ir.created) == 1


def test_widened_scope_does_not_warn(monkeypatch: pytest.MonkeyPatch) -> None:
    """The soundness property: a scope that genuinely adds a host is never
    flagged. ``telegraf/#`` covers ``telegraf/rack1/#``, not the reverse."""
    fake_ir = _check_scope(
        monkeypatch,
        pattern="telegraf/rack1/#",
        options={CONF_AUTO_DISCOVER: True, CONF_AUTO_DISCOVER_SCOPE: "telegraf/#"},
    )
    assert fake_ir.created == []
    assert "auto_discover_scope_redundant_self" in fake_ir.deleted


def test_scope_check_is_inert_when_auto_discover_is_off(monkeypatch: pytest.MonkeyPatch) -> None:
    """With the opt-in off there is no second subscription, so there is
    nothing redundant about any scope."""
    fake_ir = _check_scope(
        monkeypatch,
        pattern="telegraf/#",
        options={CONF_AUTO_DISCOVER: False, CONF_AUTO_DISCOVER_SCOPE: "telegraf/rack1/#"},
    )
    assert fake_ir.created == []
    assert "auto_discover_scope_redundant_self" in fake_ir.deleted


def test_scope_check_is_inert_without_an_issue_registry(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("custom_components.telegraf_mqtt.ir", None)
    from custom_components.telegraf_mqtt.repairs import check_auto_discover_scope

    check_auto_discover_scope(  # must not raise
        _Hass(),
        _FlowEntry(
            options={CONF_AUTO_DISCOVER: True, CONF_AUTO_DISCOVER_SCOPE: "telegraf/rack1/#"},
        ),  # type: ignore[arg-type]
    )


def test_redundant_scope_is_raised_through_the_setup_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """The check is wired into ``_apply_auto_discover``, so enabling a
    redundant scope surfaces the warning immediately rather than waiting for
    an unrelated restart."""
    fake_ir = _Ir()
    fake_mqtt = _FakeMqtt()
    entry = _Entry(
        data={CONF_TOPIC_PATTERN: "telegraf/#"},
        options={CONF_AUTO_DISCOVER: True, CONF_AUTO_DISCOVER_SCOPE: "telegraf/rack1/#"},
    )
    hass = _Hass()
    _setup(monkeypatch, fake_mqtt, entry, hass, ir=fake_ir)

    assert any(issue_id.startswith("auto_discover_scope_redundant") for issue_id, _ in fake_ir.created)


# ---------------------------------------------------------------------------
# 5. Option normalisation and defaults
# ---------------------------------------------------------------------------


def test_default_scope_is_used_when_the_option_is_absent() -> None:
    from custom_components.telegraf_mqtt import _normalize_options

    options, invalid = _normalize_options({})
    assert options.auto_discover is DEFAULT_AUTO_DISCOVER
    assert options.auto_discover_scope == DEFAULT_AUTO_DISCOVER_SCOPE
    assert invalid == []


def test_corrupt_scope_falls_back_to_the_default_and_is_flagged() -> None:
    """A hand-edited ``.storage`` must not reach the broker as a broken
    second subscription, and must be visible in the Repairs panel."""
    from custom_components.telegraf_mqtt import _normalize_options

    for bad in ("telegraf/#/deep", "sp+rt/#", "", 42, None):
        options, invalid = _normalize_options({CONF_AUTO_DISCOVER_SCOPE: bad})
        assert options.auto_discover_scope == DEFAULT_AUTO_DISCOVER_SCOPE, bad
        assert CONF_AUTO_DISCOVER_SCOPE in invalid, bad


def test_valid_scopes_pass_through_verbatim() -> None:
    from custom_components.telegraf_mqtt import _normalize_options

    for good in ("telegraf/#", "telegraf/+/cpu", "telegraf/rack1/#", "a/b/c"):
        options, invalid = _normalize_options({CONF_AUTO_DISCOVER_SCOPE: good})
        assert options.auto_discover_scope == good
        assert CONF_AUTO_DISCOVER_SCOPE not in invalid


def test_setup_without_a_usable_pattern_does_not_start_a_snoop(monkeypatch: pytest.MonkeyPatch) -> None:
    """The skip filter is the entry's pattern; with no pattern there is
    nothing to skip against, and a snoop would duplicate every message."""
    fake_mqtt = _FakeMqtt()
    entry = _Entry(
        data={CONF_TOPIC_PATTERN: ""},
        options={CONF_AUTO_DISCOVER: True},
    )
    hass = _Hass()
    _patch(monkeypatch, fake_mqtt)
    # Setup raises before the subscription on an empty pattern, so drive the
    # helper directly against the same shape.
    entry.data = {CONF_TOPIC_PATTERN: ""}
    entry.runtime_data = integration.TelegrafMqttRuntimeData(
        manager=integration.DeviceManager(),
        parser=integration.TelegrafParser(),
        parser_stats=None,
        manufacturer=None,
        model=None,
    )
    options = integration._options_from_entry(entry)
    asyncio.run(integration._apply_auto_discover(hass, entry, options))  # type: ignore[arg-type]

    assert fake_mqtt.subscribe_calls == []
    assert entry.runtime_data.unsubscribe_snoop is None


# ---------------------------------------------------------------------------
# 6. Options flow surface
# ---------------------------------------------------------------------------


def test_options_schema_prefills_the_scope_from_stored_options() -> None:
    from custom_components.telegraf_mqtt.config_flow import _build_options_schema

    schema = _build_options_schema({CONF_AUTO_DISCOVER_SCOPE: "telegraf/rack9/#"})
    defaults = {str(marker.schema): marker.default() for marker in schema.schema}
    assert defaults[CONF_AUTO_DISCOVER_SCOPE] == "telegraf/rack9/#"


def test_clean_options_preserves_an_untouched_scope() -> None:
    from custom_components.telegraf_mqtt.config_flow import _clean_options

    cleaned = _clean_options(
        {CONF_AUTO_DISCOVER: True},
        {CONF_AUTO_DISCOVER_SCOPE: "telegraf/rack9/#"},
    )
    assert cleaned[CONF_AUTO_DISCOVER_SCOPE] == "telegraf/rack9/#"


def test_options_flow_rejects_an_invalid_scope_without_persisting(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The scope is subscribed to directly, so a bad value is a broken
    subscription -- not a cosmetic typo to be normalised away."""
    from custom_components.telegraf_mqtt.config_flow import TelegrafMqttOptionsFlow

    flow = TelegrafMqttOptionsFlow()
    created: list[Any] = []
    options_entry = _FlowEntry(options={CONF_AUTO_DISCOVER_SCOPE: "telegraf/rack1/#"})

    hass = _Hass()
    hass.config_entries = _FlowCfgEntries()  # type: ignore[assignment]
    hass.config_entries.entry = options_entry  # type: ignore[attr-defined]
    flow.hass = hass  # type: ignore[attr-defined]
    # ``_config_entry_id`` is a read-only property on the base class that
    # resolves through ``self.handler.flow_id``; HA sets ``handler`` when it
    # builds the flow, so the double supplies it here.
    flow.handler = SimpleNamespace(flow_id="self")  # type: ignore[attr-defined]
    flow.async_show_form = lambda **kwargs: kwargs  # type: ignore[method-assign]
    flow.async_create_entry = lambda **kwargs: created.append(kwargs)  # type: ignore[method-assign]

    result = asyncio.run(flow.async_step_init({CONF_AUTO_DISCOVER_SCOPE: "telegraf/#/bad", CONF_AUTO_DISCOVER: True}))
    assert result["errors"] == {CONF_AUTO_DISCOVER_SCOPE: "invalid_topic"}
    assert created == []

    # ...and a valid one goes through.
    result = asyncio.run(flow.async_step_init({CONF_AUTO_DISCOVER_SCOPE: "telegraf/rack2/#"}))
    assert created
    assert created[0]["data"][CONF_AUTO_DISCOVER_SCOPE] == "telegraf/rack2/#"
