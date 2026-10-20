"""Procedural memory: mine successful tool sequences into reusable procedures.

Idea #3 (2026-08-05): ``schemas.py`` defines ``procedural`` as a first-class
memory type, but nothing ever writes one. When the agent solves a class of
task through the same tool sequence repeatedly, nothing is learned — each run
re-derives the procedure. This module fixes that:

  1. **Mine** — after a successful ``Agent.run`` (final answer, no revision,
     ``stopped_reason="done"``), extract the ordered tool-call sequence.
  2. **Store** — when a sequence recurs (the objective embedding matches a
     stored procedural memory above a threshold), bump its importance + record
     the recurrence. Otherwise store a new procedural memory capturing "for
     X-shaped objectives, this tool order worked."
  3. **Boost** — in ``selector.py``, when a procedural memory's objective
     embedding matches the current query, boost its constituent tools in the
     RAG-over-tools ranking, so the agent reaches for the proven sequence
     faster.

The *external* signal here is "the tool run succeeded" — not the model's
self-assessment — so this sidesteps the Huang et al. self-correction trap
(``docs/research.md`` §6).
"""
from __future__ import annotations

import logging
import math

from proharness.memory.embeddings import Embedder
from proharness.memory.schemas import Memory
from proharness.memory.store import MemoryStore

log = logging.getLogger(__name__)

# Minimum sequence length worth mining (a single-tool "sequence" isn't a
# procedure) and the cosine threshold above which two objectives are "the same
# shape" (so we bump the existing procedural memory rather than add a new one).
MIN_SEQUENCE_LEN = 2
DEFAULT_OBJECTIVE_COSINE = 0.80


def extract_tool_sequence(messages) -> list[str]:
    """Extract the ordered tool-call names from a finished agent run's messages.

    Returns [] for runs with no tool calls or only failed ones.
    """
    seq: list[str] = []
    for m in messages:
        # Assistant messages that requested tools carry tool_calls.
        if getattr(m, "role", None) == "assistant" and getattr(m, "tool_calls", None):
            for tc in m.tool_calls:
                seq.append(tc.name)
    return seq


class ProceduralMiner:
    """Mine + store successful tool sequences as procedural memories."""

    def __init__(
        self,
        store: MemoryStore,
        embedder: Embedder,
        *,
        min_sequence_len: int = MIN_SEQUENCE_LEN,
        objective_cosine: float = DEFAULT_OBJECTIVE_COSINE,
    ):
        self.store = store
        self.embedder = embedder
        self.min_sequence_len = min_sequence_len
        self.objective_cosine = objective_cosine

    def maybe_mine(
        self,
        objective: str,
        tool_sequence: list[str],
        *,
        success: bool = True,
    ) -> str | None:
        """If the sequence is worth keeping and the run succeeded, store (or
        bump) a procedural memory. Returns the memory id, or None if not mined.

        ``success`` defaults True; pass False for runs that hit max_steps or
        revised — we don't mine those (external signal: they didn't cleanly
        succeed).
        """
        if not success or len(tool_sequence) < self.min_sequence_len:
            return None
        try:
            # Look for an existing procedural memory for a similar objective.
            existing = self._find_matching(objective)
            if existing is not None:
                # Same objective shape → bump importance + record the recurrence.
                self._bump(existing, tool_sequence)
                return existing.id
            # New procedure. Encode the sequence compactly for the text field.
            return self._store_new(objective, tool_sequence)
        except Exception as e:
            log.debug("procedural mining failed: %s", e)
            return None

    def _find_matching(self, objective: str) -> Memory | None:
        """Find an existing procedural memory whose objective is similar."""
        # Only procedural memories are candidates.
        hits = self.store.vector_search(objective, top_k=5, filters={"type": "procedural"})
        if not hits:
            return None
        qv = self.embedder.embed(objective)
        for h in hits:
            stored = self.store.get(h.id)
            if stored is None:
                continue
            # Compare the stored objective (carried in metadata) to the query.
            stored_obj = stored.metadata.get("objective", stored.text)
            sv = self.embedder.embed(stored_obj)
            if _cosine(qv, sv) >= self.objective_cosine:
                return stored
        return None

    def _store_new(self, objective: str, tool_sequence: list[str]) -> str:
        """Store a new procedural memory capturing the objective + sequence."""
        seq_str = " → ".join(tool_sequence)
        mem = Memory(
            text=f"For objectives like {objective[:80]!r}, this tool sequence worked: {seq_str}",
            type="procedural",
            importance=6.0,
            tags=["procedural", "mined"],
            source="miner",
            metadata={
                "objective": objective,
                "tools": tool_sequence,
                "occurrences": 1,
            },
        )
        self.store.add(mem)
        log.info("mined new procedural memory: %s (tools=%s)", mem.id, tool_sequence)
        return mem.id

    def _bump(self, existing: Memory, tool_sequence: list[str]) -> None:
        """Reinforce an existing procedural memory on recurrence."""
        meta = dict(existing.metadata)
        meta["occurrences"] = int(meta.get("occurrences", 1)) + 1
        # Union the tools seen (captures minor sequence variation over time).
        tools = list(dict.fromkeys(meta.get("tools", []) + tool_sequence))
        meta["tools"] = tools
        existing.metadata = meta
        # Importance grows with recurrence (capped at 9 — below the "must keep"
        # tier that reflection treats specially).
        existing.importance = min(9.0, existing.importance + 0.5)
        self.store.update(existing)
        log.info("reinforced procedural memory %s (occurrences=%d)", existing.id, meta["occurrences"])
        # Procedural→pipeline promotion (2026-08-13 #2): graduate the sequence
        # into an authored pipeline once recurrence crosses the threshold, so
        # it becomes MCP-callable + agent-callable instead of a latent boost.
        promote_at = getattr(self.store.settings, "procedural_promote_at", 5)
        if promote_at > 0:
            try:
                from proharness.pipelines.from_procedural import promote_if_due

                slug = promote_if_due(existing, self.store, threshold=promote_at)
                if slug:
                    log.info("promoted procedural memory %s → pipeline '%s'", existing.id, slug)
            except Exception as e:
                log.debug("procedural promotion failed: %s", e)


def boost_for_query(
    store: MemoryStore,
    embedder: Embedder,
    query: str,
    *,
    top_k: int = 3,
    cosine_floor: float = 0.55,
) -> list[str]:
    """Return tool names to boost for this query, drawn from matching procedural
    memories. Used by ``selector.py`` to lift proven tools in the ranking.

    Returns [] when no procedural memory matches the query closely enough.
    """
    try:
        hits = store.vector_search(query, top_k=top_k, filters={"type": "procedural"})
    except Exception:
        return []
    if not hits:
        return []
    qv = embedder.embed(query)
    boost: list[str] = []
    seen: set[str] = set()
    for h in hits:
        stored = store.get(h.id)
        if stored is None:
            continue
        stored_obj = stored.metadata.get("objective", stored.text)
        sv = embedder.embed(stored_obj)
        if _cosine(qv, sv) >= cosine_floor:
            for t in stored.metadata.get("tools", []):
                if t not in seen:
                    boost.append(t)
                    seen.add(t)
    return boost


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)
