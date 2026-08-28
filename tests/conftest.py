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

import enum
import importlib
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


# ---------------------------------------------------------------------------
# Shared harness-free HA-stub installers
#
# Snapshot-based stand-ins covering everything sensor.py / binary_sensor.py
# import. The first install remembers whatever lived under ``homeassistant.*``
# (real HA from .venv or another test's stubs) and ``_restore_ha_stubs`` puts
# those originals back, so this file can never leak its stand-ins into
# harness-based tests that run afterwards. Moved here from
# ``test_phase4_units_statistics.py`` so phase 5's Bronze test (and any future
# harness-free platform test) can reuse the same scaffolding.
# ---------------------------------------------------------------------------


_HA_MODULE_NAMES: tuple[str, ...] = (
    "homeassistant.components",
    "homeassistant.components.sensor",
    "homeassistant.components.binary_sensor",
    "homeassistant.config_entries",
    "homeassistant.const",
    "homeassistant.core",
    "homeassistant.helpers",
    "homeassistant.helpers.device_registry",
    "homeassistant.helpers.dispatcher",
    "homeassistant.helpers.entity",
)

_SAVED_HA_MODULES: dict[str, types.ModuleType | None] = {}


def _build_ha_stub_modules() -> dict[str, types.ModuleType]:
    """Fresh stand-ins covering everything sensor.py / binary_sensor.py import."""
    components = types.ModuleType("homeassistant.components")
    sensor_mod = types.ModuleType("homeassistant.components.sensor")
    binary_mod = types.ModuleType("homeassistant.components.binary_sensor")
    config_entries = types.ModuleType("homeassistant.config_entries")
    const = types.ModuleType("homeassistant.const")
    core = types.ModuleType("homeassistant.core")
    device_registry = types.ModuleType("homeassistant.helpers.device_registry")
    dispatcher = types.ModuleType("homeassistant.helpers.dispatcher")
    helpers = types.ModuleType("homeassistant.helpers")

    class StubEntity:
        def __init__(self) -> None:
            self.write_count = 0

        def async_write_ha_state(self) -> None:
            self.write_count += 1

        def async_on_remove(self, remove_callback) -> None:
            self.remove_callback = remove_callback

    class UnitOfTemperature:
        CELSIUS = "°C"

    def callback(func):
        func.__hass_callback__ = True
        return func

    def async_dispatcher_connect(_hass, _signal, target):
        return lambda: None

    sensor_mod.SensorEntity = StubEntity
    binary_mod.BinarySensorEntity = StubEntity
    config_entries.ConfigEntry = object
    const.UnitOfTemperature = UnitOfTemperature
    core.HomeAssistant = object
    core.callback = callback
    device_registry.DeviceInfo = dict
    dispatcher.async_dispatcher_connect = async_dispatcher_connect

    entity_helpers = types.ModuleType("homeassistant.helpers.entity")

    class StubEntityCategory(str, enum.Enum):
        CONFIG = "config"
        DIAGNOSTIC = "diagnostic"

    entity_helpers.EntityCategory = StubEntityCategory

    return {
        "homeassistant.components": components,
        "homeassistant.components.sensor": sensor_mod,
        "homeassistant.components.binary_sensor": binary_mod,
        "homeassistant.config_entries": config_entries,
        "homeassistant.const": const,
        "homeassistant.core": core,
        "homeassistant.helpers": helpers,
        "homeassistant.helpers.device_registry": device_registry,
        "homeassistant.helpers.dispatcher": dispatcher,
        "homeassistant.helpers.entity": entity_helpers,
    }


def _install_ha_entity_stubs() -> None:
    if not _SAVED_HA_MODULES:
        for name in _HA_MODULE_NAMES:
            _SAVED_HA_MODULES[name] = sys.modules.get(name)
    for name, module in _build_ha_stub_modules().items():
        sys.modules[name] = module


def _restore_ha_stubs() -> None:
    """Put back whatever the pre-test environment had under homeassistant.*."""
    for name, saved in _SAVED_HA_MODULES.items():
        if saved is None:
            sys.modules.pop(name, None)
        else:
            sys.modules[name] = saved


def _install_sensor_stubs_and_reload():
    """Install HA stubs and import a pristine sensor.py bound to them."""
    _install_ha_entity_stubs()
    sys.modules.pop("custom_components.telegraf_mqtt.sensor", None)
    return importlib.import_module("custom_components.telegraf_mqtt.sensor")


def _install_binary_sensor_stubs_and_reload():
    """Install HA stubs and import a pristine binary_sensor.py bound to them."""
    _install_ha_entity_stubs()
    sys.modules.pop("custom_components.telegraf_mqtt.binary_sensor", None)
    return importlib.import_module("custom_components.telegraf_mqtt.binary_sensor")


def _pop_integration_modules() -> None:
    sys.modules.pop("custom_components.telegraf_mqtt.sensor", None)
    sys.modules.pop("custom_components.telegraf_mqtt.binary_sensor", None)
