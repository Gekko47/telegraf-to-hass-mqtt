# Changelog

All notable changes to this project will be documented in this file.

## [1.4.1] - 2026-09-06

### Fixed
- **diskio: four of the plugin's most useful fields get no unit at all**
  (`parsers/generic.py`). Telegraf's real diskio field names are
  `read_time`, `write_time`, `io_time`, `weighted_io_time` (bare names,
  no `_ms` suffix) and `io_util`. The previous `_MS_FIELD_MARKERS`
  only matched names containing `response_ms` / `_time_ms` /
  `latency_ms` / `duration_ms` -- none of which appear in Telegraf's
  actual field names -- so these four duration fields and the
  `io_util` percent gauge got no unit, no device_class. Added the
  four time fields to `_MS_FIELD_MARKERS` and a special case for
  `io_util` -> `%` in `infer_native_unit`.
- **wireless: every packet counter silently becomes a plain gauge**
  (`parsers/generic.py`). Telegraf's wireless plugin reports
  `nwid`, `crypt`, `frag`, `retry`, `misc`, `missed_beacon` as
  counter-typed fields upstream. None of the six were in
  `_TOTAL_INCREASING_FIELDS` or matched a byte marker, so they all
  fell through to the gauge default. Added all six to
  `_TOTAL_INCREASING_FIELDS` so they correctly map to
  `state_class="total_increasing"`.
- **ipmi_sensor: the entire unit-mapping feature is dead code**
  (`parsers/ipmi_sensor.py`, `parsers/generic.py`). IPMI's single
  field is always named `value`; the real unit (temperature, RPM,
  volts, watts, etc.) lives on the `unit` tag. The previous
  `_UNIT_TO_DEVICE_CLASS` dict in `ipmi_sensor.py` was defined but
  never imported by any platform layer, so every IPMI sensor (fan,
  temp, voltage, power, current) got zero unit and zero
  device_class. Replaced with a new `_TAG_UNIT_MAPPINGS` registry
  in `parsers/generic.py` that reads the `unit` tag at parse time
  and applies the mapping. The registry is extensible for future
  measurements that put units in tags.

### Changed
- **`coerce_to_bool` now recognises explicit-false string values**
  (`models.py`). Previously only the truthy strings
  (`"true"`, `"1"`, `"yes"`, `"on"`) were special-cased; a string
  `"false"`, `"0"`, `"no"`, `"off"` (any case) was treated as
  truthy because it was non-empty. Now the explicit-false strings
  return `False`, matching the existing truthy contract.
- **Handler error logging upgraded from DEBUG to WARNING**
  (`parser.py`). A faulty per-measurement handler now surfaces in
  the operator's log at WARNING instead of being silently swallowed
  at DEBUG. The `dropped_parser_error` counter continues to bump so
  diagnostics still aggregate the failure rate.
- **Invalid category override values now log a WARNING**
  (`naming.py`). Previously a typo in a `category_overrides` value
  silently fell through to the heuristic result. The function now
  emits a `WARNING` describing the accepted values
  (`"config"`, `"diagnostic"`, `""`, `None`) and the bad value that
  was provided.
- **Cleanup-policy time constants use `datetime.timedelta`**
  (`const.py`). The 30 / 60-day defaults now read
  `int(timedelta(days=30).total_seconds())` instead of the
  `30 * 24 * 60 * 60` magic number; same value, self-documenting.
- **Removed duplicate translation key definitions**
  (`naming.py`). Two blocks of `TK_SYSTEM_FIELD` /
  `TK_KERNEL_FIELD` / `TK_PROCESSES_FIELD` / `TK_SWAP_FIELD` /
  `TK_DISKIO_FIELD` / `TK_PING_FIELD` / `TK_SMART_FIELD` /
  `TK_WIRELESS_FIELD` / `TK_DOCKER_FIELD` / `TK_ZFS_FIELD` /
  `TK_NET_RESPONSE_FIELD` / `TK_HTTP_RESPONSE_FIELD` /
  `TK_INTERRUPTS_FIELD` / `TK_IPMI_FIELD` existed; the second
  block (with placeholder comments) is the authoritative one and
  the first block has been removed.
- **Removed dead `_UNIT_TO_DEVICE_CLASS` dict**
  (`parsers/ipmi_sensor.py`). The table was superseded by
  `_TAG_UNIT_MAPPINGS` in `generic.py`; the file's own updated
  docstring already pointed to the new mechanism. Kept the
  parser module to a single function with a clear contract.
- **Moved QA/QC plan to `docs/`**. The planning document
  previously sat at the repo root (`QA_QC_PLAN.md`); it now lives
  at `docs/qa-qc-plan.md` so the repo root is reserved for the
  integration itself.

## [1.4.0] - 2026-09-04

### Added
- **Fleet-scale device and metric caps** (`registry.py`, `repairs.py`,

### Added
- **Fleet-scale device and metric caps** (`registry.py`, `repairs.py`,
  `const.py`, `__init__.py`). A single config entry now caps the
  number of distinct Telegraf hosts (devices) it tracks at
  ``DEFAULT_MAX_DEVICES = 50`` and the number of metrics per device
  at ``MAX_METRICS_PER_DEVICE = 1000``. When a new device would exceed
  the cap, the manager drops the measurement and increments
  ``dropped_device_count``; the Repairs framework consults this to
  raise a ``device_cap_reached`` hint. Same for metrics and
  ``metric_cap_reached``. Both checks are idempotent create-or-delete
  calls and self-guard when the issue registry is unavailable. A value
  of 0 disables the respective cap entirely.
- **Phase 11: per-device metric cap raised to 1000 entities**
  (`const.py`). The previous cap of 50 silently dropped Telegraf
  fields on any host running the full system / lm_sensors / docker /
  smart / net plugin set; raising the cap to 1000 lets a single host
  surface every field the broker carries. The default device cap
  (``DEFAULT_MAX_DEVICES = 50``) is unchanged -- a homelab broker is
  unlikely to carry 50 distinct physical hosts. Both are user-
  overridable; ``0`` disables the cap entirely.
- **Phase 11: auto-formatting + new measurement parsers**
  (`units.py`, `parsers/`, `parser.py`, `naming.py`, `icons.py`,
  `translations_strings.py`, `translations/en.json`, `strings.json`).
  Every Telegraf input plugin in the homelab surface area is now
  recognised as a first-class measurement (``diskio``, ``system``,
  ``kernel``, ``kernel_vmstat``, ``processes``, ``swap``, ``ping``,
  ``smart``, ``docker``, ``wireless``, ``zfs``, ``net_response``,
  ``http_response``, ``ipmi_sensor``, ``interrupts``) with the right
  ``native_unit``, ``suggested_device_class``, ``suggested_state_class``,
  and translation key. The new ``units.py`` module rounds percentages
  to whole numbers (``95 %`` not ``95.0 %``), load averages to two
  decimals, ms-latency to one decimal below 10 ms / zero above, and
  leaves bytes as bytes for Home Assistant's unit-conversion layer to
  render as ``7.38 GB``. The generic parser is unchanged for any
  measurement the dispatcher does not recognise -- backwards
  compatibility is preserved bit-for-bit.
- **Scan progress bar** (`config_flow.py`, `strings.json`,
  `translations/en.json`). The discover-topics scan step now uses HA's
  ``async_show_progress`` / ``async_show_progress_done`` protocol so
  the frontend renders a determinate progress bar
  (``progress_action="scan_running"``) instead of blocking the step
  handler silently for up to 300s. The background scan-wait task
  emits integer percentage updates via ``async_update_progress``; an
  initial 0% event is emitted immediately so the bar renders even
  when the snoop finishes on the first check.
- **CHANGELOG version sync test** (`tests/test_placeholder.py`).
  ``test_changelog_declares_manifest_version`` asserts that the
  current ``manifest.json`` version appears as a ``## [X.Y.Z]``
  heading in ``CHANGELOG.md``, so a release cannot ship without
  notes.

### Fixed
- **New Telegraf measurements on an established device are not picked up
  at first start or after a reconfigure** (`__init__.py`). The integration
  subscribed to MQTT before forwarding to the sensor / binary_sensor
  platforms. With retained traffic or a fast-fire publisher, the broker
  delivered the first metrics into a registry whose
  ``SIGNAL_NEW_METRIC`` dispatcher had zero listeners (the platforms had
  not yet connected theirs), so the new entity was silently dropped.
  A manual reload could mask the symptom by re-running setup in a
  timing window that happened to land cleanly. ``async_setup_entry`` now
  forwards to the platforms BEFORE any MQTT subscription is established;
  retained messages, fresh messages, and snoop-dispatched messages all
  flow through a registry whose dispatcher listeners are already live.
  A regression test
  (``test_retained_message_during_subscribe_reaches_platforms`` in
  ``tests/test_phase10_ux.py``) pins the contract: a retained message
  delivered inside ``mqtt.async_subscribe`` must produce a
  ``SIGNAL_NEW_METRIC`` dispatch and the metric must end up in the
  manager. The test was verified to fail against the pre-fix ordering
  with ``call_order=['subscribe', 'subscribe', 'forward']`` and pass
  against the post-fix ordering.
- **`auto_discover` toggle now takes effect live** (`__init__.py`).
  The option was only ever read at setup time: turning it on via the
  options flow silently did nothing until the entry was reloaded, and
  turning it off left the long-lived snoop listener subscribed and
  dispatching into the pipeline indefinitely. The snoop start/stop
  wiring moved into `_apply_auto_discover`, shared by setup and the
  live options-update listener, so the toggle starts/stops the
  listener in place (idempotently, non-fatal on start failure) and
  logs both transitions.
- **Entity translations converted to hassfest-compliant mappings**
  (`strings.json`, `translations/en.json`). hassfest now requires every
  `entity.<platform>.<translation_key>` value to be a mapping with a
  `name` key (e.g. `{"name": "CPU {field}"}`) and rejects bare
  strings, which failed the hassfest CI job with
  `expected a mapping at 'entity.sensor.cpu_field'`. The rendered
  names are unchanged; `translations_strings.py` (the in-process
  English mirror) already used the bare-string form and is unaffected.
- **Discover-topics pick form is single-select** (`config_flow.py`).
  The form rendered a multi-select list (`multiple=True`) while
  `async_step_pick_topics` silently kept only the first pick
  (`picks[0]`) -- a user selecting several prefixes got an entry
  covering just one of them, with no error, warning, or note. The
  runtime subscription supports exactly one pattern per entry
  (`mqtt.async_subscribe` is called once with the configured
  `topic_pattern`), so the picker is now `multiple=False`, the
  submission is a single string, and the first Telegraf-shaped prefix
  is the form default. `strings.json` + `translations/en.json` no
  longer invite multi-picking; users who want another topic root add
  another entry.

### Changed
- **Expiry tick floor raised from 1s to 5s** (`const.py`, `__init__.py`).
  The periodic registry scan (`check_expiry` + `cleanup` +
  `prune_empty_devices` + the no-traffic Repairs check) runs
  synchronously on the event loop, so its interval is now
  `max(MIN_EXPIRY_TICK_SECONDS, min(expire_after, MAX_EXPIRY_TICK_SECONDS))`
  instead of flooring at 1s: a once-per-second full scan across every
  device would add measurable event-loop latency at fleet scale. The
  scan is O(devices × metrics) and shares the loop with all of Home
  Assistant. Staleness detection is timestamp-based, so the only
  observable difference is that availability flips for `expire_after`
  values below 5s are marked up to 5s late.

## [1.3.0] - 2026-08-31

Two-path config flow + snoop safety cleanup + test-scope audit closure.
This is a feature-level release: the user-facing onboarding now offers
"discover topics from broker traffic" as a first-class alternative to
typing a topic pattern, and the post-setup snoop no longer silently
widens past the user's `topic_pattern` on a shared broker.

### Added
- **Two-path config flow** (`config_flow.py`): a mode picker at
  `async_step_user` lets the user choose between **Manual** (enter a
  topic pattern) and **Discover** (let the broker tell you what's
  available). The discover path runs a short-lived snoop for the
  configured scan window (5-300s, default 30), rolls the captured
  topics up to 2nd-level prefixes (`telegraf/rack1/cpu` +
  `telegraf/rack1/mem` → `telegraf/rack1/#`), pre-selects the
  Telegraf-shaped ones, and creates the entry with the chosen
  prefix. Telegraf-shaped pre-selection keeps the pick form useful
  on a shared broker where HA-internal topics are also in the
  scan results.
- **User-supplied scan root**: the discover path's probe topic is
  user-supplied (default `telegraf/#`) -- a user on `telegraf/rack1/#`
  can scan just that subtree and the scan will not see rack2
  traffic. Topic-level isolation is preserved end-to-end.

### Changed
- **`DEFAULT_AUTO_DISCOVER` flipped from `True` to `False`**
  (`const.py`). Topic discovery now happens during the config flow
  only. The post-setup snoop is opt-in via the options flow. A
  `False` default closes the "shared-broker auto-widening" footgun
  -- a user who wants the snoop to pick up new Telegraf hosts
  under their existing pattern opts in explicitly.
- **Post-setup snoop probe is derived from `topic_pattern`**
  (`__init__.py`, `snoop.derive_probe_topic`). The snoop no longer
  hardcodes `telegraf/#`; it subscribes to whatever the user
  configured. A user on `telegraf/rack1/#` gets a snoop on
  `telegraf/rack1/#` -- rack2 on the same broker stays invisible
  to the auto-discover path.
- **`SnoopListener.__init__` is explicit about its `probe_topic`
  and `timeout_seconds`**. No defaults -- every callsite passes
  both, and the integration's wiring is the one source of truth.
  Trip wire: a refactor that hardcodes a wider probe is caught
  by `test_setup_entry_rack1_topic_runs_snoop_on_rack1_only`.

### Removed (loose code)
Following SKILL4 ("every constant, helper, and test stub must have
a callsite"):
- `const.py`: `CONF_AUTO_DISCOVER_PROBE_TOPIC`,
  `CONF_AUTO_DISCOVER_TIMEOUT`, `DEFAULT_AUTO_DISCOVER_TIMEOUT`.
  The options-flow field and the diagnostics-probe path they
  referenced were never built. `DEFAULT_AUTO_DISCOVER_PROBE_TOPIC`
  remains as a fallback inside `derive_probe_topic` and
  `SnoopListener`.
- `snoop.py`: dead defaults on `SnoopListener.__init__` (every
  production callsite passes them explicitly); an overly long
  `derive_probe_topic` docstring that claimed a `+` → `#` widening
  rule the function never implemented.
- `config_flow.py`: `_synthesize_topic_pattern` (the production
  code uses `picks[0]` directly), the module-level aliases
  `roll_up_topics` / `synthesize_topic_pattern` /
  `looks_telegraf_shaped` (no callers), and their `__all__` entries.
- `tests/test_discover_topics.py`: 5 broken
  `test_discover_path_*` tests that drove the real
  `TelegrafMqttConfigFlow` under stubbed HA machinery; their
  supporting `_FlowHass` / `_Flow` / `_install_fake_subscribe` /
  `_drive_discover` stubs.
- `tests/test_config_flow.py`: 2 dead `_synthesize_topic_pattern`
  tests.

### Test-scope audit closure
A test-scope review found 10 gaps across 3 priorities, all
closed in this release:
- **Phase 1 — security posture**: `DEFAULT_AUTO_DISCOVER is False`
  is pinned by a one-liner; the integration-level
  `setup_entry_with_default_options_does_not_install_snoop` and
  `options_flow_enables_snoop_on_reload` tests pin the no-opt-in
  default and the opt-in path respectively.
- **Phase 2 — discover-path coverage**: `_validate_scan_settings`
  4 branches, both `async_step_pick_topics` error paths
  (`invalid_topic`, `no_topics_selected`), and the
  `_pick_topics_schema` pre-selection wiring are all pinned.
- **Phase 3 — harness strength**: the rack1/rack2 isolation
  invariant at the integration layer (not just on the snoop),
  the roll-up's mixed single + multi-segment handling, and the
  title-casing for dashes and non-ASCII inputs.
- Total: 424 → 434 tests passing (no regressions). 100% coverage on
  `custom_components/`. mypy --strict clean.

### Documentation
- `.cline/skills/SKILL4.md` (new): "streamlining -- drop every
  constant, helper, and test stub that has no callsite." A
  project-specific gotcha captured from this cleanup cycle. The
  rule: a name without a production callsite is debt. Run the
  audit, delete the name and the tests that exist only to pin
  it.

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
  `topic_only`, surfaced in the options flow. Changing the strategy in
  OptionsFlow reloads the config entry (it cannot be applied live because
  the existing `DeviceManager.devices` dict is keyed by the old strategy's
  slugs and a live apply would leave them orphaned while new traffic
  creates a parallel set of registries). All other options still apply
  live without a reload.
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
