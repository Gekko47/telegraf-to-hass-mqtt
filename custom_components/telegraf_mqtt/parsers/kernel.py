"""Kernel measurement parser entrypoint (Phase 11)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..models import MetricDescriptor
from .generic import parse_generic_payload


def parse_kernel_payload(payload: Mapping[str, Any]) -> list[MetricDescriptor]:
    """Parse ``kernel`` / ``kernel_vmstat`` payloads using the generic path."""
    return parse_generic_payload(payload)
