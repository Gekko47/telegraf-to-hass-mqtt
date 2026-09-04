"""Wireless measurement parser entrypoint (Phase 11)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..models import MetricDescriptor
from .generic import parse_generic_payload


def parse_wireless_payload(payload: Mapping[str, Any]) -> list[MetricDescriptor]:
    """Parse ``wireless`` payloads using the generic descriptor path.

    Field shape: ``level`` / ``noise`` are signal-strength (dBm),
    ``link`` / ``link_quality`` is a percent, the ``*_beacon`` /
    ``crypt`` / ``frag`` / ``retry`` / ``misc`` / ``nwid`` fields
    are packet counters (``total_increasing``).
    """
    return parse_generic_payload(payload)
