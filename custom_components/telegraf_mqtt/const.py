"""Constants for telegraf_mqtt."""

from __future__ import annotations

from typing import Final

DOMAIN = "telegraf_mqtt"
CONF_DEVICE_NAME = "device_name"
CONF_EXCLUDE_PATTERNS = "exclude_patterns"
CONF_EXPIRE_AFTER = "expire_after"
CONF_FIELD_OVERRIDES = "field_overrides"
CONF_TOPIC_PATTERN = "topic_pattern"
# Phase 6: cleanup + device-lifecycle tunables (user-facing via OptionsFlow).
CONF_ENABLE_CLEANUP = "enable_cleanup"
CONF_CLEANUP_DELAY = "cleanup_delay"
CONF_DELETE_DELAY = "delete_delay"
CONF_MIN_ACTIVE_METRICS = "min_active_metrics"
# Phase 9: per-device metadata carried in the DeviceInfo.
CONF_MANUFACTURER = "manufacturer"
CONF_MODEL = "model"
CONF_SW_VERSION = "sw_version"
# Phase 10: per-entity category overrides keyed by unique_key.
# Maps unique_key -> "config" | "diagnostic" | None. A `None` value
# removes the auto-assigned category for that key, surfacing the entity
# in the primary list.
CONF_CATEGORY_OVERRIDES = "category_overrides"
# Phase 10: the platform hint for a field override. Valid values:
# "auto" (default, decide from the value's Python type), "sensor",
# "binary_sensor", or "none" (skip the field entirely).
CONF_FIELD_OVERRIDE_PLATFORM = "platform"
CONF_FIELD_OVERRIDE_NATIVE_UNIT = "native_unit"
CONF_FIELD_OVERRIDE_DEVICE_CLASS = "device_class"
CONF_FIELD_OVERRIDE_STATE_CLASS = "state_class"
CONF_FIELD_OVERRIDE_ENTITY_CATEGORY = "entity_category"
# Phase 10: how the integration derives a device_id from incoming messages.
# "host" -- use the host tag (default; matches pre-10 behaviour).
# "host_topic" -- prefer the host tag, but treat a *degenerate* host
#   (localhost, 127.0.0.1, 0.0.0.0, ::1, the literal "host") as missing
#   and append the second-level topic root. Solves the case where two
#   real hosts both publish ``host=localhost`` and the topic tree is
#   the only per-machine signal. See ``registry._derive_device_id``.
# "topic_only" -- always use the topic tree; less stable across re-arranges.
CONF_DEVICE_ID_STRATEGY = "device_id_strategy"
DEFAULT_DEVICE_ID_STRATEGY = "host"
VALID_DEVICE_ID_STRATEGIES = ("host", "host_topic", "topic_only")
# Post-setup snoop listener: when enabled, auto-picks up new Telegraf
# hosts that appear under the entry's ``topic_pattern``. Default is off
# because the probe runs on the same broker; a careless default would
# probe a wider scope than the user-configured ``topic_pattern``. The
# user opts in via the options flow.
#
# When opted in, ``__init__.py`` derives the snoop's probe topic from the
# entry's ``topic_pattern`` via ``derive_probe_topic`` -- the snoop never
# silently widens past the user's scope. Topic discovery (the
# pick-from-traffic flow) lives in the config flow, not the options flow.
CONF_AUTO_DISCOVER = "auto_discover"
DEFAULT_AUTO_DISCOVER = False
# Fallback used by ``SnoopListener`` and ``derive_probe_topic`` when no
# pattern is supplied. Belt-and-braces: the integration always supplies a
# pattern, so this only fires from code paths that haven't been wired up
# yet.
DEFAULT_AUTO_DISCOVER_PROBE_TOPIC = "telegraf/#"
DEFAULT_TOPIC_PATTERN = "telegraf/#"
# Config-flow "discover topics" mode. The user enters a probe topic and a
# scan window; the integration listens for that window, then presents the
# 2nd-level prefixes it saw so the user can pick which to subscribe to.
CONF_SCAN_ROOT_TOPIC = "scan_root_topic"
DEFAULT_SCAN_ROOT_TOPIC = "telegraf/#"
CONF_SCAN_DURATION_SECONDS = "scan_duration_seconds"
DEFAULT_SCAN_DURATION_SECONDS = 30
MIN_SCAN_DURATION_SECONDS = 5
MAX_SCAN_DURATION_SECONDS = 300
# Config-flow mode discriminator (manual topic vs. discover topics).
CONF_SETUP_MODE = "setup_mode"
SETUP_MODE_MANUAL = "manual"
SETUP_MODE_DISCOVER = "discover"
DEFAULT_DEVICE_NAME = "Telegraf MQTT"
DEFAULT_EXPIRE_AFTER = 120
# The periodic registry scan (expiry + cleanup + device pruning + the
# no-traffic Repairs check) runs synchronously on the event loop, so its
# cadence is floored well above 1s no matter how small ``expire_after``
# is: sub-5s cleanup precision is not meaningfully useful, and a
# once-per-second full scan across every device would add measurable
# event-loop latency at fleet scale (the scan is O(devices * metrics)
# and shares the loop with all of Home Assistant). Capped so very large
# ``expire_after`` values still get a sane cadence.
MIN_EXPIRY_TICK_SECONDS = 5
MAX_EXPIRY_TICK_SECONDS = 30
# Fleet-scale guard: the number of distinct Telegraf hosts (devices)
# a single config entry will track before it starts dropping new
# measurements and raising a Repairs warning. A shared broker can
# carry traffic from an arbitrary number of hosts; an unbounded
# ``DeviceManager.devices`` dict would let a single entry grow
# without limit and add event-loop latency to the periodic registry
# scan (which is O(devices * metrics)). ``MAX_DEVICES`` is a hard
# cap; ``DEFAULT_MAX_DEVICES`` is the default (30 hosts) applied
# when the user has not opted into a custom value. Set to 0 to
# disable the cap entirely (not recommended on a shared broker).
DEFAULT_MAX_DEVICES = 30
MAX_METRICS_PER_DEVICE = 50
DEFAULT_ENABLE_CLEANUP = True
DEFAULT_CLEANUP_DELAY = 30 * 24 * 60 * 60  # 30 days
DEFAULT_DELETE_DELAY = 60 * 24 * 60 * 60  # 60 days
DEFAULT_MIN_ACTIVE_METRICS = 1

# Cleanup-policy literal values. Kept here so the Literal in models.py and
# the runtime comparisons in registry.py share a single source of truth.
# ``Final`` keeps the inferred *literal* type, so the values satisfy the
# CleanupPolicy / PlatformHint Literals at the dataclass boundary.
CLEANUP_POLICY_AUTO: Final = "AUTO"
CLEANUP_POLICY_NEVER: Final = "NEVER"
CLEANUP_POLICY_ALWAYS: Final = "ALWAYS"

# Platform hint literal values used by field_overrides["platform"].
PLATFORM_HINT_AUTO: Final = "auto"
PLATFORM_HINT_SENSOR: Final = "sensor"
PLATFORM_HINT_BINARY_SENSOR: Final = "binary_sensor"
PLATFORM_HINT_NONE: Final = "none"
VALID_PLATFORM_HINTS = (
    PLATFORM_HINT_AUTO,
    PLATFORM_HINT_SENSOR,
    PLATFORM_HINT_BINARY_SENSOR,
    PLATFORM_HINT_NONE,
)

# How many entries to include in the diagnostics "pending_cleanup" list.
# Bounded so the diagnostics payload stays small even for a long-running
# install that has accumulated candidates.
DIAGNOSTICS_PENDING_CLEANUP_LIMIT = 50

# Repairs issue ids.
REPAIR_OVERLAP = "overlap_topic_patterns"
REPAIR_INVALID_OPTION = "invalid_persisted_option"
REPAIR_NO_TRAFFIC = "no_traffic_on_topic"
REPAIR_DEVICE_ID_COLLISION = "device_id_collision"
REPAIR_DEVICE_ID_CONFLICT = "device_id_conflict"

SIGNAL_NEW_DEVICE = f"{DOMAIN}_new_device_{{entry_id}}"
SIGNAL_NEW_METRIC = f"{DOMAIN}_new_metric_{{entry_id}}"
SIGNAL_METRIC_UPDATED = f"{DOMAIN}_metric_updated_{{entry_id}}"
SIGNAL_REMOVE_METRIC = f"{DOMAIN}_remove_metric_{{entry_id}}"
