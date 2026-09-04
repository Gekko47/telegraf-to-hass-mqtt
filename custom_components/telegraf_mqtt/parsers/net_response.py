"""net_response measurement parser entrypoint (Phase 11)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..models import MetricDescriptor
from .generic import parse_generic_payload


def parse_net_response_payload(payload: Mapping[str, Any]) -> list[MetricDescriptor]:
    """Parse ``net_response`` payloads using the generic descriptor path.

    Field shape: ``response_time`` is seconds (a duration), ``result_code``
    is an int (0 = success). A user who wants a binary_sensor for
    "service reachable" is expected to add a field override
    (``field_overrides["result_code"]["platform"] = "binary_sensor"``)
    because the upstream plugin does not emit a synthetic bool field.
    """
    return parse_generic_payload(payload)
