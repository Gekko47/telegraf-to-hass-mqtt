"""Telegraf JSON parser dispatch."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import Any, ClassVar

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
from .parsers.base import PayloadParser

_LOGGER = logging.getLogger(__name__)


@dataclass
class ParserStats:
    """Aggregate counters + last-message snapshot exposed via diagnostics.

    Phase 7: SPEC.md says ``diagnostics.py`` must expose "parser
    statistics, last-message metadata (redacted appropriately),
    dropped-payload counts". The ``ParserStats`` object is the single
    source of truth for all of that; the diagnostics layer reads it
    directly.

    ``last_message`` is a single-slot ring buffer (only the most recent
    payload is kept) that captures the metadata a user would need to
    debug a stuck integration: when did the last message arrive, which
    topic, how large was the raw payload, and did the parser drop it
    for one of the known reasons. The raw payload is **never** stored --
    a battery percentage, MAC address, or any other field is the
    integration's data, not the user's debugging context.
    """

    received: int = 0
    parsed: int = 0
    dropped_invalid_json: int = 0
    dropped_unsupported_shape: int = 0
    dropped_parser_error: int = 0
    unknown_measurement_fallbacks: int = 0
    last_message: dict[str, Any] | None = None

    def note_received(self, topic: str, byte_length: int) -> None:
        self.received += 1
        self.last_message = {
            "topic": topic,
            "byte_length": byte_length,
            "dropped_reason": None,
            "measurement": None,
        }

    def note_dropped(self, topic: str, byte_length: int, reason: str) -> None:
        self.received += 1
        if reason == "invalid_json":
            self.dropped_invalid_json += 1
        elif reason == "unsupported_shape":
            self.dropped_unsupported_shape += 1
        self.last_message = {
            "topic": topic,
            "byte_length": byte_length,
            "dropped_reason": reason,
            "measurement": None,
        }

    def note_parser_error(self, topic: str, byte_length: int, measurement: str) -> None:
        """Record a handler-raised fault.

        ``handler(decoded)`` is wrapped in a narrow ``try/except`` so a
        single bad assumption inside a future per-measurement handler
        (``KeyError``, ``TypeError``, ``AttributeError``, ``ValueError``)
        cannot take down the MQTT subscription. This counter gives the
        user a way to see *that* it happened, and ``last_message`` keeps
        the topic/byte_length/measurement for diagnostics download.
        """
        self.received += 1
        self.dropped_parser_error += 1
        self.last_message = {
            "topic": topic,
            "byte_length": byte_length,
            "dropped_reason": "parser_error",
            "measurement": measurement,
        }

    def note_parsed(
        self,
        topic: str,
        byte_length: int,
        measurement: str,
        *,
        fell_back: bool,
    ) -> None:
        self.received += 1
        if fell_back:
            self.unknown_measurement_fallbacks += 1
        self.parsed += 1
        self.last_message = {
            "topic": topic,
            "byte_length": byte_length,
            "dropped_reason": None,
            "measurement": measurement,
        }


class TelegrafParser:
    """Parse raw Telegraf MQTT JSON payloads into metric descriptors.

    Optionally takes a ``ParserStats`` instance that records every
    parse outcome for the diagnostics layer. If no stats are provided,
    a private one is created so the surface is always uniform and
    ``TelegrafParser.stats`` is always safe to read.
    """

    _PARSERS: ClassVar[dict[str, PayloadParser]] = {
        "battery": parse_battery_payload,
        "cpu": parse_cpu_payload,
        "disk": parse_disk_payload,
        "mem": parse_mem_payload,
        "net": parse_net_payload,
        "nvidia_gpu": parse_nvidia_gpu_payload,
        "sensors": parse_sensors_payload,
    }

    def __init__(self, stats: ParserStats | None = None) -> None:
        self.stats = stats if stats is not None else ParserStats()

    def parse(
        self,
        payload: str | bytes,
        *,
        topic: str = "<unknown>",
    ) -> list[MetricDescriptor]:
        """Parse a raw MQTT payload.

        ``topic`` is captured into the parser-stats last-message
        snapshot. It is *not* required for correctness; callers that
        don't have a topic (unit tests, e.g.) can omit it.
        """
        byte_length = len(payload) if isinstance(payload, (bytes, bytearray)) else len(str(payload))

        try:
            decoded = json.loads(payload)
        except TypeError, UnicodeDecodeError, json.JSONDecodeError:
            _LOGGER.debug("Invalid Telegraf JSON payload")
            self.stats.note_dropped(topic, byte_length, "invalid_json")
            return []

        if not isinstance(decoded, dict):
            _LOGGER.debug("Unsupported Telegraf payload shape")
            self.stats.note_dropped(topic, byte_length, "unsupported_shape")
            return []

        measurement = decoded.get("name")
        if not isinstance(measurement, str):
            _LOGGER.debug("Unsupported Telegraf payload shape")
            self.stats.note_dropped(topic, byte_length, "unsupported_shape")
            return []

        handler = self._PARSERS.get(measurement)
        fell_back = False
        if handler is None:
            # SPEC.md logging table: an unknown measurement falls back to the
            # generic parser quietly (DEBUG), never raising and never WARNING.
            _LOGGER.debug("Unknown Telegraf measurement %r; using generic parser", measurement)
            handler = parse_generic_payload
            fell_back = True
        try:
            descriptors = list(handler(decoded))
        except (KeyError, TypeError, AttributeError, ValueError) as handler_err:
            # Fault isolation around the per-measurement handler. The
            # JSON envelope has been validated and the measurement name
            # is a string, so any of these four exceptions means a bad
            # assumption inside the handler (e.g. ``fields["x"]`` on a
            # missing key, a nested-dict coerced to ``float``, a
            # ``Measurement.tags`` typo). We log at DEBUG, bump the
            # ``dropped_parser_error`` counter, and return ``[]`` so
            # the MQTT subscription keeps processing the next message.
            #
            # Exceptions outside this list (KeyboardInterrupt,
            # SystemExit, MemoryError, asyncio.CancelledError, ...)
            # propagate as designed -- the narrow catch is a guard rail
            # for *handler* bugs, not a blanket suppression.
            _LOGGER.debug(
                "Telegraf handler %r raised on topic %s: %s",
                measurement,
                topic,
                handler_err,
            )
            self.stats.note_parser_error(topic, byte_length, measurement)
            return []
        self.stats.note_parsed(topic, byte_length, measurement, fell_back=fell_back)
        return descriptors
