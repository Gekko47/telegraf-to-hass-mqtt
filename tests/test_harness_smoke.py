"""Smoke tests: prove the HA test harness itself works on this platform.

If these fail, the problem is the environment/harness — not telegraf_mqtt code.
"""

from __future__ import annotations

from homeassistant.const import __version__ as HA_VERSION
from homeassistant.loader import async_get_integration


async def test_hass_fixture_boots(hass) -> None:
    """The hass fixture starts a real Home Assistant test instance."""
    assert hass.is_running
    assert HA_VERSION == "2026.6.4"  # matches hacs.json floor (2026.6.x)


async def test_telegraf_mqtt_integration_is_loadable(hass, enable_custom_integrations) -> None:
    """HA's loader discovers telegraf_mqtt as a custom integration."""
    integration = await async_get_integration(hass, "telegraf_mqtt")
    assert integration.domain == "telegraf_mqtt"
