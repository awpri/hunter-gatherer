"""Semantic answer cache (2026-09-01 #1) — the zero-token "cached" tier.

The harness cached everything *upstream* of generation — routing decisions,
HyDE drafts, the BoN scorer — but never answers themselves: a repeated
cloud-tier query re-paid full generation every time. This module adds the
generation-level cache (all SQL lives in MemoryStore's answer-cache methods):

  - exact-match lookup over recent queries, backed by embedding-similarity
    retrieval (high floor, default 0.95, per GPTCache's semi-precise-cache
    finding);
  - a hit skips routing, recall, tool selection, AND the model call —
    a zero-token turn;
  - entries record the ``[mem:…]`` ids the answer cited, and every memory
    mutation site (consolidation UPDATE/DELETE, supersession, decay deletes)
    fires ``invalidate_for_memory`` dropping dependent entries — a cached
    answer can never outlive the belief it was grounded in. TTL + LRU cap
    are belt-and-suspenders.

Research link: GPTCache (arXiv:2303.06671 — embedding-similarity semantic
caching cut serving latency by an order of magnitude at near-identical
quality) + the §7 external-signals rule: invalidation is driven by observed
memory mutations, never a guessed TTL alone.
"""
from __future__ import annotations

import contextlib
import json
import logging
import math
import re
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

_MEM_LABEL = re.compile(r"\[mem:([0-9a-f-]{6})")


@dataclass
class CachedAnswer:
    query: str
    answer: str
    cited_mem_ids: list[str] = field(default_factory=list)
    ts: float = 0.0
    hits: int = 0


class AnswerCache:
    """Semantic answer cache over the store's answer-cache table.
    Failure-tolerant everywhere — a cache must never break the turn path."""

    def __init__(self, store, embedder=None, settings=None):
        self.store = store
        self.embedder = embedder
        self.settings = settings or getattr(store, "settings", None)

    # ── config helpers ─────────────────────────────────────────────────────
    def _enabled(self) -> bool:
        return bool(self.settings and getattr(self.settings, "answer_cache_enabled", True))

    def _sim_floor(self) -> float:
        return float(getattr(self.settings, "answer_cache_sim", 0.95) or 0.0)

    def _ttl(self) -> float:
        return float(getattr(self.settings, "answer_cache_ttl_hours", 168.0) or 0) * 3600

    def _cap(self) -> int:
        return int(getattr(self.settings, "answer_cache_max", 256) or 0)

    # ── lookup ─────────────────────────────────────────────────────────────
    def lookup(self, query: str) -> CachedAnswer | None:
        """Exact match first, then embedding similarity ≥ floor. Returns the
        cached answer (bumping hit stats) or None."""
        if not self._enabled():
            return None
        q = (query or "").strip()
        if not q:
            return None
        hit = self._row_to_cached(self.store.get_cached_answer(q))
        if hit is None and self.embedder is not None:
            hit = self._semantic_lookup(q)
        if hit is None:
            return None
        ttl = self._ttl()
        if ttl > 0 and (time.time() - hit.ts) > ttl:
            self.store.delete_cached_answer(hit.query)
            return None
        self.store.bump_cached_answer(hit.query)
        hit.hits += 1
        return hit

    def _semantic_lookup(self, query: str) -> CachedAnswer | None:
        floor = self._sim_floor()
        if floor <= 0 or self.embedder is None:
            return None
        rows = self.store.all_cached_answers(limit=max(50, self._cap()))
        if not rows:
            return None
        qv = self.embedder.embed(query)
        best, best_sim = None, 0.0
        for row in rows:
            sim = _cosine(qv, self.embedder.embed(row[0]))
            if sim > best_sim:
                best, best_sim = row, sim
        if best is None or best_sim < floor:
            return None
        log.debug("answer cache semantic hit (sim=%.3f)", best_sim)
        return self._row_to_cached(best)

    def _row_to_cached(self, row) -> CachedAnswer | None:
        if row is None:
            return None
        with contextlib.suppress(Exception):
            cited = json.loads(row[2]) if row[2] else []
            return CachedAnswer(query=row[0], answer=row[1], cited_mem_ids=cited,
                                ts=float(row[3]), hits=int(row[4]))
        return None

    # ── write / invalidate ─────────────────────────────────────────────────
    def store_answer(self, query: str, answer: str, cited_mem_ids=None) -> None:
        """Write an entry (called only after good-outcome turns). LRU-capped."""
        if not self._enabled():
            return
        with contextlib.suppress(Exception):
            self.store.upsert_cached_answer(
                (query or "").strip(), answer,
                json.dumps(sorted(set(cited_mem_ids or []))), time.time(),
                cap=self._cap(),
            )

    def invalidate_for_memory(self, memory_id: str) -> int:
        """Drop every entry whose answer cited this memory — the coherence
        hook consolidation/supersession/decay fire on mutation."""
        dropped = 0
        for row in self.store.all_cached_answers(limit=1000):
            with contextlib.suppress(Exception):
                cited = json.loads(row[2]) if row[2] else []
                if memory_id in cited:
                    self.store.delete_cached_answer(row[0])
                    dropped += 1
        if dropped:
            log.info("answer cache: invalidated %d entr(y/ies) citing %s",
                     dropped, memory_id[:8])
        return dropped

    def hit_stats(self) -> dict:
        return self.store.answer_cache_stats()


def cited_memory_ids(text: str, store=None) -> list[str]:
    """Full memory ids for the [mem:xxxxxx] labels an answer cited."""
    labels = {m.group(1) for m in _MEM_LABEL.finditer(text or "")}
    if not labels or store is None:
        return []
    out = []
    with contextlib.suppress(Exception):
        for mem in store.list_all(limit=500):
            if mem.id[:6] in labels:
                out.append(mem.id)
    return out


def _cosine(a, b) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)
