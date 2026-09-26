# Zoo Skill Suite — Plan

## 1. Analysis findings

### 1.1 The agent-config directories are empty and were never tracked

| Path | State |
|---|---|
| `.cline/skills/` | exists, **empty** |
| `.cline/workflows/` | exists, **empty** |
| `.clinerules/` | exists, **empty** |
| `.qodo/agents/` | exists, **empty** |
| `.qodo/workflows/` | exists, **empty** |
| `.roo/mcp.json` | the only `.roo` content |

Root cause: [`.gitignore:6-7`](../.gitignore:6) contains

```gitignore
# Cline Skills
.cline/*
```

`.cline/*` is ignored in full. [`CHANGELOG.md:21-22`](../CHANGELOG.md:21) claims the
long-term QA/QC system "lives in `.cline/` (ROADMAP / TASKS / quality_scale /
AGENTS)", and [`CHANGELOG.md:317`](../CHANGELOG.md:317) records
`.cline/skills/SKILL4.md` as a new file. None of that is in the tree or in git
history, so the historical system is **unrecoverable** and must not be
pretended to exist.

### 1.2 Stale references caused by the removal

- [`README.md:423`](../README.md:423) — "see `.cline/ROADMAP.md`" points at a
  non-existent file.
- [`models.py:82`](../custom_components/telegraf_mqtt/models.py:82) — cites
  "the corrected comment in `.cline/skills/architecture.md`".
- `SPEC.md` and `ROADMAP.md` are cited from 8 test files and 3 source modules
  but **do not exist**. Left alone: rewriting docstrings is out of scope and
  touches unrelated user work. The skills reference real, existing files
  instead so the suite introduces no new dangling links.

### 1.3 Recovered conventions (from CHANGELOG prose, not from files)

Naming: lowercase-hyphenated skill slugs (`.cline/skills/architecture.md` is
attested). Each skill is a single self-contained `.md` file with a
purpose-first opening line. Bodies are prose-with-runnable-commands, and the
house style is unusually heavy on *why* comments — the repo's defining
convention, visible in [`pyproject.toml`](../pyproject.toml),
[`test_syntax_compatibility.py:1-40`](../tests/test_syntax_compatibility.py:1),
and [`registry.py:80-107`](../custom_components/telegraf_mqtt/const.py:80).

### 1.4 What the skills must encode (all evidence-backed)

- **Pipeline**: MQTT → `parser.py` dispatch (31 measurements) → `MetricDescriptor`
  → `registry.DeviceManager` → `sensor.py` / `binary_sensor.py`.
- **Gates**: ruff (`py314`, line-length 120, E/W/F/I/B/C4/UP/SIM/RUF), mypy
  `--strict` on `custom_components/telegraf_mqtt` only, pytest with
  `--cov-fail-under=100`.
- **Version sync**: `manifest.json` is canonical; `pyproject.toml`,
  `CHANGELOG.md`, `hacs.json` locked by `test_release_readiness.py`.
- **Syntax floor**: CPython 3.14 runtime, 3.13 grammar floor enforced by
  `test_syntax_compatibility.py`; parenthesized multi-except needs `# fmt: skip`.
- **SKILL4 rule** (recovered from [`CHANGELOG.md:317-322`](../CHANGELOG.md:317)):
  a constant/helper/stub with no production callsite is debt — delete it and
  the tests that exist only to pin it.

## 2. Plan

Recovered conventions are applied uniformly: each skill lives at
`.cline/skills/<slug>.md`, opens with a `Purpose` line, and carries the
sections **Purpose / When to use / Scope / Procedure / Validation / Completion
criteria / Boundaries / Handoff**.

1. Un-ignore `.cline/` in `.gitignore` so the suite is trackable.
2. Add `.cline/README.md` as the index + conventions document.
3. Author ten skills covering the repo's real work streams.
4. Repair the two broken references the suite touches.
5. Validate with the repo's own gates.

### Skill roster

| Skill | Covers |
|---|---|
| `architecture-map` | pipeline, module ownership, data contracts |
| `adding-telegraf-parsers` | new measurement support, dispatch table, units |
| `ha-quality-scale` | HA brand/scale requirements, diagnostics, repairs |
| `strict-typing` | mypy `--strict`, TypeGuard/Literal patterns |
| `test-authoring` | 100% coverage gate, harness rules, naming |
| `debugging-discovery` | MQTT/discovery/device-id triage |
| `config-options-review` | config flow, options, Repairs issues |
| `docs-and-changelog` | version-sync surfaces, docs depth |
| `syntax-portability` | PEP 758 / `# fmt: skip` tripwire |
| `repo-hygiene` | SKILL4 no-callsite audit, ignore rules |

## 3. Validation

- `ruff check .` and `ruff format --check .`
- `mypy --strict custom_components/telegraf_mqtt`
- `pytest -q` (100% coverage gate + release-readiness gates)
- Grep the new skills for links to files that do not exist
- Review the final diff for omissions

## 4. Known limitations

- The historical `.cline/` system is unrecoverable; the suite is a fresh
  design that follows the conventions the CHANGELOG attests, not a restoration.
- `SPEC.md` / `ROADMAP.md` remain cited-but-absent (pre-existing, out of scope).
- Skill activation depends on the host agent reading `.cline/skills/`; the
  repository has no machine-readable skill manifest format to register against.
