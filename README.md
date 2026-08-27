# Telegraf MQTT for Home Assistant

A Home Assistant custom integration that subscribes to MQTT topics, parses Telegraf JSON payloads, and exposes the resulting metric stream as Home Assistant sensor and binary sensor entities.

## Overview

This integration is designed for the Telegraf-to-Home-Assistant MQTT flow:

- Telegraf publishes JSON payloads over MQTT
- the integration receives those messages
- the parser creates immutable metric descriptors
- the registry deduplicates, tracks availability, and applies live overrides
- the sensor and binary sensor platforms project the resolved state into Home Assistant

## Features

- MQTT topic-pattern subscription
- Parser-level metric descriptor construction
- Generic fallback parsing with measurement-aware naming support
- Registry-backed deduplication and liveness tracking
- Live options updates for expiry, exclusions, and field overrides
- Sensor entities for numeric metrics
- Binary sensor entities for boolean metrics
- Config flow and options flow support
- HACS-ready packaging metadata and translation strings

## Installation

### HACS

1. Add this repository as a custom repository in HACS.
2. Search for `Telegraf MQTT`.
3. Install the integration.
4. Restart Home Assistant.

### Manual installation

1. Copy the `custom_components/telegraf_mqtt` directory into your Home Assistant `custom_components` directory.
2. Restart Home Assistant.

## Removal

1. Settings -> Devices & Services -> **Telegraf MQTT** -> ... -> **Delete**.
2. Restart Home Assistant.
3. Manual install: also remove the `custom_components/telegraf_mqtt` directory.
4. If orphaned entries remain, remove only the Telegraf MQTT entries from the
   entity registry. Back up Home Assistant before any manual registry edit.
## Configure

After installation:

1. Open Settings → Devices & Services.
2. Add Integration.
3. Select `Telegraf MQTT`.
4. Provide:
   - `topic_pattern` such as `telegraf/#`
   - `device_name`
   - optional `manufacturer` and `model`

The integration also supports an options flow for:

- `exclude_patterns`
- `field_overrides`
- `expire_after`

## Supported payload model

The parser consumes Telegraf-style JSON payloads shaped like:

```json
{
  "name": "cpu",
  "tags": {
    "host": "host-name"
  },
  "fields": {
    "usage_idle": 88.4
  },
  "timestamp": 1721664000
}
```

The parser preserves the measurement, tags, field name, and value as an immutable descriptor object. That preserves the integration’s separation of concerns between parsing, registry state, and entity projection.

## Entity behavior

### Sensors

Numeric metrics are projected as Home Assistant `sensor` entities.

### Binary sensors

Boolean metrics are projected as Home Assistant `binary_sensor` entities.

This split is automatic: any Telegraf field whose value is a boolean (for
example `link_up` in a `net` measurement) becomes a `binary_sensor` entity,
while every numeric field stays on the `sensor` platform. No per-field
configuration is required.

Units are stored exactly as Telegraf reports them — byte counters remain raw
byte counts (`B`), never converted to KB/MB/GB — and each entity carries the
device class / state class pair Home Assistant's recorder accepts for
long-term statistics.

## Architecture

The integration uses a layered model:

1. MQTT transport layer
2. Telegraf JSON parser layer
3. `MetricDescriptor` contract layer
4. Registry layer for deduplication and availability tracking
5. Entity layer for sensor/binary sensor projection

The parser layer remains decoupled from Home Assistant entity implementation details.

## Development

The repository uses a local pytest-based regression suite.

### Run tests

```powershell
.\.venv\Scripts\python -m pytest -q
```

### Main implementation areas

- `custom_components/telegraf_mqtt/parser.py` — JSON payload dispatch
- `custom_components/telegraf_mqtt/parsers/` — measurement-specific parser entrypoints and generic fallback behavior
- `custom_components/telegraf_mqtt/naming.py` — phase-aware naming and entity metadata resolution
- `custom_components/telegraf_mqtt/registry.py` — registry state, deduplication, and live options application
- `custom_components/telegraf_mqtt/sensor.py` — numeric metric entity projection
- `custom_components/telegraf_mqtt/binary_sensor.py` — boolean metric entity projection
- `custom_components/telegraf_mqtt/config_flow.py` — config and options UI flow

## Roadmap alignment

The repo is organized around a phased roadmap (see `.cline/ROADMAP.md`):

- Phase 0: scaffolding, packaging metadata, CI, and the HA 2026.6.x platform floor
- Phase 1: multi-device core pipeline (one Home Assistant device per Telegraf host)
- Phase 2: options and availability behavior
- Phase 3: measurement-aware naming and metadata resolution
- Phase 4: units, statistics, and binary sensor projection
- Phase 5: 🥉 Bronze quality-scale gate
- Phase 6: intelligent cleanup and device lifecycle
- Phase 7: diagnostics and repairs
- Phase 8: 🥈 Silver gate (reliability hardening)
- Phase 9: 🥇 Gold gate (translations, icons, docs depth)
- Phase 10: 🏆 Platinum gate (strict typing) and HACS release

## License

This project is distributed under the repository’s existing license terms.
