"""Park-style memory recall.

score = α·recency + β·importance + γ·relevance

(Park et al., "Generative Agents", 2023 — the canonical recency+relevance+
importance retrieval pattern.) Weights are configurable in Settings; the
documented default is equal weighting (1/1/1).

  • recency    = 0.99 ** hours_since_last_access, then min-max normalized
                 across the candidate set so the score stays in [0,1].
  • importance = stored 1..10 salience, normalized to [0,1].
  • relevance  = backend query-match signal (cosine or calibrated hybrid score).

We also bump `last_accessed` / `access_count` for memories that surface, so
recency reflects actual usefulness over time. By default these touches are
*deferred* into the store's touch buffer and flushed once per turn by the
AgentLoop — recall stays a pure read on the inference hot path (idea #3).
Recency is hourly-granular (`0.99 ** hours`), so sub-second deferral changes
nothing mathematically.
"""
from __future__ import annotations

import contextlib
import json
import math
import time

from hunter_gatherer.config import Settings
from hunter_gatherer.memory.schemas import RecallHit
from hunter_gatherer.memory.store import MemoryStore

_HOURS = 3600.0
_DECAY = 0.99  # per hour
# Half-Life Regression constants (2026-08-31 #1): H0 chosen so θ=0 reproduces
# _DECAY ** hours exactly. Settles & Meeder, KDD 2016.
_HLR_H0 = math.log(2) / math.log(1.0 / _DECAY)  # ≈ 68.97 hours


def _halflife_features(payload_or_memory) -> list[float]:
    """Per-memory features x_i for h_i = H0 · 2**(θ·x_i): log2(access_count+1),
    importance/10, is-episodic, is-consolidated (reflection-sourced)."""
    acc = int(payload_or_memory.get("access_count", 0)
              if isinstance(payload_or_memory, dict)
              else getattr(payload_or_memory, "access_count", 0) or 0)
    imp = float(payload_or_memory.get("importance", 5.0)
                if isinstance(payload_or_memory, dict)
                else getattr(payload_or_memory, "importance", 5.0)) / 10.0
    typ = (payload_or_memory.get("type") if isinstance(payload_or_memory, dict)
           else getattr(payload_or_memory, "type", "semantic")) or "semantic"
    src = (payload_or_memory.get("source") if isinstance(payload_or_memory, dict)
           else getattr(payload_or_memory, "source", "")) or ""
    return [
        math.log2(acc + 1.0),
        imp,
        1.0 if typ == "episodic" else 0.0,
        1.0 if src == "reflection" else 0.0,
    ]


def _halflife_theta(settings) -> list[float]:
    """The fitted θ vector (zeros = exact legacy curve). Read from the
    learned-params cache the loop hydrates onto settings; a plain attribute
    set by hydration or the operator also works."""
    raw = getattr(settings, "recall_halflife_theta", None)
    if isinstance(raw, (list, tuple)) and len(raw) == 4:
        return [float(v) for v in raw]
    if isinstance(raw, str) and raw.strip().startswith("["):
        with contextlib.suppress(Exception):
            parsed = json.loads(raw)
            if isinstance(parsed, list) and len(parsed) == 4:
                return [float(v) for v in parsed]
    return [0.0, 0.0, 0.0, 0.0]


def _half_life(payload: dict, theta: list[float]) -> float:
    x = _halflife_features(payload)
    return _HLR_H0 * 2.0 ** sum(t * xi for t, xi in zip(theta, x, strict=True))


def _fuse(primary: list, variants: list[list]) -> list:
    """Union candidate pools by id; per memory keep the highest-scoring hit
    (relevance = max over query variants). Order follows best score."""
    if not variants:
        return list(primary)
    best: dict[str, tuple] = {}
    for hit in list(primary) + [h for pool in variants for h in pool]:
        if hit.id not in best or hit.score > best[hit.id][0]:
            best[hit.id] = (hit.score, hit)
    return [h for _s, h in sorted(best.values(), key=lambda t: -t[0])]


def recall(
    store: MemoryStore,
    query: str,
    top_k: int | None = None,
    *,
    filters: dict | None = None,
    settings: Settings | None = None,
    touch: bool = True,
    defer_touch: bool = True,
    extra_queries: list[str] | None = None,
    rewrite_query: str | None = None,
    ranking: str = "context",
) -> list[RecallHit]:
    """Return relevant memories with explicit ranking intent.

    ``context`` preserves Park recency/importance/relevance weighting.
    ``relevance`` orders direct query matches without recency/importance boosts
    or inferred linked-neighbor expansion; use it for focused factual lookup.
    Relevance scores describe matching, not confidence or truth.

    When ``touch`` is True, surfaced memories have their last_accessed /
    access_count bumped. By default ``defer_touch=True`` buffers those bumps
    (idea #3: keep recall a pure read); the AgentLoop flushes once per turn.
    Set ``defer_touch=False`` for callers without a loop (e.g. ad-hoc CLI
    searches) so the bump is applied immediately.

    ``extra_queries`` (2026-08-23 #2 — HyDE): additional query variants (e.g.
    a hypothetical answer drafted by the local tier) whose candidate pools
    fuse with the primary's — union of ids, relevance = max over variants.
    A variant can only surface additional candidates, never hide existing
    ones, so the hypothetical is *generated, not trusted*.

    ``rewrite_query`` (2026-09-17 #2): the contextualized rewrite of a
    follow-up — fused like the other variants, but hits it surfaces carry
    ``via_rewrite=True`` so the usage flywheel can measure follow-up
    grounding separately from HyDE widening."""
    if ranking not in {"context", "relevance"}:
        raise ValueError("ranking must be context or relevance")
    settings = settings or store.settings
    k = top_k or settings.recall_topk
    mode = getattr(settings, "recall_superseded_mode", "filter")
    include_superseded = mode == "downrank"
    historical = {"include_superseded": True} if include_superseded else {}
    # Over-fetch a bit so normalization has a real distribution, then trim.
    fetch_k = max(k * 3, k)
    primary = store.vector_search(query, top_k=fetch_k, filters=filters, **historical)
    variant_pools = [store.vector_search(q, top_k=fetch_k, filters=filters, **historical)
                     for q in (extra_queries or [])]
    rewrite_pool = (store.vector_search(rewrite_query, top_k=fetch_k, filters=filters, **historical)
                    if rewrite_query else [])
    rewrite_won = {h.id for h in rewrite_pool}
    raw = _fuse(primary, variant_pools + ([rewrite_pool] if rewrite_pool else []))

    if not raw:
        return []

    now = time.time()
    # Raw sub-scores. Recency uses the usage-fitted forgetting curve
    # (2026-08-31 #1): p = 2**(−Δt/h_i) with h_i = H0 · 2**(θ·x_i). θ = 0
    # (default) gives h_i = H0 = 68.97h, which reproduces the old flat
    # 0.99**hours curve EXACTLY (ln2/ln(100/99)) — the same no-op safety
    # MMR's λ=1.0 established. A fitted θ makes frequently-used facts
    # (x_i grows with log access_count, importance, type, consolidation)
    # decay slower than touched-once ones — the docstring's
    # "recency reflects actual usefulness over time" finally enters the math.
    theta = _halflife_theta(settings)
    recencies = [
        2.0 ** (-(now - h.payload.get("last_accessed", h.payload.get("created_at", now)))
                / _HOURS / _half_life(h.payload, theta))
        for h in raw
    ]
    importances = [float(h.payload.get("importance", 5.0)) / 10.0 for h in raw]
    relevances = [max(0.0, h.score) for h in raw]

    # Adaptive retrieval gate (2026-08-31 #2): drop hits whose relevance
    # sub-score falls below the floor BEFORE Park scoring / MMR — a floor
    # never adds candidates, and rerank/MMR operate above it. Default 0.0 =
    # off (fitted by `memory tune`, not guessed).
    floor = float(getattr(settings, "recall_relevance_floor", 0.0) or 0.0)
    if floor > 0.0:
        kept = [(h, rec, imp, rel) for h, rec, imp, rel in zip(raw, recencies, importances, relevances, strict=True)
                if rel >= floor]
        if kept:
            raw, recencies, importances, relevances = (
                [t[0] for t in kept], [t[1] for t in kept],
                [t[2] for t in kept], [t[3] for t in kept],
            )
        else:
            return []  # nothing clears the floor → grounded abstention upstream

    # Min-max normalize recency into [0,1] across the candidate set — BUT only
    # when the spread is meaningful. When memories were all accessed within a
    # narrow window, raw recencies are nearly identical and min-max would
    # amplify noise into a 0/1 split that dominates the score. We require the
    # spread to exceed a small fraction of the max before normalizing;
    # otherwise treat recency as uniform (≈1.0) so importance/relevance decide.
    rmin, rmax = min(recencies), max(recencies)
    rspan = rmax - rmin
    _RECENCY_MIN_SPREAD = 0.02  # below ~2% spread, treat as same recency
    if rspan > _RECENCY_MIN_SPREAD:
        recencies = [(r - rmin) / rspan for r in recencies]
    else:
        recencies = [1.0] * len(recencies)

    α, β, γ = settings.recency_weight, settings.importance_weight, settings.relevance_weight
    norm = α + β + γ or 1.0

    hits: list[RecallHit] = []
    for h, rec, imp, rel in zip(raw, recencies, importances, relevances, strict=True):
        score = rel if ranking == "relevance" else (α * rec + β * imp + γ * rel) / norm
        mem = store.get(h.id)
        if mem is None or not mem.is_retrievable(filters, include_superseded=include_superseded):
            continue
        # Belief revision (2026-08-20 #1): superseded memories are filtered
        # out of the ranked block (or downranked hard, per config) so stale
        # facts stop competing with — and sometimes outranking — their
        # replacements. Downrank keeps them reachable for the stale_requested
        # flywheel to measure wrong supersessions.
        if mem.is_superseded:
            score *= 0.25  # downrank
        hits.append(RecallHit(memory=mem, score=score, recency=rec, importance=imp,
                              relevance=rel, via_rewrite=mem.id in rewrite_won))

    hits.sort(reverse=True)

    # A-MEM one-hop link expansion (2026-08-19 #2): linked neighbors of the
    # top-2 Park-ranked hits enter the candidate pool at seed × expansion
    # factor (< 1, so a direct hit always outranks its own seed's expansion),
    # then the pool re-ranks and the top-k surface. This is the orthogonal
    # retrieval axis — association alongside query similarity — surfacing
    # related-but-differently-worded memories vector search misses. Expanded
    # hits are flagged so the recall_usage flywheel measures whether expansion
    # earns its tokens; drop link_expansion_factor to 0 to disable.
    factor = float(getattr(settings, "link_expansion_factor", 0.8) or 0.0)
    top_m = int(getattr(settings, "link_top_m", 3) or 0)
    if ranking == "context" and factor > 0 and top_m > 0 and hits:
        seen = {h.memory.id for h in hits}
        for seed in hits[:2]:
            for lid in (store.get_links(seed.memory.id) or [])[:top_m]:
                if lid in seen:
                    continue
                mem = store.get(lid)
                if mem is None or not mem.is_retrievable(filters, include_superseded=include_superseded):
                    continue
                seen.add(lid)
                rec = _DECAY ** ((now - mem.last_accessed) / _HOURS)
                hits.append(RecallHit(
                    memory=mem, score=seed.score * factor * (0.25 if mem.is_superseded else 1.0),
                    recency=rec,
                    importance=mem.importance / 10.0,
                    relevance=seed.relevance * factor, via_link=True,
                ))
        hits.sort(reverse=True)

    # MMR selection (2026-08-28 #3): the pool above is deliberately widened
    # three ways (3×k over-fetch, HyDE fusion, A-MEM expansion — whose
    # neighbors are similar to their seeds by construction), and insert-time
    # dedup only catches cosine ≥ dedup_cosine, so paraphrase variants can
    # occupy 3 of 5 slots together. Carbonell & Goldstein's diversity
    # re-ranking composes the block from the already-scored pool: iteratively
    # pick argmax λ·park − (1−λ)·max-sim-to-selected. λ=1.0 reproduces greedy
    # top-k exactly; MMR only reorders within the fused pool, never adds
    # candidates — the widening-not-trusting conservatism HyDE established.
    lam = float(getattr(settings, "recall_mmr_lambda", 1.0))
    # Focused lookup keeps the strongest matches in score order; diversity is
    # useful for broad context but can displace direct answers with distractors.
    top = (_select_mmr(hits, k, lam, store)
           if ranking == "context" and lam < 1.0 and len(hits) > k else hits[:k])
    if touch:
        for hit in top:
            store.touch(hit.memory.id, defer=defer_touch)
    return top


def _select_mmr(hits: list[RecallHit], k: int, lam: float, store) -> list[RecallHit]:
    """MMR re-selection over the top of the fused pool (bounded at 30
    candidates — one embed_batch, milliseconds on CPU)."""
    pool = hits[:30]
    if len(pool) <= k:
        return pool
    with contextlib.suppress(Exception):
        vecs = store.embedder.embed_batch([h.memory.text for h in pool])

        def cos(a: list[float], b: list[float]) -> float:
            dot = sum(x * y for x, y in zip(a, b, strict=True))
            na = math.sqrt(sum(x * x for x in a)) or 1.0
            nb = math.sqrt(sum(y * y for y in b)) or 1.0
            return dot / (na * nb)

        selected: list[int] = [0]  # highest Park score anchors the block
        rest = list(range(1, len(pool)))
        while rest and len(selected) < k:
            best_j, best_val = 0, float("-inf")
            for j, idx in enumerate(rest):
                max_sim = max(cos(vecs[idx], vecs[s]) for s in selected)
                val = lam * pool[idx].score - (1.0 - lam) * max_sim
                if val > best_val:
                    best_val, best_j = val, j
            selected.append(rest.pop(best_j))
        return [pool[i] for i in selected]
    return hits[:k]  # embedding failed → greedy


def format_recall(hits: list[RecallHit]) -> str:
    """Render hits for inclusion in an agent's context.

    Each line carries a short ``[mem:xxxxxx]`` label (2026-08-14 #1): the
    recall-usage loop scans the turn's messages for these labels to learn
    which surfaced memories were actually *used*, which drives the Park-weight
    sweep (``hunt memory tune``). Cite the label when referencing a
    memory in an answer."""
    if not hits:
        return ""
    lines = []
    for h in hits:
        lines.append(
            f"- [mem:{h.memory.id[:6]}] [{h.memory.type}] (s={h.score:.2f} rec={h.recency:.2f} "
            f"imp={h.importance:.2f} rel={h.relevance:.2f}) {h.memory.text}"
        )
    return "\n".join(lines)
