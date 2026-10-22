"""Dynamic MCP tool registration for saved pipelines.

Each saved pipeline auto-registers as its own MCP tool named
``hg_pipeline_<slug>``, whose input schema is derived from the
pipeline's declared input variables. The tool dispatches to the
PipelineInterpreter via the shared loop.

Modelled on ``mcp/server.py::_make_and_register`` — synthesizes a typed Python
closure whose signature the SDK infers into an accurate JSON schema.

Lifecycle:
  - save/update → ``unregister_pipeline_tool`` then ``register_pipeline_tool``
    (MCPServer.add_tool short-circuits on duplicate names, so remove-then-add).
  - delete → ``unregister_pipeline_tool``.
  - startup → ``register_all_pipeline_tools`` from the DB (survives restart).
"""
from __future__ import annotations

import inspect
import logging
from typing import Any

from hunter_gatherer.pipelines.schemas import Pipeline

log = logging.getLogger(__name__)

_TYPE_MAP = {"text": str, "json": dict, "list": list, "file": str, "any": str, "memory[]": str}


def _tool_name(slug: str) -> str:
    return f"hg_pipeline_{slug}"


def _tool_description(pipeline: Pipeline) -> str:
    inputs_desc = ", ".join(v.name for v in pipeline.inputs) or "none"
    base = pipeline.description or f"Run the '{pipeline.name}' pipeline."
    return (
        f"{base} Inputs: {inputs_desc}. "
        "Executes a multi-step AI pipeline (model calls, memory operations, "
        "scripts) and returns the terminal outputs + per-node transcript."
    )


def register_pipeline_tool(mcp_server: Any, loop: Any, pipeline: Pipeline) -> str:
    """Register a pipeline as an MCP tool. Returns the tool name."""
    name = _tool_name(pipeline.slug)
    description = _tool_description(pipeline)

    # Build the typed closure — one parameter per declared input variable.
    params: list[inspect.Parameter] = []
    for var in pipeline.inputs:
        py_type = _TYPE_MAP.get(var.type, str)
        default = var.default if var.default else None
        params.append(inspect.Parameter(
            var.name,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            default=default,
            annotation=py_type,
        ))

    # The dispatch body: run the pipeline with the provided inputs.
    def _dispatch(_loop=loop, _pipeline=pipeline, **kwargs) -> str:
        from hunter_gatherer.mcp.tools import serialize_result
        from hunter_gatherer.pipelines.interpreter import PipelineInterpreter
        from hunter_gatherer.pipelines.nodes import PipelineContext

        # Filter out None values (unset optional params).
        inputs = {k: v for k, v in kwargs.items() if v is not None}
        ctx = PipelineContext.from_loop(_loop)
        interp = PipelineInterpreter(_pipeline, ctx)
        result = interp.run(inputs=inputs)
        return serialize_result({
            "outputs": result.outputs,
            "stopped_reason": result.stopped_reason,
            "total_tokens": result.total_tokens,
            "total_cost_usd": result.total_cost_usd,
            "nodes_run": len(result.transcript),
            "error": result.error,
            "transcript": [
                {"node": t.node_id, "kind": t.kind, "latency_ms": t.latency_ms,
                 "tokens": t.tokens, "error": t.error}
                for t in result.transcript
            ],
        })

    sig = inspect.Signature(parameters=params)
    _dispatch.__signature__ = sig  # type: ignore[attr-defined]
    _dispatch.__name__ = name
    _dispatch.__doc__ = description

    mcp_server.add_tool(_dispatch, name=name, description=description)
    log.info("registered pipeline tool: %s (%d inputs)", name, len(pipeline.inputs))
    return name


def unregister_pipeline_tool(mcp_server: Any, slug: str) -> bool:
    """Remove a pipeline's MCP tool. Returns True if it was removed.
    Safe to call when the tool was never registered."""
    name = _tool_name(slug)
    try:
        mcp_server.remove_tool(name)
        log.info("unregistered pipeline tool: %s", name)
        return True
    except Exception:
        return False  # wasn't registered (e.g. first save, or already removed)


def register_all_pipeline_tools(mcp_server: Any, loop: Any) -> int:
    """Register MCP tools for every saved pipeline. Called at startup so tools
    survive a restart. Returns the count registered."""
    store = loop.store
    count = 0
    for pipeline in store.list_pipelines():
        register_pipeline_tool(mcp_server, loop, pipeline)
        count += 1
    if count:
        log.info("registered %d pipeline tools from DB", count)
    return count
