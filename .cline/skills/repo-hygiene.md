# repo-hygiene

## Purpose

Keep the repository structurally honest: no dead code, no dangling
references, no ignore rules that hide real content, and no refactors that
quietly widen what the integration touches.

## When to use

- Removing unused constants, helpers, or test stubs.
- Editing `.gitignore`, or a file "disappears from git status" unexpectedly.
- A docstring, comment, or README cites a file that does not exist.
- Doing a cross-cutting refactor or a rename.
- Reviewing a diff for leftovers before merge.

## Scope

In scope: `.gitignore`, dead-code audits, stale document references, rename
surgery, and one-shot scaffolding files.

Out of scope: changelog/README prose accuracy (→ `docs-and-changelog`);
lifecycle semantics (→ `ha-quality-scale`); test *design* (→ `test-authoring`).

## The no-callsite rule (recovered SKILL4)

`CHANGELOG.md` 1.2.x records a project rule from a cleanup cycle, originally
captured as `.cline/skills/SKILL4.md`:

> **streamlining — drop every constant, helper, and test stub that has no
> callsite.** A project-specific gotcha captured from this cleanup cycle. The
> rule: a name without a production callsite is debt. Run the audit, delete
> the name and the tests that exist only to pin it.

The 1.4.2 `Removed (loose code)` entry records the rule being applied: dead
constants, helpers, and stubs were deleted, along with the tests that existed
only to pin them.

Applied properly, this means:

- A constant with no reads is deleted, not "kept for future use".
- A helper with no production callsite is deleted — a helper called only from
  a test is still dead in production terms.
- The tests that pin a deleted name go **with** it. A test whose only subject
  was the removed name is itself the debt.
- The one legitimate exception is a name that is *itself* the contract:
  a public HA symbol, a `manifest.json` key, a translation key, or a
  `CONF_*` option that the config flow reads. Verify by searching for
  production reads, not for the definition.

Note the interaction with the coverage gate: deleting dead code **raises**
coverage, so the audit and `--cov-fail-under=100` agree. If a deletion makes
coverage *drop*, you deleted something reachable — investigate before
committing.

## `.gitignore` traps

This repository has already been bitten by a blanket ignore rule, so the rules
here are specific:

1. **A blanket ignore can hide version-controlled content.** A previous
   `.cline/*` rule made the whole skills tree untracked while the changelog
   and README still referred to files inside it — producing exactly the
   dangling references this skill prevents. Prefer naming the local,
   machine-specific files (`settings.json`, `tasks/`, `mcp.json`) over
   excluding a whole tree.
2. **Git cannot re-include a file whose parent directory is excluded.** If a
   directory is matched, adding `!path/inside` lines afterwards has no effect.
   The fix is to stop excluding the directory and ignore the local files by
   name. Verify with `git check-ignore -v <path>` and `git status --porcelain
   --ignored=matching -- <dir>`.
3. **`.vscode/*` is already inverted correctly** in `.gitignore` — a set of
   `!` negations for `settings.json`, `tasks.json`, `launch.json`,
   `extensions.json`, and `*.code-snippets`. That is the pattern to copy for
   a directory that is mostly local but has a few shared files.
4. **Scaffolding scripts are never committed.** The file has a block of
   `gen.py`, `write_generic.py`, `update_generic.py`, `temp_content.py`, etc.,
   each ignored on purpose and annotated not to re-add. Recreate them locally
   for a one-shot refactor, then `git clean`.

## Stale document references

Before finishing any change, search the tree for references to files that may
not exist:

- `SPEC.md` and `ROADMAP.md` are **absent** but cited from several source
  docstrings and test files. They are historical; leave existing citations
  alone unless the task is to clean them (out of scope here), but never add a
  new citation to them.
- The README previously linked `.cline/ROADMAP.md`; it now points at
  `.cline/README.md`. Keep in mind that a deleted agent-config directory
  leaves prose links behind.
- `models.py` cites `.cline/skills/architecture.md`; the equivalent skill is
  `.cline/skills/architecture-map.md`. Keep a citation accurate when the file
  it names is renamed.

The rule: after creating, renaming, or deleting any file, `grep` the repo for
its old and new path and fix every reference that now dangles.

## Refactor safety

- Do a cross-cutting rename as a single atomic change (source + tests +
  docs), then run the full gate. A half-done rename fails the gate anyway; a
  split one is just a harder failure to read.
- After a refactor, re-run the tripwire and the coverage gate — a refactor
  that drops an `except` branch can quietly change grammar or coverage.
- Preserve a tripwire's *purpose* when moving it. The repository treats
  test-guards-on-guards (e.g. the syntax tripwire's self-test) as
  load-bearing; do not "simplify" them away.

## Procedure

1. For a dead-code pass: list candidate names, confirm each has no production
   read, then delete the name **and** the tests that only pin it.
2. For a `.gitignore` change: `git check-ignore -v` the paths, then
   `git status --porcelain --ignored=matching` to confirm the intent.
3. After any file add/rename/delete: grep for the path and fix dangling
   references.
4. For a rename: change source + tests + docs together, then run the full gate.
5. Record intentional removals in `CHANGELOG.md` under `### Removed` with the
   reason (→ `docs-and-changelog`).

## Validation

```powershell
# Dead-code candidates and stale references.
git grep -n "SPEC\.md\|ROADMAP\.md"
.\.venv\Scripts\python -m ruff check .   # F401/F841 flag unused imports/locals

# Ignore-rule verification.
git check-ignore -v .cline/skills/architecture-map.md
git status --porcelain --ignored=matching -- .cline

# The gate.
.\.venv\Scripts\python -m pytest --cov=custom_components.telegraf_mqtt
```

## Completion criteria

- No constant/helper/stub without a production callsite remains (or the one
  you kept is a genuine contract symbol, and you can say which).
- No test exists solely to pin a deleted name.
- No file is accidentally ignored; the skills tree is trackable.
- No citation or link points at a file that does not exist.
- `pytest -q` passes and coverage did not regress.

## Boundaries

- Never keep a "maybe useful later" constant; that is exactly the debt the
  no-callsite rule names.
- Never delete a test that pins real behaviour to make coverage pass.
- Never re-add an ignored scaffolding script.
- Never introduce a blanket directory ignore over version-controlled content.
- Never edit the release/version tests to accommodate a structural change.

## Handoff

- Changelog/README/version surfaces → `docs-and-changelog`
- Test *content* and coverage design → `test-authoring`
- Behaviour change with HA impact → `ha-quality-scale`
