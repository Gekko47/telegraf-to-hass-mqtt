# architecture-map

## Purpose

Orient yourself in the `telegraf_mqtt` integration before changing it: what the
pipeline is, which module owns which decision, and which data contract
separates each stage. Read-only — this skill never edits package code.

## When to use

- "How does this integration work?" or any question about data flow.
- Before a change that may cross a module boundary.
- Before deciding *where* a fix belongs (a common failure is patching
  `sensor.py` when the real defect is in `parsers/generic.py`).
- When you need to know whether a value is persisted, cached, or recomputed.

## Scope

In scope: the whole package under `custom_components/telegraf_mqtt/`, the
`MetricDescriptor` contract, the registry lifecycle, and the five fleet caps.

Out of scope: editing code (hand off to the relevant implementation skill),
runtime triage (hand off to `debugging-discovery`).

## The pipeline

```
MQTT message (JSON string, topic)
  │
  ├─ __init__.py  ── async_setup_entry: subscribe, register platforms,
  │                  park teardown handles on entry.runtime_data
  │
  ├─ parser.py    ── TelegrafParser.parse()
  │                  1. json.loads           → note_dropped("invalid_json")
  │                  2. must be a dict      → note_dropped("unsupported_shape")
  │                  3. decoded["name"] must be str
  │                  4. dispatch on the measurement via _PARSERS (ClassVar)
  │                  5. unknown measurement → parse_generic_payload, DEBUG only
  │                  6. handler is wrapped in a narrow try/except so one bad
  │                     field cannot kill the subscription → note_parser_error
  │
  ├─ parsers/<measurement>.py
  │                  payload dict → Sequence[MetricDescriptor]
  │                  base.py defines the PayloadParser Protocol:
  │                    __call__(Mapping[str, Any]) -> Sequence[MetricDescriptor]
  │
  ├─ registry.py  ── DeviceManager: dedupe by unique_key, derive device_id,
  │                  apply category overrides, enforce caps, expiry + cleanup
  │
  └─ sensor.py / binary_sensor.py
                     project descriptors onto HA entities
                     (translation_key + translation_placeholders, never a
                     pre-baked `name` string)
```

Fault isolation at step 6 catches only `KeyError`, `TypeError`,
`AttributeError`, `ValueError`. Do not widen that tuple casually: it is the
boundary that keeps a malformed-but-valid payload from tearing down the
subscription.

## Module ownership

| Module | Owns | Do not put here |
|---|---|---|
| `const.py` | Every option key, default, and cap. `Final` where a `Literal` must be satisfied | Logic of any kind |
| `models.py` | `MetricDescriptor` (frozen dataclass), `MetricValue`, `TypeGuard` helpers | Per-platform knowledge |
| `parser.py` | JSON envelope validation, measurement dispatch, `ParserStats` | Per-measurement field knowledge |
| `parsers/<name>.py` | One measurement's fields, units, state/device classes | Cross-measurement heuristics |
| `parsers/generic.py` | The fallback for unknown measurements, plus shared field-marker tables | Measurement-specific overrides that belong in the plugin file |
| `parsers/static.py` | Static metadata (entity categories, cleanup policy hints) | Per-message values |
| `naming.py` | `translation_key`, category, icon-key resolution | Formatting of values |
| `icons.py` | MDI icon table | Anything but icon keys |
| `units.py` | Value formatting | Unit *inference* (that lives with the parsers) |
| `registry.py` | Registry state, dedup, caps, expiry, cleanup, device-id derivation | MQTT subscription lifecycle |
| `sensor.py` / `binary_sensor.py` | Entity projection | Deciding what a descriptor means |
| `config_flow.py` | Config, options, reconfigure UI | Runtime behaviour |
| `snoop.py` | Post-setup auto-discover listener | Config-flow topic discovery |
| `diagnostics.py` | Redacted download | Anything unredacted |
| `repairs.py` | The five Repairs issues | Log-only warnings for the same conditions |

## The data contract

`MetricDescriptor` (frozen, `models.py`) is the only thing that crosses from a
parser to the registry. Consequences you must respect:

- **It is frozen.** If a downstream stage needs different data, construct a
  new descriptor (or use `dataclasses.replace`). Never mutate in place.
- **Display is translations-only.** The resolved `name` was removed in Phase 9.
  The *only* way to get a user-visible string is to format `translation_key`
  with `translation_placeholders`. A new entity name means a new translation
  key, not a new string constant.
- **Tags are immutable.** Use `frozen_tags()`; a `MappingProxyType` default
  keeps the dataclass hashable and prevents cross-descriptor aliasing.
- **`cleanup_policy` and `platform_hint` are `Literal`s** so mypy can check the
  comparisons in `registry.py` exhaustively. Literal values live in
  `const.py` as `Final`, which is what makes them satisfy the Literal at the
  dataclass boundary.

## In-memory by design

The registry never writes field values to disk. The persistence guarantee is
Home Assistant's own entity/recorder layer, keyed on `unique_key`. This is why
`device_id_strategy` changes force a config-entry reload: the existing
`DeviceManager.devices` dict is keyed by the old strategy's slugs, so applying
a new strategy live would orphan those devices while new traffic built a
parallel registry.

## The five fleet caps

All defined in `const.py`, all deliberate — do not raise one to "fix" a test:

| Cap | Value | Why it exists |
|---|---|---|
| `MAX_DEVICES` (default 50) | per config entry | A shared broker can carry unbounded hosts; an unbounded dict adds O(devices × metrics) event-loop latency to each scan |
| `MAX_METRICS_PER_DEVICE` | 1000 | A fully-loaded host (system + lm_sensors + per-core CPU + per-interface net + docker + smart) exceeds 50 easily |
| `MIN_EXPIRY_TICK_SECONDS` | 5 | The scan runs on the shared event loop; sub-5s precision is not useful |
| `MAX_EXPIRY_TICK_SECONDS` | 30 | Caps absurd `expire_after` values at a sane cadence |
| `DEFAULT_MAX_DEVICES` | 50 | Applied when the user has not opted into a custom value |

`MAX_METRICS_PER_DEVICE` was raised from 50 to 1000 in Phase 11 precisely
because the old value silently dropped fields on a well-loaded host. The
`MAX_DEVICES` default was deliberately *not* changed in the same pass.

## Procedure

1. Read the module docstring of the file you are about to change — they carry
   the rationale, not a summary of the signature.
2. Trace the data: which stage produced this value, and which stage owns it?
   If two stages both decide the same thing, that is a design smell.
3. Check `const.py` first for any option or cap the change interacts with.
4. Before writing, confirm which skill implements the change
   (`adding-telegraf-parsers`, `strict-typing`, `config-options-review`).

## Validation

This skill changes nothing, so it has no gate. For the change it informs:

```powershell
.\.venv\Scripts\python -m pytest -q
```

## Completion criteria

- You can state, in one sentence, which module owns the decision you changed.
- You have identified the descriptor field(s) involved, or confirmed none.
- You know whether the change requires a reload (see **In-memory by design**).

## Boundaries

- Never edit package code from this skill.
- Never assume a value is persisted; confirm against `registry.py` first.
- Do not raise a cap in `const.py` to make a test pass — see `debugging-discovery`.
- `SPEC.md` and `ROADMAP.md` are cited in docstrings but **do not exist**. They
  are historical context, never a source of truth.

## Handoff

- New measurement or field → `adding-telegraf-parsers`
- Type-error or annotation change → `strict-typing`
- Option or override wiring → `config-options-review`
- Missing/duplicated/unavailable entities → `debugging-discovery`
- Any change needs tests → `test-authoring`
