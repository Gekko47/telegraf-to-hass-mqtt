"""Ping measurement parser entrypoint (Phase 11)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..models import MetricDescriptor
from .generic import parse_generic_payload


def parse_ping_payload(payload: Mapping[str, Any]) -> list[MetricDescriptor]:
    """Parse ``ping`` payloads using the generic descriptor path.

    The ``*_response_ms`` fields are millisecond durations, the
    ``packets_*`` / ``ttl`` fields are dimensionless gauges, and
    ``percent_packet_loss`` is a percent gauge. All are inferred by
    the generic parser from the field name; the per-measurement
    handler is a pure delegate.
    """
    return parse_generic_payload(payload)
