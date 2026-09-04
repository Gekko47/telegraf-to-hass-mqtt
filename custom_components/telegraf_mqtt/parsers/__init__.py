"""Telegraf measurement parsers."""

from .battery import parse_battery_payload
from .cpu import parse_cpu_payload
from .disk import parse_disk_payload
from .diskio import parse_diskio_payload
from .docker import (
    parse_docker_container_blkio_payload,
    parse_docker_container_cpu_payload,
    parse_docker_container_mem_payload,
    parse_docker_container_net_payload,
    parse_docker_container_status_payload,
    parse_docker_payload,
)
from .generic import parse_generic_payload
from .http_response import parse_http_response_payload
from .interrupts import parse_interrupts_payload
from .ipmi_sensor import parse_ipmi_sensor_payload
from .kernel import parse_kernel_payload
from .kernel_vmstat import parse_kernel_vmstat_payload
from .mem import parse_mem_payload
from .net import parse_net_payload
from .net_response import parse_net_response_payload
from .nvidia_gpu import parse_nvidia_gpu_payload
from .ping import parse_ping_payload
from .processes import parse_processes_payload
from .sensors import parse_sensors_payload
from .smart import parse_smart_payload
from .static import is_static_field
from .swap import parse_swap_payload
from .system import parse_system_payload
from .wireless import parse_wireless_payload
from .zfs import (
    parse_zfs_dataset_payload,
    parse_zfs_payload,
    parse_zfs_pool_payload,
)

__all__ = [
    "is_static_field",
    "parse_battery_payload",
    "parse_cpu_payload",
    "parse_disk_payload",
    "parse_diskio_payload",
    "parse_docker_container_blkio_payload",
    "parse_docker_container_cpu_payload",
    "parse_docker_container_mem_payload",
    "parse_docker_container_net_payload",
    "parse_docker_container_status_payload",
    "parse_docker_payload",
    "parse_generic_payload",
    "parse_http_response_payload",
    "parse_interrupts_payload",
    "parse_ipmi_sensor_payload",
    "parse_kernel_payload",
    "parse_kernel_vmstat_payload",
    "parse_mem_payload",
    "parse_net_payload",
    "parse_net_response_payload",
    "parse_nvidia_gpu_payload",
    "parse_ping_payload",
    "parse_processes_payload",
    "parse_sensors_payload",
    "parse_smart_payload",
    "parse_swap_payload",
    "parse_system_payload",
    "parse_wireless_payload",
    "parse_zfs_dataset_payload",
    "parse_zfs_payload",
    "parse_zfs_pool_payload",
]
