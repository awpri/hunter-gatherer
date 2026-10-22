"""Saved pipelines as agent-callable Tools (2026-08-12 #1).

Each saved pipeline auto-registers as an *MCP* tool for external hosts
(``mcp/pipeline_tools.py``); this module exposes the same capability to the
internal ``AgentLoop`` — the RAG-over-tools selector can then surface
"this query looks like the 'research-and-store' pipeline" and the agent calls
``run_pipeline_<slug>`` instead of re-deriving the procedure turn after turn.

The tool's name/description/input-schema are derived from the pipeline's
declared ``inputs``; dispatch runs the ``PipelineInterpreter`` through a
``PipelineContext`` built from the owning loop (shared store/router/cost).
"""
from __future__ import annotations

import json
from typing import Any

from hunter_gatherer.tools.base import Tool, ToolContext, ToolResult

_TYPE_MAP = {"text": "string", "json": "object", "list": "array", "file": "string", "any": "string", "memory[]": "string"}


class PipelineRunnerTool(Tool):
    """Wraps one saved Pipeline as a callable Tool for the agent loop."""

    def __init__(self, pipeline, loop):
        self.pipeline = pipeline
        self.loop = loop
        self.name = f"run_pipeline_{pipeline.slug}"
        self.description = (
            pipeline.description
            or f"Run the '{pipeline.name}' pipeline."
        ) + " Multi-step AI pipeline (model calls, memory ops, scripts); returns the terminal outputs."

        properties: dict[str, Any] = {}
        required: list[str] = []
        for v in pipeline.inputs:
            properties[v.name] = {
                "type": _TYPE_MAP.get(v.type, "string"),
                "description": v.description or f"Input '{v.name}' for the pipeline.",
            }
            if not v.default:
                required.append(v.name)
        self.parameters: dict[str, Any] = {
            "type": "object",
            "properties": properties,
            "required": required,
        }

    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult:
        from hunter_gatherer.pipelines.interpreter import PipelineInterpreter
        from hunter_gatherer.pipelines.nodes import PipelineContext

        # Prefer the loop captured at construction; fall back to the ctx hints.
        loop = self.loop
        if loop is None:
            return ToolResult.err("pipeline tool has no owning loop")
        pctx = PipelineContext.from_loop(loop)
        interp = PipelineInterpreter(self.pipeline, pctx)
        result = interp.run(inputs=args)
        if result.error:
            return ToolResult.err(f"pipeline error: {result.error}")
        payload = {
            "outputs": result.outputs,
            "stopped_reason": result.stopped_reason,
            "nodes_run": len(result.transcript),
            "tokens": result.total_tokens,
        }
        return ToolResult.ok(json.dumps(payload, default=str), stopped_reason=result.stopped_reason,
                             tokens=result.total_tokens)


def pipeline_tools_for_loop(loop) -> list[PipelineRunnerTool]:
    """Build one PipelineRunnerTool per saved pipeline in the loop's store.
    Called by AgentLoop at run start so pipelines saved between turns appear."""
    try:
        pipelines = loop.store.list_pipelines()
    except Exception:
        return []  # store unavailable (e.g. legacy DB before migration) — no tools
    return [PipelineRunnerTool(p, loop) for p in pipelines]
