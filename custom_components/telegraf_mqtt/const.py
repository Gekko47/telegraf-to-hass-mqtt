"""Constants for telegraf_mqtt."""

DOMAIN = "telegraf_mqtt"
CONF_DEVICE_NAME = "device_name"
CONF_TOPIC_PATTERN = "topic_pattern"
DEFAULT_TOPIC_PATTERN = "telegraf/#"
DEFAULT_DEVICE_NAME = "Telegraf MQTT"

SIGNAL_NEW_METRIC = f"{DOMAIN}_new_metric_{{entry_id}}"
SIGNAL_METRIC_UPDATED = f"{DOMAIN}_metric_updated_{{entry_id}}"
