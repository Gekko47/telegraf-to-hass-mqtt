"""Registry primitives for telegraf_mqtt."""

from __future__ import annotations

import dataclasses
import re
from collections.abc import Callable
from dataclasses import dataclass
from fnmatch import fnmatch
from time import monotonic
from typing import Any

from .models import MetricDescriptor

_DEVICE_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _slugify_device(value: str) -> str:
    """Return a deterministic, stable slug for a device identifier."""
    slug = _DEVICE_SLUG_RE.sub("_", value.lower()).strip("_")
    return slug or "unknown"


@dataclass
class MetricState:
    """Current state stored for a descriptor key."""

    raw_descriptor: MetricDescriptor
    descriptor: MetricDescriptor
    device_id: str
    device_name: str
    last_updated: float
    is_available: bool = True

    @property
    def value(self) -> Any:
        """Expose the current metric payload through the state wrapper."""
        return self.descriptor.value


class MetricRegistry:
    """Track metric descriptors, availability, and write-on-change behavior."""

    def __init__(
        self,
        expire_after: int = 120,
        *,
        clock: Callable[[], float] | None = None,
        exclude_patterns: tuple[str, ...] = (),
        field_overrides: dict[str, dict[str, Any]] | None = None,
        device_id: str = "default",
        device_name: str = "Telegraf MQTT",
        cleanup_delay: int = 30 * 24 * 60 * 60,
        delete_delay: int = 60 * 24 * 60 * 60,
    ) -> None:
        self._expire_after = expire_after
        self._clock = clock or monotonic
        self._exclude_patterns = exclude_patterns
        self._field_overrides = field_overrides or {}
        self._states: dict[str, MetricState] = {}
        self.device_id = device_id
        self.device_name = device_name
        self.last_any_metric = 0.0
        self._cleanup_delay = cleanup_delay
        self._delete_delay = delete_delay

    def apply_options(
        self,
        *,
        expire_after: int | None = None,
        exclude_patterns: tuple[str, ...] | None = None,
        field_overrides: dict[str, dict[str, Any]] | None = None,
        on_write: Callable[[str, bool, Any], None] | None = None,
    ) -> None:
        """Apply live configuration options without rebuilding the registry."""
        if expire_after is not None:
            self._expire_after = expire_after
        if exclude_patterns is not None:
            self._exclude_patterns = exclude_patterns
            for unique_key, state in self._states.items():
                if not self._matches_exclude(unique_key):
                    continue
                if state.is_available:
                    state.is_available = False
                    if on_write is not None:
                        on_write(unique_key, False, state.value)
        if field_overrides is not None:
            self._field_overrides = field_overrides
            for unique_key, state in self._states.items():
                descriptor = self._apply_overrides(state.raw_descriptor)
                if descriptor == state.descriptor:
                    continue
                state.descriptor = descriptor
                if on_write is not None:
                    on_write(unique_key, state.is_available, state.value)

    def get(self, unique_key: str) -> MetricState | None:
        """Return the current state for a key if one already exists."""
        return self._states.get(unique_key)

    def keys(self) -> tuple[str, ...]:
        """Return known metric keys."""
        return tuple(self._states)

    def _matches_exclude(self, unique_key: str) -> bool:
        return any(fnmatch(unique_key, pattern) for pattern in self._exclude_patterns)

    def _apply_overrides(self, descriptor: MetricDescriptor) -> MetricDescriptor:
        override = self._field_overrides.get(descriptor.field)
        if override is None:
            return descriptor

        return MetricDescriptor(
            unique_key=descriptor.unique_key,
            measurement=descriptor.measurement,
            tags=descriptor.tags,
            field=descriptor.field,
            value=descriptor.value,
            timestamp=descriptor.timestamp,
            name=descriptor.name,
            native_unit=override.get("native_unit", descriptor.native_unit),
            suggested_device_class=override.get("device_class", descriptor.suggested_device_class),
            suggested_state_class=override.get("state_class", descriptor.suggested_state_class),
            entity_category=override.get("entity_category", descriptor.entity_category),
            cleanup_policy=descriptor.cleanup_policy,
        )

    def update(
        self,
        descriptor: MetricDescriptor,
        *,
        on_write: Callable[[str, bool, Any], None] | None = None,
        on_discovered: Callable[[str], None] | None = None,
        metric_key: str | None = None,
    ) -> bool:
        """Store a descriptor and emit state updates only when value or availability changes."""
        raw_descriptor = descriptor
        descriptor = self._apply_overrides(raw_descriptor)
        if self._matches_exclude(raw_descriptor.unique_key):
            return False

        current = self._states.get(raw_descriptor.unique_key)
        current_time = self._clock()

        if current is None:
            self._states[raw_descriptor.unique_key] = MetricState(
                raw_descriptor=raw_descriptor,
                descriptor=descriptor,
                device_id=self.device_id,
                device_name=self.device_name,
                last_updated=current_time,
                is_available=True,
            )
            if on_discovered is not None:
                on_discovered(metric_key or descriptor.unique_key)
            if on_write is not None:
                on_write(metric_key or descriptor.unique_key, True, descriptor.value)
            return True

        prior_available = current.is_available
        changed = current.value != descriptor.value or prior_available is False
        if changed:
            self._states[descriptor.unique_key] = MetricState(
                raw_descriptor=raw_descriptor,
                descriptor=descriptor,
                device_id=self.device_id,
                device_name=self.device_name,
                last_updated=current_time,
                is_available=True,
            )
            if on_write is not None:
                on_write(metric_key or descriptor.unique_key, True, descriptor.value)
            return True

        current.raw_descriptor = raw_descriptor
        current.descriptor = descriptor
        current.last_updated = current_time
        current.device_id = self.device_id
        current.device_name = self.device_name
        return False

    def check_expiry(self, *, on_write: Callable[[str, bool, Any], None] | None = None) -> None:
        """Mark a metric unavailable when it has not been refreshed within expire_after seconds."""
        now = self._clock()
        for unique_key, state in list(self._states.items()):
            if now - state.last_updated <= self._expire_after:
                continue

            if state.is_available:
                state.is_available = False
                if on_write is not None:
                    on_write(unique_key, False, state.descriptor.value)

    def cleanup(self, *, on_write: Callable[[str, bool, Any], None] | None = None) -> list[str]:
        """Remove metrics that exceeded the cleanup delay while never removing NEVER-policy metrics."""
        now = self._clock()
        removed: list[str] = []
        for unique_key, state in list(self._states.items()):
            if state.descriptor.cleanup_policy == "NEVER":
                continue
            if state.descriptor.cleanup_policy == "ALWAYS":
                removed.append(unique_key)
                self._states.pop(unique_key, None)
                continue
            if now - state.last_updated > self._expire_after + self._cleanup_delay:
                removed.append(unique_key)
                self._states.pop(unique_key, None)
                if on_write is not None:
                    on_write(unique_key, False, state.descriptor.value)
        return removed

    def __len__(self) -> int:
        return len(self._states)

    def __iter__(self) -> Any:
        return iter(self.keys())


class DeviceManager:
    """Own one registry per discovered device and expose a single multi-device lookup surface."""

    def __init__(
        self,
        expire_after: int = 120,
        *,
        clock: Callable[[], float] | None = None,
        exclude_patterns: tuple[str, ...] = (),
        field_overrides: dict[str, dict[str, Any]] | None = None,
        device_id: str = "default",
        device_name: str = "Telegraf MQTT",
        cleanup_delay: int = 30 * 24 * 60 * 60,
        delete_delay: int = 60 * 24 * 60 * 60,
        parser: Any | None = None,
    ) -> None:
        self._clock = clock or monotonic
        self._expire_after = expire_after
        self._exclude_patterns = exclude_patterns
        self._field_overrides = field_overrides or {}
        self._cleanup_delay = cleanup_delay
        self._delete_delay = delete_delay
        self._default_device_id = device_id
        self._default_device_name = device_name
        self.devices: dict[str, MetricRegistry] = {}
        self._parser = parser
        self._on_write: Callable[[str, bool, Any], None] | None = None
        self._on_discovered: Callable[[str], None] | None = None
        self._on_new_device: Callable[[str, str], None] | None = None

    def set_parser(self, parser: Any) -> None:
        """Attach the payload parser used for every subsequent message."""
        self._parser = parser

    def set_callbacks(
        self,
        *,
        on_write: Callable[[str, bool, Any], None] | None = None,
        on_discovered: Callable[[str], None] | None = None,
        on_new_device: Callable[[str, str], None] | None = None,
    ) -> None:
        """Wire the persistent pipeline callbacks (MQTT transport → dispatcher signals).

        Called once by ``async_setup_entry`` so ``process_message(topic, payload)``
        works standalone; per-call arguments still take precedence.
        """
        if on_write is not None:
            self._on_write = on_write
        if on_discovered is not None:
            self._on_discovered = on_discovered
        if on_new_device is not None:
            self._on_new_device = on_new_device

    def get(self, metric_key: str) -> MetricState | None:
        """Resolve a metric key across all known device registries."""
        for registry in self.devices.values():
            state = registry.get(metric_key)
            if state is not None:
                return state
        return None

    def keys(self) -> tuple[str, ...]:
        """Return a stable composite key for each known metric across all devices."""
        keys: list[str] = []
        for device_id, registry in self.devices.items():
            for unique_key in registry:
                keys.append(f"{device_id}:{unique_key}")
        return tuple(keys)

    def get_metric(self, metric_key: str) -> MetricState | None:
        """Return the device-specific metric by composite key."""
        if ":" not in metric_key:
            return self.get(metric_key)
        device_id, unique_key = metric_key.split(":", 1)
        registry = self.devices.get(device_id)
        if registry is None:
            return None
        return registry.get(unique_key)

    def get_or_create_registry(self, device_id: str, device_name: str) -> MetricRegistry:
        """Lazily create a registry for a new device ID."""
        registry = self.devices.get(device_id)
        if registry is None:
            registry = MetricRegistry(
                expire_after=self._expire_after,
                clock=self._clock,
                exclude_patterns=self._exclude_patterns,
                field_overrides=self._field_overrides,
                device_id=device_id,
                device_name=device_name,
                cleanup_delay=self._cleanup_delay,
                delete_delay=self._delete_delay,
            )
            self.devices[device_id] = registry
        registry.device_name = device_name
        return registry

    def apply_options(
        self,
        *,
        expire_after: int | None = None,
        exclude_patterns: tuple[str, ...] | None = None,
        field_overrides: dict[str, dict[str, Any]] | None = None,
        on_write: Callable[[str, bool, Any], None] | None = None,
    ) -> None:
        """Apply live options to every per-device registry."""
        if expire_after is not None:
            self._expire_after = expire_after
        if exclude_patterns is not None:
            self._exclude_patterns = exclude_patterns
        if field_overrides is not None:
            self._field_overrides = field_overrides
        for registry in self.devices.values():
            registry.apply_options(
                expire_after=expire_after,
                exclude_patterns=exclude_patterns,
                field_overrides=field_overrides,
                on_write=on_write,
            )

    def process_message(
        self,
        topic: str,
        payload: str | bytes,
        *,
        on_write: Callable[[str, bool, Any], None] | None = None,
        on_discovered: Callable[[str], None] | None = None,
        on_new_device: Callable[[str, str], None] | None = None,
        parser: Any | None = None,
    ) -> None:
        """Parse a message and route each descriptor to its own device's registry.

        A single MQTT message always carries one host's measurement in practice,
        but routing is per-descriptor so a mixed payload can never cross devices.
        """
        parser = parser if parser is not None else self._parser
        if parser is None:
            raise ValueError("parser is required")
        on_write = on_write if on_write is not None else self._on_write
        on_discovered = on_discovered if on_discovered is not None else self._on_discovered
        on_new_device = on_new_device if on_new_device is not None else self._on_new_device

        descriptors = parser.parse(payload)
        groups: dict[str, list[MetricDescriptor]] = {}
        names: dict[str, str] = {}
        for descriptor in descriptors:
            device_id = self._derive_device_id(topic, descriptor)
            device_name = self._derive_device_name(topic, descriptor)
            resolved = dataclasses.replace(descriptor, device_id=device_id)
            groups.setdefault(device_id, []).append(resolved)
            names.setdefault(device_id, device_name)

        for device_id, group in groups.items():
            is_new = device_id not in self.devices
            registry = self.get_or_create_registry(device_id, names[device_id])
            registry.last_any_metric = self._clock()
            for descriptor in group:
                metric_key = f"{device_id}:{descriptor.unique_key}"
                registry.update(
                    descriptor,
                    metric_key=metric_key,
                    on_write=lambda key, available, value: self._notify_callback(key, available, value, on_write),
                    on_discovered=lambda key: self._notify_discovered_callback(key, on_discovered),
                )
            if is_new and on_new_device is not None:
                on_new_device(device_id, names[device_id])

    def _derive_device_id(self, topic: str, descriptor: MetricDescriptor) -> str:
        """Derive a stable device id: host tag first, topic root fallback."""
        host = descriptor.device_id or descriptor.tags.get("host") or ""
        if isinstance(host, str) and host:
            return _slugify_device(host)
        topic_root = topic.strip("/").split("/", 1)[0] if topic else ""
        return _slugify_device(topic_root) if topic_root else self._default_device_id

    def _derive_device_name(self, topic: str, descriptor: MetricDescriptor) -> str:
        """Derive a display name alongside the device id (same priority order)."""
        host = descriptor.device_id or descriptor.tags.get("host") or ""
        if isinstance(host, str) and host:
            return host
        topic_root = topic.strip("/").split("/", 1)[0] if topic else ""
        return topic_root or self._default_device_name

    def _notify_callback(
        self,
        metric_key: str,
        available: bool,
        value: Any,
        callback: Callable[[str, bool, Any], None] | None,
    ) -> None:
        if callback is not None:
            callback(metric_key, available, value)

    def _notify_discovered_callback(self, metric_key: str, callback: Callable[[str], None] | None) -> None:
        if callback is not None:
            callback(metric_key)

    def check_expiry(self, *, on_write: Callable[[str, bool, Any], None] | None = None) -> None:
        """Run expiry checks across every device registry."""
        for registry in self.devices.values():
            registry.check_expiry(
                on_write=lambda key, available, value: self._notify_callback(key, available, value, on_write)
            )

    def cleanup(self, *, on_write: Callable[[str, bool, Any], None] | None = None) -> list[str]:
        """Run cleanup across every ACTIVE device registry, returning composite keys.

        Offline devices (no heartbeat within ``expire_after``) are skipped
        entirely — their entities are never cleaned up.
        """
        removed: list[str] = []
        now = self._clock()
        for device_id, registry in self.devices.items():
            if now - registry.last_any_metric > self._expire_after:
                continue
            for unique_key in registry.cleanup(
                on_write=lambda key, available, value: self._notify_callback(key, available, value, on_write)
            ):
                removed.append(f"{device_id}:{unique_key}")
        return removed

    def __len__(self) -> int:
        return sum(len(registry) for registry in self.devices.values())

    def __iter__(self) -> Any:
        return iter(self.keys())
