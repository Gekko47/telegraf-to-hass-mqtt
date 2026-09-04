"""Processes measurement parser entrypoint (Phase 11)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..models import MetricDescriptor
from .generic import parse_generic_payload


def parse_processes_payload(payload: Mapping[str, Any]) -> list[MetricDescriptor]:
    """Parse ``processes`` payloads using the generic descriptor path.

    Every field the ``processes`` plugin emits is a current-state count
    (``total``, ``running``, ``blocked``, ``sleeping``, ``stopped``,
    ``zombie``, ``dead``, ``paging``, ``parked``, ``idle``, ``wait``,
    ``total_threads``). They are dimensionless gauges, which is exactly
    the generic parser's default state class.
    """
    return parse_generic_payload(payload)
