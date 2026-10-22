"""α-sweep for the router (idea #1).

Given the accumulated ``router_decisions`` rows, sweep α across [0, 1] and
score each candidate by simulating what *would have* happened if that α had
been in effect: each decision's recorded score is re-classified into a tier
(local if score < α, cloud otherwise), and the recorded outcome is consulted
to estimate the cost/quality trade-off.

Objective — the **cost/quality knee**: among all α values whose simulated
*local bad-outcome rate* is within a tolerance of the floor, pick the highest
α (the most local-heavy). Rationale: cloud routes are assumed to handle
anything, so quality is bounded by what we send to local; the cheapest α that
keeps local regret acceptably low is the operating point.

This is a counterfactual replay, not a fresh run, so it's free (no model calls)
and deterministic. Its trustworthiness depends on having enough logged rows
with non-null scores; below a small sample it falls back to reporting the
current α and explaining why.
"""
from __future__ import annotations

import contextlib
from dataclasses import dataclass

# Tiers ranked by typical cost: local ≪ cheap < cloud. Relative cost units used
# only to compare α values, never as absolute $.
_TIER_COST = {"local": 0.1, "cheap": 1.0, "cloud": 3.0}


@dataclass
class SweepPoint:
    alpha: float
    # Simulated counts at this α.
    local: int
    cloud: int
    # Estimated relative cost (sum of _TIER_COST over simulated routes).
    est_cost: float
    # Local bad-outcome rate: fraction of simulated-local decisions whose
    # actual outcome was NOT good. The lower, the better. Bounded by what we
    # actually routed local and observed.
    local_bad_rate: float
    # Raw counts for display.
    local_bad: int


@dataclass
class SweepResult:
    current_alpha: float
    points: list[SweepPoint]
    recommended_alpha: float | None
    n_rows: int
    n_scored: int  # rows with non-null score (the ones the sweep can replay)
    note: str


def _simulated_tier(score: float, alpha: float) -> str:
    return "local" if score < alpha else "cloud"


def sweep(
    decisions: list,
    *,
    current_alpha: float,
    steps: int = 101,
    bad_rate_tolerance: float = 0.05,
    min_scored: int = 8,
) -> SweepResult:
    """Run the α-sweep over logged decisions.

    `decisions` is a list of RouterDecision. Only rows with non-null `score`
    contribute (rule/heuristic routes carry no score to replay).
    """
    scored = [d for d in decisions if d.score is not None]
    n_scored = len(scored)

    if n_scored < min_scored:
        return SweepResult(
            current_alpha=current_alpha,
            points=[],
            recommended_alpha=None,
            n_rows=len(decisions),
            n_scored=n_scored,
            note=(
                f"only {n_scored} scored decisions (< {min_scored} needed); "
                "collect more turns before tuning. Current α kept."
            ),
        )

    alphas = [i / (steps - 1) for i in range(steps)]
    points: list[SweepPoint] = []

    for a in alphas:
        local_n = cloud_n = 0
        local_bad = 0
        cost = 0.0
        for d in scored:
            tier = _simulated_tier(d.score, a)
            if tier == "local":
                local_n += 1
                cost += _TIER_COST["local"]
                if d.outcome != "good":
                    local_bad += 1
            else:
                cloud_n += 1
                cost += _TIER_COST["cloud"]
        local_bad_rate = (local_bad / local_n) if local_n else 0.0
        points.append(SweepPoint(
            alpha=a, local=local_n, cloud=cloud_n,
            est_cost=cost, local_bad_rate=local_bad_rate, local_bad=local_bad,
        ))

    # Knee: among α values whose local_bad_rate is within `bad_rate_tolerance`
    # of the floor, pick the HIGHEST α (most local-heavy = cheapest) — but
    # never exceed the current α by more than the tolerance band (we don't
    # want to autonomously slash quality). If the floor itself is above the
    # tolerance, the data is saying "local can't handle these at any α" →
    # recommend lowering α (send more to cloud) and flag it.
    floor_rate = min(p.local_bad_rate for p in points)
    target_rate = floor_rate + bad_rate_tolerance
    within_band = [p for p in points if p.local_bad_rate <= target_rate]
    if within_band:
        # Highest α in the band = cheapest operating point that holds quality.
        knee_point = max(within_band, key=lambda p: p.alpha)
        recommended = round(knee_point.alpha, 2)
        note = (
            f"swept {steps} α values over {n_scored} scored decisions; "
            f"local bad-outcome floor={floor_rate:.2f}, recommending the "
            f"cheapest α within tol={bad_rate_tolerance} of that floor."
        )
    else:
        # Even the most cloud-heavy simulated α has a bad local rate above tol
        # → either tiny local_n (noise) or local model genuinely underpowered.
        # Recommend the lowest-α (most cloud) point and flag for review.
        knee_point = min(points, key=lambda p: p.alpha)
        recommended = round(knee_point.alpha, 2)
        note = (
            f"no α keeps local bad-rate within tol={bad_rate_tolerance} "
            f"(floor={floor_rate:.2f}); recommending the most cloud-heavy α "
            f"but review whether the local model is underpowered or the data "
            f"is sparse."
        )
    return SweepResult(
        current_alpha=current_alpha,
        points=points,
        recommended_alpha=recommended,
        n_rows=len(decisions),
        n_scored=n_scored,
        note=note,
    )


@dataclass
class TuneAction:
    """Outcome of a closed-loop auto-tune step (idea #3)."""

    applied: bool                # was α actually changed?
    old_alpha: float
    new_alpha: float
    recommended_alpha: float | None  # raw sweep recommendation (pre-guardrail)
    reason: str
    n_decisions: int


def recommend_alpha(
    decisions: list,
    *,
    current_alpha: float,
    min_decisions: int = 100,
) -> tuple[float | None, int, str]:
    """Run the sweep and return (recommended_alpha, n_scored, note).

    Thin wrapper over ``sweep`` that returns just the value + confidence for
    the closed-loop path. Returns ``(None, n_scored, reason)`` when there
    isn't enough data to recommend. ``min_decisions`` gates the closed loop:
    below it, the sweep declines (idea #3: only re-tune when meaningful new
    data has accumulated).
    """
    scored = [d for d in decisions if d.score is not None]
    if len(scored) < min_decisions:
        return None, len(scored), (
            f"only {len(scored)} scored decisions (< {min_decisions} needed); "
            "not enough data to auto-tune."
        )
    result = sweep(decisions, current_alpha=current_alpha)
    return result.recommended_alpha, result.n_scored, result.note


def apply_with_guardrails(
    recommended: float | None,
    current_alpha: float,
    *,
    alpha_min: float,
    alpha_max: float,
    max_step: float,
    n_decisions: int,
    reason: str = "",
) -> TuneAction:
    """Clamp a sweep recommendation to safe bounds and cap the per-step move.

    Returns a TuneAction describing what (if anything) changed. The closed
    loop calls this and writes ``new_alpha`` back to settings + persists it.
    """
    if recommended is None:
        return TuneAction(
            applied=False, old_alpha=current_alpha, new_alpha=current_alpha,
            recommended_alpha=None, n_decisions=n_decisions, reason=reason,
        )
    # Clamp to safe range.
    clamped = max(alpha_min, min(alpha_max, recommended))
    # Cap the per-step move so α drifts gradually, never jumps.
    lo, hi = current_alpha - max_step, current_alpha + max_step
    target = max(lo, min(hi, clamped))
    # No-op if the move rounds to zero.
    if abs(target - current_alpha) < 1e-6:
        return TuneAction(
            applied=False, old_alpha=current_alpha, new_alpha=current_alpha,
            recommended_alpha=recommended, n_decisions=n_decisions,
            reason=f"recommended {recommended:.3f} but within max_step={max_step} of current; no move.",
        )
    return TuneAction(
        applied=True, old_alpha=current_alpha, new_alpha=round(target, 4),
        recommended_alpha=recommended, n_decisions=n_decisions,
        reason=f"sweep recommended {recommended:.3f}; clamped to [{alpha_min},{alpha_max}], "
               f"stepped by ≤{max_step} → {target:.4f}.",
    )


def gold_from_feedback(store) -> dict[str, int]:
    """Explicit human feedback (2026-08-31 #3) as query → ±1 gold labels,
    joined through turn_feedback.decision_id → router_decisions.query."""
    gold: dict[str, int] = {}
    with contextlib.suppress(Exception):
        for row in store.fetch_turn_feedback(limit=10_000):
            if row.get("decision_id") is None:
                continue
            decision = store.decision_row(row["decision_id"])
            if decision and decision.get("query"):
                gold[decision["query"]] = int(row["rating"])
    return gold


def apply_gold_outcomes(decisions, gold: dict[str, int]):
    """Override proxy outcomes with gold ratings (+1 → 'good'; −1 → 'repeat',
    the sweep's bad-outcome bucket). Returns new objects; originals untouched."""
    if not gold:
        return decisions
    import dataclasses

    out = []
    for d in decisions:
        if d.query in gold:
            d = dataclasses.replace(d, outcome="good" if gold[d.query] > 0 else "repeat")
        out.append(d)
    return out


def format_sweep(result: SweepResult, *, top_k: int = 11) -> str:
    """Render a SweepResult for the CLI. Shows a sampled table + recommendation."""
    lines = [f"α-sweep  ({result.n_scored}/{result.n_rows} decisions scored)"]
    lines.append("")
    if not result.points:
        lines.append(f"  {result.note}")
        lines.append(f"  current α = {result.current_alpha} (unchanged)")
        return "\n".join(lines)

    pts = result.points
    half_step = 1 / (2 * (len(pts) - 1))
    # Sample top_k points across the sweep.
    step = max(1, len(pts) // top_k)
    sampled = pts[::step][:top_k]
    lines.append(
        f"  {'α':>5}  {'local':>6} {'cloud':>6}  {'cost':>7}  {'bad_rate':>9}  {'local_bad':>9}"
    )
    for p in sampled:
        marker = ""
        if abs(p.alpha - result.current_alpha) < half_step:
            marker = " ◀ current"
        if result.recommended_alpha is not None and abs(p.alpha - result.recommended_alpha) < half_step:
            marker = " ◀ recommended"
        lines.append(
            f"  {p.alpha:>5.2f}  {p.local:>6d} {p.cloud:>6d}  {p.est_cost:>7.1f}  "
            f"{p.local_bad_rate:>9.2f}  {p.local_bad:>9d}{marker}"
        )
    lines.append("")
    lines.append(f"  {result.note}")
    if result.recommended_alpha is not None:
        lines.append(
            f"  recommended α = {result.recommended_alpha}  "
            f"(autotune applies this within guardrails after reflection passes and "
            f"persists it — set HG_ROUTER_ALPHA={result.recommended_alpha} "
            f"only to pin it against autotune)"
        )
    else:
        lines.append(f"  current α = {result.current_alpha} (no change recommended)")
    return "\n".join(lines)
