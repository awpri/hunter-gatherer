"""Spend governor (2026-09-09 #2) — FrugalGPT's budget ceiling over the α lever.

Cost was enforced per-run (pipeline budgets, BoN budget) and per-failure
(retry/degrade ladder) but never globally: a heavy day simply spent what it
spent. The governor maps the day's burn fraction vs elapsed-day fraction to
an effective-α offset plus a tier cap consumed inside ``route()`` — modest
overrun raises α (cheaper routes within autotune guardrails), severe overrun
caps tiers at cheap/local, a hard reserve keeps a floor for interactive
turns while unattended runs degrade first. 0 budget = off (no-op default).

Research link: FrugalGPT (arXiv:2305.05176 — the §1 citation finally
instantiated) + the 80%-of-variance rule: a degraded day must be visible in
cost attribution, never silent.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

log = logging.getLogger(__name__)


@dataclass
class GovernorState:
    offset: float = 0.0   # added to α (cheaper routing)
    tier_cap: str = ""    # "", "cheap", or "local"
    burn: float = 0.0     # fraction of budget spent
    note: str = ""


class SpendGovernor:
    """Reads the day's token/cost rollup from router_decisions rows and maps
    overrun to routing pressure. Pure function of the data; stateless."""

    def __init__(self, settings, store):
        self.settings = settings
        self.store = store

    def daily_spend(self) -> dict:
        """Tokens + est. USD since local midnight, from router_decisions."""
        import datetime as _dt

        midnight = _dt.datetime.now().replace(hour=0, minute=0, second=0,
                                              microsecond=0).timestamp()
        with contextlib_suppress():
            row = self.store._sqlite.execute(  # noqa: SLF001
                "SELECT COALESCE(SUM(in_tokens + out_tokens), 0) FROM router_decisions WHERE ts >= ?",
                (midnight,),
            ).fetchone()
            return {"tokens": int(row[0])}
        return {"tokens": 0}

    def evaluate(self, *, unattended: bool = False) -> GovernorState:
        """Current governing state. ``unattended`` (scheduled pipelines,
        delegate runs) degrades first — interactive turns keep a reserve."""
        usd_budget = float(getattr(self.settings, "daily_usd_budget", 0) or 0)
        tok_budget = float(getattr(self.settings, "daily_token_budget", 0) or 0)
        if usd_budget <= 0 and tok_budget <= 0:
            return GovernorState()  # off

        spend = self.daily_spend()
        burn = 0.0
        if tok_budget > 0:
            burn = max(burn, spend["tokens"] / tok_budget)
        if usd_budget > 0:
            burn = max(burn, self._est_usd(spend["tokens"]) / usd_budget)

        # Elapsed-day fraction — overrun is burn relative to time passed.
        import datetime as _dt

        now = _dt.datetime.now()
        day_frac = (now.hour * 3600 + now.minute * 60) / 86400.0 or 0.01
        over = burn / max(day_frac, 0.01)  # projected end-of-day multiple

        if burn >= 1.0 and unattended:
            return GovernorState(offset=0.3, tier_cap="local", burn=burn,
                                 note="budget exhausted; unattended capped at local")
        if burn >= 1.0 or over >= 2.0:
            return GovernorState(offset=0.2, tier_cap="cheap", burn=burn,
                                 note=f"overrun ×{over:.1f}; capped at cheap, α+0.2")
        if over >= 1.3:
            return GovernorState(offset=0.1, burn=burn,
                                 note=f"trending over ×{over:.1f}; α+0.1 (cheaper routes)")
        return GovernorState(burn=burn)

    def _est_usd(self, tokens: int) -> float:
        # Coarse blended estimate — CostTracker holds exact totals; this is
        # the cheap projection the route path can afford per turn.
        return tokens * 5.0 / 1_000_000  # ~$5/1M blended


class contextlib_suppress:
    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return True
