"""Icon table for telegraf_mqtt (Phase 9).

Maps the icon keys produced by ``naming.infer_icon_key`` to Material
Design Icon names. Every emitted descriptor resolves to a non-null icon
through this table; if a new measurement type is added without an icon
key, ``infer_icon_key`` returns ``ICON_KEY_GENERIC`` which maps here.
"""

from __future__ import annotations

from .naming import (
    ICON_KEY_BATTERY,
    ICON_KEY_BINARY,
    ICON_KEY_CPU,
    ICON_KEY_DISK,
    ICON_KEY_ENERGY,
    ICON_KEY_FAN,
    ICON_KEY_GENERIC,
    ICON_KEY_MEMORY,
    ICON_KEY_NETWORK,
    ICON_KEY_PERCENTAGE,
    ICON_KEY_POWER,
    ICON_KEY_TEMPERATURE,
    ICON_KEY_VOLTAGE,
)

ICON_FOR_KEY: dict[str, str] = {
    ICON_KEY_CPU: "mdi:cpu-64-bit",
    ICON_KEY_MEMORY: "mdi:memory",
    ICON_KEY_DISK: "mdi:harddisk",
    ICON_KEY_NETWORK: "mdi:network",
    ICON_KEY_TEMPERATURE: "mdi:thermometer",
    ICON_KEY_VOLTAGE: "mdi:lightning-bolt",
    ICON_KEY_POWER: "mdi:flash",
    ICON_KEY_ENERGY: "mdi:battery-charging",
    ICON_KEY_BATTERY: "mdi:battery",
    ICON_KEY_FAN: "mdi:fan",
    ICON_KEY_PERCENTAGE: "mdi:percent",
    ICON_KEY_BINARY: "mdi:check-circle-outline",
    ICON_KEY_GENERIC: "mdi:gauge",
}
