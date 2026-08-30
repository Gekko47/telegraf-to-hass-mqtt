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
# "host_topic" -- if host is missing, append the second-level topic root.
# "topic_only" -- always use the topic tree; less stable across re-arranges.
CONF_DEVICE_ID_STRATEGY = "device_id_strategy"
DEFAULT_DEVICE_ID_STRATEGY = "host"
VALID_DEVICE_ID_STRATEGIES = ("host", "host_topic", "topic_only")
# Phase 10: enable the post-setup snoop listener that records what Telegraf
# hosts are publishing on the broker, surfaces a Repairs hint if the user's
# configured topic pattern matches nothing, and auto-extends the pattern
# when new hosts appear under a sibling topic.
CONF_AUTO_DISCOVER = "auto_discover"
DEFAULT_AUTO_DISCOVER = True
# How long the post-setup snoop listens before reporting what it saw.
CONF_AUTO_DISCOVER_TIMEOUT = "auto_discover_timeout"
DEFAULT_AUTO_DISCOVER_TIMEOUT = 10
# The wildcard topic the snoop uses. Defaults to `telegraf/#` so it sees
# every Telegraf message the broker is carrying, regardless of what the
# user's configured topic pattern is.
CONF_AUTO_DISCOVER_PROBE_TOPIC = "auto_discover_probe_topic"
DEFAULT_AUTO_DISCOVER_PROBE_TOPIC = "telegraf/#"
DEFAULT_TOPIC_PATTERN = "telegraf/#"
DEFAULT_DEVICE_NAME = "Telegraf MQTT"
DEFAULT_EXPIRE_AFTER = 120
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
