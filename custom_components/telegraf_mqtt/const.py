"""Constants for telegraf_mqtt."""

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
DEFAULT_TOPIC_PATTERN = "telegraf/#"
DEFAULT_DEVICE_NAME = "Telegraf MQTT"
DEFAULT_EXPIRE_AFTER = 120
DEFAULT_ENABLE_CLEANUP = True
DEFAULT_CLEANUP_DELAY = 30 * 24 * 60 * 60  # 30 days
DEFAULT_DELETE_DELAY = 60 * 24 * 60 * 60  # 60 days
DEFAULT_MIN_ACTIVE_METRICS = 1

SIGNAL_NEW_DEVICE = f"{DOMAIN}_new_device_{{entry_id}}"
SIGNAL_NEW_METRIC = f"{DOMAIN}_new_metric_{{entry_id}}"
SIGNAL_METRIC_UPDATED = f"{DOMAIN}_metric_updated_{{entry_id}}"
SIGNAL_REMOVE_METRIC = f"{DOMAIN}_remove_metric_{{entry_id}}"
