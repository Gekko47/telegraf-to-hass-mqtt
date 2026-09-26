# test-authoring

## Purpose

Write tests that satisfy this repository's 100%-coverage gate, the
real-HA-versus-harness-free split, and the xdist safety rules — without
writing tests that pin the wrong thing.

## When to use

- Any change to `custom_components/telegraf_mqtt/` needs test coverage.
- A test is failing, flaky, or order-dependent.
- You are adding a test file, restructuring `conftest.py`, or deciding where a
  test belongs.
- Coverage dropped and `pytest` failed on `--cov-fail-under=100`.

## Scope

In scope: everything under `tests/`, the `addopts` contract, fixture design,
xdist safety, and what to assert.

Out of scope: the three gates themselves (→ `strict-typing` for mypy,
`syntax-portability` for grammar); diagnosing a production bug (→
`debugging-discovery`).

## The gate

`pyproject.toml` `[tool.pytest.ini_options]` — this is the whole contract:

```toml
testpaths = ["tests"]
addopts = "-p no:homeassistant --cov=custom_components/telegraf_mqtt --cov-report=term-missing --cov-fail-under=100"
asyncio_mode = "auto"
asyncio_default_fixture_loop_scope = "function"
```

`[tool.coverage.report] fail_under = 100` repeats it. So:

- **A test run fails on an uncovered line**, not just on a red assertion.
  A defensive `except` branch nobody exercises is a red build.
- **`-p no:homeassistant` blocks the plugin's entry-point autoload.** On
  Windows `homeassistant` must be imported *after* `conftest.py` registers the
  POSIX shims, so `conftest.py` loads it explicitly via `pytest_plugins`.
  Do not "fix" this by re-enabling autoload; the ordering is load-bearing.
- **There is no `pytest.ini`.** pytest gives `pytest.ini` precedence over
  `pyproject.toml`, so the two could never merge — pytest logged
  "WARNING: ignoring pytest config in pyproject.toml!" and silently dropped
  `testpaths` / `filterwarnings` / `markers`. One source of truth wins. Never
  add a `pytest.ini`.
- **Asyncio mode is `auto`** — an `async def test_*` needs no decorator, and
  the fixture loop scope is pinned to `function` so a future
  pytest-asyncio upgrade cannot silently change fixture caching.

## Two kinds of test, and the boundary between them

| Kind | Uses the HA harness | Files | Cost |
|---|---|---|---|
| **Real-HA** | `hass`, `MockConfigEntry`, `async_fire_mqtt_message` | `test_harness.py`, `test_config_flow.py`, `test_sensor.py`, `test_runtime.py`, the phase files that need a live instance | Slow |
| **Harness-free** | plain imports, injected fakes | `test_parser.py`, `test_registry.py`, `test_naming_edges.py`, `test_formatting.py`, `test_platform_units.py` | Fast |

`tests/conftest.py` states the rule: "Only HA-dependent tests use those
fixtures; parser/registry/naming tests stay harness-free."

Put a test in the harness-free tier whenever the code under test can be
reached without a running HA. A parser test that boots an HA instance is a
slow test that will break whenever HA changes. Reserve the harness for
behaviour that genuinely *is* HA behaviour: config flow, entity setup/unload,
MQTT message injection, availability transitions.

## The stub-isolation invariant

`tests/test_release_readiness.py` installs minimal fake `homeassistant`
modules so the package imports with no HA present, and — this is the point —
so **execution order can never pollute the real-HA harness tests**. Both
real-HA and stub-based tests exist in one suite; the stubs are installed via
`monkeypatch.setitem(sys.modules, ...)` so they are torn down with the test.

Practical rules:

- If you add a test that needs a fake HA symbol, extend the existing
  `_install_fake_homeassistant` helper rather than creating a parallel stub
  module — two competing stub sets is how order-dependence returns.
- Never install a stub at import time (module scope). It leaks into every
  later test in the same worker.
- The phase files are named `test_phase1_devices.py` … `test_phase10_ux.py`
  because they map to the roadmap. Add to an existing phase file when the
  behaviour belongs to that phase; only create a new file for a genuinely
  new concern (e.g. `test_syntax_compatibility.py`).

## xdist safety

The repo has a documented history of xdist regressions, and the surface is
pinned by tests so it cannot recur silently:

- `addopts` deliberately does **not** contain `-n auto`; the CI gate stays a
  single deterministic run so coverage reporting is clean.
- Local dev opts in explicitly (from `pyproject.toml`):

```powershell
# Fast TDD loop: sharded, no coverage.
.\.venv\Scripts\python -m pytest -n auto

# Pre-commit style: sharded, with coverage.
.\.venv\Scripts\python -m pytest -n auto --cov=custom_components.telegraf_mqtt

# The gate: sequential, with coverage.
.\.venv\Scripts\python -m pytest --cov=custom_components.telegraf_mqtt
```

- **Shared mutable state is the hazard.** Module-level singletons, class
  attributes mutated by a test, and `sys.modules` writes at import time all
  behave differently under sharding. Use function-scoped fixtures.
- Time-based tests must not assume a wall clock. The registry uses
  `monotonic()` and accepts an injectable `now`; pass a fake rather than
  sleeping. `tests/test_phase8_performance.py` exists precisely to keep
  timing behaviour honest and bounded.

## What to assert

- **Assert behaviour, not implementation.** Pin the descriptor a parser
  produces (field, value, unit, device_class, state_class, category), not the
  internal helper it called.
- **Use realistic payloads.** A diskio test with `read_time` and `io_util`
  catches the class of bug the 1.4.1 changelog entry describes. A test with
  invented names like `response_ms` proves nothing about Telegraf.
- **Cover the absent and the zero.** Optional fields missing, `0`, `0.0`,
  `False`, and empty string are distinct inputs; the coverage gate forces you
  to reach them.
- **Cover the defensive guards.** The Repairs issues each guard against
  missing `runtime_data`, a missing manager, and a missing issue registry.
  Those guards must be exercised or coverage fails.
- **One behaviour per test.** A test that asserts six things reports one
  failure and hides five.

## Procedure

1. Pick the tier: can this run without a live HA? If yes, harness-free.
2. Write the test before the fix (or alongside), using a real payload shape.
3. Cover the new branch **and** the adjacent defensive guard — the 100% gate
   will demand the second regardless.
4. Run the narrowest selection while iterating, then the full gate.
5. If a test is flaky, fix the isolation before adding a retry.

## Validation

```powershell
# While iterating on one area.
.\.venv\Scripts\python -m pytest tests/test_parser.py -q

# Under sharding, if you touched fixtures or sys.modules.
.\.venv\Scripts\python -m pytest -n auto -q

# The gate that must pass before you report done.
.\.venv\Scripts\python -m pytest --cov=custom_components.telegraf_mqtt --cov-report=term-missing
```

## Completion criteria

- `pytest -q` passes with `--cov-fail-under=100` satisfied.
- New tests are in the correct tier (harness-free unless HA is required).
- New tests are isolated: no import-time `sys.modules` writes, no shared
  mutable state, no `sleep`.
- Every new production branch and its defensive guard are covered.
- The tests would still pass if the implementation were rewritten to a
  different correct implementation.

## Boundaries

- Never add `pytest.ini`; it would silently override `pyproject.toml`.
- Never remove `-p no:homeassistant` or reorder the Windows shims in
  `conftest.py`.
- Never add `-n auto` to `addopts`; the CI gate must stay sequential.
- Never weaken `fail_under = 100` or add `--no-cov` to a reported gate run.
- Never write a test whose only purpose is to pin a name with no production
  callsite — that is debt; see `repo-hygiene`.
- Never assert on private helper names as though they were the contract.

## Handoff

- Type errors in the code being tested → `strict-typing`
- Grammar/parse failures → `syntax-portability`
- A failing test that reveals a production bug → `debugging-discovery`
