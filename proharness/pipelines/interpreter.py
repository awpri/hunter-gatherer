"""The pipeline interpreter.

Topological-sort + concurrent execution engine. Runs ready nodes in a thread
pool (cap 5, mirroring the orchestrator), passes values along typed edges, and
emits events for live inspection (pipeline_start / node_start / node_end /
pipeline_end / pipeline_error).

Per-node cost accounting uses the CostTracker snapshot/diff pattern from the
agent loop: snapshot total tokens before the node, diff after. Model calls
auto-record to the cost tracker, so only model/compute nodes show non-zero cost.
"""
from __future__ import annotations

import contextlib
import threading
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from typing import Any

from proharness.pipelines.nodes import PipelineContext, get_executor, get_spec
from proharness.pipelines.schemas import Edge, Node, Pipeline, PortType, PortValue


@dataclass
class LoopEvent:
    """An event emitted during a pipeline run (mirrors loop.LoopEvent)."""

    kind: str
    data: dict


@dataclass
class NodeResult:
    """Per-node execution record for the run transcript."""

    node_id: str
    kind: str
    outputs: dict[str, Any] = field(default_factory=dict)
    latency_ms: float = 0.0
    tokens: int = 0
    cost_usd: float = 0.0
    error: str | None = None
    note: str | None = None  # e.g. "tier degraded → cheap (budget 84% consumed)"


@dataclass
class RunResult:
    """The outcome of a pipeline run."""

    outputs: dict[str, Any] = field(default_factory=dict)  # terminal-node outputs
    transcript: list[NodeResult] = field(default_factory=list)
    total_tokens: int = 0
    total_cost_usd: float = 0.0
    stopped_reason: str = "done"  # "done" | "cancelled" | "error"
    error: str | None = None


# ── Type adaptation ──────────────────────────────────────────────────────────
def _adapt(src_type: PortType, src_value: Any, dst_type: PortType) -> Any:
    """Adapt a value from src_type to dst_type. Returns the adapted value.
    For incompatible types, returns the raw value (best effort)."""
    if dst_type == "any" or src_type == dst_type:
        return src_value
    # text → other
    if src_type == "text":
        if dst_type == "json":
            import json
            try:
                return json.loads(src_value)
            except (json.JSONDecodeError, TypeError):
                return src_value
        if dst_type == "list":
            return [line.strip() for line in str(src_value).splitlines() if line.strip()]
    # json → text
    if src_type == "json":
        if dst_type == "text":
            import json
            return json.dumps(src_value, default=str) if not isinstance(src_value, str) else src_value
        if dst_type == "list":
            return src_value if isinstance(src_value, list) else [src_value]
    # list → text
    if src_type == "list":
        if dst_type == "text":
            return "\n".join(str(i) for i in src_value)
        if dst_type == "json":
            return list(src_value)
    # memory[] → text
    if src_type == "memory[]" and dst_type == "text":
        parts = []
        for m in src_value:
            if isinstance(m, dict):
                parts.append(m.get("text", str(m)))
            else:
                parts.append(str(m))
        return "\n\n".join(parts)
    return src_value  # best effort


# ── Tier degradation ladder (2026-08-12 #2) ─────────────────────────────────
_TIER_ORDER = ["cloud", "cheap", "local"]
_TIER_STEP = {"cloud": "cheap", "cheap": "local", "local": "local", "routed": "cheap"}


def _degrade_tier(tier: str, floor: str = "local") -> str:
    """One step down the cloud→cheap→local ladder, clamped to `floor`.
    `floor` is the lowest tier the node may be degraded to (a node with
    tier_floor="cheap" never drops to local, even at 100% budget)."""
    degraded = _TIER_STEP.get(tier, "cheap")
    try:
        if _TIER_ORDER.index(degraded) > _TIER_ORDER.index(floor):
            degraded = floor
    except ValueError:
        pass
    return degraded


class PipelineInterpreter:
    """Runs a Pipeline DAG: topological sort → concurrent node execution →
    value passing → event emission → cost accounting."""

    def __init__(self, pipeline: Pipeline, ctx: PipelineContext):
        self.pipeline = pipeline
        self.ctx = ctx
        self._node_map: dict[str, Node] = {n.id: n for n in pipeline.nodes}
        self._incoming: dict[str, list[Edge]] = defaultdict(list)
        self._outgoing: dict[str, list[Edge]] = defaultdict(list)
        for e in pipeline.edges:
            if e.source in self._node_map and e.target in self._node_map:
                self._incoming[e.target].append(e)
                self._outgoing[e.source].append(e)

    # ── validation (runs before execution) ─────────────────────────────────
    def validate(self) -> list[str]:
        """Return a list of validation errors (empty = valid)."""
        errors: list[str] = []
        # Check all edge endpoints exist.
        for e in self.pipeline.edges:
            if e.source not in self._node_map:
                errors.append(f"edge {e.id}: source node '{e.source}' not found")
            if e.target not in self._node_map:
                errors.append(f"edge {e.id}: target node '{e.target}' not found")
        # Check for cycles (Kahn's algorithm).
        if not errors:
            cycle = self._detect_cycle()
            if cycle:
                errors.append(f"cycle detected: {' → '.join(cycle)}")
        # Check all node kinds are registered.
        for n in self.pipeline.nodes:
            if get_spec(n.kind) is None:
                errors.append(f"node '{n.id}': unknown kind '{n.kind}'")
        return errors

    def _detect_cycle(self) -> list[str] | None:
        in_degree: dict[str, int] = {nid: len(self._incoming[nid]) for nid in self._node_map}
        queue = [nid for nid, d in in_degree.items() if d == 0]
        visited = 0
        while queue:
            nid = queue.pop(0)
            visited += 1
            for e in self._outgoing[nid]:
                in_degree[e.target] -= 1
                if in_degree[e.target] == 0:
                    queue.append(e.target)
        if visited < len(self._node_map):
            # Find a cycle path for a helpful message.
            remaining = [nid for nid, d in in_degree.items() if d > 0]
            return remaining
        return None

    # ── execution ──────────────────────────────────────────────────────────
    def run(
        self,
        inputs: dict[str, Any] | None = None,
        *,
        on_event: Any | None = None,
        cancel_event: threading.Event | None = None,
        checkpoint: Any | None = None,
    ) -> RunResult:
        """Execute the pipeline. Returns a RunResult with outputs + transcript.

        ``checkpoint`` (durable runs) is any object with
        ``completed_nodes() -> dict[node_id -> {port: value}]`` and
        ``save_node(node_id, kind, outputs, latency_ms, tokens, cost_usd)``.
        Restored nodes skip re-execution (their outputs re-enter the graph);
        each completed node is saved as it finishes — so a crashed run
        resumes from its last completed node instead of restarting."""
        inputs = inputs or {}

        def _emit(event_kind: str, **data) -> None:
            if on_event:
                with contextlib.suppress(Exception):
                    on_event(LoopEvent(kind=event_kind, data=data))

        errors = self.validate()
        if errors:
            _emit("pipeline_error", detail="; ".join(errors))
            return RunResult(stopped_reason="error", error="; ".join(errors))

        _emit("pipeline_start", pipeline_id=self.pipeline.id, pipeline_name=self.pipeline.name, inputs=inputs)

        result = RunResult()
        port_values: dict[tuple[str, str], PortValue] = {}  # (node_id, port_name) → value
        completed: set[str] = set()
        transcript: list[NodeResult] = []

        # ── Durable-run restore: replay checkpointed nodes (skip re-execution).
        restored: dict[str, dict] = {}
        if checkpoint is not None:
            with contextlib.suppress(Exception):
                restored = checkpoint.completed_nodes() or {}
        for nid, outputs in restored.items():
            node = self._node_map.get(nid)
            if node is None:
                continue
            spec = get_spec(node.kind)
            for port_name, value in (outputs or {}).items():
                out_def = spec.output_port(port_name) if spec else None
                ptype: PortType = out_def.type if out_def else "any"
                port_values[(nid, port_name)] = PortValue(type=ptype, value=value)
            completed.add(nid)
            transcript.append(NodeResult(node_id=nid, kind=node.kind, outputs=outputs or {},
                                          note="restored from checkpoint"))
            _emit("node_end", node_id=nid, kind=node.kind, note="restored from checkpoint")

        # ── Cost budget + tier degradation (2026-08-12 #2) ──────────────────
        # Snapshot the tracker at run start so the budget measures THIS run's
        # spend only (the tracker is shared process-wide in the unified harness).
        budget = int(self.pipeline.token_budget or 0)
        if not budget and self.ctx.settings is not None:
            budget = int(getattr(self.ctx.settings, "pipeline_default_budget", 0) or 0)
        base_tokens = self.ctx.cost.total_tokens() if self.ctx.cost else 0

        def _consumed() -> int:
            return (self.ctx.cost.total_tokens() if self.ctx.cost else 0) - base_tokens

        # Build a ready-check: all upstream nodes of ALL input edges must be
        # done, AND at least one incoming edge must have carried a value — a
        # node fed only by inactive branch ports (no values) is skipped, so
        # the untaken branch of a conditional never executes.
        def _is_ready(nid: str) -> bool:
            if nid in completed:
                return False
            incoming = self._incoming[nid]
            upstream = {e.source for e in incoming}
            if not upstream.issubset(completed):
                return False
            # A node fed only by inactive branch ports (no values arrived on
            # any input edge) is skipped — the untaken branch never executes.
            if incoming:
                return any((e.source, e.source_port) in port_values for e in incoming)
            return True

        remaining = set(self._node_map.keys())

        with ThreadPoolExecutor(max_workers=5) as pool:
            while remaining:
                if cancel_event and cancel_event.is_set():
                    result.stopped_reason = "cancelled"
                    break

                # Budget gate: stop scheduling new nodes once the run's spend
                # crosses the budget. Nodes already running finish; downstream
                # deps are abandoned. Graceful degradation (below) has already
                # fired at 80%, so crossing the cap means the run is genuinely
                # over budget.
                if budget and _consumed() >= budget:
                    result.stopped_reason = "budget_exceeded"
                    _emit("budget_exceeded", consumed=_consumed(), budget=budget)
                    break

                # Find ready nodes.
                ready = [nid for nid in remaining if _is_ready(nid)]
                if not ready:
                    # Deadlock — remaining nodes have unsatisfiable deps (e.g.
                    # a branch port that was never activated). Mark as done.
                    result.stopped_reason = result.stopped_reason if result.stopped_reason != "error" else "done"
                    break

                # Submit ready nodes concurrently.
                futures = {}
                for nid in ready:
                    node = self._node_map[nid]
                    # Tier degradation past 80% budget: rewrite the effective
                    # tier one step down the ladder (cloud→cheap→local),
                    # honoring the node's tier_floor. Preserves completion at
                    # the budget edge instead of a hard mid-DAG stop.
                    note = None
                    if budget and _consumed() >= 0.8 * budget and node.kind == "model.invoke":
                        degraded = _degrade_tier(
                            str(node.config.get("tier", "routed")),
                            str(node.config.get("tier_floor", "local")),
                        )
                        if degraded != node.config.get("tier", "routed"):
                            import dataclasses

                            node = dataclasses.replace(node, config={**node.config, "tier": degraded})
                            note = f"tier degraded → {degraded} (budget {int(100 * _consumed() / max(budget, 1))}% consumed)"
                    node_inputs = self._collect_inputs(nid, port_values, inputs)
                    futures[pool.submit(self._run_node, node, node_inputs, inputs, _emit, cancel_event, note)] = nid

                for fut in as_completed(futures):
                    nid = futures[fut]
                    node = self._node_map[nid]
                    remaining.discard(nid)
                    try:
                        node_result, outputs = fut.result()
                        completed.add(nid)
                        transcript.append(node_result)
                        result.total_tokens += node_result.tokens
                        result.total_cost_usd += node_result.cost_usd
                        # Store output values.
                        for port_name, pv in outputs.items():
                            port_values[(nid, port_name)] = pv
                        _emit("node_end", node_id=nid, kind=node.kind,
                              outputs={k: v.value for k, v in outputs.items()},
                              latency_ms=node_result.latency_ms,
                              tokens=node_result.tokens, cost_usd=node_result.cost_usd,
                              note=node_result.note)
                        # Durable-run checkpoint: persist the node as it
                        # finishes so a crashed run resumes from here.
                        if checkpoint is not None:
                            with contextlib.suppress(Exception):
                                checkpoint.save_node(
                                    nid, node.kind,
                                    {k: v.value for k, v in outputs.items()},
                                    node_result.latency_ms, node_result.tokens,
                                    node_result.cost_usd, node_result.error,
                                )
                    except Exception as e:
                        completed.add(nid)
                        nr = NodeResult(node_id=nid, kind=node.kind, error=f"{type(e).__name__}: {e}")
                        transcript.append(nr)
                        _emit("node_end", node_id=nid, kind=node.kind, error=nr.error)
                        # Checkpoint the failure too (observability: the row's
                        # error column records it; resume filters on error).
                        if checkpoint is not None:
                            with contextlib.suppress(Exception):
                                checkpoint.save_node(nid, node.kind, {}, 0.0, 0, 0.0,
                                                     f"{type(e).__name__}: {e}")
                        # A failed node's downstream deps may never fire; continue.

        # Collect terminal outputs (nodes with no outgoing edges).
        for nid, node in self._node_map.items():
            if not self._outgoing.get(nid):
                spec = get_spec(node.kind)
                if spec and spec.outputs:
                    port_name = spec.outputs[0].name
                    pv = port_values.get((nid, port_name))
                    if pv:
                        result.outputs[f"{nid}:{port_name}"] = pv.value

        result.transcript = transcript
        if result.stopped_reason == "done" and not result.outputs and not transcript:
            result.stopped_reason = "empty"

        _emit("pipeline_end", outputs=result.outputs,
              total_tokens=result.total_tokens, total_cost_usd=result.total_cost_usd,
              stopped_reason=result.stopped_reason)
        return result

    def _collect_inputs(
        self, nid: str, port_values: dict[tuple[str, str], PortValue], pipeline_inputs: dict[str, Any],
    ) -> dict[str, PortValue]:
        """Gather input values for a node from port_values, adapting types."""
        node = self._node_map[nid]
        spec = get_spec(node.kind)
        collected: dict[str, PortValue] = {}
        for edge in self._incoming[nid]:
            src_key = (edge.source, edge.source_port)
            if src_key not in port_values:
                continue  # optional input not present (e.g. branch inactive)
            pv = port_values[src_key]
            # Adapt type if the target port declares a type.
            if spec:
                dst_port = spec.input_port(edge.target_port)
                dst_type: PortType = dst_port.type if dst_port else "any"
                adapted = _adapt(pv.type, pv.value, dst_type)
                collected[edge.target_port] = PortValue(type=dst_type, value=adapted)
            else:
                collected[edge.target_port] = pv
        # Variable nodes also pull from pipeline inputs.
        if node.kind == "variable":
            var_name = node.config.get("var", "")
            raw = pipeline_inputs.get(var_name, "")
            if isinstance(raw, list):
                collected["out"] = PortValue(type="list", value=raw)
            else:
                collected["out"] = PortValue.text(str(raw))
        return collected

    def _run_node(
        self, node: Node, inputs: dict[str, PortValue], pipeline_vars: dict[str, Any],
        _emit: Any, cancel_event: threading.Event | None, note: str | None = None,
    ) -> tuple[NodeResult, dict[str, PortValue]]:
        """Execute a single node. Returns (NodeResult, output port values)."""
        executor = get_executor(node.kind)
        if executor is None:
            raise ValueError(f"unknown node kind: {node.kind}")

        _emit("node_start", node_id=node.id, kind=node.kind, tier=node.config.get("tier") if node.kind == "model.invoke" else None)

        # Cost snapshot.
        cost_base_tokens = self.ctx.cost.total_tokens() if self.ctx.cost else 0
        cost_base_usd = self.ctx.cost.total_cost_usd() if self.ctx.cost else 0.0

        t0 = time.monotonic()
        if cancel_event and cancel_event.is_set():
            raise RuntimeError("cancelled before start")
        outputs = executor.run(self.ctx, node, inputs, pipeline_vars)
        latency_ms = (time.monotonic() - t0) * 1000

        cost_after_tokens = self.ctx.cost.total_tokens() if self.ctx.cost else 0
        cost_after_usd = self.ctx.cost.total_cost_usd() if self.ctx.cost else 0.0

        nr = NodeResult(
            node_id=node.id, kind=node.kind,
            outputs={k: (v.value if isinstance(v, PortValue) else v) for k, v in outputs.items()},
            latency_ms=round(latency_ms, 1),
            tokens=cost_after_tokens - cost_base_tokens,
            cost_usd=round(cost_after_usd - cost_base_usd, 6),
            note=note,
        )
        return nr, outputs
