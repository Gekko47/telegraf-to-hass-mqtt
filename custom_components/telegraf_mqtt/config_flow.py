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
from typing import Any

import voluptuous as vol
from homeassistant import config_entries
from homeassistant.config_entries import ConfigEntry, ConfigFlowResult
from homeassistant.core import callback
from homeassistant.helpers import selector

from .const import (
    CONF_AUTO_DISCOVER,
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
    SETUP_MODE_DISCOVER,
    SETUP_MODE_MANUAL,
    VALID_DEVICE_ID_STRATEGIES,
    VALID_PLATFORM_HINTS,
)

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
    """Build the pick-list form for the discover-topics flow.

    ``prefixes`` is the roll-up of every 2nd-level prefix the scan saw.
    ``pre_selected`` is the subset of those prefixes that look
    Telegraf-shaped -- the user can deselect or add custom values.
    """
    options = [
        selector.SelectOptionDict(value=p, label=p)
        for p in prefixes[:_MAX_PICK_LIST_OPTIONS]
    ]
    return vol.Schema(
        {
            vol.Optional(
                CONF_TOPIC_PATTERN,
                default=pre_selected,
            ): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=options,
                    custom_value=True,
                    multiple=True,
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
    """
    enable_cleanup = current_options.get(CONF_ENABLE_CLEANUP, DEFAULT_ENABLE_CLEANUP)
    cleanup_delay = current_options.get(CONF_CLEANUP_DELAY, DEFAULT_CLEANUP_DELAY)
    delete_delay = current_options.get(CONF_DELETE_DELAY, DEFAULT_DELETE_DELAY)
    min_active_metrics = current_options.get(CONF_MIN_ACTIVE_METRICS, DEFAULT_MIN_ACTIVE_METRICS)
    auto_discover = current_options.get(CONF_AUTO_DISCOVER, DEFAULT_AUTO_DISCOVER)
    device_id_strategy = current_options.get(CONF_DEVICE_ID_STRATEGY, DEFAULT_DEVICE_ID_STRATEGY)
    category_overrides = current_options.get(CONF_CATEGORY_OVERRIDES, {})
    non_negative_int = vol.All(int, vol.Range(min=0))

    return vol.Schema(
        {
            vol.Optional(CONF_AUTO_DISCOVER, default=auto_discover): bool,
            vol.Optional(CONF_DEVICE_ID_STRATEGY, default=device_id_strategy): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[
                        selector.SelectOptionDict(value=v, label=_strategy_label(v)) for v in VALID_DEVICE_ID_STRATEGIES
                    ],
                    mode=selector.SelectSelectorMode.DROPDOWN,
                )
            ),
            vol.Optional(CONF_EXPIRE_AFTER, default=DEFAULT_EXPIRE_AFTER): vol.All(int, vol.Range(min=1)),
            vol.Optional(CONF_ENABLE_CLEANUP, default=enable_cleanup): bool,
            vol.Optional(CONF_CLEANUP_DELAY, default=cleanup_delay): non_negative_int,
            vol.Optional(CONF_DELETE_DELAY, default=delete_delay): non_negative_int,
            vol.Optional(CONF_MIN_ACTIVE_METRICS, default=min_active_metrics): non_negative_int,
            vol.Optional(CONF_EXCLUDE_PATTERNS, default=[]): selector.SelectSelector(
                selector.SelectSelectorConfig(
                    options=[],
                    custom_value=True,
                    multiple=True,
                    mode=selector.SelectSelectorMode.LIST,
                )
            ),
            vol.Optional(CONF_FIELD_OVERRIDES, default={}): selector.ObjectSelector(),
            vol.Optional(CONF_CATEGORY_OVERRIDES, default=category_overrides): selector.ObjectSelector(),
        }
    )


def _clean_options(user_input: dict[str, Any]) -> dict[str, Any]:
    """Normalize user input from the options flow before persisting.

    Phase 10: ``CONF_CATEGORY_OVERRIDES`` is an ObjectSelector result;
    the user can leave it as ``{}``, so we coerce the empty form value
    back to an empty dict.
    """
    return {
        CONF_AUTO_DISCOVER: bool(user_input.get(CONF_AUTO_DISCOVER, DEFAULT_AUTO_DISCOVER)),
        CONF_DEVICE_ID_STRATEGY: str(user_input.get(CONF_DEVICE_ID_STRATEGY, DEFAULT_DEVICE_ID_STRATEGY)),
        CONF_EXPIRE_AFTER: int(user_input.get(CONF_EXPIRE_AFTER, DEFAULT_EXPIRE_AFTER)),
        CONF_ENABLE_CLEANUP: bool(user_input.get(CONF_ENABLE_CLEANUP, DEFAULT_ENABLE_CLEANUP)),
        CONF_CLEANUP_DELAY: int(user_input.get(CONF_CLEANUP_DELAY, DEFAULT_CLEANUP_DELAY)),
        CONF_DELETE_DELAY: int(user_input.get(CONF_DELETE_DELAY, DEFAULT_DELETE_DELAY)),
        CONF_MIN_ACTIVE_METRICS: int(user_input.get(CONF_MIN_ACTIVE_METRICS, DEFAULT_MIN_ACTIVE_METRICS)),
        CONF_EXCLUDE_PATTERNS: list(user_input.get(CONF_EXCLUDE_PATTERNS, [])),
        CONF_FIELD_OVERRIDES: dict(user_input.get(CONF_FIELD_OVERRIDES, {})),
        CONF_CATEGORY_OVERRIDES: dict(user_input.get(CONF_CATEGORY_OVERRIDES, {})),
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
            return self.async_create_entry(title="", data=_clean_options(user_input))

        return self.async_show_form(
            step_id="init",
            data_schema=_build_options_schema(self.config_entry.options),
        )


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

    VERSION = 1

    # Per-flow scan state. The snoop listener is held so the running
    # step can wait on its auto-stop timer; the seen topics persist
    # between ``async_step_scan_running`` and ``async_step_pick_topics``
    # so the user can navigate back without re-running the scan.
    _scan_snoop: Any | None = None
    _scan_seen_topics: frozenset[str] = frozenset()
    _scan_root: str = DEFAULT_SCAN_ROOT_TOPIC
    _scan_duration: int = DEFAULT_SCAN_DURATION_SECONDS

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

    async def async_step_manual_topic(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
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
            device_name = _clean(user_input[CONF_DEVICE_NAME])
            assert device_name is not None  # guarded by _validate above
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
    async def async_step_scan_settings(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
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

    async def async_step_scan_running(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
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
        """
        # The user landed on this step without submitting scan_settings
        # (e.g. they hit the menu choice). The defaults from the
        # settings schema are filled in so the scan still has a
        # sensible probe root + window.
        if not getattr(self, "_scan_duration", None):
            self._scan_root = DEFAULT_SCAN_ROOT_TOPIC
            self._scan_duration = DEFAULT_SCAN_DURATION_SECONDS

        snoop = await self._start_scan(self._scan_root, float(self._scan_duration))
        self._scan_snoop = snoop

        # Wait for the auto-stop timer. ``is_finished`` flips when
        # ``_on_timeout`` runs; we also bail out if the user closed
        # the flow (the snoop is torn down by the unload hook).
        deadline = float(self._scan_duration) + 5.0
        try:
            async with asyncio.timeout(deadline):
                while not snoop.is_finished:
                    await asyncio.sleep(0.1)
        except TimeoutError:
            # The scan should auto-stop well before this. If we got
            # here, something went wrong with the timer -- stop the
            # snoop explicitly so we don't leak the subscription.
            _LOGGER.debug("Scan for %s exceeded deadline; stopping snoop", self._scan_root)
            snoop.stop()

        result = snoop.stop()
        self._scan_seen_topics = result.topics

        if not self._scan_seen_topics:
            # No traffic on the probe topic. Send the user back to
            # the settings step with a clear error so they can either
            # widen the probe or shorten the window.
            return self.async_show_form(
                step_id="scan_settings",
                data_schema=_scan_settings_schema(),
                errors={"base": "no_traffic_on_scan_root"},
            )

        return await self.async_step_pick_topics()

    async def async_step_pick_topics(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Present the roll-up of seen topics and let the user pick."""
        prefixes = _roll_up_topics(self._scan_seen_topics)
        pre_selected = [p for p in prefixes if _looks_telegraf_shaped(p)]

        if user_input is not None:
            picks = user_input.get(CONF_TOPIC_PATTERN, []) or []
            if not picks:
                return self.async_show_form(
                    step_id="pick_topics",
                    data_schema=_pick_topics_schema(prefixes, pre_selected),
                    errors={CONF_TOPIC_PATTERN: "no_topics_selected"},
                )

            # Each pick must be a syntactically valid subscription.
            invalid = [p for p in picks if not _valid_subscription_topic(p)]
            if invalid:
                return self.async_show_form(
                    step_id="pick_topics",
                    data_schema=_pick_topics_schema(prefixes, pre_selected),
                    errors={CONF_TOPIC_PATTERN: "invalid_topic"},
                )

            # The picker is multi-select but the current MQTT
            # subscription only supports a single pattern. Pin to the
            # first pick -- the common case is a single Telegraf
            # deployment per broker, and the user can adjust via
            # reconfigure if they need a custom pattern.
            chosen = picks[0]
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

        Returns the live ``SnoopListener``. The caller is responsible
        for stopping it (the running step waits on
        ``listener.is_finished`` and then calls ``stop()``). The
        snoop is also wired to ``async_on_unload`` so a flow abort
        tears the subscription down before the next attempt can leak.
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

        await listener.start(self.hass, mqtt.async_subscribe)

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
            device_name = _clean(user_input[CONF_DEVICE_NAME])
            assert device_name is not None  # guarded by _validate above
            await self.async_set_unique_id(topic)
            self._abort_if_unique_id_configured()
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
        except (TypeError, ValueError):
            errors[CONF_SCAN_DURATION_SECONDS] = "invalid_duration"
            return errors
        if (
            duration_int < MIN_SCAN_DURATION_SECONDS
            or duration_int > MAX_SCAN_DURATION_SECONDS
        ):
            errors[CONF_SCAN_DURATION_SECONDS] = "invalid_duration"
        return errors

    def _get_entry(self) -> ConfigEntry:
        """Return the entry being reconfigured."""
        entry: ConfigEntry = self.hass.config_entries.async_get_known_entry(self.context["entry_id"])
        return entry


# Re-export so the ``__init__`` setup can validate the runtime strategy
# at startup without importing ``.const`` separately.
__all__ = [
    "VALID_PLATFORM_HINTS",
    "TelegrafMqttConfigFlow",
    "TelegrafMqttOptionsFlow",
]
