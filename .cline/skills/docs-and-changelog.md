# docs-and-changelog

## Purpose

Keep the release, changelog, README, translation, and HACS surfaces accurate
and mutually consistent. Several of these are enforced by tests, so drift is a
red build, not a documentation nit.

## When to use

- Cutting or preparing a release; bumping a version.
- Adding or changing a user-facing option, entity name, or flow label.
- Any change that makes a README statement untrue.
- A release-readiness test fails on a version or metadata mismatch.

## Scope

In scope: `custom_components/telegraf_mqtt/manifest.json`, `pyproject.toml`
`[project] version`, `CHANGELOG.md`, `hacs.json`, `README.md`,
`strings.json`, `translations/en.json`, `translations_strings.py`.

Out of scope: option *behaviour* (→ `config-options-review`); icon keys
(→ `adding-telegraf-parsers`).

## The version-sync contract

`manifest.json` is the **canonical** version surface — HACS and HA read it and
CI's `hassfest` validates it. Every other surface is locked to it by
`tests/test_release_readiness.py`, so a release that bumps one without the
others fails the test gate:

| Surface | Test | Rule |
|---|---|---|
| `manifest.json` | (canonical) | `version` is the source of truth |
| `pyproject.toml` `[project] version` | `test_pyproject_version_matches_manifest` | must equal the manifest version |
| `CHANGELOG.md` | `test_changelog_declares_manifest_version` | must contain a `## [x.y.z] - date` heading for the current version |
| `hacs.json` | `test_hacs_packaging_metadata_is_release_ready` | packaging metadata (homeassistant floor) must match |
| `strings.json` + `translations/en.json` | `test_manifest_and_translations_are_release_ready` | pins `domain`, `name`, `codeowners`, `documentation`, `issue_tracker`; flow labels under `config.step.manual_topic`, `config.step.user`, `config.step.options` |
| `README.md` | `test_readme_documents_install_and_configuration`, `test_readme_documents_xdist_fast_path` | install/config documented, and the `pytest -n auto` fast path present |
| `pyproject.toml` xdist docs | `test_pyproject_documents_xdist_invocation` | the recommended dev invocations stay in sync with the README |

The static `pyproject.toml` version is deliberately *not* a floating value: it
is pinned so a mismatch is caught, not auto-synced.

## Changelog entry shape

Follow the existing format in `CHANGELOG.md` — Keep-a-Changelog with an
`[Unreleased]` section at the top, then `## [x.y.z] - YYYY-MM-DD`. Use the
standard subsection headings the file already uses: `### Added`,
`### Changed`, `### Fixed`, `### Removed (loose code)`, `### Documentation`.

The house style for an entry is unusually specific, and the existing entries
model it well:

- Lead with a **bold one-line summary of the defect**, then the file path.
- Explain the *root cause*, not just the fix. The 1.4.1 `diskio` entry is the
  model: it names the four real Telegraf field names, states that the existing
  marker table matched none of them, and then says what was added.
- Name the regression risk the change removes.
- For a refactor or a behaviour change, say what an on-3.13 host would have
  experienced (see the 1.2.0 typing-gate entry).

Do not pad an entry with what did **not** change. If a cleanup removed
scaffolding, say so in `### Removed` with the reason it is safe to remove.

## New user-facing strings

A new entity name is a **translation key**, not a literal string:

1. Add the key and English text to `translations/en.json`.
2. Mirror it in `strings.json` (the source) and in `translations_strings.py`
   (the in-process English translator mirror).
3. Set `translation_key` + `translation_placeholders` on the descriptor.
4. If the entity has an icon, add the key to `icons.py`.

An unmapped key shows up in the UI as the raw `translation_key`, and
`test_manifest_and_translations_are_release_ready` will not necessarily catch
it — so verify by reading the JSON, not by trusting the tests.

## README accuracy

The README makes checkable claims: the quality-gate table
(`ruff check .` / `mypy --strict custom_components` / `pytest --cov=...`), the
"Main implementation areas" list, the phase roadmap, and the `pytest -n auto`
fast path. If you add or rename a module, update that list. If you change a
gate command, update both the README **and** the `pyproject.toml` comment
(the xdist test compares them).

Do not link to files that do not exist. If the roadmap or a plan document is
absent, either create it or remove the link — a dangling link in a README is
exactly the failure this skill exists to prevent (see `repo-hygiene`).

## Procedure

1. Identify every surface the change touches: manifest, pyproject, changelog,
   hacs, README, strings, translations.
2. If it is a release, set `manifest.json` first, then bring every other
   surface to it.
3. Write the changelog entry in the established shape, with the root cause.
4. Run the release-readiness suite — it is the authority on sync.
5. Check the README's claims still hold.

## Validation

```powershell
# The release gate.
.\.venv\Scripts\python -m pytest tests/test_release_readiness.py -q

# Translation shape.
.\.venv\Scripts\python -m pytest tests/test_phase9_gold.py -q

# Full gate.
.\.venv\Scripts\python -m pytest -q
```

CI additionally runs `hassfest` and HACS workflows, which validate
`manifest.json` and packaging against Home Assistant and HACS schemas.

## Completion criteria

- `manifest.json`, `pyproject.toml`, `CHANGELOG.md`, and `hacs.json` agree.
- `CHANGELOG.md` has a dated heading for the current version under
  `[Unreleased]`.
- New strings exist in `strings.json`, `translations/en.json`, and
  `translations_strings.py`.
- README claims about gates, modules, and the fast path are still true.
- `pytest tests/test_release_readiness.py -q` passes.

## Boundaries

- Never bump `pyproject.toml` without the manifest (and vice versa); the test
  gate enforces it — use it, don't work around it.
- Never introduce a literal entity display string; Phase 9 made display
  translations-only.
- Never add a README link to a file that does not exist.
- Never edit the version-sync tests to make a mismatch pass.
- Do not treat `SPEC.md` / `ROADMAP.md` as existing; they are absent.

## Handoff

- Option definition/behaviour → `config-options-review`
- New measurement entities and icons → `adding-telegraf-parsers`
- Dangling links and stale references → `repo-hygiene`
