"""Shared data contracts for telegraf_mqtt."""

from __future__ import annotations

from dataclasses import dataclass
from types import MappingProxyType
from typing import Mapping, TypeAlias

MetricValue: TypeAlias = int | float | str | bool


@dataclass(frozen=True)
class MetricDescriptor:
    """Immutable descriptor for a single Telegraf metric field."""

    unique_key: str
    measurement: str
    tags: Mapping[str, str]
    field: str
    value: MetricValue
    timestamp: float
    name: str
    native_unit: str | None
    suggested_device_class: str | None
    suggested_state_class: str | None
    entity_category: str | None


def frozen_tags(tags: Mapping[str, str]) -> Mapping[str, str]:
    """Return an immutable copy of a tag mapping."""
    return MappingProxyType(dict(tags))
