"""Procedural→pipeline promotion (2026-08-13 #2).

Graduates a *mined* procedural memory (an emergent tool sequence the agent
rediscovered across successful runs — ``memory/procedural.py``) into an
*authored* pipeline: a linear ``Pipeline`` whose nodes are the recorded tool
sequence, with inputs derived from the objective. Once saved, the pipeline
auto-registers as an MCP tool (``proharness_pipeline_<slug>``) and becomes
agent-callable via the selector bridge (2026-08-12 #1) — the harness authors
its own reusable, editable, cost-budgeted procedures from observed success.

The promotion signal is *observed recurrence* (occurrences crossing a
threshold), not the model asserting a procedure is useful — the external-
signals-over-introspection rule.
"""
from __future__ import annotations

from proharness.pipelines.schemas import Edge, Node, Pipeline, Variable

# mined tool name → pipeline node kind
_TOOL_TO_KIND = {
    "shell": "script.run",
    "read_file": "file.read",
    "write_file": "file.write",
    "web_fetch": "web.fetch",
    "memory_recall": "memory.recall",
    "memory_store": "memory.store",
    "spawn_agent": "task.decompose",
}

# first input port per synthesized node kind (chaining rule)
_KIND_FIRST_INPUT = {
    "script.run": "stdin",
    "file.read": "path",
    "file.write": "content",
    "web.fetch": "url",
    "memory.recall": "query",
    "memory.store": "text",
    "task.decompose": "text",
    "model.invoke": "prompt",
}


def pipeline_from_procedural(memory) -> Pipeline:
    """Synthesize a linear Pipeline from a procedural memory.

    ``memory.metadata`` carries ``objective`` + ``tools`` (the recorded
    sequence). Tools with no node analogue are skipped; the rest become a
    chained node sequence fed by an ``objective`` input variable."""
    from proharness.pipelines.nodes import get_spec

    objective = memory.metadata.get("objective", memory.text[:80])
    tools: list[str] = list(memory.metadata.get("tools", []))
    occurrences = int(memory.metadata.get("occurrences", 1))

    nodes: list[Node] = [
        Node(id="n0", kind="variable", config={"var": "objective"}, position={"x": 0, "y": 0}),
    ]
    edges: list[Edge] = []
    prev_id, prev_port = "n0", "out"
    x = 220
    seq_i = 0
    for tool in tools:
        kind = _TOOL_TO_KIND.get(tool)
        if kind is None:
            continue  # no node analogue (e.g. an exotic future tool) — skip
        nid = f"n{len(nodes)}"
        nodes.append(Node(id=nid, kind=kind, config={}, position={"x": x, "y": 0}))
        spec = get_spec(kind)
        first_in = _KIND_FIRST_INPUT.get(kind) or (
            spec.inputs[0].name if spec and spec.inputs else "in"
        )
        edges.append(Edge(source=prev_id, target=nid, source_port=prev_port, target_port=first_in))
        # Chain on the node's first output port.
        prev_id = nid
        prev_port = spec.outputs[0].name if spec and spec.outputs else "out"
        x += 220
        seq_i += 1

    name = _derive_name(objective)
    return Pipeline(
        name=name,
        description=(
            f"Promoted from procedural memory (recurred {occurrences}×): "
            f"for objectives like {objective[:120]!r}, run "
            f"{' → '.join(t for t in tools if t in _TOOL_TO_KIND) or 'the recorded sequence'}."
        ),
        inputs=[Variable(name="objective", type="text", description="The task objective to run the promoted sequence on.")],
        nodes=nodes,
        edges=edges,
    )


def _derive_name(objective: str) -> str:
    """A short human pipeline name from the objective."""
    words = [w for w in objective.strip().split() if w][:4]
    return " ".join(words).title() or "Promoted Procedure"


def promote_if_due(memory, store, *, threshold: int = 5) -> str | None:
    """Promote a procedural memory into a saved Pipeline when its recurrence
    has crossed ``threshold`` and it hasn't already been promoted. Returns the
    new pipeline slug, or None. Idempotent via metadata.promoted_pipeline."""
    occurrences = int(memory.metadata.get("occurrences", 1))
    if occurrences < threshold or memory.metadata.get("promoted_pipeline"):
        return None
    pipeline = pipeline_from_procedural(memory)
    # Avoid slug collisions on repeat mining: suffix the occurrences count.
    if store.get_pipeline_by_slug(pipeline.slug):
        pipeline.slug = f"{pipeline.slug}_{occurrences}"
    store.save_pipeline(pipeline)
    # Mark the memory so we never double-promote.
    meta = dict(memory.metadata)
    meta["promoted_pipeline"] = pipeline.slug
    memory.metadata = meta
    store.update(memory)
    return pipeline.slug
