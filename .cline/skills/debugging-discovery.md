# debugging-discovery

## Purpose

Triage a runtime symptom — entities missing, duplicated, permanently
unavailable, or attached to the wrong device — from the diagnostics download
back to the responsible line of code.

## When to use

- "No entities appeared", "entities disappeared", "everything is unavailable".
- Two hosts collapsed into one device, or one host became several devices.
- A Repairs issue appeared that the user cannot clear.
- A field is missing that the user can see on the broker.
- A user is asking why the integration is idle.

## Scope

In scope: `diagnostics.py` interpretation, `registry.py` device-id derivation
and expiry/cleanup, `parser.py` drop counters, `snoop.py` and the config-flow
discovery path.

Out of scope: fixing a measurement's metadata on a *working* integration
(→ `adding-telegraf-parsers`); option design (→ `config-options-review`).

## Start with the diagnostics download, not the code

`diagnostics.py` is built for exactly this. Its shape is pinned by
`tests/test_diagnostics.py`, and the `ParserStats` counters are the fastest
route to a diagnosis. Read them in this order:

| Counter | Reading |
|---|---|
| `received` | If this is 0, nothing is arriving — the problem is the broker, the topic pattern, or the subscription. Stop here; it is not a code bug. |
| `dropped_invalid_json` | The broker is publishing something that is not JSON. Almost always another integration on the same topic. |
| `dropped_unsupported_shape` | Valid JSON that is not a Telegraf line-delimited envelope, or `name` is missing/not a string. |
| `dropped_parser_error` | A handler raised. Check `last_message.measurement` — that names the exact parser at fault. |
| `unknown_measurement_fallbacks` | The measurement is not in `_PARSERS`; it went to `generic.py`. If its fields render wrong, that is a parser gap. |
| `parsed` | Successes. Compare against `received` to get the drop rate. |

`last_message` gives `topic`, `byte_length`, `dropped_reason`, and
`measurement`. A `byte_length` of 0 means an empty publish; a small one on the
wrong topic means a topic-pattern mismatch.

Remember the redaction contract while reading: host names are hashed
(`_hash_device_id`, a 16-char SHA-256 prefix) and field values are absent. If
you need the real host name, ask the user for it — it is deliberately not in
the file.

## Symptom → likely cause

### "No entities at all"

1. `received == 0` → the subscription never fired. Check the configured
   `topic_pattern` against the real broker topics, and confirm the `mqtt`
   integration is set up (it is a `manifest.json` dependency).
2. `received > 0`, `parsed == 0`, drops > 0 → the payload shape is wrong or a
   handler raised. See the counter table.
3. `parsed > 0` but no entities → the descriptors were dropped by a cap
   (`device_cap_reached` / `metric_cap_reached` Repairs issues) or by
   `exclude_patterns`.

### "Two machines became one device"

The device id is derived from the `host` tag, slugified by `_slugify_device`.
Two distinct hosts whose tags slug-collide collapse into one device.
`repairs.py` raises `device_id_collision` for this within one entry, and
`device_id_conflict` for the same slug arriving from two config entries.

The fix is the `device_id_strategy` option:

| Strategy | Behaviour | Use when |
|---|---|---|
| `host` (default) | use the `host` tag | tags are trustworthy and unique |
| `host_topic` | prefer the `host` tag, but treat a *degenerate* host as absent and append the second-level topic root | several hosts all publish `host=localhost` — common with Docker, misconfigured agents, and unattended installs |
| `topic_only` | always use the topic tree | the topic layout is the only per-machine signal; less stable across re-arranges |

`_DEGENERATE_HOST_TAGS` is the set that triggers the `host_topic` fallback
(including `localhost`, `127.0.0.1`, `0.0.0.0`, `::1`, and the literal
`host`), and some test fixtures publish these when the real value is missing.

**Changing this option forces a config-entry reload.** That is deliberate:
`DeviceManager.devices` is keyed by the old strategy's slugs, so a live apply
would orphan the existing devices while new traffic built a parallel set.

### "Entities keep going unavailable"

`check_expiry` marks a metric unavailable when
`now - state.last_updated > expire_after`. The clock is `monotonic()` and is
injectable, which is what makes the behaviour testable.

- Check `expire_after` against Telegraf's actual `interval`. If the agent
  publishes every 60s and `expire_after` is 30, every entity flaps.
- The scan runs on a cadence floored at `MIN_EXPIRY_TICK_SECONDS = 5` and
  capped at `MAX_EXPIRY_TICK_SECONDS = 30`, so a 1-second `expire_after`
  still only refreshes every 5 seconds.
- Offline devices are skipped entirely by the cleanup pass (`last_any_metric`
  beyond `expire_after`), so their entities are never cleaned up.

### "An entity vanished"

Cleanup: `enable_cleanup`, `cleanup_delay` (30 days default),
`delete_delay` (60 days), `min_active_metrics`, and the per-descriptor
`cleanup_policy` (`AUTO` / `NEVER` / `ALWAYS` from `parsers/static.py`). A
metric that has been unavailable past `cleanup_delay` and is not above
`min_active_metrics` is removed. An entity marked `NEVER` is exempt.

### "The new host never appeared"

- Auto-discover (`auto_discover`) defaults to **off**, deliberately: the
  probe runs on the same broker, so a careless default would probe wider than
  the user's configured `topic_pattern`. The user must opt in.
- When opted in, `__init__.py` derives the probe topic from the entry's
  `topic_pattern` via `derive_probe_topic`; the snoop never silently widens
  past the user's scope.
- Config-flow topic discovery is a separate path: `async_step_user` branches
  on `setup_mode` into `async_step_scan_settings` →
  `async_step_scan_running` → `async_step_pick_topics`. The scan window is
  bounded by `MIN_SCAN_DURATION_SECONDS = 5` /
  `MAX_SCAN_DURATION_SECONDS = 300`; if the agent publishes less often than
  the window, the scan legitimately finds nothing and reports
  `no_traffic_on_topic`.

## Procedure

1. Get the diagnostics download. Do not guess from the UI.
2. Read `received` and the drop counters; classify the failure as
   *no traffic* / *bad payload* / *handler fault* / *parser gap* / *capped*.
3. Only then read the responsible module.
4. If the cause is a cap, confirm the cap is the real constraint before
   raising it — `MAX_METRICS_PER_DEVICE` is 1000 and `MAX_DEVICES` defaults
   to 50, both chosen for fleet reasons documented in `const.py`.
5. If the cause is device-id derivation, decide whether the user should change
   `device_id_strategy` (and accept the reload) before you change any code.

## Validation

```powershell
# The suites that pin this behaviour.
.\.venv\Scripts\python -m pytest tests/test_diagnostics.py tests/test_registry.py tests/test_discover_topics.py tests/test_runtime.py -q
.\.venv\Scripts\python -m pytest tests/test_phase7_diagnostics_repairs.py tests/test_phase6_lifecycle.py -q

# The gate.
.\.venv\Scripts\python -m pytest --cov=custom_components.telegraf_mqtt
```

Reproduce the symptom in a test with a fake clock before you fix it; a
timing bug that cannot be reproduced is a bug you will reintroduce.

## Completion criteria

- The symptom is explained by a specific counter, constant, or line — not a
  guess.
- The fix is at the owning module, not worked around downstream.
- Any new guard path is covered (the 100% gate will demand it).
- No cap was raised without a stated fleet-scale reason.
- `pytest -q` passes at 100% coverage.

## Boundaries

- Never raise `MAX_DEVICES` / `MAX_METRICS_PER_DEVICE` to make a symptom go
  away. The caps exist because the registry is O(devices × metrics) on the
  shared event loop.
- Never change `device_id_strategy` defaults in `const.py` to fix one user's
  setup; it is a user option.
- Never add a host name or field value to diagnostics while debugging.
- Never block on `sleep` in a test; inject the clock (`DeviceManager` and
  `MetricRegistry` both accept one).
- Do not treat `SPEC.md` as authoritative — it is absent from the repository.

## Handoff

- The measurement needs a parser or better metadata → `adding-telegraf-parsers`
- The user needs a new or changed option → `config-options-review`
- The fix needs a regression test → `test-authoring`
- A lifecycle or Repairs change → `ha-quality-scale`
