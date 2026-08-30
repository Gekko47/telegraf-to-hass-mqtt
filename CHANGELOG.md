# Changelog

All notable changes to this project will be documented in this file.

## [1.2.0] - 2026-08-29

Phase 10 — 🏆 Platinum quality-scale gate & HACS-release polish.

### Added
- **Auto-discover via long-lived snoop listener** (`snoop.py`): a second MQTT
  subscription on `telegraf/#` re-injects every captured message into the
  existing `parse -> route -> render` pipeline, so new Telegraf hosts
  auto-create devices and entities without the user adding another config
  entry. The snoop's teardown handle is parked on
  `runtime_data.unsubscribe_snoop` and released in `async_unload_entry`.
- **Per-entity category overrides** (`naming.apply_category_override`):
  `category_overrides` in the options flow lets the user re-classify any
  `unique_key` as `config` / `diagnostic` / `None` (clear the auto-assigned
  category) so noisy diagnostic entities can be promoted to the primary list.
- **Per-field `platform_hint`**: the `field_overrides[*].platform` option
  accepts `"auto" | "sensor" | "binary_sensor" | "none"` so a field can be
  routed to either platform, or skipped entirely, at the field level.
- **Configurable `device_id_strategy`**: `host` (default), `host_topic`, or
  `topic_only`, surfaced in the options flow.
- **Five Repairs issues** wired into `async_setup_entry` and the periodic
  expiry tick: `no_traffic_on_topic`, `device_id_collision` (two host
  tags slug-collide), `device_id_conflict` (two config entries
  slug-collide), `overlap_topic_patterns`, `invalid_persisted_option`. All
  five auto-resolve when the underlying condition clears; all five
  defensive-guard paths (no `runtime_data`, no `manager`, no issue
  registry) are covered by tests.
- **Strict typing gate**: `mypy --strict` runs in CI on
  `custom_components/`. New `Literal[...]` types cover
  `PlatformHint`, `CleanupPolicy`, `DeviceIdStrategy`, and `MessageFormat`.
  New `TypeGuard`s (`is_bool_metric`, `is_numeric_metric`,
  `is_string_metric`) drive the platform hint through the options flow.
- **Cost-cut: xdist-enabled local runs**: recommended dev / pre-commit
  invocations are now `pytest -n auto` (~42s) instead of the sequential
  gate (~70s). The CI gate stays sequential for deterministic coverage
  reporting. `pyproject.toml` `[tool.pytest.ini_options]` and
  `tests/test_placeholder.py` both pin the discovery surface so the
  xdist regression cannot recur silently.

### Changed
- `MetricDescriptor` is translation-only on the entity-facing surface
  (no more resolved display `name`).
- `SnoopListener` no longer auto-stops; the diagnostics probe path
  remains one-shot, but the live integration's listener is long-lived.
- `README.md` documents the `mypy --strict` and `ruff` commands and the
  sharded / sequential test invocations.

### Fixed
- `__init__.py` import block is now isort-clean; pre-commit `ruff check`
  and `ruff format` pass on the whole tree.

## [1.1.4] - 2026-08-28

Phase 9 (Gold) clean-cut plus all of Phases 6 / 7 / 8 (intelligent
cleanup, diagnostics, Silver hardening). Released as 1.1.4 because
Phases 6–8 are bug-fix / reliability level per semver; 1.2.0 is reserved
for the Platinum typing gate.

### Added
- **Phase 9 (Gold):** `translation_key` + `translation_placeholders`
  pattern on every entity; full `en.json` / `strings.json` translation
  coverage; MDI icon table (`icons.ICON_FOR_KEY`) mapping the
  `ICON_KEY_*` constants; translated exceptions (`exceptions.py` —
  `ReconfigureSubscribeFailed`, `MqttBrokerUnreachable`); `sw_version`
  on `DeviceInfo`; config-entry **reconfigure flow** that updates topic
  pattern + device metadata in place; **disabled-by-default** for
  diagnostic entities; **stale device pruning** after `delete_delay`; a
  reappearing host recreates a fresh registry.
- **Phase 8 (Silver):** edge-triggered availability logging (one INFO
  line per state transition, not per tick); parser hardening against
  malformed-payload floods; unload / reload cycle leak test; performance
  harness (100+ entities @ 1 Hz) with bounded `async_write_ha_state`
  calls; CODEOWNERS file; full README option documentation.
- **Phase 7:** `diagnostics.py` exposes a redacted payload (parser
  stats, per-device measurements, last-message metadata, options
  validity); `repairs.py` raises `overlap_topic_patterns` and
  `invalid_persisted_option` issues via `ir.async_create_issue` with
  automatic resolution.
- **Phase 6:** intelligent cleanup lifecycle — `enable_cleanup`,
  `cleanup_delay`, `delete_delay`, `min_active_metrics` are all
  OptionsFlow-tunable; `prune_empty_devices` is throttled and the
  scheduled callback is `@callback`-marked to avoid executor-thread
  dispatch; collision-resistant device ID slugification.

### Changed
- Entities are translation-first: `_attr_translation_key` +
  `_attr_translation_placeholders` from the descriptor, no
  `_attr_name`. `_attr_entity_registry_enabled_default = (entity_category
  != "diagnostic")`.
- `_apply_overrides` in the registry carries translation fields
  through unchanged.
- `ConfigEntryNotReady` for an unreachable broker now carries
  `translation_domain=DOMAIN`, `translation_key="mqtt_broker_unreachable"`,
  and the topic / error placeholders.

## [1.1.0] - 2026-08-01

### Fixed
- Corrected the Phase 3 naming path so host metadata no longer leaks into the user-visible entity title.
- Normalized measurement naming to resolve the alias-table and field-alias flow consistently for generic Telegraf payloads.
- Preserved the deterministic host-excluding `unique_key`/descriptor identity path while improving display-name quality.

### Added
- Regression coverage for the reference parser naming path and generic fallback behavior.

## [1.0.0] - 2026-08-01

### Added
- Initial Home Assistant custom integration release surface.
- MQTT subscription and parser support for Telegraf-style JSON payloads.
- Registry-backed state projection to Home Assistant sensors and binary sensors.
- Config flow and options flow support.
- HACS and Home Assistant packaging metadata alignment.
