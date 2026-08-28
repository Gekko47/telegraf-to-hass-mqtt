"""Shared data contracts for telegraf_mqtt."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType

type MetricValue = int | float | str | bool


@dataclass(frozen=True)
class MetricDescriptor:
    """Immutable descriptor for a single Telegraf metric field.

    Phase 9: the resolved display ``name`` is gone. Entity-facing display
    comes from ``translation_key`` + ``translation_placeholders``; the
    *only* way to get a user-visible string from a descriptor is to
    format the translation key with the placeholders. See ``naming.py``.
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
    cleanup_policy: str = "AUTO"
    device_id: str = ""
    # Phase 9: translations-only display.
    translation_key: str = "generic_field"
    translation_placeholders: Mapping[str, str] = MappingProxyType({})


def frozen_tags(tags: Mapping[str, str]) -> Mapping[str, str]:
    """Return an immutable copy of a tag mapping."""
    return MappingProxyType(dict(tags))
