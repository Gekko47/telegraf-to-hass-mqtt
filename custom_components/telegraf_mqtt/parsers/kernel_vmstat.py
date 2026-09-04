"""Kernel VMStat measurement parser entrypoint (Phase 11)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..models import MetricDescriptor
from .generic import parse_generic_payload


def parse_kernel_vmstat_payload(payload: Mapping[str, Any]) -> list[MetricDescriptor]:
    """Parse ``kernel_vmstat`` payloads (the modern replacement for
    ``kernel`` on Telegraf >= 1.0). Same field shape: a hundred
    ``nr_*`` / ``pg*`` / ``numa_*`` page-counter fields handled by the
    generic parser's heuristic layer.
    """
    return parse_generic_payload(payload)
