"""Telegraf measurement parsers."""

from .battery import parse_battery_payload
from .cpu import parse_cpu_payload
from .disk import parse_disk_payload
from .generic import parse_generic_payload
from .mem import parse_mem_payload
from .net import parse_net_payload
from .nvidia_gpu import parse_nvidia_gpu_payload
from .sensors import parse_sensors_payload
from .static import is_static_field

__all__ = [
    "is_static_field",
    "parse_battery_payload",
    "parse_cpu_payload",
    "parse_disk_payload",
    "parse_generic_payload",
    "parse_mem_payload",
    "parse_net_payload",
    "parse_nvidia_gpu_payload",
    "parse_sensors_payload",
]
