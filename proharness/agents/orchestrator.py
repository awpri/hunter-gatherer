"""Orchestrator-worker sub-agent delegation.

Anthropic's published multi-agent research system pattern: a lead agent
decomposes a task into independent subtasks, delegates each to a sub-agent
with its own context window (running in parallel), and synthesizes the
condensed results.

Key rules baked in:
  - Each worker gets a self-contained objective + output format + tool list.
  - Workers return CONDENSED results, not raw dumps (avoids the "game of
    telephone" of relaying everything through the orchestrator).
  - Hard cap on worker count (default 5) — Anthropic found diminishing
    returns and cost explosions past that ("50 subagents for a trivial query"
    is the cited failure mode).
  - Workers use the SAME router, so cheap sub-steps run locally and only hard
    steps escalate to cloud. This is the main cost lever.
  - Memory priming (idea #2, 2026-07-31): workers recall the top-k Park-scored
    memories relevant to their objective and get them prepended as a "Relevant
    shared context" block — so they read shared state instead of re-researching
    what the lead agent's store already knows. This completes the "shared
    external state" half of the Anthropic pattern that a memory-blind worker
    omits.
"""
from __future__ import annotations

import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, replace

from proharness.models.base import LLMClient
from proharness.tools.base import Registry, ToolContext
from proharness.tools.selector import ToolSelector

log = logging.getLogger(__name__)

MAX_WORKERS = 5  # Anthropic: past this, cost/quality tradeoff inverts.
DEFAULT_PRIME_TOPK = 4  # memories to prime each worker with (idea #2)


@dataclass
class SubTask:
    objective: str
    allowed_tools: list[str] | None = None
    max_steps: int = 6  # workers are shallow by default


@dataclass
class WorkerResult:
    """Outcome contract for a delegated worker (2026-09-07 #1).

    A worker that exhausts its steps previously returned the literal
    exhaustion string, which flowed into the lead's context looking exactly
    like a condensed finding — and the harvest path extracted facts from it.
    The contract makes failure explicit and carries SALVAGE (the partial
    yield) so the lead reads "worker failed after N steps; partial yield: …"
    and can retry only the failed shard."""

    answer: str
    stopped_reason: str = "done"  # done | max_steps | cancelled
    steps: int = 0
    tool_calls: int = 0
    salvage: str = ""

    @property
    def failed(self) -> bool:
        return self.stopped_reason != "done"

    def framed(self) -> str:
        """The string the lead agent / spawn_agent tool sees."""
        if not self.failed:
            return self.answer
        head = (f"[SUB-AGENT FAILED: {self.stopped_reason} after {self.steps} steps, "
                f"{self.tool_calls} tool calls — this is NOT a research finding.]")
        if self.salvage:
            return f"{head} Partial yield:\n{self.salvage}"
        return head


class Orchestrator:
    """Spawns and runs sub-agents. Used by the spawn_agent tool."""

    def __init__(
        self,
        llm: LLMClient,           # typically the RouterClient
        registry: Registry,
        selector: ToolSelector,
        ctx_factory=None,         # callable(SubTask) -> ToolContext; defaults to shared ctx
        store=None,               # proharness.memory.store.MemoryStore (idea #2 priming)
        prime_topk: int = DEFAULT_PRIME_TOPK,
    ):
        self.llm = llm
        self.registry = registry
        self.selector = selector
        self.ctx_factory = ctx_factory
        self.store = store
        self.prime_topk = prime_topk

    def delegate_many(self, subtasks: list[SubTask], ctx: ToolContext, max_workers: int = MAX_WORKERS) -> list[str]:
        """Run subtasks in parallel; return their condensed answers in the
        order of `subtasks`."""
        if not subtasks:
            return []
        subtasks = subtasks[:MAX_WORKERS]  # hard cap
        max_workers = min(max_workers, len(subtasks))
        results: dict[int, str] = {}

        def _run(idx: int, st: SubTask) -> tuple[int, str]:
            return idx, self.delegate_one(st, ctx)

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            from contextvars import copy_context
            futures = [pool.submit(copy_context().run, _run, i, st) for i, st in enumerate(subtasks)]
            for fut in as_completed(futures):
                idx, answer = fut.result()
                results[idx] = answer
        return [results[i] for i in range(len(subtasks))]

    def delegate_one(self, subtask: SubTask, ctx: ToolContext) -> str:
        """Run a single sub-agent; returns its framed outcome string (see
        WorkerResult.framed — failures carry salvage, never mask as findings)."""
        return self.delegate_one_result(subtask, ctx).framed()

    def delegate_one_result(self, subtask: SubTask, ctx: ToolContext) -> WorkerResult:
        """Run a single sub-agent and return the structured outcome contract
        (2026-09-07 #1): stopped_reason is consulted, and a failed worker
        returns failure-plus-salvage — never its exhaustion string dressed
        as a research finding."""
        from proharness.agents.agent import Agent
        from proharness.agents.prompts import DEFAULT_SYSTEM

        worker_ctx = self.ctx_factory(subtask) if self.ctx_factory else ctx
        if isinstance(worker_ctx, ToolContext):
            from proharness.context import ContextBudgeter

            settings = worker_ctx.settings or getattr(self.store, "settings", None)
            spec_budget = ContextBudgeter(settings).budget("local") // 2
            inherited = worker_ctx.extra.get("tool_spec_max_tokens")
            if type(inherited) is int and inherited >= 0:
                spec_budget = min(spec_budget, inherited)
            worker_ctx = replace(
                worker_ctx, settings=settings,
                extra={**worker_ctx.extra, "tool_spec_max_tokens": spec_budget},
            )

        # Memory priming (idea #2): recall the top-k memories relevant to the
        # objective and prepend as "Relevant shared context". Workers stop
        # re-deriving facts the lead agent's store already knows.
        prime_block = self._prime(subtask.objective)

        # Build a worker-scoped registry if the subtask restricts tools.
        registry = self.registry
        if subtask.allowed_tools:
            from proharness.tools.base import Registry as Reg

            scoped = Reg()
            for name in subtask.allowed_tools:
                t = self.registry.get(name)
                if t is not None:
                    scoped.register(t)
            registry = scoped

        # Worker selector over the (possibly scoped) registry.
        selector = ToolSelector(registry=registry, embedder=self.selector.embedder, top_k=self.selector.top_k)

        system_prompt = (
            DEFAULT_SYSTEM
            + "\n\nYou are a sub-agent. Produce a CONCISE result (≤200 words). Stay on your assigned objective."
            + prime_block
        )
        agent = Agent(
            llm=self.llm,
            registry=registry,
            selector=selector,
            max_steps=subtask.max_steps,
            system_prompt=system_prompt,
        )
        result = agent.run(subtask.objective, worker_ctx)
        salvage = self._salvage(result.messages) if result.stopped_reason != "done" else ""
        return WorkerResult(
            answer=result.answer, stopped_reason=result.stopped_reason,
            steps=result.steps, tool_calls=result.tool_calls, salvage=salvage,
        )

    @staticmethod
    def _salvage(messages) -> str:
        """Partial yield from a failed worker: the last assistant text, else
        the last non-empty tool result (the lead can retry only what failed)."""
        for m in reversed(messages):
            if getattr(m, "role", "") == "assistant" and (m.content or "").strip():
                return m.content.strip()[:600]
        for m in reversed(messages):
            if getattr(m, "role", "") == "tool" and (m.content or "").strip():
                return m.content.strip()[:600]
        return ""

    def _prime(self, objective: str) -> str:
        """Prime workers with current public memories and bounded exact sources.

        Document retrieval is independent of memory hits, so a newly imported
        original is useful before anyone has distilled its contents into facts.
        """
        if self.store is None:
            return ""
        blocks = []
        try:
            from proharness.memory.evidence import is_sensitive_memory
            from proharness.memory.recall import format_recall, recall

            hits = recall(
                self.store, objective, top_k=self.prime_topk,
                touch=False,  # workers don't bump access stats
            )
            public_hits = []
            for hit in hits:
                # Re-fetch SQLite truth: privacy or validity can change after
                # candidate retrieval, and historical recall may return old facts.
                current = self.store.get(hit.memory.id)
                if (current is not None and current.is_retrievable()
                        and not is_sensitive_memory(current, self.store)):
                    public_hits.append(replace(hit, memory=current))
            if public_hits:
                blocks.append(
                    "\n\nRelevant shared context (from the lead agent's memory):\n"
                    + format_recall(public_hits)
                )
        except Exception as e:
            log.debug("worker memory priming failed: %s", e)
        try:
            from proharness.memory.archive_context import build_document_context

            settings = self.store.settings
            document_budget = getattr(settings, "document_context_max_tokens", 768)
            if document_budget > 0:
                # Reserve one estimated token for the separator appended here.
                report = build_document_context(
                    self.store, objective, max_tokens=max(0, document_budget - 1),
                    top_k=getattr(settings, "document_context_top_k", 3),
                )
                if report["text"]:
                    blocks.append("\n\n" + report["text"])
        except Exception as e:
            log.debug("worker document priming failed: %s", e)
        return "".join(blocks)
