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

This split prevents boolean fields from being forced into the numeric sensor path and keeps the entity model consistent with Home Assistant semantics.

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

The repo is organized around a phased roadmap:

- Phase 0: scaffold and package layout
- Phase 1: generic parser baseline
- Phase 2: registry and live option behavior
- Phase 3: measurement-aware naming and metadata resolution
- Phase 4: boolean routing to binary sensor entities
- Phase 5: integration metadata and translation polish
- Phase 6: release packaging metadata
- Phase 7: repository hand-off and documentation readiness

## License

This project is distributed under the repository’s existing license terms.
