"""Diagnostics payload tests (Phase 7).

Harness-free: ``async_get_config_entry_diagnostics`` is invoked
directly with fake entry/manager/runtime-data objects. The shape
is pinned against SPEC.md's "diagnostics" section: config, runtime,
parser stats, last-message metadata, options validity, per-device
measurements.

A separate test pins the redaction contract: the diagnostic payload
must never include the raw Telegraf payload, individual field values,
or the host identity. ``device_id`` is hashed and ``device_name`` is
omitted from the per-device block for the same reason.
"""

from __future__ import annotations

import asyncio
import hashlib
import json
from typing import ClassVar

from custom_components.telegraf_mqtt.const import (
    CONF_EXPIRE_AFTER,
    CONF_TOPIC_PATTERN,
    DOMAIN,
)
from custom_components.telegraf_mqtt.diagnostics import (
    async_get_config_entry_diagnostics,
)


class _FakeState:
    def __init__(self, measurement: str) -> None:
        self.descriptor = type("D", (), {"measurement": measurement})


class FakeRegistry:
    device_name = "Host One"
    last_any_metric = 995.0

    def __init__(self, measurements: list[str] | None = None) -> None:
        # ``_states`` is what the diagnostics module reads to gather
        # the per-device measurements list.
        self._states = {f"key_{m}": _FakeState(m) for m in (measurements or [])}

    def __len__(self) -> int:
        return len(self._states)


class FakeManager:
    def __init__(self) -> None:
        self.devices = {"host1": FakeRegistry(["cpu", "mem"])}
        self._expire_after = 120
        self._exclude_patterns = ("mem_*",)
        self._field_overrides = {"used_percent": {"native_unit": "%"}}
        self._cleanup_delay = 30 * 24 * 60 * 60
        self._delete_delay = 60 * 24 * 60 * 60
        self._enable_cleanup = True
        self._min_active_metrics = 1

    def _clock(self) -> float:
        return 1000.0


class FakeParserStats:
    received = 5
    parsed = 4
    dropped_invalid_json = 1
    dropped_unsupported_shape = 0
    unknown_measurement_fallbacks = 0
    # ``last_message`` mirrors the keys ``ParserStats`` actually stores
    # (see ``parser.py``). The diagnostics layer forwards this dict
    # verbatim, so adding extra keys here (a fake raw payload / a fake
    # field value / a fake host identity) gives the redaction test
    # something to look for. The integration code never includes any
    # of these in the real ``last_message``; if it ever does, the
    # assertion below will fail.
    last_message: ClassVar[dict] = {
        "topic": "telegraf/host1/cpu",
        "byte_length": 142,
        "dropped_reason": None,
        "measurement": "cpu",
        # Fake raw payload, field value, and host identity -- none of
        # which should ever appear in the serialised diagnostics.
        "raw_payload": '{"name":"cpu","fields":{"usage_user":42.0},"tags":{"host":"host-a"}}',
        "value": 42.0,
        "host": "host-a",
    }


class FakeRuntimeData:
    manufacturer = "Acme"
    model = "PC-1"
    manager = FakeManager()
    parser_stats = FakeParserStats()


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


def test_diagnostics_contains_every_spec_field() -> None:
    """SPEC.md diagnostics section: config, parser stats, last-message
    metadata, dropped-payload counts, known measurements/entities.
    """
    data = asyncio.run(async_get_config_entry_diagnostics(None, FakeEntryWithRuntime()))
    # Top-level shape
    assert set(data) >= {"entry", "config", "runtime", "options_validity"}
    assert data["entry"] == {
        "entry_id": "entry-1",
        "domain": DOMAIN,
        "title": "Telegraf",
        "unique_id": "telegraf/#",
    }
    assert data["config"] == {
        "data": {CONF_TOPIC_PATTERN: "telegraf/#"},
        "options": {"expire_after": 60},
    }
    runtime = data["runtime"]
    # Manufacturer/model are part of SPEC's "known entities" surface
    # in a broad sense (identifies the device family).
    assert runtime["manufacturer"] == "Acme"
    assert runtime["model"] == "PC-1"
    # Manager: per-device diagnostics includes the measurement list.
    manager_block = runtime["manager"]
    assert manager_block["device_count"] == 1
    (host1,) = manager_block["devices"]
    # ``device_id`` is hashed in the redacted diagnostics payload
    # so the underlying Telegraf host name (encoded in the slug)
    # is never exposed. The digest is stable -- the same input
    # always produces the same output -- so correlatability is
    # preserved.
    expected_device_id = hashlib.sha256(b"host1").hexdigest()[:16]
    assert host1["device_id"] == expected_device_id
    # ``device_name`` is omitted from the redacted payload.
    assert "device_name" not in host1
    assert host1["measurements"] == ["cpu", "mem"]
    assert host1["metric_count"] == 2
    assert host1["last_any_metric_age_seconds"] == 5.0
    assert manager_block["options"]["expire_after"] == 120
    assert manager_block["options"]["enable_cleanup"] is True
    # Parser stats: counters + last_message snapshot.
    ps = runtime["parser_stats"]
    assert ps["received"] == 5
    assert ps["parsed"] == 4
    assert ps["dropped_invalid_json"] == 1
    assert ps["unknown_measurement_fallbacks"] == 0
    assert ps["last_message"]["topic"] == "telegraf/host1/cpu"
    assert ps["last_message"]["byte_length"] == 142
    # options_validity is per-option booleans.
    validity = data["options_validity"]
    assert validity[CONF_EXPIRE_AFTER] is True
    assert validity["cleanup_delay"] is True
    assert validity["enable_cleanup"] is True


def test_diagnostics_never_leaks_raw_payload() -> None:
    """The redaction contract: no raw payload, no field value, no
    host identity may appear anywhere in the diagnostic payload.
    """
    data = asyncio.run(async_get_config_entry_diagnostics(None, FakeEntryWithRuntime()))
    text = json.dumps(data, default=str)
    # Forbidden strings (would indicate a leak). Each of these is
    # intentionally seeded into the fake ``last_message`` so the
    # assertion fails if the diagnostics layer ever starts forwarding
    # that key -- not just if a generic substring shows up.
    forbidden_substrings = (
        # The fake raw payload: every byte of this string would leak
        # in a real downloaded diagnostics file. A regression that
        # adds a ``raw_payload`` / ``payload`` / ``value_bytes`` key
        # to ``last_message`` will surface here.
        '{"name":"cpu","fields":{"usage_user":42.0},"tags":{"host":"host-a"}}',
        "raw_payload",
        "value_bytes",
        # The fake field value (``42.0`` from the seeded payload) --
        # field values are integration data, not user debugging
        # context and must never appear in a downloaded file.
        "usage_user",
        # The fake host identity -- the user's machine name. The
        # underlying Telegraf host (e.g. "host-a") must never
        # appear in a downloaded diagnostics file. ``device_id`` is
        # hashed and ``device_name`` is omitted from the per-device
        # block to enforce that.
        "host-a",
    )
    for forbidden in forbidden_substrings:
        assert forbidden not in text, f"diagnostics leaked {forbidden!r}"
    # The ``last_message`` key set must remain closed: a regression
    # that widens it (e.g. adds a "raw" / "payload" / "host" key)
    # would break the redaction contract.
    last_msg = data["runtime"]["parser_stats"]["last_message"]
    assert set(last_msg) == {
        "topic",
        "byte_length",
        "dropped_reason",
        "measurement",
    }, f"last_message key set widened: {sorted(last_msg)}"
    # Per-device entries: the raw ``device_id`` (a slug derived from
    # the Telegraf ``host`` tag) and the ``device_name`` are both
    # omitted / redacted so the host identity never appears in a
    # downloaded file. ``device_id`` is replaced with a stable
    # SHA-256 digest so the operator can still correlate devices
    # within a single download.
    devices = data["runtime"]["manager"]["devices"]
    for entry in devices:
        assert "device_name" not in entry, "device_name must be omitted from the redacted diagnostics"
        assert "host1" not in entry["device_id"], "raw device_id slug must not appear in the redacted diagnostics"
        # ``Host One`` echoes the user-chosen display name; the
        # underlying Telegraf host name ("host-a") is what
        # the redaction contract protects, and the user-chosen
        # display name can echo the host in practice.
        assert "Host One" not in json.dumps(entry)


def test_diagnostics_omits_runtime_block_without_runtime_data() -> None:
    data = asyncio.run(async_get_config_entry_diagnostics(None, FakeEntryWithoutRuntime()))
    assert "runtime" not in data
    assert data["entry"]["entry_id"] == "entry-2"
    # options_validity is always present, even without runtime data.
    assert "options_validity" in data


def test_diagnostics_is_json_serializable() -> None:
    """The real HA downloads the diagnostics as JSON -- a non-serializable
    value would break the download flow. Pin it.
    """
    data = asyncio.run(async_get_config_entry_diagnostics(None, FakeEntryWithRuntime()))
    # NO ``default=str`` here. The real HA download flow serialises
    # the diagnostics with plain ``json.dumps``; a non-serialisable
    # value (e.g. a ``set`` or a custom class) must raise
    # ``TypeError`` here the same way it would in production.
    payload = json.dumps(data)
    assert "telegraf_mqtt" in payload
