# strict-typing

## Purpose

Write and change code that passes `mypy --strict` on the integration package
without weakening it. Phase 10 made strict typing a hard CI gate; this skill
keeps it green honestly rather than by suppressing.

## When to use

- Any edit under `custom_components/telegraf_mqtt/`.
- A mypy failure appears in CI or from a local run.
- You are adding a parameter, return value, dataclass field, or `Literal`.
- You are tempted to add `# type: ignore` — this skill is the alternative.

## Scope

In scope: annotations, `Literal`/`TypeGuard`/`Final` usage, `Any` discipline,
`cast` justification, dataclass contracts in the package.

Out of scope: `tests/` (deliberately out of mypy's `files` list — the suite
drives untyped HA fakes and test doubles by design), and grammar/parse
failures (→ `syntax-portability`).

## The gate

`pyproject.toml` `[tool.mypy]`:

```toml
python_version = "3.14"
files = ["custom_components/telegraf_mqtt"]
strict = true
show_error_codes = true
explicit_package_bases = true

[[tool.mypy.overrides]]
module = "homeassistant.*"
follow_imports = "silent"
```

Consequences you must internalise:

- **`tests/` is not type-checked.** An `Any`-typed test double is not a
  shortcut the gate will catch; do not use "the tests do it" as a precedent
  for package code.
- **Home Assistant is followed silently, not deeply.** Its types are used, its
  own findings are ignored — because HA is installed from sources and
  following imports fully would type-check HA itself.
- **CI runs `mypy --strict custom_components`** (see
  `.github/workflows/pytest.yml`); pre-commit runs it with
  `--strict --explicit-package-bases` and `pass_filenames: false`.

## The `Any` discipline

`Any` is allowed in exactly three kinds of place in this package, and each
carries a comment saying why:

1. **Home Assistant objects we do not own.** `repairs.py` uses
   `(hass: Any, entry: Any)` on every check; the module resolves the issue
   registry lazily through `_ir(hass)` because `hass.data`'s type is not
   dependable across HA versions.
2. **MQTT message handles from the broker callback.** `__init__.py`'s
   `message_received(message: Any)` and `snoop.py`'s `_on_message` receive
   paho objects that are untyped at the boundary.
3. **Deferred, genuinely-value-agnostic positions.** `MetricState.value`,
   `sensor.py`'s `native_value`, and the registry's write callback
   `Callable[[str, bool, Any], None]` — these carry a value whose type is
   decided at runtime by the platform split.

A fourth appears in `__init__.py` and is worth calling out because it is a
**known gap, not a best practice**:

```python
parser_stats: Any  # ``custom_components.telegraf_mqtt.parser.ParserStats``
```

`ParserStats` lives in the same package, so `Any` is not forced by an external
boundary. It is a pre-existing looseness. If you touch the runtime-data
dataclass, annotating it properly is a welcome improvement — but do not
silently rewrite call sites to achieve it.

**Never** use `Any` to silence a narrowing problem. Use the `TypeGuard`
helpers in `models.py`.

## Narrowing: prefer `TypeGuard` over `cast`

`models.py` ships three guards precisely so the platform split type-checks:

```python
is_bool_metric(value: object) -> TypeGuard[bool]
is_numeric_metric(value: object) -> TypeGuard[int | float]
is_string_metric(value: object) -> TypeGuard[str]
```

Note the subtlety the docstring records: **`bool` must be tested before
`int`** because `bool` is a subclass of `int` in Python. `is_numeric_metric`
excludes `bool` explicitly for the same reason. Preserve that ordering.

When `cast` *is* the right tool, it is used at a real type-system boundary,
not an internal one:

- `sensor.py` casts `descriptor.suggested_device_class` to
  `SensorDeviceClass | None` — the value is a HA enum at runtime, but the
  descriptor carries it as `str | None`. The comment says so.
- `registry.py` casts a validated `platform_hint_raw` to `PlatformHint`
  **only after** the `in VALID_PLATFORM_HINTS` check. Validate, then cast.
  Casting before validating defeats the check.

## `Literal` and `Final` — keep them coupled

`models.py` defines the vocabulary; `const.py` supplies the values:

```python
CleanupPolicy = Literal["AUTO", "NEVER", "ALWAYS"]
PlatformHint = Literal["auto", "sensor", "binary_sensor", "none"]
```

```python
CLEANUP_POLICY_AUTO: Final = "AUTO"  # in const.py
PLATFORM_HINT_NONE: Final = "none"
```

`Final` is load-bearing: it keeps the inferred **literal** type, which is what
lets the constants satisfy the `Literal` at the dataclass boundary. If you add
a variant, add it in **both** places or mypy rejects the assignment. The
`Literal` types exist so mypy can check the comparisons in `registry.py`
exhaustively — that exhaustiveness is the feature, so do not widen a `Literal`
to `str` to make a comparison compile.

## The frozen descriptor

`MetricDescriptor` is `@dataclass(frozen=True)` with `Mapping` fields and a
`MappingProxyType` default. Two typing-relevant consequences:

- Immutability is a type-level promise; mutating a field is both a runtime
  `FrozenInstanceError` and a lie to the checker. Use `dataclasses.replace`.
- `frozen_tags()` is the constructor for tag mappings. Passing a mutable
  `dict` where `MappingProxyType` is expected is how aliasing bugs enter.

## Procedure

1. Run the gate first, so you know the pre-existing error count:
   `.\.venv\Scripts\python -m mypy --strict custom_components`
2. Fix the error at the narrowest honest scope. The order of preference:
   1. Annotate properly.
   2. Narrow with an existing or new `TypeGuard`.
   3. `cast` **after** a runtime validation check, with a comment naming the
      boundary.
   4. Widen a `Literal` only if the value genuinely belongs to the domain —
      and then update `const.py` in the same change.
   5. `# type: ignore[code]` only as a last resort, always with the exact
      error code and a comment saying when it can be removed.
3. Never touch `[tool.mypy]` to silence a finding. Loosening `strict`,
   narrowing `files`, or adding a per-module `ignore_errors` to make CI pass
   is a regression that hides every future error too.
4. Keep `from __future__ import annotations` at the top of every new module.

## Validation

```powershell
.\.venv\Scripts\python -m mypy --strict custom_components
.\.venv\Scripts\python -m ruff check .
.\.venv\Scripts\python -m pytest -q
```

All three must be clean. mypy reports `Success: no issues found` for the 27
source files.

## Completion criteria

- `mypy --strict custom_components` is clean with no new suppression.
- `[tool.mypy]` is unchanged, or changed for a stated, justified reason.
- Any `cast` is preceded by a runtime check and carries a comment naming the
  boundary.
- New `Literal` variants exist in both `models.py` and `const.py`.
- `pytest -q` still passes at 100% coverage.

## Boundaries

- Never widen a `Literal` to `str` to silence a comparison.
- Never annotate test files expecting the gate to care; `tests/` is out of
  scope for mypy.
- Never add `Any` where a `TypeGuard` already exists.
- Never reorder `is_bool_metric` before/after `is_numeric_metric` without
  reading the `bool`-is-a-subclass-of-`int` note.
- Never edit `pyproject.toml` `[tool.mypy]` to make a run pass.
- Parse-level errors are not typing errors — hand off to `syntax-portability`.

## Handoff

- `except`-clause or grammar parse failures → `syntax-portability`
- New measurement fields needing annotations → `adding-telegraf-parsers`
- Tests for the changed code → `test-authoring`
