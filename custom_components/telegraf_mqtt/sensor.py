"""Sensor platform for telegraf_mqtt."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any, cast

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity, SensorStateClass
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, UnitOfTemperature
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    PLATFORM_HINT_SENSOR,
    SIGNAL_METRIC_UPDATED,
    SIGNAL_NEW_METRIC,
    SIGNAL_REMOVE_METRIC,
)
from .heuristics import ENTITY_CATEGORY_DIAGNOSTIC
from .icons import ICON_FOR_KEY
from .models import is_bool_metric
from .naming import infer_icon_key
from .registry import DeviceManager

_LOGGER = logging.getLogger(__name__)


def _entity_category(value: str | None) -> EntityCategory | None:
    """Coerce a resolved category string into HA's enum at the platform boundary.

    The value originates from ``category_overrides``, which is free text
    the user types, so an unrecognised string is a *configuration* mistake
    rather than a bug -- and HA would raise ``ValueError`` from inside
    entity construction, which fails the whole platform setup rather than
    the one field. Degrade to "no category" (the entity still appears in
    the primary list) and name the offending value in the log so the user
    can fix it.
    """
    if not value:
        return None
    try:
        return EntityCategory(value)
    except ValueError:
        _LOGGER.warning(
            "Unknown entity category %r; ignoring it. Valid values are 'config' and 'diagnostic'.",
            value,
        )
        return None


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry, async_add_entities: AddEntitiesCallback) -> None:
    """Set up sensor entities from a config entry.

    **One dispatcher listener, not one per entity (WS-E3).** The table
    ``entities`` is this platform's single index from a registry metric key
    to the entity that owns it, and the entry-wide ``SIGNAL_METRIC_UPDATED``
    is dispatched to exactly ONE listener -- the closure ``route_update``
    below -- for the whole platform.

    Every entity used to connect to that signal itself in
    ``async_added_to_hass``. HA's dispatcher is a flat list, so a single
    MQTT message carrying M changed fields invoked all N entity listeners M
    times each. At the project's own caps (``DEFAULT_MAX_DEVICES = 50``,
    ``MAX_METRICS_PER_DEVICE = 1000``) a ``system`` or ``disk`` payload
    produced thousands of no-op callback invocations on the event loop, every
    one of which re-read the registry and compared a key against itself.

    With the table, the cost is one dict lookup per dispatch regardless of
    how many entities the platform owns, and the routing re-evaluation and
    the state write happen in the same pass over one key.
    """
    manager = entry.runtime_data.manager
    entities: dict[str, TelegrafMqttSensor] = {}

    @callback
    def add_metric(metric_key: str) -> None:
        state = manager.get_metric(metric_key)
        if state is None or metric_key in entities:
            return
        # Phase 10 platform routing: bool values belong to the
        # binary_sensor platform unless a field override forced this
        # field onto the sensor platform (``platform_hint="sensor"``).
        # Non-bool values are always sensor material.
        if is_bool_metric(state.value) and state.descriptor.platform_hint != PLATFORM_HINT_SENSOR:
            return
        entity = TelegrafMqttSensor(entry, metric_key, entities)
        entities[metric_key] = entity
        async_add_entities([entity])

    # The combined routing + write listener. Phase 10 re-routing: a
    # ``field_overrides`` change can flip a bool field's ``platform_hint``
    # (sensor <-> binary_sensor, or ``none``) after the entity has already
    # been added, and the entity must move -- otherwise the user sees a
    # bool value rendered as a "True/False" sensor string on the wrong
    # platform until they reload the config entry. The registry's
    # ``apply_options`` re-applies the override and fires
    # ``SIGNAL_METRIC_UPDATED`` for every changed state, so this single
    # listener is where both "is this still mine?" and "write my state"
    # are answered.
    @callback
    def route_update(metric_key: str) -> None:
        state = manager.get_metric(metric_key)
        if state is None:
            # The metric is gone from the registry. It may or may not still
            # own an entity here: the ``platform_hint == "none"`` override
            # and the cleanup lifecycle both drop the state, and only the
            # latter sends SIGNAL_REMOVE_METRIC. Releasing the key means a
            # metric that later returns is re-adopted rather than being
            # permanently blocked by a stale table entry.
            entities.pop(metric_key, None)
            return
        # Same forward check as ``add_metric``, inverted: if a bool
        # field is no longer pinned to ``sensor`` it belongs on the
        # binary_sensor platform (or, with ``hint=none``, nowhere).
        if is_bool_metric(state.value) and state.descriptor.platform_hint != PLATFORM_HINT_SENSOR:
            if entities.pop(metric_key, None) is not None:
                async_dispatcher_send(
                    hass,
                    SIGNAL_REMOVE_METRIC.format(entry_id=entry.entry_id),
                    metric_key,
                )
            return
        entity = entities.get(metric_key)
        if entity is None:
            # This platform now owns the metric (or the flip was undone):
            # route through ``add_metric`` so the dedup guard and the
            # platform check live in exactly one place.
            add_metric(metric_key)
            return
        entity.handle_metric_updated(metric_key)

    for metric_key in manager:
        add_metric(metric_key)

    entry.async_on_unload(
        async_dispatcher_connect(
            hass,
            SIGNAL_NEW_METRIC.format(entry_id=entry.entry_id),
            add_metric,
        )
    )
    entry.async_on_unload(
        async_dispatcher_connect(
            hass,
            SIGNAL_METRIC_UPDATED.format(entry_id=entry.entry_id),
            route_update,
        )
    )

    @callback
    def forget_metric(device_id: str, unique_key: str) -> None:
        """Drop a metric whose entity has just been deleted.

        Without this the table keeps claiming the metric even after
        ``remove_metric_entity`` has removed the entity from HA's entity
        registry. When the host republishes, ``on_discovered`` fires,
        ``add_metric`` sees the key already present, and returns early --
        so the entity never comes back until the config entry is reloaded.
        That is the cleanup lifecycle the user is told about
        (``Active -> Unavailable -> Cleanup Candidate -> Deleted``) silently
        failing to reverse itself.
        """
        for metric_key in (f"{device_id}:{unique_key}", unique_key):
            entities.pop(metric_key, None)

    entry.async_on_unload(
        async_dispatcher_connect(
            hass,
            SIGNAL_REMOVE_METRIC.format(entry_id=entry.entry_id),
            forget_metric,
        )
    )


class TelegrafMqttSensor(SensorEntity):
    """Sensor backed by one Telegraf metric on one discovered device.

    Phase 9: user-facing display is translation-driven. The entity sets
    ``_attr_translation_key`` and ``_attr_translation_placeholders`` from
    the descriptor and lets HA render the localised string. There is no
    ``_attr_name`` -- every entity uses the translation path. The icon
    comes from ``icons.ICON_FOR_KEY`` keyed on the descriptor's inferred
    icon key. Diagnostic entities are disabled by default.
    """

    _attr_has_entity_name = True
    _attr_should_poll = False
    _attr_translation_key: str | None = None
    # HA's base ``Entity`` types this as ``Mapping[str, str]`` although
    # ``None`` is the de-facto unset value; the ignore pins the runtime
    # contract the entity code relies on.
    _attr_translation_placeholders: Mapping[str, str] | None = None  # type: ignore[assignment]
    _attr_entity_registry_enabled_default: bool = True

    def __init__(
        self,
        entry: ConfigEntry,
        metric_key: str,
        platform_entities: dict[str, TelegrafMqttSensor] | None = None,
    ) -> None:
        self._entry = entry
        self._metric_key = metric_key
        # The platform's key -> entity table, so the entity can unregister
        # itself when HA removes it for a reason the integration did not
        # initiate (a reload, the platform being torn down, a user action).
        # Without this the table would keep a strong reference to a dead
        # entity forever, and its key would block the metric from ever
        # being re-adopted.
        self._platform_entities = platform_entities
        self._refresh_descriptor_attributes()

    def _refresh_descriptor_attributes(self) -> None:
        state = self._manager.get_metric(self._metric_key)
        if state is None:
            return
        descriptor = state.descriptor
        self._attr_unique_id = f"{DOMAIN}_{state.device_id}_{descriptor.unique_key}"
        self._attr_translation_key = descriptor.translation_key
        self._attr_translation_placeholders = dict(descriptor.translation_placeholders)
        self._attr_native_unit_of_measurement = _normalize_native_unit(descriptor.native_unit)
        # The descriptors carry plain strings; HA's attrs are enum types
        # with identical values. A stray value must not crash entity
        # creation, so this is a cast rather than an enum construction.
        self._attr_device_class = cast("SensorDeviceClass | None", descriptor.suggested_device_class)
        self._attr_state_class = cast("SensorStateClass | None", descriptor.suggested_state_class)
        self._attr_entity_category = _entity_category(descriptor.entity_category)
        self._attr_entity_registry_enabled_default = descriptor.entity_category != ENTITY_CATEGORY_DIAGNOSTIC
        self._attr_icon = ICON_FOR_KEY.get(
            infer_icon_key(descriptor.measurement, descriptor.field),
            ICON_FOR_KEY["generic"],
        )
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, state.device_id)},
            name=state.device_name,
            manufacturer=self._entry.runtime_data.manufacturer,
            model=self._entry.runtime_data.model,
            sw_version=self._entry.runtime_data.sw_version,
        )

    @property
    def _manager(self) -> DeviceManager:
        # ``ConfigEntry.runtime_data`` is untyped from HA's side; the
        # integration guarantees a ``TelegrafMqttRuntimeData`` here.
        manager: DeviceManager = self._entry.runtime_data.manager
        return manager

    async def async_will_remove_from_hass(self) -> None:
        """Unregister from the platform table when HA drops this entity.

        The counterpart to the platform's ``forget_metric`` handler. That
        one fires when *the integration* removes the entity; this one
        covers every other path, so a stale key can never survive in the
        table and silently block the metric's re-adoption.
        """
        if self._platform_entities is not None:
            self._platform_entities.pop(self._metric_key, None)

    @callback
    def handle_metric_updated(self, metric_key: str) -> None:
        """Write HA state when this metric changes.

        Called by the platform's single ``route_update`` listener rather
        than by a per-entity dispatcher subscription. Kept as a public,
        directly-callable method so a unit test can drive one entity's
        refresh without standing up the whole platform, and so the
        "non-matching key is ignored" contract stays testable in isolation.
        """
        if metric_key == self._metric_key:
            self._refresh_descriptor_attributes()
            self.async_write_ha_state()

    @property
    def available(self) -> bool:
        """Return whether this metric is currently available."""
        state = self._manager.get_metric(self._metric_key)
        return state is not None and state.is_available

    @property
    def native_value(self) -> Any:
        """Return the current registry value."""
        state = self._manager.get_metric(self._metric_key)
        if state is None:
            return None
        return state.value

    @property
    def extra_state_attributes(self) -> dict[str, Any] | None:
        """Expose Telegraf identity details for troubleshooting."""
        state = self._manager.get_metric(self._metric_key)
        if state is None:
            return None
        descriptor = state.descriptor
        return {
            "measurement": descriptor.measurement,
            "field": descriptor.field,
            "tags": dict(descriptor.tags),
            "timestamp": descriptor.timestamp,
        }


def _normalize_native_unit(native_unit: str | None) -> str | None:
    if native_unit == "\u00b0C":
        return UnitOfTemperature.CELSIUS
    return native_unit
