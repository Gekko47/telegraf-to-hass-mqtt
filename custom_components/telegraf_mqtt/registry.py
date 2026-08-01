"""Registry primitives for telegraf_mqtt."""

from __future__ import annotations

from dataclasses import dataclass
from fnmatch import fnmatch
from time import monotonic
from typing import Any, Callable

from .models import MetricDescriptor


@dataclass
class MetricState:
    """Current state stored for a descriptor key."""

    raw_descriptor: MetricDescriptor
    descriptor: MetricDescriptor
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
    ) -> None:
        self._expire_after = expire_after
        self._clock = clock or monotonic
        self._exclude_patterns = exclude_patterns
        self._field_overrides = field_overrides or {}
        self._states: dict[str, MetricState] = {}

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
        )

    def update(
        self,
        descriptor: MetricDescriptor,
        *,
        on_write: Callable[[str, bool, Any], None] | None = None,
        on_discovered: Callable[[str], None] | None = None,
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
                last_updated=current_time,
                is_available=True,
            )
            if on_discovered is not None:
                on_discovered(descriptor.unique_key)
            if on_write is not None:
                on_write(descriptor.unique_key, True, descriptor.value)
            return True

        prior_available = current.is_available
        changed = current.value != descriptor.value or prior_available is False
        if changed:
            self._states[descriptor.unique_key] = MetricState(
                raw_descriptor=raw_descriptor,
                descriptor=descriptor,
                last_updated=current_time,
                is_available=True,
            )
            if on_write is not None:
                on_write(descriptor.unique_key, True, descriptor.value)
            return True

        current.raw_descriptor = raw_descriptor
        current.descriptor = descriptor
        current.last_updated = current_time
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

    def __len__(self) -> int:
        return len(self._states)
