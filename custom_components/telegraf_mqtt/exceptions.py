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


# ``MqttBrokerUnreachable`` used to live here. It was deleted in 1.5.0:
# nothing could raise it. The broker-unreachable condition is reported at
# setup by ``__init__._broker_unreachable_not_ready``, which builds a
# ``ConfigEntryNotReady`` so HA retries -- the only correct behaviour for
# a transient broker outage. An ordinary ``HomeAssistantError`` is fatal
# and would strand the user with a dead entry, so this class could not be
# used there, and no other surface needed it. The
# ``mqtt_broker_unreachable`` translation key is still reachable, and
# still used, through that ``ConfigEntryNotReady``.
