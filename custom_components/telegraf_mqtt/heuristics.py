"""Measurement heuristics for Phase 3 naming and entity metadata."""

from __future__ import annotations

from typing import Any

MEASUREMENT_PROFILES: dict[str, dict[str, Any]] = {
    "disk": {"entity_category": "diagnostic"},
    "mem": {"entity_category": None},
    "cpu": {"entity_category": None},
    "net": {"entity_category": None},
    "sensors": {"entity_category": None},
    "battery": {"entity_category": None},
    "nvidia_gpu": {"entity_category": None},
}

TAG_ALIASES: dict[str, str] = {
    "package_id_0": "CPU Package",
    "coretemp-isa-0000": "CPU",
    "cpu-total": "CPU Total",
    "disk": "Disk",
    "interface": "Interface",
}

FIELD_ALIASES: dict[str, str] = {
    "temp_input": "Temperature",
    "temp": "Temperature",
    "usage_idle": "Usage Idle",
    "usage_user": "Usage User",
    "used_percent": "Used Percent",
    "used": "Used",
    "free": "Free",
    "bytes_recv": "Bytes Received",
    "bytes_sent": "Bytes Sent",
    "gpu_util": "GPU Utilization",
    "mem_used": "Memory Used",
    "percentage": "Percentage",
    "voltage": "Voltage",
    "uptime": "Uptime",
    "boot_time": "Boot Time",
}

ENTITY_CATEGORY_DIAGNOSTIC = "diagnostic"
