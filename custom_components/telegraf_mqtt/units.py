"""Unit and precision rules for telegraf_mqtt (Phase 11).

Telegraf publishes raw numeric values -- the integration's contract is "bytes
stay bytes; native_unit set; no pre-conversion" (Phase 4). But the user-
facing display needs human-friendly formatting on top of the raw value:

* a memory field arriving as ``B`` should render as ``"7.38 GB"``, not
  ``"7,932,480,512 B"``;
* a CPU utilisation arriving as ``%`` should render as ``"95 %"`` for whole
  numbers and ``"3.7 %"`` otherwise;
* a load average of ``3.72`` should render as ``"3.72"``, not as
  ``"3.720000057220459"``;
* a ping of ``9.5`` ms should round to one decimal, while a ping of ``123``
  ms should round to zero.

This module owns that formatting. It deliberately produces *display
strings*, not converted numeric values, so the raw byte / percent /
duration data still reaches Home Assistant's recorder and statistics
graphs unchanged. The platform layer calls :func:`format_value` for the
user-facing ``state`` representation; ``native_value`` keeps the raw number.

No Home Assistant imports live here -- the module is importable under the
same harness-free test scaffolding as ``parsers/generic.py`` so the
unit-allowlist tests in ``tests/test_phase4_units_statistics.py`` can
exercise every branch without a running HA instance.
"""

from __future__ import annotations

import math
from typing import Final

# Precision rules. The number is the number of decimal places.
_PERCENTAGE_PRECISION_WHOLE: Final = 0
_PERCENTAGE_PRECISION_FRACTIONAL: Final = 1
_LOAD_PRECISION: Final = 2
_TEMPERATURE_PRECISION: Final = 1
_VOLTAGE_PRECISION: Final = 2
_POWER_PRECISION_LARGE: Final = 0
_POWER_PRECISION_SMALL: Final = 1
_POWER_THRESHOLD: Final = 100.0
_LATENCY_PRECISION_LARGE: Final = 0
_LATENCY_PRECISION_SMALL: Final = 1
_LATENCY_THRESHOLD: Final = 10.0
_FREQUENCY_PRECISION: Final = 0
_GENERIC_PRECISION: Final = 3


def format_precision(value: float, native_unit: str | None) -> float:
    """Return ``value`` rounded to a sensible precision for its unit.

    Used by the sensor platform to display the ``state`` rounded to the
    right number of decimal places without mutating ``native_value`` (the
    raw number that Home Assistant's recorder consumes).
    """
    if native_unit == "%":
        if math.isclose(value, round(value)):
            return float(round(value))
        return round(value, _PERCENTAGE_PRECISION_FRACTIONAL)
    if native_unit == "°C":
        return round(value, _TEMPERATURE_PRECISION)
    if native_unit == "V":
        return round(value, _VOLTAGE_PRECISION)
    if native_unit == "W":
        if abs(value) >= _POWER_THRESHOLD:
            return round(value, _POWER_PRECISION_LARGE)
        return round(value, _POWER_PRECISION_SMALL)
    if native_unit == "ms":
        if abs(value) >= _LATENCY_THRESHOLD:
            return round(value, _LATENCY_PRECISION_LARGE)
        return round(value, _LATENCY_PRECISION_SMALL)
    if native_unit == "MHz" or native_unit == "GHz":
        return round(value, _FREQUENCY_PRECISION)
    return value


def format_value(value: float, native_unit: str | None) -> str:
    """Format a numeric value for display.

    The raw value is *not* converted: bytes stay as bytes, durations stay
    as seconds, percentages stay as fractions of 100. Home Assistant's
    recorder / unit-conversion layer renders the right suffix on the UI
    side. This function only controls the number of decimal places and
    the gap between value and unit.
    """
    if native_unit is None:
        rounded = format_precision(value, None)
        if isinstance(rounded, float) and rounded.is_integer():
            return str(int(rounded))
        return str(rounded)
    rounded = format_precision(value, native_unit)
    if isinstance(rounded, float) and rounded.is_integer():
        return f"{int(rounded)} {native_unit}"
    return f"{rounded} {native_unit}"


__all__ = ["format_precision", "format_value"]
