"""Node-based visual pipeline engine.

A *pipeline* is a named DAG of typed nodes (model invocations, memory
operations, scripts, file I/O, control flow) connected by typed edges. The
interpreter topologically sorts, runs ready nodes concurrently, and streams
per-node events for live inspection. Saved pipelines persist in SQLite and
auto-register as individual MCP tools (``mcp__proharness_pipeline_<slug>``).

Design principle: every node is a thin wrapper over an existing internal API
(router.chat, recall, Consolidator, Orchestrator, ShellTool, etc.) — no new
primitives, all composition. A pipeline run context mirrors ``ToolContext``
(store + router + cost + workdir), so nodes reuse the same shared state.
"""
from __future__ import annotations

from proharness.pipelines.interpreter import PipelineInterpreter, RunResult
from proharness.pipelines.schemas import (
    Edge,
    Node,
    Pipeline,
    PortValue,
    Variable,
    run_pipeline,
)

__all__ = [
    "Edge",
    "Node",
    "Pipeline",
    "PipelineInterpreter",
    "PortValue",
    "RunResult",
    "Variable",
    "run_pipeline",
]
