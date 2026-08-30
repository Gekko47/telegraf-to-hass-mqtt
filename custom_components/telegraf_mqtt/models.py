"""Shared data contracts for telegraf_mqtt."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Literal, TypeGuard

from .const import (
    CLEANUP_POLICY_AUTO,
    PLATFORM_HINT_AUTO,
)

type MetricValue = int | float | str | bool
"""The set of primitive value types Telegraf can publish per field."""

CleanupPolicy = Literal["AUTO", "NEVER", "ALWAYS"]
"""Lifecycle hint carried on a descriptor; see ``parsers/static.py``."""

PlatformHint = Literal["auto", "sensor", "binary_sensor", "none"]
"""User-controlled routing hint from a field override."""


def is_bool_metric(value: object) -> TypeGuard[bool]:
    """Narrow a value to ``bool`` for the platform-routing split.

    ``bool`` must be tested before ``int`` because ``bool`` is a subclass
    of ``int`` in Python. Used by the sensor/binary_sensor platforms to
    let strict type-checkers prove the platform split.
    """
    return isinstance(value, bool)


def is_numeric_metric(value: object) -> TypeGuard[int | float]:
    """Narrow a value to ``int | float`` (excluding ``bool``)."""
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def is_string_metric(value: object) -> TypeGuard[str]:
    """Narrow a value to ``str``."""
    return isinstance(value, str)


def coerce_to_bool(value: MetricValue) -> bool:
    """Coerce a numeric-or-bool Telegraf value to a bool for binary_sensor.

    Convention: 0 / 0.0 / "" -> False; everything else -> True. Used when
    a user has set a ``field_overrides[platform] = "binary_sensor"`` hint
    on a field that arrives as an int (0/1) rather than a JSON bool.
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return value != 0
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "on"}
    return False


@dataclass(frozen=True)
class MetricDescriptor:
    """Immutable descriptor for a single Telegraf metric field.

    Phase 9: the resolved display ``name`` is gone. Entity-facing display
    comes from ``translation_key`` + ``translation_placeholders``; the
    *only* way to get a user-visible string from a descriptor is to
    format the translation key with the placeholders. See ``naming.py``.

    Phase 10: ``cleanup_policy`` is a ``Literal`` so strict mypy can
    exhaustively check the comparisons in ``registry.py``. The field
    belongs on the descriptor by design: it is set at parse time
    (``parsers/static.py`` for static metadata, the parser default
    otherwise) and never mutated afterwards. See the corrected comment
    in ``.cline/skills/architecture.md``.

    Phase 10: ``platform_hint`` lets a user override the platform a field
    lands on. Default ``"auto"`` preserves the pre-10 behaviour
    (decide from the value's Python type). See ``parsers/generic.py``.
    """

    unique_key: str
    measurement: str
    tags: Mapping[str, str]
    field: str
    value: MetricValue
    timestamp: float
    native_unit: str | None
    suggested_device_class: str | None
    suggested_state_class: str | None
    entity_category: str | None
    cleanup_policy: CleanupPolicy = CLEANUP_POLICY_AUTO
    device_id: str = ""
    # Phase 9: translations-only display.
    translation_key: str = "generic_field"
    translation_placeholders: Mapping[str, str] = MappingProxyType({})
    # Phase 10: platform routing hint from a field_overrides entry.
    platform_hint: PlatformHint = PLATFORM_HINT_AUTO


def frozen_tags(tags: Mapping[str, str]) -> Mapping[str, str]:
    """Return an immutable copy of a tag mapping."""
    return MappingProxyType(dict(tags))
