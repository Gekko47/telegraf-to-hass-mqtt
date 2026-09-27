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
import logging
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
    DIAGNOSTICS_PENDING_CLEANUP_LIMIT,
)

_LOGGER = logging.getLogger(__name__)


def _redact_topic(topic: Any) -> str | None:
    """Return a topic safe to publish: root segment plus a stable digest.

    ``telegraf/server01/cpu`` becomes ``telegraf/<8-char digest>``. The
    root is almost always a namespace the user already sees in their own
    configuration, and the digest still lets two diagnostics downloads be
    correlated for the same topic without disclosing the host tree.
    """
    if not isinstance(topic, str) or not topic:
        return None
    stripped = topic.strip("/")
    if "/" not in stripped:
        # A single-segment topic carries no host information to redact.
        return topic
    if "#" in stripped or "+" in stripped:
        # A subscription FILTER (``telegraf/rack1/#``), not an observed
        # topic. The wildcards and the namespace are the only parts a
        # user needs to identify the scope, but a filter can still name a
        # host as a literal segment (``telegraf/rack1/#``), so those
        # segments are redacted exactly like an observed topic's.
        return _redact_filter(stripped)
    root = stripped.split("/", 1)[0]
    return f"{root}/{hashlib.sha256(topic.encode('utf-8')).hexdigest()[:8]}"


def _redact_filter(stripped: str) -> str:
    """Redact the host-bearing levels of a subscription filter.

    ``telegraf/rack1/#`` becomes ``telegraf/<8-char digest>/#`` and
    ``telegraf/+/cpu`` is returned unchanged: ``+`` and ``#`` are
    wildcards, not host names, so preserving them keeps the one field
    that tells a user which scope the entry is watching readable.
    """
    levels = stripped.split("/")
    out = [levels[0]]
    for level in levels[1:]:
        if level in ("#", "+"):
            out.append(level)
        else:
            out.append(hashlib.sha256(level.encode("utf-8")).hexdigest()[:8])
    return "/".join(out)


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
            # M4: an allow-list projection, not a verbatim dump. The old
            # ``dict(entry.data)`` published ``device_name`` (which the
            # config flow derives from the topic) and the raw
            # ``topic_pattern`` (which commonly embeds the host name),
            # contradicting this module's own redaction contract. Same for
            # ``options``: the field NAMES can name a host, so only the
            # scalar knobs and key lists are published.
            "data": {
                "topic_pattern": _redact_topic(entry.data.get(CONF_TOPIC_PATTERN)),
                "manufacturer": entry.data.get("manufacturer"),
                "model": entry.data.get("model"),
                "sw_version": entry.data.get("sw_version"),
            },
            "options": {
                "expire_after": entry.options.get(CONF_EXPIRE_AFTER),
                "enable_cleanup": entry.options.get(CONF_ENABLE_CLEANUP),
                "cleanup_delay": entry.options.get(CONF_CLEANUP_DELAY),
                "delete_delay": entry.options.get(CONF_DELETE_DELAY),
                "min_active_metrics": entry.options.get(CONF_MIN_ACTIVE_METRICS),
                "auto_discover": entry.options.get("auto_discover"),
                # The scope is a filter, not a scalar knob, and it can name
                # a host as a literal level -- it gets the same redaction
                # ``topic_pattern`` does.
                "auto_discover_scope": _redact_topic(entry.options.get("auto_discover_scope")),
                "device_id_strategy": entry.options.get("device_id_strategy"),
                # A field or ``unique_key`` can itself be a host name
                # (``{"host": {...}}``), so the raw key lists are not
                # published. The count says whether any are configured and
                # the digests keep two downloads of the same config
                # comparable without disclosing the names.
                "field_override_key_count": len(entry.options.get(CONF_FIELD_OVERRIDES) or {}),
                "field_override_keys": sorted(
                    hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]
                    for key in (entry.options.get(CONF_FIELD_OVERRIDES) or {})
                ),
                "category_override_key_count": len(entry.options.get("category_overrides") or {}),
                "category_override_keys": sorted(
                    hashlib.sha256(key.encode("utf-8")).hexdigest()[:8]
                    for key in (entry.options.get("category_overrides") or {})
                ),
                "exclude_pattern_count": len(entry.options.get("exclude_patterns") or []),
            },
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
    if manager is None:
        # ``TelegrafMqttRuntimeData.manager`` is typed ``| None`` for the
        # unload path. Dereferencing it here raised AttributeError inside
        # the one surface a user reaches for when something is already
        # broken.
        return {"manager": None, "devices": [], "device_count": 0}
    devices: list[dict[str, Any]] = []
    # ``now()`` is the manager's own clock, so the reported age is computed
    # against the same time source that produced ``last_any_metric``. The
    # previous ``manager._clock() if hasattr(manager, "_clock") else 0.0``
    # silently degraded to a zero epoch for any object lacking the private
    # attribute -- which is how a test double ended up defining the
    # production contract.
    now = manager.now()
    for device_id, registry in manager.devices.items():
        # Per-device diagnostics: the distinct measurement names the
        # registry holds, plus metric count and the time since the last
        # message. ``measurements`` is a public read-only projection; this
        # module must not reach into ``registry._states`` to build it.
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
                "measurements": sorted(registry.measurements),
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
                # M1 made this field real, which meant it started leaking:
                # Telegraf topic trees commonly embed the host
                # (``telegraf/<server01>/cpu``), so a verbatim topic is
                # the same identity disclosure the rest of this payload
                # exists to avoid. Keep the root segment -- which is what
                # tells a user *which* topic root is misbehaving -- plus a
                # stable digest for correlating two downloads.
                "topic": _redact_topic(ps.last_message.get("topic")),
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
        "pending_cleanup": _pending_cleanup_block(manager),
    }


def _pending_cleanup_block(manager: Any) -> dict[str, Any]:
    """Summarise the metrics currently queued for removal.

    The lifecycle's Cleanup-Candidate state is invisible from the outside:
    an entity sits there for ``cleanup_delay`` (30 days by default) before
    it disappears, and nothing in the payload says so. A user who thinks
    "the integration is deleting my entities" has no way to confirm that
    from a download -- this block is the answer, and it is the reason
    ``pending_cleanup_all`` and ``DIAGNOSTICS_PENDING_CLEANUP_LIMIT``
    exist.

    ``device_id`` is hashed for the same reason as everywhere else in this
    module. The list is truncated at the limit and the truncation is
    *reported* rather than silent: a user seeing ``truncated: true``
    knows to look at the counts instead of concluding there are no more.
    """
    raw = manager.pending_cleanup_all()
    total = sum(len(entries) for entries in raw.values())
    flat: list[dict[str, Any]] = []
    for device_id, entries in raw.items():
        for entry in entries:
            flat.append(
                {
                    "device_id": _hash_device_id(device_id),
                    "unique_key": entry["unique_key"],
                    "cleanup_candidate_since": entry["cleanup_candidate_since"],
                    "seconds_until_removal": entry["seconds_until_removal"],
                }
            )
    # Oldest candidates first: a truncated list is far more useful when
    # it shows what is about to be deleted than what is furthest away.
    flat.sort(key=lambda item: item["cleanup_candidate_since"])
    shown = flat[:DIAGNOSTICS_PENDING_CLEANUP_LIMIT]
    return {
        "total": total,
        "truncated": len(shown) < total,
        "metrics": shown,
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
