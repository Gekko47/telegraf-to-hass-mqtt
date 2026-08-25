"""Test bootstrap for local custom component imports and the HA test harness.

Layers:
1. sys.path bootstrap so ``custom_components.telegraf_mqtt`` imports without install.
2. Windows-only POSIX shims (HA core imports ``fcntl`` and ``resource`` at module level;
   the HA test harness imports those modules even when the functionality under test
   never uses them). Registered before the plugin loads.
3. ``pytest_homeassistant_custom_component`` plugin — provides a running HA test
   instance (``hass``), ``MockConfigEntry``, MQTT message injection
   (``async_fire_mqtt_message``), etc. Only HA-dependent tests use those fixtures;
   parser/registry/naming tests stay harness-free per AGENTS.md.
"""

from __future__ import annotations

import shutil
import sys
import types
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

# --- Windows-only POSIX shims (no-op stand-ins; see docstring) -------------
if sys.platform == "win32":
    if "fcntl" not in sys.modules:
        fcntl_stub = types.ModuleType("fcntl")
        fcntl_stub.LOCK_EX = 2  # type: ignore[attr-defined]
        fcntl_stub.LOCK_NB = 4  # type: ignore[attr-defined]
        fcntl_stub.flock = lambda fd, operation: None  # type: ignore[attr-defined]
        sys.modules["fcntl"] = fcntl_stub
    if "resource" not in sys.modules:
        resource_stub = types.ModuleType("resource")
        resource_stub.RLIMIT_NOFILE = 7  # type: ignore[attr-defined]
        resource_stub.getrlimit = lambda res: (8192, 8192)  # type: ignore[attr-defined]
        resource_stub.setrlimit = lambda res, limits: None  # type: ignore[attr-defined]
        sys.modules["resource"] = resource_stub

import pytest  # noqa: E402  (must follow the shims above on Windows)

pytest_plugins = ["pytest_homeassistant_custom_component.plugins"]


@pytest.fixture(autouse=True)
def auto_enable_custom_integrations(enable_custom_integrations):
    """Allow HA setup tests to load custom_components/telegraf_mqtt."""
    yield


if sys.platform == "win32":

    @pytest.fixture(autouse=True)
    def _win_sockets_for_hass_setup(socket_enabled):
        """Allow sockets under the HA harness on Windows.

        The plugin blocks all sockets by design, but on Windows even ``hass``
        fixture setup touches real sockets (zeroconf/ifaddr NIC enumeration),
        which nothing under test relies on. No-op on POSIX, where the strict
        default stays in force.
        """
        yield


@pytest.fixture
def hass_config_dir(hass_tmp_config_dir: str) -> str:
    """Point the HA test instance's config dir at this repo's layout.

    The plugin defaults to its own ``testing_config`` folder, where
    ``custom_components/telegraf_mqtt`` doesn't exist. Seed the temp config
    dir with the repo's real integration so ``enable_custom_integrations``
    can discover it. Only instantiated by tests that use ``hass``.
    """
    shutil.copytree(
        ROOT / "custom_components",
        Path(hass_tmp_config_dir) / "custom_components",
        dirs_exist_ok=True,
    )
    return hass_tmp_config_dir
