"""Belief-change event bus (2026-09-09 #3).

Consolidation (ADD/UPDATE/DELETE) and reflection (supersession) call
``note_memory_event``; the pipeline scheduler's ``memory_match`` triggers
observe the queue on its minute tick — the event-driven half of Park's
plan-and-react (react when a *belief* changes, not only on schedules).

Process-local by design: events produced by a store in this process feed the
scheduler in this process (the serve process hosts both; the stdio MCP
servers have no scheduler). Best-effort everywhere.
"""
from __future__ import annotations

import logging

log = logging.getLogger(__name__)

_MAX_EVENTS = 50
_listeners: list = []


def add_listener(fn) -> None:
    """Register a callable ``(ts, text, kind)`` — the scheduler does this at
    startup to bridge events into its per-instance queue."""
    if fn not in _listeners:
        _listeners.append(fn)


def note_memory_event(text: str, kind: str) -> None:
    """Record a belief change; listeners (scheduler) are notified inline.
    kind ∈ {add, update, delete, supersede}."""
    import time as _time

    event = (_time.time(), (text or "")[:500], kind)
    for fn in list(_listeners):
        try:
            fn(*event)
        except Exception as e:  # a listener must never break the writer
            log.debug("memory-event listener failed: %s", e)
