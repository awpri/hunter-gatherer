"""Bounded tool-result sidecar (2026-08-23 #1).

The main loop condenses over-budget tool results to a local-tier digest and
parks the raw payload here, keyed by tool_call id. The always-on
``tool_result_fetch`` tool retrieves the full payload on demand — the agent
escalates to the raw dump only when the digest provably wasn't enough
(Anthropic's condense principle, finally applied to the loop's own results).

The sidecar lives in the store's SQLite (same file, same backups) as a
``tool_results`` table with a bounded prune: only the most recent
``max_rows`` payloads are kept — parked results are context relief, not an
archive. Every dynamic value passes through a ``?`` placeholder.
"""
from __future__ import annotations

import contextlib
import time


class ResultSidecar:
    """Park/fetch raw tool payloads. Constructed lazily from the ToolContext's
    store by the agent; failure-tolerant (condensation must never break a
    tool call)."""

    def __init__(self, store, *, max_rows: int = 100):
        self.store = store
        self.max_rows = max_rows
        with contextlib.suppress(Exception):
            conn = store._sqlite  # noqa: SLF001 — same-file sidecar by design
            conn.execute("CREATE TABLE IF NOT EXISTS tool_results (call_id TEXT PRIMARY KEY, payload TEXT NOT NULL, created_at REAL NOT NULL)")
            conn.commit()

    def park(self, call_id: str, payload: str) -> bool:
        """Store a raw payload; prune old rows beyond max_rows. Returns False
        when the sidecar is unavailable (caller keeps the raw text)."""
        with contextlib.suppress(Exception):
            conn = self.store._sqlite  # noqa: SLF001
            with self.store._lock:  # noqa: SLF001
                conn.execute("INSERT OR REPLACE INTO tool_results (call_id, payload, created_at) VALUES (?,?,?)",
                             (call_id, payload, time.time()))
                self._prune(conn)
                conn.commit()
            return True
        return False

    def _prune(self, conn) -> None:
        """Keep only the newest max_rows payloads (two simple statements — no
        subqueries)."""
        keep = max(1, int(self.max_rows))
        row = conn.execute("SELECT created_at FROM tool_results ORDER BY created_at DESC LIMIT 1 OFFSET ?",
                           (keep - 1,)).fetchone()
        if row is not None:
            conn.execute("DELETE FROM tool_results WHERE created_at < ?", (row[0],))

    def fetch(self, call_id: str, *, max_chars: int = 20_000) -> str | None:
        """Return the parked payload (truncated to max_chars), or None."""
        with contextlib.suppress(Exception):
            row = self.store._sqlite.execute(  # noqa: SLF001
                "SELECT payload FROM tool_results WHERE call_id = ?", (call_id,)
            ).fetchone()
            if row and row[0]:
                text = row[0]
                if len(text) > max_chars:
                    return text[:max_chars] + f"\n…(truncated, {len(text) - max_chars} chars remain)"
                return text
        return None

    def count(self) -> int:
        with contextlib.suppress(Exception):
            row = self.store._sqlite.execute(  # noqa: SLF001
                "SELECT COUNT(*) FROM tool_results"
            ).fetchone()
            return int(row[0]) if row else 0
        return 0
