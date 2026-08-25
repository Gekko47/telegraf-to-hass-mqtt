"""Telegraf JSON parser dispatch."""

from __future__ import annotations

import json
import logging
from typing import ClassVar

from .models import MetricDescriptor
from .parsers import (
    parse_battery_payload,
    parse_cpu_payload,
    parse_disk_payload,
    parse_generic_payload,
    parse_mem_payload,
    parse_net_payload,
    parse_nvidia_gpu_payload,
    parse_sensors_payload,
)

_LOGGER = logging.getLogger(__name__)


class TelegrafParser:
    """Parse raw Telegraf MQTT JSON payloads into metric descriptors."""

    _PARSERS: ClassVar[dict] = {
        "battery": parse_battery_payload,
        "cpu": parse_cpu_payload,
        "disk": parse_disk_payload,
        "mem": parse_mem_payload,
        "net": parse_net_payload,
        "nvidia_gpu": parse_nvidia_gpu_payload,
        "sensors": parse_sensors_payload,
    }

    def parse(self, payload: str | bytes) -> list[MetricDescriptor]:
        """Parse a raw MQTT payload."""
        try:
            decoded = json.loads(payload)
        except TypeError, UnicodeDecodeError, json.JSONDecodeError:
            _LOGGER.debug("Invalid Telegraf JSON payload")
            return []

        if not isinstance(decoded, dict):
            _LOGGER.debug("Unsupported Telegraf payload shape")
            return []

        measurement = decoded.get("name")
        if not isinstance(measurement, str):
            _LOGGER.debug("Unsupported Telegraf payload shape")
            return []

        handler = self._PARSERS.get(measurement, parse_generic_payload)
        return handler(decoded)
