"""Repairs (Issue Registry) integration for telegraf_mqtt.

Phase 7 of the ROADMAP addresses two recoverable config problems that
SPEC.md says should be raised as Repairs issues rather than just
logged:

  1. Overlapping topic patterns across two config entries
     (e.g. ``telegraf/#`` and ``telegraf/+/cpu``).
  2. Invalid values persisted to disk in the options dict
     (e.g. ``expire_after="abc"``).

Both checks are idempotent: every call deletes any prior issue with
the same ``issue_id`` and re-creates one if the problem is still
present. HA's ``ir.async_create_issue`` is itself idempotent by
``issue_id``; calling it twice with the same body is a no-op.

These helpers run during ``async_setup_entry`` so the Repairs UI is
up-to-date by the time the user opens it. They are not raised during
``async_setup_entry``'s *options-validation* phase because HA would
abort the setup before the issue is recorded; we record them at the
end of setup with the manager already wired.
"""

from __future__ import annotations

from collections.abc import Iterable
from typing import Any

from .const import (
    CONF_AUTO_DISCOVER,
    CONF_AUTO_DISCOVER_SCOPE,
    CONF_CLEANUP_DELAY,
    CONF_DELETE_DELAY,
    CONF_ENABLE_CLEANUP,
    CONF_EXPIRE_AFTER,
    CONF_MIN_ACTIVE_METRICS,
    CONF_TOPIC_PATTERN,
    DEFAULT_CLEANUP_DELAY,
    DEFAULT_DELETE_DELAY,
    DEFAULT_ENABLE_CLEANUP,
    DEFAULT_EXPIRE_AFTER,
    DEFAULT_MIN_ACTIVE_METRICS,
    DOMAIN,
    NO_TRAFFIC_GRACE_SECONDS,
    REPAIR_AUTO_DISCOVER_SCOPE,
    REPAIR_DEVICE_CAP,
    REPAIR_DEVICE_ID_COLLISION,
    REPAIR_DEVICE_ID_CONFLICT,
    REPAIR_INVALID_OPTION,
    REPAIR_METRIC_CAP,
    REPAIR_NO_TRAFFIC,
    REPAIR_OVERLAP,
)
from .topics import mqtt_filter_covers


# Issue IDs are scoped to the *current* entry: no two entries can
# collide on (or delete) each other's issues. Overlap issues are
# additionally anchored to the *other* entry that the current one
# conflicts with, so changing the conflicting entry's pattern
# auto-clears the prior issue on the next setup call (HA's
# ``ir.async_create_issue`` is idempotent, but we explicitly
# ``async_delete_issue`` when the overlap is fixed).
#
# The TRANSLATION keys come from ``const.REPAIR_*`` rather than inline
# literals. They used to be inlined here while the same names sat unused in
# ``const.py``, which is exactly why three of them (``no_traffic_on_topic``,
# ``device_id_collision``, ``device_id_conflict``) could ship as Repairs
# issues with no matching entry in ``strings.json`` -- the user saw a
# broken issue in Settings and nothing in CI noticed. Importing the
# constants makes a rename a single-site edit and lets a completeness test
# cross-check the keys against the translation file.
def _overlap_issue_id(entry_id: str, other_entry_id: str) -> str:
    return f"{REPAIR_OVERLAP}_{entry_id}_{other_entry_id}"


def _invalid_option_issue_id(entry_id: str) -> str:
    return f"{REPAIR_INVALID_OPTION}_{entry_id}"


def _no_traffic_issue_id(entry_id: str) -> str:
    return f"{REPAIR_NO_TRAFFIC}_{entry_id}"


def _device_id_collision_issue_id(entry_id: str) -> str:
    return f"{REPAIR_DEVICE_ID_COLLISION}_{entry_id}"


def _device_id_conflict_issue_id(entry_id: str) -> str:
    return f"{REPAIR_DEVICE_ID_CONFLICT}_{entry_id}"


def _auto_discover_scope_issue_id(entry_id: str) -> str:
    return f"{REPAIR_AUTO_DISCOVER_SCOPE}_{entry_id}"


def _device_cap_issue_id(entry_id: str) -> str:
    return f"{REPAIR_DEVICE_CAP}_{entry_id}"


def _metric_cap_issue_id(entry_id: str) -> str:
    return f"{REPAIR_METRIC_CAP}_{entry_id}"


# Conservative MQTT topic overlap check. Two patterns overlap when
# one is a strict prefix of the other up to a ``/`` boundary AND the
# shorter pattern ends in ``#`` or a literal segment. Identical
# patterns are caught earlier by HA's ``async_set_unique_id`` and
# ``_abort_if_unique_id_configured`` so we don't double-report them.
def _patterns_overlap(a: str, b: str) -> bool:
    """Conservative MQTT topic overlap check (per MQTT 3.1.1 §4.7).

    Two patterns overlap when they share at least one real topic
    in their respective match sets. The algorithm walks both
    patterns segment-by-segment:

      - Exact equality → overlap.
      - MQTT §4.7 ``$``-topic rule: a subscription whose first
        level is a wildcard (``#`` or ``+``) does not match topics
        that start with ``$``. If one filter is a leading-wildcard
        filter and the other starts with ``$``, the two filter
        sets are disjoint → no overlap.
      - Either pattern is a bare ``#`` (and the other does not
        start with ``$``) → matches everything → overlap.
      - Parent-level match: a trailing ``/#`` is treated as also
        matching its parent topic, so ``a/b`` and ``a/b/#`` overlap.
      - ``#`` in either pattern at position ``i`` with the other
        pattern having a non-empty remaining tail → overlap (the
        ``#`` swallows the rest).
      - At every position ``i``, the segments are equal OR at least
        one is ``+`` (single-segment wildcard) → overlap.
      - Otherwise → disjoint.

    Composed of single-responsibility helpers, one per rule, so the
    top-level function reads as the algorithm outline.
    """
    if a == b:
        return True
    a_parts = a.split("/")
    b_parts = b.split("/")
    if _is_dollar_topic_disjoint(a_parts, b_parts):
        return False
    # Bare ``#`` matches every topic *except* the ``$``-prefixed
    # ones, which the ``$``-disjoint check above already excluded
    # on the ``$`` side. So a bare ``#`` here implies overlap.
    if a_parts == ["#"] or b_parts == ["#"]:
        return True
    if _is_parent_topic_overlap(a, b):
        return True
    return _segments_overlap(a_parts, b_parts)


def _is_leading_wildcard(parts: list[str]) -> bool:
    return bool(parts) and parts[0] in ("#", "+")


def _is_dollar_prefixed(parts: list[str]) -> bool:
    return bool(parts) and parts[0].startswith("$")


def _is_dollar_topic_disjoint(a_parts: list[str], b_parts: list[str]) -> bool:
    """MQTT 3.1.1 §4.7 ``$``-topic rule.

    A leading-wildcard filter (first segment ``#`` or ``+``) does
    not match topics whose first level begins with ``$``, so a
    leading-wildcard filter can never overlap a ``$``-prefixed
    filter.
    """
    return (_is_dollar_prefixed(a_parts) and _is_leading_wildcard(b_parts)) or (
        _is_dollar_prefixed(b_parts) and _is_leading_wildcard(a_parts)
    )


def _is_parent_topic_overlap(a: str, b: str) -> bool:
    """A trailing ``/#`` is treated as also matching its parent topic.

    ``a/b`` and ``a/b/#`` share the parent hierarchy even though
    the literal ``a/b`` is not strictly in the match set of
    ``a/b/#`` per MQTT 3.1.1.
    """
    return a + "/#" == b or b + "/#" == a


def _segments_overlap(a_parts: list[str], b_parts: list[str]) -> bool:
    """Walk the segments, allowing ``#`` to terminate one early.

    The walk allows ``#`` to terminate one of the patterns early
    as long as the other has more segments. Same length with every
    segment compatible → overlap; different lengths with no ``#``
    encountered → disjoint.
    """
    for i, (seg_a, seg_b) in enumerate(zip(a_parts, b_parts, strict=False)):
        if seg_a == "#":
            # ``#`` matches the rest of b only if b has anything beyond.
            return i < len(b_parts) - 1 or b_parts[i:] != [seg_a]
        if seg_b == "#":
            return i < len(a_parts) - 1 or a_parts[i:] != [seg_b]
        if seg_a == "+" or seg_b == "+":
            continue
        if seg_a != seg_b:
            return False
    return len(a_parts) == len(b_parts)


def check_overlapping_topics(hass: Any, entry: Any) -> list[str]:
    """Raise (or auto-resolve) Repairs issues for topic-pattern overlaps.

    Idempotent: deletes any prior overlap_issue for the same
    (entry, other_entry) pair and recreates one if the overlap is
    still present. Returns the list of ``other_entry_id``s that
    currently overlap with this entry (useful for tests).
    """
    ir = _ir(hass)
    if ir is None:
        return []
    own = entry.data.get(CONF_TOPIC_PATTERN) if hasattr(entry, "data") else None
    if not own:
        return []
    overlapping: list[str] = []
    others = list(hass.config_entries.async_entries(DOMAIN))
    for other in others:
        if other.entry_id == entry.entry_id:
            continue
        other_pattern = other.data.get(CONF_TOPIC_PATTERN) if hasattr(other, "data") and other.data else None
        if not other_pattern:
            continue
        if not _patterns_overlap(own, other_pattern):
            continue
        overlapping.append(other.entry_id)
        ir.async_create_issue(
            hass,
            domain=DOMAIN,
            issue_id=_overlap_issue_id(entry.entry_id, other.entry_id),
            is_fixable=False,
            severity=ir.IssueSeverity.WARNING,
            translation_key=REPAIR_OVERLAP,
            translation_placeholders={
                "own_topic": own,
                "other_topic": other_pattern,
                "other_entry_title": getattr(other, "title", ""),
            },
        )
    # Auto-resolve: any overlap_issue for an entry_id that is no
    # longer overlapping must be deleted.
    for other in others:
        if other.entry_id == entry.entry_id:
            continue
        if other.entry_id not in overlapping:
            ir.async_delete_issue(
                hass,
                domain=DOMAIN,
                issue_id=_overlap_issue_id(entry.entry_id, other.entry_id),
            )
    return overlapping


def check_invalid_persisted_option(hass: Any, entry: Any, invalid_keys: Iterable[str]) -> None:
    """Raise (or auto-resolve) a Repairs issue for invalid options.

    Called from ``_options_from_entry_with_repair``. If
    ``invalid_keys`` is empty, the prior issue is deleted; otherwise a
    single issue lists every invalid key.
    """
    ir = _ir(hass)
    if ir is None:
        return
    invalid_list = sorted(set(invalid_keys))
    if not invalid_list:
        ir.async_delete_issue(
            hass,
            domain=DOMAIN,
            issue_id=_invalid_option_issue_id(entry.entry_id),
        )
        return
    placeholders = {
        "options": ", ".join(invalid_list),
        "defaults": _describe_defaults(invalid_list),
    }
    ir.async_create_issue(
        hass,
        domain=DOMAIN,
        issue_id=_invalid_option_issue_id(entry.entry_id),
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=REPAIR_INVALID_OPTION,
        translation_placeholders=placeholders,
    )


def check_no_traffic(hass: Any, entry: Any) -> None:
    """Raise (or auto-resolve) a Repairs issue for "no traffic on topic".

    Called from the periodic ``check_expiry`` callback scheduled by
    ``integration._schedule_expiry_check``.

    Fires only after SUSTAINED silence (``NO_TRAFFIC_GRACE_SECONDS``),
    measured from the last message -- or from entry startup, if nothing
    has ever arrived. It previously fired on "no message has EVER
    arrived", which any host publishing more slowly than ``expire_after``
    tripped, and because the issue also cleared on the next message the
    card flapped: raised, auto-resolved, raised again, every cycle.
    A Repairs panel that flickers trains users to ignore it.
    """
    ir = _ir(hass)
    if ir is None:
        return
    runtime_data = getattr(entry, "runtime_data", None)
    if runtime_data is None:
        return
    manager = getattr(runtime_data, "manager", None)
    if manager is None:
        return
    # M2: require SUSTAINED silence, not "never received anything".
    #
    # The old condition was ``has_received_messages()`` -- one message in
    # the entry's entire lifetime satisfied it, and it was checked on every
    # expiry tick. Any host publishing more slowly than ``expire_after``
    # (120s by default) therefore saw the issue created, auto-resolved on
    # its next message, and created again: a flapping Repairs card that
    # trains users to ignore the Repairs panel.
    #
    # Silence is judged against the grace window instead, and the two
    # "never any" vs "went quiet" cases are distinguished so a brand-new
    # entry is not warned before its first publish interval has elapsed.
    # Nothing having EVER arrived measures against the entry's own startup
    # (so a freshly-added entry is not warned before Telegraf's first
    # publish interval has had a chance to elapse); a host that went
    # quiet measures against its last message, so only sustained silence
    # counts.
    since_last = manager.seconds_since_last_message()
    silence = manager.seconds_since_startup() if since_last is None else since_last

    if silence < NO_TRAFFIC_GRACE_SECONDS:
        ir.async_delete_issue(
            hass,
            domain=DOMAIN,
            issue_id=_no_traffic_issue_id(entry.entry_id),
        )
        return
    # Compose a short, scannable description of the empty state.
    seen_topics_preview = ", ".join(sorted(manager.seen_topics)[:5])
    if not seen_topics_preview:
        seen_topics_preview = "(none)"
    ir.async_create_issue(
        hass,
        domain=DOMAIN,
        issue_id=_no_traffic_issue_id(entry.entry_id),
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=REPAIR_NO_TRAFFIC,
        translation_placeholders={
            "configured_topic": entry.data.get(CONF_TOPIC_PATTERN, ""),
            "seen_topics": seen_topics_preview,
        },
    )


def check_device_id_collision(hass: Any, entry: Any) -> None:
    """Raise (or auto-resolve) a Repairs issue for device-id collisions.

    A "collision" is two distinct ``host`` tags producing the same
    device_id slug (e.g. ``My Server`` and ``my_server`` after
    slug normalization). The integration merges them silently, which
    is correct behaviour but confusing for the user -- this Repairs
    issue tells them which hosts are colliding and what to do about
    it (set a unique ``agent_hostname`` per Telegraf instance).
    """
    ir = _ir(hass)
    if ir is None:
        return
    runtime_data = getattr(entry, "runtime_data", None)
    if runtime_data is None:
        return
    manager = getattr(runtime_data, "manager", None)
    if manager is None:
        return
    collisions = manager.find_device_id_collisions()
    if not collisions:
        ir.async_delete_issue(
            hass,
            domain=DOMAIN,
            issue_id=_device_id_collision_issue_id(entry.entry_id),
        )
        return
    # Build a scannable description of the collisions: one line per
    # device_id slug with the host tags that produced it.
    parts = [f"{device_id} \u2190 {', '.join(sorted(hosts))}" for device_id, hosts in sorted(collisions.items())]
    description = "; ".join(parts)
    ir.async_create_issue(
        hass,
        domain=DOMAIN,
        issue_id=_device_id_collision_issue_id(entry.entry_id),
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=REPAIR_DEVICE_ID_COLLISION,
        translation_placeholders={"description": description},
    )


def check_device_id_conflict(hass: Any, entry: Any) -> None:
    """Raise (or auto-resolve) a Repairs issue for cross-entry device-id conflicts.

    A "conflict" is the same device_id slug produced by two different
    config entries (typically with different topic patterns). The
    integration will create the device twice but HA's device registry
    will merge them on first write, which is the wrong outcome. This
    Repairs issue lists the conflicting entries so the user can
    adjust one of the topic patterns.
    """
    ir = _ir(hass)
    if ir is None:
        return
    runtime_data = getattr(entry, "runtime_data", None)
    if runtime_data is None:
        return
    manager = getattr(runtime_data, "manager", None)
    if manager is None:
        return
    own_device_ids = set(manager.devices)
    if not own_device_ids:
        ir.async_delete_issue(
            hass,
            domain=DOMAIN,
            issue_id=_device_id_conflict_issue_id(entry.entry_id),
        )
        return
    conflicting_entries: list[str] = []
    for other in hass.config_entries.async_entries(DOMAIN):
        if other.entry_id == entry.entry_id:
            continue
        other_data = getattr(other, "data", None) or {}
        other_pattern = other_data.get(CONF_TOPIC_PATTERN)
        other_manager = getattr(getattr(other, "runtime_data", None), "manager", None)
        if other_manager is None or not other_pattern:
            continue
        overlap = own_device_ids & set(other_manager.devices)
        if overlap:
            conflicting_entries.append(f"{other.title or other.entry_id} ({other_pattern})")
    if not conflicting_entries:
        ir.async_delete_issue(
            hass,
            domain=DOMAIN,
            issue_id=_device_id_conflict_issue_id(entry.entry_id),
        )
        return
    ir.async_create_issue(
        hass,
        domain=DOMAIN,
        issue_id=_device_id_conflict_issue_id(entry.entry_id),
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=REPAIR_DEVICE_ID_CONFLICT,
        translation_placeholders={
            "conflicts": "; ".join(conflicting_entries),
        },
    )


def check_auto_discover_scope(hass: Any, entry: Any) -> None:
    """Raise (or auto-resolve) a Repairs issue for a redundant scope.

    The user has enabled auto-discover but set ``auto_discover_scope`` to
    something the entry's own ``topic_pattern`` already covers
    entirely. Every message the snoop receives is then one the snoop's
    ``exclude_filter`` skips, so the subscription buys nothing and the
    user is paying a second broker subscription for it.

    The check is sound in the direction that matters: ``mqtt_filter_covers``
    only returns True when coverage is provable from the two filters, so
    a scope that genuinely adds a host will never be flagged. It is a
    WARNING, not an error -- a redundant scope is a configuration smell,
    not a broken entry, and auto-discover still costs the user nothing
    beyond the extra SUBSCRIBE.

    Called from ``_apply_auto_discover`` so it re-evaluates on setup, on a
    live options toggle, and after a reconfigure-induced reload.
    """
    ir = _ir(hass)
    if ir is None:
        return
    issue_id = _auto_discover_scope_issue_id(entry.entry_id)
    options = getattr(entry, "options", None) or {}
    scope = options.get(CONF_AUTO_DISCOVER_SCOPE)
    pattern = (getattr(entry, "data", None) or {}).get(CONF_TOPIC_PATTERN)
    if not options.get(CONF_AUTO_DISCOVER) or not isinstance(scope, str) or not isinstance(pattern, str):
        ir.async_delete_issue(hass, domain=DOMAIN, issue_id=issue_id)
        return
    if not mqtt_filter_covers(pattern, scope):
        ir.async_delete_issue(hass, domain=DOMAIN, issue_id=issue_id)
        return
    ir.async_create_issue(
        hass,
        domain=DOMAIN,
        issue_id=issue_id,
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=REPAIR_AUTO_DISCOVER_SCOPE,
        translation_placeholders={"configured_topic": pattern, "auto_discover_scope": scope},
    )


def _describe_defaults(invalid_keys: list[str]) -> str:
    defaults = {
        CONF_EXPIRE_AFTER: DEFAULT_EXPIRE_AFTER,
        CONF_CLEANUP_DELAY: DEFAULT_CLEANUP_DELAY,
        CONF_DELETE_DELAY: DEFAULT_DELETE_DELAY,
        CONF_MIN_ACTIVE_METRICS: DEFAULT_MIN_ACTIVE_METRICS,
        CONF_ENABLE_CLEANUP: DEFAULT_ENABLE_CLEANUP,
    }
    parts = [f"{k}={defaults[k]}" for k in invalid_keys if k in defaults]
    return "; ".join(parts) if parts else "(see options flow)"


def check_device_cap(hass: Any, entry: Any) -> None:
    """Raise (or auto-resolve) a Repairs issue for the device cap.

    When ``manager.dropped_device_count > 0``, the integration has
    dropped measurements from new Telegraf hosts because the entry's
    ``max_devices`` cap was reached. The issue lists the cap and the
    number of dropped measurements so the user can either widen the
    topic pattern (to consolidate hosts) or add another config entry
    (to split the load). The issue auto-resolves when the count drops
    back to zero (e.g. after a reload).
    """
    ir = _ir(hass)
    if ir is None:
        return
    runtime_data = getattr(entry, "runtime_data", None)
    if runtime_data is None:
        return
    manager = getattr(runtime_data, "manager", None)
    if manager is None:
        return
    dropped = getattr(manager, "dropped_device_count", 0)
    if dropped == 0:
        ir.async_delete_issue(
            hass,
            domain=DOMAIN,
            issue_id=_device_cap_issue_id(entry.entry_id),
        )
        return
    ir.async_create_issue(
        hass,
        domain=DOMAIN,
        issue_id=_device_cap_issue_id(entry.entry_id),
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=REPAIR_DEVICE_CAP,
        translation_placeholders={
            "dropped": str(dropped),
            "max_devices": str(getattr(manager, "_max_devices", 0)),
            "configured_topic": entry.data.get(CONF_TOPIC_PATTERN, ""),
        },
    )


def check_metric_cap(hass: Any, entry: Any) -> None:
    """Raise (or auto-resolve) a Repairs issue for the per-device metric cap.

    When ``manager.dropped_metric_count > 0``, the integration has
    dropped new metrics from existing devices because a device's
    ``max_metrics_per_device`` cap was reached. The issue lists the
    cap and the number of dropped measurements. The issue auto-resolves
    when the count drops back to zero.
    """
    ir = _ir(hass)
    if ir is None:
        return
    runtime_data = getattr(entry, "runtime_data", None)
    if runtime_data is None:
        return
    manager = getattr(runtime_data, "manager", None)
    if manager is None:
        return
    dropped = getattr(manager, "dropped_metric_count", 0)
    if dropped == 0:
        ir.async_delete_issue(
            hass,
            domain=DOMAIN,
            issue_id=_metric_cap_issue_id(entry.entry_id),
        )
        return
    ir.async_create_issue(
        hass,
        domain=DOMAIN,
        issue_id=_metric_cap_issue_id(entry.entry_id),
        is_fixable=False,
        severity=ir.IssueSeverity.WARNING,
        translation_key=REPAIR_METRIC_CAP,
        translation_placeholders={
            "dropped": str(dropped),
            "max_metrics_per_device": str(getattr(manager, "_max_metrics_per_device", 0)),
            "configured_topic": entry.data.get(CONF_TOPIC_PATTERN, ""),
        },
    )


# The ``ir`` import on the integration module is itself guarded so the
# whole integration is importable under ``importlib.util`` (no HA).
# We re-resolve it from the integration module so a single
# monkeypatch of ``integration.ir`` propagates here.
def _ir(hass: Any) -> Any:
    import importlib

    integration = importlib.import_module("custom_components.telegraf_mqtt")
    return getattr(integration, "ir", None)
