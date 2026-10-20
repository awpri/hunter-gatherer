"""Telemetry endpoints: router decisions, α state, cost, eval, one-shot run."""
from __future__ import annotations

from fastapi import APIRouter, Query, Request
from pydantic import BaseModel

router = APIRouter()


def _loop(request: Request):
    return request.app.state.loop


def _settings(request: Request):
    return request.app.state.settings


@router.get("/router/decisions")
def router_decisions(request: Request, limit: int = Query(200, ge=1, le=5000)):
    """Recent routing decisions (outcome-feedback log)."""
    decisions = _loop(request).store.fetch_router_decisions(limit=limit)
    return {
        "count": len(decisions),
        "decisions": [
            {"ts": d.ts, "tier": d.tier, "model": d.model, "score": d.score,
             "alpha": d.alpha, "outcome": d.outcome,
             "in_tokens": d.in_tokens, "out_tokens": d.out_tokens,
             "query": d.query[:120], "reason": d.reason}
            for d in decisions
        ],
    }


@router.get("/router/alpha")
def router_alpha(request: Request):
    """Current α + guardrails + the cost/quality sweep curve."""
    from proharness.models.router_tune import recommend_alpha, sweep

    settings = _settings(request)
    loop = _loop(request)
    decisions = loop.store.fetch_router_decisions(limit=10_000)
    recommended, n_scored, note = recommend_alpha(
        decisions, current_alpha=settings.router_alpha,
        min_decisions=settings.router_autotune_min_decisions,
    )
    sweep_result = sweep(decisions, current_alpha=settings.router_alpha)
    return {
        "current_alpha": settings.router_alpha,
        "recommended_alpha": recommended,
        "guardrails": {
            "min": settings.router_alpha_min,
            "max": settings.router_alpha_max,
            "max_step": settings.router_alpha_max_step,
            "autotune_enabled": settings.router_autotune_enabled,
            "min_decisions": settings.router_autotune_min_decisions,
        },
        "n_scored": n_scored,
        "n_total": len(decisions),
        "note": note,
        "sweep_curve": [
            {"alpha": round(p.alpha, 2), "local": p.local, "cloud": p.cloud,
             "est_cost": round(p.est_cost, 2), "local_bad_rate": round(p.local_bad_rate, 3)}
            for p in sweep_result.points[::max(1, len(sweep_result.points) // 21)]
        ],
    }


@router.get("/cost")
def cost(request: Request):
    """Session cost breakdown."""
    loop = _loop(request)
    return {
        "total_tokens": loop.cost.total_tokens(),
        "total_cost_usd": round(loop.cost.total_cost_usd(), 6),
        "per_model": {
            model: {"in_tokens": u.in_tokens, "out_tokens": u.out_tokens}
            for model, u in loop.cost.per_model().items()
        },
    }


@router.get("/eval")
def eval_run(request: Request, filter: str | None = Query(None)):
    """Run the behavior-layer regression suite (model-free, deterministic)."""
    from proharness.eval.runner import run_eval

    loop = _loop(request)
    report = run_eval(settings=_settings(request), embedder=loop.embedder, task_filter=filter)
    return {
        "behavior_pass": report.behavior_pass,
        "behavior_total": report.behavior_total,
        "behavior_pct": round(report.behavior_pct, 1),
        "results": [
            {"id": tr.task.id, "difficulty": tr.task.difficulty, "passed": tr.behavior.passed,
             "signals": [{"name": s.name, "passed": s.passed, "detail": s.detail}
                         for s in tr.behavior.signals]}
            for tr in report.results
        ],
    }


class RunBody(BaseModel):
    """Body for POST /api/run — a synchronous one-shot turn (non-streaming)."""
    text: str
    session_id: str | None = None  # idea #2: per-session isolation


class FeedbackBody(BaseModel):
    """Body for POST /api/feedback (2026-08-31 #3) — the human gold-label
    channel the proxy flywheels never had."""
    rating: int  # +1 helpful / -1 not
    session_id: str | None = None  # joins to the session's last decision row
    decision_id: int | None = None  # or the explicit row id
    correction: str | None = None  # "no, the NAS is called titan" — extraction intake


@router.post("/feedback")
def post_feedback(request: Request, body: FeedbackBody):
    """Record explicit feedback for a turn (2026-08-31 #3).

    rating ±1 (gold label — the tuning sweeps weight these N× proxy rows);
    an optional correction text routes through fact extraction as a Mem0
    contradiction candidate. Returns the joined decision row when found."""
    loop = request.app.state.loop
    decision_id = body.decision_id or loop.last_decision_id(body.session_id)
    loop.store.log_turn_feedback(body.rating, session_id=body.session_id,
                                 decision_id=decision_id, correction=body.correction)
    if body.correction and body.correction.strip():
        _intake_correction(loop, body.correction.strip())
    decision = loop.store.decision_row(decision_id) if decision_id else None
    return {"recorded": True, "decision_id": decision_id, "decision": decision}


def _intake_correction(loop, text: str) -> int:
    """Route correction text through the extractor + Consolidator (2026-08-31
    #3): a user correction is exactly the Mem0 UPDATE/DELETE trigger the
    belief-revision sweep adjudicates. Returns facts stored."""
    from proharness.memory.consolidate import Consolidator
    from proharness.memory.extract import make_extractor
    from proharness.memory.schemas import Memory

    try:
        extractor = make_extractor(
            loop.router.local,
            max_facts=int(getattr(loop.settings, "extract_max_facts", 4)),
        )
        facts = extractor(text) or [type("F", (), {"text": text, "type": "semantic",
                                                   "importance": 7.0})()]
        cons = Consolidator(store=loop.store, settings=loop.settings)
        n = 0
        for fact in facts:
            if cons.store_with_consolidation(Memory(
                    text=fact.text, type=fact.type,  # type: ignore[arg-type]
                    importance=float(fact.importance), source="user-correction")):
                n += 1
        return n
    except Exception:
        return 0


@router.post("/run")
def run(request: Request, body: RunBody):
    """Run one turn synchronously and return the final result (no streaming).

    For streaming, use the WebSocket at /ws/stream. This endpoint suits clients
    that can't consume SSE/WS (simple HTTP scripts, MCP hosts that want a
    plain POST). Supports session isolation via session_id (idea #2).
    """
    loop = _loop(request)
    result = loop.run(body.text, session_id=body.session_id)
    return {
        "answer": result.answer,
        "steps": result.steps,
        "tool_calls": result.tool_calls,
        "revisions": result.revisions,
        "stopped_reason": result.stopped_reason,
        "tokens": loop.cost.total_tokens(),
        "cost_usd": round(loop.cost.total_cost_usd(), 6),
    }
