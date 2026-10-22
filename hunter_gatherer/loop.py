"""AgentLoop: wires router + memory + tools + skills + sub-agents.

This is the top-level orchestrator a CLI command drives. It owns:
  - the cost tracker (session-scoped)
  - the router (picks model per turn)
  - the memory store (NAS-backed)
  - the tool registry + RAG-over-tools selector
  - the orchestrator (for spawn_agent)
  - the skill set (loaded from SKILL.md dirs)

A single .run(user_message) call returns the agent's final answer, with tool
events streamable via an on_event callback.
"""
from __future__ import annotations

import contextlib
import json
import logging
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from hunter_gatherer.memory.reflect import ReflectionReport

from hunter_gatherer.agents.agent import Agent, AgentResult
from hunter_gatherer.agents.orchestrator import Orchestrator, SubTask
from hunter_gatherer.agents.prompts import DEFAULT_SYSTEM, skill_system
from hunter_gatherer.config import Settings
from hunter_gatherer.cost import CostTracker
from hunter_gatherer.memory.consolidate import Consolidator
from hunter_gatherer.memory.embeddings import Embedder, get_embedder
from hunter_gatherer.memory.store import MemoryStore
from hunter_gatherer.models.router import build_router
from hunter_gatherer.skills import Skill, default_skill_dirs, discover_skills
from hunter_gatherer.tools.base import ToolContext
from hunter_gatherer.tools.builtin import default_registry
from hunter_gatherer.tools.selector import ToolSelector

log = logging.getLogger(__name__)


@dataclass
class LoopEvent:
    """A UI event emitted by the loop during a run.

    Kinds (open enum — new kinds may be added; callers should treat unknown
    kinds gracefully):
      - turn_start   : {query}                 — bracketing: a turn began
      - turn_end     : {answer, stopped_reason} — bracketing: turn finished
      - route        : {tier, model, reason}   — router decision this turn
      - skill        : {name}                  — a skill activated
      - tool         : {name, args, result_preview} — a tool call completed
      - token        : {delta}                 — final-answer token stream
      - reflection   : {summarized,deduped,decayed,deleted,created,trigger}
      - autotune     : {old_alpha,new_alpha,recommended,n_decisions,reason}
      - cost         : {tokens, usd}           — session running totals
    """
    kind: str
    data: dict = field(default_factory=dict)


class AgentLoop:
    """Top-level harness. Construct once; call .run() per turn."""

    def __init__(
        self,
        settings: Settings | None = None,
        *,
        embedder: Embedder | None = None,
        cost: CostTracker | None = None,
        workdir: str = ".",
    ):
        self.settings = settings or Settings()
        self.cost = cost or CostTracker(settings=self.settings)
        self.embedder = embedder or get_embedder(self.settings)
        self.workdir = workdir

        # Memory store (SQLite source-of-truth + Qdrant or in-memory vectors).
        self.store = MemoryStore(self.settings, embedder=self.embedder)

        self._router_alpha_pinned = "router_alpha" in self.settings.model_fields_set

        # Closed-loop persistence: rehydrate the newest tuned values (router α,
        # Park weights) unless explicitly pinned via env/kwarg — env > stored >
        # default, so tuning survives restarts and HG_* overrides win.
        with contextlib.suppress(Exception):
            from hunter_gatherer.memory.params import hydrate_learned_params

            hydrate_learned_params(self.settings, self.store)

        # Model router. Uses the same embedder for the similarity layer; the
        # store wires the self-growing exemplar set (2026-08-11 #1).
        self.router = build_router(
            self.settings, cost=self.cost, embedder=_EmbedderShim(self.embedder), store=self.store
        )

        # Tools + selector.
        self.registry = default_registry()
        # Selector carries the store so it can apply the procedural boost
        # (idea #3): lift tools from matching procedural memories into the ranking.
        self.selector = ToolSelector(registry=self.registry, embedder=self.embedder, store=self.store)

        # Orchestrator for spawn_agent. Store is passed so workers can be
        # memory-primed (idea #2): recall the objective's top-k memories and
        # prepend as shared context.
        self.orchestrator = Orchestrator(self.router, self.registry, self.selector, store=self.store)

        # Skills.
        self.skills = discover_skills(*default_skill_dirs())

        # Per-session working memory (idea #2). The long-term MemoryStore stays
        # shared (cross-session recall is the point); only per-conversation
        # history is isolated. ``self.history`` is kept as an alias to the
        # default session for CLI back-compat.
        from hunter_gatherer.sessions import DEFAULT_SESSION, SessionStore

        self.sessions = SessionStore(
            on_evict=self._summarize_evicted if self.settings.session_summarize_on_evict else None
        )
        self._default_session = DEFAULT_SESSION

        # Dynamic-tools signature (2026-08-12 #1 → generalized 2026-09-17 #1):
        # tracks the pipeline set AND inbound-MCP tool set so the registry is
        # rebuilt only when something actually changed.
        self._pipeline_sig: tuple | None = None
        # Inbound MCP manager (2026-09-17 #1): built lazily on first refresh
        # when HG_MCP_SERVERS is configured; None otherwise (no-op).
        self._mcp_manager = None
        # HyDE per-query cache (2026-08-23 #2): query → hypothetical answer
        # ("" = negative result, cached so it isn't re-asked per turn).
        self._hyde_cache: dict[str, str] = {}
        # Per-session last router-decision row id (2026-08-31 #3): the join
        # handle explicit feedback attaches to.
        self._last_decision_ids: dict[str, int | None] = {}
        # Session route state (2026-09-09 #1): prev tier + turn count per
        # session, feeding RouteContext so follow-ups carry the conversation's
        # escalation instead of routing blind.
        self._session_route_state: dict[str, tuple[str, int]] = {}
        # Query-rewrite per-session cache (2026-09-17 #2): last rewrite per
        # session, so a follow-up turn doesn't re-ask the local tier for the
        # same history window repeatedly.
        self._rewrite_cache: dict[str, str] = {}

    def last_decision_id(self, session_id: str | None = None) -> int | None:
        """The router-decision row id of the most recent turn in a session —
        what POST /api/feedback and hg_feedback join their rating to."""
        sid = self._default_session if session_id is None else session_id
        if sid in self._last_decision_ids:
            return self._last_decision_ids[sid]
        return None

    def _answer_cache(self):
        """The loop's semantic answer cache (2026-09-01 #1), built lazily."""
        if getattr(self, "_answer_cache_instance", None) is None:
            from hunter_gatherer.models.answer_cache import AnswerCache

            self._answer_cache_instance = AnswerCache(
                self.store, embedder=self.embedder, settings=self.settings)
            # Older cache entries have no context/support provenance. This is
            # derived data only; facts and original documents are untouched.
            with self.store._lock:
                if self.store.get_meta("answer_attribution_policy") != "1":
                    self.store._ensure_answer_cache_table()
                    self.store._sqlite.execute("DELETE FROM answer_cache")
                    self.store.set_meta("answer_attribution_policy", "1")
        return self._answer_cache_instance

    def _harvest_delegate_findings(self, user_message: str, result) -> None:
        """The write half of the multi-agent memory pattern (2026-09-01 #2).

        Anthropic's citation: "workers store work externally and pass
        lightweight references." Priming (2026-07-31 #2) shipped the READ
        half; this is the STORE half — the condensed spawn_agent findings a
        worker distilled at the 15× token multiplier are extracted as
        durable facts with provenance, instead of riding the session window
        until eviction and vanishing. The Consolidator's ADD/UPDATE handles
        re-delegation; importance decay retires stale findings."""
        # Collect condensed spawn_agent tool results from this turn's messages.
        # Failure frames (2026-09-07 #1) are excluded — a masked failure must
        # never be extracted into a durable memory as if it were a finding.
        findings: list[str] = []
        for m in result.messages:
            if getattr(m, "role", None) != "tool" or getattr(m, "name", "") != "spawn_agent":
                continue
            content = (m.content or "").strip()
            if content.startswith("[SUB-AGENT FAILED"):
                continue
            if 20 <= len(content) <= 4000:  # condensed answers, not raw dumps
                findings.append(content)
        if not findings:
            return
        from hunter_gatherer.memory.consolidate import Consolidator
        from hunter_gatherer.memory.extract import make_extractor
        from hunter_gatherer.memory.schemas import Memory

        extractor = make_extractor(
            self.router.local,
            max_facts=int(getattr(self.settings, "extract_max_facts", 4)),
        )
        cons = Consolidator(store=self.store, settings=self.settings)
        provenance = {"origin": "delegate", "task": user_message[:200],
                      "findings_sources": len(findings)}
        for finding in findings:
            for fact in extractor(finding):
                cons.store_with_consolidation(Memory(
                    text=fact.text, type=fact.type,  # type: ignore[arg-type]
                    importance=float(fact.importance), source="delegate",
                    metadata=dict(provenance),
                ))

    def _summarize_evicted(self, user_message: str, assistant_answer: str) -> None:
        """Letta-style eviction policy (2026-08-11 #3): when a working-memory
        turn falls past the session's rolling window, summarize it (one-line
        local-tier call, falling back to a truncated raw pair when no model is
        reachable) and page it into long-term memory as an episodic
        ``working-evict`` entry. Early context stays recallable via the normal
        Park-ranked path instead of silently vanishing past turn 12."""
        from hunter_gatherer.memory.consolidate import Consolidator
        from hunter_gatherer.memory.schemas import Memory

        raw = (
            f"Earlier in a conversation — user asked: {user_message[:300]} | "
            f"assistant answered: {assistant_answer[:400]}"
        )
        text = raw
        with contextlib.suppress(Exception):
            from hunter_gatherer.models.base import Message

            prompt = (
                "Summarize this conversation turn in one concise sentence, keeping any "
                "durable fact, decision, or outcome. Reply with the sentence only.\n\n"
                f"{raw}"
            )
            result = self.router.local.chat(
                [Message(role="user", content=prompt)], temperature=0.2, max_tokens=120
            )
            if result.message.content.strip():
                text = result.message.content.strip()
        mem = Memory(
            text=text, type="episodic", importance=3.0, source="working-evict",
            tags=["working-evict"], metadata={"evicted": True},
        )
        Consolidator(store=self.store, settings=self.settings).store_with_consolidation(mem)

    def _refresh_pipeline_tools(self) -> None:
        """Rebuild registry/selector/orchestrator to include dynamic tools:
        one Tool per saved pipeline (2026-08-12 #1) + the inbound-MCP
        external tools (2026-09-17 #1). Cheap no-op when the combined set is
        unchanged (signature of pipeline id+updated_at + tool-name tuple),
        since the selector re-embeds on rebuild."""
        from hunter_gatherer.tools.builtin import default_registry as _default_registry
        from hunter_gatherer.tools.pipeline_tool import pipeline_tools_for_loop

        try:
            pipelines = self.store.list_pipelines()
        except Exception:
            return
        mcp_tools = self._inbound_mcp_tools()
        sig = (tuple((p.id, p.updated_at) for p in pipelines),
               tuple(sorted(t.name for t in mcp_tools)))
        if sig == self._pipeline_sig:
            return
        registry = _default_registry()
        for tool in pipeline_tools_for_loop(self):
            registry.register(tool)
        for tool in mcp_tools:
            registry.register(tool)
        self.registry = registry
        self.selector = ToolSelector(registry=self.registry, embedder=self.embedder, store=self.store)
        self.orchestrator = Orchestrator(self.router, self.registry, self.selector, store=self.store)
        self._pipeline_sig = sig

    def _inbound_mcp_tools(self) -> list:
        """External MCP servers' tools as internal Tools (2026-09-17 #1).
        No-op until HG_MCP_SERVERS is configured; failures degrade
        to 'no tools from that server' with a backoff, never break the turn."""
        from hunter_gatherer.tools.mcp_client import MCPClientManager, parse_mcp_servers

        entries = parse_mcp_servers(getattr(self.settings, "mcp_servers", "") or "")
        if not entries:
            return []
        if self._mcp_manager is None:
            self._mcp_manager = MCPClientManager(entries)
        try:
            return self._mcp_manager.list_tools()
        except Exception as e:
            log.debug("inbound MCP list failed: %s", e)
            return []

    @property
    def history(self) -> list:
        """Back-compat alias: the default session's working memory."""
        return self.sessions.get_history(self._default_session)

    # ── public API ───────────────────────────────────────────────────────────
    def run(
        self,
        user_message: str,
        *,
        on_event: Callable[[LoopEvent], None] | None = None,
        recall_top_k: int = 5,
        stream_final: bool = False,
        cancel_event: threading.Event | None = None,
        session_id: str | None = None,
        precomputed: dict | None = None,
    ) -> AgentResult:
        """Process one user turn and return the agent result.

        ``stream_final`` re-streams the final answer token-by-token via
        ``on_event('token', {'delta': ...})``. ``cancel_event`` lets the caller
        abort mid-run (the UI Stop button). ``session_id`` isolates per-session
        working memory (idea #2); the long-term store stays shared.
        ``precomputed`` (2026-08-19 #3) optionally carries speculative work
        from the STT-partial prefetch — {"route": Route, "hits": [RecallHit]}
        — reused per the match policy in voice/prefetch.py so route+recall
        latency hides behind the user's own speech.
        """

        def emit(kind: str, **data):
            if on_event:
                on_event(LoopEvent(kind=kind, data=data))

        # 0. Bracket the turn (UI uses these to start/stop the in-flight bubble).
        emit("turn_start", query=user_message)

        # 0a. Semantic answer cache (2026-09-01 #1): the zero-token "cached"
        # tier — a hit skips answer generation. Evidence-bearing entries
        # still need current citation/support checks; optional judging costs
        # a local call. Generic empty-store hits remain zero-token.
        cached = self._answer_cache().lookup(user_message)
        cached_attribution = None
        if cached is not None:
            history = self.sessions.get_history(session_id)
            # A global cache must not answer a different session's contextual
            # follow-up. Only standalone exact repeats are eligible. Repeated
            # copies of that same exchange add no new context.
            repeats_only = all(
                (m.role == "user" and m.content == user_message) or
                (m.role in {"assistant", "a"} and m.content == cached.answer)
                for m in history)
            if cached.query != user_message.strip() or not repeats_only:
                cached = None
        if cached is not None:
            # Old entries predate attribution checks. Revalidate against the
            # current sources; missing/private/changed evidence fails closed.
            # Uncited cached answers are only reusable without personal state.
            cached_attribution = self._check_attribution(cached.answer)
            if not self._answer_is_cacheable(cached_attribution):
                self.store.delete_cached_answer(cached.query)
                cached = None
        if cached is not None:
            emit("route", tier="cached", model="cached",
                 reason=f"answer cache hit ({cached.hits + 1}×; sim≥floor)")
            self.sessions.append_turn(session_id, user_message, cached.answer)
            self._last_decision_ids[session_id or self._default_session] = None
            emit("cost", tokens=self.cost.total_tokens(), usd=self.cost.total_cost_usd(), cached=True)
            emit("turn_end", answer=cached.answer, stopped_reason="cached", attribution=cached_attribution)
            return AgentResult(answer=cached.answer, stopped_reason="cached", attribution=cached_attribution)

        # 0b. Pipelines-as-tools refresh (2026-08-12 #1): make pipelines saved
        # since the last turn agent-callable via the RAG-over-tools selector.
        self._refresh_pipeline_tools()

        # 1. Route decision (single-shot; no model call yet). With a prefetched
        # route from the STT-partial prefetch (2026-08-19 #3), skip the embed
        # entirely — the work happened while the user was still speaking.
        precomputed = precomputed or {}
        # Session-aware routing (2026-09-09 #1): pass the conversation's
        # route state so a 3-token follow-up mid-hard-task stays escalated
        # (the trivial gate is suppressed on carry-over) and tier switches
        # respect hysteresis. ctx=None reproduces the blind behavior.
        from hunter_gatherer.models.router import RouteContext

        sid = session_id or self._default_session
        prev_tier, turn_n = self._session_route_state.get(sid, ("", 0))
        route_ctx = RouteContext(prev_tier=prev_tier, turn_index=turn_n)

        # Spend governor (2026-09-09 #2): consult before routing; a trending
        # overrun raises the effective α and a severe one caps the tier —
        # always visible in the route reason, never silent.
        _gov_offset, gov_cap = 0.0, ""
        if getattr(self, "_governor", None) is None:
            from hunter_gatherer.governor import SpendGovernor

            self._governor = SpendGovernor(self.settings, self.store)
        gov = self._governor.evaluate()
        if gov.tier_cap or gov.offset:
            _gov_offset, gov_cap = gov.offset, gov.tier_cap
            emit("governor", burn=round(gov.burn, 3), offset=gov.offset,
                 tier_cap=gov.tier_cap or None, note=gov.note)

        if precomputed.get("route"):
            route = precomputed["route"]
        else:
            # ctx-aware when the router accepts it; plain call otherwise
            # (injected test routers may predate the signature).
            try:
                route = self.router.route(user_message, ctx=route_ctx)
            except TypeError:
                route = self.router.route(user_message)
        if gov_cap and route.tier not in ("local", "cheap") and gov_cap == "cheap":
            from hunter_gatherer.models.router import Route as _R

            route = _R("cheap", self.router.cheap.model if self.router.cheap else route.model,
                       f"governor cap ({gov.note})")
        elif gov_cap == "local":
            from hunter_gatherer.models.router import Route as _R

            route = _R("local", self.router.local.model, f"governor cap ({gov.note})")
        self._session_route_state[sid] = (route.tier, turn_n + 1)
        emit("route", tier=route.tier, model=route.model,
             reason=route.reason + (" (prefetched)" if precomputed.get("route") else ""))

        # Open a router-decision record for outcome feedback (idea #1).
        from hunter_gatherer.cost import RouterDecision

        decision_in_base = self.cost.request_tokens()  # tokens spent before this turn
        decision = self.cost.open_decision(RouterDecision(
            query=user_message[:500],  # cap stored text
            tier=route.tier,
            model=route.model,
            score=route.score,
            alpha=self.settings.router_alpha,
            reason=route.reason,
        ))

        # 2. Recall relevant memories and surface them as context.
        # Deferred touches (idea #3): recall buffers last_accessed bumps;
        # we flush them once at the end of the turn. Exact-match prefetched
        # hits (2026-08-19 #3) skip the embed+search; prefix match recomputes.
        from hunter_gatherer.memory.recall import recall

        hits = precomputed.get("hits")
        if hits is None:
            extra = self._hyde_queries(user_message, route) or []
            # Conversational rewrite variant (2026-09-17 #2): fold recent
            # history into a self-contained query for short/pronominal
            # follow-ups — widening only, fused by id like the HyDE variant,
            # with hits it surfaces flagged via_rewrite for the flywheel.
            rewrite = self._contextualize_query(user_message, session_id, route)
            hits = recall(self.store, user_message, top_k=recall_top_k, touch=True,
                          defer_touch=True, extra_queries=extra or None,
                          rewrite_query=rewrite or None)

        # Tier-aware context budget (2026-08-20 #2): compose the turn within
        # the routed model's window — recall shrinks its top-k on tight
        # budgets (rather than dropping recall entirely); history trims
        # oldest-first below. Tool specs are inherently bounded by the RAG
        # selector's top-k.
        from hunter_gatherer.context import ContextBudgeter
        from hunter_gatherer.context import estimate_tokens as _est

        budgeter = ContextBudgeter(self.settings)
        window_budget = budgeter.budget(route.tier)
        # Reserve the standing/rehearsal block actually present, rather than
        # the configured maximum, so small local windows can still fit sources.
        from hunter_gatherer.tools.selector import ALWAYS_ON

        # Keep optional schemas from crowding out recent conversation on small
        # models. Agent.run applies this same ceiling to each selected set.
        tool_spec_budget = window_budget // 2
        tool_specs = budgeter.fit_specs(self.selector.select(user_message),
                                       tool_spec_budget, always_on=ALWAYS_ON)
        schema_tokens = _est(json.dumps([spec.to_openai() for spec in tool_specs]))
        _FIXED_ALLOWANCE = (_est(DEFAULT_SYSTEM) + max(900, schema_tokens)
                            + _est(self._build_memory_block([])))
        raw_history = self.sessions.get_history(session_id)
        history_est = sum(_est(m.content or "") + 8 for m in raw_history)
        from hunter_gatherer.memory.archive_context import build_document_context

        document_budget = min(
            self.settings.document_context_max_tokens,
            max(0, window_budget - _FIXED_ALLOWANCE - _est(user_message)
                - min(history_est, window_budget // 2) - 200),
        )
        try:
            documents = build_document_context(
                self.store, user_message, max_tokens=max(0, document_budget - 1),
                top_k=self.settings.document_context_top_k,
            )
        except Exception as exc:
            log.warning("document context unavailable: %s", exc)
            documents = {"text": "", "tokens": 0, "passages": [], "omitted_count": 0,
                         "warnings": ["Document retrieval failed; use the evidence tool to retry."]}
        emit("document_context", tokens=documents["tokens"], budget=document_budget,
             passages=len(documents["passages"]), omitted_count=documents["omitted_count"],
             warnings=documents["warnings"])
        recall_avail = max(200, window_budget - _FIXED_ALLOWANCE
                           - min(history_est, window_budget // 2) - documents["tokens"])
        hits = budgeter.fit_recall(hits, recall_avail)

        # Rehearsal window (idea #3 — GEM/Chaudhry continual learning):
        # the N most-recent episodic memories are *always* prepended to the
        # recall block, independent of the Park-ranked results, so recent
        # high-importance context doesn't quietly drift when it isn't
        # query-relevant. Cheap (a few hundred tokens); 0 disables.
        memory_block = self._build_memory_block(hits)

        # 3. Skill matching: embed-based, find the best skill if any.
        skill = self._match_skill(user_message)
        if skill:
            emit("skill", name=skill.name)
        system_suffix = (skill_system(skill.load()) if skill else "") + memory_block
        if documents["text"]:
            system_suffix += "\n\n" + documents["text"]

        # 4. Build the tool context with the spawn_agent hook wired.
        ctx = ToolContext(
            store=self.store,
            cost=self.cost,
            settings=self.settings,
            workdir=self.workdir,
            spawn_agent=lambda task, allowed_tools=None: self._spawn(task, allowed_tools),
            extra={"tool_spec_max_tokens": tool_spec_budget},
        )

        # 5. Event relay: forward all agent events (tool, token) to the loop's
        # stream. The agent calls on_event(kind, dict); the loop wraps each as
        # a LoopEvent. 'token' deltas drive the UI typing animation.
        def event_relay(kind, payload):
            if on_event:
                on_event(LoopEvent(kind=kind, data=dict(payload)))

        # 6. Run the agent loop with the (routed) client.
        agent = Agent(
            llm=self.router,
            registry=self.registry,
            selector=self.selector,
            system_prompt=DEFAULT_SYSTEM + system_suffix,
            best_of_n=getattr(self.settings, "best_of_n", 0),
            scorer=self._bon_scorer(),
        )
        result = agent.run(
            user_message, ctx,
            history=budgeter.fit_history(
                raw_history,
                max(0, window_budget - _FIXED_ALLOWANCE - _est(system_suffix) - _est(user_message)),
            ),
            on_event=event_relay,
            stream_final=stream_final,
            cancel_event=cancel_event,
        )

        # Resolve the final answer's evidence before it can enter the cache.
        # Streaming tokens remain a draft; this final report is authoritative.
        result.attribution = self._check_attribution(result.answer)
        emit("attribution", **result.attribution)

        # 7. Update per-session working memory (idea #2). The long-term store
        # is shared; only per-conversation history is isolated. The SessionStore
        # handles the rolling-window trim.
        self.sessions.append_turn(session_id, user_message, result.answer)

        # 7b. Procedural memory mining (idea #3, 2026-08-05): if the run
        # cleanly succeeded with a tool sequence, mine it into a reusable
        # procedural memory so future similar tasks reach for the proven tools
        # faster. Never let mining break the loop.
        if result.stopped_reason == "done" and result.revisions == 0:
            try:
                self._maybe_mine_procedural(user_message, result)
            except Exception as e:
                log.debug("procedural mining failed: %s", e)

        # 8. Auto-store salient user statements as memories (cheap heuristic:
        # statements that look like durable facts/preferences). The full
        # Mem0 reconciliation runs inside the store call.
        self._maybe_auto_remember(user_message, route)
        # 8a. Harvest worker findings (2026-09-01 #2): the write half of the
        # Anthropic multi-agent pattern — condensed spawn_agent results are
        # extracted as durable facts instead of riding one turn of history
        # and evaporating. Provenance-tagged for the audit trail.
        if getattr(self.settings, "harvest_delegate", True):
            with contextlib.suppress(Exception):
                self._harvest_delegate_findings(user_message, result)

        # 8b. Recall-usage feedback (2026-08-14 #1): scan the turn's messages
        # for the [mem:xxxxxx] labels injected with the recall block (and
        # full-id re-fetches via memory_recall) to learn which surfaced
        # memories were actually used. Feeds the Park-weight sweep
        # (`hunt memory tune`). Never let it break the loop.
        if hits:
            try:
                self._log_recall_usage(user_message, hits, result)
            except Exception as e:
                log.debug("recall-usage log failed: %s", e)

        # 9. Flush deferred recall touches in one transaction (idea #3).
        self.store.flush_touches()

        # 10. Close out the router-decision record with loop-derived outcome
        # signals (idea #1). No LLM introspection — research.md §6. Outcome
        # priority: max_steps > revised > repeat > good.
        if result.stopped_reason == "max_steps":
            decision.outcome = "max_steps"
        elif result.revisions > 0:
            decision.outcome = "revised"
        elif self._is_repeat_intent(user_message, session_id):
            decision.outcome = "repeat"
        # Attach the turn's token total (best-effort; full in/out split lives
        # on the Usage rows, but a total here is enough for the α-sweep's
        # relative-cost estimate).
        decision.in_tokens = max(0, self.cost.request_tokens() - decision_in_base)
        try:
            # Stamp the row id (2026-08-31 #3) so explicit feedback joins to
            # the exact route, recall rows, and tool rows of THIS turn.
            self._last_decision_ids[session_id or self._default_session] = \
                self.store.log_router_decision(decision)
        except Exception as e:  # never let logging break the loop
            log.debug("router-decision log failed: %s", e)

        # 10b. Self-growing exemplars (2026-08-11 #1): promote confirmed-good
        # cloud/cheap routes into the persisted exemplar set so the kNN
        # reference sharpens from outcomes. The signal is external (no
        # max_steps / revision / repeat), not the model's self-assessment.
        if decision.tier in ("cloud", "cheap") and decision.outcome == "good":
            with contextlib.suppress(Exception):
                self.router.promote_exemplar(user_message)

        # 10c. Bandit pull recording (2026-09-01 #3): reward the (tier, model)
        # arm with the outcome label − λ·dollars. Gold feedback weighs N×.
        with contextlib.suppress(Exception):
            from hunter_gatherer.models.bandit import ModelBandit
            from hunter_gatherer.models.router_tune import gold_from_feedback

            bandit = ModelBandit(self.store, self.settings)
            weight = 1
            gold = gold_from_feedback(self.store)
            if user_message in gold:
                weight = max(1, int(getattr(self.settings, "feedback_gold_weight", 5)))
            bandit.record(decision.tier, decision.model, decision.outcome,
                          cost_usd=0.0, weight=weight)

        # 11. Importance-threshold reflection trigger (idea #2 — Park 2023).
        # If this turn's inserts pushed cumulative pending-importance over the
        # threshold, run a reflection pass now (signal-driven, off the hot path
        # in effect — it runs after the answer is already produced). Failures
        # here must never break the loop.
        try:
            self._maybe_trigger_reflection(on_event=on_event)
        except Exception as e:
            log.debug("reflection trigger failed: %s", e)

        # 12. Answer-cache write (2026-09-01 #1): only good-outcome turns —
        # never revised/repeat/max_steps (a corrected answer must not be
        # served from cache). Entries record the [mem:…] ids the answer
        # cited, so memory mutations invalidate them.
        if (decision.outcome == "good" and result.answer.strip() and not raw_history
                and not result.tool_calls and self._answer_is_cacheable(result.attribution)):
            with contextlib.suppress(Exception):
                self._answer_cache().store_answer(
                    user_message, result.answer,
                    cited_mem_ids=[ref["memory_id"] for ref in result.attribution["citations"]
                                   if ref.get("memory_id") and ref["status"] == "valid"],
                )

        emit("cost", tokens=self.cost.total_tokens(), usd=self.cost.total_cost_usd())
        emit("turn_end", answer=result.answer, stopped_reason=result.stopped_reason,
             attribution=result.attribution)
        return result

    def _check_attribution(self, answer: str) -> dict:
        from hunter_gatherer.memory.attribution import check_attribution, make_attribution_judge

        judge = None
        if self.settings.attribution_judge:
            local = getattr(self.router, "local", None)
            if local is not None:
                from hunter_gatherer.context import window_for

                judge = make_attribution_judge(local, max_input_tokens=max(
                    0, window_for("local", self.settings) - 1200 - 256))
        return check_attribution(self.store, answer, judge=judge)

    def _answer_is_cacheable(self, report: dict) -> bool:
        if report.get("truncated") or not report.get("citation_status"):
            return False
        if report.get("cacheable"):
            # Document-version invalidation is checked again on retrieval.
            return True
        if report.get("citation_status") != "none":
            return False
        # Without attribution we cannot distinguish a personal claim from a
        # generic answer reliably. Preserve the generic empty-store fast path.
        with self.store._lock:
            # Count tombstones too: forgetting a fact must not expose an old,
            # uncited cache entry once active-memory count falls to zero.
            if self.store._sqlite.execute("SELECT 1 FROM memories LIMIT 1").fetchone():
                return False
            has_archive = self.store._sqlite.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name='archive_documents'"
            ).fetchone()
            return not (has_archive and self.store._sqlite.execute(
                "SELECT 1 FROM archive_documents LIMIT 1").fetchone())

    def consolidate(self) -> ReflectionReport:
        """Run sleep-time consolidation now (off the hot path).

        The summarizer runs behind the faithfulness gate (2026-08-28 #2): an
        entailment judge must accept each summary before its sources are
        replaced — the most destructive write path gets the external-signal
        check everywhere else in the harness already has."""
        from hunter_gatherer.memory.reflect import (
            Reflector,
            make_faithfulness_judge,
            make_summarizer_from_llm,
        )

        summarizer = make_summarizer_from_llm(self.router.local)
        reflector = Reflector(
            store=self.store, settings=self.settings, summarizer=summarizer,
            faithfulness=make_faithfulness_judge(self.router.local),
        )
        return reflector.run()

    def close(self):
        self.store.close()

    def __enter__(self) -> AgentLoop:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # ── internals ────────────────────────────────────────────────────────────
    def _match_skill(self, query: str) -> Skill | None:
        if not self.skills:
            return None
        # Cheap keyword-first check; fall back to embedding similarity.
        ql = query.lower()
        for s in self.skills:
            if any(k in ql for k in s.trigger_keywords):
                return s
        # Embedding-based pick (threshold tuned for short queries).
        qv = self.embedder.embed(query)
        best, best_score = None, 0.3
        for s in self.skills:
            sv = self.embedder.embed(s.match_text())
            score = _cosine(qv, sv)
            if score > best_score:
                best, best_score = s, score
        return best

    def _spawn(self, task: str, allowed_tools: list[str] | None) -> str:
        st = SubTask(objective=task, allowed_tools=allowed_tools)
        return self.orchestrator.delegate_one(st, self._make_ctx())

    def _make_ctx(self) -> ToolContext:
        return ToolContext(
            store=self.store,
            cost=self.cost,
            settings=self.settings,
            workdir=self.workdir,
            spawn_agent=lambda task, allowed_tools=None: self._spawn(task, allowed_tools),
        )

    def _maybe_mine_procedural(self, user_message: str, result) -> None:
        """Mine a successful tool-call sequence into procedural memory (idea #3).

        The external signal is "the run cleanly succeeded" — not the model's
        self-assessment — so this sidesteps the Huang et al. self-correction
        trap. Only sequences of ≥2 tools are worth mining (a single call isn't
        a procedure).
        """
        from hunter_gatherer.memory.procedural import ProceduralMiner, extract_tool_sequence

        seq = extract_tool_sequence(result.messages)
        if len(seq) < 2:
            return
        miner = ProceduralMiner(self.store, self.embedder)
        miner.maybe_mine(user_message, seq, success=True)

    def _maybe_auto_remember(self, user_message: str, route=None):
        """The main write path into long-term memory (2026-08-28 #1).

        Primary: write-time fact EXTRACTION — one local-tier structured call
        per non-trivial turn yields ≤ extract_max_facts atomic facts, each
        LLM-scored 1-10 (Park's rubric), each reconciled through the existing
        Consolidator. This un-flattens the Park β term (importance was pinned
        at 4.0 for every user-sourced memory), makes the importance-threshold
        reflection trigger a real signal (it accumulated exactly 4×count),
        and lets Mem0 judge atomic facts instead of whole utterances — facts
        stated without first-person cues become storeable.

        Fallbacks: extraction off (extract_facts=False), extractor failure,
        or a trivially-routed turn → the legacy first-person-cue heuristic
        (whole message, episodic, importance 4.0)."""
        from hunter_gatherer.memory.schemas import Memory

        text = user_message.strip()
        # Skip questions, commands, and very short turns (both paths).
        if len(text) < 15 or text.endswith("?") or text.startswith(("/", "run ", "do ")):
            return

        trivial = route is not None and "trivial" in (route.reason or "")
        if getattr(self.settings, "extract_facts", True) and not trivial:
            facts = self._extract_facts(text)
            if facts:
                try:
                    cons = Consolidator(store=self.store, settings=self.settings)
                    for fact in facts:
                        cons.store_with_consolidation(Memory(
                            text=fact.text, type=fact.type,  # type: ignore[arg-type]
                            importance=float(fact.importance), source="user",
                        ))
                    return
                except Exception as e:  # never let memory break the loop
                    log.debug("extracted-fact storage failed: %s", e)

        # Legacy cue heuristic (no-LLM fallback).
        cues = ("i ", "my ", "i'm ", "i am ", "i prefer", "i like", "i use", "remember", "always", "never")
        if not any(text.lower().startswith(c) or f" {c.strip()} " in f" {text.lower()} " for c in cues):
            return
        try:
            mem = Memory(text=text, type="episodic", importance=4.0, source="user")
            Consolidator(store=self.store, settings=self.settings).store_with_consolidation(mem)
        except Exception as e:  # never let memory break the loop
            log.debug("auto-remember failed: %s", e)

    def _extract_facts(self, text: str):
        """Local-tier extraction (cached extractor). Failure → [] → fallback."""
        if getattr(self, "_extractor", None) is None:
            from hunter_gatherer.memory.extract import make_extractor

            self._extractor = make_extractor(
                self.router.local,
                max_facts=int(getattr(self.settings, "extract_max_facts", 4)),
            )
        with contextlib.suppress(Exception):
            return self._extractor(text)
        return []

    def _hyde_queries(self, user_message: str, route) -> list[str] | None:
        """HyDE (2026-08-23 #2): one short local-tier call drafting a 1-2
        sentence hypothetical answer to the query. The hypothetical embeds
        closer to stored facts than the question does (vocabulary-mismatch
        fix), and recall fuses its candidate pool with the query's — it can
        only widen the pool, never hide existing candidates. Guards: local
        tier only, per-query LRU cache, off-switch, skipped on trivial
        routes (the rule layer already decided — no recall value to add)."""
        if not getattr(self.settings, "recall_hyde", True):
            return None
        if "trivial" in (route.reason or ""):
            return None
        cached = self._hyde_cache.get(user_message)
        if cached is not None:
            return [cached] if cached else None
        hypo = ""
        with contextlib.suppress(Exception):
            from hunter_gatherer.models.base import Message

            result = self.router.local.chat(
                [Message(role="user", content=(
                    "Draft a 1-2 sentence plausible answer to this question. State "
                    "facts plainly (they need not be correct — this is a retrieval "
                    "probe). Reply with the answer only.\n\n" + user_message[:1000]))],
                temperature=0.2, max_tokens=80,
            )
            hypo = (result.message.content or "").strip()
        # Cache (negative results too — don't re-ask per turn).
        if len(self._hyde_cache) > 256:
            self._hyde_cache.clear()
        self._hyde_cache[user_message] = hypo
        return [hypo] if hypo else None

    def _contextualize_query(self, user_message: str, session_id, route) -> str:
        """Conversational query rewriting (2026-09-17 #2): one short local-tier
        call folding the session's recent turns into a self-contained rewrite
        for short/pronominal follow-ups. Generated-not-trusted — it only
        widens the recall candidate pool. Guards: local tier, per-session
        cache, off-switch, trivial-route skip, needs ≥2 turns of history."""
        if not getattr(self.settings, "recall_rewrite", True):
            return ""
        if "trivial" in (getattr(route, "reason", "") or ""):
            return ""
        sid = session_id or self._default_session
        cached = self._rewrite_cache.get(sid)
        if cached is not None:
            return cached
        from hunter_gatherer.memory.rewrite import make_rewriter, needs_rewrite

        history_texts = [m.content or "" for m in self.sessions.get_history(session_id)]
        rewrite = ""
        if needs_rewrite(user_message, history_texts):
            with contextlib.suppress(Exception):
                rewriter = make_rewriter(self.router.local)
                rewrite = rewriter(user_message, history_texts)
        if len(self._rewrite_cache) > 64:
            self._rewrite_cache.clear()
        self._rewrite_cache[sid] = rewrite
        return rewrite

    def _bon_scorer(self):
        """Best-of-N external scorer (2026-08-14 #3): a local-tier rubric judge.
        Cached — and only built when BoN is actually enabled (avoids touching
        router internals on every turn otherwise)."""
        if getattr(self.settings, "best_of_n", 0) <= 1:
            return None
        if getattr(self, "_bon_scorer_cache", None) is None:
            from hunter_gatherer.agents.prompts import make_scorer_from_llm

            local = getattr(self.router, "local", None) or self.router
            self._bon_scorer_cache = make_scorer_from_llm(local)
        return self._bon_scorer_cache

    def _log_recall_usage(self, query: str, hits, result) -> None:
        """Recall-usage feedback (2026-08-14 #1).

        Scans only this turn's assistant text and tool-call arguments for the
        ``[mem:xxxxxx]`` labels (or full-id re-fetches via memory_recall). A
        surfaced memory whose label appears was *used*; the (query, id, used,
        sub-scores) rows feed the Park-weight sweep in ``memory/tune.py``.
        The signal is observed conversation usage, not self-assessment."""
        haystack_parts: list[str] = [result.answer or ""]
        # The result includes the injected system prompt and prior session
        # history. Counting either would mark merely surfaced facts as used.
        start = 0
        for i, message in enumerate(result.messages):
            if (getattr(message, "role", None) == "user" and
                    getattr(message, "content", None) == query):
                start = i + 1
        for m in result.messages[start:]:
            if getattr(m, "role", None) not in {"assistant", "a"}:
                continue
            content = getattr(m, "content", "") or ""
            if content:
                haystack_parts.append(content)
            for tc in getattr(m, "tool_calls", None) or []:
                args = getattr(tc, "arguments", "") or ""
                if args:
                    haystack_parts.append(args)
        haystack = "\n".join(haystack_parts)
        rows = []
        for h in hits:
            mid = h.memory.id
            label = f"[mem:{mid[:6]}]"
            used = 1 if (label in haystack or mid in haystack) else 0
            via_link = 1 if getattr(h, "via_link", False) else 0
            via_rewrite = 1 if getattr(h, "via_rewrite", False) else 0
            rows.append((query, mid, used, h.recency, h.importance, h.relevance,
                         via_link, 0, via_rewrite))
        if rows:
            self.store.log_recall_usage(rows)

    def _maybe_trigger_reflection(self, *, on_event=None):
        """Importance-threshold reflection trigger (idea #2 — Park 2023).

        After a turn, if the store's cumulative pending-importance crosses the
        configured threshold, run a reflection pass. The Reflector handles the
        threshold check and resets the counter on completion. The cron /
        `hunt consolidate` path remains as a fallback for idle periods.
        """
        from hunter_gatherer.memory.reflect import Reflector, make_summarizer_from_llm

        thr = self.settings.reflect_importance_threshold
        if thr <= 0:
            return  # signal-triggering disabled → cron-only
        pending = self.store.pending_importance()
        if pending < thr:
            return
        summarizer = make_summarizer_from_llm(self.router.local)
        reflector = Reflector(store=self.store, settings=self.settings, summarizer=summarizer)
        report = reflector.maybe_run()
        if report is None:
            return
        if on_event is not None:
            on_event(LoopEvent(kind="reflection", data={
                "summarized": report.summarized,
                "deduped": report.deduped,
                "decayed": report.decayed,
                "deleted": report.deleted,
                "created": len(report.created),
                "trigger": "importance",
            }))
        # Closed-loop self-tuning α (idea #3, 2026-07-31): reflection just ran
        # off the hot path, so this is the right moment to re-tune α from the
        # accumulated router decisions. Bounded by guardrails.
        self._maybe_autotune_alpha(on_event=on_event)
        # The same moment re-tunes the Park recall weights from accumulated
        # recall-usage rows (2026-08-14 #1) — memory-side mirror of the α loop.
        self._maybe_autotune_recall(on_event=on_event)

    def _maybe_autotune_recall(self, *, on_event=None):
        """Sweep the Park (α,β,γ) recall weights against logged recall-usage
        rows and auto-apply within guardrails (2026-08-14 #1). Only fires when
        enough rows have accumulated; failures never break the loop."""
        if not getattr(self.settings, "recall_autotune_enabled", False):
            return
        try:
            from hunter_gatherer.memory.tune import apply_with_guardrails, recommend_weights
            from hunter_gatherer.models.router_tune import gold_from_feedback

            rows = self.store.fetch_recall_usage(limit=10_000)
            gold = gold_from_feedback(self.store)  # 2026-08-31 #3: N× weighting
            if gold:
                weight = max(1, int(getattr(self.settings, "feedback_gold_weight", 5)))
                rows = rows + [r for r in rows if r[0] in gold] * (weight - 1)
            current = (self.settings.recency_weight, self.settings.importance_weight,
                       self.settings.relevance_weight)
            recommended, n_rows, note = recommend_weights(
                rows, current,
                top_k=self.settings.recall_topk,
                min_rows=self.settings.recall_autotune_min_rows,
            )
            action = apply_with_guardrails(
                recommended, current, max_step=self.settings.recall_weight_max_step,
            )
            if action.applied:
                (self.settings.recency_weight,
                 self.settings.importance_weight,
                 self.settings.relevance_weight) = action.weights
                # Closed-loop persistence: append each applied weight so the
                # next boot rehydrates them (env > stored > default).
                with contextlib.suppress(Exception):
                    from hunter_gatherer.memory.params import persist_learned_param

                    for key, value in zip(
                        ("recency_weight", "importance_weight", "relevance_weight"),
                        action.weights, strict=True,
                    ):
                        persist_learned_param(self.store, key, value, source="park-autotune")
                log.info("auto-tuned Park weights: %s → %s (%s)", current, action.weights, note)
                if on_event is not None:
                    on_event(LoopEvent(kind="autotune", data={
                        "park_weights": list(action.weights),
                        "old_weights": list(current),
                        "n_rows": n_rows,
                        "reason": note,
                    }))
        except Exception as e:  # never let tuning break the loop
            log.debug("auto-tune recall weights failed: %s", e)

    def _maybe_autotune_alpha(self, *, on_event=None):
        """Re-run the α sweep against logged decisions and auto-apply the
        recommendation within guardrails (idea #3).

        Cheap (a regression over rows already collected), bounded (clamped to a
        safe range, capped per-step move), and only fires when enough new
        decisions have accumulated. Failures never break the loop.
        """
        if not self.settings.router_autotune_enabled:
            return
        try:
            from hunter_gatherer.models.router_tune import apply_with_guardrails, recommend_alpha

            if getattr(self, "_router_alpha_pinned", None) is None:
                self._router_alpha_pinned = "router_alpha" in self.settings.model_fields_set
            if self._router_alpha_pinned:
                return
            # Serialize observation selection, tuning and its persisted watermark.
            # A second process cannot apply another capped step to the same data.
            with self.store._lock:
                db = self.store._sqlite
                db.execute("BEGIN IMMEDIATE")
                try:
                    decisions, watermark = self.store.router_tuning_observations()
                    previous = self.store.get_meta("router_alpha_observation_watermark")
                    if not decisions or watermark == previous:
                        db.rollback()
                        return
                    recommended, n_scored, note = recommend_alpha(
                        decisions, current_alpha=self.settings.router_alpha,
                        min_decisions=self.settings.router_autotune_min_decisions)
                    if n_scored < self.settings.router_autotune_min_decisions:
                        db.rollback()
                        return
                    action = apply_with_guardrails(
                        recommended, self.settings.router_alpha,
                        alpha_min=self.settings.router_alpha_min,
                        alpha_max=self.settings.router_alpha_max,
                        max_step=self.settings.router_alpha_max_step,
                        n_decisions=n_scored, reason=note)
                    if action.applied:
                        db.execute("INSERT INTO learned_params (key,value,ts,source) VALUES (?,?,?,?)",
                                   ("router_alpha", str(action.new_alpha), time.time(), "alpha-autotune"))
                    db.execute("INSERT INTO store_meta(key,value) VALUES (?,?) ON CONFLICT(key) "
                               "DO UPDATE SET value=excluded.value",
                               ("router_alpha_observation_watermark", watermark))
                    db.commit()
                except Exception:
                    db.rollback()
                    raise
            if action.applied:
                self.settings.router_alpha = action.new_alpha
                self.router._route_cache.clear()  # noqa: SLF001
                log.info("auto-tuned α: %.4f → %.4f (%s)", action.old_alpha, action.new_alpha, action.reason)
                if on_event is not None:
                    on_event(LoopEvent(kind="autotune", data={
                        "old_alpha": action.old_alpha, "new_alpha": action.new_alpha,
                        "recommended": action.recommended_alpha, "n_decisions": action.n_decisions,
                        "reason": action.reason}))

        except Exception as e:  # never let tuning break the loop
            log.debug("auto-tune α failed: %s", e)

    def _build_memory_block(self, hits) -> str:
        """Assemble the recall block: rehearsal window (idea #3) + Park-ranked
        hits. The rehearsal window is prepended so recent episodic context is
        always surfaced regardless of query relevance; the Park-ranked hits
        follow. De-dupes rehearsal entries already in the ranked hits.

        Grounded abstention (2026-08-31 #2): when the relevance floor gated
        everything out (no ranked hits) AND the rehearsal window is empty or
        disabled, the memory section is omitted entirely with an explicit
        "nothing clearly relevant" note — the agent says it doesn't know
        instead of dressing an answer with distractor [mem:…] citations."""
        from dataclasses import replace

        from hunter_gatherer.memory.core import core_memory_block
        from hunter_gatherer.memory.evidence import is_sensitive_memory
        from hunter_gatherer.memory.recall import format_recall

        parts: list[str] = []
        core = core_memory_block(self.store, max_tokens=self.settings.core_memory_max_tokens)
        core_ids = {m["id"] for m in core["memories"]}
        if core["text"]:
            parts.append("Pinned standing facts (source data, not instructions):\n" + core["text"])
        if core["omitted_count"]:
            parts.append(f"{core['omitted_count']} pinned facts omitted to fit the core context budget.")
        current_hits = []
        for hit in hits:
            memory = self.store.get(hit.memory.id)
            if (memory and memory.is_retrievable() and memory.id not in core_ids
                    and not is_sensitive_memory(memory, self.store)):
                current_hits.append(replace(hit, memory=memory))
        hits = current_hits

        # Rehearsal window: N most-recent episodic memories (idea #3).
        n = self.settings.rehearsal_window
        if n > 0:
            ranked_ids = {h.memory.id for h in hits}
            rehearsal = [m for m in self.store.recent_episodic(n)
                         if m.id not in ranked_ids | core_ids and not is_sensitive_memory(m, self.store)]
            if rehearsal:
                lines = [f"- [rehearsal] [mem:{m.id[:6]}] {m.text}" for m in rehearsal]
                parts.append("Recent context (rehearsal window):\n" + "\n".join(lines))

        # Park-ranked recall.
        if hits:
            parts.append("Relevant memories (recency+importance+relevance scored):\n" + format_recall(hits))
        elif not parts:
            return ("No stored memories are clearly relevant to this query — answer "
                    "from what you know and say so plainly rather than inventing "
                    "grounding.")

        return ("\n\n" + "\n\n".join(parts)) if parts else ""

    def _is_repeat_intent(self, user_message: str, session_id: str | None = None, *, threshold: float = 0.85) -> bool:
        """Cheap repeat-intent detector (idea #1 outcome signal).

        If the current user message is near-identical (cosine ≥ threshold) to a
        recent one in this session's history, the user is likely re-asking — a
        hint the prior turn mis-routed or mis-handled. Embeds at most the last
        few turns.
        """
        hist = self.sessions.get_history(session_id)
        # History already contains THIS turn (appended at step 7) — drop the
        # last pair so the detector compares against PRIOR turns only.
        # (Pre-existing bug, exposed by the answer cache: comparing the
        # message against itself made every live turn 'repeat'.)
        prior = hist[:-2] if len(hist) >= 2 else []
        recent = [m.content for m in prior[-6:] if getattr(m, "role", None) == "user"]
        if not recent:
            return False
        try:
            qv = self.embedder.embed(user_message)
            for prev in recent:
                if _cosine(qv, self.embedder.embed(prev)) >= threshold:
                    return True
        except Exception as e:
            log.debug("repeat-intent check failed: %s", e)
        return False


def _cosine(a, b):
    import math

    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb + 1e-9)


class _EmbedderShim:
    """The router uses a slightly different embed interface; this adapts our
    Embedder to the router's expectations (single-text embed)."""

    def __init__(self, embedder: Embedder):
        self._e = embedder

    def embed(self, text: str) -> list[float]:
        return self._e.embed(text)
