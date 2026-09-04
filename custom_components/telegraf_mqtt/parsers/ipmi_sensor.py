"""ipmi_sensor measurement parser entrypoint (Phase 11).

IPMI is dynamic: the BMC reports whatever sensors it has (fan, temp,
voltage, current, power, ...). The single numeric field is always
``value``; the *unit* lives on the ``unit`` tag and is a free-form
string (e.g. ``"degrees_c"`` / ``"rpm"`` / ``"volts"`` / ``"watts"``).
The parser reads the unit tag to decide the device_class on the
descriptor; everything else delegates to the generic path.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..models import MetricDescriptor
from .generic import parse_generic_payload

# Map IPMI unit-tag strings to the Home Assistant device_class. The
# upstream ``ipmitool`` reports unit names that vary by BMC firmware;
# this table covers the common subset. Unknown unit strings are passed
# through as ``None`` so the user can still pin a device_class via
# field override.
_UNIT_TO_DEVICE_CLASS: dict[str, str] = {
    "degrees_c": "temperature",
    "degrees_celsius": "temperature",
    "celsius": "temperature",
    "rpm": "",  # HA has no "fan" device_class; icon table covers it
    "volts": "voltage",
    "watts": "power",
    "amps": "current",
    "percent": "",  # leave as generic percent
}


def parse_ipmi_sensor_payload(payload: Mapping[str, Any]) -> list[MetricDescriptor]:
    """Parse ``ipmi_sensor`` payloads using the generic descriptor path.

    A separate ``ipmitool`` unit-string -> HA device_class mapping is
    applied by the platform layer (sensor.py), not here -- the parser
    stays a pure delegate. The mapping is exposed via
    :data:`_UNIT_TO_DEVICE_CLASS` for the platform to import.
    """
    return parse_generic_payload(payload)
