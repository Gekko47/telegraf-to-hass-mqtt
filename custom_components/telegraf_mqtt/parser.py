"""Telegraf JSON parser dispatch."""

from __future__ import annotations

import json
import logging

from .models import MetricDescriptor
from .parsers.generic import parse_generic_payload

_LOGGER = logging.getLogger(__name__)


class TelegrafParser:
    """Parse raw Telegraf MQTT JSON payloads into metric descriptors."""

    def parse(self, payload: str | bytes) -> list[MetricDescriptor]:
        """Parse a raw MQTT payload."""
        try:
            decoded = json.loads(payload)
        except (TypeError, UnicodeDecodeError, json.JSONDecodeError):
            _LOGGER.debug("Invalid Telegraf JSON payload")
            return []

        if not isinstance(decoded, dict):
            _LOGGER.debug("Unsupported Telegraf payload shape")
            return []

        measurement = decoded.get("name")
        if isinstance(measurement, str):
            _LOGGER.debug("Unknown measurement %s; using generic parser", measurement)

        return parse_generic_payload(decoded)
