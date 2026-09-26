# syntax-portability

## Purpose

Write code that imports on the **older** of the two Python versions this
project cares about. The runtime floor is CPython 3.14, but the package
deliberately keeps its grammar portable to 3.13 so a PEP-758 regression can
never ship again.

## When to use

- Writing or editing any `except` clause.
- Using any syntax newer than the pinned grammar floor.
- A module fails to import on a HA host with `SyntaxError` even though CI
  passed.
- Touching `pyproject.toml` `[tool.ruff]` `target-version` or the
  `MIN_SYNTAX_VERSION` in the tripwire test.
- Reviewing a diff for grammar that only parses on the CI interpreter.

## Scope

In scope: the runtime/grammar floor, `# fmt: skip` on multi-exception
clauses, `ast.parse(..., feature_version=...)`, and the toolchain blind spot
this skill exists to close.

Out of scope: type annotations (→ `strict-typing`); formatting style generally
(→ the ruff gate).

## The two floors

| Floor | Value | Where declared |
|---|---|---|
| **Runtime** (what CI and HA run) | 3.14 | `requires-python = ">=3.14"`, ruff `target-version = "py314"`, mypy `python_version = "3.14"`, CI `python-version: '3.14'` |
| **Grammar** (what the package must parse under) | 3.13 | `MIN_SYNTAX_VERSION: tuple[int, int] = (3, 13)` in `tests/test_syntax_compatibility.py` |

`3.14` is forced: Home Assistant 2026.6.x sets `Requires-Python` to CPython
`>=3.14`. The `3.13` grammar floor is chosen **deliberately one generation
below** the runtime — the parenthesized forms cost nothing, and keeping the
grammar portable means the package stays importable if the HA floor ever moves
down. Bump it only with intent; whatever is pinned there becomes the enforced
floor.

## The trap: PEP 758

Python 3.14 legalised **unparenthesized multi-exception `except` clauses**:

```python
except TypeError, ValueError:        # legal on 3.14, SyntaxError on <=3.13
```

On a HA host with CPython 3.13 this is a hard `SyntaxError` at import time —
the integration cannot load at all, and any test importing the package fails
at collection. Commit `5d6f9fc` shipped four of these (`__init__.py`
`_coerce_int_option`, `diagnostics.py`, `parsers/generic.py`, and
`parser.py` `parse` — the main ingest path). They imported only because CI
runs 3.14.

**The entire toolchain accepts the bug.** This is why a dedicated test exists:

- `python -m py_compile` and the CI interpreter are 3.14, so they accept it.
- ruff's parser is version-aware and at `target-version = "py314"` also
  accepts it.
- Worse, **`ruff format` at this target actively *strips* the parentheses**,
  converting a portable `except (A, B):` into the unportable
  `except A, B:`. The package keeps the parenthesized form, so **every such
  clause carries a bare `# fmt: skip`** — those parentheses are load-bearing.

```python
# Correct and load-bearing. Keep the parentheses AND the fmt: skip.
try:
    decoded = json.loads(payload)
except (TypeError, UnicodeDecodeError, json.JSONDecodeError):  # fmt: skip
    ...
```

## The tripwire

`tests/test_syntax_compatibility.py` parses every `*.py` in the package with
`ast.parse(..., feature_version=MIN_SYNTAX_VERSION)`, which CPython evaluates
against the *older* grammar. It runs on every pytest invocation, including CI,
so a 3.14-only syntax regression cannot reach main.

It also **guards the guard**: `test_tripwire_rejects_unparenthesized_multi_except`
asserts that `feature_version` really does reject the PEP-758 form. If a
future CPython changed `feature_version` semantics, that test fails and forces
a rework instead of letting the gate silently become a no-op.

## What else the floor covers

The tripwire is grammar-generic, not PEP-758-specific. Anything legal in
3.14 but not 3.13 fails it — for example PEP 649/749-era deferred-annotation
behaviour, or 3.14-only builtin generics. It parses with `feature_version`,
so any such construct is caught. The one thing to remember is that
`from __future__ import annotations` is present in every module here, which
keeps annotation evaluation lazy and portable.

## Procedure

1. Before writing a multi-exception `except`, decide: parenthesized, with
   `# fmt: skip`. Never unparenthesized.
2. Never run bare `ruff format` over a multi-except clause without checking
   the parentheses survived — the formatter removes them at this target.
3. After any grammar-adjacent change, run the tripwire directly:
   `.\.venv\Scripts\python -m pytest tests/test_syntax_compatibility.py -q`
4. If you introduce genuinely new 3.14-only syntax, you have two honest
   choices: (a) don't, or (b) raise `requires-python`, the ruff target, the
   mypy `python_version`, the CI `python-version`, **and** `MIN_SYNTAX_VERSION`
   together, with a changelog note. Changing one of them alone silently
   breaks a gate.

## Validation

```powershell
# The tripwire, fast.
.\.venv\Scripts\python -m pytest tests/test_syntax_compatibility.py -q

# Confirm the formatter would not strip your parentheses.
.\.venv\Scripts\python -m ruff format --check custom_components

# The gate.
.\.venv\Scripts\python -m pytest -q
```

## Completion criteria

- Every multi-exception clause is parenthesized and carries a bare
  `# fmt: skip`.
- `pytest tests/test_syntax_compatibility.py -q` passes.
- `ruff format --check` leaves the parentheses intact.
- If a version was bumped, all five declarations moved together.

## Boundaries

- Never write an unparenthesized multi-exception `except`.
- Never remove a `# fmt: skip` from a multi-except clause — the formatter
  will strip the parentheses and silently break 3.13 hosts.
- Never bump one version declaration without the others.
- Never raise `MIN_SYNTAX_VERSION` to make a failing tripwire pass; fix the
  syntax instead.
- Never remove or weaken `test_tripwire_rejects_unparenthesized_multi_except`
  — it is the guard on the guard.

## Handoff

- Type annotation issues (not grammar) → `strict-typing`
- Multi-except clause inside a parser → `adding-telegraf-parsers`
- Tests for the changed code → `test-authoring`
