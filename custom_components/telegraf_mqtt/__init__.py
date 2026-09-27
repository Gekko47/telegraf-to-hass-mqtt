"""The telegraf_mqtt integration."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

try:
    from homeassistant.components import mqtt
    from homeassistant.config_entries import ConfigEntry
    from homeassistant.const import Platform
    from homeassistant.core import HomeAssistant, callback
    from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
    from homeassistant.helpers import entity_registry as er
    from homeassistant.helpers import issue_registry as ir
    from homeassistant.helpers.dispatcher import (
        async_dispatcher_connect,
        async_dispatcher_send,
    )
    from homeassistant.helpers.event import (
        async_track_time_interval,
    )
except ModuleNotFoundError:  # pragma: no cover - exercised only in unit-test import isolation
    # Import-isolation fallback: the same names exist with permissive
    # types so the module still imports (and is unit-testable) without a
    # running Home Assistant. Each assignment silences mypy for exactly
    # the type it replaces; the ``is not None`` runtime guards make the
    # narrowed shapes safe.
    ConfigEntry = object  # type: ignore[misc,assignment]
    Platform = None  # type: ignore[misc,assignment]
    HomeAssistant = object  # type: ignore[misc,assignment]
    callback = lambda target: target  # type: ignore[assignment]  # noqa: E731 - identity when HA is absent
    mqtt = None  # type: ignore[assignment]
    async_dispatcher_connect = None  # type: ignore[assignment]
    async_dispatcher_send = None  # type: ignore[assignment]
    async_track_time_interval = None  # type: ignore[assignment]
    ConfigEntryNotReady = Exception  # type: ignore[misc,assignment]
    er = None  # type: ignore[assignment]
    ir = None  # type: ignore[assignment]

from .const import (
    BROKER_WAIT_TIMEOUT_SECONDS,
    CONF_AUTO_DISCOVER,
    CONF_AUTO_DISCOVER_SCOPE,
    CONF_CATEGORY_OVERRIDES,
    CONF_CLEANUP_DELAY,
    CONF_DELETE_DELAY,
    CONF_DEVICE_ID_STRATEGY,
    CONF_ENABLE_CLEANUP,
    CONF_EXCLUDE_PATTERNS,
    CONF_EXPIRE_AFTER,
    CONF_FIELD_OVERRIDES,
    CONF_MIN_ACTIVE_METRICS,
    CONF_TOPIC_PATTERN,
    DEFAULT_AUTO_DISCOVER,
    DEFAULT_AUTO_DISCOVER_SCOPE,
    DEFAULT_CLEANUP_DELAY,
    DEFAULT_DELETE_DELAY,
    DEFAULT_DEVICE_ID_STRATEGY,
    DEFAULT_ENABLE_CLEANUP,
    DEFAULT_EXPIRE_AFTER,
    DEFAULT_MIN_ACTIVE_METRICS,
    DOMAIN,
    MAX_EXPIRY_TICK_SECONDS,
    MIN_EXPIRY_TICK_SECONDS,
    SIGNAL_METRIC_UPDATED,
    SIGNAL_NEW_DEVICE,
    SIGNAL_NEW_METRIC,
    SIGNAL_REMOVE_METRIC,
    VALID_DEVICE_ID_STRATEGIES,
)
from .parser import ParserStats, TelegrafParser
from .registry import DeviceManager
from .repairs import (
    check_auto_discover_scope,
    check_device_cap,
    check_device_id_collision,
    check_device_id_conflict,
    check_invalid_persisted_option,
    check_metric_cap,
    check_no_traffic,
    check_overlapping_topics,
)
from .snoop import SnoopListener

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR] if Platform is not None else []


@dataclass
class TelegrafMqttRuntimeData:
    """Runtime state for a config entry."""

    manager: DeviceManager | None
    parser: TelegrafParser
    parser_stats: Any  # ``custom_components.telegraf_mqtt.parser.ParserStats``
    manufacturer: str | None
    model: str | None
    sw_version: str | None = None
    unsubscribe: Callable[[], None] | None = None
    unsubscribe_snoop: Callable[[], None] | None = None
    cancel_expiry: Callable[[], None] | None = None
    # The auto-discover scope the running snoop is subscribed to, and the
    # main-subscription filter it is skipping. Both are captured so
    # ``_apply_auto_discover`` can tell "already correct, leave it alone"
    # from "the user changed the scope, restart the listener" without a
    # second subscription being opened on the way. ``None`` means no
    # snoop is running.
    snoop_scope: str | None = None
    snoop_exclude_filter: str | None = None
    # The strategy this entry was *set up* with, captured before any live
    # options apply can overwrite ``DeviceManager._device_id_strategy``.
    # The options-update listener compares against this rather than against
    # the manager, because by the time it runs the manager already holds the
    # NEW value and the comparison would always be equal -- which is exactly
    # why the previous two-listener arrangement never reached its reload.
    applied_device_id_strategy: str = DEFAULT_DEVICE_ID_STRATEGY


def _broker_unreachable_not_ready(topic: str, error: str) -> ConfigEntryNotReady:
    """Build a ``ConfigEntryNotReady`` for an unreachable MQTT broker.

    Shared by the wait-precheck and real subscription error paths in
    ``async_setup_entry`` so the toast text, translation domain/key, and
    topic/error placeholders stay in sync. The caller is responsible for
    raising the result with ``raise ... from <err>`` to preserve exception
    chaining.

    Setup MUST surface a retryable failure: an unreachable broker is a
    transient condition, and HA's retry loop is the correct response. An
    ordinary ``HomeAssistantError`` would be treated as fatal and leave
    the user with a dead entry and a restart as the only way out.

    ``exceptions.MqttBrokerUnreachable`` was the typed exception for this
    condition and has now been DELETED. It could not be raised here
    without breaking the retry semantics above, no other surface needed a
    fatal broker error, and the ``mqtt_broker_unreachable`` translation
    key is fully reachable through this ``ConfigEntryNotReady`` -- so it
    was a class with no production callsite carrying a translation key
    that did not need it. See AC14 in the hardening plan.
    """
    ready_exc = ConfigEntryNotReady(f"Could not subscribe to {topic}: {error}")
    ready_exc.translation_domain = DOMAIN
    ready_exc.translation_key = "mqtt_broker_unreachable"
    ready_exc.translation_placeholders = {"topic": topic, "error": error}
    return ready_exc


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up telegraf_mqtt from a config entry."""
    # Tolerate invalid persisted options: a corrupted value falls back
    # to the default AND raises a Repair issue so the user can correct
    # it from the UI without the entry failing to set up.
    # ``_options_from_entry_with_repair`` already surfaces invalid persisted
    # options as Repairs issues; the second tuple element is not needed here.
    options, _invalid_options = _options_from_entry_with_repair(hass, entry)
    parser_stats = ParserStats()
    parser = TelegrafParser(stats=parser_stats)
    manager = DeviceManager(
        expire_after=options.expire_after,
        exclude_patterns=options.exclude_patterns,
        field_overrides=options.field_overrides,
        cleanup_delay=options.cleanup_delay,
        delete_delay=options.delete_delay,
        enable_cleanup=options.enable_cleanup,
        min_active_metrics=options.min_active_metrics,
        parser=parser,
        category_overrides=options.category_overrides,
        device_id_strategy=options.device_id_strategy,
    )
    entry.runtime_data = TelegrafMqttRuntimeData(
        manager=manager,
        parser=parser,
        parser_stats=parser_stats,
        manufacturer=entry.data.get("manufacturer"),
        model=entry.data.get("model"),
        sw_version=entry.data.get("sw_version"),
        applied_device_id_strategy=options.device_id_strategy,
    )

    # Connect platform dispatcher listeners BEFORE any MQTT subscription
    # is established. ``manager.set_callbacks`` wires the synchronous
    # ``on_discovered`` -> ``_dispatch_new_metric`` -> ``async_dispatcher_send``
    # path: when ``manager.process_message`` discovers a new metric (which
    # happens for any retained or fresh MQTT payload), it fires
    # ``SIGNAL_NEW_METRIC`` synchronously. If the sensor / binary_sensor
    # platforms have not yet connected their listeners -- the previous
    # ordering did the broker subscription first, the platform forward
    # second -- that signal is dispatched into a void of zero listeners
    # and the new entity never appears. The platform's startup-time
    # ``for metric_key in manager: add_metric(metric_key)`` loop cannot
    # recover from this: by the time the platforms listen, the metric
    # is already in the registry but ``SIGNAL_NEW_METRIC`` was emitted
    # with no listener connected, so the platform's first chance to
    # notice it is the next retained message -- which may never come.
    #
    # Forwarding platforms first guarantees that:
    # 1. ``SIGNAL_NEW_METRIC`` listeners are connected before the broker
    #    subscription is established.
    # 2. Any retained message the broker flushes during
    #    ``mqtt.async_subscribe`` flows through a registry whose
    #    dispatcher has live listeners, so the new entity is created
    #    during the same ``await`` instead of being silently dropped.
    # 3. The snoop listener (started after the main subscription) re-
    #    dispatches into the same already-connected pipeline, so
    #    ``auto_discover`` events also reach the platform.
    if Platform is not None:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    if mqtt is not None:
        # M8: a corrupt entry (hand-edited .storage, a partial write) used
        # to raise a bare KeyError here. That is not a retryable error, so
        # HA retried forever with a raw traceback and no user-facing
        # message. Fail with a translated error instead.
        topic_pattern = entry.data.get(CONF_TOPIC_PATTERN)
        if not isinstance(topic_pattern, str) or not topic_pattern:
            _LOGGER.error(
                "Config entry %s has no usable %s in its data; it cannot be set up",
                entry.entry_id,
                CONF_TOPIC_PATTERN,
            )
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="missing_topic_pattern",
                translation_placeholders={"entry_title": entry.title},
            )

        manager.set_callbacks(
            on_write=lambda metric_key, available, value: _dispatch_metric_updated(hass, entry, metric_key),
            on_discovered=lambda metric_key: _dispatch_new_metric(hass, entry, metric_key),
            on_new_device=_make_new_device_callback(hass, entry),
            on_remove=lambda device_id, unique_key: _dispatch_remove_metric(hass, entry, device_id, unique_key),
        )

        async def message_received(message: Any) -> None:
            manager.process_message(message.topic, message.payload)

        # WS-E1: bound the precheck. ``async_wait_for_mqtt_client`` resolves
        # only on a client-connected event, so with an unreachable broker an
        # unbounded await leaves the entry stuck in "setting up" forever --
        # no error, no retry, no message. The timeout turns that silence into
        # a translated "broker unreachable" and a normal HA retry cycle.
        #
        # The ``hasattr`` guard is gone on purpose: at the declared
        # 2026.6.0 floor this API is guaranteed, and the guard only ever
        # existed to accommodate older test doubles -- which meant the
        # fakes, not the integration, defined the contract.
        #
        # The catch is deliberately NARROW. ``async_wait_for_mqtt_client``
        # fails in exactly two ways that mean "the broker is not usable
        # yet": the bounded wait elapses, or the MQTT client raises
        # ``OSError`` while connecting. Anything else -- a ``TypeError``
        # from a bad call, an ``AttributeError`` from an API that changed
        # shape -- is a BUG, and must not be laundered into
        # ``ConfigEntryNotReady``. An over-broad catch here is worse than
        # no catch at all: HA retries forever, the traceback is never
        # logged, and the entry silently never loads with no signal that
        # anything other than connectivity is wrong.
        try:
            async with asyncio.timeout(BROKER_WAIT_TIMEOUT_SECONDS):
                await mqtt.async_wait_for_mqtt_client(hass)
        except (TimeoutError, OSError) as wait_err:
            raise _broker_unreachable_not_ready(topic_pattern, str(wait_err)) from wait_err

        try:
            entry.runtime_data.unsubscribe = await mqtt.async_subscribe(hass, topic_pattern, message_received)
        except Exception as real_err:
            raise _broker_unreachable_not_ready(topic_pattern, str(real_err)) from real_err
        _LOGGER.info("Subscribed to Telegraf MQTT topic pattern %s", topic_pattern)

        # Post-setup snoop listener. Runs only when the user has the
        # auto-discover option enabled (default off -- the user must opt
        # in via the options flow). The listener installs a second
        # subscription on ``auto_discover_scope`` and hands every message
        # the main subscription did NOT already cover back to
        # ``manager.process_message``, so Telegraf hosts publishing
        # outside ``topic_pattern`` become real devices and entities
        # without the user adding another config entry. The unsubscribe
        # handle is stored on the runtime data and torn down in
        # ``async_unload_entry``.
        #
        # Both filters are the user's own: the scope is the literal option
        # value (nothing is widened implicitly), and the skip filter is
        # ``topic_pattern``, which makes the feature purely additive. The
        # start / stop / restart wiring lives in ``_apply_auto_discover``
        # so the live options-update listener can drive it in place too.
        await _apply_auto_discover(hass, entry, options)

    if async_track_time_interval is not None:
        _schedule_expiry_check(hass, entry)

    if async_dispatcher_connect is not None:
        entry.async_on_unload(_listener_remove_metric(hass, entry))

    # ONE listener, not two. ``_async_options_updated`` owns both the
    # live-apply path and the strategy-reload decision; it must make the
    # reload decision BEFORE calling ``apply_options`` (which overwrites
    # the manager's strategy slot). See that function for the details.
    #
    # No ``hasattr`` guard. ``ConfigEntry`` has provided both methods for
    # many major versions, and a guard here is worse than useless: if it
    # ever evaluated false the options listener would simply not be
    # registered, and every option change would be silently accepted and
    # then ignored -- the exact WS-A failure, recurring in a new form,
    # with no error anywhere. Silent feature death is strictly worse than
    # an AttributeError on an object that is not a ConfigEntry.
    entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    # Phase 7: Repairs for recoverable config problems. The overlap
    # check is idempotent -- if this entry's pattern is fine and no
    # other entry overlaps, it deletes any prior overlap_issue and
    # creates nothing. Same for invalid-persisted-option checks via
    # _options_from_entry_with_repair above.
    check_overlapping_topics(hass, entry)
    # Phase 10: Repairs for runtime-detected problems. ``check_no_traffic``
    # is invoked from inside the periodic expiry callback (see
    # ``_schedule_expiry_check``) so the snoop listener has time to
    # receive messages before we flag the topic pattern as silent.
    # ``check_device_id_collision`` fires when two distinct host tags
    # collapse onto the same device_id slug. ``check_device_id_conflict``
    # fires when two config entries produced the same device_id from
    # different topic patterns.
    check_device_id_collision(hass, entry)
    check_device_id_conflict(hass, entry)
    # Fleet-scale guard: raise a Repairs hint when the device or
    # metric cap has caused measurements to be dropped. Both checks
    # are idempotent create-or-delete calls and self-guard when the
    # issue registry is unavailable.
    check_device_cap(hass, entry)
    check_metric_cap(hass, entry)

    return True


async def async_migrate_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Migrate a config entry forward to the current schema version.

    **v1 -> v2.** Backfill every option key the entry does not already have
    with its documented default, without touching any key that IS present.
    A sparse ``entry.options`` (``{}`` is the common case for an entry whose
    options dialog was never opened) becomes explicit, so what the
    integration is actually running is readable from ``.storage``.

    This is deliberately *additive*. ``_normalize_options`` already falls
    back to the same defaults for missing keys, so a v1 entry is
    semantically identical before and after -- persisting them just makes
    the state visible. A full revert of this migration is therefore safe:
    leftover default keys are ignored.

    ``CONF_TOPIC_PATTERN`` is never synthesised. An entry missing it is
    genuinely broken, and guessing a subscription would point the
    integration at the user's whole broker; the setup guard raises a
    translated error instead.
    """
    if entry.version > 2:
        # Forward-compat: a downgrade. Refuse rather than corrupt.
        _LOGGER.error(
            "Config entry %s is at schema version %s, newer than this release supports (2)",
            entry.entry_id,
            entry.version,
        )
        return False

    if entry.version < 2:
        stored = dict(entry.options or {})
        defaults: dict[str, Any] = {
            CONF_AUTO_DISCOVER: DEFAULT_AUTO_DISCOVER,
            CONF_EXPIRE_AFTER: DEFAULT_EXPIRE_AFTER,
            CONF_ENABLE_CLEANUP: DEFAULT_ENABLE_CLEANUP,
            CONF_CLEANUP_DELAY: DEFAULT_CLEANUP_DELAY,
            CONF_DELETE_DELAY: DEFAULT_DELETE_DELAY,
            CONF_MIN_ACTIVE_METRICS: DEFAULT_MIN_ACTIVE_METRICS,
            CONF_CATEGORY_OVERRIDES: {},
            CONF_EXCLUDE_PATTERNS: [],
            CONF_FIELD_OVERRIDES: {},
            CONF_DEVICE_ID_STRATEGY: DEFAULT_DEVICE_ID_STRATEGY,
            CONF_AUTO_DISCOVER_SCOPE: DEFAULT_AUTO_DISCOVER_SCOPE,
        }
        added = sorted(key for key in defaults if key not in stored)
        # ``setdefault`` semantics: a key the user already set is preserved
        # exactly, including a deliberately unusual value.
        for key, value in defaults.items():
            stored.setdefault(key, value)
        _LOGGER.info(
            "Migrated config entry %s from v1 to v2; backfilled %d option key(s): %s",
            entry.entry_id,
            len(added),
            ", ".join(added) or "(none)",
        )
        hass.config_entries.async_update_entry(entry, options=stored, version=2)

    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a telegraf_mqtt config entry."""
    if Platform is None:
        return True
    unload_ok = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    runtime_data = entry.runtime_data
    if unload_ok and runtime_data.unsubscribe is not None:
        runtime_data.unsubscribe()
        runtime_data.unsubscribe = None
    if unload_ok and runtime_data.unsubscribe_snoop is not None:
        runtime_data.unsubscribe_snoop()
        runtime_data.unsubscribe_snoop = None
    # Drop the recorded scope too, so a teardown that ran without the
    # handle (mid-start failure) cannot leave ``_apply_auto_discover``
    # believing a snoop is still subscribed to a filter it is not.
    runtime_data.snoop_scope = None
    runtime_data.snoop_exclude_filter = None
    if unload_ok and runtime_data.cancel_expiry is not None:
        runtime_data.cancel_expiry()
        runtime_data.cancel_expiry = None
    return unload_ok


@dataclass(frozen=True)
class TelegrafMqttOptions:
    """Normalized runtime options.

    Phase 6: ``enable_cleanup``, ``cleanup_delay``, ``delete_delay`` and
    ``min_active_metrics`` are all user-facing (OptionsFlow). ``expire_after``
    is unchanged from Phase 2.

    Phase 10: ``category_overrides`` and ``device_id_strategy`` are
    user-facing. ``auto_discover`` is also user-facing (default off --
    the user must opt in via the options flow) and controls whether the
    post-setup snoop listener runs; ``auto_discover_scope`` is the
    explicit, user-editable filter that listener subscribes to, so
    nothing is widened implicitly.
    """

    expire_after: int
    exclude_patterns: tuple[str, ...]
    field_overrides: dict[str, dict[str, Any]]
    enable_cleanup: bool
    cleanup_delay: int
    delete_delay: int
    min_active_metrics: int
    category_overrides: dict[str, str | None]
    device_id_strategy: str
    auto_discover: bool
    auto_discover_scope: str


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Apply options live, or reload when the device-id strategy changed.

    This is the single options-update listener. It also owns the reload
    decision, and it MUST make that decision *before* calling
    ``apply_options``.

    Why the ordering is load-bearing: ``device_id_strategy`` feeds
    ``DeviceManager._derive_device_id``, so a change invalidates every key
    in ``self.devices`` (keyed by the old strategy's slugs). A live apply
    cannot repair those registries -- rebuilding them changes every
    entity's ``unique_id``, which the project documents as MAJOR-breaking.
    Reloading is the only way to keep the entity registry consistent.

    The previous implementation registered TWO listeners: a live-apply one
    and a reload one, and relied on registration order. The live one ran
    first and called ``apply_options(device_id_strategy=...)``, which
    overwrites ``DeviceManager._device_id_strategy``. The reload one then
    compared that already-mutated value against the incoming option and
    found them equal, so ``async_reload`` was unreachable -- the reload
    never fired. Comparing against
    ``runtime_data.applied_device_id_strategy`` (captured at setup, before
    any live apply) removes the ordering dependency entirely.
    """
    options = _options_from_entry(entry)
    runtime = getattr(entry, "runtime_data", None)
    if runtime is None or runtime.manager is None:
        # Mid-unload, or a partially-initialised entry. There is nothing to
        # apply and nothing to reload; bail rather than raise inside HA's
        # update-listener machinery.
        return

    if runtime.applied_device_id_strategy != options.device_id_strategy:
        _LOGGER.info(
            "device_id_strategy changed from %s to %s; reloading the config entry so every device id is re-derived",
            runtime.applied_device_id_strategy,
            options.device_id_strategy,
        )
        await hass.config_entries.async_reload(entry.entry_id)
        return

    entry.runtime_data.manager.apply_options(
        expire_after=options.expire_after,
        exclude_patterns=options.exclude_patterns,
        field_overrides=options.field_overrides,
        enable_cleanup=options.enable_cleanup,
        min_active_metrics=options.min_active_metrics,
        cleanup_delay=options.cleanup_delay,
        delete_delay=options.delete_delay,
        category_overrides=options.category_overrides,
        device_id_strategy=options.device_id_strategy,
        on_write=lambda unique_key, available, value: _dispatch_metric_updated(hass, entry, unique_key),
    )
    _schedule_expiry_check(hass, entry)
    # The auto-discover toggle is applied live too: the snoop listener
    # is started in place when the option turns on and its subscription
    # is torn down when it turns off. Without this, an opt-in did
    # nothing until the next reload and an opt-out left the long-lived
    # listener subscribed and dispatching indefinitely. Idempotent --
    # no-op when the listener state already matches the option.
    await _apply_auto_discover(hass, entry, options)
    # Re-run the runtime-detected device-id Repairs checks immediately:
    # a structural option change (exclude_patterns, device_id_strategy,
    # ...) can create or resolve a collision/conflict, and the user
    # should see that without waiting for the next periodic tick. Both
    # checks are idempotent create-or-delete calls and self-guard when
    # the issue registry is unavailable. The ``device_id_strategy``
    # reload path re-runs them after the rebuild via ``async_setup_entry``.
    runtime.applied_device_id_strategy = options.device_id_strategy
    check_device_id_collision(hass, entry)
    check_device_id_conflict(hass, entry)
    # Also re-run the fleet-scale cap checks: a live options change
    # does not reset the dropped counts, but the user should see the
    # current cap state reflected in the Repairs UI without a reload.
    check_device_cap(hass, entry)
    check_metric_cap(hass, entry)


async def _apply_auto_discover(hass: HomeAssistant, entry: ConfigEntry, options: TelegrafMqttOptions) -> None:
    """Start, restart, or stop the auto-discover snoop to match the options.

    Shared by ``async_setup_entry`` (initial opt-in) and the live
    options-update listener, so a toggle takes effect immediately in both
    directions instead of silently doing nothing until the next reload --
    or, worse, leaving a long-lived listener subscribed and dispatching
    after the user disabled it.

    Three outcomes, keyed off the handle the runtime already parks:

    * no listener + ``auto_discover`` on -> subscribe to
      ``options.auto_discover_scope``;
    * listener running + the scope or the main ``topic_pattern`` changed
      -> stop it and start a fresh one. A scope change cannot be applied
      by mutating the running listener, because ``SnoopListener`` reads
      both filters from a per-message broker callback; and the restart is
      cheap enough (one SUBSCRIBE / UNSUBSCRIBE round-trip) that a
      stop-then-start is the right trade for a correct, single
      subscription;
    * listener running + nothing changed -> return. This is the common
      case on every options save that touches anything else, and it is
      what keeps the broker subscription count at exactly one.

    Stopping with no listener parked is a no-op.

    ``exclude_filter`` is the entry's own ``topic_pattern``, so the snoop
    only ever dispatches messages the main subscription did not already
    handle -- that is what makes the feature additive instead of
    doubling every message's parse and dispatcher fan-out.
    """
    runtime = entry.runtime_data
    if mqtt is None or runtime is None or runtime.manager is None:
        return
    topic_pattern = entry.data.get(CONF_TOPIC_PATTERN)
    if not isinstance(topic_pattern, str) or not topic_pattern:
        # Setup's own guard is the user-facing error; here it just means
        # there is no valid filter to skip against, so do not start a
        # snoop that would duplicate every message.
        _LOGGER.debug("Auto-discover skipped: entry has no usable %s", CONF_TOPIC_PATTERN)
        return

    if not options.auto_discover:
        if runtime.unsubscribe_snoop is not None:
            runtime.unsubscribe_snoop()
            runtime.unsubscribe_snoop = None
            _LOGGER.info("Auto-discover snoop stopped")
        runtime.snoop_scope = None
        runtime.snoop_exclude_filter = None
        return

    if (
        runtime.unsubscribe_snoop is not None
        and runtime.snoop_scope == options.auto_discover_scope
        and runtime.snoop_exclude_filter == topic_pattern
    ):
        # Already subscribed to exactly the right pair of filters.
        return

    if runtime.unsubscribe_snoop is not None:
        # A restart, not a second subscription: tear the old one down
        # first so the broker never carries two snoop callbacks.
        runtime.unsubscribe_snoop()
        runtime.unsubscribe_snoop = None
        _LOGGER.debug("Auto-discover scope changed; restarting the snoop listener")

    snoop = SnoopListener(
        timeout_seconds=0.0,
        probe_topic=options.auto_discover_scope,
        dispatcher=runtime.manager.process_message,
        exclude_filter=topic_pattern,
    )
    try:
        await snoop.start(hass, mqtt.async_subscribe)
    except Exception as snoop_err:
        # Snoop failure is non-fatal -- the main subscription is the
        # user-facing path; log and move on without parking a teardown
        # handle, so the next options save retries from a clean state.
        _LOGGER.debug("Snoop listener failed to start: %s", snoop_err)
        snoop.stop()
    else:
        runtime.unsubscribe_snoop = snoop.stop
        runtime.snoop_scope = options.auto_discover_scope
        runtime.snoop_exclude_filter = topic_pattern
        _LOGGER.info(
            "Auto-discover snoop subscribed to %s (skipping anything already covered by %s)",
            options.auto_discover_scope,
            topic_pattern,
        )
    check_auto_discover_scope(hass, entry)


# REMOVED (was ``_async_options_maybe_reload``):
#   This was the second options-update listener. It compared
#   ``runtime.manager.device_id_strategy`` -- the slot the FIRST listener had
#   already overwritten via ``apply_options`` -- against the incoming option,
#   so the comparison was always equal and ``async_reload`` was unreachable.
#   The reload decision now lives in ``_async_options_updated`` and compares
#   against ``runtime_data.applied_device_id_strategy``, which no live apply
#   mutates. Tests: ``tests/test_phase10_ux.py`` drives the single listener.


def _coerce_int_option(
    raw_options: dict[str, Any],
    key: str,
    default: int,
    *,
    minimum: int = 0,
) -> tuple[int, bool]:
    """Coerce a numeric option, returning ``(value, was_invalid)``.

    ``was_invalid`` is True if the value was present but not coercible
    to a non-negative int. ``_options_from_entry_with_repair`` uses
    this to surface a Repair issue for each invalid field while still
    applying the default so setup does not crash.
    """
    if key not in raw_options:
        return default, False
    raw = raw_options[key]
    if isinstance(raw, bool) or (isinstance(raw, float) and not raw.is_integer()):
        return default, True
    try:
        value = int(raw)
    except (TypeError, ValueError):  # fmt: skip
        return default, True
    if value < minimum:
        return default, True
    return value, False


def _coerce_bool_option(raw_options: dict[str, Any], key: str, default: bool) -> tuple[bool, bool]:
    if key not in raw_options:
        return default, False
    value = raw_options[key]
    if not isinstance(value, bool):
        return default, True
    return value, False


def _is_valid_filter(value: Any) -> bool:
    """Return whether ``value`` is a usable MQTT subscription filter.

    A ``telegraf/#``-shaped string with no illegal wildcard placement. The
    options flow already rejects an invalid value before it is persisted,
    so this exists for the *stored* config: a hand-edited ``.storage``, a
    truncated write, or a value written by an older release. Duplicates
    ``config_flow._valid_subscription_topic`` rather than importing it --
    ``config_flow`` pulls in ``voluptuous`` and Home Assistant's
    ``selector`` module, and this runs on the integration's import path.
    """
    if not isinstance(value, str) or not value:
        return False
    parts = value.split("/")
    for index, part in enumerate(parts):
        if "#" in part and (part != "#" or index != len(parts) - 1):
            return False
        if "+" in part and part != "+":
            return False
    return True


def _normalize_options(
    raw_options: dict[str, Any],
) -> tuple[TelegrafMqttOptions, list[str]]:
    """Coerce raw config-entry options into ``TelegrafMqttOptions``.

    Shared by setup (``_options_from_entry_with_repair``) and the live
    update / expiry-scheduling consumers (``_options_from_entry``) so
    every path sees identical normalization. Invalid persisted values
    -- such as a corrupted ``expire_after="abc"`` -- fall back to their
    documented defaults and are listed in the returned ``invalid_keys``
    instead of raising ``ValueError``/``TypeError``.
    """
    invalid: list[str] = []

    expire_after, bad = _coerce_int_option(raw_options, CONF_EXPIRE_AFTER, DEFAULT_EXPIRE_AFTER, minimum=1)
    if bad:
        invalid.append(CONF_EXPIRE_AFTER)

    cleanup_delay, bad = _coerce_int_option(raw_options, CONF_CLEANUP_DELAY, DEFAULT_CLEANUP_DELAY)
    if bad:
        invalid.append(CONF_CLEANUP_DELAY)

    delete_delay, bad = _coerce_int_option(raw_options, CONF_DELETE_DELAY, DEFAULT_DELETE_DELAY)
    if bad:
        invalid.append(CONF_DELETE_DELAY)

    min_active_metrics, bad = _coerce_int_option(raw_options, CONF_MIN_ACTIVE_METRICS, DEFAULT_MIN_ACTIVE_METRICS)
    if bad:
        invalid.append(CONF_MIN_ACTIVE_METRICS)

    enable_cleanup, bad = _coerce_bool_option(raw_options, CONF_ENABLE_CLEANUP, DEFAULT_ENABLE_CLEANUP)
    if bad:
        invalid.append(CONF_ENABLE_CLEANUP)

    # Validate the persisted device_id_strategy against the known set so a
    # corrupted value (typo, empty string, old/missing entry) cannot reach
    # DeviceManager -- it would otherwise fall back to the default silently
    # inside the registry and the user would never see a Repair issue.
    raw_device_id_strategy = raw_options.get(CONF_DEVICE_ID_STRATEGY, DEFAULT_DEVICE_ID_STRATEGY)
    device_id_strategy = (
        raw_device_id_strategy if raw_device_id_strategy in VALID_DEVICE_ID_STRATEGIES else DEFAULT_DEVICE_ID_STRATEGY
    )
    if device_id_strategy != raw_device_id_strategy:
        invalid.append(CONF_DEVICE_ID_STRATEGY)

    auto_discover, bad = _coerce_bool_option(raw_options, CONF_AUTO_DISCOVER, DEFAULT_AUTO_DISCOVER)
    if bad:
        invalid.append(CONF_AUTO_DISCOVER)

    # A scope the user typed is stored verbatim; a corrupt or empty one
    # falls back to the documented default. It is a user-visible filter,
    # so flag it for the Repairs panel rather than silently substituting
    # a different subscription than the one on screen.
    raw_scope = raw_options.get(CONF_AUTO_DISCOVER_SCOPE, DEFAULT_AUTO_DISCOVER_SCOPE)
    auto_discover_scope = raw_scope if _is_valid_filter(raw_scope) else DEFAULT_AUTO_DISCOVER_SCOPE
    if auto_discover_scope != raw_scope:
        invalid.append(CONF_AUTO_DISCOVER_SCOPE)

    options = TelegrafMqttOptions(
        expire_after=expire_after,
        exclude_patterns=tuple(str(pattern) for pattern in raw_options.get(CONF_EXCLUDE_PATTERNS, [])),
        field_overrides=dict(raw_options.get(CONF_FIELD_OVERRIDES, {})),
        enable_cleanup=enable_cleanup,
        cleanup_delay=cleanup_delay,
        delete_delay=delete_delay,
        min_active_metrics=min_active_metrics,
        category_overrides={
            str(key): (None if value in (None, "") else str(value))
            for key, value in dict(raw_options.get(CONF_CATEGORY_OVERRIDES, {})).items()
        },
        device_id_strategy=device_id_strategy,
        auto_discover=auto_discover,
        auto_discover_scope=auto_discover_scope,
    )
    return options, invalid


def _options_from_entry_with_repair(hass: HomeAssistant, entry: ConfigEntry) -> tuple[TelegrafMqttOptions, list[str]]:
    """Normalize config entry options, surfacing invalid ones as a Repair issue.

    Returns a tuple of ``(options, list_of_invalid_keys)``. Setup still
    succeeds with the defaults for any invalid field; the user sees
    the issue in Settings -> Repairs and can correct it from the
    options UI.
    """
    raw_options = getattr(entry, "options", {}) or {}
    options, invalid = _normalize_options(raw_options)

    # Phase 7: raise / clear the Repair issue for invalid options.
    check_invalid_persisted_option(hass, entry, invalid)

    return options, invalid


def _options_from_entry(entry: ConfigEntry) -> TelegrafMqttOptions:
    """Normalize config entry options into registry settings.

    Uses the exact same safe coercion path as setup
    (``_normalize_options``), so a corrupted persisted value such as
    ``expire_after="abc"`` falls back to its default instead of
    raising. Retained for callers that must not touch the Repair-issue
    registry on every options change (the live ``_async_options_updated``
    listener and ``_schedule_expiry_check`` rescheduling): they consume
    the normalized ``TelegrafMqttOptions`` with no coercion errors
    escaping, while setup owns the Repairs side effect.
    """
    raw_options = getattr(entry, "options", {}) or {}
    options, _invalid = _normalize_options(raw_options)
    return options


def _schedule_expiry_check(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Schedule or replace the periodic registry expiry check."""
    if async_track_time_interval is None:
        return

    runtime_data = entry.runtime_data
    if runtime_data.cancel_expiry is not None:
        runtime_data.cancel_expiry()

    # The tick body is a synchronous full-registry scan on the event
    # loop (expiry + cleanup + device pruning + the no-traffic Repairs
    # check), so the cadence is floored well above 1s: at fleet scale a
    # once-per-second scan would add measurable loop latency for every
    # integration sharing the loop. Staleness *detection* is
    # timestamp-based, so the floor only delays the availability flip
    # by at most MIN_EXPIRY_TICK_SECONDS for very small expire_after
    # values -- precision that is not meaningfully useful anyway.
    interval_seconds = max(
        MIN_EXPIRY_TICK_SECONDS,
        min(_options_from_entry(entry).expire_after, MAX_EXPIRY_TICK_SECONDS),
    )

    # @callback is REQUIRED here: async_track_time_interval offloads plain
    # sync functions to executor threads (HassJob), where the dispatcher
    # send below is a hard error under HA's thread-safety checks.
    @callback
    def check_expiry(now: Any) -> None:
        runtime_data.manager.check_expiry(
            on_write=lambda metric_key, available, value: _dispatch_metric_updated(hass, entry, metric_key)
        )
        # cleanup() returns (device_id, unique_key) pairs. Fire
        # SIGNAL_REMOVE_METRIC for each so the entity is dropped from HA's
        # entity registry rather than left as an unavailable shell.
        for device_id, unique_key in runtime_data.manager.cleanup(
            on_write=lambda metric_key, available, value: _dispatch_metric_updated(hass, entry, metric_key)
        ):
            _dispatch_remove_metric(hass, entry, device_id, unique_key)
        # ``delete_delay``: retire whole devices whose host has gone silent,
        # reporting every entity they still hold so those are removed too.
        runtime_data.manager.prune_stale_devices(
            on_remove=lambda device_id, unique_key: _dispatch_remove_metric(hass, entry, device_id, unique_key)
        )
        # Phase 10: surface a Repairs hint if the snoop listener has had
        # at least one tick and no message matched the configured topic
        # pattern. Running this from the periodic callback (rather than
        # ``async_setup_entry``) gives the snoop listener time to receive
        # messages before we flag the topic as silent. ``check_no_traffic``
        # is idempotent, so repeated ticks just refresh / auto-resolve the
        # issue as traffic state changes.
        check_no_traffic(hass, entry)

    runtime_data.cancel_expiry = async_track_time_interval(hass, check_expiry, timedelta(seconds=interval_seconds))


def _dispatch_metric_updated(hass: HomeAssistant, entry: ConfigEntry, unique_key: str) -> None:
    """Dispatch a registry update signal for one metric."""
    if async_dispatcher_send is not None:
        async_dispatcher_send(
            hass,
            SIGNAL_METRIC_UPDATED.format(entry_id=entry.entry_id),
            unique_key,
        )


def _dispatch_new_metric(hass: HomeAssistant, entry: ConfigEntry, metric_key: str) -> None:
    """Dispatch the new-metric signal so platforms add an entity for it."""
    if async_dispatcher_send is not None:
        async_dispatcher_send(
            hass,
            SIGNAL_NEW_METRIC.format(entry_id=entry.entry_id),
            metric_key,
        )


def _dispatch_remove_metric(
    hass: HomeAssistant,
    entry: ConfigEntry,
    device_id: str,
    unique_key: str,
) -> None:
    """Dispatch the remove-metric signal for one ``(device_id, unique_key)``.

    Fired from the periodic cleanup pass for every metric the registry
    removed, from ``prune_stale_devices`` for every entity a retired device
    still held, and from ``MetricRegistry`` for the
    ``platform_hint == "none"`` override.

    The payload is a real 2-tuple rather than the ``"{device_id}:{unique_key}"``
    string this used to send. The listener re-derives the entity's
    ``unique_id`` from those two parts, and the string form could not be
    split back unambiguously once a ``unique_key`` was allowed to contain a
    ``:`` -- it silently produced a wrong lookup and an orphaned entity.
    """
    if async_dispatcher_send is not None:
        async_dispatcher_send(
            hass,
            SIGNAL_REMOVE_METRIC.format(entry_id=entry.entry_id),
            device_id,
            unique_key,
        )


def _make_new_device_callback(hass: HomeAssistant, entry: ConfigEntry) -> Callable[[str, str], None]:
    """Build the device-discovery callback that announces a newly seen host."""

    def on_new_device(device_id: str, device_name: str) -> None:
        _LOGGER.info("Discovered new Telegraf device %s (%s)", device_name, device_id)
        if async_dispatcher_send is not None:
            async_dispatcher_send(
                hass,
                SIGNAL_NEW_DEVICE.format(entry_id=entry.entry_id),
                device_id,
                device_name,
            )

    return on_new_device


def remove_metric_entity(hass: HomeAssistant, device_id: str, unique_key: str) -> bool:
    """Delete the entity for one ``(device_id, unique_key)`` pair.

    The platform's ``unique_id`` pattern is
    ``f"{DOMAIN}_{state.device_id}_{descriptor.unique_key}"``, and both
    parts are supplied separately here, so there is no string to
    re-parse and no ambiguity about where the device ends and the metric
    begins.

    The lookup goes through ``async_get_entity_id``, HA's own O(1)
    index. The previous implementation iterated every entity in the
    instance -- across every platform -- on each removal, which is
    O(total entities) per removed metric inside a periodic event-loop
    callback.
    """
    if er is None:
        return False
    registry = er.async_get(hass)
    target_unique_id = f"{DOMAIN}_{device_id}_{unique_key}"
    for platform in ("sensor", "binary_sensor"):
        entity_id = registry.async_get_entity_id(platform, DOMAIN, target_unique_id)
        if entity_id is not None:
            registry.async_remove(entity_id)
            return True
    return False


def _listener_remove_metric(hass: HomeAssistant, entry: ConfigEntry) -> Callable[..., Any]:
    """Build the dispatcher listener that turns ``SIGNAL_REMOVE_METRIC`` into
    an entity-registry removal.

    Kept as a module-level function (rather than an inline closure in
    ``async_setup_entry``) so the listener body is a single statement
    that is trivially covered by the ``remove_metric_entity`` tests --
    and so the real-harness test does not need to wait on async
    dispatcher task scheduling.

    ``async_dispatcher_connect`` is itself synchronous (it returns an
    unsubscribe callable); the listener we register is an async
    function so callers can ``await`` its body.
    """
    if async_dispatcher_connect is None:
        return lambda: None

    async def _on_remove(device_id: str, unique_key: str) -> None:
        remove_metric_entity(hass, device_id, unique_key)

    return async_dispatcher_connect(
        hass,
        SIGNAL_REMOVE_METRIC.format(entry_id=entry.entry_id),
        _on_remove,
    )
