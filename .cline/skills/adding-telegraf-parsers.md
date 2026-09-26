# adding-telegraf-parsers

## Purpose

Add or correct support for a Telegraf measurement so its fields reach Home
Assistant with the right unit, device class, state class, and category.

## When to use

- A Telegraf input plugin publishes a measurement this integration does not
  handle (or handles only via the generic fallback).
- A field renders with the wrong unit, no unit, or a wrong device/state class.
  The `CHANGELOG.md` 1.4.1 entries for `diskio` and `wireless` are exactly this
  class of bug.
- A new translation key or icon is needed for a new entity.
- A user asks why a known Telegraf field has no unit at all.

## Scope

In scope: `custom_components/telegraf_mqtt/parsers/`, the `_PARSERS` dispatch
table in `parser.py`, `naming.py` keys, `icons.py` entries.

Out of scope: option definitions (→ `config-options-review`), runtime
symptoms on a *working* integration (→ `debugging-discovery`).

## Decide first: dedicated parser or generic fix?

The dispatch table in `parser.py` maps **31 measurement names** to handlers.
An unknown measurement silently falls back to `parse_generic_payload` at
DEBUG level and increments `unknown_measurement_fallbacks`. So:

- **Does the field render acceptably already?** If the generic path infers the
  right unit/class/category, do not add a dedicated parser. Adding one is
  maintenance debt for no gain.
- **Is the generic inference wrong or missing for a whole measurement?** Add
  `parsers/<name>.py` and register it.
- **Is it wrong for one field shared across measurements?** Fix the marker
  table or add a per-measurement override in `generic.py`, and pin it in
  `tests/test_generic_edge_cases.py`.

This decision is the first thing to write down before editing.

## Procedure

### 1. Get the real payload

Do not invent field names. Read the Telegraf plugin's upstream docs or capture
a real line-delimited JSON payload from the broker. The `CHANGELOG.md` 1.4.1
fix is the cautionary case: Telegraf's `diskio` reports bare
`read_time` / `write_time` / `io_time` / `weighted_io_time` / `io_util`, while
the existing marker table only matched `response_ms` / `_time_ms` /
`latency_ms` / `duration_ms` — **none of which appear in the real field
names** — so four duration fields and a percent gauge got no unit at all.

```bash
mosquitto_sub -t 'telegraf/diskio' -C 1 -v
```

### 2. Add the parser module

Create `custom_components/telegraf_mqtt/parsers/<name>.py`:

- Signature must satisfy the `PayloadParser` protocol in `parsers/base.py`:
  `__call__(payload: Mapping[str, Any]) -> Sequence[MetricDescriptor]`.
- Module opens with a one-line docstring; the *function* carries the
  explanatory docstring.
- `from __future__ import annotations` is present in every module here.
- Build tags with `frozen_tags()`.
- Use `build_unique_key(measurement, tags, field)` from `generic.py` for
  `unique_key` — it is the deterministic dedup key, and re-deriving it by
  hand is how duplicate entities appear.
- Never populate a display `name`; set `translation_key` and
  `translation_placeholders`. Phase 9 removed `name` on purpose.

### 3. Register it — three places, all required

| Location | What |
|---|---|
| `parsers/<name>.py` | the `parse_<name>_payload` function |
| `parsers/__init__.py` | the import **and** the `__all__` entry |
| `parser.py` | the `_PARSERS` `ClassVar` dispatch entry |

Skipping `__all__` is the most common miss; the import alone leaves the
symbol unexported.

### 4. Set the metadata, in this precedence

1. **User override** (`field_overrides`) — wins outright.
2. **`_TAG_UNIT_MAPPINGS`** in `generic.py` — per-measurement tag-driven
   overrides, already used by `ipmi_sensor`.
3. **Per-measurement override sets** — `_PERCENT_MEASUREMENT_OVERRIDES`,
   `_BYTE_MEASUREMENT_OVERRIDES`, `_BYTE_MEASUREMENT_INCLUDE` /
   `_BYTE_MEASUREMENT_EXCLUDE` (fields on a byte measurement that are *not*
   bytes, e.g. a percent).
4. **Field-name inference** — `infer_native_unit` (via `_BYTE_FIELD_MARKERS`,
   `_MS_FIELD_MARKERS`, `_SECONDS_FIELD_MARKERS`),
   `infer_device_class`, `infer_state_class`.
5. **None** — no unit, no class.

New information goes at the highest level that is still accurate for the whole
measurement. A field-name hack that only works for one plugin belongs in a
per-measurement set, not in a global marker tuple.

### 5. Category and lifecycle

- `resolve_entity_category(measurement, field)` in `naming.py` decides
  `diagnostic` vs primary. Load averages, process counts, and `system.uptime`
  are `diagnostic`; that is tested, so preserve it.
- `static_cleanup_policy` from `parsers/static.py` supplies the
  `CleanupPolicy` (`AUTO` / `NEVER` / `ALWAYS`).

### 6. Translations and icons

A new entity name needs a translation key in
`translations/en.json`, mirrored in `translations_strings.py`, plus an entry
in `icons.py` if it has an icon. See `docs-and-changelog` for the exact
surfaces — an unmapped key produces a raw `translation_key` in the UI.

## Validation

```powershell
# 1. The new measurement is wired end to end.
.\.venv\Scripts\python -m pytest tests/test_parser.py -q

# 2. Generic-regression suite if you touched generic.py.
.\.venv\Scripts\python -m pytest tests/test_generic_edge_cases.py -q

# 3. The three gates, in CI order.
.\.venv\Scripts\python -m ruff check .
.\.venv\Scripts\python -m ruff format --check .
.\.venv\Scripts\python -m mypy --strict custom_components
.\.venv\Scripts\python -m pytest -q
```

`pytest` runs with `--cov-fail-under=100`; an untested branch in the new
parser fails the run, not just the coverage report.

## Completion criteria

- The `_PARSERS` entry exists **and** the symbol is in `parsers/__init__.py`
  `__all__`.
- Every new field has a deliberate unit/device/state class, or a documented
  reason it has none.
- `tests/test_parser.py` (or the relevant phase file) pins the new behaviour
  with a realistic payload, including at least one absent or zero field.
- `unknown_measurement_fallbacks` no longer increments for this measurement.
- The three gates pass.

## Boundaries

- Never invent field names; use the upstream plugin's real schema.
- Never add a dedicated parser when the generic path is already correct.
- Never add a global marker substring to fix one measurement's field — that
  silently reclassifies every other plugin's similarly-named field.
- Never populate `MetricDescriptor.name`; it was removed in Phase 9.
- Never mutate a descriptor; it is frozen — use `dataclasses.replace`.
- Never widen the `except (KeyError, TypeError, AttributeError, ValueError)`
  in `parser.py` without a stated reason; it is the fault-isolation boundary.

## Handoff

- Tests are required for every step here → `test-authoring`
- Type errors from the new annotations → `strict-typing`
- `except`-clause parse failures → `syntax-portability`
- A new user-facing option (not just metadata) → `config-options-review`
- Translation/CHANGELOG/README surfaces → `docs-and-changelog`
