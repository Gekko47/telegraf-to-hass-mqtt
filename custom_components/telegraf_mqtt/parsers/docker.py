"""Docker measurement parser entrypoint (Phase 11).

The ``docker`` input plugin splits into several measurements on the
wire (docker_container_cpu, docker_container_mem, docker_container_net,
docker_container_blkio, docker_container_status, docker_swarm,
docker_disk_usage). All are handled by the generic parser; the per-
measurement delegate keeps the dispatcher table open and explicit so
``docker_container_*`` payloads never hit the unknown-fallback path.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from ..models import MetricDescriptor
from .generic import parse_generic_payload


def parse_docker_container_cpu_payload(payload: Mapping[str, Any]) -> list[MetricDescriptor]:
    return parse_generic_payload(payload)


def parse_docker_container_mem_payload(payload: Mapping[str, Any]) -> list[MetricDescriptor]:
    return parse_generic_payload(payload)


def parse_docker_container_net_payload(payload: Mapping[str, Any]) -> list[MetricDescriptor]:
    return parse_generic_payload(payload)


def parse_docker_container_blkio_payload(payload: Mapping[str, Any]) -> list[MetricDescriptor]:
    return parse_generic_payload(payload)


def parse_docker_container_status_payload(payload: Mapping[str, Any]) -> list[MetricDescriptor]:
    return parse_generic_payload(payload)


def parse_docker_payload(payload: Mapping[str, Any]) -> list[MetricDescriptor]:
    """Catch-all delegate for any docker_* measurement not enumerated above."""
    return parse_generic_payload(payload)
