"""interrupts measurement parser entrypoint (Phase 11)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..models import MetricDescriptor
from .generic import parse_generic_payload


def parse_interrupts_payload(payload: Mapping[str, Any]) -> list[MetricDescriptor]:
    """Parse ``interrupts`` / ``soft_interrupts`` payloads.

    The single field ``count`` is a dimensionless counter. The
    per-measurement delegate keeps the dispatcher table open.
    """
    return parse_generic_payload(payload)
