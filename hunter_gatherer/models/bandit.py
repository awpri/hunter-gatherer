"""Within-tier model selection (2026-09-01 #3) — a UCB1 cost/quality bandit.

The router's learned stack optimizes which *tier* handles a query while
config pins exactly one model per tier. This module adds the arm dimension:
per-tier candidate lists (singleton lists reproduce today's behavior exactly
— the no-op-safety house pattern) and hand-rolled UCB1 (Auer et al. 2002; no
scikit-learn, per the router-classifier rule) over (tier, model) arms.

Reward = outcome label (good=1 / revised=0.5 / repeat=0 / max_steps=0) from
the existing router_decisions signal, minus λ·normalized per-model dollars
from the arm's own cost history. Statistics persist through the store's
``model_arms`` table; UCB's optimism handles cold start.

Research link: RouteLLM (model-pair choice under cost/quality — the lever
extended from tier to model) + Auer et al., "Finite-time Analysis of the
Multiarmed Bandit Problem" + Anthropic's 80%-of-variance finding (per-model
dollars feed the reward directly).
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

_OUTCOME_REWARD = {"good": 1.0, "revised": 0.5, "repeat": 0.0, "max_steps": 0.0}


def candidates_for(tier: str, settings) -> list[str]:
    """Per-tier model candidates from config; a singleton (or empty → the
    pinned model) reproduces today's one-model-per-tier behavior exactly."""
    raw = ""
    if tier == "cheap":
        raw = getattr(settings, "cheap_models", "") or ""
    elif tier == "cloud":
        raw = getattr(settings, "cloud_models", "") or ""
    return [m.strip() for m in raw.split(",") if m.strip()]


@dataclass
class ArmStats:
    model: str
    pulls: int = 0
    reward_sum: float = 0.0
    cost_sum: float = 0.0  # USD total, normalized by pulls at reward time
    meta: dict = field(default_factory=dict)


class ModelBandit:
    """UCB1 over (tier, model) arms with cost-penalized reward. All SQL
    lives in MemoryStore's model-arms methods."""

    def __init__(self, store, settings):
        self.store = store
        self.settings = settings

    def _stats(self, tier: str) -> dict[str, ArmStats]:
        return {r[0]: ArmStats(r[0], int(r[1]), float(r[2]), float(r[3]))
                for r in self.store.fetch_model_arms(tier)}

    def select(self, tier: str, pinned_model: str) -> str:
        """Pick the arm for this tier. Single candidate (or exploration off)
        → the pinned behavior; else UCB1 with optimism for cold start."""
        if not getattr(self.settings, "bandit_exploration", True):
            return pinned_model
        candidates = candidates_for(tier, self.settings)
        if len(candidates) <= 1:
            return pinned_model
        stats = self._stats(tier)
        for cand in candidates:
            stats.setdefault(cand, ArmStats(cand))
        total_pulls = sum(a.pulls for a in stats.values()) or 1
        lam = float(getattr(self.settings, "bandit_cost_lambda", 0.2) or 0)

        def ucb(model: str) -> float:
            a = stats[model]
            if a.pulls == 0:
                return float("inf")  # optimism: try every arm once
            q = a.reward_sum / a.pulls
            if lam > 0 and a.cost_sum > 0:
                q -= lam * (a.cost_sum / a.pulls)
            return q + math.sqrt(2.0 * math.log(total_pulls + 1) / a.pulls)

        return max(candidates, key=ucb)

    def record(self, tier: str, model: str, outcome: str, cost_usd: float = 0.0,
               weight: int = 1) -> None:
        """Record a pull: reward from the outcome label (gold feedback can
        pass weight>1), cost from CostTracker-derived dollars."""
        reward = _OUTCOME_REWARD.get(outcome, 0.5)
        n = max(1, int(weight))
        for _ in range(n):
            self.store.upsert_model_arm(tier, model, reward, cost_usd / n)

    def table(self, tier: str | None = None) -> list[dict]:
        """Arm statistics for observability / the λ sweep."""
        return self.store.model_arms_table(tier)
