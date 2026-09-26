# config-options-review

## Purpose

Define a new user-facing option correctly and review changes to existing ones:
the constant, the flow wiring, the live-apply-vs-reload decision, the
overrides, and the Repairs issue that guards an invalid value.

## When to use

- Adding a config or options-flow field.
- Changing a default, a validation rule, or a `Literal` vocabulary.
- Reviewing a PR that touches `config_flow.py` or an `apply_options` call.
- Deciding whether an option can be applied live or needs a reload.

## Scope

In scope: `const.py` option keys and defaults, `config_flow.py` (config,
options, reconfigure), `DeviceManager.apply_options` and
`MetricRegistry.apply_options`, `field_overrides`, `category_overrides`.

Out of scope: whether HA brand rules still hold (→ `ha-quality-scale`); the
runtime symptom a user reported (→ `debugging-discovery`).

## The live-apply vs reload boundary

This is the decision that most often gets made wrongly. `Phase 10` in the
`CHANGELOG` records the rule:

**Applies live (no reload):** `expire_after`, `exclude_patterns`,
`field_overrides`, `cleanup_delay`, `delete_delay`, `enable_cleanup`,
`min_active_metrics`, `category_overrides`, and the cleanup/auto-discover
tunables. These are read per message or per tick, so `apply_options` just
updates the field.

**Forces a reload:** `device_id_strategy`, and the `topic_pattern`. Both
change the *identity* of what is stored. The rationale is in `registry.py`:
`DeviceManager.devices` is keyed by slugs derived under the current
strategy, so a live apply would orphan every existing device while new
traffic built a parallel set of registries. The reconfigure step triggers the
reload for the same reason.

When adding an option, decide which bucket it is in **before** writing code,
and write the reason in the docstring. If a new option derives device or
entity identity, it belongs in the reload bucket by construction.

## Procedure for a new option

1. **Define the constant and default in `const.py`.** Use `Final` when the
   value must satisfy a `Literal` at a dataclass boundary; add a
   `VALID_*` tuple for a closed vocabulary so validation and the
   `Literal` stay in one place.
2. **Add the flow field** in `config_flow.py`. The config flow and the
   options flow are separate classes; an option that should be changeable
   after setup belongs in the options flow only.
3. **Validate** using the same error keys the flow already uses
   (`invalid_topic`, `no_topics_selected`) and the same `_clean` normalisation,
   so a whitespace-only value is rejected like an empty one.
4. **Classify live vs reload** (see above) and implement the apply path in
   `apply_options` or force the reload, with a docstring saying why.
5. **Add a guard** if an invalid persisted value is possible:
   `repairs.py::check_invalid_persisted_option` with a namespaced issue id,
   auto-resolving when the value is corrected.
6. **Pin it** in the relevant test file, including the invalid-input path.

## Existing option surfaces

| Option | Key | Notes |
|---|---|---|
| topic pattern | `CONF_TOPIC_PATTERN` | default `telegraf/#`; reload/reconfigure |
| device name | `CONF_DEVICE_NAME` | normalised via `_clean` |
| expire after | `CONF_EXPIRE_AFTER` | default 120 s; live |
| exclude patterns | `CONF_EXCLUDE_PATTERNS` | live |
| field overrides | `CONF_FIELD_OVERRIDES` | per-field `platform` / `native_unit` / `device_class` / `state_class` / `entity_category`; live |
| category overrides | `CONF_CATEGORY_OVERRIDES` | `unique_key` → `config` / `diagnostic` / `None`; live |
| device id strategy | `CONF_DEVICE_ID_STRATEGY` | `host` / `host_topic` / `topic_only`; **reload** |
| auto discover | `CONF_AUTO_DISCOVER` | default **off**; opt-in only |
| scan settings | `CONF_SCAN_ROOT_TOPIC`, `CONF_SCAN_DURATION_SECONDS` | config-flow discovery only; bounded 5–300 s |
| setup mode | `CONF_SETUP_MODE` | `manual` / `discover`; unknown values default to manual |
| device metadata | `CONF_MANUFACTURER`, `CONF_MODEL`, `CONF_SW_VERSION` | carried into `DeviceInfo` |
| cleanup | `CONF_ENABLE_CLEANUP`, `CONF_CLEANUP_DELAY`, `CONF_DELETE_DELAY`, `CONF_MIN_ACTIVE_METRICS` | live |

Override precedence is fixed and tested: **user override wins over everything**
the parser inferred. A `None` value in `category_overrides` is meaningful — it
*clears* an auto-assigned category, surfacing the entity in the primary list.

## Review checklist for an options PR

- Every new key is a `CONF_*` in `const.py` with a documented default.
- Every closed vocabulary has a `VALID_*` tuple and matches the `Literal` in
  `models.py` if one exists.
- Live vs reload is decided, implemented, and justified in a docstring.
- Defaults that change runtime scope (like `auto_discover`) default to the
  conservative value and are opt-in; the snoop must never widen past the
  user's configured `topic_pattern`.
- A guard or Repairs issue exists for any value that can be invalid when
  persisted.
- The invalid-input path is tested, not just the happy path.

## Validation

```powershell
.\.venv\Scripts\python -m pytest tests/test_config_flow.py tests/test_phase2_options_availability.py tests/test_release_readiness.py -q
.\.venv\Scripts\python -m pytest tests/test_phase7_diagnostics_repairs.py -q
.\.venv\Scripts\python -m mypy --strict custom_components
.\.venv\Scripts\python -m pytest -q
```

## Completion criteria

- The key, default, and (if applicable) `VALID_*` tuple are in `const.py`.
- The flow field validates and the invalid path is tested.
- The live-vs-reload decision is made and documented in a docstring.
- Any persistable-invalid value has a Repairs issue that auto-resolves.
- `mypy --strict` and `pytest -q` (100% coverage) pass.

## Boundaries

- Never add an option that widens a broker subscription beyond the user's
  configured `topic_pattern` by default.
- Never apply a strategy/identity change live when it would orphan existing
  devices; force the reload.
- Never store an option outside `const.py`.
- Never make a new option silently break a persisted entry — pair it with an
  invalid-option Repairs issue or a default fallback.
- Do not treat `SPEC.md` as authoritative — it is absent from the repository.

## Handoff

- HA brand/lifecycle/Repairs implications → `ha-quality-scale`
- Users reporting a runtime symptom → `debugging-discovery`
- Tests for the new option → `test-authoring`
- Version/changelog/README surfaces for the new option → `docs-and-changelog`
