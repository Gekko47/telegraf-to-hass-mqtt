"""Post-setup snoop listener for telegraf_mqtt (Phase 10).

Two operating modes are supported:

* **record-only** (no ``dispatcher``): the listener records every host
  tag and topic the broker carries during its window. Used by the
  "no traffic on topic" Repairs check.

* **dispatch** (``dispatcher`` callable): the listener additionally
  re-injects each captured message into the integration's existing
  parse -> route -> render pipeline. New Telegraf hosts the user's
  configured ``topic_pattern`` missed are auto-added as devices and
  entities without the user having to add another config entry.
"""

from __future__ import annotations

import logging
import re
from collections.abc import Callable
from dataclasses import dataclass
from time import monotonic
from typing import Any

from .const import DEFAULT_AUTO_DISCOVER_PROBE_TOPIC

_LOGGER = logging.getLogger(__name__)

# Signature matches ``DeviceManager.process_message`` exactly so the
# snoop can hand each captured MQTT message straight back into the
# integration's primary pipeline. The two positional args are
# ``(topic, payload)``; ``message`` is the broker-side ``MQTTMessage``
# and the dispatcher is responsible for reading ``.topic`` and
# ``.payload`` off it.
SnoopDispatcher = Callable[[str, Any], None]


def derive_probe_topic(topic_pattern: str) -> str:
    """Return a probe topic that never widens past ``topic_pattern``.

    The post-setup snoop must never silently widen past the user's
    configured subscription, so this function is a no-op on the
    pattern itself: whatever the user configured is what the snoop
    subscribes to. The single thing it does is fall back to the
    documented default ``telegraf/#`` when the input is empty or
    whitespace -- a corrupted entry shouldn't get a probe that
    subscribes to literally everything on the broker.
    """
    cleaned = (topic_pattern or "").strip()
    if not cleaned:
        return DEFAULT_AUTO_DISCOVER_PROBE_TOPIC
    return cleaned


@dataclass
class SnoopResult:
    """Snapshot returned by the snoop listener when its timer expires or
    ``stop()`` is called explicitly."""

    hosts: frozenset[str]
    topics: frozenset[str]
    duration_seconds: float
    dispatched_count: int = 0
    dispatcher_errors: int = 0
    """Captured messages where the dispatcher raised.

    The snoop subscriber catches any exception raised by the
    dispatcher (it cannot let a bad payload break the broker-side
    subscription), but the user still wants to see *that* it
    happened -- otherwise an integration that's silently broken on
    the auto-discover path is indistinguishable from one that's
    working. Pair with ``dispatched_count`` to gauge ratio.
    """


class SnoopListener:
    """An MQTT subscription that records -- and optionally dispatches --
    what the broker carries.

    ``timeout_seconds=0.0`` makes the listener long-lived; the caller
    is responsible for invoking ``stop()`` (the integration stores the
    unsubscribe handle on the runtime data and tears it down at entry
    unload). Any positive value schedules an auto-stop timer.
    """

    def __init__(
        self,
        *,
        probe_topic: str,
        timeout_seconds: float,
        clock: Callable[[], float] | None = None,
        dispatcher: SnoopDispatcher | None = None,
    ) -> None:
        self._probe_topic = probe_topic
        self._timeout = max(0.0, float(timeout_seconds))
        self._clock = clock or monotonic
        self._dispatcher = dispatcher
        self._seen_hosts: set[str] = set()
        self._seen_topics: set[str] = set()
        self._dispatched_count: int = 0
        self._dispatcher_errors: int = 0
        self._started_at: float | None = None
        self._unsubscribe: Callable[[], None] | None = None
        # ``_cancel_timer`` releases the ``loop.call_later`` handle so the
        # timer doesn't fire after ``stop()`` has already torn the listener
        # down (or after the entry has been unloaded). Only set when
        # ``start()`` actually schedules a timer (``self._timeout > 0`` and
        # we have a usable event loop).
        self._cancel_timer: Callable[[], None] | None = None
        self._finished: bool = False

    @property
    def is_finished(self) -> bool:
        return self._finished

    @property
    def dispatched_count(self) -> int:
        """Number of captured messages re-injected into the dispatcher.

        Always 0 in record-only mode.
        """
        return self._dispatched_count

    async def start(self, hass: Any, subscribe: Callable[..., Any]) -> None:
        """Install the snoop subscription on the broker.

        ``subscribe`` is ``mqtt.async_subscribe`` -- the listener
        captures whatever it returns (an unsubscribe callable) for
        later use by ``stop()``. When ``timeout_seconds > 0`` a
        ``loop.call_later`` timer is armed so the listener stops
        itself automatically after the configured window (the
        diagnostics probe path that relies on the 10-second default);
        ``timeout_seconds == 0`` keeps the listener long-lived and
        skips the timer entirely (the integration runtime path).
        """
        self._started_at = self._clock()
        self._unsubscribe = await subscribe(
            hass,
            self._probe_topic,
            self._on_message,
        )
        # Schedule the auto-stop timer only when a timeout is configured
        # *and* the caller handed us something that exposes an asyncio
        # event loop. Tests that pass ``hass=None`` or ``timeout=0``
        # bypass the timer entirely, which keeps the long-lived
        # runtime path and the harness doubles both unchanged.
        loop = getattr(hass, "loop", None) if hass is not None else None
        if self._timeout > 0.0 and loop is not None and hasattr(loop, "call_later"):
            self._cancel_timer = loop.call_later(self._timeout, self._on_timeout)

    async def _on_message(self, message: Any) -> None:
        """Record one inbound MQTT message's host tag + topic, and optionally
        hand the message to the dispatcher so the primary pipeline can
        create entities for newly-seen Telegraf hosts."""
        topic = getattr(message, "topic", None)
        if isinstance(topic, str):
            self._seen_topics.add(topic)
        # The host tag is inside the JSON payload; we parse it here
        # rather than going through the full TelegrafParser because
        # we want to record even messages with an unexpected shape.
        payload = getattr(message, "payload", None)
        if isinstance(payload, (str, bytes, bytearray)):
            host = _extract_host(payload)
            if host:
                self._seen_hosts.add(host)
        if self._dispatcher is not None and isinstance(topic, str):
            try:
                self._dispatcher(topic, payload)
            except Exception as dispatch_err:
                # A dispatcher failure must not stop subsequent messages
                # from being processed. Log at debug; bump the
                # ``dispatcher_errors`` counter so the snoop result
                # tells the user the dispatcher is sick, not silent.
                _LOGGER.debug("Snoop dispatcher raised for %s: %s", topic, dispatch_err)
                self._dispatcher_errors += 1
            else:
                self._dispatched_count += 1

    def stop(self) -> SnoopResult:
        """Cancel the subscription and return a snapshot of what was seen."""
        if self._cancel_timer is not None:
            # Cancel the auto-stop timer so it can't fire after we've
            # already torn the listener down (e.g. on entry unload).
            # The raw ``asyncio`` loop returns a ``TimerHandle`` whose
            # ``.cancel()`` method does the job, while Home Assistant's
            # higher-level helpers (and our tests' doubles) hand back a
            # plain callable -- handle both.
            cancel = self._cancel_timer
            if hasattr(cancel, "cancel") and callable(cancel.cancel):
                cancel.cancel()
            else:
                cancel()
            self._cancel_timer = None
        if self._unsubscribe is not None:
            self._unsubscribe()
            self._unsubscribe = None
        self._finished = True
        duration = 0.0
        if self._started_at is not None:
            duration = max(0.0, self._clock() - self._started_at)
        return SnoopResult(
            hosts=frozenset(self._seen_hosts),
            topics=frozenset(self._seen_topics),
            duration_seconds=duration,
            dispatched_count=self._dispatched_count,
            dispatcher_errors=self._dispatcher_errors,
        )

    def _on_timeout(self) -> None:
        """Timer callback invoked by the loop after ``self._timeout`` seconds.

        Stops the listener so the caller (or the diagnostics Repairs
        flow) sees a finished snapshot. ``stop()`` is idempotent, so a
        subsequent unload-time ``stop()`` is a safe no-op even though
        the runtime already parked the bound ``stop`` method on
        ``runtime_data.unsubscribe_snoop``.
        """
        if not self._finished:
            self.stop()


def _extract_host(payload: str | bytes | bytearray) -> str:
    r"""Extract the ``host`` tag from a JSON payload without a full parse.

    The snoop listener is invoked on every MQTT message, including
    ones the parser would reject. We want to record the host even
    when the payload is malformed so a corrupted payload doesn't
    raise and break the snoop.

    The pattern is anchored to a *key*: ``"host"\s*:``. Searching
    for the bare string ``"host"`` would grab the wrong value on
    any payload where ``"host"`` appears earlier as a substring --
    e.g. a sibling tag named ``"hostname"`` (the value of which
    would be returned instead of the real host tag) or a string
    field whose value happens to contain the literal ``"host":``
    sequence. The regex anchor makes sure we only match the
    JSON-key position, which is what Telegraf's actual wire format
    uses (``{"tags": {"host": "..."}}``).
    """
    if isinstance(payload, (bytes, bytearray)):
        # ``errors="replace"`` never raises for any byte sequence, so a
        # corrupted payload degrades to replacement characters instead
        # of an exception -- no try/except needed on this hot path.
        text = bytes(payload).decode("utf-8", errors="replace")
    elif isinstance(payload, str):
        text = payload
    else:
        return ""
    # Two-stage: first anchor on the JSON *key* position (``"host":``),
    # then walk the value the way the previous hand-rolled scanner did.
    # The first stage is the bug fix -- the bare-string ``text.find('"host"')``
    # would grab the wrong value on payloads where ``"host"`` appears
    # earlier as a substring (e.g. ``"hostname"`` or a string value
    # containing the literal ``"host":``). The second stage preserves
    # the original best-effort semantics: a string value is captured
    # verbatim up to the next ``"``; a non-string value, a missing
    # value, or an unclosed quote degrade to ``""`` or the partial
    # value, exactly as before.
    key_match = _HOST_KEY_RE.search(text)
    if key_match is None:
        return ""
    value_start = key_match.end()
    # Skip JSON whitespace between the colon and the value.
    while value_start < len(text) and text[value_start] in " \t":
        value_start += 1
    if value_start >= len(text) or text[value_start] != '"':
        return ""
    value_start += 1
    value_end = text.find('"', value_start)
    if value_end == -1:
        # Unclosed quote: return everything to end-of-payload. Matches
        # the previous scanner's behaviour; the snoop is best-effort.
        return text[value_start:]
    return text[value_start:value_end]


# Anchored once at import time: the ``host`` *key* (followed by ``:``,
# with optional JSON whitespace). The pattern intentionally rejects:
#   * ``"hostname": ...`` -- the bare ``"host"`` substring inside
#     ``"hostname"`` is not followed by ``:`` on the next characters,
#   * ``"path": "/some/host/file"`` -- the literal ``"host":`` inside
#     the string value is not the key position,
# so the first match in any well-formed Telegraf payload is the
# real ``host`` tag. The value is *not* part of the regex on purpose:
# the previous hand-rolled scanner had bespoke rules for unclosed
# quotes and non-string values, and keeping that logic outside the
# regex preserves the existing observable behaviour.
_HOST_KEY_RE = re.compile(r'"host"\s*:')
