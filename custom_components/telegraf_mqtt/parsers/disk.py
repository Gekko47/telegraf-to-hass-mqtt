"""Disk measurement parser entrypoint."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .generic import parse_generic_payload


def parse_disk_payload(payload: Mapping[str, Any]) -> list:
    """Parse disk payloads using the generic descriptor path."""
    return parse_generic_payload(payload)
