"""Settings endpoints.

GET returns all settings grouped by runtime-tunable vs startup-only. PATCH
applies only runtime-tunable fields (rejects startup-only ones with 409).
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

router = APIRouter()

# Fields safe to mutate on a live Settings instance (read each turn).
RUNTIME_TUNABLE = {
    "router_alpha", "router_trivial_max_chars", "local_only",
    "router_autotune_enabled", "router_autotune_min_decisions",
    "router_alpha_min", "router_alpha_max", "router_alpha_max_step",
    "recency_weight", "importance_weight", "relevance_weight", "recall_topk",
    "dedup_cosine", "reflect_importance_threshold", "rehearsal_window",
    "tts",
    "price_local", "price_gpt4o_mini_in", "price_gpt4o_mini_out",
    "price_gpt4o_in", "price_gpt4o_out",
}
# Price fields are all runtime-tunable (the loop reads them live).
for _f in ("price_local", "price_gpt4o_mini_in", "price_gpt4o_mini_out",
           "price_gpt4o_in", "price_gpt4o_out"):
    assert _f in RUNTIME_TUNABLE


class SettingsPatch(BaseModel):
    """A partial update; only runtime-tunable keys are accepted."""
    # Allow arbitrary keys; we validate in the handler.
    model_config = {"extra": "allow"}


def _settings(request: Request):
    return request.app.state.settings


@router.get("")
def get_settings(request: Request):
    """All settings, grouped by mutability."""
    s = _settings(request)
    data = s.model_dump()
    # The UI needs configuration status, never credential bytes or MCP env.
    for key in data:
        if key.endswith(("_api_key", "_token", "_password", "_secret")) or key == "mcp_servers":
            data[key] = "[configured]" if data[key] else ""
    runtime = {k: v for k, v in data.items() if k in RUNTIME_TUNABLE}
    startup = {k: v for k, v in data.items() if k not in RUNTIME_TUNABLE}
    return {"runtime_tunable": runtime, "startup_only": startup}


@router.patch("")
def patch_settings(request: Request, body: SettingsPatch):
    """Apply runtime-tunable settings live. Startup-only fields → 409."""
    s = _settings(request)
    rejected: dict[str, str] = {}
    applied: dict = {}
    for key, value in body.model_dump().items():
        if key in RUNTIME_TUNABLE:
            setattr(s, key, value)
            applied[key] = value
            # α changes should bust the router's route cache.
            if key == "router_alpha" and hasattr(request.app.state, "loop"):
                request.app.state.loop.router._route_cache.clear()  # noqa: SLF001
        else:
            rejected[key] = "startup-only; restart required"
    if rejected:
        raise HTTPException(
            409,
            detail={"rejected": rejected, "applied": applied,
                    "hint": "startup-only fields require a server restart"},
        )
    return {"applied": applied}
