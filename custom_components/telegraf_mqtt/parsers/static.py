"""Static-metadata field policy (Phase 6, hardened in Phase 10).

The ``MetricDescriptor`` carries a ``cleanup_policy`` ``Literal`` field.
Static system metadata that does not change after first read (CPU model,
vendor id, boot time format, etc.) is marked ``"NEVER"`` so the cleanup
loop never removes it; a device keeps its identity even if the metric
stream goes quiet.

Everything else defaults to ``"AUTO"`` (the descriptor dataclass default).
The literal values live in ``const.py`` so the runtime comparisons in
``registry.py`` and the dataclass annotation in ``models.py`` share a
single source of truth.
"""

from __future__ import annotations

from ..const import CLEANUP_POLICY_NEVER

# (measurement, field) pairs that describe a system's fixed metadata. These
# fields never change after first read and so are protected from the
# auto-cleanup loop. Extend as more plugins surface static descriptors.
_STATIC_FIELDS: frozenset[tuple[str, str]] = frozenset(
    {
        # system plugin: fixed identity / count metadata.
        ("system", "n_cpus"),
        ("system", "n_users"),
        ("system", "uptime_format"),
        # cpu plugin: model / vendor identification is static on real hardware.
        ("cpu", "model_name"),
        ("cpu", "vendor_id"),
        ("cpu", "flags"),
        ("cpu", "cache_size"),
    }
)


def is_static_field(measurement: str, field: str) -> bool:
    """Return whether ``(measurement, field)`` describes fixed system metadata.

    Lookup is case-insensitive on the measurement name (Telegraf lowercases
    measurement names in practice, but be defensive against mixed case).
    """
    return (measurement.lower(), field.lower()) in _STATIC_FIELDS


def static_cleanup_policy(measurement: str, field: str) -> str:
    """Return the ``cleanup_policy`` for a ``(measurement, field)`` pair.

    Centralised here so the parser does not need to import the literal
    values from ``const.py``; tests pin the contract.
    """
    return CLEANUP_POLICY_NEVER if is_static_field(measurement, field) else "AUTO"
