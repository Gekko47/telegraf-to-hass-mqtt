"""System measurement parser entrypoint (Phase 11)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..models import MetricDescriptor
from .generic import parse_generic_payload


def parse_system_payload(payload: Mapping[str, Any]) -> list[MetricDescriptor]:
    """Parse ``system`` payloads using the generic descriptor path.

    Telegraf's ``system`` input plugin emits load averages, uptime,
    user / CPU counts, and (with the ``os`` / ``dmi`` includes) BIOS /
    board / chassis identity. Every one of those is handled correctly
    by the generic parser's heuristic layer (bytes / duration / count
    / percent / string), so the per-measurement handler is a delegate
    to keep the dispatcher table open and explicit.
    """
    return parse_generic_payload(payload)
