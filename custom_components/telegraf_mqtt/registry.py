"""Registry primitives for telegraf_mqtt."""

from __future__ import annotations

import dataclasses
import hashlib
import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from fnmatch import fnmatch
from time import monotonic
from typing import Any, cast

from .const import (
    CLEANUP_POLICY_ALWAYS,
    CLEANUP_POLICY_NEVER,
    DEFAULT_DEVICE_ID_STRATEGY,
    PLATFORM_HINT_AUTO,
    PLATFORM_HINT_NONE,
    VALID_DEVICE_ID_STRATEGIES,
    VALID_PLATFORM_HINTS,
)
from .models import MetricDescriptor, PlatformHint, coerce_to_bool
from .naming import apply_category_override

_LOGGER = logging.getLogger(__name__)

_DEVICE_SLUG_RE = re.compile(r"[^a-z0-9]+")


def _resolve_category_for(
    descriptor: MetricDescriptor,
    overrides: dict[str, str | None],
) -> str | None:
    """Return the entity category after applying the per-entity override.

    Returns ``descriptor.entity_category`` unchanged when no override
    key is present. Re-resolving the heuristic unconditionally would
    stomp on descriptors that were constructed with a non-default
    category (e.g. in tests, or by a future feature that wants a
    different baseline).
    """
    if not overrides or descriptor.unique_key not in overrides:
        return descriptor.entity_category
    return apply_category_override(
        descriptor.measurement,
        descriptor.field,
        descriptor.unique_key,
        overrides,
    )


def _slugify_device(value: str) -> str:
    """Return a deterministic, stable, collision-resistant device slug.

    Readable text is preserved; when normalization would collapse distinct
    characters (e.g. ``host-1`` vs ``host_1``), a short digest of the original
    value is appended so two different identifiers never merge into one slug.

    Two identifiers that differ only in case (``localhost`` vs
    ``LOCALHOST``) deliberately map to the same slug, because case-only
    differences are *exactly* the kind of accidental collision a Telegraf
    host tag produces when one container inherits the default. The
    digest is only appended when normalization collapsed distinct
    non-case characters.
    """
    lowered = value.lower()
    slug = _DEVICE_SLUG_RE.sub("_", lowered).strip("_")
    if slug == lowered:
        # Normalization was lossless (case-insensitive); keep the readable
        # slug unchanged so two case-variants of the same identifier
        # deliberately collapse -- they're the same Telegraf host.
        return slug or "unknown"
    digest = hashlib.sha256(value.encode("utf-8")).hexdigest()[:32]
    return f"{slug or 'unknown'}_{digest}"


@dataclass
class MetricState:
    """Current state stored for a descriptor key.

    Phase 6 lifecycle: a metric moves through
    ``Active -> Unavailable -> Cleanup Candidate -> Deleted``. The
    ``cleanup_candidate_since`` timestamp is set on the first
    Active->Unavailable transition (``check_expiry``) and cleared by any
    subsequent ``update`` that brings the metric back to life. The
    ``cleanup`` pass only removes metrics that have been candidates for
    at least ``cleanup_delay`` seconds; static-metadata metrics
    (``cleanup_policy == "NEVER"``) never become candidates.
    """

    raw_descriptor: MetricDescriptor
    descriptor: MetricDescriptor
    device_id: str
    device_name: str
    last_updated: float
    is_available: bool = True
    cleanup_candidate_since: float | None = None

    @property
    def value(self) -> Any:
        """Expose the current metric payload through the state wrapper."""
        return self.descriptor.value


class MetricRegistry:
    """Track metric descriptors, availability, and write-on-change behavior."""

    def __init__(
        self,
        expire_after: int = 120,
        *,
        clock: Callable[[], float] | None = None,
        exclude_patterns: tuple[str, ...] = (),
        field_overrides: dict[str, dict[str, Any]] | None = None,
        device_id: str = "default",
        device_name: str = "Telegraf MQTT",
        cleanup_delay: int = 30 * 24 * 60 * 60,
        delete_delay: int = 60 * 24 * 60 * 60,
        category_overrides: dict[str, str | None] | None = None,
        device_id_strategy: str = DEFAULT_DEVICE_ID_STRATEGY,
    ) -> None:
        self._expire_after = expire_after
        self._clock = clock or monotonic
        self._exclude_patterns = exclude_patterns
        self._field_overrides = field_overrides or {}
        self._states: dict[str, MetricState] = {}
        self.device_id = device_id
        self.device_name = device_name
        self.last_any_metric = 0.0
        self._cleanup_delay = cleanup_delay
        self._delete_delay = delete_delay
        self._category_overrides = category_overrides or {}
        self._device_id_strategy = (
            device_id_strategy if device_id_strategy in VALID_DEVICE_ID_STRATEGIES else DEFAULT_DEVICE_ID_STRATEGY
        )

    def apply_options(
        self,
        *,
        expire_after: int | None = None,
        exclude_patterns: tuple[str, ...] | None = None,
        field_overrides: dict[str, dict[str, Any]] | None = None,
        cleanup_delay: int | None = None,
        delete_delay: int | None = None,
        category_overrides: dict[str, str | None] | None = None,
        device_id_strategy: str | None = None,
        on_write: Callable[[str, bool, Any], None] | None = None,
    ) -> None:
        """Apply live configuration options without rebuilding the registry."""
        if expire_after is not None:
            self._expire_after = expire_after
        if cleanup_delay is not None:
            self._cleanup_delay = cleanup_delay
        if delete_delay is not None:
            self._delete_delay = delete_delay
        if category_overrides is not None:
            self._category_overrides = category_overrides
            # Re-emit so the platform's _refresh_descriptor_attributes picks up the new category.
            for unique_key, state in self._states.items():
                descriptor = self._apply_overrides(state.raw_descriptor)
                if descriptor == state.descriptor:
                    continue
                state.descriptor = descriptor
                if on_write is not None:
                    on_write(unique_key, state.is_available, state.value)
        if device_id_strategy is not None and device_id_strategy in VALID_DEVICE_ID_STRATEGIES:
            self._device_id_strategy = device_id_strategy
        if exclude_patterns is not None:
            self._exclude_patterns = exclude_patterns
            for unique_key, state in self._states.items():
                if not self._matches_exclude(unique_key):
                    continue
                if state.is_available:
                    state.is_available = False
                    if on_write is not None:
                        on_write(unique_key, False, state.value)
        if field_overrides is not None:
            self._field_overrides = field_overrides
            for unique_key, state in self._states.items():
                descriptor = self._apply_overrides(state.raw_descriptor)
                if descriptor == state.descriptor:
                    continue
                state.descriptor = descriptor
                if on_write is not None:
                    on_write(unique_key, state.is_available, state.value)

    def get(self, unique_key: str) -> MetricState | None:
        """Return the current state for a key if one already exists."""
        return self._states.get(unique_key)

    def keys(self) -> tuple[str, ...]:
        """Return known metric keys."""
        return tuple(self._states)

    def _matches_exclude(self, unique_key: str) -> bool:
        return any(fnmatch(unique_key, pattern) for pattern in self._exclude_patterns)

    def _apply_overrides(self, descriptor: MetricDescriptor) -> MetricDescriptor:
        override = self._field_overrides.get(descriptor.field)
        category = _resolve_category_for(descriptor, self._category_overrides)

        # No per-field override + no per-entity category override -> fast path.
        if override is None and category is descriptor.entity_category:
            return descriptor

        platform_hint: PlatformHint = PLATFORM_HINT_AUTO
        new_value = descriptor.value
        if override is not None:
            platform_hint_raw = override.get("platform", PLATFORM_HINT_AUTO)
            if platform_hint_raw in VALID_PLATFORM_HINTS:
                platform_hint = cast(PlatformHint, platform_hint_raw)
            if platform_hint == "binary_sensor" and not isinstance(descriptor.value, bool):
                # The user wants this field on a binary_sensor platform; coerce
                # int 0/1 (or a string) to a bool using the shared convention.
                new_value = coerce_to_bool(descriptor.value)

        return MetricDescriptor(
            unique_key=descriptor.unique_key,
            measurement=descriptor.measurement,
            tags=descriptor.tags,
            field=descriptor.field,
            value=new_value,
            timestamp=descriptor.timestamp,
            native_unit=(
                override.get("native_unit", descriptor.native_unit) if override is not None else descriptor.native_unit
            ),
            suggested_device_class=(
                override.get("device_class", descriptor.suggested_device_class)
                if override is not None
                else descriptor.suggested_device_class
            ),
            suggested_state_class=(
                override.get("state_class", descriptor.suggested_state_class)
                if override is not None
                else descriptor.suggested_state_class
            ),
            entity_category=category,
            cleanup_policy=descriptor.cleanup_policy,
            device_id=descriptor.device_id,
            translation_key=descriptor.translation_key,
            translation_placeholders=descriptor.translation_placeholders,
            platform_hint=platform_hint,
        )

    def pending_cleanup(self) -> list[dict[str, Any]]:
        """Return a snapshot of metrics currently in the Cleanup Candidate state.

        Phase 10: surfaced in the diagnostics payload so a user with a
        misbehaving ``cleanup_delay`` can see exactly which entities are
        queued for removal. Each entry carries the unique_key, the time
        it became a candidate, and the seconds until it would actually
        be removed on the next cleanup tick. Redaction of device_id is
        the diagnostics layer's responsibility -- here we just emit
        everything the registry knows.

        Returned list is sorted by ``cleanup_candidate_since`` (oldest
        first) so the diagnostics view is stable.
        """
        now = self._clock()
        candidates: list[dict[str, Any]] = []
        for unique_key, state in self._states.items():
            if state.cleanup_candidate_since is None:
                continue
            seconds_in_state = max(0.0, now - state.cleanup_candidate_since)
            seconds_until_removal = max(0.0, self._cleanup_delay - seconds_in_state)
            candidates.append(
                {
                    "unique_key": unique_key,
                    "cleanup_candidate_since": state.cleanup_candidate_since,
                    "seconds_until_removal": seconds_until_removal,
                }
            )
        candidates.sort(key=lambda entry: entry["cleanup_candidate_since"])
        return candidates

    def update(
        self,
        descriptor: MetricDescriptor,
        *,
        on_write: Callable[[str, bool, Any], None] | None = None,
        on_discovered: Callable[[str], None] | None = None,
        metric_key: str | None = None,
    ) -> bool:
        """Store a descriptor and emit state updates only when value or availability changes.

        Phase 6: any incoming message clears ``cleanup_candidate_since`` --
        the metric is alive again, so it cannot be a candidate for removal.
        """
        raw_descriptor = descriptor
        descriptor = self._apply_overrides(raw_descriptor)
        if self._matches_exclude(raw_descriptor.unique_key):
            return False
        # Phase 10: a field override of `{"platform": "none"}` removes
        # the field from the registry entirely. We also drop the existing
        # state if this is a transition from a non-`none` hint to `none`.
        if descriptor.platform_hint == PLATFORM_HINT_NONE:
            existing = self._states.pop(raw_descriptor.unique_key, None)
            if existing is not None and on_write is not None:
                on_write(metric_key or descriptor.unique_key, False, existing.descriptor.value)
            return False

        current = self._states.get(raw_descriptor.unique_key)
        current_time = self._clock()

        if current is None:
            self._states[raw_descriptor.unique_key] = MetricState(
                raw_descriptor=raw_descriptor,
                descriptor=descriptor,
                device_id=self.device_id,
                device_name=self.device_name,
                last_updated=current_time,
                is_available=True,
            )
            if on_discovered is not None:
                on_discovered(metric_key or descriptor.unique_key)
            if on_write is not None:
                on_write(metric_key or descriptor.unique_key, True, descriptor.value)
            return True

        prior_available = current.is_available
        changed = current.value != descriptor.value or prior_available is False
        if changed:
            self._states[descriptor.unique_key] = MetricState(
                raw_descriptor=raw_descriptor,
                descriptor=descriptor,
                device_id=self.device_id,
                device_name=self.device_name,
                last_updated=current_time,
                is_available=True,
            )
            if on_write is not None:
                on_write(metric_key or descriptor.unique_key, True, descriptor.value)
            if prior_available is False:
                # Phase 8 (Silver, log-when-unavailable): an entity coming
                # back online is edge-triggered (the value/availability
                # actually changed, never every message), so this is exactly
                # one DEBUG line per recovery -- no log spam.
                _LOGGER.debug(
                    "Telegraf metric %s (device %s) is available again",
                    descriptor.unique_key,
                    self.device_name,
                )
            return True

        # No-op refresh: bring the metric back to life in lifecycle terms.
        current.raw_descriptor = raw_descriptor
        current.descriptor = descriptor
        current.last_updated = current_time
        current.device_id = self.device_id
        current.device_name = self.device_name
        current.cleanup_candidate_since = None
        return False

    def check_expiry(self, *, on_write: Callable[[str, bool, Any], None] | None = None) -> None:
        """Mark a metric unavailable when it has not been refreshed within expire_after seconds.

        Phase 6: on the Active->Unavailable transition, set
        ``cleanup_candidate_since = now`` so the cleanup pass has a stable
        timestamp to compare against ``cleanup_delay``. ``NEVER``-policy
        metrics (static system metadata) never enter the Cleanup Candidate
        state.
        """
        now = self._clock()
        for unique_key, state in list(self._states.items()):
            if now - state.last_updated <= self._expire_after:
                continue
            if state.descriptor.cleanup_policy == CLEANUP_POLICY_NEVER:
                # Static metadata is exempt from the cleanup lifecycle.
                continue
            if state.is_available:
                state.is_available = False
                state.cleanup_candidate_since = now
                if on_write is not None:
                    on_write(unique_key, False, state.descriptor.value)
                # Phase 8 (Silver, log-when-unavailable): the Active->Unavailable
                # transition is fired exactly once here, so this is one DEBUG
                # line per transition -- never once per expiry tick.
                _LOGGER.debug(
                    "Telegraf metric %s (device %s) went unavailable after no update for %ss",
                    unique_key,
                    state.device_name,
                    self._expire_after,
                )

    def cleanup(self, *, on_write: Callable[[str, bool, Any], None] | None = None) -> list[str]:
        """Remove Cleanup-Candidate metrics that have been stale for >= cleanup_delay.

        Phase 6 lifecycle: this pass only removes metrics that have actually
        been Cleanup Candidates for at least ``cleanup_delay`` seconds.
        ``NEVER``-policy metrics are skipped (they are never candidates);
        ``ALWAYS``-policy metrics are removed immediately on the first
        ``cleanup`` call (matching the pre-Phase-6 "always-cleanup" semantics
        for diagnostics/test fixtures).
        """
        now = self._clock()
        removed: list[str] = []
        for unique_key, state in list(self._states.items()):
            if state.descriptor.cleanup_policy == CLEANUP_POLICY_NEVER:
                continue
            if state.descriptor.cleanup_policy == CLEANUP_POLICY_ALWAYS:
                if on_write is not None:
                    on_write(unique_key, False, state.descriptor.value)
                removed.append(unique_key)
                self._states.pop(unique_key, None)
                continue
            if state.cleanup_candidate_since is not None and now - state.cleanup_candidate_since > self._cleanup_delay:
                if on_write is not None:
                    on_write(unique_key, False, state.descriptor.value)
                removed.append(unique_key)
                self._states.pop(unique_key, None)
        return removed

    def __len__(self) -> int:
        return len(self._states)

    def __iter__(self) -> Any:
        return iter(self.keys())


class DeviceManager:
    """Own one registry per discovered device and expose a single multi-device lookup surface."""

    def __init__(
        self,
        expire_after: int = 120,
        *,
        clock: Callable[[], float] | None = None,
        exclude_patterns: tuple[str, ...] = (),
        field_overrides: dict[str, dict[str, Any]] | None = None,
        device_id: str = "default",
        device_name: str = "Telegraf MQTT",
        cleanup_delay: int = 30 * 24 * 60 * 60,
        delete_delay: int = 60 * 24 * 60 * 60,
        enable_cleanup: bool = True,
        min_active_metrics: int = 1,
        parser: Any | None = None,
        category_overrides: dict[str, str | None] | None = None,
        device_id_strategy: str = DEFAULT_DEVICE_ID_STRATEGY,
    ) -> None:
        self._clock = clock or monotonic
        self._expire_after = expire_after
        self._exclude_patterns = exclude_patterns
        self._field_overrides = field_overrides or {}
        self._cleanup_delay = cleanup_delay
        self._delete_delay = delete_delay
        self._enable_cleanup = enable_cleanup
        self._min_active_metrics = max(0, int(min_active_metrics))
        self._default_device_id = device_id
        self._default_device_name = device_name
        self._category_overrides = category_overrides or {}
        self._device_id_strategy = (
            device_id_strategy if device_id_strategy in VALID_DEVICE_ID_STRATEGIES else DEFAULT_DEVICE_ID_STRATEGY
        )
        self.devices: dict[str, MetricRegistry] = {}
        self._parser = parser
        self._on_write: Callable[[str, bool, Any], None] | None = None
        self._on_discovered: Callable[[str], None] | None = None
        self._on_new_device: Callable[[str, str], None] | None = None
        # Phase 10: the snoop listener feeds ``record_seen_host`` for every
        # incoming message. The Repairs framework consults
        # ``seen_hosts`` and ``first_message_at`` to raise a hint when the
        # user's configured topic pattern matches no traffic.
        self._seen_hosts: set[str] = set()
        self._seen_topics: set[str] = set()
        self.first_message_at: float | None = None
        self.last_message_at: float | None = None
        # Phase 10: track the inverse mapping (host tag -> set of
        # device_id slugs the host produced) so the Repairs framework
        # can warn the user when two distinct host tags collapse onto
        # the same device_id slug. Common case: two Telegraf
        # containers with the same ``host=localhost`` default.
        self._host_to_device_id: dict[str, set[str]] = {}

    def set_parser(self, parser: Any) -> None:
        """Attach the payload parser used for every subsequent message."""
        self._parser = parser

    def set_callbacks(
        self,
        *,
        on_write: Callable[[str, bool, Any], None] | None = None,
        on_discovered: Callable[[str], None] | None = None,
        on_new_device: Callable[[str, str], None] | None = None,
    ) -> None:
        """Wire the persistent pipeline callbacks (MQTT transport → dispatcher signals).

        Called once by ``async_setup_entry`` so ``process_message(topic, payload)``
        works standalone; per-call arguments still take precedence.
        """
        if on_write is not None:
            self._on_write = on_write
        if on_discovered is not None:
            self._on_discovered = on_discovered
        if on_new_device is not None:
            self._on_new_device = on_new_device

    def get(self, metric_key: str) -> MetricState | None:
        """Resolve a metric key across all known device registries."""
        for registry in self.devices.values():
            state = registry.get(metric_key)
            if state is not None:
                return state
        return None

    def keys(self) -> tuple[str, ...]:
        """Return a stable composite key for each known metric across all devices."""
        keys: list[str] = []
        for device_id, registry in self.devices.items():
            for unique_key in registry:
                keys.append(f"{device_id}:{unique_key}")
        return tuple(keys)

    def get_metric(self, metric_key: str) -> MetricState | None:
        """Return the device-specific metric by composite key."""
        if ":" not in metric_key:
            return self.get(metric_key)
        device_id, unique_key = metric_key.split(":", 1)
        registry = self.devices.get(device_id)
        if registry is None:
            return None
        return registry.get(unique_key)

    def get_or_create_registry(self, device_id: str, device_name: str) -> MetricRegistry:
        """Lazily create a registry for a new device ID."""
        registry = self.devices.get(device_id)
        if registry is None:
            registry = MetricRegistry(
                expire_after=self._expire_after,
                clock=self._clock,
                exclude_patterns=self._exclude_patterns,
                field_overrides=self._field_overrides,
                device_id=device_id,
                device_name=device_name,
                cleanup_delay=self._cleanup_delay,
                delete_delay=self._delete_delay,
                category_overrides=self._category_overrides,
                device_id_strategy=self._device_id_strategy,
            )
            self.devices[device_id] = registry
        registry.device_name = device_name
        return registry

    def apply_options(
        self,
        *,
        expire_after: int | None = None,
        exclude_patterns: tuple[str, ...] | None = None,
        field_overrides: dict[str, dict[str, Any]] | None = None,
        enable_cleanup: bool | None = None,
        min_active_metrics: int | None = None,
        cleanup_delay: int | None = None,
        delete_delay: int | None = None,
        category_overrides: dict[str, str | None] | None = None,
        device_id_strategy: str | None = None,
        on_write: Callable[[str, bool, Any], None] | None = None,
    ) -> None:
        """Apply live options to every per-device registry.

        Phase 6: ``enable_cleanup`` and ``min_active_metrics`` are fan-out
        manager-level tunables (no per-registry equivalent). ``expire_after``,
        ``exclude_patterns``, ``field_overrides``, ``cleanup_delay`` and
        ``delete_delay`` are propagated to each per-device registry as well
        so existing registries pick up live value changes (and the stored
        manager-level values are what ``get_or_create_registry`` uses for
        any device discovered after the update).

        Phase 10: ``category_overrides`` and ``device_id_strategy`` are
        also stored at the manager level so devices discovered after the
        update use the new values, and fanned out to existing registries
        so live re-resolution picks up the change.
        """
        if expire_after is not None:
            self._expire_after = expire_after
        if exclude_patterns is not None:
            self._exclude_patterns = exclude_patterns
        if field_overrides is not None:
            self._field_overrides = field_overrides
        if enable_cleanup is not None:
            self._enable_cleanup = enable_cleanup
        if min_active_metrics is not None:
            self._min_active_metrics = max(0, int(min_active_metrics))
        if cleanup_delay is not None:
            self._cleanup_delay = max(0, int(cleanup_delay))
        if delete_delay is not None:
            self._delete_delay = max(0, int(delete_delay))
        if category_overrides is not None:
            self._category_overrides = category_overrides
        if device_id_strategy is not None and device_id_strategy in VALID_DEVICE_ID_STRATEGIES:
            self._device_id_strategy = device_id_strategy
        for device_id, registry in self.devices.items():
            registry.apply_options(
                expire_after=expire_after,
                exclude_patterns=exclude_patterns,
                field_overrides=field_overrides,
                cleanup_delay=self._cleanup_delay,
                delete_delay=self._delete_delay,
                category_overrides=category_overrides,
                device_id_strategy=device_id_strategy,
                on_write=(None if on_write is None else self._registry_on_write(device_id, on_write)),
            )

    def process_message(
        self,
        topic: str,
        payload: str | bytes,
        *,
        on_write: Callable[[str, bool, Any], None] | None = None,
        on_discovered: Callable[[str], None] | None = None,
        on_new_device: Callable[[str, str], None] | None = None,
        parser: Any | None = None,
    ) -> None:
        """Parse a message and route each descriptor to its own device's registry.

        A single MQTT message always carries one host's measurement in practice,
        but routing is per-descriptor so a mixed payload can never cross devices.
        """
        parser = parser if parser is not None else self._parser
        if parser is None:
            raise ValueError("parser is required")
        on_write = on_write if on_write is not None else self._on_write
        on_discovered = on_discovered if on_discovered is not None else self._on_discovered
        on_new_device = on_new_device if on_new_device is not None else self._on_new_device

        descriptors = parser.parse(payload)
        # Phase 10: track every incoming message for the snoop listener's
        # seen_hosts / seen_topics sets, even if the parser dropped the
        # payload. ``first_message_at`` / ``last_message_at`` feed the
        # Repairs framework's "no traffic on topic" hint.
        if descriptors:
            self.record_seen_host(
                descriptors[0].tags.get("host", ""),
                topic,
            )
        else:
            # Parser dropped the payload; still record the topic so the
            # snoop can tell the user "I see traffic on these topics,
            # but the payload shape wasn't what I expected."
            self.record_seen_host("", topic)
        groups: dict[str, list[MetricDescriptor]] = {}
        names: dict[str, str] = {}
        for descriptor in descriptors:
            device_id = self._derive_device_id(topic, descriptor)
            device_name = self._derive_device_name(topic, descriptor)
            resolved = dataclasses.replace(descriptor, device_id=device_id)
            groups.setdefault(device_id, []).append(resolved)
            names.setdefault(device_id, device_name)
            # Phase 10: track the (host tag -> device_id) mapping so the
            # Repairs framework can warn the user when two distinct host
            # tags collide on the same device_id slug (e.g. two Telegraf
            # containers that both publish ``host=localhost``).
            host_tag = descriptor.tags.get("host", "")
            if host_tag and device_id:
                self._host_to_device_id.setdefault(host_tag, set()).add(device_id)

        for device_id, group in groups.items():
            is_new = device_id not in self.devices
            registry = self.get_or_create_registry(device_id, names[device_id])
            registry.last_any_metric = self._clock()
            for descriptor in group:
                metric_key = f"{device_id}:{descriptor.unique_key}"
                registry.update(
                    descriptor,
                    metric_key=metric_key,
                    on_write=lambda key, available, value: self._notify_callback(key, available, value, on_write),
                    on_discovered=lambda key: self._notify_discovered_callback(key, on_discovered),
                )
            if is_new and on_new_device is not None:
                on_new_device(device_id, names[device_id])

    def record_seen_host(self, host: str, topic: str) -> None:
        """Record a host tag + topic the snoop listener saw.

        Called for every incoming MQTT message, not just messages that
        produced a device. This keeps ``seen_hosts`` accurate even when
        the user's configured topic pattern would have rejected the
        message (so the Repairs framework can tell them which topics
        are *actually* being published).
        """
        now = self._clock()
        if self.first_message_at is None:
            self.first_message_at = now
        self.last_message_at = now
        if host:
            self._seen_hosts.add(host)
        if topic:
            self._seen_topics.add(topic)

    @property
    def seen_hosts(self) -> frozenset[str]:
        """Return the immutable snapshot of hosts seen since setup."""
        return frozenset(self._seen_hosts)

    @property
    def seen_topics(self) -> frozenset[str]:
        """Return the immutable snapshot of MQTT topics seen since setup."""
        return frozenset(self._seen_topics)

    def has_received_messages(self) -> bool:
        """Return whether any MQTT message has reached the snoop listener."""
        return self.first_message_at is not None

    def find_device_id_collisions(self) -> dict[str, list[str]]:
        """Return device_id slugs produced by more than one distinct ``host`` tag.

        Result maps ``device_id -> sorted list of host tags`` for every
        slug the integration has seen at least two different host tags
        for. Used by ``repairs.check_device_id_collision`` to surface a
        hint when two Telegraf instances are being merged into one
        device because they share an indistinguishable host tag (e.g.
        two containers both running with ``host=localhost``).

        The result is computed on demand from ``_host_to_device_id``;
        the helper is O(seen_hosts) which is bounded by the broker
        activity, not by ``self.devices`` size.
        """
        device_to_hosts: dict[str, set[str]] = {}
        for host, device_ids in self._host_to_device_id.items():
            for device_id in device_ids:
                device_to_hosts.setdefault(device_id, set()).add(host)
        return {device_id: sorted(hosts) for device_id, hosts in device_to_hosts.items() if len(hosts) > 1}

    def pending_cleanup_all(self) -> dict[str, list[dict[str, Any]]]:
        """Aggregate ``pending_cleanup()`` across every device registry.

        Returns ``{device_id: [pending entries...]}`` so the diagnostics
        layer can show one section per host. Empty registries are
        omitted.
        """
        result: dict[str, list[dict[str, Any]]] = {}
        for device_id, registry in self.devices.items():
            entries = registry.pending_cleanup()
            if entries:
                result[device_id] = entries
        return result

    def _derive_device_id(self, topic: str, descriptor: MetricDescriptor) -> str:
        """Derive a stable device id honouring ``device_id_strategy``.

        - ``"host"`` (default, pre-10 behaviour): use the host tag, falling
          back to the first non-wildcard topic segment.
        - ``"host_topic"``: use the host tag when present, otherwise append
          the second-level topic segment to the first segment. Useful when
          a single Telegraf agent is misconfigured and every metric
          arrives with ``host=localhost`` but the topic tree still tells
          hosts apart.
        - ``"topic_only"``: always use the topic tree. Less stable across
          re-arranges; only useful when the broker-side topic structure
          is the authoritative device anchor.
        """
        host = descriptor.device_id or descriptor.tags.get("host") or ""
        topic_root, topic_sub = self._split_topic(topic)
        if self._device_id_strategy == "topic_only":
            return _slugify_device(topic_root) if topic_root else self._default_device_id
        if isinstance(host, str) and host:
            return _slugify_device(host)
        if self._device_id_strategy == "host_topic" and topic_sub:
            return _slugify_device(f"{topic_root}_{topic_sub}")
        return _slugify_device(topic_root) if topic_root else self._default_device_id

    def _derive_device_name(self, topic: str, descriptor: MetricDescriptor) -> str:
        """Derive a display name alongside the device id (same priority order)."""
        host = descriptor.device_id or descriptor.tags.get("host") or ""
        topic_root, _topic_sub = self._split_topic(topic)
        if isinstance(host, str) and host:
            return host
        return topic_root or self._default_device_name

    @staticmethod
    def _split_topic(topic: str) -> tuple[str, str]:
        """Return ``(first, second)`` non-wildcard topic segments, lowercased.

        MQTT wildcards (``+``, ``#``) are stripped before the split so the
        snoop listener (which subscribes to a wildcard) and the live
        subscription (which may also be wildcarded) both produce the same
        device-id seed for the same real host.
        """
        cleaned = [part for part in topic.strip("/").split("/") if part and part not in {"+", "#"}]
        if not cleaned:
            return "", ""
        if len(cleaned) == 1:
            return cleaned[0].lower(), ""
        return cleaned[0].lower(), cleaned[1].lower()

    def _registry_on_write(
        self,
        device_id: str,
        on_write: Callable[[str, bool, Any], None],
    ) -> Callable[[str, bool, Any], None]:
        """Wrap a user write callback so registry keys get the device prefix.

        Per-device registries emit bare unique keys; the manager-level
        callbacks (and therefore HA's dispatcher signals) are keyed by
        ``{device_id}:{unique_key}``. A named def -- not a lambda with a
        default-argument capture -- keeps the callable properly typed for
        the strict mypy gate.
        """

        def _on_write(key: str, available: bool, value: Any) -> None:
            self._notify_callback(f"{device_id}:{key}", available, value, on_write)

        return _on_write

    def _notify_callback(
        self,
        metric_key: str,
        available: bool,
        value: Any,
        callback: Callable[[str, bool, Any], None] | None,
    ) -> None:
        if callback is not None:
            callback(metric_key, available, value)

    def _notify_discovered_callback(self, metric_key: str, callback: Callable[[str], None] | None) -> None:
        if callback is not None:
            callback(metric_key)

    def check_expiry(self, *, on_write: Callable[[str, bool, Any], None] | None = None) -> None:
        """Run expiry checks across every device registry."""
        for device_id, registry in self.devices.items():
            registry.check_expiry(on_write=(None if on_write is None else self._registry_on_write(device_id, on_write)))

    def cleanup(self, *, on_write: Callable[[str, bool, Any], None] | None = None) -> list[str]:
        """Run cleanup across every ACTIVE device registry, returning composite keys.

        Phase 6 changes:
        - When ``enable_cleanup`` is False, this is a complete no-op: nothing
          is removed, no callbacks are fired. The manager keeps every metric
          in every device forever. (Useful for users who want a pure
          "discovery + expiry" integration with no deletion.)
        - The per-registry ``min_active_metrics`` guard skips a device when
          it has fewer than the threshold *available* metrics. The intent is
          to keep at least one entity per device alive even when most
          metrics have become Cleanup Candidates; an empty device is still
          a candidate for ``prune_empty_devices`` once the heartbeat
          expires.
        - Offline devices (no heartbeat within ``expire_after``) are skipped
          entirely -- their entities are never cleaned up, matching the
          pre-Phase-6 contract.
        """
        if not self._enable_cleanup:
            return []

        removed: list[str] = []
        now = self._clock()
        for device_id, registry in self.devices.items():
            if now - registry.last_any_metric > self._expire_after:
                continue
            available_count = sum(1 for state in registry._states.values() if state.is_available)
            if available_count < self._min_active_metrics:
                # Leave every entity in this registry alone: the device is
                # already near-empty, and pruning it to zero would let
                # prune_empty_devices pick it up on the same tick. The
                # user-facing effect: cleanup is a no-op for a device
                # that's already at the floor.
                #
                # Coverage note: the branch IS hit by
                # test_min_active_metrics_protects_devices_below_floor, but
                # coverage.py counts the multi-statement if/continue as one
                # statement, so the pragma stays.
                continue  # pragma: no cover
            for unique_key in registry.cleanup(
                on_write=(None if on_write is None else self._registry_on_write(device_id, on_write))
            ):
                removed.append(f"{device_id}:{unique_key}")
        return removed

    def prune_empty_devices(self) -> list[str]:
        """Drop devices that have been empty for >= ``delete_delay`` seconds.

        Phase 6 lifecycle: a device is removed when (a) it has zero metrics
        left and (b) its last heartbeat is older than ``delete_delay``. The
        device can always reappear later when a new message arrives --
        ``get_or_create_registry`` will create a fresh registry on demand.

        The heartbeat update inside ``process_message`` already prevents
        this method from pruning a device that's actively reporting empty
        payloads (which are very rare but possible). Devices with at least
        one metric are never pruned here, regardless of age.
        """
        now = self._clock()
        removed: list[str] = []
        for device_id, registry in list(self.devices.items()):
            if len(registry) > 0:
                continue
            if now - registry.last_any_metric <= self._delete_delay:
                continue
            removed.append(device_id)
            self.devices.pop(device_id, None)
            # Logging is a user-facing surface for stale-devices: the
            # operator should see when a host went away. The logger is
            # re-resolved through ``logging.getLogger`` so importing this
            # module at runtime can be done lazily in tests.
            import logging as _logging

            _logging.getLogger(__name__).info("Pruned empty Telegraf device %s", device_id)
        return removed

    def __len__(self) -> int:
        return sum(len(registry) for registry in self.devices.values())

    def __iter__(self) -> Any:
        return iter(self.keys())
