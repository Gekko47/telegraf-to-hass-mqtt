"""The telegraf_mqtt integration."""

from __future__ import annotations

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
    from homeassistant.exceptions import ConfigEntryNotReady
    from homeassistant.helpers import entity_registry as er
    from homeassistant.helpers import issue_registry as ir
    from homeassistant.helpers.dispatcher import (
        async_dispatcher_connect,
        async_dispatcher_send,
    )
    from homeassistant.helpers.event import async_track_time_interval
except ModuleNotFoundError:  # pragma: no cover - exercised only in unit-test import isolation
    ConfigEntry = object
    Platform = None
    HomeAssistant = object
    callback = lambda target: target  # noqa: E731 - identity when HA is absent
    mqtt = None
    async_dispatcher_connect = None
    async_dispatcher_send = None
    async_track_time_interval = None
    ConfigEntryNotReady = Exception
    er = None
    ir = None

from .const import (
    CONF_CLEANUP_DELAY,
    CONF_DELETE_DELAY,
    CONF_ENABLE_CLEANUP,
    CONF_EXCLUDE_PATTERNS,
    CONF_EXPIRE_AFTER,
    CONF_FIELD_OVERRIDES,
    CONF_MIN_ACTIVE_METRICS,
    CONF_TOPIC_PATTERN,
    DEFAULT_CLEANUP_DELAY,
    DEFAULT_DELETE_DELAY,
    DEFAULT_ENABLE_CLEANUP,
    DEFAULT_EXPIRE_AFTER,
    DEFAULT_MIN_ACTIVE_METRICS,
    DOMAIN,
    SIGNAL_METRIC_UPDATED,
    SIGNAL_NEW_DEVICE,
    SIGNAL_NEW_METRIC,
    SIGNAL_REMOVE_METRIC,
)
from .exceptions import (
    MqttBrokerUnreachable,
    ReconfigureSubscribeFailed,
    TelegrafMqttException,
)
from .parser import ParserStats, TelegrafParser
from .registry import DeviceManager
from .repairs import (
    check_invalid_persisted_option,
    check_overlapping_topics,
)

_LOGGER = logging.getLogger(__name__)

PLATFORMS = [Platform.SENSOR, Platform.BINARY_SENSOR] if Platform is not None else []


@dataclass
class TelegrafMqttRuntimeData:
    """Runtime state for a config entry."""

    manager: DeviceManager
    parser: TelegrafParser
    parser_stats: Any  # ``custom_components.telegraf_mqtt.parser.ParserStats``
    manufacturer: str | None
    model: str | None
    sw_version: str | None = None
    unsubscribe: Callable[[], None] | None = None
    cancel_expiry: Callable[[], None] | None = None


def MqttBrokerUnreachable(topic: str, error: str) -> ConfigEntryNotReady:
    """Build a ``ConfigEntryNotReady`` for an unreachable MQTT broker.

    Shared by the probe and real subscription error paths in
    ``async_setup_entry`` so the toast text, translation domain/key, and
    topic/error placeholders stay in sync. The caller is responsible for
    raising the result with ``raise ... from <err>`` to preserve exception
    chaining.
    """
    ready_exc = ConfigEntryNotReady(
        f"Could not subscribe to {topic}: {error}"
    )
    ready_exc.translation_domain = DOMAIN
    ready_exc.translation_key = "mqtt_broker_unreachable"
    ready_exc.translation_placeholders = {"topic": topic, "error": error}
    return ready_exc


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up telegraf_mqtt from a config entry."""
    # Tolerate invalid persisted options: a corrupted value falls back
    # to the default AND raises a Repair issue so the user can correct
    # it from the UI without the entry failing to set up.
    options, invalid_options = _options_from_entry_with_repair(
        hass, entry
    )
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
    )
    entry.runtime_data = TelegrafMqttRuntimeData(
        manager=manager,
        parser=parser,
        parser_stats=parser_stats,
        manufacturer=entry.data.get("manufacturer"),
        model=entry.data.get("model"),
        sw_version=entry.data.get("sw_version"),
    )

    if mqtt is not None:
        topic_pattern = entry.data[CONF_TOPIC_PATTERN]

        manager.set_callbacks(
            on_write=lambda metric_key, available, value: _dispatch_metric_updated(hass, entry, metric_key),
            on_discovered=lambda metric_key: _dispatch_new_metric(hass, entry, metric_key),
            on_new_device=_make_new_device_callback(hass, entry),
        )

        async def message_received(message: Any) -> None:
            manager.process_message(message.topic, message.payload)

        # Phase 5 test-before-setup: validate the MQTT subscription before the
        # entry is considered "set up". A short-lived probe subscribes to the
        # configured topic with a no-op callback and immediately unsubscribes;
        # a successful SUBSCRIBE-ACK confirms the broker is reachable and
        # authorized for the topic. On failure we raise ``ConfigEntryNotReady``
        # so HA surfaces a retry-able error to the user instead of
        # half-configuring the entry.
        def _on_probe_message(_message: Any) -> None:  # pragma: no cover - probe is purely a SUBSCRIBE-ACK check; the callback never fires
            pass

        try:
            probe_unsubscribe = await mqtt.async_subscribe(
                hass, topic_pattern, _on_probe_message
            )
        except Exception as probe_err:  # noqa: BLE001 - broker errors vary, surface uniformly
            # Phase 9: ConfigEntryNotReady accepts translation_domain +
            # translation_key + translation_placeholders for the toast.
            raise MqttBrokerUnreachable(topic_pattern, str(probe_err)) from probe_err
        probe_unsubscribe()
        _LOGGER.debug(
            "MQTT subscribe probe OK for %s; installing real subscription", topic_pattern
        )

        try:
            entry.runtime_data.unsubscribe = await mqtt.async_subscribe(
                hass, topic_pattern, message_received
            )
        except Exception as real_err:  # pragma: no cover - the probe guards broker reachability; this arm is only reachable on a connection drop between SUBSCRIBE-ACKs
            raise MqttBrokerUnreachable(topic_pattern, str(real_err)) from real_err
        _LOGGER.info("Subscribed to Telegraf MQTT topic pattern %s", topic_pattern)

    if Platform is not None:
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    if async_track_time_interval is not None:
        _schedule_expiry_check(hass, entry)

    if async_dispatcher_connect is not None:
        entry.async_on_unload(_listener_remove_metric(hass, entry))

    if hasattr(entry, "async_on_unload") and hasattr(entry, "add_update_listener"):
        entry.async_on_unload(entry.add_update_listener(_async_options_updated))

    # Phase 7: Repairs for recoverable config problems. The overlap
    # check is idempotent -- if this entry's pattern is fine and no
    # other entry overlaps, it deletes any prior overlap_issue and
    # creates nothing. Same for invalid-persisted-option checks via
    # _options_from_entry_with_repair above.
    check_overlapping_topics(hass, entry)

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
    """

    expire_after: int
    exclude_patterns: tuple[str, ...]
    field_overrides: dict[str, dict[str, Any]]
    enable_cleanup: bool
    cleanup_delay: int
    delete_delay: int
    min_active_metrics: int


async def _async_options_updated(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Apply options live without reloading the config entry."""
    options = _options_from_entry(entry)
    entry.runtime_data.manager.apply_options(
        expire_after=options.expire_after,
        exclude_patterns=options.exclude_patterns,
        field_overrides=options.field_overrides,
        enable_cleanup=options.enable_cleanup,
        min_active_metrics=options.min_active_metrics,
        cleanup_delay=options.cleanup_delay,
        delete_delay=options.delete_delay,
        on_write=lambda unique_key, available, value: _dispatch_metric_updated(hass, entry, unique_key),
    )
    _schedule_expiry_check(hass, entry)


def _coerce_int_option(
    raw_options: dict,
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
    if isinstance(raw, bool) or (
        isinstance(raw, float) and not raw.is_integer()
    ):
        return default, True
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default, True
    if value < minimum:
        return default, True
    return value, False


def _coerce_bool_option(
    raw_options: dict, key: str, default: bool
) -> tuple[bool, bool]:
    if key not in raw_options:
        return default, False
    value = raw_options[key]
    if not isinstance(value, bool):
        return default, True
    return value, False


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

    expire_after, bad = _coerce_int_option(
        raw_options, CONF_EXPIRE_AFTER, DEFAULT_EXPIRE_AFTER, minimum=1
    )
    if bad:
        invalid.append(CONF_EXPIRE_AFTER)

    cleanup_delay, bad = _coerce_int_option(
        raw_options, CONF_CLEANUP_DELAY, DEFAULT_CLEANUP_DELAY
    )
    if bad:
        invalid.append(CONF_CLEANUP_DELAY)

    delete_delay, bad = _coerce_int_option(
        raw_options, CONF_DELETE_DELAY, DEFAULT_DELETE_DELAY
    )
    if bad:
        invalid.append(CONF_DELETE_DELAY)

    min_active_metrics, bad = _coerce_int_option(
        raw_options, CONF_MIN_ACTIVE_METRICS, DEFAULT_MIN_ACTIVE_METRICS
    )
    if bad:
        invalid.append(CONF_MIN_ACTIVE_METRICS)

    enable_cleanup, bad = _coerce_bool_option(
        raw_options, CONF_ENABLE_CLEANUP, DEFAULT_ENABLE_CLEANUP
    )
    if bad:
        invalid.append(CONF_ENABLE_CLEANUP)

    options = TelegrafMqttOptions(
        expire_after=expire_after,
        exclude_patterns=tuple(
            str(pattern) for pattern in raw_options.get(CONF_EXCLUDE_PATTERNS, [])
        ),
        field_overrides=dict(raw_options.get(CONF_FIELD_OVERRIDES, {})),
        enable_cleanup=enable_cleanup,
        cleanup_delay=cleanup_delay,
        delete_delay=delete_delay,
        min_active_metrics=min_active_metrics,
    )
    return options, invalid


def _options_from_entry_with_repair(
    hass: HomeAssistant, entry: ConfigEntry
) -> tuple[TelegrafMqttOptions, list[str]]:
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

    interval_seconds = max(1, min(_options_from_entry(entry).expire_after, 30))

    # @callback is REQUIRED here: async_track_time_interval offloads plain
    # sync functions to executor threads (HassJob), where the dispatcher
    # send below is a hard error under HA's thread-safety checks.
    @callback
    def check_expiry(now: Any) -> None:
        runtime_data.manager.check_expiry(
            on_write=lambda metric_key, available, value: _dispatch_metric_updated(hass, entry, metric_key)
        )
        # cleanup() returns a list of removed metric keys (composite form).
        # For each removal, fire SIGNAL_REMOVE_METRIC so the entity-registry
        # cleanup in _handle_remove_metric actually drops the entity.
        # prune_empty_devices is logged inside the manager itself.
        for removed_key in runtime_data.manager.cleanup(
            on_write=lambda metric_key, available, value: _dispatch_metric_updated(hass, entry, metric_key)
        ):
            _dispatch_remove_metric(hass, entry, removed_key)
        runtime_data.manager.prune_empty_devices()

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


def _dispatch_remove_metric(hass: HomeAssistant, entry: ConfigEntry, metric_key: str) -> None:
    """Dispatch the remove-metric signal so the entity-registry handler can drop the entity.

    The signal is fired from inside the periodic cleanup pass for every key
    that the registry actually removed. The listener registered in
    ``async_setup_entry`` looks up the entity by ``unique_id`` in HA's
    entity registry and calls ``async_remove`` on it, preserving siblings
    and the parent device.
    """
    if async_dispatcher_send is not None:
        async_dispatcher_send(
            hass,
            SIGNAL_REMOVE_METRIC.format(entry_id=entry.entry_id),
            metric_key,
        )


def _make_new_device_callback(hass: HomeAssistant, entry: ConfigEntry):
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


def remove_metric_entity(hass: HomeAssistant, composite_key: str) -> bool:
    if er is None:
        return False
    registry = er.async_get(hass)
    # The platform's unique_id pattern is
    # ``f"{DOMAIN}_{state.device_id}_{descriptor.unique_key}"`` where
    # ``state.device_id`` already contains ``_`` (it's the host slug) and
    # ``descriptor.unique_key`` uses ``_`` between its parts. The composite
    # key from cleanup uses ``:`` as the device/unique_key separator, so
    # we translate that one separator back to ``_`` for the lookup.
    target_unique_id = f"{DOMAIN}_{composite_key.replace(':', '_', 1)}"
    for reg_entry in list(registry.entities.values()):
        if reg_entry.platform == DOMAIN and reg_entry.unique_id == target_unique_id:
            registry.async_remove(reg_entry.entity_id)
            return True
    return False


def _listener_remove_metric(
    hass: HomeAssistant, entry: ConfigEntry
) -> Callable[..., Any]:
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

    async def _on_remove(unique_key: str) -> None:
        remove_metric_entity(hass, unique_key)

    return async_dispatcher_connect(
        hass,
        SIGNAL_REMOVE_METRIC.format(entry_id=entry.entry_id),
        _on_remove,
    )
