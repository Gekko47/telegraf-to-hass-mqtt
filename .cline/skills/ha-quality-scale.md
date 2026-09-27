# ha-quality-scale

## Purpose

Keep the integration compliant with Home Assistant's quality-scale rules and
brand expectations. This integration is at the **🏆 Platinum** gate, so
regressions here are release blockers, not nits.

## When to use

- Any change to `manifest.json`, the config flow, the options flow, or entity
  setup/teardown.
- Adding or changing an entity's `DeviceInfo`, category, or availability.
- Touching diagnostics, repairs, cleanup, or unload behaviour.
- Reviewing a change for HA-brand compliance before merge.

## Scope

In scope: `manifest.json`, `__init__.py` (setup/unload), `config_flow.py`,
`diagnostics.py`, `repairs.py`, `binary_sensor.py` / `sensor.py` lifecycle,
`registry.py` cleanup and device pruning.

Out of scope: which option exists (→ `config-options-review`); parser metadata
(→ `adding-telegraf-parsers`); tests (→ `test-authoring`).

## The current declared state

`custom_components/telegraf_mqtt/manifest.json`:

| Field | Value | Note |
|---|---|---|
| `config_flow` | `true` | UI config required |
| `dependencies` | `["mqtt"]` | the `mqtt` integration must be set up first |
| `iot_class` | `local_push` | correct — data arrives pushed over the broker, no polling |
| `loggers` | `["custom_components.telegraf_mqtt"]` | log level configurable from the UI |
| `requirements` | `[]` | no third-party deps; adding one is a Platinum regression |
| `version` | `1.5.0` | must match `pyproject.toml` and a `## [X.Y.Z]` heading in `CHANGELOG.md`. `hacs.json` carries **no** version field -- only `name` and the `homeassistant` floor. |

CI validates this with `hassfest` (`.github/workflows/hassfest.yml`) and HACS
with `.github/workflows/hacs.yml`. Run them locally before a release.

## Lifecycle contract

```
async_setup_entry
  ├─ create the snoop listener (if auto_discover)  → park on runtime_data
  ├─ forward platforms
  └─ register the periodic expiry tick
async_unload_entry
  ├─ unsubscribe MQTT
  ├─ release the snoop teardown handle (runtime_data.unsubscribe_snoop)
  └─ stop the periodic tick
```

Rules that are actually enforced by tests:

- **Every teardown handle is parked on `entry.runtime_data`.** A handle held in
  a closure or a module global leaks the subscription across reloads. This is
  tested, not aspirational.
- **Unload is symmetric with setup.** If setup registers it, unload releases
  it — no exceptions, including on the `device_id_strategy` reload path.
- **No blocking I/O on the event loop.** The periodic scan is O(devices ×
  metrics) and shares the loop with all of Home Assistant, which is why the
  cadence is floored at `MIN_EXPIRY_TICK_SECONDS` and capped at
  `MAX_EXPIRY_TICK_SECONDS`.

## Diagnostics redaction contract

`diagnostics.py` is a **user-facing debug surface** that gets attached to
public GitHub issues. The contract, in the module's own words:

- The raw Telegraf payload is **never** stored.
- Field values are **never** included.
- The host identity (the user's machine name) is **never** included. Device
  IDs are replaced by `_hash_device_id()` — a 16-char SHA-256 prefix that is
  stable enough to correlate devices within one download and opaque enough
  that the machine name never leaves the integration.
- Byte length **is** included (it answers "is the broker sending reasonable
  amounts?" and reveals nothing).

`ParserStats.last_message` is a single-slot ring buffer holding only
`topic` / `byte_length` / `dropped_reason` / `measurement`. A battery
percentage or MAC address is the integration's data, not the user's debugging
context — keep it that way.

The payload shape is deliberately stable: config, runtime, parser stats,
last-message metadata, options validity, per-device state. `tests/
test_diagnostics.py::test_diagnostics_contains_every_spec_field` pins it.

## Repairs issues

Seven issues ship today. Each **auto-resolves** when the underlying condition
clears, and each has defensive guards for missing `runtime_data`, missing
`manager`, and a missing issue registry — all covered by tests.

| Issue id | Condition |
|---|---|
| `no_traffic_on_topic` | nothing received on the configured pattern |
| `device_id_collision` | two host tags slug-collide **within** one entry |
| `device_id_conflict` | two **config entries** produce the same slug |
| `overlap_topic_patterns` | two entries' topic patterns overlap |
| `invalid_persisted_option` | a persisted option is not valid for this version |
| `device_cap_reached` | `MAX_DEVICES` exceeded; new measurements dropped |
| `metric_cap_reached` | `MAX_METRICS_PER_DEVICE` exceeded |

`overlap_topic_patterns` follows MQTT 3.1.1 §4.7 properly: `$`-prefixed
topics are disjoint from non-`$` ones, a trailing `/#` also matches its parent
topic, and `#` terminates the segment walk. It is *conservative* — a false
positive warns, a false negative double-counts. Prefer the warning.

When adding an issue: give it a namespaced id helper, raise it from the
periodic tick or setup, and delete it via `async_delete_issue` when the
condition clears. A log line instead of a Repairs issue is a Platinum
regression — recoverable config problems must be actionable in the UI.

## Entities and devices

- One HA **device per Telegraf host**, not per message. The device is keyed by
  the `device_id` derived under the active `device_id_strategy`.
- Entities belong to a device and are addressed by `unique_key`; the registry
  is in-memory and HA's recorder owns persistence.
- **Availability** follows `expire_after`: an entity that stops receiving goes
  `unavailable` on the expiry tick and is cleaned up per the cleanup policy.
  An entity that is permanently unavailable without a Repairs issue is a bug.
- `device_class`, `state_class`, and `unit` must be consistent across a
  device. Mixed units on one device produce broken history graphs.
- `entity_category` = `diagnostic` keeps noise out of the primary list
  (load averages, process counts, uptime).

## Procedure

1. Read the module docstring of the file you are changing — they state the
   contract, not the signature.
2. Check `manifest.json` if you touched dependencies, iot class, or versioning.
3. For a new Repairs condition: follow the existing seven-issue pattern
   exactly (id helper, raise, auto-resolve, defensive guards, tests).
4. For a new diagnostics field: prove it cannot leak a host name or a field
   value. If unsure, hash it or leave it out.
5. For a lifecycle change: confirm the teardown handle is parked on
   `runtime_data` and released in `async_unload_entry`.

## Validation

```powershell
# HA-facing tests.
.\.venv\Scripts\python -m pytest tests/test_config_flow.py tests/test_diagnostics.py tests/test_phase7_diagnostics_repairs.py tests/test_phase6_lifecycle.py -q

# The three gates.
.\.venv\Scripts\python -m ruff check .
.\.venv\Scripts\python -m mypy --strict custom_components
.\.venv\Scripts\python -m pytest -q

# Release surfaces (CI also runs hassfest + HACS).
.\.venv\Scripts\python -m pytest tests/test_release_readiness.py -q
```

## Completion criteria

- `manifest.json` is still accurate for the change.
- Setup/unload are symmetric; every handle is on `runtime_data`.
- Diagnostics expose nothing sensitive.
- New problems raise a Repairs issue, not just a log line.
- Full `pytest -q` passes at 100% coverage.

## Boundaries

- Never add a `requirements` entry. The project has zero third-party
  dependencies by design and this is a stated non-goal.
- Never add a field value or host name to diagnostics.
- Never block the event loop in setup, unload, or the periodic tick.
- Never persist field values to disk; the registry is in-memory by design.
- Never change `iot_class` without confirming the data really is pushed.
- Do not treat `SPEC.md` as a source of truth — it does not exist in the repo.

## Handoff

- Adding/changing an option → `config-options-review`
- Missing or unavailable entities at runtime → `debugging-discovery`
- Tests for anything above → `test-authoring`
- Version bumps, changelog, README → `docs-and-changelog`
