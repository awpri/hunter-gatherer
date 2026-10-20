"""Park-weight tuning from observed recall usage (2026-08-14 #1).

The memory-side mirror of ``models/router_tune.py``: the router's α is swept
against logged outcomes; here the Park weights (α·recency + β·importance +
γ·relevance) are swept against logged *recall-usage* rows — one row per
surfaced memory per turn, recording whether the memory was actually used
(label cited / re-fetched) plus the sub-scores at injection time. Because the
sub-scores are persisted, the sweep re-ranks each query's candidates offline
under candidate weights and measures used-rate@k — no re-embedding, no model
calls.

Guardrailed auto-apply mirrors the router pattern: after reflection passes,
if enough rows have accumulated, apply the recommended weights within a max
step per adjustment and a bounded range.
"""
from __future__ import annotations

from dataclasses import dataclass, field

Weights = tuple[float, float, float]  # (recency, importance, relevance)

_GRID = [0.0, 0.25, 0.5, 0.75, 1.0, 1.25, 1.5, 1.75, 2.0]


@dataclass
class RecallTuneResult:
    current: Weights
    best: Weights
    current_rate: float  # used-rate@k under current weights
    best_rate: float
    n_rows: int
    n_queries: int
    top_k: int
    curve: list[dict] = field(default_factory=list)  # representative sweep points


def _used_rate(rows: list[tuple], weights: Weights, top_k: int) -> float:
    """Mean used-rate@k across queries: re-score each query's rows with the
    candidate weights, rank, take top-k, and measure what fraction of the
    top-k was actually used (weighted by k to penalize tiny groups)."""
    α, β, γ = weights
    norm = (α + β + γ) or 1.0
    by_query: dict[str, list[tuple]] = {}
    for row in rows:
        # Newer rows append link, feedback and rewrite provenance. These
        # flags must not break the six scoring fields shared with old logs.
        q, mid, used, rec, imp, rel = row[:6]
        by_query.setdefault(q, []).append((mid, used, rec, imp, rel))
    total_weight = 0.0
    total_rate = 0.0
    for group in by_query.values():
        scored = sorted(
            group,
            key=lambda r: (α * r[2] + β * r[3] + γ * r[4]) / norm,
            reverse=True,
        )
        k = min(top_k, len(scored))
        if k == 0:
            continue
        hits_used = sum(r[1] for r in scored[:k])
        total_rate += hits_used / k * k  # weight by k
        total_weight += k
    return total_rate / total_weight if total_weight else 0.0


def sweep_weights(rows: list[tuple], current: Weights, *, top_k: int = 5) -> RecallTuneResult:
    """Grid-search (α,β,γ) over the logged usage rows to maximize used-rate@k."""
    from collections import Counter

    n_queries = len(Counter(r[0] for r in rows))
    current_rate = _used_rate(rows, current, top_k)
    best_w, best_rate = current, current_rate
    curve: list[dict] = []
    for α in _GRID:
        for β in _GRID:
            for γ in _GRID:
                if α == 0 and β == 0 and γ == 0:
                    continue
                w = (α, β, γ)
                rate = _used_rate(rows, w, top_k)
                if rate > best_rate:
                    best_w, best_rate = w, rate
                if α == 1.0 and β == 1.0:  # keep the γ axis for the report
                    curve.append({"weights": w, "used_rate": round(rate, 4)})
    return RecallTuneResult(
        current=current, best=best_w, current_rate=current_rate, best_rate=best_rate,
        n_rows=len(rows), n_queries=n_queries, top_k=top_k, curve=curve,
    )


@dataclass
class TuneAction:
    applied: bool
    weights: Weights
    note: str


def recommend_weights(
    rows: list[tuple], current: Weights, *, top_k: int = 5, min_rows: int = 200,
) -> tuple[Weights | None, int, str]:
    """Return (recommended_weights | None, n_rows, note). None when there's
    too little data to trust a move (mirror of recommend_alpha)."""
    if len(rows) < min_rows:
        return None, len(rows), f"insufficient data ({len(rows)}/{min_rows} rows) — collect more turns"
    result = sweep_weights(rows, current, top_k=top_k)
    if result.best_rate <= result.current_rate + 1e-6:
        return None, len(rows), f"current weights already optimal (used-rate {result.current_rate:.3f})"
    return result.best, len(rows), (
        f"used-rate@{top_k} {result.current_rate:.3f} → {result.best_rate:.3f} "
        f"over {result.n_queries} queries"
    )


def apply_with_guardrails(
    recommended: Weights | None, current: Weights, *,
    max_step: float = 0.25, lo: float = 0.0, hi: float = 2.0,
) -> TuneAction:
    """Move each weight toward the recommendation by at most max_step, clamped
    to [lo, hi]. Degenerate all-zero results are rejected outright."""
    if recommended is None:
        return TuneAction(False, current, "no recommendation")
    if sum(recommended) <= 0:
        return TuneAction(False, current, "degenerate recommendation (all zeros) rejected")
    moved = tuple(
        min(hi, max(lo, c + max(-max_step, min(max_step, r - c))))
        for c, r in zip(current, recommended, strict=True)
    )
    if sum(moved) <= 0:
        return TuneAction(False, current, "guardrails collapsed weights to zero")
    return TuneAction(True, moved, f"applied within guardrails (max step {max_step})")


def format_sweep(result: RecallTuneResult) -> str:
    """Human-readable sweep report (mirrors router_tune.format_sweep)."""
    cur = result.current
    best = result.best
    lines = [
        "Recall Park-weight sweep (used-rate@k from logged recall usage)",
        f"  rows: {result.n_rows}  queries: {result.n_queries}  k: {result.top_k}",
        f"  current (recency={cur[0]:.2f}, importance={cur[1]:.2f}, relevance={cur[2]:.2f})"
        f" → used-rate {result.current_rate:.3f}",
        f"  best    (recency={best[0]:.2f}, importance={best[1]:.2f}, relevance={best[2]:.2f})"
        f" → used-rate {result.best_rate:.3f}",
    ]
    if result.curve:
        lines.append("  γ-axis slice (α=1.0, β=1.0):")
        for pt in result.curve:
            w = pt["weights"]
            lines.append(f"    γ={w[2]:.2f} → {pt['used_rate']:.3f}")
    if result.best == result.current:
        lines.append("  → current weights already optimal; no change recommended.")
    else:
        lines.append(
            "  → `memory tune --apply` (or the post-reflection autotune) applies this "
            "within guardrails and persists it to learned_params; set "
            "PROHARNESS_RECENCY_WEIGHT/IMPORTANCE_WEIGHT/RELEVANCE_WEIGHT only to pin."
        )
    return "\n".join(lines)


# ── Half-Life Regression θ (2026-08-31 #1) + relevance-floor sweep (2026-08-31 #2) ──
import contextlib  # noqa: E402
import math as _math  # noqa: E402

_HLR_H0 = _math.log(2) / _math.log(1.0 / 0.99)  # ≈ 68.97h — θ=0 reproduces 0.99**hours exactly
_THETA_CAP = 1.0       # |θ_i| bound (guardrail)
_THETA_STEP = 0.25     # max per-component move per apply


def _halflife_features_of(memory) -> list[float]:
    acc = int(getattr(memory, "access_count", 0) or 0)
    imp = float(getattr(memory, "importance", 5.0)) / 10.0
    typ = getattr(memory, "type", "semantic") or "semantic"
    src = getattr(memory, "source", "") or ""
    return [
        _math.log2(acc + 1.0), imp,
        1.0 if typ == "episodic" else 0.0,
        1.0 if src == "reflection" else 0.0,
    ]


def fit_halflife_theta(store, rows: list[tuple], *, epochs: int = 400, lr: float = 0.05
                       ) -> tuple[list[float] | None, dict]:
    """Fit θ from (features, Δt, used) triples recovered from recall_usage rows
    (Δt comes back out of the logged recency sub-score) + per-memory features
    via id lookup. Hand-rolled SGD — no scikit-learn, per the house rule.
    Returns (theta | None, report). None when there's too little signal."""
    obs: list[tuple[list[float], float, float]] = []  # (x, Δt_hours, used)
    for r in rows:
        # rows: (query, memory_id, used, recency, importance, relevance, via_link, stale)
        if len(r) < 6 or r[5] is None:
            continue
        mem = store.get(r[1])
        if mem is None:
            continue
        rec = float(r[3])
        if rec <= 0.0 or rec >= 1.0:
            continue
        with contextlib.suppress(Exception):
            dt_hours = _math.log(rec) / _math.log(0.99)  # invert 0.99**Δt
            if dt_hours < 0 or dt_hours > 24 * 365:
                continue
            obs.append((_halflife_features_of(mem), dt_hours, float(r[2])))
    report = {"observations": len(obs)}
    if len(obs) < 40 or len({u for _, _, u in obs}) < 2:
        report["note"] = "insufficient observations (need ≥40 with both outcomes)"
        return None, report
    theta = [0.0, 0.0, 0.0, 0.0]
    ln2 = _math.log(2)

    def _p(x, dt, th):
        h = _HLR_H0 * 2.0 ** sum(t * xi for t, xi in zip(th, x, strict=True))
        return 2.0 ** (-dt / h), h

    for _ in range(epochs):
        grad = [0.0] * 4
        for x, dt, used in obs:
            p, h = _p(x, dt, theta)
            err = p - used
            dp_dtheta = p * (dt * ln2 * ln2 / h)  # d/dθ_i shares this scalar
            for j in range(4):
                grad[j] += err * dp_dtheta * x[j]
        for j in range(4):
            theta[j] -= lr * grad[j] / len(obs)
    theta = [max(-_THETA_CAP, min(_THETA_CAP, t)) for t in theta]
    # Fit quality: mean squared error vs the flat θ=0 baseline.
    def _mse(th):
        return sum((used - _p(x, dt, th)[0]) ** 2 for x, dt, used in obs) / len(obs)
    fitted, baseline = _mse(theta), _mse([0.0] * 4)
    report["mse_fitted"] = round(fitted, 5)
    report["mse_flat"] = round(baseline, 5)
    if fitted >= baseline:
        report["note"] = "fitted curve no better than flat (θ=0); keeping no-op default"
        return None, report
    report["theta"] = [round(t, 3) for t in theta]
    return theta, report


def guardrail_theta(recommended: list[float] | None,
                    current: list[float]) -> list[float] | None:
    """Clamp each θ component into ±cap and ≤ step from current. Returns None
    when there is nothing to apply."""
    if recommended is None:
        return None
    return [
        max(-_THETA_CAP, min(_THETA_CAP, c + max(-_THETA_STEP, min(_THETA_STEP, r - c))))
        for c, r in zip(current, recommended, strict=True)
    ]


def sweep_relevance_floor(rows: list[tuple]) -> tuple[float, dict]:
    """Fitted, not guessed (2026-08-31 #2): choose the smallest floor whose
    above-floor used-rate improves on the overall rate while keeping ≥50% of
    injected rows. Returns (recommended_floor, report)."""
    if not rows:
        return 0.0, {"note": "no rows"}
    rels = [float(r[5]) if len(r) > 5 and r[5] is not None else 0.0 for r in rows]
    used = [int(r[2]) for r in rows]
    overall = sum(used) / len(used)
    best_floor, best_gain = 0.0, 0.0
    for floor in (0.05, 0.1, 0.15, 0.2, 0.25, 0.3, 0.35, 0.4):
        above = [u for u, rel in zip(used, rels, strict=True) if rel >= floor]
        if not above or len(above) < 0.5 * len(rows):
            continue  # coverage guard: never drop half the block
        rate = sum(above) / len(above)
        gain = rate - overall
        if gain > best_gain + 1e-9:
            best_floor, best_gain = floor, gain
    return best_floor, {
        "overall_used_rate": round(overall, 3),
        "recommended_floor": best_floor,
        "gain": round(best_gain, 3),
    }
