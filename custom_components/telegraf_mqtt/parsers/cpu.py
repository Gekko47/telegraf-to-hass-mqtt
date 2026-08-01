"""CPU measurement parser entrypoint."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .generic import parse_generic_payload


def parse_cpu_payload(payload: Mapping[str, Any]) -> list:
    """Parse CPU payloads using the generic descriptor path."""
    return parse_generic_payload(payload)
