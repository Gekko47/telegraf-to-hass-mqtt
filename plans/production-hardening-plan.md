# Telegraf MQTT — Production Hardening Plan

Derived from the repository-wide review of `custom_components/telegraf_mqtt/`
at `manifest.json` version `1.4.2`. Every finding from that review is
reconciled and mapped in §9 (Traceability). Nothing in the review is
dropped; two items are re-classified with reasons in §1.2.

**Status of this document:** plan only. No code is modified by it.

**Maintainer decisions applied (2026-09):**
- **D1 — HA floor is `2026.6.0`.** The three agreeing surfaces
  ([`hacs.json`](hacs.json), [`CHANGELOG.md:7`](CHANGELOG.md:7), CI at
  [`pytest.yml:20-24`](.github/workflows/pytest.yml:20)) are authoritative.
  No change to the version floor.
- **D2 — `auto_discover` must work**, on initial setup *and* on reconfigure.
  It is not removed. §3 WS-I specifies how to make it actually function.

---

## 1. Analysis findings

### 1.1 Confirmed defects (evidence-based, reproducible)

| ID | Summary | Primary location |
|---|---|---|
| C1 | `device_id_strategy` change never reloads the entry; reload path unreachable | `__init__.py:409-432` + `:320-334`, `registry.py:754-755` |
| C2 | Options flow silently resets `expire_after`, `exclude_patterns`, `field_overrides` | `config_flow.py:294,299,307` |
| C3 | Entities never re-appear after a cleanup-cycle removal | `__init__.py:610,695-719`; `sensor.py:38,76-77`; `registry.py:398-434` |
| C4 | Three Repairs issues have no translations | `repairs.py:302,347,404` vs `strings.json:84-101` |
| H1 | `auto_discover` probe topic is the identity → feature delivers nothing, costs 2× work | `snoop.py:38-52`, `__init__.py:389,216-232` |
| H2 | O(N²) dispatcher fan-out; every entity subscribes to the entry-wide signal | `sensor.py:171-177`, `binary_sensor.py:170-176`, `registry.py:835` |
| H3 | `remove_metric_entity` scans the whole HA entity registry; fragile `:` key surgery | `__init__.py:687-691` |
| H4 | Setup can hang forever on `mqtt.async_wait_for_mqtt_client` (no timeout) | `__init__.py:204-208` |
| M1 | Diagnostics `last_message.topic` is permanently `"<unknown>"` | `registry.py:790`, `parser.py:189` |
| M2 | `no_traffic` Repairs issue flaps on any host slower than `expire_after` | `repairs.py:285`, `registry.py:883` |
| M3 | No Repairs issue is fixable (`is_fixable=False` × 7) — *recommendation, not a defect* | `repairs.py:209,258,300,343,401,455,495` |
| M4 | Diagnostics redaction contract violated by verbatim `config.data` | `diagnostics.py:18-21,76` |
| M5 | Diagnostics raises `AttributeError` when `manager is None` | `diagnostics.py:92` vs `repairs.py:282` |
| M6 | `topic_pattern` validity check reads `entry.options`, topic lives in `entry.data` | `diagnostics.py:202-204` |
| M7 | `platform: "none"` leaves a stale `unavailable` entity, contradicting its docstring | `registry.py:240-243,378-396` vs `sensor.py:70-72` |
| M8 | No `async_migrate_entry`; `entry.data[CONF_TOPIC_PATTERN]` indexed unguarded | `__init__.py:184`, `config_flow.py:371` |
| M9 | `config.abort.reconfigure_successful` missing | `config_flow.py:730` vs `strings.json:77` |
| M10 | `auto_discover` / `device_id_strategy` unlabelled; `field_overrides` undocumented | `config_flow.py:285-286,307` vs `strings.json:50-63` |
| M11 | `_seen_topics` / `_seen_hosts` / `_host_to_device_id` grow unbounded | `registry.py:599-608`, `repairs.py:293` |
| M12 | Scan subscribe failure escapes as an unhandled exception | `config_flow.py:688` |
| M13 | `pending_cleanup_all()` never surfaced in diagnostics | `const.py:138`, absent from `diagnostics.py` |
| **M14** | **`delete_delay` can never fire** — the device-pruning path is unreachable, so dead devices accumulate forever and eventually exhaust the device cap | `registry.py:1039-1058` × `:1077` |
| N3 | **Reconfigure never validates the new topic pattern** — it writes `data_updates` and reloads unconditionally | `config_flow.py:696-752` |

### 1.2 Findings re-classified

**H5 — `hacs.json` HA version floor: CLOSED, not a defect (decision D1).**
The review could not confirm whether `2026.6.x` exists because no virtualenv
was present in the workspace. The three agreeing surfaces are authoritative
and mutually consistent:

- `CHANGELOG.md:7` dates release `1.4.2` at **2026-09-07**; every prior
  entry is dated 2026.
- `pytest.yml:20-24` pins CPython **3.14** and
  `pytest-homeassistant-custom-component==0.13.340`, commented "this release
  depends on `homeassistant==2026.6.4`".
- `test_harness.py:108` asserts `HA_VERSION == "2026.6.4"` — a test that can
  only pass against a real installed 2026.6.4.
- `hacs.json` declares `2026.6.0` as the HACS minimum.

**No code or metadata change is planned for H5.** It is retained in the
traceability table as *closed by decision*, so a later reader can see it was
examined rather than forgotten. Phase 0's baseline capture is retained, but
the conditional "if the floor is wrong, fix it" branch is deleted.

**N1 (surfaced during planning, low) — `_resolve_category_for`
docstring/behaviour divergence.** The docstring at `registry.py:44-47` claims
the function avoids stomping a descriptor's non-default category. It only
avoids re-deriving the heuristic when *no* override key matches; for matched
keys it calls `apply_category_override`, which re-runs
`resolve_entity_category(measurement, field)` (`naming.py:324`). Harmless
today (no parser sets a category outside the heuristic set), but the
docstring promises an invariant the code does not hold.

**N2 (surfaced during planning, low) — unguarded enum construction.**
`_entity_category` calls `EntityCategory(value)` directly
(`sensor.py:32`, `binary_sensor.py:32`). A parser or hand-edited
`entry.options` value outside `{config, diagnostic}` raises `ValueError`
inside `_refresh_descriptor_attributes`, which runs both in the entity
constructor and in `_handle_metric_updated`. The `category_overrides` path
is currently safe because `naming.apply_category_override` validates values
(`naming.py:331-349`); the risk is from the parser surface.

### 1.3 Duplicates merged

- **C3 and M7 are one defect with two triggers.** Same root cause: the
  platform's `added` set and HA's entity registry are two sources of truth
  with no reconciliation on `SIGNAL_REMOVE_METRIC`. C3 is the cleanup-cycle
  trigger; M7 is the `platform: "none"` trigger. Planned as one workstream
  (WS-C).
- **M4, M5, M6 and M13 all touch `diagnostics.py`** and are one workstream
  (WS-F).
- **M1 is a precondition for M13** — one-line change, ships with the
  diagnostics payload edit.
- **H3 and C3 share the `remove_metric_entity` payload redesign** (WS-C).
- **N3 and H1 both concern reconfigure-time correctness** and are sequenced
  together in WS-I, because the reconfigure pre-flight and the auto-discover
  re-subscription are the same code path.

---

## 2. Home Assistant version constraints

The project targets **HA 2026.6.0-or-newer** (`hacs.json`, per decision D1)
on **CPython 3.14+** (`pyproject.toml:requires-python`). Every API this plan
relies on is available at that floor, and several current workarounds exist
*only* because of an incorrect assumption that the API might be absent.

| Surface | Constraint | Consequence for this plan |
|---|---|---|
| `mqtt.async_wait_for_mqtt_client` | Real HA API, described in-code as "canonical on HA 2026.6" (`__init__.py:195-196`) | **Remove the `hasattr` guard** at `__init__.py:204`. With a 2026.6.0 floor (D1) the call is guaranteed. The guard exists only for "older HA test doubles" (`__init__.py:200-203`) — deleting it lets the test fakes converge on the real signature instead of the reverse. |
| `asyncio.timeout` | 3.11+ | Available. H4's fix is a plain `async with asyncio.timeout(...)`. |
| `type X = ...` (`models.py:15`) | 3.12+ | Available; unchanged. |
| `ConfigFlowResult` (`config_flow.py:32`) | HA 2024.4+ | Available. |
| `ConfigEntry.runtime_data` | HA 2024.6+ | Available. May be unset on a not-yet-loaded entry; every consumer must use `getattr` (existing pattern, `repairs.py:279`). |
| `OptionsFlow.config_entry` | 2024.11+ property; must **not** be assigned | `TelegrafMqttOptionsFlow` correctly does not assign it. M8's migration must not introduce an assignment. |
| `async_update_reload_and_abort` | HA 2024.11+ | Available. M9 adds its `abort` translation; N3's pre-flight sits in the same step. |
| `entity_registry.async_get_entity_id` | Stable, pre-dates 2024 | H3's fix. |
| `ConfigEntryNotReady` + `translation_domain` | Stable | H4's timeout path reuses `_broker_unreachable_not_ready` (`__init__.py:105`). |
| `async_migrate_entry` | Stable | M8. Bumps `ConfigFlow.VERSION` 1 → 2. |
| `SelectorConfig`/`ObjectSelector` | Stable | M10's documented `field_overrides` schema. |

**Compatibility rule for the whole plan:** no change may raise the minimum
HA version above `2026.6.0` (D1), and no change may introduce an API newer
than `2026.6.0`. If any workstream discovers it needs a newer API, it must
stop and be re-planned rather than edit [`hacs.json`](hacs.json).

---

## 3. Workstreams

Workstreams are independent unless a dependency is stated. WS-A through
WS-C are the critical path (data loss). WS-D, WS-F, WS-G are parallelizable
from day one. WS-E is the performance/lifecycle phase.

### WS-A — `device_id_strategy` reload (C1)

**Affected:** `__init__.py` (`_async_options_updated`,
`_async_options_maybe_reload`, listener registration,
`TelegrafMqttRuntimeData`); `registry.py` (nothing structural).

**Current behaviour:** two update listeners are registered in order
(`__init__.py:241` live, `:246` reload). HA awaits them in registration
order. The live listener calls `manager.apply_options(device_id_strategy=...)`,
which writes `manager._device_id_strategy` (`registry.py:754-755`). The
reload listener then compares that already-mutated value against the new
options (`__init__.py:430`) — always equal — so `:432` never executes.

**Required change:** the reload decision must be made against the value in
effect *before* the live apply.

**Implementation actions:**

1. Add `applied_device_id_strategy: str` to `TelegrafMqttRuntimeData`
   (`__init__.py:90-102`), set in `async_setup_entry` from
   `options.device_id_strategy`.
2. Collapse to **one** update listener. `_async_options_updated` becomes the
   single entry point: read `runtime.applied_device_id_strategy` first; if it
   differs from the incoming `options.device_id_strategy`, short-circuit to
   `await hass.config_entries.async_reload(entry.entry_id)` **without** calling
   `apply_options` (a live apply is meaningless when a reload follows).
   Otherwise apply live exactly as today.
3. Delete `_async_options_maybe_reload` and its registration (`:246`).
4. Keep `DeviceManager.device_id_strategy` (`registry.py:859-871`) — still
   the assertion surface for tests, just no longer the comparison source.

**Explicit user-visible behaviour change:** enabling this fix means, for the
first time, changing `device_id_strategy` actually re-derives every device
slug. `CHANGELOG.md:341-348` documents this as the intent and acknowledges
the `unique_id` churn as MAJOR-breaking. Users who changed the strategy while
the reload was dead have a live registry keyed by the *old* slugs; on next
restart their devices re-derive under the new strategy and their entities are
recreated. This is a one-time, self-healing transition and must be a
`### Breaking` note in the release (§WS-H).

**Dependencies:** none. **Blocks:** nothing, but WS-C's regression test for
entity-registry stability should ship in the same release (shared `unique_id`
surface).

**Tests:**

- *Unit* — `tests/test_phase10_ux.py`: replace the isolated
  `test_device_id_strategy_change_triggers_config_entry_reload` (`:707-743`,
  which mutates `entry.options` directly and never runs the live listener)
  with a test driving the **single** listener against a manager whose
  `applied_device_id_strategy` differs, asserting exactly one `async_reload`
  and that `apply_options` was **not** called.
- *Integration* — `tests/test_runtime.py` (already models
  `_update_listeners` at `:57-59`): assert the listener list length is 1 after
  setup, and that a non-strategy option change still applies live without
  reloading.
- *Regression* — `tests/test_harness.py`: set up an entry, feed two hosts,
  `async_update_entry(options={"device_id_strategy": "topic_only"})`, reload,
  re-feed; assert the device registry now has the new slugs and the old
  entity-registry entries are gone (re-created, not preserved — the test pins
  the *documented* behaviour so any future change is deliberate).

**Acceptance criteria:** changing the strategy reloads the entry exactly
once; no other option triggers a reload; no orphaned entity-registry entries
survive a reload cycle.

---

### WS-B — Options-flow option preservation (C2)

**Affected:** `config_flow.py` (`_build_options_schema`, `_clean_options`).

**Current behaviour:** three schema fields default to *constants* instead of
*current values* — `expire_after` (`config_flow.py:294`),
`exclude_patterns` (`:299`), `field_overrides` (`:307`). Every neighbouring
field uses `current_options.get(...)` (`:274-280`). `_clean_options`
(`:313-331`) then persists the defaults verbatim. Opening the options dialog
and saving destroys the user's values silently.

**Required change:** every optional field must pre-fill from
`current_options`, so an untouched field round-trips to its existing value.

**Implementation actions:**

1. In `_build_options_schema`, replace the three hardcoded defaults with
   `current_options.get(CONF_X, DEFAULT_X)`, matching the existing pattern.
2. Add a **defensive merge in `_clean_options`**: prefer the submitted value,
   falling back to the current stored value for keys the form did not render.
   This protects against a client that omits a field.
3. Do **not** change `_clean_options`' output shape — the persisted key set is
   identical, so no stored-config migration is required.

**Migration impact:** none. `entry.options` schema unchanged; only the form's
pre-fill is corrected. Deliberately *not* paired with a version bump.

**Dependencies:** none. WS-I adds a fourth field to the same schema; land
WS-B first so WS-I's field follows the corrected pattern.

**Tests:**

- *Unit* — `tests/test_config_flow.py`: with `current_options` holding
  `expire_after=900`, `exclude_patterns=["disk_*"]`,
  `field_overrides={"usage_idle": {"platform": "sensor"}}`, submit with only
  `auto_discover` changed; assert `_clean_options` preserves all three.
- *Unit* — schema-level: assert each of the three `vol.Optional(...)` keys
  carries the current value as its `default()`.
- *Regression* — `hass.config_entries.async_update_entry` with
  pre-populated options, then assert `entry.options` still holds them after
  the update listener runs.

**Acceptance criteria:** a user with non-default `expire_after`,
`exclude_patterns` and `field_overrides` can open and save the options dialog
untouched and observe an identical stored options dict.

---

### WS-C — Entity lifecycle and removal (C3, M7, H3)

**Affected:** `__init__.py` (`remove_metric_entity`, `_listener_remove_metric`,
`_dispatch_remove_metric`); `sensor.py` + `binary_sensor.py` (`async_setup_entry`
`added` set, and — if WS-E3 lands in the same release — the entity dispatch
table); `registry.py` (`_drop_metric_on_platform_none` docstring).

**Current behaviour, three coupled problems:**

1. `__init__.py:607-610` fires `SIGNAL_REMOVE_METRIC` with a composite key
   `"{device_id}:{unique_key}"` for each metric cleanup removed.
2. `_listener_remove_metric` (`:695-719`) calls `remove_metric_entity`, which
   (a) scans **every** entity in the HA registry (`:688-691`, O(total
   entities) per removal) and (b) reconstructs the platform's `unique_id` via
   `composite_key.replace(':', '_', 1)` (`:687`) — correct only when
   `unique_key` contains no `:`.
3. The platform's `added` set (`sensor.py:38`, `binary_sensor.py:39`) is never
   cleared on `SIGNAL_REMOVE_METRIC`. Only `reevaluate_routing` clears it
   (`sensor.py:76-77`), and that runs on `SIGNAL_METRIC_UPDATED`. When the
   host republishes, `_register_new_metric` (`registry.py:398-434`) fires
   `on_discovered` → `add_metric` → `metric_key in added` → early return →
   the entity is never recreated. `entity_registry.async_remove` also removes
   the state, so the user sees the entity vanish permanently.

For `platform: "none"`, `registry.py:378-396` pops the state and fires
`on_write(..., False, ...)`; both platforms' `reevaluate_routing` return early
on `state is None` (`sensor.py:70-72`), so no remove signal is sent and the
entity lingers as permanently `unavailable`.

**Required change:** make the dispatcher payload unambiguous, make entity
removal O(1), and reconcile the platform's bookkeeping on every removal path.

**Implementation actions:**

1. **Change the `SIGNAL_REMOVE_METRIC` payload** from a single composite
   string to a 2-tuple `(device_id: str, unique_key: str)`. The signal is
   internal — only this integration consumes it — so this is not a public API
   break. Update `_dispatch_remove_metric` (`__init__.py:644-658`) and
   `_listener_remove_metric` (`:695-719`).
2. **Rewrite `remove_metric_entity(hass, device_id, unique_key)`** to build
   `f"{DOMAIN}_{device_id}_{unique_key}"` and resolve it via
   `registry.async_get_entity_id("sensor", DOMAIN, uid)`, falling back to
   `("binary_sensor", DOMAIN, uid)`. Two O(1) index lookups replace the full
   scan, and the `:`-in-`unique_key` ambiguity disappears because the
   composite key is never re-parsed. Keep the `er is None` import-isolation
   guard (`:678-679`).
3. **Have each platform subscribe to `SIGNAL_REMOVE_METRIC`** and
   `added.discard(metric_key)` (plus, if WS-E3 has landed, drop the entity
   from the dispatch table). This is the C3 fix.
4. **Make the `platform: "none"` path send `SIGNAL_REMOVE_METRIC`** rather
   than relying on `on_write(..., False, ...)`. In
   `MetricRegistry._drop_metric_on_platform_none` (`registry.py:378-396`) the
   manager has both `device_id` (on the popped `MetricState`) and
   `unique_key`; route the removal through the same dedicated signal. Keep the
   `on_write(..., False, ...)` call so the availability transition is still
   observed. This is the M7 fix.
5. **Correct the docstring** at `registry.py:240-243`, which currently claims
   the platform removes the entity — after (4) that becomes true.
6. **Document the behaviour** in `README.md`: a metric removed by the cleanup
   lifecycle is deleted from the entity registry; if the host returns the
   entity is re-created. Because `async_remove` frees the `entity_id`, a
   returning entity may receive a **different** `entity_id` than before
   removal (its `unique_id` is unchanged, so no duplicate is created).

**Dependencies:** coordinate with WS-E3 — if both land in one release, step 3
also drops the entity from the dispatch table. Sequence WS-E3 first, WS-C
second, so the `added.discard` is written once against the final structure.

**Tests:**

- *Unit* — `remove_metric_entity` resolves via `async_get_entity_id` (assert
  the method is called and `registry.entities` is never iterated), returns
  `False` when neither platform matches, and builds the `unique_id` correctly
  for a `unique_key` that **contains a colon** (the case the old string
  surgery got wrong).
- *Unit* — `MetricRegistry._drop_metric_on_platform_none` emits the remove
  signal with the `(device_id, unique_key)` tuple.
- *Integration* — `tests/test_phase6_lifecycle.py`: the existing
  `remove_metric_entity(hass, "server01:battery_percentage")` call at `:710`
  must be updated to the new signature. Add a **round-trip** test: metric
  added → cleanup removes it (dispatcher fires) → metric republished →
  entity is back in `hass.states` and the entity registry.
- *Regression* — `tests/test_platform_units.py` already drives
  `SIGNAL_REMOVE_METRIC` on the override-flip path (`:400,441,469,516`);
  confirm those still pass with the new payload, and add the `platform: none`
  case asserting the entity is **gone**, not `unavailable`.

**Acceptance criteria:** a metric that goes quiet past `cleanup_delay` and
returns is present in the entity registry again; `remove_metric_entity` never
iterates `registry.entities`; a `unique_key` containing `:` still resolves.

---

### WS-D — Translations and user-facing strings (C4, M9, M10)

**Affected:** `strings.json`, `translations/en.json`; `repairs.py`
(constant indirection); `const.py`.

**Current behaviour:** `no_traffic_on_topic` (`repairs.py:302`),
`device_id_collision` (`:347`) and `device_id_conflict` (`:404`) are created
with translation keys neither translation file defines. `repairs.py` inlines
every key as a literal while `const.py:141-145` declares `REPAIR_*`
constants nothing uses — the indirection that let the drift happen.

**Implementation actions:**

1. Add the three missing `issues` blocks to **both** `strings.json` and
   `translations/en.json`, with `title` + `description` carrying the existing
   placeholders (`{own_topic}`/`{other_topic}`/`{other_entry_title}`,
   `{configured_topic}`/`{seen_topics}`, `{description}`, `{conflicts}`).
2. Add `config.abort.reconfigure_successful` (M9) — the default reason used by
   `async_update_reload_and_abort` at `config_flow.py:730`. Also add
   `config.abort.cannot_connect` for N3's new pre-flight failure path.
3. Add option labels and descriptions for `auto_discover`,
   `auto_discover_scope` (new, WS-I) and `device_id_strategy` under
   `config.step.options.data`, plus a `field_overrides` `data_description`
   documenting the
   `{"<field>": {"platform": ..., "native_unit": ..., "device_class": ...,
   "state_class": ...}}` shape (M10).
4. **Close the drift permanently:** make `repairs.py` import its keys from
   `const.py`'s `REPAIR_*` constants so a missing key fails at import time
   rather than silently at runtime. Adopt the same for the `_issue_id`
   prefixes.
5. **Add a translation-completeness test** (gate `G2.2`) that fails the build
   if any `translation_key` used in `repairs.py` or `config_flow.py` is absent
   from `strings.json`. This is the durable fix; the three missing strings are
   just the instances it would have caught.
6. Correct the `delete_delay` option description: it gates **empty device**
   pruning via `prune_empty_devices` (`registry.py:1061-1090`), not metric
   deletion. "Delete delay (seconds)" is actively misleading.

**Dependencies:** step 4 touches `repairs.py`, which WS-C also edits.
Sequence WS-D's `repairs.py` edit after WS-C, or coordinate in one diff.

**Tests:** extend `tests/test_release_readiness.py` with
`test_every_used_translation_key_is_defined`, walking `repairs.py` and
`config_flow.py` for `translation_key=` / `errors={...}` / `reason=` literals
and asserting each resolves in `strings.json`. Keep
`test_manifest_and_translations_are_release_ready` (`:255`) as-is.

**Acceptance criteria:** `hassfest` passes; no log line "Translation key ...
not found"; the completeness test is in the CI gate.

---

### WS-E — Async lifecycle, performance and observability plumbing (H4, H2, M1)

**E1 — Bound the MQTT precheck (H4) and wire `MqttBrokerUnreachable`
(`__init__.py:204-208`).** The precheck is awaited with no timeout; HA's
helper resolves only on a client-connected event, so an unreachable broker
leaves the config entry in "setting up" forever with no error and no retry.

```python
try:
    async with asyncio.timeout(BROKER_WAIT_TIMEOUT_SECONDS):
        await mqtt.async_wait_for_mqtt_client(hass)
except (TimeoutError, OSError) as wait_err:
    raise MqttBrokerUnreachable(topic_pattern, str(wait_err)) from wait_err
```

Add `BROKER_WAIT_TIMEOUT_SECONDS = 30` to `const.py` with a comment recording
*why* the bound exists. `MqttBrokerUnreachable`
([`exceptions.py:34-46`](custom_components/telegraf_mqtt/exceptions.py:34))
already carries `translation_domain`/`translation_key`/
`translation_placeholders` for exactly this message, and
`strings.json:106-108` already defines it — the class is currently dead code
and this gives it its production callsite. `_broker_unreachable_not_ready`
(`__init__.py:105-120`) stays for the *subscribe* failure path at `:212-213`,
which must remain a `ConfigEntryNotReady` so HA retries. **Also delete the
`hasattr` guard** — at the 2026.6.0 floor (D1) the API is guaranteed (§2).

*Tests:* unit with a fake that never resolves, asserting a
`MqttBrokerUnreachable` is raised within a patched-short timeout; plus a
smoke test confirming the normal case still subscribes.

**E2 — Pass the topic to the parser (M1).** `registry.py:790` calls
`parser.parse(payload)`, so `ParserStats.last_message["topic"]` is always the
`"<unknown>"` default from `parser.py:189` — the single most useful field in
the diagnostics payload is dead. Change to `parser.parse(payload, topic=topic)`.
One line. Ships with WS-F's payload work.

**E3 — Replace per-entity dispatcher subscriptions with one platform-level
listener (H2).** Every entity connects to the entry-wide
`SIGNAL_METRIC_UPDATED` in `async_added_to_hass` (`sensor.py:169-177`,
`binary_sensor.py:168-176`). HA's dispatcher is a flat list, so each of the M
changed descriptors in one MQTT message invokes all N listeners. At the
project's own caps (`DEFAULT_MAX_DEVICES = 50`,
`MAX_METRICS_PER_DEVICE = 1000`, `const.py:108-109`) a single `disk` or
`system` payload produces hundreds of dispatches against thousands of
listeners, all on the event loop.

Refactor each platform's `async_setup_entry` to hold
`entities: dict[str, TelegrafMqttSensor]` and route:

```python
def route_update(metric_key: str) -> None:
    entity = entities.get(metric_key)
    if entity is not None:
        entity.refresh_from_registry()
```

Replace `reevaluate_routing` with a combined `route_update` that performs
the routing check *and* the state write, so the existing routing tests
(`test_platform_units.py`) keep passing against a single listener. Pop
`entities` in `async_will_remove_from_hass` and in the `SIGNAL_REMOVE_METRIC`
handler (WS-C step 3). Remove the per-entity `async_added_to_hass`
subscription. **Do not delete `_handle_metric_updated`** — rename it and call
it from `route_update` so the unit tests that drive it directly keep working.

*Risk:* **medium-high** — the largest structural change, and it owns the
entity write path. Ship as its own PR behind its own gate.

*Tests:*
- *Performance* — extend `tests/test_phase8_performance.py`. The existing
  tests count `on_write` (`_WriteCounter`, `:23-35`) and therefore **cannot**
  see this defect. Add a counting fake dispatcher recording listener
  invocations per dispatch; drive 4 devices × 25 metrics × 10 ticks; assert
  the total is O(N) per message, not O(N²). Record before/after counts in the
  PR description.
- *Integration* — `tests/test_harness.py`:
  `test_two_hosts_produce_two_grouped_devices` (`:122`) and
  `test_reload_preserves_entity_ids_without_duplicates` (`:143`) must pass
  unchanged; add a test that a value change updates the state machine, and one
  that a repeated identical payload still creates no duplicate entity.

**E4 — Bound the `no_traffic` check (M2) and the seen-sets (M11).** Replace
"has *ever* received a message" (`registry.py:883-885`) with a
sustained-silence predicate: raise only when `last_message_at is None` and
`now - first_message_at > NO_TRAFFIC_GRACE_SECONDS`, or when
`now - last_message_at > max(NO_TRAFFIC_GRACE_SECONDS, 3 * expire_after)`.
This stops the flap for any host publishing slower than `expire_after`.
Separately, cap `_seen_topics` / `_seen_hosts` with bounded structures
(`MAX_SEEN_TOPICS = 500`, `MAX_SEEN_HOSTS = 100`, `const.py`) keeping a
*sample* plus the most recent N. Nothing depends on completeness of the topic
set: `check_no_traffic` reads only the first 5 for its preview
(`repairs.py:293`), and `find_device_id_collisions` only needs host tags.

*Tests:* unit — a fake clock stepping past the grace window with no messages
raises exactly one issue and does not flap; a host publishing at half
`expire_after` never raises. Unit — the cap evicts oldest-first and
`has_received_messages()` stays correct.

**Dependencies:** E3 lands after or with WS-C. E1, E2 and E4 are independent
and can land any time.

---

### WS-F — Diagnostics payload and observability (M4, M5, M6, M13, N2)

1. **M4 (redaction).** `diagnostics.py:76` emits `dict(entry.data)` verbatim,
   including `device_name` (derived from the topic at
   `config_flow.py:96-101`) and `topic_pattern` (commonly
   `telegraf/server01/#`) — contradicting the module docstring at `:18-21` and
   the inline comment at `:110-113`, both of which promise the host identity
   is never included. Replace the verbatim dump with an explicit allow-list
   projection: emit `topic_pattern` only as a `_hash_device_id`-style digest,
   keep `manufacturer` / `model` / `sw_version` (already surfaced under
   `runtime` at `:159-161`), drop the raw `device_name`. Apply the same
   treatment to `config.options`: emit `field_overrides` **keys** only, the
   pattern `diagnostics.py:151` already uses for the runtime block. Update the
   docstring to state precisely what is and is not emitted.
2. **M5 (crash guard).** `runtime_data.manager` is typed
   `DeviceManager | None` (`__init__.py:94`) and `diagnostics.py:92-95`
   dereferences it unguarded, unlike every consumer in `repairs.py:282-284`.
   Add the same early return and emit `{"manager": None}`.
3. **M6 (dead check).** `diagnostics.py:202-204` looks for
   `CONF_TOPIC_PATTERN` in `raw_options` (`entry.options`), but the topic is
   stored in `entry.data` (`config_flow.py:446-448`). Pass `entry.data` in for
   that check.
4. **M13 (pending cleanup).** `pending_cleanup_all()` (`registry.py:907-919`)
   and `DIAGNOSTICS_PENDING_CLEANUP_LIMIT` (`const.py:138`) exist for exactly
   this and are unused. Add a `pending_cleanup` block truncated to the limit,
   with `device_id` hashed.
5. **N2 (defensive enum).** Wrap `EntityCategory(value)` in a
   `try/except ValueError` that falls back to `None` and logs a WARNING with
   the offending value, in both `sensor.py:30-32` and `binary_sensor.py:31-33`.

**Tests:** `tests/test_diagnostics.py` — assert `device_name`, the raw
`topic_pattern` and `field_overrides` *values* are absent; assert
`pending_cleanup` appears and is capped; assert a `manager=None` runtime
produces a payload rather than raising. Assert the literal host string is not
a substring of the serialised payload.

**Acceptance criteria:** the diagnostics JSON contains no literal host or
device name; a download succeeds for a partially-initialised entry; a
stuck-topic diagnosis is possible from the payload alone.

---

### WS-G — Schema migration, guards and dead code (M8, L1-L6, N1, M12)

**G1 — `async_migrate_entry` (M8).** `config_flow.py:371` declares
`VERSION = 1` and no migration exists.

1. Bump `VERSION` to `2`.
2. Add `async_migrate_entry(hass, entry)` in `__init__.py`:
   - **v1 → v2:** backfill *missing* option keys from `DEFAULT_*` constants
     **without touching keys that are present**. Sparse `entry.options` (`{}`
     is common) must stay semantically identical — `_normalize_options`
     (`__init__.py:472-536`) already falls back to defaults, so the backfill
     is explicit rather than behaviour-changing. Persist only on difference;
     log at INFO.
   - **All versions:** if `CONF_TOPIC_PATTERN` is missing from `entry.data`,
     do **not** synthesise a topic. Leave the entry; the setup guard below
     surfaces it.
3. Replace the unguarded `entry.data[CONF_TOPIC_PATTERN]` at
   `__init__.py:184` with a `.get()` plus an explicit `ConfigEntryNotReady`
   (or a translated `HomeAssistantError` + Repairs issue) so a corrupt entry
   fails with a readable message rather than a raw `KeyError` traceback and
   an infinite retry loop.

**G2 — Wire up or remove the no-callsite names (L1).** Applying the repo's
own SKILL4 rule ("every constant, helper and test stub must have a callsite",
[`CHANGELOG.md:273-274,316-322`](CHANGELOG.md:273)) — but **prefer wiring over
deletion wherever a sensible production callsite exists**:

| Name | Action |
|---|---|
| `exceptions.MqttBrokerUnreachable` | **Wire in** — WS-E1's timeout path. Already has the right translation key and placeholders. |
| `exceptions.ReconfigureSubscribeFailed` + `strings.json:103-105` | **Wire in** — WS-I's reconfigure pre-flight (N3). The string becomes reachable. |
| `parser.py:75-82` `ParserStats.note_received` | **Delete** — no sensible callsite; `note_parsed`/`note_dropped`/`note_parser_error` already cover every outcome. |
| `registry.py:175,205` `MetricRegistry._delete_delay` + the `delete_delay` param of `MetricRegistry.apply_options` (`:189`) | **Delete via WS-J** — the field is written in three places and read in none; the parameter is plumbed into every per-device registry on every options change, implying a per-registry use that does not exist. M14 establishes that the manager-level field it was credited to is *also* non-functional, so the deletion folds into WS-J rather than standing alone as cosmetic cleanup. |
| `const.py:141-145` `REPAIR_*` | **Wire in** — WS-D step 4. |
| `__init__.py:40` `DEFAULT_TOPIC_PATTERN` import | **Delete** — unused. |
| `registry.py:44-47` docstring (N1) | **Correct the docstring.** No code change — no parser sets a non-heuristic category today, so honouring the claim would be speculative. |

Net effect: **no user-facing feature is removed by this plan.** The only
deletions are a dead method, the `delete_delay` plumbing folded into WS-J,
and a
dead import — all with no production callsite.

**G3 — Hygiene (L2-L6, M12).**

- `registry.py:1087-1089`: delete the `import logging as _logging`
  inside-the-loop; use the module-level `_LOGGER` (`:29`).
- `registry.py:1042`: add a public `available_count` property to
  `MetricRegistry` (same for `diagnostics.py:94-101`, which reaches into
  `manager._clock` and `registry._states`).
- `registry.py:1050-1054`: the `# pragma: no cover` contradicts the comment
  directly above it. Remove the pragma; coverage stays at 100%.
- `config_flow.py:441,727`: replace `assert device_name is not None` with an
  explicit guard returning a form error.
- `config_flow.py:522-531`: re-submitting scan settings while a scan task is
  live re-shows progress bound to the **old** task while `_wait_for_scan`
  (`:574,588`) reads the **new** `self._scan_duration`. Cancel and relaunch,
  or pass duration/root as task parameters instead of reading `self`.
- `config_flow.py:686-688` (M12): wrap `mqtt.async_subscribe` in `_start_scan`
  and return a form with a translated `scan_failed` error rather than letting
  the exception escape the step.

**G4 — Migration test surface.** Because `VERSION` moves 1 → 2:

- *Unit* — `async_migrate_entry` on a v1 entry with `options={}` leaves
  options semantically identical; on a v1 entry with populated options changes
  nothing; on a v2 entry is a no-op.
- *Integration* — `tests/test_harness.py`: a `MockConfigEntry(version=1)` with
  sparse options, `async_setup`, assert the entry reaches `LOADED` and
  entities still appear. This is the backward-compatibility gate for the
  release.

---

### WS-H — Release wrapper (documentation, version sync, rollout)

1. **Version decision: 1.4.2 → 1.5.0.** WS-A (the reload finally firing) and
   WS-I (auto-discover becoming a real feature) are both user-visible
   behaviour changes; 1.4.x is a patch line and must not carry them. Bump
   `manifest.json` and `pyproject.toml` together — locked by
   `test_pyproject_version_matches_manifest`
   ([`test_release_readiness.py:290`](tests/test_release_readiness.py:290)).
2. **CHANGELOG** must gain a `## [1.5.0]` heading with
   `### Breaking` / `### Added` / `### Fixed` / `### Changed`, and must state
   explicitly the one-time `unique_id` churn users see if they had changed
   `device_id_strategy` while the reload was dead.
3. **README**: document the cleanup → removal → reappearance entity lifecycle
   (WS-C step 6), the corrected meaning of `delete_delay` (WS-D step 6), the
   real `auto_discover` / `auto_discover_scope` behaviour (WS-I), and confirm
   the prerequisites section (README:56) still matches `hacs.json` (D1: it
   does). Add a security note that topic-pattern scope is the access-control
   boundary and that the overlap Repairs issue is the only guard against two
   entries double-subscribing.
4. **CI**: no workflow change required under D1.

---

### WS-I — Make `auto_discover` actually work (H1, N3) — decision D2

**The problem.** `derive_probe_topic` (`snoop.py:38-52`) is the identity
function, so the snoop subscribes to exactly the topic the main subscription
already holds (`__init__.py:211` vs `:389`) and re-injects every message into
`process_message` a second time. The `__init__.py:216-232` docstring promises
it adds hosts "the user's configured topic pattern **missed**" — impossible
when probe == pattern. Today the feature costs 2× parse and 2× dispatcher
fan-out and delivers nothing the main path does not already deliver.

**The design that makes it work, without the 1.3.0 footgun.** 1.3.0 removed
silent widening because a careless default would probe a shared broker
broader than the user intended (`CHANGELOG.md:260-265`). The fix is not to
re-add silent widening — it is to make the **discovery scope an explicit,
visible, user-controlled option**:

1. **New option `auto_discover_scope`** (`const.py`
   `CONF_AUTO_DISCOVER_SCOPE`, default `DEFAULT_AUTO_DISCOVER_PROBE_TOPIC` =
   `telegraf/#`, `DEFAULT_AUTO_DISCOVER` stays `False`). The snoop subscribes
   to the *scope*, which is independent of — and may be broader than — the
   entry's `topic_pattern`. Nothing is widened implicitly; the user sees and
   edits the exact filter that will be subscribed.
2. **Skip what the main subscription already handles.** Add
   `exclude_filter: str | None` to `SnoopListener.__init__`, checked in
   `_on_message` (`snoop.py:151-177`): if
   `mqtt_filter_matches(message.topic, exclude_filter)`, record the topic for
   the seen-sets but **skip the dispatch**. Set `exclude_filter` to the
   entry's `topic_pattern` in `_apply_auto_discover`
   (`__init__.py:359-406`). This makes auto-discover purely **additive** —
   it can only add hosts the main subscription misses, never re-process one
   it already has — and it removes the 2× cost that made the current
   implementation objectionable.
3. **New module `topics.py`** with two functions, unit-tested against MQTT
   3.1.1 §4.7:
   - `mqtt_filter_matches(topic: str, mqtt_filter: str) -> bool` — exact
     matcher: `+` matches exactly one level (including an empty one), `#`
     matches the remainder **and** its own parent level, a filter beginning
     with a wildcard never matches a `$`-prefixed topic.
   - `mqtt_filter_covers(a: str, b: str) -> bool` — does filter `a` cover the
     whole of filter `b`? Used to warn when the scope adds nothing.
   - **Do not** refactor `repairs._patterns_overlap` onto these. It is a
     deliberately *conservative over-approximation* used only to raise a
     warning; substituting an exact matcher would silently **reduce** repair
     detections. Leaving it alone is a correctness decision, not an oversight.
4. **Options flow**: add `auto_discover_scope` as a `vol.Optional` string
   field next to `auto_discover` (`config_flow.py:285`), with a
   `data_description` in WS-D step 3 warning about shared brokers. Validate
   with the existing `_valid_subscription_topic` syntax check
   (`config_flow.py:83-93`) and surface a `invalid_topic` error.
5. **Normalization + Repairs** (`__init__.py` `_normalize_options`,
   `:472-536`): add `auto_discover_scope` to `TelegrafMqttOptions`, coerced
   to the default when absent or syntactically invalid, and listed in
   `invalid` so `check_invalid_persisted_option` raises an issue. Add a new
   Repairs check `check_auto_discover_scope` that raises a WARNING issue when
   `auto_discover` is on and `mqtt_filter_covers(scope, topic_pattern)` is
   true — the user has enabled a scope that can only ever re-see what they
   already receive. Needs a new `REPAIR_*` constant and a new
   `issues.auto_discover_scope_redundant` string (WS-D step 4).
6. **Initial setup** (`__init__.py:184-232`): unchanged in shape —
   `_apply_auto_discover` is already called at the end of `async_setup_entry`
   (`:232`) and already derives the probe. Change the probe source from
   `derive_probe_topic(topic_pattern)` to `options.auto_discover_scope`, and
   pass `exclude_filter=topic_pattern`. Keep the non-fatal start failure
   handling (`:392-399`).
7. **Live toggle** (`_async_options_updated` → `_apply_auto_discover`,
   `:342`): unchanged; the idempotent start/stop already works. Update the
   scope live too — if the scope changes while the snoop is running, stop and
   restart it (a `stop()` then `start()` is cheap and keeps the subscription
   count at exactly one).
8. **Reconfigure (N3, decision D2)**: `async_step_reconfigure`
   (`config_flow.py:696-752`) currently writes `data_updates` and reloads
   unconditionally — it never checks that the new pattern is actually
   subscribable, and it never re-points the snoop's `exclude_filter`. Add a
   **pre-flight**: subscribe to the candidate pattern via
   `mqtt.async_subscribe` inside a short `asyncio.timeout`, and on failure
   return the form with `errors={"base": "cannot_connect"}` and a translated
   `ReconfigureSubscribeFailed` (`exceptions.py:18-31`,
   `strings.json:103-105`) — which finally gives that dead exception and dead
   translation string their production callsite. On success, unsubscribe
   immediately and proceed. Because `async_update_reload_and_abort` triggers a
   reload, `async_unload_entry` tears the old snoop down and
   `async_setup_entry` re-points both the main subscription and the snoop's
   `exclude_filter` at the new pattern. This makes auto-discover work
   correctly across a reconfigure, and gives the user immediate feedback
   instead of a silent failed reload.

**`derive_probe_topic` disposition:** its only remaining caller is
`_apply_auto_discover`, which now passes an explicit scope. Per SKILL4, delete
`snoop.py:38-52` and the `DEFAULT_AUTO_DISCOVER_PROBE_TOPIC` reference in its
docstring, keeping the constant as the *option default* in `const.py`. Its
current docstring already admits it is a no-op on the pattern, so this is a
docstring-truth alignment as much as a deletion.

**Tests:**

- *Unit* — `topics.py`: a table-driven suite over `mqtt_filter_matches`
  (`a/#` matches `a`; `a/+/c` matches `a//c` but not `a/c`; `+/x` does not
  match `$SYS/x`; `#` does not match `$SYS/x`) and `mqtt_filter_covers`
  (`telegraf/#` covers `telegraf/rack1/#`; `telegraf/rack1/#` does not cover
  `telegraf/#`).
- *Unit* — `SnoopListener`: with `dispatcher` set and
  `exclude_filter="telegraf/rack1/#"`, a message on `telegraf/rack1/cpu` is
  recorded but **not** dispatched; a message on `telegraf/rack2/cpu` **is**.
  Assert `dispatched_count` and `dispatcher_errors` accordingly.
- *Integration* — `tests/test_harness.py`, new test: entry on
  `telegraf/rack1/#` with `auto_discover=True` and
  `auto_discover_scope="telegraf/#"`; fire a message on `telegraf/rack1/cpu`
  and one on `telegraf/rack2/cpu`; assert **one** device for rack1 (not two,
  proving the skip logic) and a **new** device + entities for rack2. Fire the
  rack1 message twice and assert `parser_stats.received` advanced by exactly
  two, proving no double-processing.
- *Integration* — reconfigure test: reconfigure `telegraf/rack1/#` →
  `telegraf/#` with auto-discover on; assert exactly two MQTT subscriptions
  exist afterwards (main + snoop), the snoop's exclude filter tracks the new
  pattern, and a rack1-only message still produces one device.
- *Integration* — N3 pre-flight: reconfigure to a pattern the fake subscribe
  rejects; assert the form returns `errors={"base": "cannot_connect"}` and
  `entry.data` is unchanged.
- *Regression* — `tests/test_harness.py:227-255`
  (`test_options_flow_toggles_auto_discover_live_real`) must still pass; the
  snoop teardown handle is parked only when `auto_discover` is on **and** the
  scope is valid.
- *Repairs* — unit for `check_auto_discover_scope`: raises when the scope is
  fully covered by the pattern, auto-resolves when it is widened.

**Acceptance criteria:** with auto-discover on, a host publishing only under
`auto_discover_scope` and outside `topic_pattern` becomes a device with
entities; a host inside `topic_pattern` is processed exactly once; the feature
behaves identically after initial setup, a live options toggle, and a
reconfigure.

---

### WS-J — Make `delete_delay` actually clean entities (M14)

**Affected:** `registry.py` (`DeviceManager.cleanup`,
`DeviceManager.prune_empty_devices`, `MetricRegistry.__init__`,
`MetricRegistry.apply_options`); `__init__.py` (the expiry tick's call to
`prune_empty_devices` at `:611`).

**The question that surfaced this.** `MetricRegistry._delete_delay`
(`registry.py:175, 206`) is written in three places and read in none; the
`delete_delay` parameter is plumbed from the manager into every per-device
registry on construction (`:700`) and on every options change (`:762`). It is
tempting to call it "superseded by the manager-level field". **That is wrong,
and checking it revealed a worse defect.** The manager-level field
(`registry.py:581`) is read in exactly one place — `prune_empty_devices` at
`:1079` — and that method is **unreachable in the normal lifecycle**:

```
prune_empty_devices requires  len(registry) == 0        registry.py:1077
        ^ nothing can empty a registry, because:
cleanup() skips any device whose heartbeat is stale        registry.py:1040
cleanup() skips any device with available_count < min      registry.py:1043
```

Trace a host that simply stops publishing, with the shipped defaults
(`expire_after=120`, `min_active_metrics=1`, `cleanup_delay=30d`,
`delete_delay=60d`, `DEFAULT_MAX_DEVICES=50`):

1. `t0` — host stops. `last_any_metric = t0`, all metrics still available.
2. `t0 + 120s` — `check_expiry` flips every metric unavailable. `cleanup` now
   sees `available_count = 0 < min_active_metrics (1)` and skips
   (`:1043-1054`).
3. `t0 + 121s` onward — `now - last_any_metric > expire_after`, so `cleanup`
   skips the device entirely (`:1040`). `registry._states` is never emptied.
4. Forever — `prune_empty_devices` sees `len(registry) > 0` and skips
   (`:1077`).

**`delete_delay` never fires. So does `cleanup_delay`, for a fully-silent
host. So does the `CLEANUP_POLICY_ALWAYS` fast path**, which is inside
`registry.cleanup` and therefore also never reached for an offline device.

**Why this is worse than unbounded memory.** `DeviceManager.devices` grows
without bound, and `get_or_create_registry` (`:684`) gates *new* devices on
`len(self.devices) >= self._max_devices`. Dead hosts therefore **consume the
device cap**: once 50 hosts have ever been seen and 20 have since gone away
for good, 20 slots are permanently occupied, and a 51st *live* host is
silently dropped (`dropped_device_count += 1`) with only a Repairs hint. The
user's monitoring degrades without any error.

**Required change:** `delete_delay` should be what its label says — the
automatic entity-cleaning control keyed on how long the **host** has been
silent. Emptiness is the wrong precondition.

**Implementation actions:**

1. **Make pruning heartbeat-based, not emptiness-based.** Replace
   `prune_empty_devices` with `prune_stale_devices` (rename the single caller
   at `__init__.py:611`). Condition: drop the device when
   `now - registry.last_any_metric > self._delete_delay`, regardless of how
   many metrics it still holds.
2. **Emit entity removals for the pruned device.** Today the method only pops
   `self.devices`; the metrics it held would be orphaned in HA's entity
   registry. Give it an `on_remove: Callable[[str, str], None]` taking
   `(device_id, unique_key)` and call it for every metric in the pruned
   registry, so `__init__.py` can dispatch the WS-C
   `SIGNAL_REMOVE_METRIC` payload. **Without this, WS-J makes the leak
   worse** — it moves the orphaning from "never happens" to "happens by
   design". This is a hard dependency on WS-C's 2-tuple payload.
3. **Honour `enable_cleanup`.** `cleanup()` returns early when
   `enable_cleanup` is False (`:1034-1035`); `prune_empty_devices` ignores
   the flag entirely. A user who sets `enable_cleanup: false` expects
   "nothing is ever deleted" (the docstring at `:1020-1023` says exactly
   that) and currently still has devices pruned once a registry empties.
   Gate pruning on the same flag.
4. **Delete the dead plumbing** (folded in from WS-G2's L1 row): remove
   `MetricRegistry._delete_delay`, the `delete_delay` constructor parameter
   (`:161`), and the `delete_delay` parameter of `MetricRegistry.apply_options`
   (`:190`, `:205-206`) plus the manager's pass-through at `:700` and `:762`.
   Update the `DeviceManager.apply_options` docstring at `:727-729`, which
   currently claims `delete_delay` is "propagated to each per-device registry
   so existing registries pick up live value changes" — it never was.
5. **Keep the HA device-registry entry.** Removing the entities (step 2) is
   the "entity cleaning" the option promises. Removing the *device* entry via
   `device_registry.async_remove_device()` would additionally discard any
   device-level user configuration, and a host that comes back is better off
   re-associating. This is a deliberate decision, not an oversight; record it
   in the docstring and the README.
6. **Rewrite the three-tier lifecycle in the docs** so the options are
   explainable: `expire_after` (120s) → metric goes unavailable;
   `cleanup_delay` (30d) → a *live* device's stale metrics are removed;
   `delete_delay` (60d) → a *silent* host's entire entity set is removed.
   `delete_delay > cleanup_delay` means the silent-host path dominates for
   departed machines, which is the correct ordering.

**Dependencies:** hard dependency on **WS-C** (step 2 needs the 2-tuple
removal payload). Sequence WS-C first. Must land before or with WS-E4, since
both touch the expiry tick.

**Tests:**

- *Unit* — the full trace above as a fake-clock test: a device with 3
  metrics goes silent, the clock advances past `delete_delay`, and
  `prune_stale_devices` removes the device and calls `on_remove` exactly 3
  times. **This test fails against the current code** — that is the point;
  it is the regression tripwire.
- *Unit* — a live device whose metrics expire is **not** pruned before
  `delete_delay`, and its stale metrics are still removed by `cleanup` at
  `cleanup_delay` (proving step 1 did not break the live path).
- *Unit* — `enable_cleanup=False` prunes nothing.
- *Unit* — a device pruned at `delete_delay` and then republished gets a
  fresh registry via `get_or_create_registry` (`:670-707`) and re-emits
  `on_new_device`; no duplicate device, same `unique_id` shape.
- *Regression* — a **cap** test, the user-visible payoff: 60 hosts seen, 20
  go silent past `delete_delay`, a 51st live host arrives; assert it is
  accepted and `dropped_device_count == 0`. Against the current code this
  asserts nothing and the host is dropped.
- *Integration* — `tests/test_phase6_lifecycle.py`: full HA harness; assert
  the entity registry is empty for the departed host after pruning, and that
  the device entry remains.

**Acceptance criteria:** `delete_delay` demonstrably fires; a departed
host's entities leave the entity registry; the device cap is no longer
consumed by dead hosts; `enable_cleanup: false` deletes nothing.

---

## 4. Phasing, workstreams and gates

```
Phase 0  Baseline & freeze        ── G0 ──┐
Phase 1  Correctness (data loss)  ── G1 ──┤   critical path
Phase 2  User-facing integrity    ── G2 ──┤
Phase 3  Lifecycle & performance  ── G3 ──┤
Phase 4  Observability & hygiene  ── G4 ──┤
Phase 5  Release                  ── G5 ──┘
```

### Phase 0 — Baseline and freeze (no behaviour change)

- **WS-A/0.1** Record the current full-suite pass count, coverage percentage,
  and event-loop cost of the 100-entity perf test. These are the regression
  baselines for `G1`-`G4`. (The HA-floor question is closed by D1; no
  metadata change.)
- **WS-H/0.2** Confirm the WS-I design in §3 with the implementer before
  coding — the `exclude_filter` + explicit-scope approach is a new user-facing
  option and the only item in this plan that adds configuration surface.

**Gate `G0`**
```
ruff check . && ruff format --check .
mypy --strict custom_components
pytest -q                      # 100% coverage gate
prek run --all-files
```

### Phase 1 — Correctness / data loss (critical path)

1. **WS-B** (C2) — smallest, zero regression risk. *Parallel with 2.*
2. **WS-A** (C1) — the only change that alters `unique_id` behaviour.
3. **WS-C** (C3, M7, H3) — largest behavioural change; lands with or after
   WS-E3 so the `added`/dispatch-table reconciliation is written once.
4. **WS-J** (M14) — `delete_delay` entity cleaning. Hard dependency on WS-C's
   2-tuple removal payload, so it lands immediately after. `registry.py`
   only, no platform or dispatcher change, so it is low implementation risk
   despite being the highest-consequence item in the plan.

**Gate `G1`** — all of `G0`, plus:
- `pytest -q tests/test_harness.py` (real-harness devices, reload stability,
  retained-message ordering) green.
- `pytest -q tests/test_runtime.py` (listener list length now 1) green.
- New WS-A and WS-C integration tests green.
- New WS-J unit tests green, including the device-cap regression (60 hosts
  seen, 20 departed past `delete_delay`, a 51st live host is accepted with
  `dropped_device_count == 0`).
- Manual smoke on a live broker: change `device_id_strategy`, confirm exactly
  one reload; add an exclude pattern, save options twice, confirm it survives.

### Phase 2 — User-facing integrity

1. **WS-D** (C4, M9, M10) — strings plus the constant indirection; low risk.
   `hassfest` must pass.
2. **WS-G/0** (M8, G1) — migration + setup guard.

**Gate `G2`** — all of `G1`, plus:
- `hassfest` green (translation completeness).
- New `test_every_used_translation_key_is_defined` green.
- `pytest -q tests/test_release_readiness.py` green.
- Migration test: a v1 `MockConfigEntry` reaches `LOADED` with sparse options.

### Phase 3 — Lifecycle, performance and auto-discover

1. **WS-E1** (H4) — independent, small, high value. Ships first; it also
   gives `MqttBrokerUnreachable` its callsite.
2. **WS-E2** (M1) — one line; pairs with WS-F.
3. **WS-E4** (M2, M11) — self-contained.
4. **`topics.py`** (WS-I step 3) — new module, no behaviour change yet;
   land it with full unit coverage so WS-I's later steps are incremental.
5. **WS-I** (H1, N3) — the skip logic, the new option, the redundant-scope
   Repairs check, and the reconfigure pre-flight.
6. **WS-E3** (H2) — **largest risk; own PR, own gate.** After WS-I, so the
   skip logic's benefit is measured on the already-deduplicated path.

**Gate `G3`** — all of `G2`, plus:
- New O(N) dispatcher-counting perf test green, with before/after invocation
  counts recorded in the PR.
- Broker-unreachable test proves the timeout path.
- Auto-discover integration tests green: new host discovered, in-scope host
  processed exactly once, behaviour identical after setup / toggle /
  reconfigure.
- N3 pre-flight test green: a rejected pattern leaves `entry.data` unchanged.
- Full `tests/test_harness.py` and `test_platform_units.py` green (entity
  write path and routing semantics preserved).

### Phase 4 — Observability and hygiene

1. **WS-F** (M4, M5, M6, M13, N2).
2. **WS-G/2, WS-G/3** (L1-L6, N1, M12).

Both are independent of Phases 1-3 and can be developed in parallel from day
one, landing whenever their gate passes.

**Gate `G4`** — all of `G3`, plus:
- Diagnostics payload asserted free of host/device names.
- `pytest -q tests/test_diagnostics.py` green including the `manager=None`
  case.
- SKILL4 audit re-run: no constant, helper or stub in
  `custom_components/telegraf_mqtt/` lacks a production callsite.

### Phase 5 — Release

1. **WS-H** — version 1.5.0, CHANGELOG, README.
2. Confirm `manifest.json` / `pyproject.toml` / `CHANGELOG.md` agree
   (`test_release_readiness.py:290,337`).
3. Tag and publish through the existing HACS flow.

**Gate `G5`** — all gates above, plus the full CI matrix green and a manual
end-to-end pass: install → configure (manual path) → configure (discover
path) → reconfigure → options change → reload → unload → remove entry, with
no orphaned devices or entities, no lingering MQTT subscriptions, and exactly
one subscription per configured scope.

### Parallelizable workstreams

| Workstream | Start | Notes |
|---|---|---|
| WS-B (C2) | Phase 1 | Independent of everything |
| WS-A (C1) | Phase 1 | Independent |
| WS-C (C3/M7/H3) | Phase 1 | Coordinate with WS-E3 |
| WS-J (M14) | Phase 1 | After WS-C (needs the removal payload); before/with WS-E4 |
| WS-D (C4/M9/M10) | Phase 2 | `repairs.py` edit overlaps WS-C |
| WS-E1 (H4) | Phase 3 | Independent; wires a dead exception |
| WS-F (M4/M5/M6/M13) | **Phase 1 (dev)** | Lands Phase 4 |
| WS-G (M8, L1-L6) | Phase 2 | `const.py` overlaps WS-D/E1/E4 — one `const.py` owner per PR |
| `topics.py` + WS-I | Phase 3 | New option + pre-flight; reconfigure path |
| WS-E3 (H2) | Phase 3 | Highest regression risk; serialised last |

### Critical path

`Phase 0 → WS-A → WS-C → WS-J → WS-I → WS-E3 → WS-H (release)`.
WS-B, WS-D, WS-E1, WS-E2, WS-E4, WS-F and WS-G are off the critical path and
can be absorbed or deferred without blocking the release. The only items
that gate everything by scoping it are the WS-I design confirmation (Phase
0/0.2) and the WS-C/WS-E3 ordering.

---

## 5. Cross-cutting concerns

**Async lifecycle and cancellation safety.** No new `asyncio.create_task` is
introduced; the only task in the codebase (`config_flow.py:552`) is HA's
`progress_task` and is HA-cancelled on abort. WS-E1 introduces
`asyncio.timeout`, which cancels cleanly on HA stop. WS-I's reconfigure
pre-flight uses `asyncio.timeout` around a subscribe it immediately
unsubscribes — verify that the pre-flight's unsubscribe runs on both the
success and failure paths, or a subscription leaks per reconfigure attempt.
WS-G3 changes the scan task's parameter passing; it must not introduce a
second untracked task. WS-C and WS-E3 change subscription lifetime: verify
every `async_dispatcher_connect` unsubscribe handle is still parked via
`entry.async_on_unload`, and that `async_will_remove_from_hass` pops entities
in a `@callback`.

**Concurrency and races.** All processing is single-threaded on the event
loop; there is no `await` inside `process_message` (`registry.py:768-839`),
so the registry cannot interleave. The one real race was the two-listener
ordering that produced C1 — WS-A removes it by construction. WS-C introduces a
read-modify-write on the platform's `added` set from two dispatch signals;
both handlers must be `@callback` and must not `await`. WS-I's
`exclude_filter` is read from a per-message callback — it must be immutable
for the listener's lifetime (rebuild the listener on scope change rather than
mutating a field mid-flight).

**Error handling.** WS-E1 converts an unbounded await into a bounded,
translated failure. WS-I's pre-flight converts a silent bad reconfigure into
a translated form error. WS-G3 converts an unhandled exception in the scan
step into a translated form error. WS-F removes an `AttributeError` path. No
new bare `except` is introduced; the existing narrow-catch discipline in
`parser.py:227-247` and `snoop.py:169-175` is preserved — note that WS-I's
skip logic must sit *before* the existing dispatcher try/except so a bad
payload is still counted in `dispatcher_errors` exactly once.

**Observability.** WS-E2 makes `last_message.topic` real; WS-F adds
`pending_cleanup`. Together these turn a diagnostics download from "counts
only" into an actionable artifact. WS-D's translation-completeness test is the
durable guard against user-facing strings silently rotting. WS-I's Repairs
check surfaces a misconfigured scope the user would otherwise never notice.

**Entity and device-registry stability.** WS-A is the one item that changes
`unique_id` derivation. It is the documented intent, the change is one-time,
and the regression test pins it explicitly. WS-C changes when entities are
removed and re-created — the `unique_id` is preserved so no duplicate is ever
created, but the `entity_id` may differ after a removal/reappearance cycle;
that must be in the README and CHANGELOG. WS-I adds entities for newly
discovered hosts, each with the standard `unique_id` shape, so a host that
later moves under `topic_pattern` maps to the same device and entity — it
must not duplicate.

**Naming and category overrides.** No change to `naming.py`'s resolution
logic. WS-D adds the `field_overrides` schema documentation so users stop
needing the source. N1 corrects a docstring rather than changing behaviour.

**Security.** The integration holds no credentials. The security-adjacent
surface is diagnostics redaction (M4) and the fact that the entry inherits
the MQTT broker's ACLs. WS-I's `auto_discover_scope` is a **new** surface
that subscribes more broadly than `topic_pattern` — it is exactly the
shared-broker footgun 1.3.0 closed, reintroduced deliberately and
user-controlled. Mitigations: default `False`; the scope is an explicit,
visible, editable field rather than a silent derivation; the redundant-scope
Repairs check tells the user when it adds nothing; the README must state that
the scope is an access-relevant subscription. The `overlap_topic_patterns`
check (`repairs.py:180-229`) is the only guard against two entries
double-subscribing and must keep working for any scope pair.

---

## 6. Rollout safeguards and rollback

| Phase | Risk if shipped wrong | Safeguard | Rollback |
|---|---|---|---|
| 0 | Baselines not captured | Recorded before any change | Re-capture |
| 1 (WS-B) | Form pre-fill regresses | Unit test asserts `default()` == current | Revert one line per field |
| 1 (WS-A) | Reload fires unexpectedly, churning `unique_id` | Gate asserts non-strategy changes do **not** reload; manual smoke | Remove the single listener registration; behaviour reverts to today's (broken) no-reload state |
| 1 (WS-C) | Entities flap in/out of existence | Round-trip integration test; README documents the lifecycle | Revert the `added.discard` line; entities stay gone (today's behaviour) |
| 1 (WS-J) | A still-live host is pruned, deleting its entities | `delete_delay` defaults to 60 days; heartbeat-based test; live-path regression test | Revert to `prune_empty_devices`; devices are never pruned (today's behaviour) |
| 2 (WS-D) | A missing translation key breaks a different flow | Completeness test scans *all* used keys | Revert `strings.json` / `en.json` |
| 2 (WS-G0) | Migration mangles a real entry | Migration is additive-only; v1 entry test with sparse options | Bump `VERSION` back to 1 and delete `async_migrate_entry`; extra default keys are ignored by `_normalize_options`, so even a full revert is safe |
| 3 (WS-E1) | 30s timeout too aggressive on slow networks | Named, documented constant; test patches a short value | Raise the constant; no logic change |
| 3 (WS-I) | Scope subscribes broader than intended | Default `False`; explicit editable field; redundant-scope Repairs; README warning | Set `auto_discover_scope` to the old behaviour; or the `exclude_filter` line is a one-line revert that restores today (duplicate) processing |
| 3 (WS-I) | Reconfigure pre-flight leaks a subscription | Assert subscription count is 1 after each pre-flight attempt | Remove the pre-flight; reconfigure reverts to today's unvalidated behaviour |
| 3 (WS-E3) | Entities stop updating — **worst case** | Full `test_harness.py` must pass; own PR | Revert the PR; the old per-entity path is intact in history |
| 4 (WS-F) | Diagnostics lose information | Assertions check presence, not just absence | Revert the projection helper |
| 5 | Version surfaces drift | Two existing release-readiness tests already lock this | Bump back |

**Rollback posture.** Phases 1-4 are all revertible by a single revert; none
performs an irreversible data migration. The migration in WS-G/0 is the only
persisted-data change, it is additive, and reverting it is safe. WS-I adds one
option key; a user who never opts in is unaffected, and a revert leaves the
key inert in `entry.options` (ignored by `_normalize_options`).

**Feature flags.** None proposed. WS-E3 is the highest-risk change and is
isolated by shipping as its own PR behind its own gate. WS-I's blast radius
is bounded by `DEFAULT_AUTO_DISCOVER = False`, which stays `False`.

---

## 7. Documentation updates

| File | Update | Workstream |
|---|---|---|
| `CHANGELOG.md` | New `## [1.5.0]` with `Breaking` / `Added` / `Fixed` / `Changed`; the one-time `unique_id` churn note; `auto_discover` behaviour change | WS-H |
| `README.md:56` | Verify the 2026.6.x prerequisite still matches `hacs.json` (D1: unchanged) | WS-H |
| `README.md` (options) | Correct `delete_delay`; document `field_overrides` shape; document `auto_discover_scope` and the shared-broker caution | WS-D, WS-I |
| `README.md` (lifecycle) | Document cleanup → removal → reappearance, and the possible `entity_id` change | WS-C |
| `README.md` (security) | Topic-pattern scope is the access boundary; the auto-discover scope is an access-relevant subscription | WS-H, WS-I |
| `strings.json` / `en.json` | New issue blocks (incl. `auto_discover_scope_redundant`), abort reasons (`reconfigure_successful`, `cannot_connect`), option labels/descriptions | WS-D |
| `const.py` comments | Record *why* `BROKER_WAIT_TIMEOUT_SECONDS`, the seen-set caps and `CONF_AUTO_DISCOVER_SCOPE` exist | WS-E, WS-I |
| Module docstrings | `registry.py:240-243` (M7), `registry.py:44-47` (N1), `__init__.py:112-114` (now live), `__init__.py:216-232` (auto-discover behaviour) | WS-C, WS-G2, WS-I |
| `.github/workflows/pytest.yml:24` | No change under D1; correct the comment only if the pin changes | WS-H |

---

## 8. Acceptance criteria (release-level)

A 1.5.0 release is acceptable when **all** of the following hold:

1. `ruff check .`, `ruff format --check .`, `mypy --strict custom_components`,
   `pytest -q` (100% coverage), `prek run --all-files` and `hassfest` are
   all green.
2. Every gate `G0`-`G5` has been executed and passed in order.
3. No translation key used in code is absent from `strings.json`, enforced by
   an automated test.
4. A v1 config entry with sparse `options` migrates to v2, loads, and produces
   entities.
5. Changing `device_id_strategy` reloads the entry exactly once; changing any
   other option does not.
6. Saving the options dialog with no field edited leaves `entry.options`
   byte-identical.
7. A metric removed by the cleanup lifecycle returns to the entity registry
   when its host republishes.
8. `remove_metric_entity` performs no iteration over `registry.entities`.
9. **`delete_delay` demonstrably fires:** a host silent past `delete_delay` has
   its entities removed from the entity registry, `enable_cleanup: false`
   removes nothing, and dead hosts no longer consume
   `DEFAULT_MAX_DEVICES` (demonstrated by the 60-hosts-seen cap test).
9. With the broker unreachable, setup fails with a translated error within
   the configured timeout and no longer hangs.
10. Dispatcher invocations for a multi-metric message are O(N) in entity
    count, demonstrated by a test.
11. **With `auto_discover` on**, a host publishing under `auto_discover_scope`
    but outside `topic_pattern` becomes a device with entities; a host inside
    `topic_pattern` is processed exactly once (no duplicate device, no
    double-counted `parser_stats.received`); the behaviour is identical after
    initial setup, a live options toggle, and a reconfigure.
12. A reconfigure to an unsubscribable pattern returns a translated form
    error and leaves `entry.data` unchanged.
13. A diagnostics download contains no literal host or device name and
    includes `pending_cleanup` and a real `last_message.topic`.
14. The SKILL4 audit finds no constant, helper or stub in the integration
    without a production callsite.
15. `CHANGELOG.md`, `manifest.json` and `pyproject.toml` agree on 1.5.0 and
    the CHANGELOG documents the breaking changes.

---

## 9. Traceability

Every finding from the review, the pre-plan todo list, and the items surfaced
during planning. "Confirmed" = reproducible defect found by inspection;
"Risk" = unverified assumption.

| ID | Class | Workstream | Plan item | Validation |
|---|---|---|---|---|
| C1 | Confirmed | WS-A | §3 WS-A, `G1` | Unit (single-listener reload), integration (harness reload), regression |
| C2 | Confirmed | WS-B | §3 WS-B, `G1` | Unit (`_clean_options` + schema defaults), regression |
| C3 | Confirmed | WS-C | §3 WS-C steps 1-3, `G1` | Integration round-trip (removed → republished → entity back) |
| C4 | Confirmed | WS-D | §3 WS-D steps 1,4,5, `G2` | `hassfest`, new translation-completeness test |
| H1 | Confirmed | WS-I | §3 WS-I (skip logic + explicit scope), `G3` | Unit (skip vs dispatch), integration (new host discovered, in-scope host once) |
| H2 | Confirmed | WS-E | §3 WS-E3, `G3` | New dispatcher-counting perf test; full harness suite |
| H3 | Confirmed | WS-C | §3 WS-C steps 1-2, `G1` | Unit (no `registry.entities` iteration; colon `unique_key`) |
| H4 | Risk (unbounded await) | WS-E | §3 WS-E1, `G3` | Unit (never-resolving fake → translated failure in time) |
| H5 | **Closed by D1** | — | §1.2 | Three surfaces agree; no change planned |
| M1 | Confirmed | WS-E | §3 WS-E2, `G3` | Unit (diagnostics `last_message.topic` == real topic) |
| M2 | Confirmed | WS-E | §3 WS-E4, `G3` | Unit (fake clock, no flap; slow host never raises) |
| M3 | Recommendation | WS-D | Deferred, explicitly | Noted so it is not lost |
| M4 | Confirmed | WS-F | §3 WS-F step 1, `G4` | Diagnostics negative assertions |
| M5 | Confirmed | WS-F | §3 WS-F step 2, `G4` | Unit (`manager=None` produces a payload) |
| M6 | Confirmed | WS-F | §3 WS-F step 3, `G4` | Unit (topic-pattern validity now reports) |
| M7 | Confirmed (merged with C3) | WS-C | §3 WS-C step 4, `G1` | Unit (`_drop_metric_on_platform_none` emits remove) + integration |
| M8 | Confirmed | WS-G | §3 WS-G steps 1-3, `G2` | Unit (migration cases) + integration (v1 entry loads) |
| M9 | Confirmed | WS-D | §3 WS-D step 2, `G2` | Completeness test; config-flow abort test |
| M10 | Confirmed | WS-D | §3 WS-D step 3, `G2` | Completeness test; schema label assertions |
| M11 | Confirmed | WS-E | §3 WS-E4, `G3` | Unit (cap evicts, `has_received_messages` stays correct) |
| M12 | Confirmed | WS-G | §3 WS-G3 (scan error), `G4` | Unit (subscribe failure → translated form error) |
| M13 | Confirmed | WS-F | §3 WS-F step 4, `G4` | Unit (`pending_cleanup` present and capped) |
| M14 | Confirmed (new) | WS-J | §3 WS-J, `G1` | Unit (full silent-host trace — fails on current code), cap regression, live-path regression, integration |
| N3 | Confirmed (new) | WS-I | §3 WS-I step 8, `G3` | Integration (rejected pattern leaves `entry.data` unchanged) |
| L1 dead code | Hygiene | WS-G + WS-J | §3 WS-G2, §3 WS-J step 4, `G4` | SKILL4 audit; two names wired, `delete_delay` plumbing folded into WS-J, three plain deletions remain |
| L2 import-in-loop | Hygiene | WS-G | §3 WS-G3, `G4` | ruff; covered branch |
| L3 private access | Hygiene | WS-G | §3 WS-G3, `G4` | mypy `--strict`; new public properties |
| L4 bad `pragma` | Hygiene | WS-G | §3 WS-G3, `G4` | Coverage stays at 100% without the pragma |
| L5 `assert` guard | Hygiene | WS-G | §3 WS-G3, `G4` | Unit for the guard branch |
| L6 scan re-entry | Hygiene | WS-G | §3 WS-G3, `G4` | Unit (re-submit settings while task live) |
| L7 100%-coverage misconception | Process | §4 gates | `G0`-`G5` | Every gate lists behaviour tests, not just coverage |
| N1 docstring divergence | New (low) | WS-G | §3 WS-G2, `G4` | Docstring corrected; no behaviour test needed |
| N2 unguarded enum | New (low) | WS-F | §3 WS-F step 5, `G4` | Unit (bad category value → WARNING + `None`, no raise) |

**Pre-plan todo reconciliation.** Items 11-14 map to C1, C2, C4, C3; item 15
to H5 (**closed by D1**); item 16 to H1 (**reversed by D2** — auto-discover is
now repaired in WS-I, not removed); items 17-20 to H2, H3, H4, M1; item 21 to
M9 + M10 + the new `auto_discover_scope` label; item 22 to M2 + M3 (M3
explicitly deferred); item 23 to M4 + M5 + M6; item 24 to L1-L6, now scoped
as *wire up two, delete four* rather than delete six. No todo is dropped.

### 9.1 Features this plan removes

**None.** The earlier draft proposed removing `auto_discover`; decision D2
reverses that, and §3 WS-I specifies how to make it work instead. The other
deletions are not features — they are names with no production callsite, and
even those were reduced from six to four by wiring two of them into real
paths (`MqttBrokerUnreachable` into WS-E1, `ReconfigureSubscribeFailed` into
WS-I's reconfigure pre-flight). The four remaining deletions are
`ParserStats.note_received` (a method superseded by three siblings),
the `MetricRegistry._delete_delay` field and its two plumbing parameters
(folded into WS-J, which makes `delete_delay` actually work rather than
merely tidying dead state), and one unused import. No user-facing behaviour is
lost by any of them — WS-J *adds* the behaviour the option already promises.
