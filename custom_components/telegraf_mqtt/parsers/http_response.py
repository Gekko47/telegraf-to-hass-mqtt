"""http_response measurement parser entrypoint (Phase 11)."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..models import MetricDescriptor
from .generic import parse_generic_payload


def parse_http_response_payload(payload: Mapping[str, Any]) -> list[MetricDescriptor]:
    """Parse ``http_response`` payloads using the generic descriptor path.

    Field shape: ``response_time`` is seconds, ``http_response_code`` /
    ``content_length`` are ints, ``result_code`` is the success code
    (0 = success, 6 = status mismatch). Same pattern as
    ``net_response`` -- a user who wants a binary_sensor "endpoint
    reachable" is expected to add a field override on ``result_code``.
    """
    return parse_generic_payload(payload)
