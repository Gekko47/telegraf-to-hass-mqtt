"""Diagnostics support for the telegraf_mqtt integration (Phase 7).

The diagnostics payload is the integration's user-facing debug surface:
"why is my entity unavailable", "what's in my registry", "is the parser
happy". SPEC.md requires:

  - current configuration
  - known measurements / entities
  - parser statistics
  - last-message metadata (redacted appropriately)
  - dropped-payload counts

HA downloads this payload as JSON. The dict is built in a single
``async_get_config_entry_diagnostics`` entry point; the shape is
deliberately stable so that future tools (a custom inspector, the
hassfest-action integration) can rely on it.

Redaction contract: the raw Telegraf payload, every field value, and
the host identity (the user's machine name) are NEVER included. The
payload size (in bytes) is included for "is the broker sending
reasonable amounts" diagnostics.
"""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant

from .const import (
    CONF_CLEANUP_DELAY,
    CONF_DELETE_DELAY,
    CONF_ENABLE_CLEANUP,
    CONF_EXPIRE_AFTER,
    CONF_FIELD_OVERRIDES,
    CONF_MIN_ACTIVE_METRICS,
    CONF_TOPIC_PATTERN,
)


def _hash_device_id(device_id: str) -> str:
    """Return a stable, opaque hash of a Telegraf ``device_id``.

    The registry derives ``device_id`` from the payload's ``host`` tag
    (slugified). The slug usually preserves readable text (e.g.
    ``example-host`` -> ``example_host``), which would expose the
    user's machine name in a downloaded diagnostics file. We replace
    the raw slug with a short, stable SHA-256 digest: stable enough
    to correlate devices within a single download, opaque enough
    that the host name never leaves the integration.
    """
    return hashlib.sha256(device_id.encode("utf-8")).hexdigest()[:16]


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> dict[str, Any]:
    """Build the user-facing diagnostics payload for one config entry.

    Top-level keys: ``entry``, ``config``, ``runtime``, ``options_validity``.
    """
    runtime_data = getattr(entry, "runtime_data", None)

    payload: dict[str, Any] = {
        "entry": {
            "entry_id": entry.entry_id,
            "domain": entry.domain,
            "title": entry.title,
            "unique_id": entry.unique_id,
        },
        "config": {
            "data": dict(entry.data),
            "options": dict(entry.options),
        },
        "runtime": _runtime_snapshot(runtime_data),
        "options_validity": _options_validity(entry.options),
    }
    if runtime_data is None:
        payload.pop("runtime")
    return payload


def _runtime_snapshot(runtime_data: Any) -> dict[str, Any]:
    """Build the ``runtime`` block from the runtime data, with redaction."""
    if runtime_data is None:
        return {}

    manager = runtime_data.manager
    devices: list[dict[str, Any]] = []
    now = manager._clock() if hasattr(manager, "_clock") else 0.0
    for device_id, registry in manager.devices.items():
        # Per-device diagnostics: collect the distinct measurement
        # names the registry has ever cached, plus metric count and
        # the time since the last message.
        measurements: set[str] = set()
        for state in registry._states.values():
            measurements.add(state.descriptor.measurement)
        devices.append(
            {
                # Hash the device_id so the underlying Telegraf host
                # name (encoded in the slug) is never exposed in a
                # downloaded diagnostics file. The digest is stable,
                # so correlatability within and across downloads is
                # preserved without leaking the host identity.
                "device_id": _hash_device_id(device_id),
                # ``device_name`` is the user-chosen display name from
                # config flow. It can echo the host name in practice
                # and is omitted from the redacted diagnostics payload
                # for the same reason as ``device_id`` above.
                "metric_count": len(registry),
                "measurements": sorted(measurements),
                "last_any_metric_age_seconds": max(0.0, now - registry.last_any_metric),
            }
        )

    parser_stats: dict[str, Any] = {}
    if getattr(runtime_data, "parser_stats", None) is not None:
        ps = runtime_data.parser_stats
        # Project ``last_message`` to its closed key set instead of
        # forwarding the dict verbatim. The parser already keeps
        # redaction, but a regression in the parser (or a future
        # field like ``raw_payload`` / ``value_bytes`` / ``host``)
        # must not silently leak into a downloaded diagnostics
        # file. The closed key set is pinned by
        # ``tests/test_phase7_diagnostics_repairs.py`` and the
        # diagnostics redaction test in ``tests/test_diagnostics.py``.
        last_message: dict[str, Any] | None = None
        if ps.last_message is not None:
            last_message = {
                "topic": ps.last_message.get("topic"),
                "byte_length": ps.last_message.get("byte_length"),
                "dropped_reason": ps.last_message.get("dropped_reason"),
                "measurement": ps.last_message.get("measurement"),
            }
        parser_stats = {
            "received": ps.received,
            "parsed": ps.parsed,
            "dropped_invalid_json": ps.dropped_invalid_json,
            "dropped_unsupported_shape": ps.dropped_unsupported_shape,
            "unknown_measurement_fallbacks": ps.unknown_measurement_fallbacks,
            "last_message": last_message,
        }

    manager_options = {
        "expire_after": manager._expire_after,
        "exclude_patterns": list(manager._exclude_patterns),
        "field_overrides_keys": sorted(manager._field_overrides.keys()),
        "cleanup_delay": manager._cleanup_delay,
        "delete_delay": manager._delete_delay,
        "enable_cleanup": manager._enable_cleanup,
        "min_active_metrics": manager._min_active_metrics,
    }

    return {
        "manufacturer": runtime_data.manufacturer,
        "model": runtime_data.model,
        "manager": {
            "options": manager_options,
            "devices": devices,
            "device_count": len(manager.devices),
        },
        "parser_stats": parser_stats,
    }


def _options_validity(raw_options: Mapping[str, Any]) -> dict[str, bool]:
    """Per-option validity booleans for the user-facing options.

    Each boolean is True if the value can be coerced to the expected
    type with the same defaults the integration would use. Repairs
    issues for invalid options are independent of this view: a value
    can be valid here while a Repair is still pending (e.g. the
    user accepted a default, but the original is still bad on disk).
    """
    validity: dict[str, bool] = {}

    def _valid_int(name: str, minimum: int = 0) -> bool:
        if name not in raw_options:
            return True
        try:
            value = int(raw_options[name])
        except (TypeError, ValueError):  # fmt: skip
            return False
        return value >= minimum

    def _valid_bool(name: str) -> bool:
        if name not in raw_options:
            return True
        return isinstance(raw_options[name], bool)

    validity[CONF_EXPIRE_AFTER] = _valid_int(CONF_EXPIRE_AFTER, minimum=1)
    validity[CONF_CLEANUP_DELAY] = _valid_int(CONF_CLEANUP_DELAY)
    validity[CONF_DELETE_DELAY] = _valid_int(CONF_DELETE_DELAY)
    validity[CONF_MIN_ACTIVE_METRICS] = _valid_int(CONF_MIN_ACTIVE_METRICS)
    validity[CONF_ENABLE_CLEANUP] = _valid_bool(CONF_ENABLE_CLEANUP)

    # Topics are always strings; non-empty and no embedded NULs.
    if CONF_TOPIC_PATTERN in raw_options:
        topic = raw_options[CONF_TOPIC_PATTERN]
        validity[CONF_TOPIC_PATTERN] = isinstance(topic, str) and bool(topic) and "\x00" not in topic

    # Field overrides: a dict of {field: {unit?, device_class?, ...}}.
    if CONF_FIELD_OVERRIDES in raw_options:
        overrides = raw_options[CONF_FIELD_OVERRIDES]
        validity[CONF_FIELD_OVERRIDES] = isinstance(overrides, dict) and all(
            isinstance(k, str) and isinstance(v, dict) for k, v in overrides.items()
        )

    return validity
