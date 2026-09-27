"""Config flow for telegraf_mqtt.

Two entry paths are supported:

* **Manual topic** -- the user enters a topic pattern (e.g. ``telegraf/#``
  or ``telegraf/rack1/#``) and the integration subscribes to it. Entities
  are auto-detected from whatever flows under that pattern.
* **Discover topics** -- the user enters a *probe topic* (default
  ``telegraf/#``) and a *scan window* (5-300 s, default 30 s). The
  integration listens on the probe topic for the window, then presents
  the distinct 2nd-level topic prefixes it saw (e.g. ``telegraf/rack1``,
  ``telegraf/rack2``). The user picks which to subscribe to -- the
  pick-list is pre-selected with prefixes that look Telegraf-shaped --
  and the resulting ``topic_pattern`` is locked in. Entities then
  auto-detect from traffic under the chosen pattern.

Both paths converge on the same end state: a user-confirmed
``topic_pattern`` and a subscription that uses it. The difference is
how that pattern is filled in.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Mapping
from time import monotonic
from typing import Any, cast

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry, ConfigFlowResult
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_AUTO_DISCOVER,
    CONF_AUTO_DISCOVER_SCOPE,
    CONF_CATEGORY_OVERRIDES,
    CONF_CLEANUP_DELAY,
    CONF_DELETE_DELAY,
    CONF_DEVICE_ID_STRATEGY,
    CONF_DEVICE_NAME,
    CONF_ENABLE_CLEANUP,
    CONF_EXCLUDE_PATTERNS,
    CONF_EXPIRE_AFTER,
    CONF_FIELD_OVERRIDES,
    CONF_MIN_ACTIVE_METRICS,
    CONF_MODEL,
    CONF_SCAN_DURATION_SECONDS,
    CONF_SCAN_ROOT_TOPIC,
    CONF_SETUP_MODE,
    CONF_SW_VERSION,
    CONF_TOPIC_PATTERN,
    DEFAULT_AUTO_DISCOVER,
    DEFAULT_AUTO_DISCOVER_SCOPE,
    DEFAULT_CLEANUP_DELAY,
    DEFAULT_DELETE_DELAY,
    DEFAULT_DEVICE_ID_STRATEGY,
    DEFAULT_DEVICE_NAME,
    DEFAULT_ENABLE_CLEANUP,
    DEFAULT_EXPIRE_AFTER,
    DEFAULT_MIN_ACTIVE_METRICS,
    DEFAULT_SCAN_DURATION_SECONDS,
    DEFAULT_SCAN_ROOT_TOPIC,
    DEFAULT_TOPIC_PATTERN,
    DOMAIN,
    MAX_SCAN_DURATION_SECONDS,
    MIN_SCAN_DURATION_SECONDS,
    RECONFIGURE_PREFLIGHT_TIMEOUT_SECONDS,
    SETUP_MODE_DISCOVER,
    SETUP_MODE_MANUAL,
    VALID_DEVICE_ID_STRATEGIES,
    VALID_PLATFORM_HINTS,
)
from .exceptions import ReconfigureSubscribeFailed

_LOGGER = logging.getLogger(__name__)


# Cap on the number of distinct prefixes presented in the pick list. A
# shared broker may carry an arbitrary number of topic trees; pinning
# the list keeps the form responsive and the picker scannable.
_MAX_PICK_LIST_OPTIONS = 200


def _valid_subscription_topic(topic: str) -> bool:
    """Return whether a string is a syntactically valid MQTT subscription topic."""
    if not topic:
        return False
    parts = topic.split("/")
    for index, part in enumerate(parts):
        if "#" in part and (part != "#" or index != len(parts) - 1):
            return False
        if "+" in part and part != "+":
            return False
    return True


def _default_device_name(topic: str) -> str:
    """Build a default device name from the first static topic segment."""
    for part in topic.split("/"):
        if part and part not in {"+", "#"}:
            return part.replace("_", " ").replace("-", " ").title()
    return DEFAULT_DEVICE_NAME


def _roll_up_topics(seen: frozenset[str]) -> list[str]:
    """Roll leaf topics up to their 2nd-level prefix.

    Topics are grouped by their first two ``/``-separated segments and
    presented as a wildcard subscription the user can opt into:

    * ``telegraf/rack1/cpu`` and ``telegraf/rack1/mem`` -> ``telegraf/rack1/#``
    * ``telegraf/rack2/cpu`` and ``telegraf/rack2/mpu`` -> ``telegraf/rack2/#``
    * ``sensors/office/temp`` -> ``sensors/office/#``
    * A leaf with only one segment (``cpu``) is grouped under itself.

    The result is sorted for stable UI rendering and capped at
    ``_MAX_PICK_LIST_OPTIONS`` to keep the form responsive. Returned
    list items are syntactically valid subscription topics.
    """
    grouped: dict[str, set[str]] = {}
    for topic in seen:
        parts = topic.split("/", 1)
        head = parts[0]
        if len(parts) == 1 or not parts[1]:
            prefix_key = head
            grouped.setdefault(prefix_key, set()).add(head)
        else:
            tail = parts[1]
            tail_parts = tail.split("/", 1)
            sub = tail_parts[0]
            prefix_key = f"{head}/{sub}"
            grouped.setdefault(prefix_key, set()).add(topic)

    # Build subscription patterns. Each grouped key maps to ``<head>/<sub>/#``
    # unless the head itself is already a single-segment leaf, in which
    # case the subscription is just ``<head>``.
    options: list[str] = []
    for key in sorted(grouped):
        if "/" in key:
            options.append(f"{key}/#")
        else:
            options.append(key)
    return options[:_MAX_PICK_LIST_OPTIONS]


def _looks_telegraf_shaped(prefix: str) -> bool:
    """Heuristic: is a 2nd-level prefix likely a Telegraf topic tree?

    The scan may surface a mix of Telegraf topics, HA internal topics,
    and anything else the broker happens to carry. Pre-selecting the
    obvious Telegraf-shaped ones (head == ``telegraf``) lets the user
    confirm with a single click in the common case while leaving
    non-Telegraf topics visible for the edge case where the user has
    a different convention.
    """
    head = prefix.split("/", 1)[0]
    return head.lower() == "telegraf"


def _config_schema(defaults: dict[str, Any]) -> vol.Schema:
    """Build the manual-topic / reconfigure schema with the given defaults."""
    return vol.Schema(
        {
            vol.Required(CONF_TOPIC_PATTERN, default=defaults[CONF_TOPIC_PATTERN]): str,
            vol.Required(CONF_DEVICE_NAME, default=defaults[CONF_DEVICE_NAME]): str,
            vol.Optional(CONF_MODEL, default=defaults.get(CONF_MODEL, "")): str,
            vol.Optional("manufacturer", default=defaults.get("manufacturer", "")): str,
            vol.Optional(CONF_SW_VERSION, default=defaults.get(CONF_SW_VERSION, "")): str,
        }
    )


def _pick_mode_schema() -> vol.Schema:
    """Build the menu step that branches between manual and discover flows."""
    return vol.Schema(
        {
            vol.Required(CONF_SETUP_MODE): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        selector.SelectOptionDict(
                            value=SETUP_MODE_MANUAL,
                            label="I know my MQTT topic pattern",
                        ),
                        selector.SelectOptionDict(
                            value=SETUP_MODE_DISCOVER,
                            label="Discover topics from broker traffic",
                        ),
                    ],
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
        }
    )


def _scan_settings_schema() -> vol.Schema:
    """Build the discover-topics scan settings form."""
    return vol.Schema(
        {
            vol.Required(CONF_SCAN_ROOT_TOPIC, default=DEFAULT_SCAN_ROOT_TOPIC): str,
            vol.Required(
                CONF_SCAN_DURATION_SECONDS,
                default=DEFAULT_SCAN_DURATION_SECONDS,
            ): vol.All(int, vol.Range(min=MIN_SCAN_DURATION_SECONDS, max=MAX_SCAN_DURATION_SECONDS)),
        }
    )


def _pick_topics_schema(prefixes: list[str], pre_selected: list[str]) -> vol.Schema:
    """Build the pick form for the discover-topics flow.

    ``prefixes`` is the roll-up of every 2nd-level prefix the scan saw.
    ``pre_selected`` is the subset of those prefixes that look
    Telegraf-shaped; the first of them becomes the form's default.

    The runtime subscription supports exactly one pattern per entry
    (``mqtt.async_subscribe`` is called once with ``topic_pattern``),
    so the picker is single-select: the UI matches what the entry will
    actually do instead of inviting extra picks that would be silently
    discarded. ``custom_value=True`` still lets the user hand-type a
    pattern the scan did not surface. With no Telegraf-shaped prefix
    the field has no default and an untouched submit lands on the
    step's friendly ``no_topics_selected`` error.
    """
    options = [selector.SelectOptionDict(value=p, label=p) for p in prefixes[:_MAX_PICK_LIST_OPTIONS]]
    # Voluptuous validates defaults, so a ``default=None`` sentinel is
    # not an option against a strict selector: when there is nothing
    # to pre-select, leave the key without a default instead.
    field: vol.Marker
    if pre_selected:
        field = vol.Optional(CONF_TOPIC_PATTERN, default=pre_selected[0])
    else:
        field = vol.Optional(CONF_TOPIC_PATTERN)
    return vol.Schema(
        {
            field: selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=options,
                    custom_value=True,
                    multiple=False,
                    mode=selector.SelectSelectorMode.LIST,
                )
            ),
        }
    )


def _clean(value: Any) -> str | None:
    """Return None for empty/whitespace strings, otherwise the stripped value."""
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _strategy_label(strategy: str) -> str:
    """Human-readable label for a ``device_id_strategy`` value."""
    return {
        "host": "Host tag (default)",
        "host_topic": "Host tag, then topic segment",
        "topic_only": "Topic tree only",
    }.get(strategy, strategy)


def _build_options_schema(current_options: Mapping[str, Any]) -> vol.Schema:
    """Build the Phase 10 multi-section options schema.

    Sections:
    - Discovery: enable the post-setup snoop listener and pick a
      ``device_id_strategy`` for resolving ``host`` collisions.
    - Cleanup lifecycle.
    - Filter / override: ``exclude_patterns``, ``field_overrides``,
      and the per-entity ``category_overrides`` map.

    Every field pre-fills from ``current_options``. This is load-bearing,
    not cosmetic: ``_clean_options`` persists whatever the form returns, so
    a field that defaults to a *constant* rather than the *current* value is
    silently reset to that constant the moment the user saves the dialog
    after touching anything else.
    """
    enable_cleanup = current_options.get(CONF_ENABLE_CLEANUP, DEFAULT_ENABLE_CLEANUP)
    cleanup_delay = current_options.get(CONF_CLEANUP_DELAY, DEFAULT_CLEANUP_DELAY)
    delete_delay = current_options.get(CONF_DELETE_DELAY, DEFAULT_DELETE_DELAY)
    min_active_metrics = current_options.get(CONF_MIN_ACTIVE_METRICS, DEFAULT_MIN_ACTIVE_METRICS)
    auto_discover = current_options.get(CONF_AUTO_DISCOVER, DEFAULT_AUTO_DISCOVER)
    auto_discover_scope = current_options.get(CONF_AUTO_DISCOVER_SCOPE, DEFAULT_AUTO_DISCOVER_SCOPE)
    device_id_strategy = current_options.get(CONF_DEVICE_ID_STRATEGY, DEFAULT_DEVICE_ID_STRATEGY)
    category_overrides = current_options.get(CONF_CATEGORY_OVERRIDES, {})
    expire_after = current_options.get(CONF_EXPIRE_AFTER, DEFAULT_EXPIRE_AFTER)
    exclude_patterns = list(current_options.get(CONF_EXCLUDE_PATTERNS, []))
    field_overrides = dict(current_options.get(CONF_FIELD_OVERRIDES, {}))
    non_negative_int = vol.All(int, vol.Range(min=0))

    return vol.Schema(
        {
            vol.Optional(CONF_AUTO_DISCOVER, default=auto_discover): bool,
            vol.Optional(CONF_AUTO_DISCOVER_SCOPE, default=auto_discover_scope): str,
            vol.Optional(CONF_DEVICE_ID_STRATEGY, default=device_id_strategy): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        selector.SelectOptionDict(value=v, label=_strategy_label(v)) for v in VALID_DEVICE_ID_STRATEGIES
                    ],
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Optional(CONF_EXPIRE_AFTER, default=expire_after): vol.All(int, vol.Range(min=1)),
            vol.Optional(CONF_ENABLE_CLEANUP, default=enable_cleanup): bool,
            vol.Optional(CONF_CLEANUP_DELAY, default=cleanup_delay): non_negative_int,
            vol.Optional(CONF_DELETE_DELAY, default=delete_delay): non_negative_int,
            vol.Optional(CONF_MIN_ACTIVE_METRICS, default=min_active_metrics): non_negative_int,
            vol.Optional(CONF_EXCLUDE_PATTERNS, default=exclude_patterns): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[],
                    custom_value=True,
                    multiple=True,
                    mode=selector.SelectSelectorMode.LIST,
                )
            ),
            vol.Optional(CONF_FIELD_OVERRIDES, default=field_overrides): selector.ObjectSelector(),
            vol.Optional(CONF_CATEGORY_OVERRIDES, default=category_overrides): selector.ObjectSelector(),
        }
    )


def _clean_options(
    user_input: dict[str, Any],
    current_options: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Normalize user input from the options flow before persisting.

    Phase 10: ``CONF_CATEGORY_OVERRIDES`` is an ObjectSelector result;
    the user can leave it as ``{}``, so we coerce the empty form value
    back to an empty dict.

    ``current_options`` is the entry's stored options. Any key the client
    omitted from the submission falls back to the *stored* value rather
    than to the compiled-in default, so a partially-rendered form (or a
    client that drops a field) can never silently reset a setting the
    user did not touch. The schema pre-fills from the same source, so in
    the normal path the two mechanisms agree.
    """
    stored: Mapping[str, Any] = current_options or {}

    def _get(key: str, fallback: Any) -> Any:
        if key in user_input:
            return user_input[key]
        return stored.get(key, fallback)

    return {
        CONF_AUTO_DISCOVER: bool(_get(CONF_AUTO_DISCOVER, DEFAULT_AUTO_DISCOVER)),
        CONF_AUTO_DISCOVER_SCOPE: str(_get(CONF_AUTO_DISCOVER_SCOPE, DEFAULT_AUTO_DISCOVER_SCOPE)),
        CONF_DEVICE_ID_STRATEGY: str(_get(CONF_DEVICE_ID_STRATEGY, DEFAULT_DEVICE_ID_STRATEGY)),
        CONF_EXPIRE_AFTER: int(_get(CONF_EXPIRE_AFTER, DEFAULT_EXPIRE_AFTER)),
        CONF_ENABLE_CLEANUP: bool(_get(CONF_ENABLE_CLEANUP, DEFAULT_ENABLE_CLEANUP)),
        CONF_CLEANUP_DELAY: int(_get(CONF_CLEANUP_DELAY, DEFAULT_CLEANUP_DELAY)),
        CONF_DELETE_DELAY: int(_get(CONF_DELETE_DELAY, DEFAULT_DELETE_DELAY)),
        CONF_MIN_ACTIVE_METRICS: int(_get(CONF_MIN_ACTIVE_METRICS, DEFAULT_MIN_ACTIVE_METRICS)),
        CONF_EXCLUDE_PATTERNS: list(_get(CONF_EXCLUDE_PATTERNS, [])),
        CONF_FIELD_OVERRIDES: dict(_get(CONF_FIELD_OVERRIDES, {})),
        CONF_CATEGORY_OVERRIDES: dict(_get(CONF_CATEGORY_OVERRIDES, {})),
    }


class TelegrafMqttOptionsFlow(config_entries.OptionsFlow):
    """Handle telegraf_mqtt options changes.

    Phase 10: the single ``init`` step grew to cover discovery settings
    and per-entity category overrides. Each option is documented in
    ``strings.json``; the schema is built by ``_build_options_schema``
    so the field set is in one place.
    """

    async def async_step_init(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Show the multi-section options form."""
        if user_input is not None:
            scope_error = self._validate_scope(user_input)
            if scope_error is not None:
                # Re-show the form with the scope field flagged rather
                # than persisting a filter the broker would reject: the
                # snoop subscribes to it directly, so a bad value here is
                # a broken second subscription, not a cosmetic typo.
                return self.async_show_form(
                    step_id="init",
                    data_schema=_build_options_schema(self.config_entry.options),
                    errors={CONF_AUTO_DISCOVER_SCOPE: scope_error},
                )
            return self.async_create_entry(
                title="",
                data=_clean_options(user_input, self.config_entry.options),
            )

        return self.async_show_form(
            step_id="init",
            data_schema=_build_options_schema(self.config_entry.options),
        )

    def _validate_scope(self, user_input: dict[str, Any]) -> str | None:
        """Return an error key for an invalid ``auto_discover_scope``, else None."""
        if CONF_AUTO_DISCOVER_SCOPE not in user_input:
            # Field omitted from the submission; ``_clean_options`` falls
            # back to the stored value, which is already trusted.
            return None
        scope = str(user_input[CONF_AUTO_DISCOVER_SCOPE] or "").strip()
        if not _valid_subscription_topic(scope):
            return "invalid_topic"
        return None


class TelegrafMqttConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for telegraf_mqtt.

    Two paths share the same end state:

    * ``async_step_user`` -> ``async_step_manual_topic`` -> create
      (the user already knows the pattern they want).
    * ``async_step_user`` -> ``async_step_scan_settings`` ->
      ``async_step_scan_running`` -> ``async_step_pick_topics`` -> create
      (the user wants the broker to tell them what's available, then
      picks which prefixes to subscribe to).

    Reconfigure is unchanged: ``async_step_reconfigure`` still shows the
    flat topic + device-metadata form and triggers a reload when the
    topic pattern changes.
    """

    # v2: options gained ``auto_discover_scope``, and every option key is
    # backfilled from its default so a sparse ``entry.options`` becomes
    # explicit. See ``__init__.async_migrate_entry``.
    VERSION = 2
    MINOR_VERSION = 1

    # Per-flow scan state. The snoop listener is held so the running
    # step can wait on its auto-stop timer; the seen topics persist
    # between ``async_step_scan_running`` and ``async_step_pick_topics``
    # so the user can navigate back without re-running the scan.
    _scan_snoop: Any | None = None
    _scan_seen_topics: frozenset[str] = frozenset()
    _scan_root: str = DEFAULT_SCAN_ROOT_TOPIC
    _scan_duration: int = DEFAULT_SCAN_DURATION_SECONDS
    # The background task that drives the scan-wait loop. Held so the
    # progress step can re-enter itself as a SHOW_PROGRESS step (HA's
    # flow manager re-invokes the current step when the task completes)
    # without relaunching the scan.
    _scan_task: Any | None = None
    # The scan's wall-clock start time (from ``monotonic()``) so
    # ``_wait_for_scan`` can compute elapsed time for the progress bar.
    _scan_start_time: float = 0.0
    # The most recent scan result, stored so the step can harvest it
    # after the background task completes.
    _scan_result: Any | None = None

    @staticmethod
    @callback
    def async_get_options_flow(config_entry: Any) -> TelegrafMqttOptionsFlow:
        """Return the options flow handler for an existing entry."""
        return TelegrafMqttOptionsFlow()

    async def async_step_user(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Branch on the user's chosen setup mode.

        Previously this step collected ``topic_pattern`` and the device
        metadata directly. The two-path config flow now branches here:
        a ``SelectSelector`` lets the user choose manual or discover
        mode, and the appropriate follow-up step takes over.
        """
        if user_input is None:
            return self.async_show_form(
                step_id="user",
                data_schema=_pick_mode_schema(),
            )

        mode = user_input.get(CONF_SETUP_MODE)
        if mode == SETUP_MODE_DISCOVER:
            return await self.async_step_scan_settings()
        # Default to manual for any unknown / missing value so a
        # future-added mode doesn't strand the user.
        return await self.async_step_manual_topic()

    async def async_step_manual_topic(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Show the topic + device-metadata form (the original flow)."""
        if user_input is not None:
            errors = self._validate(user_input)
            if errors:
                return self.async_show_form(
                    step_id="manual_topic",
                    data_schema=_config_schema(
                        {
                            CONF_TOPIC_PATTERN: user_input.get(CONF_TOPIC_PATTERN, DEFAULT_TOPIC_PATTERN),
                            CONF_DEVICE_NAME: user_input.get(CONF_DEVICE_NAME, DEFAULT_DEVICE_NAME),
                            CONF_MODEL: user_input.get(CONF_MODEL, ""),
                            "manufacturer": user_input.get("manufacturer", ""),
                            CONF_SW_VERSION: user_input.get(CONF_SW_VERSION, ""),
                        }
                    ),
                    errors=errors,
                )

            topic_pattern = user_input[CONF_TOPIC_PATTERN]
            # ``_clean`` returns ``str | None``, but ``_validate`` above has
            # already rejected every input it returns ``None`` for, so the
            # narrowing here is a restatement of that check rather than a
            # second, unreachable branch. An earlier revision used an
            # ``assert`` and then an explicit ``if ... is None: show_form``;
            # neither could ever run, because ``errors`` is non-empty for
            # exactly those inputs. ``cast`` records the invariant without
            # adding a statement that can never be covered or reached.
            device_name = cast(str, _clean(user_input[CONF_DEVICE_NAME]))
            await self.async_set_unique_id(topic_pattern)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=device_name,
                data={
                    CONF_TOPIC_PATTERN: topic_pattern,
                    CONF_DEVICE_NAME: device_name,
                    "manufacturer": _clean(user_input.get("manufacturer")),
                    CONF_MODEL: _clean(user_input.get(CONF_MODEL)),
                    CONF_SW_VERSION: _clean(user_input.get(CONF_SW_VERSION)),
                },
            )

        return self.async_show_form(
            step_id="manual_topic",
            data_schema=_config_schema(
                {
                    CONF_TOPIC_PATTERN: DEFAULT_TOPIC_PATTERN,
                    CONF_DEVICE_NAME: _default_device_name(DEFAULT_TOPIC_PATTERN),
                    CONF_MODEL: "",
                    "manufacturer": "",
                    CONF_SW_VERSION: "",
                }
            ),
        )

    # ------------------------------------------------------------------
    # Discover path
    # ------------------------------------------------------------------
    async def async_step_scan_settings(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Collect the probe topic + scan window before the scan starts."""
        if user_input is not None:
            errors = self._validate_scan_settings(user_input)
            if errors:
                return self.async_show_form(
                    step_id="scan_settings",
                    data_schema=_scan_settings_schema(),
                    errors=errors,
                )
            self._scan_root = _clean(user_input[CONF_SCAN_ROOT_TOPIC]) or DEFAULT_SCAN_ROOT_TOPIC
            self._scan_duration = int(user_input[CONF_SCAN_DURATION_SECONDS])
            return await self.async_step_scan_running()

        return self.async_show_form(
            step_id="scan_settings",
            data_schema=_scan_settings_schema(),
        )

    async def async_step_scan_running(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Run the snoop for the configured window, then move to the picker.

        The snoop installs a transient broker subscription on the user's
        configured probe topic, listens for ``scan_duration_seconds``,
        tears itself down, and we move to the pick-topics step with the
        collected topics. ``async_step_pick_topics`` will read
        ``self._scan_seen_topics`` and present the roll-up.

        The scan is launched exactly once per visit to this step. The
        user can navigate back to ``scan_settings`` to adjust the
        probe / duration -- in that case ``async_step_scan_running``
        will be re-entered and the scan will be relaunched with the
        new parameters.

        Progress is surfaced via HA's ``async_show_progress`` /
        ``async_show_progress_done`` protocol so the frontend renders a
        determinate progress bar (``progress_action="scan_running"``)
        instead of blocking the step handler silently for up to 300s.
        """
        # The user landed on this step without submitting scan_settings
        # (e.g. they hit the menu choice). The defaults from the
        # settings schema are filled in so the scan still has a
        # sensible probe root + window.
        if not getattr(self, "_scan_duration", None):
            self._scan_root = DEFAULT_SCAN_ROOT_TOPIC
            self._scan_duration = DEFAULT_SCAN_DURATION_SECONDS

        # If a previous visit already launched the scan task and it
        # has not finished, re-show progress (HA re-invokes the step
        # when the flow manager advances it). If the task is done,
        # harvest its result and chain to the next step.
        if self._scan_task is not None and not self._scan_task.done():
            return self.async_show_progress(
                step_id="scan_running",
                progress_action="scan_running",
                description_placeholders={
                    "probe": self._scan_root,
                    "duration": str(self._scan_duration),
                },
                progress_task=self._scan_task,
            )

        if self._scan_task is not None and self._scan_task.done():
            # The scan completed (or was force-stopped at the deadline);
            # harvest the result and chain to the next step.
            self._scan_task = None
            result = self._scan_result
            if result is None:  # pragma: no cover - defensive
                # Defensive: the task should always store a result before
                # completing, but if it somehow does not, treat as no
                # traffic and chain to the no-traffic step.
                return self.async_show_progress_done(next_step_id="scan_no_traffic")
            self._scan_seen_topics = result.topics
            if not self._scan_seen_topics:
                return self.async_show_progress_done(next_step_id="scan_no_traffic")
            return self.async_show_progress_done(next_step_id="pick_topics")

        # First (re-)entry: launch the background scan-wait task.
        snoop = await self._start_scan(self._scan_root, float(self._scan_duration))
        if snoop is None:
            # The broker refused the scan subscription. ``_start_scan``
            # has already logged the reason; send the user back to the
            # settings form with a translated error instead of showing a
            # stack trace and abandoning the flow.
            return self.async_show_form(
                step_id="scan_settings",
                data_schema=_scan_settings_schema(),
                errors={"base": "scan_failed"},
            )
        self._scan_snoop = snoop
        self._scan_start_time = monotonic()
        # Bind duration/root at task creation, not inside the task: a second
        # scan overwriting ``self._scan_duration`` mid-flight must not change
        # how the *first* task's progress bar and deadline are computed.
        self._scan_task = self.hass.async_create_task(
            self._wait_for_scan(snoop, float(self._scan_duration), self._scan_root),
        )
        return self.async_show_progress(
            step_id="scan_running",
            progress_action="scan_running",
            description_placeholders={
                "probe": self._scan_root,
                "duration": str(self._scan_duration),
            },
            progress_task=self._scan_task,
        )

    async def _wait_for_scan(self, snoop: Any, duration: float, root: str) -> Any:
        """Wait for the snoop's auto-stop timer, emitting progress.

        Returns the ``SnoopResult`` from ``snoop.stop()``. Emits
        integer percentage updates via ``async_update_progress`` so the
        frontend renders a determinate progress bar showing how much of
        the configured scan window has elapsed. A safety deadline
        (``duration + 5s``) force-stops the snoop if the auto-stop
        timer misfires, so the broker subscription cannot outlive the
        flow.

        ``duration`` and ``root`` are passed in rather than read off
        ``self``. A user who goes back and changes the scan window
        re-enters this step; the previously-created task is still live
        and was reading ``self._scan_duration`` -- so it would report
        progress against the *new* window while its own deadline had been
        computed from the *old* one, and the old task's ``_scan_result``
        would race the new one. Binding both values at task creation
        removes the shared mutable state entirely.
        """
        deadline = float(duration) + 5.0
        last_percent = -1
        try:
            async with asyncio.timeout(deadline):
                # Emit an initial progress event so the frontend renders
                # the bar immediately rather than waiting for the first
                # tick. The scan may finish on the first check
                # (``is_finished`` is already True from a parked
                # auto-stop timer in tests), so without this the
                # frontend would never see a progress event.
                self.async_update_progress(0.0)
                while not snoop.is_finished:
                    await asyncio.sleep(0.1)
                    elapsed = monotonic() - self._scan_start_time
                    progress = min(1.0, elapsed / float(duration))
                    percent = int(progress * 100)
                    if percent != last_percent:
                        self.async_update_progress(progress)
                        last_percent = percent
        except TimeoutError:
            _LOGGER.debug("Scan for %s exceeded deadline; stopping snoop", root)
            snoop.stop()
        result = snoop.stop()
        self._scan_seen_topics = result.topics
        self._scan_result = result
        return result

    async def async_step_scan_no_traffic(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Show the scan_settings form with a no-traffic error.

        A progress step cannot transition directly to a form step, so
        ``async_step_scan_running`` chains here via
        ``async_show_progress_done(next_step_id="scan_no_traffic")`` when
        the scan captures zero topics. This step immediately re-shows
        the settings form with a clear error so the user can widen the
        probe or shorten the window.
        """
        return self.async_show_form(
            step_id="scan_settings",
            data_schema=_scan_settings_schema(),
            errors={"base": "no_traffic_on_scan_root"},
        )

    async def async_step_pick_topics(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Present the roll-up of seen topics and let the user pick one."""
        prefixes = _roll_up_topics(self._scan_seen_topics)
        pre_selected = [p for p in prefixes if _looks_telegraf_shaped(p)]

        if user_input is not None:
            # The picker is single-select: the runtime subscription
            # supports exactly one pattern per entry, so the
            # submission is a single string and nothing is silently
            # discarded. A user who wants another topic root adds
            # another entry (re-running the scan or typing the
            # pattern manually).
            pattern = user_input.get(CONF_TOPIC_PATTERN)
            if not pattern:
                return self.async_show_form(
                    step_id="pick_topics",
                    data_schema=_pick_topics_schema(prefixes, pre_selected),
                    errors={CONF_TOPIC_PATTERN: "no_topics_selected"},
                )

            # The pick must be a syntactically valid subscription.
            if not _valid_subscription_topic(pattern):
                return self.async_show_form(
                    step_id="pick_topics",
                    data_schema=_pick_topics_schema(prefixes, pre_selected),
                    errors={CONF_TOPIC_PATTERN: "invalid_topic"},
                )

            chosen = pattern
            device_name = _default_device_name(chosen)
            await self.async_set_unique_id(chosen)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(
                title=device_name,
                data={
                    CONF_TOPIC_PATTERN: chosen,
                    CONF_DEVICE_NAME: device_name,
                    "manufacturer": None,
                    CONF_MODEL: None,
                    CONF_SW_VERSION: None,
                },
            )

        return self.async_show_form(
            step_id="pick_topics",
            data_schema=_pick_topics_schema(prefixes, pre_selected),
        )

    # ------------------------------------------------------------------
    # Scan plumbing
    # ------------------------------------------------------------------
    async def _start_scan(self, probe_topic: str, duration: float) -> Any:
        """Subscribe a snoop to ``probe_topic`` for ``duration`` seconds.

        Returns the live ``SnoopListener``, or ``None`` if the broker
        refused the subscription -- the caller turns that into a
        translated form error. The caller is responsible for stopping
        the listener (the running step waits on ``listener.is_finished``
        and then calls ``stop()``). The snoop is also wired to
        ``async_on_unload`` so a flow abort tears the subscription down
        before the next attempt can leak.
        """
        from .snoop import SnoopListener  # local import keeps the module HA-agnostic

        listener = SnoopListener(
            probe_topic=probe_topic,
            timeout_seconds=float(duration),
        )
        # ``mqtt.async_subscribe`` lives on HA's mqtt component. The
        # import is wrapped so a stripped-down test environment can
        # still drive the flow via a monkeypatched subscribe.
        from homeassistant.components import mqtt

        try:
            await listener.start(self.hass, mqtt.async_subscribe)
        except Exception as scan_err:
            # The subscribe is a broker round-trip and can fail for
            # reasons the user can act on (an ACL that denies the probe
            # root, a broker that is not up, a filter the broker
            # rejects). Letting the exception escape the step shows an
            # opaque stack trace in the UI and abandons the flow; a
            # translated ``scan_failed`` on the settings form tells them
            # what to fix and keeps their input.
            _LOGGER.warning("Topic discovery scan could not subscribe to %s: %s", probe_topic, scan_err)
            return None

        # Make sure the snoop is torn down if the user closes the flow
        # before the scan finishes.
        if hasattr(self, "async_on_unload"):
            self.async_on_unload(listener.stop)
        return listener

    async def async_step_reconfigure(self, user_input: dict[str, Any] | None = None) -> ConfigFlowResult:
        """Phase 9: handle the reconfigure step.

        A reconfigure that changes the topic pattern triggers a reload of
        the config entry, which swaps the MQTT subscription cleanly via
        ``async_unload_entry`` + ``async_setup_entry``. Metadata-only
        reconfigures (device_name, manufacturer, model, sw_version) also
        reload because the DeviceInfo carried on every entity is built
        from ``entry.data`` -- a reload is the simplest way to refresh
        the visible device metadata.

        **The subscribe pre-flight.** The new pattern is checked against the
        broker *before* it is committed. Previously the flow wrote
        ``data_updates`` unconditionally, so a pattern the broker rejects (a
        typo the syntax check cannot catch, an ACL that denies it, a broker
        that is not actually up) was committed and then failed the reload --
        leaving the user with a silently broken entry and a dialog that had
        already reported success. A short-lived subscription is opened on the
        candidate and torn down again on success; on failure the form comes
        back with a translated error and ``entry.data`` is untouched.

        The reload this triggers is also what re-points auto-discover at the
        new pattern: ``async_unload_entry`` stops the snoop and
        ``async_setup_entry`` starts a fresh one whose ``exclude_filter`` is
        the new ``topic_pattern``.
        """
        entry = self._get_entry()
        if user_input is not None:
            errors = self._validate(user_input)
            if errors:
                return self.async_show_form(
                    step_id="reconfigure",
                    data_schema=_config_schema(
                        {
                            CONF_TOPIC_PATTERN: user_input.get(CONF_TOPIC_PATTERN, ""),
                            CONF_DEVICE_NAME: user_input.get(CONF_DEVICE_NAME, ""),
                            CONF_MODEL: user_input.get(CONF_MODEL, ""),
                            "manufacturer": user_input.get("manufacturer", ""),
                            CONF_SW_VERSION: user_input.get(CONF_SW_VERSION, ""),
                        }
                    ),
                    errors=errors,
                )

            topic = user_input[CONF_TOPIC_PATTERN]
            # See ``async_step_manual_topic``: ``_validate`` has already
            # rejected every input ``_clean`` returns ``None`` for, so this
            # narrowing restates that check rather than adding a branch that
            # can never be taken.
            device_name = cast(str, _clean(user_input[CONF_DEVICE_NAME]))
            await self.async_set_unique_id(topic)
            self._abort_if_unique_id_configured()
            try:
                await self._can_subscribe(topic)
            except ReconfigureSubscribeFailed as preflight_err:
                # Log the translated exception so the developer-facing
                # line and the user's ``cannot_connect`` toast describe
                # the same failure. The form re-renders with the user's
                # input intact; ``entry.data`` is untouched, so the entry
                # keeps running on its previous pattern.
                _LOGGER.debug("Reconfigure rejected: %s", preflight_err)
                return self.async_show_form(
                    step_id="reconfigure",
                    data_schema=_config_schema(
                        {
                            CONF_TOPIC_PATTERN: topic,
                            CONF_DEVICE_NAME: device_name or "",
                            CONF_MODEL: user_input.get(CONF_MODEL, ""),
                            "manufacturer": user_input.get("manufacturer", ""),
                            CONF_SW_VERSION: user_input.get(CONF_SW_VERSION, ""),
                        }
                    ),
                    errors={"base": "cannot_connect"},
                )
            return self.async_update_reload_and_abort(
                entry,
                data_updates={
                    CONF_TOPIC_PATTERN: topic,
                    CONF_DEVICE_NAME: device_name,
                    "manufacturer": _clean(user_input.get("manufacturer")),
                    CONF_MODEL: _clean(user_input.get(CONF_MODEL)),
                    CONF_SW_VERSION: _clean(user_input.get(CONF_SW_VERSION)),
                },
            )

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=_config_schema(
                {
                    CONF_TOPIC_PATTERN: entry.data.get(CONF_TOPIC_PATTERN, DEFAULT_TOPIC_PATTERN),
                    CONF_DEVICE_NAME: entry.data.get(CONF_DEVICE_NAME, DEFAULT_DEVICE_NAME),
                    CONF_MODEL: entry.data.get(CONF_MODEL) or "",
                    "manufacturer": entry.data.get("manufacturer") or "",
                    CONF_SW_VERSION: entry.data.get(CONF_SW_VERSION) or "",
                }
            ),
        )

    def _validate(self, user_input: dict[str, Any]) -> dict[str, str]:
        """Return a per-field error dict, or {} when the input is valid."""
        errors: dict[str, str] = {}
        topic = user_input.get(CONF_TOPIC_PATTERN, "")
        if not _valid_subscription_topic(topic):
            errors[CONF_TOPIC_PATTERN] = "invalid_topic"
        # Normalize CONF_DEVICE_NAME with _clean so whitespace-only values
        # are rejected the same way as an empty string, and the stripped
        # value is the one stored on the entry (see async_step_user and
        # async_step_reconfigure for the matching create-entry path).
        if _clean(user_input.get(CONF_DEVICE_NAME)) is None:
            errors[CONF_DEVICE_NAME] = "required"
        return errors

    def _validate_scan_settings(self, user_input: dict[str, Any]) -> dict[str, str]:
        """Validate the scan-settings form (probe root + duration)."""
        errors: dict[str, str] = {}
        probe = user_input.get(CONF_SCAN_ROOT_TOPIC, "")
        if not _valid_subscription_topic(probe):
            errors[CONF_SCAN_ROOT_TOPIC] = "invalid_topic"
        duration = user_input.get(CONF_SCAN_DURATION_SECONDS, 0)
        try:
            duration_int = int(duration)
        except (TypeError, ValueError):  # fmt: skip
            errors[CONF_SCAN_DURATION_SECONDS] = "invalid_duration"
            return errors
        if duration_int < MIN_SCAN_DURATION_SECONDS or duration_int > MAX_SCAN_DURATION_SECONDS:
            errors[CONF_SCAN_DURATION_SECONDS] = "invalid_duration"
        return errors

    async def _can_subscribe(self, topic: str) -> None:
        """Verify the broker accepts a subscription on ``topic``.

        Opens a short-lived subscription and immediately tears it down.
        Returns ``None`` when the broker accepted it; raises
        ``ReconfigureSubscribeFailed`` (translated, carrying ``topic`` and
        ``error``) when it did not or the wait timed out.

        The unsubscribe is guaranteed on the success path, so a successful
        pre-flight leaves the broker holding exactly the subscriptions the
        running entry already has (main + snoop) -- this check must never
        be the reason the broker ends up with an extra subscriber.

        The wait is bounded: ``async_subscribe`` on a connected broker
        resolves quickly, but on one that is mid-reconnect it can await
        indefinitely, which would leave the dialog hung with no feedback.
        The timeout turns that into the same form error as an outright
        rejection.
        """
        from homeassistant.components import mqtt

        try:
            async with asyncio.timeout(RECONFIGURE_PREFLIGHT_TIMEOUT_SECONDS):
                unsubscribe = await mqtt.async_subscribe(self.hass, topic, _preflight_message_sink)
        except Exception as preflight_err:
            raise ReconfigureSubscribeFailed(topic, str(preflight_err)) from preflight_err
        try:
            unsubscribe()
        except Exception:  # fmt: skip
            # A broker that rejects the UNSUBSCRIBE is a broker problem,
            # not a reason to block the user's reconfigure; the
            # subscription is dropped when the MQTT integration reconnects.
            _LOGGER.debug("Reconfigure pre-flight unsubscribe failed for %s", topic)

    def _get_entry(self) -> ConfigEntry:
        """Return the entry being reconfigured."""
        entry: ConfigEntry = self.hass.config_entries.async_get_known_entry(self.context["entry_id"])
        return entry


async def _preflight_message_sink(message: Any) -> None:
    """Discard a message the reconfigure pre-flight subscription receives.

    The pre-flight only asks "does the broker accept this filter?", not
    "what is on it?". A real sink is still required: HA's MQTT
    integration rejects a callable-shaped message handler, and a retained
    message delivered during the pre-flight must not raise.
    """
    return None


# Re-export so the ``__init__`` setup can validate the runtime strategy
# at startup without importing ``.const`` separately.
__all__ = [
    "VALID_PLATFORM_HINTS",
    "TelegrafMqttConfigFlow",
    "TelegrafMqttOptionsFlow",
]
