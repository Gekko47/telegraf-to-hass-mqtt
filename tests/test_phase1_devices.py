"""Phase 1 unit tests: multi-device identity, routing, and isolation guardrails."""

from __future__ import annotations

import ast
import json
from pathlib import Path

from custom_components.telegraf_mqtt.parser import TelegrafParser
from custom_components.telegraf_mqtt.registry import DeviceManager

_COMPONENT_DIR = Path(__file__).resolve().parents[1] / "custom_components" / "telegraf_mqtt"

CPU_PAYLOAD = json.dumps(
    {
        "name": "cpu",
        "tags": {"host": "CachyOS Gekko", "cpu": "cpu-total"},
        "fields": {"usage_idle": 88.4},
        "timestamp": 1700000000,
    }
)

MEM_PAYLOAD_NO_HOST = json.dumps(
    {
        "name": "mem",
        "tags": {},
        "fields": {"used_percent": 41.2},
        "timestamp": 1700000000,
    }
)


def test_parser_sets_raw_device_id_from_host_tag() -> None:
    """The parser records the raw host tag; the manager owns the final slug."""
    descriptor = TelegrafParser().parse(CPU_PAYLOAD)[0]
    assert descriptor.device_id == "CachyOS Gekko"


def test_parser_leaves_device_id_empty_without_host() -> None:
    descriptor = TelegrafParser().parse(MEM_PAYLOAD_NO_HOST)[0]
    assert descriptor.device_id == ""


def test_manager_routes_descriptors_by_their_own_host() -> None:
    """A payload yielding descriptors for two hosts lands on two registries."""
    manager = DeviceManager()
    combined_payload = json.dumps(
        {
            "fields": {"usage_idle": 88.4},
            "name": "mixed",
            "tags": {"host": "server02"},
            "timestamp": 1700000000,
        }
    )
    fake_descriptors = TelegrafParser().parse(CPU_PAYLOAD) + TelegrafParser().parse(combined_payload)

    class _MixedParser:
        @staticmethod
        def parse(payload: str | bytes) -> list:
            return fake_descriptors

    manager.process_message("telegraf/data", "{}", parser=_MixedParser())

    assert set(manager.devices) == {"cachyos_gekko", "server02"}
    assert manager.get_metric("cachyos_gekko:mixed_usage_idle") is None
    state = manager.get_metric("cachyos_gekko:cpu_cpu_total_usage_idle")
    assert state is not None and state.value == 88.4


def test_manager_slugs_host_into_stable_device_id() -> None:
    manager = DeviceManager()
    discovered: list[tuple[str, str]] = []

    for _ in range(2):
        manager.process_message(
            "telegraf/data",
            CPU_PAYLOAD,
            parser=TelegrafParser(),
            on_new_device=lambda device_id, name: discovered.append((device_id, name)),
        )

    assert set(manager.devices) == {"cachyos_gekko"}
    assert discovered == [("cachyos_gekko", "CachyOS Gekko")]
    state = manager.get_metric("cachyos_gekko:cpu_cpu_total_usage_idle")
    assert state is not None
    assert state.descriptor.device_id == "cachyos_gekko"
    assert state.device_name == "CachyOS Gekko"


def test_manager_falls_back_to_topic_root_without_host() -> None:
    manager = DeviceManager()
    manager.process_message("telegraf/server05/mem", MEM_PAYLOAD_NO_HOST, parser=TelegrafParser())

    assert set(manager.devices) == {"telegraf"}
    assert manager.keys() == ("telegraf:mem_used_percent",)


def test_manager_uses_default_identity_when_no_host_and_no_topic() -> None:
    manager = DeviceManager(device_id="fallback", device_name="Fallback PC")
    manager.process_message("", MEM_PAYLOAD_NO_HOST, parser=TelegrafParser())

    assert set(manager.devices) == {"fallback"}
    assert manager.get_metric("fallback:mem_used_percent") is not None


def test_composite_keys_are_stable_across_reprocessing() -> None:
    manager = DeviceManager()
    for _ in range(3):
        manager.process_message("telegraf/data", CPU_PAYLOAD, parser=TelegrafParser())
    assert manager.keys() == ("cachyos_gekko:cpu_cpu_total_usage_idle",)


def test_parser_layer_never_imports_homeassistant() -> None:
    """Architecture guardrail: parser layer stays HA-free (AGENTS.md)."""
    parser_files = [_COMPONENT_DIR / "parser.py", *(_COMPONENT_DIR / "parsers").glob("*.py")]
    assert len(parser_files) >= 9

    for path in parser_files:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                module_names = [alias.name for alias in node.names]
            elif isinstance(node, ast.ImportFrom):
                module_names = [node.module or ""]
            else:
                continue
            for module_name in module_names:
                assert not module_name.startswith("homeassistant"), f"{path.name} must not import {module_name}"
