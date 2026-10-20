"""Closed-loop persistence for learned parameters.

History: the autotune loops mutated ``settings`` in memory
(``self.settings.router_alpha = ...``) and never persisted — every tuned α
and Park weight reverted to ``.env`` values on restart, discarding all
accumulated tuning, while ``apply_with_guardrails``'s own docstring claimed
the caller persists. This module closes that loop:

  - ``set_learned_param`` appends every applied value to the
    ``learned_params`` table (write-on-apply; the history is the audit trail).
  - ``hydrate_learned_params`` runs at AgentLoop construction and applies the
    newest stored value to any field that was NOT explicitly set —
    **env > stored > default** precedence. An explicit ``PROHARNESS_*``
    override therefore pins a knob (disabling autotune for it); everything
    else rehydrates so tuning survives restarts.

Tunable keys are declared below with their field names and parsers; add a
key here + a ``set_learned_param`` call at the apply site to extend.
"""
from __future__ import annotations

import contextlib
import json
import logging

log = logging.getLogger(__name__)


def _parse_float_list(raw: str):
    with contextlib.suppress(Exception):
        parsed = json.loads(raw)
        if isinstance(parsed, list) and len(parsed) == 4:
            return [float(v) for v in parsed]
    return None


# key → (settings field name, parser str → value)
TUNABLE: dict[str, tuple[str, type]] = {
    "router_alpha": ("router_alpha", float),
    "recency_weight": ("recency_weight", float),
    "importance_weight": ("importance_weight", float),
    "relevance_weight": ("relevance_weight", float),
    "recall_halflife_theta": ("recall_halflife_theta", _parse_float_list),
    "recall_relevance_floor": ("recall_relevance_floor", float),
}


def hydrate_learned_params(settings, store) -> dict[str, object]:
    """Apply newest stored values to unset fields. Returns the applied map
    (key → value) — empty when nothing rehydrated."""
    applied: dict[str, object] = {}
    explicitly_set = set(getattr(settings, "model_fields_set", ()) or ())
    for key, (field, parser) in TUNABLE.items():
        if field in explicitly_set:
            continue  # env/kwarg override wins — pinned knob, no autotune
        raw = store.latest_learned_param(key)
        if raw is None:
            continue
        try:
            value = parser(raw)
        except (TypeError, ValueError):
            log.debug("learned param %s=%r unparseable; skipping", key, raw)
            continue
        if value is None:
            continue  # parser rejected the stored form
        setattr(settings, field, value)
        applied[key] = value
    if applied:
        log.info("hydrated learned params: %s", applied)
    return applied


def persist_learned_param(store, key: str, value, source: str = "autotune") -> None:
    """Write-on-apply helper used by the autotune loops and the CLI."""
    if key not in TUNABLE:
        raise KeyError(f"not a declared tunable: {key}")
    store.set_learned_param(key, value, source=source)
