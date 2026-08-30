"""Network measurement parser entrypoint."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..models import MetricDescriptor
from .generic import parse_generic_payload


def parse_net_payload(payload: Mapping[str, Any]) -> list[MetricDescriptor]:
    """Parse network payloads using the generic descriptor path."""
    return parse_generic_payload(payload)
