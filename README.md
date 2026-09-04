# Telegraf MQTT for Home Assistant

A Home Assistant custom integration that subscribes to MQTT topics, parses Telegraf JSON payloads, and exposes the resulting metric stream as Home Assistant sensor and binary sensor entities. One HA device per physical PC, fully translated, no YAML.

## Overview

This integration is designed for the Telegraf-to-Home-Assistant MQTT flow:

- Telegraf publishes JSON payloads over MQTT (the `mqtt` output plugin with the JSON serializer).
- The integration receives those messages via the official Home Assistant `mqtt` integration.
- The parser creates immutable metric descriptors with translation keys + placeholders (no hardcoded English strings).
- The registry deduplicates, tracks availability, and applies live overrides.
- The sensor and binary sensor platforms project the resolved state into Home Assistant.
- Every entity is grouped under a per-host device (`DeviceInfo`).

## Features

- MQTT topic-pattern subscription
- Parser-level metric descriptor construction with translation keys
- Generic fallback parsing with measurement-aware naming support
- Registry-backed deduplication and liveness tracking
- Live options updates for expiry, exclusions, and field overrides
- Sensor entities for numeric metrics
- Binary sensor entities for boolean metrics
- Config flow, options flow, and reconfigure flow (all UI, no YAML)
- Translatable entities, exceptions, and config forms
- HACS-ready packaging metadata
- Repairs for overlapping topic patterns and invalid persisted options
- Diagnostics download (redacted)

## Installation

### HACS

1. Add this repository as a custom repository in HACS:

   ```
   https://github.com/Gekko47/telegraf-to-hass-mqtt
   ```

2. Search for `Telegraf MQTT`.
3. Install the integration.
4. Restart Home Assistant.

See the [HACS custom repository documentation](https://www.hacs.xyz/docs/use/download/download/#to-download-a-custom-repository)
for how to add a repository outside the default HACS list.

### Manual installation

1. Copy the `custom_components/telegraf_mqtt` directory into your Home Assistant
   `custom_components` directory.
2. Restart Home Assistant.

### Requirements

- Home Assistant **2026.6.x or newer** (auto-detected; not a HACS release, so it
  updates in place from the repo).
- The official Home Assistant [`mqtt`](https://www.home-assistant.io/integrations/mqtt/)
  integration configured and connected to your broker (this integration uses it and
  adds no MQTT client of its own).
- No other Python dependencies are required.

### Upgrade

Because the integration is installed from a repository (HACS) or manually, upgrade by
updating from HACS or re-copying the directory, then restarting Home Assistant. Existing
entities, devices, and configuration are preserved across upgrades.

## Removal

1. Settings -> Devices & Services -> **Telegraf MQTT** -> ... -> **Delete**.
2. Restart Home Assistant.
3. Manual install: also remove the `custom_components/telegraf_mqtt` directory.
4. If orphaned entries remain, remove only the Telegraf MQTT entries from the
   entity registry. Back up Home Assistant before any manual registry edit.

## Configure

After installation:

1. Open Settings -> Devices & Services.
2. Add Integration.
3. Select `Telegraf MQTT`.
4. Provide:
   - `topic_pattern` such as `telegraf/#`
   - `device_name`
   - optional `manufacturer`, `model`, `sw_version`

The integration also supports an options flow (Settings -> Devices & Services -> Telegraf MQTT
-> Configure) for live updates without removing the entry. Every option is documented below.

| Option | Type | Default | Description |
|---|---|---|---|
| `exclude_patterns` | list of strings | `[]` | Glob patterns matched against each metric's `unique_key`; matching metrics are **not** created. Example: `["mem_*", "swap_*"]`. |
| `field_overrides` | dict of `field → {key: value}` | `{}` | Override metadata per field, layered on top of the built-in heuristics. Supported keys: `native_unit`, `device_class`, `state_class`, `entity_category`. |
| `expire_after` | int seconds | `120` | Time after the last message before a metric is marked unavailable. |
| `enable_cleanup` | bool | `True` | When on, stale metrics (unavailable for `cleanup_delay`) are removed. |
| `cleanup_delay` | int seconds | `2592000` (30 days) | How long a metric must be unavailable before cleanup considers it. |
| `delete_delay` | int seconds | `5184000` (60 days) | How long an empty device must stay empty before it is removed. |
| `min_active_metrics` | int | `1` | Cleanup is a no-op for a device with fewer than this many active metrics. |

The options flow coerces invalid persisted values to the documented default and raises
a Repairs issue so the user can correct them from the UI without the entry failing to
set up.

### Reconfigure

Settings -> Devices & Services -> **Telegraf MQTT** -> ... -> **Reconfigure** opens a
form to change the topic pattern, device name, manufacturer, model, or software version
without removing and re-adding the entry. The integration reloads the config entry to
swap the MQTT subscription; the previous subscription is unsubscribed cleanly during
unload. Re-configuring to a topic pattern already used by another entry aborts with a
duplicate-topic error.

## Reference

This section documents what the integration supports, what it doesn't, how data arrives,
and what to do when something looks wrong.

### Data update

The integration is `local_push` — entities update on the next Telegraf message.
The integration never polls.

What triggers a state write:

- The value of a metric changed compared to the last received value.
- The availability flipped (Active <-> Unavailable). The transition is logged once
  at DEBUG (INFO or below) per flip, so repeated expiry ticks never spam the log.
- A new metric is discovered on an already-known device (new entity, new state).

What does **not** trigger a state write:

- A re-received identical value (the registry detects no real change).
- A timestamp-only update on the same value (the registry detects no real change).
- An excluded metric (it's never inserted into the registry, so it can never
  emit a signal).

Expiry is a periodic timer (interval = `max(5, min(expire_after, 30))` seconds).
At each tick the registry flips a metric to unavailable if `now - last_updated >
expire_after`. Recovery is immediate on the next matching message. The 5-second
floor keeps the tick -- a synchronous full-registry scan on the event loop --
from adding loop latency at fleet scale; staleness is still measured against
`expire_after`, so very small values are only marked unavailable up to 5s late.

### Supported devices

The integration accepts any Telegraf output. It does not care about the host OS
or Telegraf version, only that the message is valid JSON of the documented shape
(see `SPEC.md` for the wire format). In practice, anything that can run a
Telegraf agent with the `mqtt` output plugin and the JSON serializer works.

Common measurement names and the Telegraf input plugin they typically come from:

| Measurement | Typical input plugin | Notes |
|---|---|---|
| `cpu` | `cpu` | `cpu-total` aggregates the whole CPU; per-core metrics arrive when per-CPU reporting is enabled. |
| `mem` | `mem` | Percentage fields are state class `measurement`; byte counts are `total_increasing`. |
| `disk` | `disk` | Whole measurement is diagnostic. The root disk is rendered as `Disk Root <field>`. |
| `diskio` | `diskio` | Per-device I/O counters; `read_bytes` / `write_bytes` render as `7.38 GB` via the `data_size` device class. |
| `net` | `net` | Disambiguated by the `interface` tag. |
| `sensors` | `lm_sensors` | Disambiguated by `chip` + `feature`. CPU package temperature is rendered as `CPU Package Temperature`. |
| `system` | `system` | `n_cpus`, `n_users`, `uptime_format` are static identity; `load1`/`load5`/`load15` round to two decimals; `uptime` is `total_increasing` / `duration`. |
| `kernel` / `kernel_vmstat` | `kernel` (legacy) / `kernel_vmstat` (modern) | Counters and gauges from `/proc/vmstat` and friends. |
| `processes` | `processes` | Process counts, always diagnostic. |
| `swap` | `swap` | Similar shape to `mem`; `in` / `out` are byte counters. |
| `ping` | `ping` | `*_response_ms` fields render as `9.5 ms` / `123 ms` (adaptive precision). |
| `smart` | `smart` | Per-device S.M.A.R.T. attributes; `temp_c` is temperature, `power_on_hours` is duration. |
| `docker` / `docker_container_*` | `docker` | Per-container metrics; entity names include the container name. |
| `wireless` | `wireless` | `level` / `noise` are `dBm`; packet counters are `total_increasing`. |
| `zfs` / `zfs_pool` / `zfs_dataset` | `zfs` | Pool + dataset metrics; entity names include the pool / dataset. |
| `net_response` | `net_response` | TCP / UDP response time + success code. |
| `http_response` | `http_response` | HTTP response time + status code. |
| `ipmi_sensor` | `ipmi_sensor` | Per-sensor BMC metrics; the `unit` tag drives the device class. |
| `interrupts` / `soft_interrupts` | `interrupts` | Per-IRQ counters from `/proc/interrupts`. |
| `nvidia_gpu` | `nvidia-smi` (via Telegraf's exec input) | Field names vary by exec script. |
| `battery` | `exec`/`upower` (custom) | Field names vary. |

Unknown measurement names fall back to the generic parser and use the
`generic_field` translation key. The user sees `<Title-cased Field Name>` in
English, or the localised equivalent.

### Supported functions

The integration ships two entity platforms:

- `sensor` — every numeric (int / float) field becomes a `sensor` entity.
- `binary_sensor` — every boolean field becomes a `binary_sensor` entity.

What the integration does **not** ship:

- **No derived / computed sensors.** Calculated values (usage percentage from two
  fields, duration formatting, rate-of-change) should use Home Assistant's
  built-in Template Helpers — see
  [Template integration](https://www.home-assistant.io/integrations/template/).
- **No Number / Select / Switch platforms.** v1 is sensor + binary_sensor only.
- **No services or device triggers.** v1 is read-only telemetry.

The parser is platform-agnostic: the `MetricDescriptor` is the contract, and
extending with new entity platforms is a platform-only change.

### Examples

**Example 1 — a CPU usage metric on a Linux host**

Telegraf publishes:

```json
{
  "name": "cpu",
  "tags": {"host": "host-a", "cpu": "cpu-total"},
  "fields": {"usage_idle": 88.4, "usage_user": 7.1},
  "timestamp": 1721664000
}
```

Result:

- A device is auto-created under the entry's `device_name` with `manufacturer`,
  `model`, and `sw_version` from the config flow.
- One sensor entity per field:
  - `sensor.host_a_cpu_cpu_total_usage_idle` — `CPU CPU Total Usage Idle`
    (icon: `mdi:cpu-64-bit`), unit inferred as none, state class `measurement`.
  - `sensor.host_a_cpu_cpu_total_usage_user` — `CPU CPU Total Usage User`.

**Example 2 — a disk usage metric (diagnostic, disabled by default)**

```json
{
  "name": "disk",
  "tags": {"host": "host-a", "path": "/", "fstype": "ext4"},
  "fields": {"used_percent": 63.5, "free": 128849018880},
  "timestamp": 1721664000
}
```

Result:

- Two sensor entities, both **disabled by default** (disk is diagnostic):
  - `sensor.host_a_disk_root_used_percent` — `Disk Root Used Percent`
    (icon: `mdi:harddisk`), unit `%`, entity category `diagnostic`, **off by default**.
  - `sensor.host_a_disk_ext4_root_free` — `Disk Root Free`, unit none.
- Enable them from Settings -> Devices & Services -> Entities -> select each entity
  -> "Enable entity".

**Example 3 — a battery binary sensor**

```json
{
  "name": "battery",
  "tags": {"host": "host-a", "state": "discharging"},
  "fields": {"percentage": 87.0, "voltage": 11.4},
  "timestamp": 1721664000
}
```

Result:

- A `binary_sensor` for the boolean field if the measurement contains one.
- Two sensors for the numeric fields:
  - `sensor.host_a_battery_discharging_percentage` — `Battery Percentage`,
    device class `battery`.
  - `sensor.host_a_battery_discharging_voltage` — `Battery Voltage`,
    device class `voltage`.

### Use cases

- **Single-server monitor** — one Telegraf agent on the host running Home Assistant.
  Add the integration once with `topic_pattern: telegraf/#` and all metrics appear
  under one device.
- **Multi-host fleet dashboard** — many Telegraf agents, all publishing to the same
  MQTT topic tree. A single config entry subscribes to the union pattern; the
  integration creates one device per host automatically (no restart, no reload).
- **Laptop-on-the-go** — Telegraf publishes the hostname as the `host` tag. The
  integration creates a device the first time the laptop is seen and cleans it up
  after `delete_delay` of inactivity.
- **Prometheus-style post-processing** — use Home Assistant's `statistics` integration
  and Template Helpers on top of the sensor entities for long-term retention, rates,
  and aggregations.

### Known limitations

- **No derived sensors in the integration.** Use Template Helpers instead.
- **No MQTT client of the integration's own.** You must configure the official `mqtt`
  integration; this integration only subscribes to MQTT topics through it.
- **No per-measurement auth.** All Telegraf messages on the topic pattern are
  treated as trusted.
- **No services, no device triggers.** v1 is read-only telemetry.
- **Offline devices are kept.** The integration never removes a device that has
  at least one metric; it only marks the affected entities unavailable. Empty +
  aged devices (`delete_delay`) are pruned (Stale Devices).
- **No reconnection logic of its own.** When the broker drops, the official
  `mqtt` integration auto-resubscribes; this integration's subscription rides on
  top.

### Troubleshooting

| Symptom | Likely cause | Fix |
|---|---|---|
| Entities show "unavailable" | Telegraf stopped publishing, or `expire_after` is too short for your update interval | Raise `expire_after` in the options flow. Verify with `mosquitto_sub` that messages still arrive. |
| Duplicate entities appearing | Two config entries subscribed to overlapping topic patterns | Open Repairs -> "Overlapping topic patterns" -> change one pattern so they cover distinct streams. |
| No entities at all | (a) Telegraf is not using the JSON serializer, (b) the topic pattern doesn't match, (c) the broker isn't connected | Verify with `mosquitto_sub -t 'telegraf/#' -v` that messages arrive. Confirm the config-flow topic matches. |
| An entity I expect is missing | The `unique_key` matches one of your `exclude_patterns` globs | Open the options dialog, clear `exclude_patterns`, save. |
| Options dialog rejects my number | Persisted value is non-numeric / negative | Open Repairs -> "Invalid Telegraf MQTT option(s)" -> correct the value. |
| Reconfigure doesn't take effect | The new topic pattern matches another entry's pattern | Open Repairs -> "Overlapping topic patterns" -> pick a distinct pattern. |
| Diagnostics show only the broker config | Integration hasn't seen any messages yet | Wait for at least one Telegraf message, then re-download. |
| Entities are off by default | They are diagnostic (disk, system, lifecycle) | Open Settings -> Devices & Services -> Entities, enable the ones you want. |

### Entity behavior

- **Display name** is fully translation-driven. Every entity carries a
  `translation_key` and `translation_placeholders`; Home Assistant renders the
  localised string. There are no hardcoded English strings in the entity layer.
- **Icon** is set per-entity from the descriptor's inferred icon key, mapped to
  Material Design Icons (`mdi:cpu-64-bit`, `mdi:memory`, `mdi:harddisk`, …).
- **Device class** (sensor entities only) is inferred from the field name (`temperature`, `voltage`,
  `power`, `energy`, `battery`) and may be overridden via the
  `field_overrides` option. Binary sensor entities do not get a device class
  from the parser.
- **State class** (sensor entities only) is `total_increasing` for byte counters and uptime,
  `measurement` for everything else numeric, and unset for boolean / string
  fields. Binary sensor entities do not carry a state class.
- **Unit** is taken from the parser's heuristic and may be overridden via
  `field_overrides`.
- **Entity category** is `diagnostic` for disk usage, system identity,
  uptime, boot time, load averages, and process counts. All other entities
  have no category.
- **Disabled by default** — entities whose `entity_category == diagnostic`
  are added to the entity registry as disabled. Enable them per-entity from
  Settings -> Devices & Services -> Entities.

## Architecture

The integration uses a layered model:

1. MQTT transport layer
2. Telegraf JSON parser layer
3. `MetricDescriptor` contract layer
4. Registry layer for deduplication and availability tracking
5. Entity layer for sensor / binary sensor projection

The parser layer remains decoupled from Home Assistant entity implementation details.

## Development

The repository uses a local pytest-based regression suite.

### Run tests

```powershell
.\.venv\Scripts\python -m pytest -q
```

### Fast sharded run (optional)

For the TDD loop, shard the suite with `pytest-xdist` (install first if it
is not already in your dev venv — it is not declared in `pyproject.toml`):

```powershell
# One-time: install the sharding plugin into the dev venv
.\.venv\Scripts\python -m pip install pytest-xdist

# TDD loop: sharded, no coverage (fastest, ~1 min on 12 cores)
.\.venv\Scripts\python -m pytest -n auto

# Local pre-commit: sharded, with coverage (~1 min)
.\.venv\Scripts\python -m pytest -n auto --cov=custom_components.telegraf_mqtt

# CI / full gate: sequential, with coverage (default, ~2 min)
.\.venv\Scripts\python -m pytest -q
```

The sharded and sequential runs give identical results — the only difference
is wall time (≈2× faster with coverage, ≈3-4× faster without).

### Coverage

The suite runs with a **100% line-coverage gate** on `custom_components/telegraf_mqtt`
(the stated floor is >=90%; the current measured floor is 100%). Verify the gate on every
change with:

```powershell
.\.venv\Scripts\python -m pytest --cov=custom_components.telegraf_mqtt --cov-report=term-missing --no-cov-on-fail
```

### Quality gates (Phase 10 — 🏆 Platinum)

The integration targets Home Assistant's full quality scale, with **strict typing** as
the final gate. Two extra tools run alongside the test suite on every change.

| Gate | Command | Notes |
|---|---|---|
| Lint + format | `ruff check . && ruff format --check .` | Enforced in CI via the `prek` pre-commit framework. Auto-fixable: `ruff check --fix .` and `ruff format .`. |
| Type check | `mypy --strict custom_components` | Enforced in CI. Pinned via `pyproject.toml` `[tool.mypy]`. All 27 source files must report `Success: no issues found`. |
| Tests + 100% coverage | `pytest --cov=custom_components.telegraf_mqtt` | Enforced in CI. The Phase 10 / Platinum exit criterion: "Type checker runs in CI in strict mode and passes." |

The `prek` (or `pre-commit`) install runs the same `ruff` checks locally before you push:

```powershell
.\.venv\Scripts\python -m pip install prek
.\.venv\Scripts\prek install
.\.venv\Scripts\prek run --all-files
```

### Main implementation areas

- `custom_components/telegraf_mqtt/parser.py` — JSON payload dispatch
- `custom_components/telegraf_mqtt/parsers/` — measurement-specific parser entrypoints and generic fallback behavior
- `custom_components/telegraf_mqtt/naming.py` — translation-key, category, and icon-key resolution
- `custom_components/telegraf_mqtt/icons.py` — MDI icon table
- `custom_components/telegraf_mqtt/translations_strings.py` — in-process English translator mirror
- `custom_components/telegraf_mqtt/exceptions.py` — translatable exceptions
- `custom_components/telegraf_mqtt/registry.py` — registry state, deduplication, and live options application
- `custom_components/telegraf_mqtt/sensor.py` — numeric metric entity projection
- `custom_components/telegraf_mqtt/binary_sensor.py` — boolean metric entity projection
- `custom_components/telegraf_mqtt/config_flow.py` — config, options, and reconfigure UI flow
- `custom_components/telegraf_mqtt/diagnostics.py` — diagnostics download (redacted)
- `custom_components/telegraf_mqtt/repairs.py` — Repairs issues
- `custom_components/telegraf_mqtt/snoop.py` — post-setup auto-discover listener

## Roadmap alignment

The repo is organized around a phased roadmap (see `.cline/ROADMAP.md`):

- [x] Phase 0: scaffolding, packaging metadata, CI, and the HA 2026.6.x platform floor
- [x] Phase 1: multi-device core pipeline (one Home Assistant device per Telegraf host)
- [x] Phase 2: options and availability behavior
- [x] Phase 3: measurement-aware naming and metadata resolution
- [x] Phase 4: units, statistics, and binary sensor projection
- [x] Phase 5: 🥉 Bronze quality-scale gate
- [x] Phase 6: intelligent cleanup and device lifecycle
- [x] Phase 7: diagnostics and repairs
- [x] Phase 8: 🥈 Silver gate (reliability hardening)
- [x] Phase 9: 🥇 Gold gate (translations, icons, docs depth)
- [x] Phase 10: 🏆 Platinum gate (strict typing, mypy --strict in CI) and HACS release (1.2.0)

## License

This project is distributed under the repository’s existing license terms.
