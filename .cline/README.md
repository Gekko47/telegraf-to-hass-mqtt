# Zoo agent skills

Repository-specific skills for an autonomous coding agent working on
`telegraf_mqtt`. Each file in `skills/` is a self-contained capability: it
names the trigger, the exact scope, a grounded procedure, the validation that
proves the work, and the boundary where it hands off to another skill.

## Why this directory exists

A previous `.cline/` tree held a QA/QC system (`ROADMAP` / `TASKS` /
`quality_scale` / `AGENTS` plus numbered skills such as `SKILL4.md`), per
`CHANGELOG.md`. It was never committed — `.gitignore` carried a blanket
`.cline/*` rule — so it is not recoverable from the repository and is not
reconstructed here. What follows is a fresh suite that follows the
conventions the changelog *attests* (see [Conventions](#conventions)).

## How to use a skill

1. Match the task against the [Skill index](#skill-index) triggers.
2. Read the whole skill file before editing. Each states its own scope.
3. Follow its **Procedure**; run its **Validation** commands verbatim.
4. Check **Completion criteria** before reporting done.
5. Respect **Boundaries** — several skills are explicitly non-overlapping and
   hand off to each other.

Skills are advisory workflow, not a replacement for the gates. `ruff`,
`mypy --strict`, and `pytest` are the authority; a skill that conflicts with
them is wrong.

## Skill index

| Skill | Trigger | Covers |
|---|---|---|
| [`architecture-map`](skills/architecture-map.md) | "how does this work?", any change that may cross module boundaries | The `parse → route → render` pipeline, module ownership, data contracts, the five fleet caps |
| [`adding-telegraf-parsers`](skills/adding-telegraf-parsers.md) | a new Telegraf measurement or plugin needs support; a field gets the wrong unit/class | Dispatch-table registration, descriptor construction, unit inference, per-plugin edge cases |
| [`ha-quality-scale`](skills/ha-quality-scale.md) | anything touching HA-facing behaviour | `manifest.json`, `iot_class`, config/options flow, translations, diagnostics redaction, Repairs, cleanup |
| [`strict-typing`](skills/strict-typing.md) | new or changed code in the integration package | The `mypy --strict` gate, `Literal`/`TypeGuard` idioms, `Any` discipline, frozen-dataclass contract |
| [`test-authoring`](skills/test-authoring.md) | tests are required, failing, or need restructuring | The 100% coverage gate, real-HA vs harness-free tests, naming, `conftest.py` shims, phase files |
| [`debugging-discovery`](skills/debugging-discovery.md) | entities missing, duplicated, unavailable, or stuck `unavailable` | Diagnostics download triage, device-id strategies, expiry/cleanup, discovery and the snoop |
| [`config-options-review`](skills/config-options-review.md) | adding or changing a user-facing option; reviewing an options PR | Option constants, live-apply vs reload boundary, overrides, the five Repairs issues |
| [`docs-and-changelog`](skills/docs-and-changelog.md) | a release, a user-visible option, or a doc change | Version-sync surfaces, changelog entry shape, README, translations, HACS metadata |
| [`syntax-portability`](skills/syntax-portability.md) | writing an `except` clause or any new grammar | The 3.14-runtime / 3.13-grammar floor, `# fmt: skip`, what the toolchain will not catch |
| [`repo-hygiene`](skills/repo-hygiene.md) | cleanup, dead code, ignore-rule or ref-surgery changes | The no-callsite audit, ignore-rule traps, stale document references, refactor safety |

## Non-overlap map

Deliberate boundaries, so an agent does not run two skills over one change:

- `adding-telegraf-parsers` owns *new measurements/fields*;
  `debugging-discovery` owns *runtime symptoms*; `architecture-map` is
  read-only orientation and hands off to both.
- `strict-typing` owns *type errors*; `syntax-portability` owns *grammar
  errors*. A `ruff`/mypy failure that is a parse-level rejection belongs to
  `syntax-portability`.
- `config-options-review` owns *option definition and wiring*;
  `ha-quality-scale` owns *whether HA brand rules are still satisfied*.
  (Seven Repairs issues ship today, not the five named in the 1.2.0
  changelog entry; `ha-quality-scale` carries the current list.)
- `docs-and-changelog` owns *prose and version surfaces*; `repo-hygiene` owns
  *structural cleanliness* (ignore rules, dangling refs, dead code).
- Every skill that changes package code routes its validation through
  `test-authoring`; they do not restate the test suite.

## Conventions

Recovered from the repository, not invented:

- **Naming** — lowercase hyphenated slugs, `.md`, one capability per file.
  Attested by the `architecture.md` citation in
  `custom_components/telegraf_mqtt/models.py` and the `SKILL4.md` entry in
  `CHANGELOG.md`.
- **Section order** — `Purpose`, `When to use`, `Scope`, `Procedure`,
  `Validation`, `Completion criteria`, `Boundaries`, `Handoff`. Chosen so every
  skill answers the same questions in the same order.
- **Prose style** — the house voice is *explain why*, and the existing code
  sets a high bar. See the module docstrings in
  `custom_components/telegraf_mqtt/parser.py` and
  `tests/test_syntax_compatibility.py`. A comment that does not say *why*
  something is load-bearing, non-obvious, or a historical trap is not finished.
- **Paths** — repository-relative, backticked, no leading `./`.
- **Commands** — quoted verbatim from `pyproject.toml`, `.pre-commit-config.yaml`,
  and `.github/workflows/pytest.yml` so they stay runnable.
- **No fabricated history** — a skill cites a commit hash or a file only when
  the repository actually contains it.

## Validation

```powershell
# The three gates every change must pass (CI order, see .github/workflows/pytest.yml).
.\.venv\Scripts\python -m ruff check .
.\.venv\Scripts\python -m ruff format --check .
.\.venv\Scripts\python -m mypy --strict custom_components
.\.venv\Scripts\python -m pytest -q
```

`pytest` enforces `--cov-fail-under=100` from
`[tool.pytest.ini_options]`, so an uncovered line fails the run.

## Known repository limitations

- `SPEC.md` and `ROADMAP.md` are cited from several source docstrings and
  tests but **do not exist** in the repository. Treat them as historical
  context, never as a source of truth. `README.md` previously linked
  `.cline/ROADMAP.md`; that link now points here.
- There is no machine-readable skill manifest in this repository, so skill
  activation depends on the host agent reading this directory. This suite is
  documentation, not a plugin loader.
- `.cline/workflows/` is reserved for future multi-step automations and is
  intentionally empty.
