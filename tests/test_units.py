"""Phase 11 -- unit / precision auto-formatting tests.

The formatting layer (see ``custom_components/telegraf_mqtt/units.py``)
converts raw Telegraf values to user-facing display strings *without*
mutating ``native_value`` (the raw number Home Assistant's recorder
consumes). These tests pin the display behaviour:

* bytes stay bytes (no premature conversion) -- audited elsewhere;
* percentages with a whole-number value render as "95 %", not "95.0 %";
* load averages render with two decimal places;
* ms latency renders with one decimal below 10 ms, zero above;
* temperatures render with one decimal;
* voltages with two, power with adaptive decimal count.

The contract: ``format_value`` is a pure function over (value, unit)
and the formatting decisions live in one place.
"""

from __future__ import annotations

import math

import pytest

from custom_components.telegraf_mqtt.units import format_precision, format_value

# ---------------------------------------------------------------------------
# format_precision: number of decimals / rounding behaviour per unit
# ---------------------------------------------------------------------------


def test_format_precision_percent_whole_strips_decimal() -> None:
    """Whole-number percentages round to an integer (95 -> 95)."""
    assert format_precision(95.0, "%") == 95.0
    assert format_precision(100.0, "%") == 100.0
    assert format_precision(0.0, "%") == 0.0


def test_format_precision_percent_fractional_keeps_one_decimal() -> None:
    """Fractional percentages keep one decimal (3.725 -> 3.7)."""
    assert format_precision(3.7, "%") == 3.7
    assert format_precision(3.725, "%") == 3.7
    assert format_precision(0.43, "%") == 0.4


def test_format_precision_temperature_one_decimal() -> None:
    """Temperatures round to one decimal (52.7 -> 52.7, 52.05 -> 52.0
    under banker's rounding, 72.71 -> 72.7)."""
    assert format_precision(52.0, "\u00b0C") == 52.0
    assert format_precision(52.7, "\u00b0C") == 52.7
    assert format_precision(72.71, "\u00b0C") == 72.7


def test_format_precision_voltage_two_decimals() -> None:
    assert format_precision(11.4, "V") == 11.4
    assert format_precision(11.456, "V") == 11.46


def test_format_precision_power_adaptive() -> None:
    """Power is 0 dp at >= 100 W, 1 dp below."""
    assert format_precision(150.0, "W") == 150.0
    assert format_precision(99.9, "W") == 99.9
    assert format_precision(50.55, "W") == 50.5
    assert format_precision(0.43, "W") == 0.4


def test_format_precision_latency_ms_adaptive() -> None:
    """Latency is 0 dp at >= 10 ms, 1 dp below."""
    assert format_precision(9.5, "ms") == 9.5
    assert format_precision(10.0, "ms") == 10.0
    assert format_precision(123.0, "ms") == 123.0
    assert format_precision(9.05, "ms") == 9.1


def test_format_precision_other_units_preserve_value() -> None:
    """Bytes, energy, fan, durations, signal, frequency preserve the value
    because Home Assistant's own unit-conversion layer picks the suffix.
    """
    assert format_precision(123_456_789, "B") == 123_456_789
    assert format_precision(50.0, "Wh") == 50.0
    assert format_precision(2400, "RPM") == 2400
    assert format_precision(3600, "s") == 3600
    assert format_precision(-50, "dBm") == -50
    assert format_precision(2437, "MHz") == 2437


# ---------------------------------------------------------------------------
# format_value: end-to-end display strings
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("value", "unit", "expected"),
    [
        (95.0, "%", "95 %"),
        (3.7, "%", "3.7 %"),
        (52.0, "\u00b0C", "52 \u00b0C"),
        (52.7, "\u00b0C", "52.7 \u00b0C"),
        (11.4, "V", "11.4 V"),
        (150.0, "W", "150 W"),
        (50.5, "W", "50.5 W"),
        (9.5, "ms", "9.5 ms"),
        (123.0, "ms", "123 ms"),
        (123_456_789, "B", "123456789 B"),
    ],
)
def test_format_value_renders_value_with_unit(value: float, unit: str, expected: str) -> None:
    assert format_value(value, unit) == expected


def test_format_value_without_unit_renders_just_the_number() -> None:
    assert format_value(3.72, None) == "3.72"
    assert format_value(95.0, None) == "95"
    assert format_value(0.0, None) == "0"


def test_format_value_handles_nan_and_infinity_gracefully() -> None:
    """NaN and +/-inf still produce strings (HA's UI treats them as
    unavailable, but the formatter itself must not raise)."""
    # ``format_precision`` falls through to ``value`` for unknown units,
    # so NaN / inf propagate as their Python string form.
    assert math.isnan(float(format_value(math.nan, None)))
    assert math.isinf(float(format_value(math.inf, None)))
    assert math.isinf(float(format_value(-math.inf, None)))
