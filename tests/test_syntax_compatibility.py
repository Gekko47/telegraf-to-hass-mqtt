"""Syntax-compatibility tripwire for the integration package.

The runtime floor is pinned repo-wide to CPython 3.14 (``requires-python``,
ruff ``target-version``, mypy ``python_version``, CI's python-version)
because HA 2026.6.x requires it. PEP 758 (Python 3.14) legalized
unparenthesized multi-exception ``except`` clauses --
``except TypeError, ValueError:`` -- so every tool in that 3.14 toolchain
parses them happily. On CPython 3.13 and below the same line is a hard
``SyntaxError`` at import time: the integration cannot load at all, and
any test module that imports the package fails at collection.

Nothing in the toolchain catches this regression:

* ``python -m py_compile`` and the CI interpreter are 3.14, where the
  syntax is legal, so both accept it silently.
* ruff's parser is version-aware and, at ``target-version = "py314"``,
  also accepts it.

This module is the dedicated tripwire: it parses every file in the
package with ``ast.parse(..., feature_version=MIN_SYNTAX_VERSION)``,
which CPython evaluates against the *older* grammar -- 3.14-only syntax
is rejected while everything the package legitimately uses still parses.
It runs on every ``pytest`` invocation, including the CI gate, so a
syntax error that only manifests on Python <= 3.13 can no longer reach
main.

``MIN_SYNTAX_VERSION`` is deliberately pinned one generation *below* the
runtime floor: the parenthesized forms cost nothing, and keeping the
grammar portable means the package stays importable if the HA floor ever
moves down -- and a PEP-758-style regression can never ship again. Bump
it only with intent; whatever is pinned here becomes the enforced
syntax floor.

Historical note: ``5d6f9fc`` shipped four unparenthesized clauses
(``__init__.py`` ``_coerce_int_option``, ``diagnostics.py``,
``parsers/generic.py``, and ``parser.py`` ``parse`` -- the main ingest
path). They only imported because CI runs Python 3.14; on Home Assistant
hosts with CPython 3.13 the integration was unimportable. Fixed in the
same pass that added this gate.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

PACKAGE_DIR = Path(__file__).resolve().parents[1] / "custom_components" / "telegraf_mqtt"

# See the module docstring: one generation below the runtime floor (3.14),
# on purpose. Compare with tuples, never strings, so (3, 10)-style bumps
# stay lexicographically sane.
MIN_SYNTAX_VERSION: tuple[int, int] = (3, 13)


def _package_sources() -> list[tuple[Path, str]]:
    """Return every ``*.py`` file in the package with its source text."""
    return [
        (path, path.read_text(encoding="utf-8"))
        for path in sorted(PACKAGE_DIR.rglob("*.py"))
        if "__pycache__" not in path.parts
    ]


def test_tripwire_rejects_unparenthesized_multi_except() -> None:
    """Guard the guard: ``feature_version`` must actually reject PEP 758 syntax.

    If a future CPython changed ``feature_version`` semantics so the
    clause below parsed, this test would fail and force a rework of the
    gate instead of letting it silently become a no-op.
    """
    snippet = "try:\n    pass\nexcept TypeError, ValueError:\n    pass\n"
    with pytest.raises(SyntaxError):
        ast.parse(snippet, feature_version=MIN_SYNTAX_VERSION)


def test_package_parses_under_minimum_syntax_version() -> None:
    """Every package file must parse under the pinned grammar floor.

    A failure here means a 3.14-only construct reached the package; on
    Home Assistant installs running an older CPython the integration
    would die at import time (config flow, platforms, diagnostics --
    everything), with no other gate standing in the way.
    """
    sources = _package_sources()
    assert sources, "no package sources found -- PACKAGE_DIR wrong?"
    for path, source in sources:
        try:
            ast.parse(source, filename=str(path), feature_version=MIN_SYNTAX_VERSION)
        except SyntaxError as exc:
            raise AssertionError(
                f"{path.relative_to(PACKAGE_DIR)} uses syntax newer than Python "
                f"{MIN_SYNTAX_VERSION[0]}.{MIN_SYNTAX_VERSION[1]} (import-time "
                f"SyntaxError on older HA hosts): {exc}. If this is an "
                "unparenthesized multi-exception ``except A, B:`` clause, ruff "
                "format (target py314) stripped the parentheses -- restore "
                "``except (A, B):`` and keep the trailing ``# fmt: skip`` "
                "directive on that line."
            ) from exc
