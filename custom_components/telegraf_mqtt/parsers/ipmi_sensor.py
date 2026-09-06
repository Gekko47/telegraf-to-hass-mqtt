"""ipmi_sensor measurement parser entrypoint (Phase 11).

IPMI is dynamic: the BMC reports whatever sensors it has (fan, temp,
voltage, current, power, ...). The single numeric field is always
``value``; the *unit* lives on the ``unit`` tag and is a free-form
string (e.g. ``"degrees_c"`` / ``"rpm"`` / ``"volts"`` / ``"watts"``).
The parser reads the unit tag to decide the device_class on the
descriptor; everything else delegates to the generic path.

The unit tag is mapped to HA native_unit and device_class via the
``_TAG_UNIT_MAPPINGS`` registry in ``parsers.generic``.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..models import MetricDescriptor
from .generic import parse_generic_payload


def parse_ipmi_sensor_payload(payload: Mapping[str, Any]) -> list[MetricDescriptor]:
    """Parse ``ipmi_sensor`` payloads using the generic descriptor path.

    The ``unit`` tag is mapped to HA native_unit and device_class via the
    ``_TAG_UNIT_MAPPINGS`` registry in ``parsers.generic``.
    """
    return parse_generic_payload(payload)
