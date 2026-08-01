"""Sensors measurement parser entrypoint."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .generic import parse_generic_payload


def parse_sensors_payload(payload: Mapping[str, Any]) -> list:
    """Parse sensors payloads using the generic descriptor path."""
    return parse_generic_payload(payload)
