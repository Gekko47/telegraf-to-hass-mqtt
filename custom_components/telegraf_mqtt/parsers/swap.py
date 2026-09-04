"""Swap measurement parser entrypoint (Phase 11)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..models import MetricDescriptor
from .generic import parse_generic_payload


def parse_swap_payload(payload: Mapping[str, Any]) -> list[MetricDescriptor]:
    """Parse ``swap`` payloads using the generic descriptor path.

    Same shape as ``mem``: ``free`` / ``used`` / ``total`` are byte
    snapshots (``B`` / ``data_size``), ``used_percent`` is a percent
    gauge, and ``in`` / ``out`` are cumulative byte counters since
    boot (``total_increasing``). All are inferred from the field name
    by ``generic.infer_*`` so the per-measurement handler is a pure
    delegate.
    """
    return parse_generic_payload(payload)
