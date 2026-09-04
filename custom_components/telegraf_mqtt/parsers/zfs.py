"""ZFS measurement parser entrypoint (Phase 11).

The ``zfs`` plugin emits three measurements: ``zfs`` (arcstats /
kstat aggregates), ``zfs_pool`` (per-pool), ``zfs_dataset`` (per-dataset).
All are delegated to the generic parser; the per-measurement entry
points keep the dispatcher table open and explicit.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..models import MetricDescriptor
from .generic import parse_generic_payload


def parse_zfs_pool_payload(payload: Mapping[str, Any]) -> list[MetricDescriptor]:
    return parse_generic_payload(payload)


def parse_zfs_dataset_payload(payload: Mapping[str, Any]) -> list[MetricDescriptor]:
    return parse_generic_payload(payload)


def parse_zfs_payload(payload: Mapping[str, Any]) -> list[MetricDescriptor]:
    """Catch-all for the default ``zfs`` measurement (arcstats, etc.)."""
    return parse_generic_payload(payload)
