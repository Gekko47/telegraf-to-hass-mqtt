"""Constants for telegraf_mqtt."""

DOMAIN = "telegraf_mqtt"
CONF_DEVICE_NAME = "device_name"
CONF_EXCLUDE_PATTERNS = "exclude_patterns"
CONF_EXPIRE_AFTER = "expire_after"
CONF_FIELD_OVERRIDES = "field_overrides"
CONF_TOPIC_PATTERN = "topic_pattern"
DEFAULT_TOPIC_PATTERN = "telegraf/#"
DEFAULT_DEVICE_NAME = "Telegraf MQTT"
DEFAULT_EXPIRE_AFTER = 120

SIGNAL_NEW_METRIC = f"{DOMAIN}_new_metric_{{entry_id}}"
SIGNAL_METRIC_UPDATED = f"{DOMAIN}_metric_updated_{{entry_id}}"
