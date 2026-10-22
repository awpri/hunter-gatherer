"""Pipeline CRUD + validate + run endpoints.

Wraps MemoryStore pipeline persistence + the PipelineInterpreter. The run
endpoint is synchronous (for a streaming run, use the WebSocket at
/ws/pipeline/{id}/run). MCP auto-registration happens on save/update/delete
via app.state.mcp_server if present.
"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

router = APIRouter()


# ── request/response models ──────────────────────────────────────────────────
class NodeModel(BaseModel):
    id: str
    kind: str
    config: dict = {}
    position: dict = {"x": 0, "y": 0}


class EdgeModel(BaseModel):
    id: str = ""
    source: str
    source_port: str = "out"
    target: str
    target_port: str = "in"


class VariableModel(BaseModel):
    name: str
    type: str = "text"
    default: str = ""
    description: str = ""


class PipelineIn(BaseModel):
    """Body for POST/PUT. The full pipeline definition."""
    name: str = ""
    slug: str = ""
    description: str = ""
    inputs: list[VariableModel] = []
    nodes: list[NodeModel] = []
    edges: list[EdgeModel] = []
    token_budget: int | None = None  # per-run token cap; 0/None = unlimited
    trigger: dict | None = None  # {"cron": "..."} | {"interval_minutes": N} | {"importance_threshold": N}


class PipelineOut(BaseModel):
    id: str
    name: str
    slug: str
    description: str
    inputs: list[dict]
    nodes: list[dict]
    edges: list[dict]
    token_budget: int | None = None
    trigger: dict | None = None
    version: int
    created_at: float
    updated_at: float


class RunBody(BaseModel):
    inputs: dict = {}


def _store(request: Request):
    return request.app.state.loop.store


def _to_out(p) -> PipelineOut:
    d = p.to_dict()
    return PipelineOut(**d)


def _register_mcp(request: Request, pipeline) -> None:
    """Auto-register/unregister the pipeline as an MCP tool (no-op if MCP is off)."""
    mcp_server = getattr(request.app.state, "mcp_server", None)
    if mcp_server is None:
        return
    from hunter_gatherer.mcp.pipeline_tools import register_pipeline_tool, unregister_pipeline_tool

    unregister_pipeline_tool(mcp_server, pipeline.slug)  # remove-then-add (dedup short-circuit)
    register_pipeline_tool(mcp_server, request.app.state.loop, pipeline)


def _unregister_mcp(request: Request, slug: str) -> None:
    mcp_server = getattr(request.app.state, "mcp_server", None)
    if mcp_server is None:
        return
    from hunter_gatherer.mcp.pipeline_tools import unregister_pipeline_tool

    unregister_pipeline_tool(mcp_server, slug)


# ── CRUD ─────────────────────────────────────────────────────────────────────
@router.get("", response_model=list[PipelineOut])
def list_pipelines(request: Request):
    return [_to_out(p) for p in _store(request).list_pipelines()]


@router.post("", response_model=PipelineOut, status_code=201)
def create_pipeline(request: Request, body: PipelineIn):
    from hunter_gatherer.pipelines.schemas import Pipeline

    p = Pipeline.from_dict({
        "name": body.name, "slug": body.slug, "description": body.description,
        "inputs": [v.model_dump() for v in body.inputs],
        "nodes": [n.model_dump() for n in body.nodes],
        "edges": [e.model_dump() for e in body.edges],
        "token_budget": body.token_budget,
        "trigger": body.trigger,
    })
    _store(request).save_pipeline(p)
    _register_mcp(request, p)
    return _to_out(p)


@router.get("/{pipeline_id}", response_model=PipelineOut)
def get_pipeline(request: Request, pipeline_id: str):
    p = _store(request).get_pipeline(pipeline_id)
    if not p:
        raise HTTPException(404, "pipeline not found")
    return _to_out(p)


@router.put("/{pipeline_id}", response_model=PipelineOut)
def update_pipeline(request: Request, pipeline_id: str, body: PipelineIn):
    store = _store(request)
    p = store.get_pipeline(pipeline_id)
    if not p:
        raise HTTPException(404, "pipeline not found")
    # Update fields from body, preserving id + created_at.
    p.name = body.name
    p.slug = body.slug or p.slug
    p.description = body.description
    from hunter_gatherer.pipelines.schemas import Edge, Node, Variable

    p.inputs = [Variable(**v.model_dump()) for v in body.inputs]
    p.nodes = [Node(**n.model_dump()) for n in body.nodes]
    p.edges = [Edge(**e.model_dump()) for e in body.edges]
    p.token_budget = body.token_budget
    p.trigger = body.trigger
    p.version += 1
    store.save_pipeline(p)
    _register_mcp(request, p)
    return _to_out(p)


@router.delete("/{pipeline_id}")
def delete_pipeline(request: Request, pipeline_id: str):
    store = _store(request)
    p = store.get_pipeline(pipeline_id)
    if not p:
        raise HTTPException(404, "pipeline not found")
    slug = p.slug
    store.delete_pipeline(pipeline_id)
    _unregister_mcp(request, slug)
    return {"deleted": True, "id": pipeline_id}


# ── validate (dry-run) ───────────────────────────────────────────────────────
@router.post("/{pipeline_id}/validate")
def validate_pipeline(request: Request, pipeline_id: str):
    from hunter_gatherer.pipelines.interpreter import PipelineInterpreter
    from hunter_gatherer.pipelines.nodes import PipelineContext

    p = _store(request).get_pipeline(pipeline_id)
    if not p:
        raise HTTPException(404, "pipeline not found")
    ctx = PipelineContext.from_loop(request.app.state.loop)
    interp = PipelineInterpreter(p, ctx)
    errors = interp.validate()
    return {"valid": len(errors) == 0, "errors": errors}


@router.post("/validate", response_model=dict)
def validate_body(request: Request, body: PipelineIn):
    """Validate a pipeline definition without saving (for live editor feedback)."""
    from hunter_gatherer.pipelines.interpreter import PipelineInterpreter
    from hunter_gatherer.pipelines.nodes import PipelineContext
    from hunter_gatherer.pipelines.schemas import Edge, Node, Pipeline

    p = Pipeline(
        name=body.name, slug=body.slug or "draft",
        inputs=[v.model_dump() for v in body.inputs],
        nodes=[Node(**n.model_dump()) for n in body.nodes],
        edges=[Edge(**e.model_dump()) for e in body.edges],
    )
    ctx = PipelineContext.from_loop(request.app.state.loop)
    interp = PipelineInterpreter(p, ctx)
    errors = interp.validate()
    return {"valid": len(errors) == 0, "errors": errors}


# ── synchronous run (for non-streaming callers) ──────────────────────────────
@router.post("/{pipeline_id}/run")
def run_pipeline(request: Request, pipeline_id: str, body: RunBody):
    from hunter_gatherer.pipelines.interpreter import PipelineInterpreter
    from hunter_gatherer.pipelines.nodes import PipelineContext

    p = _store(request).get_pipeline(pipeline_id)
    if not p:
        raise HTTPException(404, "pipeline not found")
    ctx = PipelineContext.from_loop(request.app.state.loop)
    interp = PipelineInterpreter(p, ctx)
    result = interp.run(inputs=body.inputs)
    return {
        "outputs": result.outputs,
        "stopped_reason": result.stopped_reason,
        "total_tokens": result.total_tokens,
        "total_cost_usd": result.total_cost_usd,
        "error": result.error,
        "transcript": [
            {
                "node_id": t.node_id, "kind": t.kind,
                "outputs": t.outputs, "latency_ms": t.latency_ms,
                "tokens": t.tokens, "cost_usd": t.cost_usd, "error": t.error,
            }
            for t in result.transcript
        ],
    }
