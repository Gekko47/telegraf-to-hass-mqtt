"""Diagnostics payload tests (harness-free; the coroutine is invoked directly)."""

from __future__ import annotations

import asyncio

from custom_components.telegraf_mqtt.const import CONF_TOPIC_PATTERN, DOMAIN
from custom_components.telegraf_mqtt.diagnostics import async_get_config_entry_diagnostics


class FakeRegistry:
    device_name = "Host One"
    last_any_metric = 995.0

    def __len__(self) -> int:
        return 2


class FakeManager:
    def __init__(self) -> None:
        self.devices = {"host1": FakeRegistry()}

    def _clock(self) -> float:
        return 1000.0


class FakeRuntimeData:
    manufacturer = "Acme"
    model = "PC-1"
    manager = FakeManager()


class FakeEntryWithRuntime:
    entry_id = "entry-1"
    domain = DOMAIN
    title = "Telegraf"
    unique_id = "telegraf/#"

    def __init__(self) -> None:
        self.data = {CONF_TOPIC_PATTERN: "telegraf/#"}
        self.options = {"expire_after": 60}
        self.runtime_data = FakeRuntimeData()


class FakeEntryWithoutRuntime:
    entry_id = "entry-2"
    domain = DOMAIN
    title = "Telegraf"
    unique_id = "telegraf/2/#"

    def __init__(self) -> None:
        self.data = {CONF_TOPIC_PATTERN: "telegraf/2/#"}
        self.options = {}


def test_diagnostics_reports_config_and_redacted_runtime_snapshot() -> None:
    data = asyncio.run(async_get_config_entry_diagnostics(None, FakeEntryWithRuntime()))
    assert data["entry_id"] == "entry-1"
    assert data["domain"] == DOMAIN
    assert data["unique_id"] == "telegraf/#"
    assert data["data"] == {CONF_TOPIC_PATTERN: "telegraf/#"}
    assert data["options"] == {"expire_after": 60}
    assert data["runtime"]["manufacturer"] == "Acme"
    assert data["runtime"]["model"] == "PC-1"
    assert data["runtime"]["devices"]["host1"] == {
        "device_name": "Host One",
        "metrics": 2,
        "last_any_metric_age_seconds": 5.0,
    }


def test_diagnostics_omits_runtime_block_without_runtime_data() -> None:
    data = asyncio.run(async_get_config_entry_diagnostics(None, FakeEntryWithoutRuntime()))

    assert "runtime" not in data
    assert data["entry_id"] == "entry-2"
