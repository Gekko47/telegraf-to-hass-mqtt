"""MQTT topic-filter semantics (MQTT 3.1.1 section 4.7).

Pure string logic -- deliberately no Home Assistant import, for the same
reason ``parser.py`` and ``registry.py`` are HA-free: this module is
exercised directly by unit tests and must never be able to drag an
integration dependency into the parse path.

Two questions are answered here, and only these two:

* ``mqtt_filter_matches`` -- would a broker deliver ``topic`` to a
  subscriber holding ``mqtt_filter``? Used by the auto-discover snoop to
  decide whether a message the *main* subscription already handles should
  be skipped rather than re-dispatched.
* ``mqtt_filter_covers`` -- is every topic matched by filter ``b`` also
  matched by filter ``a``? Used by the redundant-scope Repairs check to
  warn when an auto-discover scope can only ever re-see what the entry
  already receives.

**Scope note (deliberate non-change).** ``repairs._patterns_overlap`` is
NOT built on these functions and must not be. That helper is a
*conservative over-approximation* used only to raise a warning, so
answering "overlap" incorrectly in the permissive direction is harmless.
Substituting an exact matcher here would silently *reduce* the number of
warnings an imprecise helper emits, which is a behaviour regression on a
diagnostic, not a refactor. Leaving it alone is a correctness decision.
"""

from __future__ import annotations

_MULTI_LEVEL = "#"
_SINGLE_LEVEL = "+"
_SHARED_PREFIX = "$"
"""Topic root reserved for broker-internal topics (MQTT 3.1.1 section 4.7.2).

A filter whose *first* level is a wildcard never matches a ``$``-prefixed
topic, so ``#`` and ``+/x`` must not be treated as covering ``$SYS/...``.
"""


def mqtt_filter_matches(topic: str, mqtt_filter: str) -> bool:
    """Return whether a subscriber on ``mqtt_filter`` receives ``topic``.

    Implements MQTT 3.1.1 section 4.7 exactly:

    * ``#`` matches the parent level *and* any number of child levels, so
      ``sport/#`` matches ``sport`` as well as ``sport/tennis/player1``.
    * ``+`` matches exactly one level, **including an empty one**, so
      ``sport/+/player1`` matches ``sport//player1`` but not ``sport/player1``.
    * A filter starting with ``#`` or ``+`` never matches a ``$``-prefixed
      topic, so ``#`` does not match ``$SYS/broker/uptime``.

    An empty ``topic`` or ``mqtt_filter`` matches nothing. ``mqtt_filter``
    is assumed to be syntactically valid (the config flow rejects an
    invalid subscription pattern before it is ever persisted); a filter
    with an embedded wildcard such as ``sp+rt`` is therefore treated as a
    literal level, which is how a broker treats it too.
    """
    if not topic or not mqtt_filter:
        return False
    topic_parts = topic.split("/")
    filter_parts = mqtt_filter.split("/")
    if topic.startswith(_SHARED_PREFIX) and filter_parts[0] in (_MULTI_LEVEL, _SINGLE_LEVEL):
        return False
    index = 0
    for part in filter_parts:
        if part == _MULTI_LEVEL:
            # Consumes the rest of the topic, including nothing at all.
            return True
        if index >= len(topic_parts):
            # The filter still has levels but the topic ran out.
            return False
        if part != _SINGLE_LEVEL and part != topic_parts[index]:
            return False
        index += 1
    # A filter with no trailing ``#`` must consume the whole topic.
    return index == len(topic_parts)


def mqtt_filter_covers(outer: str, inner: str) -> bool:
    """Return whether every topic matched by ``inner`` is also matched by ``outer``.

    The relation is deliberately **sound, not complete**: it only returns
    ``True`` when coverage is provable from the filters alone, so a caller
    using it to *warn* never nags about a scope that actually does add
    something. Cases it cannot prove (an asymmetric single-level wildcard,
    e.g. ``a/+/c`` vs ``a/b/c``) report "not covered" rather than guessing.

    ``outer`` covers ``inner`` when, level by level, the two agree on every
    shared level and ``outer`` is at least as permissive from there on:
    a ``#`` in ``outer`` swallows the whole remainder, an exhausted
    ``inner`` is covered by an exhausted ``outer`` or a trailing ``#``,
    and an unbounded ``inner`` (``#``) requires an unbounded ``outer``.
    """
    if not outer or not inner:
        return False
    return _covers_from(outer.split("/"), 0, inner.split("/"), 0)


def _covers_from(
    outer: list[str],
    outer_index: int,
    inner: list[str],
    inner_index: int,
) -> bool:
    """Tail comparison backing :func:`mqtt_filter_covers`, one level at a time.

    Walks both filters in lockstep and descends a level only when the two
    agree on the current one, so the cost is O(levels) with no list copies
    and no recursion depth tied to a topic tree's height.
    """
    while True:
        if inner_index == len(inner):
            # ``inner`` is fully consumed; ``outer`` must be too, or end in
            # a ``#`` that covers the level ``inner`` stops at.
            return outer_index == len(outer) or outer[outer_index] == _MULTI_LEVEL
        if outer_index == len(outer):
            # ``outer`` ran out first, so it cannot cover the rest of ``inner``.
            return False
        outer_part = outer[outer_index]
        inner_part = inner[inner_index]
        if outer_part == _MULTI_LEVEL:
            # ``outer`` swallows everything below this level. A ``$``-prefixed
            # ``inner`` is still covered: ``$`` topics are only excluded from
            # *wildcard-first* filters, and ``outer`` matched a literal level
            # before reaching its ``#``.
            return True
        if inner_part == _MULTI_LEVEL:
            # ``inner`` is unbounded here, so only an unbounded ``outer``
            # can contain it.
            return outer_part == _MULTI_LEVEL
        if outer_part == _SINGLE_LEVEL:
            # ``+`` matches exactly one COMPLETE level, whatever it is, so
            # an outer ``+`` covers an inner literal at this level. Both
            # sides advance -- the levels below still have to line up,
            # otherwise ``telegraf/+/mem`` would be reported as covering
            # ``telegraf/rack1/cpu``.
            outer_index += 1
            inner_index += 1
            continue
        if inner_part == _SINGLE_LEVEL:
            # The mirror image: an inner ``+`` matches any value at this
            # level, and a literal ``outer`` only ever covers one of them,
            # so coverage is not provable from the filters alone.
            return False
        if outer_part != inner_part:
            return False
        outer_index += 1
        inner_index += 1
