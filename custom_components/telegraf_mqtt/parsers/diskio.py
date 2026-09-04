"""DiskIO measurement parser entrypoint (Phase 11).

Telegraf's ``diskio`` input plugin emits per-device counters tagged
with the device name (``name=sda`` / ``name=nvme0n1``). The fields
break into four shape buckets the generic parser handles correctly:

* byte counters (``read_bytes`` / ``write_bytes``) -- ``B`` /
  ``data_size`` / ``total_increasing``;
* millisecond durations (``read_time`` / ``write_time`` / ``io_time``
  / ``weighted_io_time``) -- ``ms`` / ``duration``;
* in-flight / queue-depth gauges (``iops_in_progress``) -- dimensionless;
* percent gauges (``io_util``) -- ``%``; ``io_util`` is documented
  on a 0--1 scale in the upstream plugin, so a user who wants 0--100
  is expected to add a field override (the README documents the quirk).
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..models import MetricDescriptor
from .generic import parse_generic_payload


def parse_diskio_payload(payload: Mapping[str, Any]) -> list[MetricDescriptor]:
    """Parse ``diskio`` payloads using the generic descriptor path."""
    return parse_generic_payload(payload)
