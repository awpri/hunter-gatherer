"""Memory consolidation on write (Mem0 pattern).

On every candidate insert, we similarity-search the store; if a near-duplicate
exists (cosine ≥ dedup_cosine), a small LLM judges one of:

    ADD      – the candidate is genuinely new; store it.
    UPDATE   – an existing memory should be edited to incorporate the new info.
    DELETE   – the candidate directly contradicts an existing memory; remove
               the stale one (controlled forgetting) and store the new one.
    NOOP     – the candidate adds nothing; drop it.

The judgment call uses the cheap tier (local model) to keep cost down. When no
LLM is available, we fall back to: dedup → NOOP, contradiction heuristic via
negation keywords → DELETE, else ADD.

Every decision is recorded as an audit row in SQLite (metadata.history) so the
memory's evolution is traceable.
"""
from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

from proharness.config import Settings
from proharness.memory.schemas import ConsolidationDecision, Memory
from proharness.memory.store import MemoryStore

log = logging.getLogger(__name__)

Op = Literal["ADD", "UPDATE", "DELETE", "NOOP"]

# Negation/contradiction cues for the no-LLM heuristic.
_CONTRADICTION_CUES = [
    "not ", "no longer", "used to", "actually", "corrected:", "wait,",
    "previously", "changed to", "instead of", "wrong",
]


@dataclass
class Consolidator:
    """Decides what to do with a candidate memory given the current store."""

    store: MemoryStore
    settings: Settings
    judge: Callable[[str, str], Op] | None = None
    """Optional LLM-backed judge: takes (candidate_text, existing_text) and
    returns ADD/UPDATE/DELETE/NOOP. If None, a heuristic is used."""

    def decide(self, candidate: Memory) -> ConsolidationDecision:
        """Inspect the store for near-duplicates and decide the op."""
        return self._decide_from_hits(candidate, self.store.vector_search(candidate.text, top_k=3))

    def _decide_from_hits(self, candidate: Memory, hits) -> ConsolidationDecision:
        # Filter to true near-duplicates by the configured cosine threshold.
        near = [h for h in hits if h.score >= self.settings.dedup_cosine]

        if not near:
            return ConsolidationDecision(op="ADD", reason="no near-duplicate found")

        # Take the single most similar existing memory as the reconciliation target.
        best = max(near, key=lambda h: h.score)
        existing = self.store.get(best.id)
        if existing is None:
            return ConsolidationDecision(op="ADD", reason="near-dup id missing from sqlite")
        if candidate.is_permanent or existing.is_permanent:
            return ConsolidationDecision(op="ADD", target_id=existing.id,
                                         reason="permanent record requires explicit reconciliation")

        op = self._judge(candidate.text, existing.text)

        if op == "UPDATE":
            return ConsolidationDecision(op="UPDATE", target_id=existing.id, reason="LLM: update existing")
        if op == "DELETE":
            return ConsolidationDecision(op="DELETE", target_id=existing.id, reason="LLM: contradiction")
        if op == "NOOP":
            return ConsolidationDecision(op="NOOP", target_id=existing.id, reason="LLM: redundant")
        return ConsolidationDecision(op="ADD", reason="LLM: add despite similarity")

    def apply(self, candidate: Memory, decision: ConsolidationDecision) -> Memory | None:
        """Execute a decision; return None only when a NOOP changes nothing."""
        from proharness.memory.evidence import is_sensitive_memory

        target = self.store.get(decision.target_id) if decision.target_id else None
        tightened = False
        if (target is not None and not target.metadata.get("sensitive")
                and is_sensitive_memory(candidate, self.store)):
            # Re-importing an existing fact as private must protect the original
            # too, even when dedup would otherwise discard the new labels.
            target.metadata = {**target.metadata, "sensitive": True}
            self.store.update(target)
            tightened = True
        if candidate.is_permanent or (target is not None and target.is_permanent):
            decision = ConsolidationDecision(op="ADD", target_id=decision.target_id,
                                             reason="permanent retention prevents automatic reconciliation")
        if decision.op == "NOOP":
            if tightened:
                decision.reason = "existing fact retained; sensitivity tightened"
            self._audit(candidate, decision)
            log.debug("consolidate NOOP: %s", candidate.text[:60])
            return target if tightened else None

        if decision.op == "DELETE" and decision.target_id:
            old = self.store.get(decision.target_id)
            self.store.delete(decision.target_id)
            self._invalidate_cache(decision.target_id)  # 2026-09-01 #1
            if old:
                self._audit(candidate, decision, old=old.text)

        if decision.op == "UPDATE" and decision.target_id:
            old = self.store.get(decision.target_id)
            merged = self._merge(old, candidate) if old else candidate
            self.store.update(merged)
            self._invalidate_cache(decision.target_id)  # 2026-09-01 #1
            if old:
                self._audit(candidate, decision, old=old.text)
            return merged

        # ADD (or DELETE-then-ADD for contradictions).
        self.store.add(candidate)
        self._audit(candidate, decision)
        return candidate

    def store_with_consolidation(self, candidate: Memory) -> Memory | None:
        """Convenience: decide + apply in one call. Returns stored memory or None.

        Also persists A-MEM links (2026-08-19 #2): the dedup neighborhood
        search runs once here, and the top-m similar neighbors become the
        stored memory's outgoing links — marginal cost is one INSERT."""
        hits = self.store.vector_search(candidate.text, top_k=3)
        decision = self._decide_from_hits(candidate, hits)
        stored = self.apply(candidate, decision)
        if stored is not None:
            self._persist_links(stored, hits)
            self._note_memory_event(stored.text, decision.op.lower())  # 2026-09-09 #3
        return stored

    def _persist_links(self, stored: Memory, hits) -> None:
        """Top-m neighbors → outgoing links (Zettelkasten). Never breaks the store path."""
        top_m = int(getattr(self.settings, "link_top_m", 3) or 0)
        if top_m <= 0:
            return
        with contextlib.suppress(Exception):
            links = [(h.id, h.score) for h in hits if h.id != stored.id][:top_m]
            if links:
                self.store.set_links(stored.id, links)

    # ── internals ────────────────────────────────────────────────────────────
    def _invalidate_cache(self, memory_id: str) -> None:
        """Answer-cache coherence hook (2026-09-01 #1): a memory the cache's
        answers cited just mutated — drop the dependent entries. Best-effort;
        never breaks the store path."""
        with contextlib.suppress(Exception):
            from proharness.models.answer_cache import AnswerCache

            AnswerCache(self.store).invalidate_for_memory(memory_id)

    def _note_memory_event(self, text: str, kind: str) -> None:
        """Belief-change event seam (2026-09-09 #3): the scheduler's
        memory_match triggers observe ADD/UPDATE decisions. Best-effort."""
        with contextlib.suppress(Exception):
            from proharness.memory.events import note_memory_event

            note_memory_event(text, kind)


    def _judge(self, candidate_text: str, existing_text: str) -> Op:
        if self.judge is not None:
            try:
                op = self.judge(candidate_text, existing_text).strip().upper()
                if op.startswith(("ADD", "UPDATE", "DELETE", "NOOP")):
                    return op.split()[0]  # type: ignore[return-value]
            except Exception as e:  # pragma: no cover
                log.warning("judge failed (%s); falling back to heuristic", e)
        return self._heuristic(candidate_text, existing_text)

    @staticmethod
    def _heuristic(candidate_text: str, existing_text: str) -> Op:
        """Cheap no-LLM fallback."""
        c = candidate_text.lower()
        # If candidate looks like a correction of the existing fact → DELETE old, ADD new.
        if any(cue in c for cue in _CONTRADICTION_CUES):
            return "DELETE"
        # Otherwise treat high-similarity as redundant.
        return "NOOP"

    def _merge(self, old: Memory, new: Memory) -> Memory:
        """Update-in-place: keep old id, fold in new text + bump importance.
        Preserves an audit trail in metadata['history']."""
        from proharness.memory.evidence import is_sensitive_memory

        # A generated rewrite must not weaken the privacy of its sources or
        # expose sensitive text retained in the merge history.
        protected = {key: True for key in ("excluded", "application_excluded")
                     if old.metadata.get(key) or new.metadata.get(key)}
        if is_sensitive_memory(old, self.store) or is_sensitive_memory(new, self.store):
            protected["sensitive"] = True
        history = list(old.metadata.get("history", []))
        history.append({"at": time.time(), "from": old.text, "to": new.text, "source": new.source})
        old.text = new.text if new.text else old.text
        old.importance = max(old.importance, new.importance)
        old.tags = sorted(set(old.tags) | set(new.tags))
        old.last_accessed = time.time()
        old.metadata = {**old.metadata, "history": history,
                        **{k: v for k, v in new.metadata.items() if k != "history"}, **protected}
        old.source = new.source
        return old

    def _audit(self, candidate: Memory, decision: ConsolidationDecision, old: str | None = None) -> None:
        """Record the decision in a lightweight audit table."""
        try:
            self.store._sqlite.execute(  # noqa: SLF001
                """
                CREATE TABLE IF NOT EXISTS consolidation_audit (
                    ts REAL, op TEXT, target_id TEXT, candidate TEXT, old TEXT, reason TEXT
                )
                """
            )
            self.store._sqlite.execute(  # noqa: SLF001
                "INSERT INTO consolidation_audit VALUES (?, ?, ?, ?, ?, ?)",
                (time.time(), decision.op, decision.target_id, candidate.text, old, decision.reason),
            )
            self.store._sqlite.commit()
        except Exception as e:  # pragma: no cover
            log.debug("audit write failed: %s", e)


def make_judge_from_llm(llm) -> Callable[[str, str], Op]:
    """Build a judge callable from any LLMClient. Uses a tight prompt so the
    response is a single token — cheap on both local and cloud models."""

    prompt_tmpl = (
        "You are a memory reconciler. Decide what to do with a NEW candidate "
        "fact given an EXISTING fact in the agent's memory.\n\n"
        "EXISTING: {existing}\n"
        "NEW:      {candidate}\n\n"
        "Reply with exactly one word:\n"
        "  ADD      (new info, nothing like it exists)\n"
        "  UPDATE   (existing fact should be amended/extended with the new info)\n"
        "  DELETE   (new fact contradicts/supersedes the existing one)\n"
        "  NOOP     (new fact adds nothing new)\n\n"
        "Decision:"
    )

    def judge(candidate: str, existing: str) -> Op:
        from proharness.models.base import Message

        result = llm.chat([Message(role="user", content=prompt_tmpl.format(candidate=candidate, existing=existing))], temperature=0.0, max_tokens=5)
        return result.message.content.strip().split()[0].upper()  # type: ignore[return-value]

    return judge
