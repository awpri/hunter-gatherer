"""Tier-aware context budget (2026-08-20 #2).

"Memory is context management" (Letta) — but the shipped code sized context
tier-blindly: a fixed 12-turn history window, a fixed recall top-k, and a
fixed tool-spec set, identical for a 4k-token local model and a 200k cloud
model. Silent overflow risk at local; pure per-token waste at cloud.

The budgeter maps the routed tier to a context window, reserves output
headroom, and allocates the remainder across the turn's prompt parts in
priority order:

    system + matched skill → rehearsal window → recall block → tool specs
    → history (trimmed oldest-first; the on_evict summarization hook already
    pages what leaves into long-term memory)

Recall shrinks its top-k on tight budgets rather than dropping recall
entirely. Windows are overridable via ``PROHARNESS_CONTEXT_WINDOWS``
("local=4096,cheap=32768,cloud=200000").
"""
from __future__ import annotations

import json
from dataclasses import replace

DEFAULT_WINDOWS = {"local": 4096, "cheap": 32768, "cloud": 200000}


def parse_windows(override: str) -> dict[str, int]:
    """Parse a "tier=tokens,..." override string into a window dict."""
    out: dict[str, int] = {}
    for part in (override or "").split(","):
        part = part.strip()
        if not part or "=" not in part:
            continue
        tier, _, value = part.partition("=")
        try:
            out[tier.strip().lower()] = int(value.strip())
        except ValueError:
            continue
    return out


def window_for(tier: str, settings=None) -> int:
    """Context window for a routed tier (override > per-tier default > 32k)."""
    overrides = parse_windows(getattr(settings, "context_windows", "") or "")
    if tier in overrides:
        return overrides[tier]
    if "all" in overrides:
        return overrides["all"]
    return DEFAULT_WINDOWS.get(tier, 32_000)


def estimate_tokens(text: str) -> int:
    """Cheap ~4-chars/token estimate — consistent with tools.base."""
    return max(1, len(text or "") // 4)


class ContextBudgeter:
    """Allocate a tier's context budget across one turn's prompt parts."""

    def __init__(self, settings=None):
        self.settings = settings
        self.output_reserve = int(getattr(settings, "context_output_reserve", 2048) or 0)

    def budget(self, tier: str) -> int:
        """Input-token budget for the turn: window minus output headroom."""
        return max(512, window_for(tier, self.settings) - self.output_reserve)

    def fit_recall(self, hits, available_tokens: int, *, per_hit_slack: int = 24):
        """Return the largest prefix of Park-ranked hits whose rendered block
        fits in ``available_tokens``. Always keeps at least the top hit when
        one exists — recall shrinks, it doesn't vanish."""
        kept = []
        used = 0
        for h in hits:
            cost = estimate_tokens(h.memory.text) + per_hit_slack
            if kept and used + cost > available_tokens:
                break
            kept.append(h)
            used += cost
        return kept

    def fit_specs(self, specs, available_tokens: int, *, always_on=()):
        """Drop optional specs, then omit parameter prose if mandatory specs
        still overflow. Always-on tools retain their validation contracts.
        Overlong tool descriptions may reduce to their first sentence;
        structural minimums can exceed tiny budgets."""
        kept = list(specs)
        # Descriptions and protocol framing count too; large retrieval schemas
        # cannot be priced as parameter dictionaries plus a fixed allowance.
        while kept and estimate_tokens(json.dumps([s.to_openai() for s in kept])) > available_tokens:
            for i in range(len(kept) - 1, -1, -1):
                if kept[i].name not in always_on:
                    kept.pop(i)
                    break
            else:
                # Keep the mandatory retrieval tools and their validation
                # contracts, but omit parameter prose when framing overflows.
                # Copy schemas so a constrained worker never mutates its parent.
                def compact(value):
                    if isinstance(value, dict):
                        return {key: compact(item) for key, item in value.items()
                                if key != "description"}
                    if isinstance(value, list):
                        return [compact(item) for item in value]
                    return value

                kept = [replace(spec, parameters=compact(spec.parameters)) for spec in kept]
                if estimate_tokens(json.dumps([s.to_openai() for s in kept])) > available_tokens:
                    # Purpose stays explicit; omit repeated prose before exceeding
                    # the prompt budget. Never change validation or registry specs.
                    kept = [replace(spec, description=spec.description.split(". ", 1)[0].rstrip(".") + ".")
                            for spec in kept]
                break  # structural contracts may exceed impossibly small budgets
        return kept

    def fit_history(self, history, available_tokens: int):
        """Keep the newest turns whose estimated tokens fit (oldest-first
        trimming; pairs stay aligned)."""
        if available_tokens <= 0:
            return []
        kept = list(history)
        while kept and sum(estimate_tokens(m.content or "") + 8 for m in kept) > available_tokens:
            kept = kept[2:] if len(kept) > 1 else []
        return kept
