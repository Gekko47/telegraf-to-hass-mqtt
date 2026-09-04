"""S.M.A.R.T. measurement parser entrypoint (Phase 11)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..models import MetricDescriptor
from .generic import parse_generic_payload


def parse_smart_payload(payload: Mapping[str, Any]) -> list[MetricDescriptor]:
    """Parse ``smart_device`` / ``smart_attribute`` payloads using the
    generic descriptor path. Field shape is mixed: ``temp_c`` is
    degrees Celsius, ``power_on_hours`` is hours, counters like
    ``reallocated_sector_count`` / ``current_pending_sector`` /
    ``offline_uncorrectable`` are dimensionless, and ``health_ok`` is
    a bool that lands on the binary_sensor platform.
    """
    return parse_generic_payload(payload)
