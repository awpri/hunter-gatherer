"""Token + dollar accounting, plus router-decision outcome logging.

Anthropic's published finding: token usage explains ~80% of the variance in
agent-eval performance, and multi-agent systems burn ~15× chat tokens. So cost
is a first-class metric, recorded on every model call and reportable per
session / per task.

Idea #1 extends this with **router decision logging**: each turn records the
``Route`` (tier, model, score, α) alongside a cheap outcome flag captured from
signals already present in the loop (verification-gate revision, max-steps
exhaustion, user re-asking the same intent). The accumulated rows are both the
α-sweep's regression input *and* the training set the documented "train a real
router" extension point needs.
"""

from __future__ import annotations

import time
from collections.abc import Iterable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from threading import Lock
from typing import Literal

_ACTIVE_USAGE = ContextVar("hg_request_usage", default=None)

# A turn's outcome, derived from loop signals (no LLM introspection — the
# research.md §6 rule). 'good' is the absence of every negative signal.
Outcome = Literal["good", "revised", "max_steps", "repeat"]


@dataclass
class Usage:
    model: str
    in_tokens: int
    out_tokens: int

    @property
    def total_tokens(self) -> int:
        return self.in_tokens + self.out_tokens


@dataclass
class RouterDecision:
    """One routing decision + its observed outcome. Persisted to the
    ``router_decisions`` SQLite table by the store.

    `score` is the embedding-similarity score in [0,1] that produced the
    route (None for rule/heuristic routes). `alpha` is the threshold in effect
    at decision time. `outcome` starts as 'good' and is downgraded by the loop
    as negative signals arrive.
    """

    query: str
    tier: str
    model: str
    score: float | None
    alpha: float
    outcome: Outcome = "good"
    in_tokens: int = 0
    out_tokens: int = 0
    ts: float = field(default_factory=time.time)
    # Free-form reason echoed from the Route (for human inspection).
    reason: str = ""


@dataclass
class CostTracker:
    """Thread-safe accumulator. One per session/task."""

    settings: object = None  # hunter_gatherer.config.Settings; avoid hard import
    entries: list[Usage] = field(default_factory=list)
    decisions: list[RouterDecision] = field(default_factory=list)
    _lock: Lock = field(default_factory=Lock, repr=False)

    def record(self, model: str, in_tokens: int, out_tokens: int) -> None:
        if in_tokens == 0 and out_tokens == 0:
            return
        with self._lock:
            usage = Usage(model=model, in_tokens=in_tokens, out_tokens=out_tokens)
            self.entries.append(usage)
        collector = _ACTIVE_USAGE.get()
        if collector is not None and collector is not self:
            collector.record(model, in_tokens, out_tokens)

    def extend(self, others: Iterable[Usage]) -> None:
        for usage in list(others):
            self.record(usage.model, usage.in_tokens, usage.out_tokens)

    @contextmanager
    def request_usage(self):
        """Attribute this request's calls without subtracting global totals.

        Thread-pool tasks must propagate context with copy_context().run.
        """
        collector = CostTracker(settings=self.settings)
        token = _ACTIVE_USAGE.set(collector)
        try:
            yield collector
        finally:
            _ACTIVE_USAGE.reset(token)

    # ── router decisions (idea #1) ───────────────────────────────────────────
    def open_decision(self, decision: RouterDecision) -> RouterDecision:
        """Register a decision at route time. The loop updates its outcome/
        token fields in place over the course of the turn, then the store
        persists it via ``record_decision``."""
        with self._lock:
            self.decisions.append(decision)
        return decision

    def record_decision(self, decision: RouterDecision) -> None:
        """No-op placeholder; persistence lives in the store (which owns the
        SQLite connection). Kept on the tracker for API symmetry and so callers
        that don't wire the store still accumulate decisions in-memory."""
        # The actual INSERT happens in MemoryStore.log_router_decision; nothing
        # to do here except keep the decision visible on the tracker.
        return

    # ── aggregates ───────────────────────────────────────────────────────────
    def request_tokens(self) -> int:
        """Current request total, or process total outside a request scope."""
        collector = _ACTIVE_USAGE.get()
        return (collector or self).total_tokens()

    def total_tokens(self) -> int:
        with self._lock:
            return sum(u.total_tokens for u in self.entries)

    def total_cost_usd(self) -> float:
        if self.settings is None:
            return 0.0
        cost = 0.0
        with self._lock:
            entries = list(self.entries)
        for u in entries:
            cost += u.in_tokens / 1_000_000 * self.settings.price_for(u.model, "in")
            cost += u.out_tokens / 1_000_000 * self.settings.price_for(u.model, "out")
        return cost

    def per_model(self) -> dict[str, Usage]:
        agg: dict[str, Usage] = {}
        with self._lock:
            entries = list(self.entries)
        for u in entries:
            cur = agg.setdefault(u.model, Usage(model=u.model, in_tokens=0, out_tokens=0))
            cur.in_tokens += u.in_tokens
            cur.out_tokens += u.out_tokens
        return agg

    def report(self) -> str:
        """Human-readable report."""
        lines = [
            f"Total tokens: {self.total_tokens():,}",
            f"Total cost:   ${self.total_cost_usd():.4f}",
            "Per model:",
        ]
        for model, u in sorted(self.per_model().items(), key=lambda kv: -kv[1].total_tokens):
            lines.append(f"  {model:24s} in={u.in_tokens:>8,}  out={u.out_tokens:>8,}")
        if self.decisions:
            lines.append(f"Router decisions: {len(self.decisions)}")
            from collections import Counter

            counts = Counter(d.outcome for d in self.decisions)
            lines.append("  outcomes: " + ", ".join(f"{k}={v}" for k, v in sorted(counts.items())))
        return "\n".join(lines)
