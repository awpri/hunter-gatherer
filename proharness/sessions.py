"""Per-session conversation isolation (idea #2 — working vs long-term boundary).

ProHarness draws a clean line the codebase previously blurred:

  - **Working memory** = conversation history, per-session, ephemeral. Two
    browser tabs talking to the same server must NOT interleave their turns
    into one history. ``SessionStore`` holds these, keyed by session id.
  - **Long-term memory** = the SQLite + Qdrant store, shared, durable.
    Cross-session recall is the *point* of long-term memory; only working
    history is isolated.

Research link: the MemGPT/Letta working-memory tier — the project cites this
taxonomy in ``docs/research.md`` §2 but the original code held one shared
``AgentLoop.history`` list, which silently corrupted conversations the moment
a second client connected. This makes the boundary a real architectural seam.

A ``DEFAULT`` session keeps the single-session CLI path working unchanged.
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from contextlib import contextmanager

from proharness.models.base import Message

DEFAULT_SESSION = "default"

# Rolling window: how many turns (user+assistant pairs) each session keeps.
MAX_TURNS = 12


class SessionStore:
    """Thread-safe per-session working-memory store.

    Each session holds a rolling window of conversation ``Message`` objects.
    The long-term ``MemoryStore`` is shared across sessions (cross-session
    recall is the point); only per-conversation working history is isolated.
    """

    def __init__(self, max_turns: int = MAX_TURNS, *, on_evict=None):
        """``on_evict`` (2026-08-11 #3) is an optional callable
        ``(user_message, assistant_answer) -> None`` invoked for each evicted
        turn just before it leaves the window — the Letta eviction policy hook
        that pages old working memory into long-term storage (as a summarized
        episodic memory) instead of silently dropping it."""
        self._max_turns = max_turns
        self._on_evict = on_evict
        self._sessions: dict[str, list[Message]] = {}
        self._lock = threading.Lock()
        self._turn_locks = {}

    @contextmanager
    def turn(self, session_id=None):
        """Serialize a complete conversation turn; release unused lock entries."""
        sid = session_id or DEFAULT_SESSION
        with self._lock:
            entry = self._turn_locks.setdefault(sid, [threading.Lock(), 0])
            entry[1] += 1
        try:
            with entry[0]:
                yield
        finally:
            with self._lock:
                entry[1] -= 1
                if entry[1] == 0:
                    self._turn_locks.pop(sid, None)

    def get_history(self, session_id: str | None = None) -> list[Message]:
        """Return the working-memory window for a session (copy)."""
        sid = session_id or DEFAULT_SESSION
        with self._lock:
            return list(self._sessions.get(sid, []))

    def append_turn(self, session_id: str | None, user_message: str, assistant_answer: str) -> None:
        """Append a user/assistant pair to a session and trim to the window."""
        sid = session_id or DEFAULT_SESSION
        evicted: list[tuple[str, str]] = []
        with self._lock:
            hist = self._sessions.setdefault(sid, [])
            hist.append(Message(role="user", content=user_message))
            hist.append(Message(role="assistant", content=assistant_answer))
            # Keep a rolling window of the most recent turns. Evicted pairs are
            # handed to the on_evict hook (outside the lock) before vanishing.
            if len(hist) > self._max_turns * 2:
                drop = len(hist) - self._max_turns * 2
                dropped = hist[:drop]
                self._sessions[sid] = hist[drop:]
                for i in range(0, len(dropped), 2):
                    u = dropped[i]
                    a = dropped[i + 1] if i + 1 < len(dropped) else None
                    evicted.append((u.content or "", (a.content or "") if a else ""))
        if evicted and self._on_evict is not None:
            import contextlib

            for user_msg, asst_msg in evicted:
                with contextlib.suppress(Exception):
                    self._on_evict(user_msg, asst_msg)

    def clear(self, session_id: str | None = None) -> None:
        sid = session_id or DEFAULT_SESSION
        with self._lock:
            self._sessions.pop(sid, None)

    def list_sessions(self) -> list[str]:
        with self._lock:
            return list(self._sessions.keys())

    def session_count(self) -> int:
        with self._lock:
            return len(self._sessions)


def with_session_history(
    sessions: SessionStore,
    session_id: str | None,
    on_history: Callable[[list[Message]], None] | None = None,
) -> list[Message]:
    """Convenience: load a session's history and optionally observe it.

    Returns the history list (for passing to ``Agent.run(history=...)``).
    """
    hist = sessions.get_history(session_id)
    if on_history:
        on_history(hist)
    return hist
