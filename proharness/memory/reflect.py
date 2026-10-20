"""Sleep-time reflection / consolidation — the anti-forgetting engine.

Runs OFF the hot inference path (cron / `proharness consolidate` subcommand /
importance-threshold trigger):
  1. Summarize clusters of old, low-importance episodic memories into higher-
     level semantic ones (Park "reflection" pass).
  2. Dedup near-identical memories that slipped past the on-write check.
  3. Decay importance of stale, never-accessed memories so they fall out of
     recall naturally (soft forgetting).

This is proharness's primary defense against catastrophic forgetting. The
*why-it-matters* citation is Tadros et al., *Nature Communications* 2022
("Sleep-like unsupervised replay reduces catastrophic forgetting in ANNs"):
offline replay/consolidation phases interleaved with learning preserve prior
knowledge without access to the original inputs — exactly the shape of
`proharness consolidate` running off the hot path while new memories stream
in. Letta remains the *technique* citation (sleep-time compute).

Consolidation is a *stability* operation, not just compression: consolidated
semantic memories get an importance-boost (they distill many episodic sources,
so they outweigh any single one) and decay slower than raw episodic memories
(idea #1). This is what lets proharness honestly claim "research-grounded
continual learning" rather than "memory + a cron job."
"""
from __future__ import annotations

import contextlib
import logging
import time
from collections.abc import Callable
from dataclasses import dataclass

from proharness.config import Settings
from proharness.memory.consolidate import Consolidator
from proharness.memory.schemas import Memory
from proharness.memory.store import MemoryStore

log = logging.getLogger(__name__)

# Thresholds (hours / counts). Tunable via direct attribute access.
DEFAULT_EPISODIC_AGE_HOURS = 72       # summarize episodic memories older than this
DEFAULT_DECAY_AGE_HOURS = 24 * 30     # decay importance after a month unaccessed
DEFAULT_DECAY_FACTOR = 0.85           # multiply importance by this (raw memories)
DEFAULT_MIN_IMPORTANCE = 1.0          # floor; below this we delete (raw memories)
# Consolidated memories are stability anchors (idea #1): they decay slower and
# survive to a lower floor, because distilling many episodic sources into one
# semantic memory is itself a stability operation worth preserving.
DEFAULT_CONSOLIDATED_DECAY_FACTOR = 0.95  # gentler decay for reflection-produced memories
DEFAULT_CONSOLIDATED_MIN_IMPORTANCE = 0.5  # lower deletion floor for them
# Per-source boost added to a cluster's max importance when consolidating
# (idea #1: a memory distilled from N sources outweighs any single one).
DEFAULT_CONSOLIDATION_IMPORTANCE_BOOST = 0.5  # per source, capped at 10
DEFAULT_CLUSTER_COSINE = 0.78         # group similar memories for summarization
DEFAULT_SUMMARIZE_MAX_IMPORTANCE = 7.0  # only summarize lower-importance episodic


@dataclass
class ReflectionReport:
    summarized: int = 0
    deduped: int = 0
    decayed: int = 0
    deleted: int = 0
    superseded: int = 0  # belief-revision marks (2026-08-20 #1)
    rejected: int = 0  # consolidation-faithfulness rejections (2026-08-28 #2)
    created: list[str] = None  # type: ignore[assignment]

    def __post_init__(self):
        if self.created is None:
            self.created = []


class Reflector:
    def __init__(
        self,
        store: MemoryStore,
        settings: Settings,
        summarizer: Callable[[list[Memory]], str] | None = None,
        consolidator: Consolidator | None = None,
        judge: Callable[[str, str], str] | None = None,
        faithfulness: Callable[[str, str], str] | None = None,
    ):
        self.store = store
        self.settings = settings
        self.summarizer = summarizer
        self.consolidator = consolidator or Consolidator(store=store, settings=settings)
        # Belief-revision judge (2026-08-20 #1): takes (older_text, newer_text)
        # and returns "SUPERSEDE" when the pair contradicts. Build with
        # make_contradiction_judge(llm) — the local tier, per the house rule.
        self.judge = judge
        # Consolidation-faithfulness judge (2026-08-28 #2): takes
        # (summary, concatenated_sources) and returns ACCEPT/REJECT — a
        # summary is only allowed to replace its sources when its claims are
        # entailed by them. Build with make_faithfulness_judge(llm).
        self.faithfulness = faithfulness
        self.episodic_age_h = DEFAULT_EPISODIC_AGE_HOURS
        self.decay_age_h = DEFAULT_DECAY_AGE_HOURS
        self.decay_factor = DEFAULT_DECAY_FACTOR
        self.min_importance = DEFAULT_MIN_IMPORTANCE
        self.consolidated_decay_factor = DEFAULT_CONSOLIDATED_DECAY_FACTOR
        self.consolidated_min_importance = DEFAULT_CONSOLIDATED_MIN_IMPORTANCE
        self.consolidation_boost = DEFAULT_CONSOLIDATION_IMPORTANCE_BOOST
        self.cluster_cosine = DEFAULT_CLUSTER_COSINE
        self.summarize_max_importance = DEFAULT_SUMMARIZE_MAX_IMPORTANCE

    def run(self, *, summarize: bool = True, dedup: bool = True, decay: bool = True,
            revise_beliefs: bool = True) -> ReflectionReport:
        report = ReflectionReport()
        # Snapshot before the pass (belt-and-suspenders): deletes are soft
        # (tombstones) and recoverable via store.restore, but a consistent
        # point-in-time copy makes a whole bad pass reversible in one step.
        with contextlib.suppress(Exception):
            if summarize or dedup or decay or revise_beliefs:
                self.store.snapshot("reflection")
        if summarize:
            report.summarized, report.created = self._summarize_episodic()
            report.rejected = getattr(self, "_rejected_summaries", 0)
        if dedup:
            report.deduped = self._dedup_pass()
        if decay:
            report.decayed, report.deleted = self._decay_pass()
        if revise_beliefs:
            report.superseded = self._contradiction_pass()
        # Reset the importance trigger counter after a completed pass (idea #2).
        self.store.reset_reflection_state()
        log.info(
            "reflection: summarized=%d deduped=%d decayed=%d deleted=%d created=%d "
            "superseded=%d rejected=%d",
            report.summarized, report.deduped, report.decayed, report.deleted,
            len(report.created), report.superseded, report.rejected,
        )
        return report

    # ── belief revision (2026-08-20 #1) ─────────────────────────────────────
    def _contradiction_pass(self) -> int:
        """Reflection-time contradiction sweep: pair semantic memories against
        paraphrase-band neighbors (cosine in [contradiction_cosine,
        dedup_cosine) — the band insert-time dedup never sees), judge each
        pair, and mark SUPERSESSION instead of deleting:

            older.metadata.superseded_by = newer.id, valid_to = now
            newer.metadata.valid_from = max(its own, now)

        This retires the documented limitation (research.md §2): paraphrased
        contradictions ("lives in Berlin" → "moved to Munich") land ~0.77,
        never triggered the Mem0 DELETE path, and both facts coexisted — with
        Park scoring sometimes surfacing the stale one. Supersession keeps the
        audit trail a hard delete destroys."""
        if self.judge is None:
            return 0
        now = time.time()
        floor = float(getattr(self.settings, "contradiction_cosine", 0.72))
        max_pairs = int(getattr(self.settings, "contradiction_max_pairs", 20))
        pairs = self.store.contradiction_candidates(
            floor=floor, ceiling=self.settings.dedup_cosine, max_pairs=max_pairs,
        )
        marked = 0
        for older, newer, _score in pairs:
            if older.is_permanent or newer.is_permanent:
                continue
            with contextlib.suppress(Exception):
                verdict = (self.judge(older.text, newer.text) or "").strip().upper()
                if not verdict.startswith("SUPERSEDE"):
                    continue
                older.metadata = {**older.metadata, "superseded_by": newer.id, "valid_to": now}
                self.store.update(older)
                newer.metadata = {**newer.metadata,
                                  "valid_from": max(float(newer.metadata.get("valid_from", 0.0)), now)}
                self.store.update(newer)
                self._invalidate_cache(older.id)  # 2026-09-01 #1: coherence
                marked += 1
        if marked:
            log.info("belief revision: %d contradiction(s) marked as superseded", marked)
        return marked

    def maybe_run(
        self,
        threshold: float | None = None,
        *,
        summarize: bool = True,
        dedup: bool = True,
        decay: bool = True,
    ) -> ReflectionReport | None:
        """Park-2023 importance-threshold trigger (idea #2).

        Runs a reflection pass iff the store's cumulative pending-importance
        since the last pass crosses `threshold` (defaults to
        settings.reflect_importance_threshold). Returns None (no-op) when below
        threshold; returns the ReflectionReport when it runs.

        This makes reflection signal-driven ("something worth reflecting on
        accumulated") rather than purely clock-driven, while the cron /
        `proharness consolidate` path remains as a fallback for idle periods.
        """
        thr = threshold if threshold is not None else self.settings.reflect_importance_threshold
        if thr <= 0:
            return None  # signal-triggering disabled → cron-only
        pending = self.store.pending_importance()
        if pending < thr:
            log.debug("reflection not triggered: pending=%.1f < threshold=%.1f", pending, thr)
            return None
        log.info("reflection triggered by importance: pending=%.1f ≥ threshold=%.1f", pending, thr)
        return self.run(summarize=summarize, dedup=dedup, decay=decay)

    # ── passes ───────────────────────────────────────────────────────────────
    def _summarize_episodic(self) -> tuple[int, list[str]]:
        """Cluster old episodic memories, summarize each cluster into a single
        semantic memory, then delete the originals."""
        if self.summarizer is None:
            log.info("no summarizer configured; skipping episodic summarization")
            return 0, []
        from proharness.memory.evidence import is_sensitive_memory

        now = time.time()
        cutoff = now - self.episodic_age_h * 3600
        old_episodic = [
            m
            for m in self.store.list_all(limit=10_000, type_filter="episodic")
            if m.is_retrievable() and not m.is_permanent
            and m.created_at < cutoff and m.importance < self.summarize_max_importance
        ]
        if not old_episodic:
            return 0, []
        clusters = self._cluster(old_episodic)
        summarized = 0
        rejected = 0
        created: list[str] = []
        for cluster in clusters:
            if len(cluster) < 2:
                continue
            try:
                summary = self.summarizer(cluster)
            except Exception as e:
                log.warning("summarizer failed on a cluster: %s", e)
                continue
            # Faithfulness gate (2026-08-28 #2): this pass is the most
            # aggressive write path — the summary is importance-boosted into
            # the store's most durable memory and then its sources are
            # deleted, so a hallucinating summary becomes the hardest-to-
            # forget fact AND removes the only texts that could contradict
            # it. An entailment judge over (summary, sources) must ACCEPT
            # before the replacement happens; one re-summarize is allowed,
            # then the cluster stays intact for the next pass. Prevention-at-
            # the-write; snapshots are recovery-after.
            if self.faithfulness is not None:
                sources = "\n".join(f"- {m.text}" for m in cluster)
                verdict = ""
                with contextlib.suppress(Exception):
                    verdict = (self.faithfulness(summary, sources) or "").strip().upper()
                if not verdict.startswith("ACCEPT"):
                    with contextlib.suppress(Exception):
                        summary = self.summarizer(cluster)  # one retry
                        verdict = (self.faithfulness(summary, sources) or "").strip().upper()
                if not verdict.startswith("ACCEPT"):
                    rejected += 1
                    log.warning("consolidation rejected by faithfulness gate; "
                                "keeping %d source memories intact", len(cluster))
                    continue
            # Importance boost (idea #1): a memory distilled from N sources
            # outweighs any single one — consolidation is a stability op, not
            # just compression. Cap at 10 (the scale ceiling).
            base_imp = max(m.importance for m in cluster)
            boosted = min(10.0, base_imp + self.consolidation_boost * len(cluster))
            protected = {key: True for key in ("excluded", "application_excluded")
                         if any(m.metadata.get(key) for m in cluster)}
            if any(is_sensitive_memory(m, self.store) for m in cluster):
                protected["sensitive"] = True
            new = Memory(
                text=summary,
                type="semantic",
                importance=boosted,
                tags=sorted({t for m in cluster for t in m.tags}),
                source="reflection",
                metadata={"summarized_from": [m.id for m in cluster], **protected},
            )
            self.store.add(new)
            created.append(new.id)
            # A-MEM link inheritance (2026-08-19 #2): the consolidated memory
            # adopts its sources' links and links to each source, then the
            # graph densifies exactly where the store is condensing.
            with contextlib.suppress(Exception):
                inherited: dict[str, float] = {}
                for m in cluster:
                    inherited[m.id] = 1.0  # direct source links
                    for lid in self.store.get_links(m.id):
                        inherited.setdefault(lid, 0.9)
                top_m = int(getattr(self.store.settings, "link_top_m", 3) or 0)
                if top_m > 0 and inherited:
                    links = sorted(inherited.items(), key=lambda kv: -kv[1])[: top_m + len(cluster)]
                    self.store.set_links(new.id, links)
            for m in cluster:
                self.store.delete(m.id)
            summarized += len(cluster)
        self._rejected_summaries = rejected  # surfaced into ReflectionReport by run()
        return summarized, created

    def _dedup_pass(self) -> int:
        """Catch near-duplicates that slipped through on-write consolidation."""
        all_mems = [m for m in self.store.list_all(limit=10_000)
                    if m.is_retrievable() and not m.is_permanent]
        seen: list[Memory] = []
        removed = 0
        for m in all_mems:
            dup = False
            for s in seen:
                hits = self.store.vector_search(m.text, top_k=1)
                if hits and hits[0].score >= self.settings.dedup_cosine and hits[0].id == s.id:
                    # Merge m into s, then delete m.
                    self.consolidator._merge(s, m)  # noqa: SLF001
                    self.store.update(s)
                    self.store.delete(m.id)
                    removed += 1
                    dup = True
                    break
            if not dup:
                seen.append(m)
        return removed

    def _decay_pass(self) -> tuple[int, int]:
        """Soften importance of stale, never-accessed memories; delete those
        that fall below the floor (soft forgetting).

        Consolidated memories (idea #1) decay slower and survive to a lower
        floor — they're stability anchors distilled from many sources, so
        forgetting them undoes the consolidation work."""
        now = time.time()
        cutoff = now - self.decay_age_h * 3600
        decayed = 0
        deleted = 0
        for m in self.store.list_all(limit=10_000):
            if not m.is_retrievable() or m.is_permanent:
                continue
            if m.last_accessed > cutoff or m.access_count > 0:
                continue
            # Stability policy (idea #1): consolidated memories use a gentler
            # decay factor and a lower deletion floor than raw memories.
            if m.is_consolidated:
                factor = self.consolidated_decay_factor
                floor = self.consolidated_min_importance
            else:
                factor = self.decay_factor
                floor = self.min_importance
            new_imp = m.importance * factor
            if new_imp < floor:
                self.store.delete(m.id)
                self._invalidate_cache(m.id)  # 2026-09-01 #1: coherence
                deleted += 1
            else:
                m.importance = new_imp
                self.store.update(m)
                decayed += 1
        return decayed, deleted

    def _invalidate_cache(self, memory_id: str) -> None:
        """Answer-cache coherence hook (2026-09-01 #1). Best-effort."""
        with contextlib.suppress(Exception):
            from proharness.models.answer_cache import AnswerCache

            AnswerCache(self.store).invalidate_for_memory(memory_id)

    # ── clustering ───────────────────────────────────────────────────────────
    def _cluster(self, mems: list[Memory]) -> list[list[Memory]]:
        """Greedy cosine clustering. Fine for the small sets sleep-time handles."""
        if not mems:
            return []
        vecs = self.store.embedder.embed_batch([m.text for m in mems])
        clusters: list[list[Memory]] = []
        used = [False] * len(mems)
        for i, m in enumerate(mems):
            if used[i]:
                continue
            cluster = [m]
            used[i] = True
            for j in range(i + 1, len(mems)):
                if used[j]:
                    continue
                if _cosine(vecs[i], vecs[j]) >= self.cluster_cosine:
                    cluster.append(mems[j])
                    used[j] = True
            clusters.append(cluster)
        return clusters


def make_summarizer_from_llm(llm):
    """Build a summarizer callable from any LLMClient."""

    def summarize(cluster: list[Memory]) -> str:
        from proharness.models.base import Message

        joined = "\n".join(f"- ({m.type}) {m.text}" for m in cluster)
        prompt = (
            "Synthesize the following memories into ONE concise higher-level "
            "semantic fact. Drop ephemeral detail; keep durable knowledge. "
            "Reply with the fact only, no preamble.\n\n"
            f"{joined}\n\nFact:"
        )
        result = llm.chat([Message(role="user", content=prompt)], temperature=0.2, max_tokens=200)
        return result.message.content.strip()

    return summarize


def _cosine(a: list[float], b: list[float]) -> float:
    import math

    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def make_faithfulness_judge(llm) -> Callable[[str, str], str]:
    """Build a consolidation-faithfulness judge from a CHEAP client (the local
    tier — 2026-08-28 #2). Takes (summary, concatenated_sources); replies
    ACCEPT when every claim in the summary is entailed by the sources."""
    from proharness.agents.prompts import ENTAILMENT_PROMPT
    from proharness.models.base import Message

    def judge(summary: str, sources: str) -> str:
        result = llm.chat(
            [Message(role="user", content=ENTAILMENT_PROMPT.format(
                summary=summary[:2000], sources=sources[:4000]))],
            temperature=0.0, max_tokens=5,
        )
        return result.message.content.strip()

    return judge


def make_contradiction_judge(llm) -> Callable[[str, str], str]:
    """Build a belief-revision judge from a CHEAP client (the local tier —
    2026-08-20 #1). Takes (older_text, newer_text); replies SUPERSEDE when
    the newer statement contradicts or replaces the older one, else KEEP."""

    prompt = (
        "You are a temporal belief judge for a memory store. Given an OLDER\n"
        "and a NEWER statement, decide whether the newer one contradicts or\n"
        "replaces the older one (a correction, a move, a change of fact).\n\n"
        f"OLDER: {('{older}')}\n"
        f"NEWER: {('{newer}')}\n\n"
        "Reply with exactly one word: SUPERSEDE or KEEP.\nDecision:"
    )

    def judge(older: str, newer: str) -> str:
        from proharness.models.base import Message

        result = llm.chat(
            [Message(role="user", content=prompt.format(older=older[:800], newer=newer[:800]))],
            temperature=0.0, max_tokens=5,
        )
        return result.message.content.strip()

    return judge
