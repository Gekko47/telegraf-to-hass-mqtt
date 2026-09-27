"""Binary sensor platform for telegraf_mqtt."""

from __future__ import annotations

import logging
from collections.abc import Mapping
from typing import Any

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.dispatcher import async_dispatcher_connect, async_dispatcher_send
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    DOMAIN,
    PLATFORM_HINT_NONE,
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

    See ``sensor._entity_category``: the value is user-typed free text
    from ``category_overrides``, and an unrecognised one must not take
    down the whole platform with a ``ValueError`` from entity
    construction.
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
    """Set up binary sensor entities from a config entry.

    **One dispatcher listener, not one per entity (WS-E3).** See the
    matching note in ``sensor.py``: the table ``entities`` is this
    platform's single index from a registry metric key to the entity that
    owns it, and the entry-wide ``SIGNAL_METRIC_UPDATED`` is dispatched
    once per platform rather than once per entity. At the project's own
    caps a single ``system`` payload would otherwise invoke every binary
    sensor's listener once per changed field, on the event loop.
    """
    manager = entry.runtime_data.manager
    entities: dict[str, TelegrafMqttBinarySensor] = {}

    @callback
    def add_metric(metric_key: str) -> None:
        state = manager.get_metric(metric_key)
        if state is None or metric_key in entities:
            return
        # Phase 10 platform routing: only bool values land here (the
        # registry coerces 0/1 and strings when an override requests the
        # binary_sensor platform). A field the user forced onto the
        # sensor platform -- or excluded entirely (``hint=none``) -- is
        # rejected so all three routes stay disjoint.
        if not is_bool_metric(state.value) or state.descriptor.platform_hint in (
            PLATFORM_HINT_SENSOR,
            PLATFORM_HINT_NONE,
        ):
            return
        entity = TelegrafMqttBinarySensor(entry, metric_key, entities)
        entities[metric_key] = entity
        async_add_entities([entity])

    # The combined routing + write listener (the binary_sensor mirror of
    # ``sensor.route_update``). Phase 10 re-routing: a ``field_overrides``
    # change can flip a field's ``platform_hint`` (binary_sensor <->
    # sensor, or ``none``) after the entity has already been added, and
    # the entity must move -- otherwise the user sees the bool value
    # stranded on the wrong platform until they reload the config entry.
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
        # Inverse of ``add_metric``: drop the entity if the metric is
        # no longer routed here -- either because the user pinned it
        # to ``sensor``, excluded it (``hint=none``), or because the
        # value type changed and the sensor platform now owns it.
        if not is_bool_metric(state.value) or state.descriptor.platform_hint in (
            PLATFORM_HINT_SENSOR,
            PLATFORM_HINT_NONE,
        ):
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

        Without this the table keeps claiming the metric after
        ``remove_metric_entity`` has removed it from HA's entity registry,
        so when the host republishes ``add_metric`` returns early on the
        dedup guard and the entity never comes back until the config entry
        is reloaded.
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


class TelegrafMqttBinarySensor(BinarySensorEntity):
    """Binary sensor backed by one Telegraf metric on one discovered device.

    Phase 9: translation-driven display, icon from icons.ICON_FOR_KEY,
    diagnostic entities disabled by default. Mirrors TelegrafMqttSensor.
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
        platform_entities: dict[str, TelegrafMqttBinarySensor] | None = None,
    ) -> None:
        self._entry = entry
        self._metric_key = metric_key
        # The platform's key -> entity table, so the entity can unregister
        # itself when HA removes it for a reason the integration did not
        # initiate (a reload, the platform being torn down, a user action).
        self._platform_entities = platform_entities
        self._refresh_descriptor_attributes()

    @property
    def _manager(self) -> DeviceManager:
        # ``ConfigEntry.runtime_data`` is untyped from HA's side; the
        # integration guarantees a ``TelegrafMqttRuntimeData`` here.
        manager: DeviceManager = self._entry.runtime_data.manager
        return manager

    def _refresh_descriptor_attributes(self) -> None:
        state = self._manager.get_metric(self._metric_key)
        if state is None:
            return
        descriptor = state.descriptor
        self._attr_unique_id = f"{DOMAIN}_{state.device_id}_{descriptor.unique_key}"
        self._attr_translation_key = descriptor.translation_key
        self._attr_translation_placeholders = dict(descriptor.translation_placeholders)
        self._attr_entity_category = _entity_category(descriptor.entity_category)
        self._attr_entity_registry_enabled_default = descriptor.entity_category != ENTITY_CATEGORY_DIAGNOSTIC
        self._attr_icon = ICON_FOR_KEY.get(
            infer_icon_key(descriptor.measurement, descriptor.field),
            ICON_FOR_KEY["binary"],
        )
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, state.device_id)},
            name=state.device_name,
            manufacturer=self._entry.runtime_data.manufacturer,
            model=self._entry.runtime_data.model,
            sw_version=self._entry.runtime_data.sw_version,
        )

    async def async_will_remove_from_hass(self) -> None:
        """Unregister from the platform table when HA drops this entity.

        The counterpart to the platform's ``forget_metric`` handler, so a
        stale key can never survive in the table and silently block the
        metric's re-adoption.
        """
        if self._platform_entities is not None:
            self._platform_entities.pop(self._metric_key, None)

    @callback
    def handle_metric_updated(self, metric_key: str) -> None:
        """Write HA state when this metric changes.

        Called by the platform's single ``route_update`` listener rather
        than by a per-entity dispatcher subscription. Kept public and
        directly callable so a unit test can drive one entity's refresh
        without standing up the whole platform.
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
    def is_on(self) -> bool | None:
        """Return the current boolean metric state."""
        state = self._manager.get_metric(self._metric_key)
        if state is None:
            return None
        return state.value if isinstance(state.value, bool) else None

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
