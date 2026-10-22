"""The tool-use agent loop.

Single-agent execution: send messages → if the model requests tools, execute
them, append results, repeat → until the model produces a final answer with no
tool calls.

Self-correction policy (Huang et al., ICLR 2024 — LLMs cannot reliably
self-correct reasoning without external signal): we do NOT run reflection
loops on the model's own output. We only re-attempt after a *failed external
check* — a tool that returned an error, a non-zero exit code, or a schema
violation. And we hard-cap revisions at 1, since diminishing returns dominate.

Token streaming (for the UI): pass ``stream_final=True`` to re-stream the
final-answer turn token-by-token via ``on_event("token", {"delta": ...})``.
Tool-calling turns always use ``chat()`` (tool calls are not streamed). Pass
``cancel_event`` to allow mid-run cancellation (checked each step + before
each model call) — the UI Stop button.
"""
from __future__ import annotations

import contextlib
import json
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass, field

from hunter_gatherer.models.base import LLMClient, Message
from hunter_gatherer.tools.base import Registry, ToolContext
from hunter_gatherer.tools.selector import ToolSelector

log = logging.getLogger(__name__)

DEFAULT_MAX_STEPS = 12         # tool-call rounds before giving up
DEFAULT_MAX_REVISIONS = 1      # Huang et al.: cap reflection at 1


@dataclass
class AgentResult:
    answer: str
    steps: int = 0               # tool-call rounds used
    tool_calls: int = 0
    revisions: int = 0
    messages: list[Message] = field(default_factory=list)
    stopped_reason: str = "done"
    attribution: dict | None = None


class Agent:
    """A single agent: one LLMClient + a tool registry + a selector.

    The selector (RAG-over-tools) picks which tool schemas to inject each turn,
    so the prompt stays small even with many tools available.
    """

    def __init__(
        self,
        llm: LLMClient,
        registry: Registry,
        selector: ToolSelector,
        *,
        max_steps: int = DEFAULT_MAX_STEPS,
        max_revisions: int = DEFAULT_MAX_REVISIONS,
        system_prompt: str = "",
        best_of_n: int = 0,
        scorer: Callable[[str, str], float] | None = None,
    ):
        self.llm = llm
        self.registry = registry
        self.selector = selector
        self.max_steps = max_steps
        self.max_revisions = max_revisions
        self.system_prompt = system_prompt
        # Best-of-N sampling (2026-08-14 #3): sample N candidates on the
        # final-answer turn and return the argmax under an EXTERNAL scorer
        # (never the same model critiquing itself — Huang et al.). 0 disables.
        self.best_of_n = max(0, int(best_of_n))
        self.scorer = scorer

    def run(
        self,
        user_message: str,
        ctx: ToolContext,
        *,
        history: list[Message] | None = None,
        on_event: Callable[[str, dict], None] | None = None,
        stream_final: bool = False,
        cancel_event: threading.Event | None = None,
    ) -> AgentResult:
        """Execute the task in `user_message`. Returns the final answer.

        ``history`` is prior conversation (without system). ``on_event`` is an
        optional callback for streaming UI events. When ``stream_final`` is
        True, the final-answer turn is re-streamed token-by-token via
        ``on_event("token", {"delta": ...})``. ``cancel_event`` allows the
        caller to abort mid-run (checked each step + before each model call).
        """
        from hunter_gatherer.agents.prompts import DEFAULT_SYSTEM

        def cancelled() -> bool:
            return cancel_event is not None and cancel_event.is_set()

        messages: list[Message] = []
        sys_text = self.system_prompt or DEFAULT_SYSTEM
        # System message MUST come first; then prior conversation; then the
        # new user turn.
        messages.append(Message(role="system", content=sys_text))
        if history:
            messages.extend(history)
        messages.append(Message(role="user", content=user_message))

        steps = 0
        tool_calls = 0
        revisions = 0
        last_failed_step = False

        while steps < self.max_steps:
            if cancelled():
                return AgentResult(
                    answer="(cancelled)", steps=steps, tool_calls=tool_calls,
                    revisions=revisions, messages=messages, stopped_reason="cancelled",
                )
            steps += 1
            # RAG-over-tools: pick specs relevant to the latest user content.
            query = _last_user_text(messages) or user_message
            specs = self.selector.select(query)
            spec_budget = (getattr(ctx, "extra", {}) or {}).get("tool_spec_max_tokens")
            if type(spec_budget) is int and spec_budget >= 0:
                from hunter_gatherer.context import ContextBudgeter
                from hunter_gatherer.tools.selector import ALWAYS_ON

                specs = ContextBudgeter(getattr(ctx, "settings", None)).fit_specs(
                    specs, spec_budget, always_on=ALWAYS_ON,
                )
            result = self.llm.chat(messages, tools=specs, temperature=0.2)
            msg = result.message

            if not msg.tool_calls:
                # Final answer. Optionally re-stream it token-by-token for the
                # UI (chat() already produced the full text; we re-issue via
                # stream() to get live deltas). Tool-detection stays on chat()
                # so provider differences in streaming tool-call chunks don't
                # cause regressions.
                if last_failed_step and revisions < self.max_revisions:
                    revisions += 1
                answer = msg.content
                # Best-of-N (2026-08-14 #3): sample extra candidates and pick
                # the argmax under an external scorer. Guarded — only on
                # non-local routes and only while the turn's token spend is
                # under the configured budget — so the N× multiplier never
                # runs away.
                if self.best_of_n > 1 and self.scorer is not None and self._bon_allowed(ctx, user_message):
                    answer = self._best_of_n(messages, specs, user_message, ctx, fallback=answer)
                streamed_answer = answer
                if stream_final and not cancelled():
                    streamed_answer = self._stream_final(
                        messages, specs, on_event, cancel_event, fallback=answer
                    )
                final_msg = Message(role="assistant", content=streamed_answer)
                messages.append(final_msg)
                return AgentResult(
                    answer=streamed_answer,
                    steps=steps,
                    tool_calls=tool_calls,
                    revisions=revisions,
                    messages=messages,
                )

            messages.append(msg)
            # Execute the requested tools; append tool-result messages.
            # Intra-step parallelism (2026-09-07 #3): when the model emits
            # several calls in one step (its own declaration of independence),
            # fan out through a bounded executor — the step costs max(latency)
            # not sum(latency). Results append in the model's ORIGINAL call
            # order regardless of completion order (protocol-correct pairing
            # + deterministic history for prefix-cache stability). concurrency
            # ≤ 1 reproduces the sequential loop exactly.
            last_failed_step = False
            calls = list(msg.tool_calls)
            concurrency = int(getattr(getattr(ctx, "settings", None), "tool_concurrency", 1) or 1)
            available = tuple(s.name for s in specs)

            def _exec_one(tc, _available=available):
                tool = self.registry.get(tc.name)
                if tool is None:
                    return tc, (f"error: unknown tool '{tc.name}'. "
                                f"Available: {list(_available)}"), True
                tr = tool.safe_run(tc.arguments, ctx)
                res = ("ERROR: " + tr.content) if tr.error else tr.content
                return tc, res, bool(tr.error)

            if len(calls) > 1 and concurrency > 1:
                from concurrent.futures import ThreadPoolExecutor

                with ThreadPoolExecutor(max_workers=min(concurrency, len(calls))) as pool:
                    from contextvars import copy_context
                    futures = [pool.submit(copy_context().run, _exec_one, call) for call in calls]
                    ordered = [future.result() for future in futures]
            else:
                ordered = []
                for tc in calls:
                    if cancelled():
                        break
                    ordered.append(_exec_one(tc))

            for tc, res, failed in ordered:
                if cancelled():
                    break
                tool_calls += 1
                if failed:
                    last_failed_step = True
                if on_event:
                    on_event("tool", {"name": tc.name, "args": tc.arguments,
                                      "result_preview": res[:200]})
                # Tool-result condensation (2026-08-23 #1): over-budget non-error
                # results are compressed to a local-tier digest; the raw payload
                # parks in a bounded sidecar retrievable via tool_result_fetch.
                # Every subsequent model call in the session re-carries what
                # rides here — this is the dominant recurring token sink.
                res = self._condense_result(res, tc, ctx, on_event)
                messages.append(Message(role="tool", content=res, tool_call_id=tc.id, name=tc.name))

        if cancelled():
            return AgentResult(
                answer="(cancelled)", steps=steps, tool_calls=tool_calls,
                revisions=revisions, messages=messages, stopped_reason="cancelled",
            )
        # Exhausted steps.
        log.warning("agent hit max_steps=%d without a final answer", self.max_steps)
        return AgentResult(
            answer="(agent exhausted its tool-call budget without a final answer)",
            steps=steps,
            tool_calls=tool_calls,
            revisions=revisions,
            messages=messages,
            stopped_reason="max_steps",
        )

    # ── Tool-result condensation (2026-08-23 #1) ────────────────────────────
    def _condense_result(self, res: str, tc, ctx, on_event) -> str:
        """Compress an over-budget tool result to a faithful local-tier digest,
        parking the raw payload in the sidecar with a fetch pointer.

        Never condenses: errors (the revision gate needs them verbatim — an
        external signal must not be summarized away), results already under
        budget, or when condensation is disabled/unavailable — in every such
        case the raw text rides as before."""
        from hunter_gatherer.tools.base import estimate_tokens

        budget = int(getattr(getattr(ctx, "settings", None), "tool_result_max_tokens", 0) or 0)
        from hunter_gatherer.tools.archive import ARCHIVE_TOOL_NAMES, bound_archive_result

        if getattr(tc, "name", "") in ARCHIVE_TOOL_NAMES and not res.startswith("ERROR"):
            include_sensitive = False
            with contextlib.suppress(ValueError, TypeError, AttributeError):
                include_sensitive = json.loads(tc.arguments).get("include_sensitive") is True
            return bound_archive_result(
                res, max_tokens=budget, store=ctx.store, include_sensitive=include_sensitive,
            )
        if (getattr(tc, "name", "") == "tool_result_fetch" or budget <= 0
                or res.startswith("ERROR") or estimate_tokens(res) <= budget):
            return res
        if ctx.store is None:
            return res
        with contextlib.suppress(Exception):
            from hunter_gatherer.tools.results import ResultSidecar

            sidecar = ResultSidecar(ctx.store)
            call_id = getattr(tc, "id", None) or f"call_{id(tc):x}"
            if not sidecar.park(call_id, res):
                return res
            digest = _digest_with_local(self._local_client(), res)
            if not digest:
                return res
            if on_event:
                on_event("tool_condensed", {
                    "call_id": call_id, "raw_tokens": estimate_tokens(res),
                    "digest_tokens": estimate_tokens(digest),
                })
            return (
                f"{digest}\n\n[digested — full raw result parked; call "
                f"tool_result_fetch with call_id={call_id} if you need it]"
            )
        return res

    def _local_client(self):
        """The local tier specifically — condensation must never spend cloud
        tokens to save cloud tokens. Falls back to the plain client when the
        LLM isn't a router."""
        return getattr(self.llm, "local", None) or self.llm

    # ── Best-of-N (2026-08-14 #3) ───────────────────────────────────────────
    def _bon_allowed(self, ctx: ToolContext, user_message: str) -> bool:
        """Fire BoN only when (a) the route isn't local (local candidates are
        too weak for sampling to pay), and (b) the turn's token spend is still
        under the configured budget — the N× multiplier never runs away."""
        try:
            route_fn = getattr(self.llm, "route", None)
            if route_fn is not None and route_fn(user_message).tier == "local":
                return False
            cost = getattr(ctx, "cost", None)
            if cost is not None:
                budget = getattr(
                    getattr(ctx, "settings", None), "best_of_n_budget", 6000
                )
                if cost.total_tokens() >= budget:
                    return False
        except Exception:
            return False
        return True

    def _best_of_n(
        self,
        messages: list[Message],
        specs,
        user_message: str,
        ctx: ToolContext,
        *,
        fallback: str,
    ) -> str:
        """Sample up to best_of_n candidates (the first + temperature>0
        re-samples) and return the argmax under the external scorer. Any
        failure falls back to the first candidate — BoN must never break the
        answer path."""
        n = min(self.best_of_n, 3)  # hard cap (research.md §6 cost analysis)
        candidates = [fallback]
        try:
            for _ in range(n - 1):
                res = self.llm.chat(messages, tools=specs, temperature=0.8)
                if res.message.content and not res.message.tool_calls:
                    candidates.append(res.message.content)
            if len(candidates) < 2:
                return fallback
            best, best_score = fallback, float("-inf")
            for cand in candidates:
                score = self.scorer(user_message, cand) if self.scorer else 0.0  # type: ignore[misc]
                if score > best_score:
                    best, best_score = cand, score
            return best
        except Exception as e:
            log.debug("best-of-n sampling failed (%s); using first candidate", e)
            return fallback

    def _stream_final(
        self,
        messages: list[Message],
        specs,
        on_event: Callable[[str, dict], None] | None,
        cancel_event: threading.Event | None,
        *,
        fallback: str,
    ) -> str:
        """Re-stream the final answer token-by-token. Returns the accumulated
        text. Falls back to ``fallback`` (the chat() result) if streaming is
        unavailable or errors. Never raises — streaming is a UX nicety."""
        if on_event is None:
            return fallback
        try:
            collected: list[str] = []
            for delta in self.llm.stream(messages, tools=specs, temperature=0.2):
                if cancel_event is not None and cancel_event.is_set():
                    break
                if delta:
                    collected.append(delta)
                    on_event("token", {"delta": delta})
            return "".join(collected) or fallback
        except NotImplementedError:
            # Provider doesn't support streaming (e.g. Anthropic pre-impl).
            return fallback
        except Exception as e:
            log.debug("stream_final failed (%s); using chat() result", e)
            return fallback


def _last_user_text(messages: list[Message]) -> str:
    for m in reversed(messages):
        if getattr(m, "role", None) == "user":
            return m.content or ""
    return ""


_DIGEST_PROMPT = (
    "Condense this tool output into a faithful digest for an agent that must "
    "keep working. Keep: key results, numbers, file paths, error indicators, "
    "final states. Drop: decoration, repetition, bulk listing. Reply with the "
    "digest only.\n\n{payload}"
)


def _digest_with_local(local_llm, payload: str) -> str:
    """One cheap local-tier call → a faithful digest of an over-budget tool
    result (2026-08-23 #1). Empty string on any failure (caller keeps raw)."""
    try:
        result = local_llm.chat(
            [Message(role="user", content=_DIGEST_PROMPT.format(payload=payload[:24_000]))],
            temperature=0.0, max_tokens=350,
        )
        return (result.message.content or "").strip()
    except Exception:
        return ""
