"""Translatable exceptions for the telegraf_mqtt integration (Phase 9).

These exceptions carry ``translation_domain`` and ``translation_key`` so
HA renders them via the integration's own translations. The user sees
the localised message in the toast, the developer gets a typed
exception to assert against in tests.
"""

from __future__ import annotations

from homeassistant.exceptions import HomeAssistantError


class TelegrafMqttException(HomeAssistantError):
    """Base class for telegraf_mqtt exceptions."""


class ReconfigureSubscribeFailed(TelegrafMqttException):
    """Raised when a reconfigure-flow subscribe to the new topic pattern fails.

    Placeholders: ``topic``, ``error``.
    Translation key: ``exceptions.reconfigure_subscribe_failed``.
    """

    def __init__(self, topic: str, error: str) -> None:
        super().__init__(
            f"Could not subscribe to {topic}: {error}",
            translation_domain="telegraf_mqtt",
            translation_key="reconfigure_subscribe_failed",
            translation_placeholders={"topic": topic, "error": error},
        )


class MqttBrokerUnreachable(TelegrafMqttException):
    """Raised when the MQTT broker is not reachable on initial subscribe.

    Placeholders: ``topic``, ``error``.
    Translation key: ``exceptions.mqtt_broker_unreachable``.
    """

    def __init__(self, topic: str, error: str) -> None:
        super().__init__(
            f"Could not reach the MQTT broker at {topic}: {error}",
            translation_domain="telegraf_mqtt",
            translation_key="mqtt_broker_unreachable",
            translation_placeholders={"topic": topic, "error": error},
        )
