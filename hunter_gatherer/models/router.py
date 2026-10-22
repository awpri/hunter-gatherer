"""Cost-aware model router.

Implements single-shot routing (RouteLLM insight: cascades compound latency
because they issue multiple sequential calls; routing picks ONE model per
query before any model call). Two cheap layers, evaluated in order:

  1. Rule layer — regex/length heuristics catch trivially-local queries with
     zero embedding cost.
  2. Embedding-similarity layer — cosine between the query embedding and a
     small set of "needs-cloud" exemplars; escalate iff score ≥ α.

α (router_alpha) is the cost/quality knob the user tunes. Lower → cloud used
more aggressively. The local_only flag forces local regardless.

The router also memoizes a per-query decision so repeated turns in a
conversation don't re-embed.
"""
from __future__ import annotations

import contextlib
import logging
import re
from dataclasses import dataclass
from typing import Literal

from hunter_gatherer.config import Settings
from hunter_gatherer.cost import CostTracker
from hunter_gatherer.models.base import LLMClient

Tier = Literal["local", "cheap", "cloud"]

# Sentinel for the lazily-loaded trained classifier: distinguishes "not yet
# looked" from "looked and absent".
_UNLOADED = object()

# A small hand-picked set of exemplars that signal "this probably needs a
# strong cloud model." Tunable. These are embedded once and cached.
_NEEDS_CLOUD_EXEMPLARS = [
    "Design a distributed system for ordering and consistency across regions.",
    "Refactor this large codebase to introduce a new abstraction safely.",
    "Write a complex SQL query with multiple window functions and CTEs.",
    "Analyze this legal contract for risky clauses and summarize obligations.",
    "Debug a subtle concurrency race condition in my async code.",
    "Plan a multi-week project and break it into trackable milestones.",
    "Write production code with thorough error handling and tests.",
    "Compare architectural tradeoffs of several approaches in depth.",
]

log = logging.getLogger(__name__)


class _WithAlpha:
    """Settings view with a bumped router_alpha (hysteresis, 2026-09-09 #1);
    avoids mutating the shared settings object."""

    def __init__(self, base, alpha):
        self._base = base
        self.router_alpha = alpha

    def __getattr__(self, name):
        return getattr(self._base, name)


def _with_alpha(settings, alpha):
    return _WithAlpha(settings, alpha)


_TRIVIAL_PATTERNS = [
    re.compile(r"^(hi|hello|hey|yo|sup|ok|okay|thanks|thank you|cool|got it|yes|no|yep|nope)[!.\s]*$", re.I),
    re.compile(r"^(what (time|day)|who are you|what can you do)\b", re.I),
]


@dataclass
class Route:
    tier: Tier
    model: str
    reason: str  # human-readable explanation for observability
    # Embedding-similarity score in [0,1] that produced this route (None when
    # the rule layer or heuristic decided without an embedder). Recorded on
    # router decisions so the α-sweep can regress score → outcome.
    score: float | None = None


@dataclass
class RouteContext:
    """Session features for routing (2026-09-09 #1). The one feature axis the
    learned stack never had: queries were routed in a vacuum, so a 3-token
    follow-up mid-hard-task was indistinguishable from a greeting. ctx=None
    reproduces the blind behavior exactly (no-op safety)."""

    prev_tier: str = ""       # "" (first turn) | local | cheap | cloud
    turn_index: int = 0
    session_cloud_fraction: float = 0.0  # escalated turns / total so far


class RouterClient:
    """Picks the cheapest model likely to handle a query well, then delegates."""

    def __init__(
        self,
        *,
        local: LLMClient,
        cheap: LLMClient | None = None,
        cloud: LLMClient | None = None,
        settings: Settings,
        embedder=None,  # hunter_gatherer.memory.embeddings.Embedder; injected to avoid cycle
        store=None,  # MemoryStore; enables the self-growing exemplar set (2026-08-11 #1)
    ):
        self.local = local
        self.cheap = cheap
        self.cloud = cloud
        self.settings = settings
        self._embedder = embedder
        self._store = store
        self._exemplar_vecs: list[list[float]] | None = None
        self._exemplar_ids: list[str | None] | None = None
        # Trained classifier head (2026-08-19 #1): lazy-loaded from the store,
        # consulted before the kNN layer with a confidence-gated fallback.
        self._classifier = _UNLOADED
        # LRU on the (last user message) text to avoid re-embedding within a turn.
        self._route_cache: dict[str, Route] = {}
        # Circuit breakers (2026-09-07 #2): provider key → (consecutive
        # failures, breaker-opened ts). 3 failures opens for 60s.
        self._breakers: dict = {}

    # ── public ───────────────────────────────────────────────────────────────
    @property
    def model(self) -> str:
        return self.local.model  # nominal; actual model varies per call

    def route(self, query: str, ctx: "RouteContext | None" = None) -> Route:
        """Decide which tier handles `query`. Cheap, no LLM call.

        that starves the context budget)."""
        # Cache key includes the conversation's prev tier: a cached blind
        # decision must never shadow a ctx-dependent one (and vice versa).
        cache_key = query if ctx is None or not ctx.prev_tier else f"{ctx.prev_tier}|{query}"
        if cached := self._route_cache.get(cache_key):
            return cached
        route = self._decide(query, ctx)
        self._route_cache[cache_key] = route
        return route

    def select(self, route: Route) -> LLMClient:
        """Return the concrete client for a route, downgrading gracefully."""
        if route.tier == "local" or self.settings.local_only:
            return self.local
        if route.tier == "cheap":
            return self.cheap or self.local
        if route.tier == "cloud":
            return self.cloud or self.cheap or self.local
        return self.local

    # chat() delegates to the selected client; satisfies LLMClient protocol.
    def chat(self, messages, tools=None, temperature=0.2, max_tokens=None):
        query = _last_user_text(messages)
        for client in self._ladder(self.route(query)):
            try:
                return client.chat(messages, tools=tools, temperature=temperature,
                                   max_tokens=max_tokens)
            except Exception as e:
                self._note_failure(client, e)
        raise RuntimeError("all tiers failed (failure ladder exhausted)")

    def stream(self, messages, tools=None, temperature=0.2):
        query = _last_user_text(messages)
        for client in self._ladder(self.route(query)):
            try:
                yield from client.stream(messages, tools=tools, temperature=temperature)
                return
            except Exception as e:
                self._note_failure(client, e)
        raise RuntimeError("all tiers failed (failure ladder exhausted)")

    # ── failure ladder (2026-09-07 #2) ─────────────────────────────────────
    def _ladder(self, route: Route):
        """Yield the route's client, then each tier ONE step down the
        cloud→cheap→local ladder on failure — degrading, never escalating
        (quality-conservative). A tripped circuit breaker skips straight past
        a downed provider without paying its timeout. The happy path yields
        exactly one client and costs nothing extra."""
        client = self.select(route)
        seen: list = [client]
        yield client
        order = ["cloud", "cheap", "local"]
        try:
            idx = order.index(route.tier)
        except ValueError:
            return
        for tier in order[idx + 1:]:
            nxt = self.select(Route(tier, "", "failure-ladder degradation"))  # type: ignore[arg-type]
            if nxt not in seen and not self._breaker_open(nxt):
                seen.append(nxt)
                yield nxt

    def _note_failure(self, client, exc) -> None:
        """Count provider failures; 3 consecutive opens the breaker for 60s."""
        key = getattr(client, "base_url", None) or getattr(client, "model", "?")
        fails, _ts = self._breakers.get(key, (0, 0.0))
        self._breakers[key] = (fails + 1, _ts)
        if fails + 1 >= 3:
            import time as _time

            self._breakers[key] = (fails + 1, _time.time())
            log.warning("circuit breaker OPEN for %s (60s)", key)

    def _breaker_open(self, client) -> bool:
        key = getattr(client, "base_url", None) or getattr(client, "model", "?")
        fails, opened = self._breakers.get(key, (0, 0.0))
        if fails < 3:
            return False
        import time as _time

        if _time.time() - opened > 60.0:  # half-open: probe again
            self._breakers[key] = (0, 0.0)
            return False
        return True

    # ── decision logic ───────────────────────────────────────────────────────
    def _decide(self, query: str, ctx: "RouteContext | None" = None) -> Route:
        s = self.settings

        # 0. Hard override.
        if s.local_only or (self.cheap is None and self.cloud is None):
            return Route("local", self.local.model, "local-only mode or no cloud configured")

        # 1. Rule layer: trivially short / greetings / simple lookups —
        # SUPPRESSED on follow-up carry-over (2026-09-09 #1): when the
        # conversation's previous tier was cloud/cheap, a short "why?" is a
        # follow-up mid-hard-task, not a greeting.
        stripped = query.strip()
        prev_escalated = ctx is not None and ctx.prev_tier in ("cloud", "cheap")
        if not prev_escalated and len(stripped) <= s.router_trivial_max_chars \
                and any(p.match(stripped) for p in _TRIVIAL_PATTERNS):
            return Route("local", self.local.model, "rule: trivial (greeting/short)")

        # If there's no embedder, fall back to a length+keyword heuristic.
        if self._embedder is None:
            return self._heuristic_route(query)

        # 2. Embedding-similarity layer.
        score = self._cloud_score(query)

        # Session hysteresis (2026-09-09 #1): a mid-session tier switch must
        # cross α by the margin (flapping starves the context budget — a
        # local turn's 4k window trims history the next cloud turn needs).
        # Effective thresholds; margin 0 = the blind per-query behavior.
        margin = float(getattr(s, "router_switch_margin", 0.0) or 0.0)
        if ctx is not None and ctx.prev_tier == "local" and margin > 0:
            s = _with_alpha(s, s.router_alpha + margin)  # escalate later
        # (de-escalating mid-session needs no extra margin — cheaper is safe)

        # 2b. Trained classifier head (2026-08-19 #1): consult the fitted
        # logistic router first, falling back to the kNN threshold whenever
        # the head is missing / under-trained / low-confidence — the upgrade
        # can only help or no-op.
        p = self._classifier_prob(query, score)
        if p is not None:
            if p >= s.router_alpha:
                if p < s.router_alpha + 0.2 and self.cheap is not None:
                    return self._with_arm(Route("cheap", self.cheap.model,
                                                f"classifier p={p:.2f} ≥ α", score=score))
                target = self.cloud or self.cheap or self.local
                tier = "cloud" if self.cloud else "cheap"
                return self._with_arm(Route(tier, target.model,
                                            f"classifier p={p:.2f} ≥ α", score=score))
            return Route("local", self.local.model, f"classifier p={p:.2f} < α", score=score)

        if score >= s.router_alpha:
            # Prefer cheap tier when the score is in the middle band.
            if score < s.router_alpha + 0.2 and self.cheap is not None:
                return self._with_arm(Route("cheap", self.cheap.model,
                                            f"embed score {score:.2f} ≥ α", score=score))
            target = self.cloud or self.cheap or self.local
            tier = "cloud" if self.cloud else "cheap"
            return self._with_arm(Route(tier, target.model,
                                        f"embed score {score:.2f} ≥ α", score=score))
        return Route("local", self.local.model, f"embed score {score:.2f} < α", score=score)

    def _with_arm(self, route: Route) -> Route:
        """Within-tier model selection (2026-09-01 #3): when the tier has
        multiple configured candidates, the UCB1 bandit picks the arm
        (Route.model carries the choice). Singleton lists → pinned behavior."""
        with contextlib.suppress(Exception):
            from hunter_gatherer.models.bandit import ModelBandit

            bandit = ModelBandit(self._store, self.settings) if self._store else None
            if bandit is not None:
                chosen = bandit.select(route.tier, route.model)
                if chosen != route.model:
                    route = Route(route.tier, chosen, route.reason + " (bandit)", score=route.score)
        return route

    def _classifier_prob(self, query: str, score: float) -> float | None:
        """P(escalate) from the trained head, or None to use the kNN fallback.
        Loads the persisted head lazily (once); None when absent, under-
        trained, or |p − 0.5| is below the confidence floor."""
        if self._classifier is _UNLOADED:
            self._classifier = None
            if self._store is not None:
                with contextlib.suppress(Exception):
                    from hunter_gatherer.models.router_classifier import load_classifier

                    self._classifier = load_classifier(
                        self._store,
                        min_rows=getattr(self.settings, "router_classifier_min_rows", 50),
                    )
        clf = self._classifier
        if clf is None:
            return None
        from hunter_gatherer.models.router_classifier import extract_features

        with contextlib.suppress(Exception):
            p = clf.predict(extract_features(query, score))
            if abs(p - 0.5) >= getattr(self.settings, "router_classifier_confidence_floor", 0.15):
                return p
        return None

    def _heuristic_route(self, query: str) -> Route:
        """Length + keyword heuristic when no embedder is available."""
        if len(query) > 400 or any(
            kw in query.lower()
            for kw in ("design", "architect", "refactor", "debug", "analyze", "compare", "plan")
        ):
            target = self.cloud or self.cheap or self.local
            return Route("cloud" if self.cloud else "cheap", target.model, "heuristic: complex")
        if self.cheap is not None and len(query) > 120:
            return Route("cheap", self.cheap.model, "heuristic: medium length")
        return Route("local", self.local.model, "heuristic: simple")

    def _cloud_score(self, query: str) -> float:
        """Cosine similarity of query to the nearest 'needs-cloud' exemplar,
        mapped to [0, 1]."""
        vecs = self._exemplars()
        q = self._embedder.embed(query)
        best = max(_cosine(q, v) for v in vecs)
        # Cosine for sentence embeddings tends to land in [0.3, 0.9]; rescale
        # into a usable [0,1] decision score.
        return max(0.0, min(1.0, (best - 0.3) / 0.6))

    def _exemplars(self) -> list[list[float]]:
        """Embedded exemplar vectors. Loads from the persisted set when a store
        is wired (seeding from the constant on first run — 2026-08-11 #1);
        falls back to the constant otherwise. Cached until invalidated."""
        vecs, _ids = self._exemplars_with_ids()
        return vecs

    def _exemplars_with_ids(self) -> tuple[list[list[float]], list[str | None]]:
        if self._exemplar_vecs is None or self._exemplar_ids is None:
            assert self._embedder is not None
            if self._store is not None:
                with contextlib.suppress(Exception):
                    self._store.seed_router_exemplars(_NEEDS_CLOUD_EXEMPLARS)
                    rows = self._store.list_router_exemplars()
                    texts = [r[1] for r in rows]
                    ids: list[str | None] = [r[0] for r in rows]
                    self._exemplar_vecs = [self._embedder.embed(t) for t in texts]
                    self._exemplar_ids = ids
                    return self._exemplar_vecs, self._exemplar_ids
            # No store (or store error): the static constant.
            self._exemplar_vecs = [self._embedder.embed(t) for t in _NEEDS_CLOUD_EXEMPLARS]
            self._exemplar_ids = [None] * len(self._exemplar_vecs)
        return self._exemplar_vecs, self._exemplar_ids

    def invalidate_exemplars(self) -> None:
        """Drop the cached exemplar embeddings so the next route() re-loads
        from the store (called after a promotion)."""
        self._exemplar_vecs = None
        self._exemplar_ids = None
        self._route_cache.clear()  # routes may differ under the new reference set

    def reload_classifier(self) -> None:
        """Drop the cached classifier head so the next route() re-loads it
        from the store (called after `hunter_gatherer router train`)."""
        self._classifier = _UNLOADED
        self._route_cache.clear()  # routes may differ under the new head

    def promote_exemplar(self, query: str) -> bool:
        """Promote a confirmed-good cloud/cheap-routed query into the persisted
        exemplar set (2026-08-11 #1 — the self-growing kNN router). Returns
        True if added; False when a near-duplicate matched (its match_count is
        bumped instead, driving eviction bias). No-op without store/embedder."""
        if self._embedder is None or self._store is None:
            return False
        query = query.strip()
        if not query:
            return False
        vecs, ids = self._exemplars_with_ids()
        if not vecs:
            added = self._store.add_router_exemplar(query, cap=self.settings.router_exemplar_max)
            self.invalidate_exemplars()
            return added is not None
        q = self._embedder.embed(query)
        sims = [_cosine(q, v) for v in vecs]
        best_i = sims.index(max(sims))
        floor = getattr(self.settings, "router_exemplar_promote_cosine", 0.90)
        if sims[best_i] >= floor:
            matched_id = ids[best_i] if best_i < len(ids) else None
            if matched_id:
                with contextlib.suppress(Exception):
                    self._store.bump_router_exemplar(matched_id)
            return False
        added = self._store.add_router_exemplar(query, cap=self.settings.router_exemplar_max)
        self.invalidate_exemplars()
        return added is not None


def _cosine(a: list[float], b: list[float]) -> float:
    import math

    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)


def _last_user_text(messages) -> str:
    for m in reversed(messages):
        if getattr(m, "role", None) == "user":
            return m.content or ""
    return ""


def build_router(settings: Settings, cost: CostTracker | None = None, embedder=None, store=None) -> RouterClient:
    """Construct the default router from settings. Importing the embedder is
    deferred so the router can be used without the memory subsystem. ``store``
    wires the self-growing exemplar set (2026-08-11 #1)."""
    from hunter_gatherer.models.cloud import make_cloud_client, make_local_client_from_settings

    local = make_local_client_from_settings(settings, cost=cost)
    cheap = make_cloud_client(settings.cheap_model, settings, cost=cost) if settings.has_openai else None
    cloud = make_cloud_client(settings.cloud_model, settings, cost=cost) if settings.has_openai else None
    return RouterClient(local=local, cheap=cheap, cloud=cloud, settings=settings, embedder=embedder, store=store)
