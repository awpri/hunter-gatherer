"""Node executor registry.

Each node ``kind`` maps to a ``NodeExecutor`` that wraps an existing internal
API (router.chat, recall, Consolidator, Orchestrator, ShellTool, etc.). Nodes
are thin — no logic is duplicated from the rest of the codebase.

Port definitions per kind are declared class-side via ``NodeSpec`` so the
interpreter (and the React Flow canvas) can validate type compatibility and
render the right handles.
"""
from __future__ import annotations

import json
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from proharness.pipelines.schemas import Node, PortType, PortValue


@dataclass
class PortDef:
    """A port's name + type + whether it's required."""

    name: str
    type: PortType = "text"
    required: bool = False
    description: str = ""


@dataclass
class NodeSpec:
    """Declarative description of a node kind — its ports, config, category."""

    kind: str
    category: str  # "source" | "model" | "compute" | "io" | "memory" | "control"
    label: str
    inputs: list[PortDef] = field(default_factory=list)
    outputs: list[PortDef] = field(default_factory=list)
    config_fields: dict[str, dict] = field(default_factory=dict)  # name → {type, default, description}
    description: str = ""

    def input_port(self, name: str) -> PortDef | None:
        return next((p for p in self.inputs if p.name == name), None)

    def output_port(self, name: str) -> PortDef | None:
        return next((p for p in self.outputs if p.name == name), None)


class NodeExecutor(ABC):
    """Base class. Subclasses set ``spec`` (a NodeSpec) and implement run()."""

    spec: NodeSpec

    @abstractmethod
    def run(
        self,
        ctx: PipelineContext,
        node: Node,
        inputs: dict[str, PortValue],
        pipeline_vars: dict[str, Any],
    ) -> dict[str, PortValue]:
        """Execute the node. ``inputs`` maps port names to received values.
        Returns a dict mapping output port names to PortValues."""
        ...


# ── Pipeline run context ─────────────────────────────────────────────────────
@dataclass
class PipelineContext:
    """The shared state a pipeline run operates on. Built from an AgentLoop or
    constructed directly. Mirrors the pieces of ToolContext that nodes need,
    plus the router and orchestrator (which ToolContext doesn't expose)."""

    store: Any = None
    router: Any = None
    cost: Any = None
    orchestrator: Any = None
    settings: Any = None
    workdir: str = "."

    @classmethod
    def from_loop(cls, loop: Any) -> PipelineContext:
        return cls(
            store=loop.store,
            router=loop.router,
            cost=loop.cost,
            orchestrator=loop.orchestrator,
            settings=loop.settings,
            workdir=getattr(loop, "workdir", "."),
        )


# ── Registry ─────────────────────────────────────────────────────────────────
_SPECS: dict[str, NodeSpec] = {}
_EXECUTORS: dict[str, NodeExecutor] = {}


def register(spec: NodeSpec):
    """Decorator: register a NodeExecutor subclass under its spec."""

    def decorator(cls: type[NodeExecutor]) -> type[NodeExecutor]:
        cls.spec = spec
        _SPECS[spec.kind] = spec
        _EXECUTORS[spec.kind] = cls()
        return cls

    return decorator


def get_spec(kind: str) -> NodeSpec | None:
    return _SPECS.get(kind)


def get_executor(kind: str) -> NodeExecutor | None:
    return _EXECUTORS.get(kind)


def all_specs() -> list[NodeSpec]:
    return list(_SPECS.values())


def specs_by_category() -> dict[str, list[NodeSpec]]:
    out: dict[str, list[NodeSpec]] = {}
    for s in _SPECS.values():
        out.setdefault(s.category, []).append(s)
    return out


# ── Helper: template substitution ────────────────────────────────────────────
_VAR_RE = re.compile(r"\{\{\s*(\w+)\s*\}\}")


def render_template(template: str, variables: dict[str, str]) -> str:
    """Substitute ``{{var}}`` placeholders. Missing vars become empty strings
    (no KeyError — pipelines should be forgiving)."""

    def repl(m: re.Match) -> str:
        return str(variables.get(m.group(1), ""))

    return _VAR_RE.sub(repl, template)


# ═══════════════════════════════════════════════════════════════════════════════
# SOURCE NODES
# ═══════════════════════════════════════════════════════════════════════════════
@register(NodeSpec(
    kind="text", category="source", label="Text",
    outputs=[PortDef("out", "text")],
    config_fields={"value": {"type": "string", "default": "", "description": "Fixed text to output."}},
    description="A fixed text string.",
))
class TextNode(NodeExecutor):
    def run(self, ctx, node, inputs, pipeline_vars):
        return {"out": PortValue.text(node.config.get("value", ""))}


@register(NodeSpec(
    kind="variable", category="source", label="Variable",
    outputs=[PortDef("out", "any")],
    config_fields={"var": {"type": "string", "default": "", "description": "Pipeline input variable name."}},
    description="Reads an external input variable passed by the caller.",
))
class VariableNode(NodeExecutor):
    def run(self, ctx, node, inputs, pipeline_vars):
        var_name = node.config.get("var", "")
        raw = pipeline_vars.get(var_name, "")
        # Infer type: lists stay lists, everything else is text.
        if isinstance(raw, list):
            return {"out": PortValue(type="list", value=raw)}
        return {"out": PortValue.text(str(raw))}


@register(NodeSpec(
    kind="template", category="source", label="Template",
    inputs=[PortDef("in", "text", required=False, description="Primary input, referenced as {{in}}.")],
    outputs=[PortDef("out", "text")],
    config_fields={"template": {"type": "string", "default": "", "description": "Text with {{var}} placeholders. Use {{in}} for the incoming value and {{name}} for pipeline variables."}},
    description="Render a text template with {{placeholders}} filled from inputs and pipeline variables.",
))
class TemplateNode(NodeExecutor):
    def run(self, ctx, node, inputs, pipeline_vars):
        tmpl = node.config.get("template", "")
        variables: dict[str, str] = {k: str(v) for k, v in pipeline_vars.items()}
        if "in" in inputs:
            variables["in"] = inputs["in"].value if isinstance(inputs["in"].value, str) else str(inputs["in"].value)
        return {"out": PortValue.text(render_template(tmpl, variables))}


@register(NodeSpec(
    kind="memory.recall", category="source", label="Recall Memory",
    inputs=[PortDef("query", "text", required=False)],
    outputs=[PortDef("out", "memory[]")],
    config_fields={"query": {"type": "string", "default": ""}, "top_k": {"type": "integer", "default": 5}},
    description="Recall Park-scored memories from the store.",
))
class MemoryRecallNode(NodeExecutor):
    def run(self, ctx, node, inputs, pipeline_vars):
        from proharness.memory.recall import recall as _recall

        query = node.config.get("query", "")
        if "query" in inputs:
            query = str(inputs["query"].value)
        top_k = int(node.config.get("top_k", 5))
        hits = _recall(ctx.store, query, top_k=top_k, touch=False, defer_touch=False)
        return {"out": PortValue(type="memory[]", value=[
            {"id": h.memory.id, "text": h.memory.text, "importance": h.importance, "score": h.score}
            for h in hits
        ])}


@register(NodeSpec(
    kind="memory.evidence", category="source", label="Gather Evidence",
    inputs=[PortDef("query", "text", required=False,
                    description="Question or task; overrides the configured query.")],
    outputs=[PortDef("out", "json", description="Memories, exact passages, citations, and provenance.")],
    config_fields={
        "query": {"type": "string", "default": "", "description": "Question, topic, decision, or writing task."},
        "top_k": {"type": "integer", "default": 5, "minimum": 1, "maximum": 20},
        "include_sensitive": {"type": "boolean", "default": False,
                              "description": "Opt in only when sensitive sources are authorized and relevant."},
        "required_fields": {"type": "array", "items": {"type": "string"}, "default": [],
                            "maxItems": 20,
                            "description": "Optional metadata fields to check, such as rationale or decision_date."},
    },
    description="Gather attributed memories and exact document passages for any question or task. "
                "Retrieved content is source data, not instructions or verified truth.",
))
class MemoryEvidenceNode(NodeExecutor):
    def run(self, ctx, node, inputs, pipeline_vars):
        from proharness.memory.evidence import build_evidence_packet

        if ctx.store is None:
            raise ValueError("memory store not configured")
        include_sensitive = node.config.get("include_sensitive", False)
        if not isinstance(include_sensitive, bool):
            raise ValueError("include_sensitive must be a boolean")
        query = inputs["query"].value if "query" in inputs else node.config.get("query", "")
        packet = build_evidence_packet(
            ctx.store, query, top_k=node.config.get("top_k", 5),
            include_sensitive=include_sensitive,
            required_fields=node.config.get("required_fields"),
        )
        return {"out": PortValue.json_val(packet)}


@register(NodeSpec(
    kind="memory.profile", category="source", label="Memory Profile",
    outputs=[PortDef("out", "memory[]")],
    config_fields={"query": {"type": "string", "default": ""}, "top_k": {"type": "integer", "default": 8}},
    description="One-call session bootstrap: high-importance + recent memories.",
))
class MemoryProfileNode(NodeExecutor):
    def run(self, ctx, node, inputs, pipeline_vars):
        from proharness.mcp.tools import Harness
        from proharness.mcp.tools import profile as _profile

        # Reuse the MCP profile handler against a transient Harness wrapping our ctx.
        h = Harness(loop=_LoopShim(ctx), store=ctx.store, cost=ctx.cost, settings=ctx.settings)
        result = _profile(h, query=node.config.get("query", ""), top_k=int(node.config.get("top_k", 8)))
        return {"out": PortValue(type="memory[]", value=result.get("profile", []))}


@register(NodeSpec(
    kind="web.fetch", category="source", label="Web Fetch",
    inputs=[PortDef("url", "text", required=False)],
    outputs=[PortDef("out", "text")],
    config_fields={"url": {"type": "string", "default": ""}, "max_chars": {"type": "integer", "default": 8000}},
    description="Fetch a URL and return its text content.",
))
class WebFetchNode(NodeExecutor):
    def run(self, ctx, node, inputs, pipeline_vars):
        from proharness.tools.builtin import WebFetchTool

        url = node.config.get("url", "")
        if "url" in inputs:
            url = str(inputs["url"].value)
        tool = WebFetchTool()
        result = tool.run({"url": url, "max_chars": int(node.config.get("max_chars", 8000))},
                          _tool_ctx(ctx))
        return {"out": PortValue.text(result.content if not result.error else f"error: {result.content}")}


@register(NodeSpec(
    kind="file.read", category="source", label="Read File",
    inputs=[PortDef("path", "text", required=False)],
    outputs=[PortDef("out", "text")],
    config_fields={"path": {"type": "string", "default": ""}, "max_lines": {"type": "integer", "default": 2000}},
    description="Read a file from the workdir.",
))
class FileReadNode(NodeExecutor):
    def run(self, ctx, node, inputs, pipeline_vars):
        from proharness.tools.builtin import ReadFileTool

        path = node.config.get("path", "")
        if "path" in inputs:
            path = str(inputs["path"].value)
        tool = ReadFileTool()
        result = tool.run({"path": path, "max_lines": int(node.config.get("max_lines", 2000))}, _tool_ctx(ctx))
        return {"out": PortValue.text(result.content if not result.error else f"error: {result.content}")}


# ═══════════════════════════════════════════════════════════════════════════════
# MODEL NODES
# ═══════════════════════════════════════════════════════════════════════════════
@register(NodeSpec(
    kind="model.invoke", category="model", label="Model",
    inputs=[PortDef("prompt", "text", required=True)],
    outputs=[PortDef("out", "text")],
    config_fields={
        "tier": {"type": "string", "default": "routed", "description": "local | cheap | cloud | routed"},
        "tier_floor": {"type": "string", "default": "local", "description": "Lowest tier budget-degradation may drop to."},
        "system": {"type": "string", "default": "", "description": "System prompt."},
        "temperature": {"type": "number", "default": 0.2},
        "max_tokens": {"type": "integer", "default": 4096},
    },
    description="Invoke a model (routed by default, or pin to a tier).",
))
class ModelInvokeNode(NodeExecutor):
    def run(self, ctx, node, inputs, pipeline_vars):
        from proharness.models.base import Message

        prompt = str(inputs.get("prompt", PortValue.text("")).value)
        system = node.config.get("system", "")
        tier = node.config.get("tier", "routed")
        temperature = float(node.config.get("temperature", 0.2))
        max_tokens = int(node.config.get("max_tokens", 4096))

        messages: list[Message] = []
        if system:
            messages.append(Message(role="system", content=system))
        messages.append(Message(role="user", content=prompt))

        if tier == "routed" or not ctx.router:
            client = ctx.router
        else:
            from proharness.models.router import Route
            route = Route(tier=tier, model=tier, reason="pinned")  # type: ignore[arg-type]
            select = getattr(ctx.router, "select", None)
            client = select(route) if select is not None else ctx.router

        result = client.chat(messages, temperature=temperature, max_tokens=max_tokens)
        return {"out": PortValue.text(result.message.content)}


# ═══════════════════════════════════════════════════════════════════════════════
# COMPUTE NODES
# ═══════════════════════════════════════════════════════════════════════════════
@register(NodeSpec(
    kind="script.run", category="compute", label="Script",
    inputs=[PortDef("stdin", "text", required=False, description="Text piped to stdin.")],
    outputs=[PortDef("out", "text")],
    config_fields={"command": {"type": "string", "default": "", "description": "Shell command to execute."}, "timeout": {"type": "number", "default": 60}},
    description="Run a shell command; stdout becomes the output.",
))
class ScriptRunNode(NodeExecutor):
    def run(self, ctx, node, inputs, pipeline_vars):
        from proharness.tools.builtin import ShellTool

        command = render_template(node.config.get("command", ""), {k: str(v) for k, v in pipeline_vars.items()})
        str(inputs.get("stdin", PortValue.text("")).value) if "stdin" in inputs else ""
        tool = ShellTool()
        result = tool.run({"command": command, "timeout": float(node.config.get("timeout", 60))}, _tool_ctx(ctx))
        return {"out": PortValue.text(result.content if not result.error else f"error: {result.content}")}


@register(NodeSpec(
    kind="transform", category="compute", label="Transform",
    inputs=[PortDef("in", "text", required=True)],
    outputs=[PortDef("out", "json")],
    config_fields={
        "mode": {"type": "string", "default": "json_extract", "description": "json_extract | split_lines | head"},
        "expression": {"type": "string", "default": "", "description": "JSON path (e.g. 'tasks') or split delimiter."},
    },
    description="Transform text: extract JSON, split lines, or take the head.",
))
class TransformNode(NodeExecutor):
    def run(self, ctx, node, inputs, pipeline_vars):
        text = str(inputs.get("in", PortValue.text("")).value)
        mode = node.config.get("mode", "json_extract")
        expr = node.config.get("expression", "")
        if mode == "json_extract":
            # Try to parse the text as JSON and extract the keyed field.
            try:
                data = json.loads(text)
                if isinstance(data, dict) and expr:
                    return {"out": PortValue(type="json", value=data.get(expr, data))}
                return {"out": PortValue(type="json", value=data)}
            except json.JSONDecodeError:
                # Fallback: line-by-line extraction (e.g. "- task one\n- task two").
                items = [line.lstrip("- ").strip() for line in text.strip().splitlines() if line.strip()]
                return {"out": PortValue(type="list", value=items)}
        elif mode == "split_lines":
            items = [line.strip() for line in text.strip().splitlines() if line.strip()]
            return {"out": PortValue(type="list", value=items)}
        elif mode == "head":
            return {"out": PortValue.text(text[: int(expr or 500)])}
        return {"out": PortValue.text(text)}


@register(NodeSpec(
    kind="task.decompose", category="compute", label="Decompose",
    inputs=[PortDef("text", "text", required=True)],
    outputs=[PortDef("tasks", "list")],
    config_fields={"count": {"type": "integer", "default": 5, "description": "Max subtasks to produce."}},
    description="Break text into a list of concise subtasks via the model.",
))
class TaskDecomposeNode(NodeExecutor):
    def run(self, ctx, node, inputs, pipeline_vars):
        from proharness.models.base import Message

        text = str(inputs.get("text", PortValue.text("")).value)
        count = int(node.config.get("count", 5))
        system = (
            f"Break the following into at most {count} concise, actionable subtasks. "
            "Respond with ONLY a JSON array of strings, no prose. "
            'Example: ["research X", "summarize Y", "store Z"]'
        )
        result = ctx.router.chat(
            [Message(role="system", content=system), Message(role="user", content=text)],
            temperature=0.1, max_tokens=1024,
        )
        raw = result.message.content.strip()
        # Try JSON parse; fall back to line splitting.
        try:
            items = json.loads(raw)
            if isinstance(items, list):
                return {"tasks": PortValue(type="list", value=[str(i) for i in items])}
        except json.JSONDecodeError:
            pass
        items = [line.lstrip("- ").strip().rstrip(",") for line in raw.splitlines() if line.strip()]
        return {"tasks": PortValue(type="list", value=items)}


# ═══════════════════════════════════════════════════════════════════════════════
# I/O NODES
# ═══════════════════════════════════════════════════════════════════════════════
@register(NodeSpec(
    kind="file.write", category="io", label="Write File",
    inputs=[PortDef("content", "text", required=True)],
    outputs=[PortDef("path", "file")],
    config_fields={"path": {"type": "string", "default": "output.txt"}, "append": {"type": "boolean", "default": False}},
    description="Write text to a file in the workdir.",
))
class FileWriteNode(NodeExecutor):
    def run(self, ctx, node, inputs, pipeline_vars):
        from proharness.tools.builtin import WriteFileTool

        content = str(inputs.get("content", PortValue.text("")).value)
        path = node.config.get("path", "output.txt")
        tool = WriteFileTool()
        result = tool.run({"path": path, "content": content}, _tool_ctx(ctx))
        return {"path": PortValue(type="file", value=path if not result.error else f"error: {result.content}")}


# ═══════════════════════════════════════════════════════════════════════════════
# MEMORY NODES
# ═══════════════════════════════════════════════════════════════════════════════
@register(NodeSpec(
    kind="memory.store", category="memory", label="Store Memory",
    inputs=[PortDef("text", "text", required=True)],
    outputs=[PortDef("id", "text")],
    config_fields={
        "importance": {"type": "number", "default": 5.0},
        "type": {"type": "string", "default": "semantic", "description": "semantic | episodic | procedural"},
        "source": {"type": "string", "default": "pipeline"},
    },
    description="Store a memory with Mem0 ADD/UPDATE/DELETE reconciliation.",
))
class MemoryStoreNode(NodeExecutor):
    def run(self, ctx, node, inputs, pipeline_vars):
        from proharness.memory.consolidate import Consolidator
        from proharness.memory.schemas import Memory

        text = str(inputs.get("text", PortValue.text("")).value)
        mem = Memory(
            text=text,
            type=node.config.get("type", "semantic"),
            importance=float(node.config.get("importance", 5.0)),
            source=node.config.get("source", "pipeline"),
        )
        cons = Consolidator(store=ctx.store, settings=ctx.settings)
        stored = cons.store_with_consolidation(mem)
        return {"id": PortValue.text(stored.id if stored else "")}


@register(NodeSpec(
    kind="memory.search", category="memory", label="Search Memory",
    inputs=[PortDef("query", "text", required=False)],
    outputs=[PortDef("out", "memory[]")],
    config_fields={"query": {"type": "string", "default": ""}, "top_k": {"type": "integer", "default": 8}},
    description="Raw hybrid vector search (dense + sparse, RRF-fused).",
))
class MemorySearchNode(NodeExecutor):
    def run(self, ctx, node, inputs, pipeline_vars):
        query = node.config.get("query", "")
        if "query" in inputs:
            query = str(inputs["query"].value)
        top_k = int(node.config.get("top_k", 8))
        hits = ctx.store.vector_search(query, top_k=top_k)
        return {"out": PortValue(type="memory[]", value=[
            {"id": h.id, "text": h.payload.get("preview", ""), "score": h.score}
            for h in hits
        ])}


# ═══════════════════════════════════════════════════════════════════════════════
# CONTROL NODES
# ═══════════════════════════════════════════════════════════════════════════════
@register(NodeSpec(
    kind="branch", category="control", label="Branch",
    inputs=[PortDef("in", "text", required=True)],
    outputs=[PortDef("true", "text"), PortDef("false", "text")],
    config_fields={
        "mode": {"type": "string", "default": "contains", "description": "contains | equals | not_empty | regex"},
        "value": {"type": "string", "default": ""},
    },
    description="Conditional: routes input to the 'true' or 'false' output port.",
))
class BranchNode(NodeExecutor):
    def run(self, ctx, node, inputs, pipeline_vars):
        text = str(inputs.get("in", PortValue.text("")).value)
        mode = node.config.get("mode", "contains")
        value = node.config.get("value", "")
        if mode == "contains":
            taken = value in text
        elif mode == "equals":
            taken = text.strip() == value.strip()
        elif mode == "not_empty":
            taken = bool(text.strip())
        elif mode == "regex":
            taken = bool(re.search(value, text))
        else:
            taken = False
        active_port = "true" if taken else "false"
        return {active_port: PortValue.text(text)}


@register(NodeSpec(
    kind="merge", category="control", label="Merge",
    inputs=[PortDef("a", "text", required=False), PortDef("b", "text", required=False)],
    outputs=[PortDef("out", "text")],
    config_fields={"separator": {"type": "string", "default": "\n\n"}},
    description="Fan-in: concatenate two optional inputs into one output.",
))
class MergeNode(NodeExecutor):
    def run(self, ctx, node, inputs, pipeline_vars):
        sep = node.config.get("separator", "\n\n")
        parts = []
        for port in ("a", "b"):
            if port in inputs and inputs[port].value:
                parts.append(str(inputs[port].value))
        return {"out": PortValue.text(sep.join(parts))}


@register(NodeSpec(
    kind="map", category="control", label="Map",
    inputs=[PortDef("items", "list", required=True)],
    outputs=[PortDef("out", "list")],
    config_fields={
        "body_kind": {"type": "string", "default": "memory.store", "description": "Node kind to run per item."},
        "body_config": {"type": "object", "default": {}, "description": "Config for the body node."},
        "input_port": {"type": "string", "default": "text", "description": "Body input port to feed each item into."},
    },
    description="Run a body node per list item; collect results as a list.",
))
class MapNode(NodeExecutor):
    def run(self, ctx, node, inputs, pipeline_vars):
        items_pv = inputs.get("items")
        if items_pv is None or not isinstance(items_pv.value, list):
            return {"out": PortValue(type="list", value=[])}
        body_kind = node.config.get("body_kind", "memory.store")
        body_config = node.config.get("body_config", {})
        input_port = node.config.get("input_port", "text")

        executor = get_executor(body_kind)
        if executor is None:
            return {"out": PortValue(type="list", value=[])}

        # Build a synthetic body node.
        body_node = Node(id="_map_body", kind=body_kind, config=body_config)
        results = []
        for item in items_pv.value:
            body_inputs = {input_port: PortValue.text(str(item))}
            out = executor.run(ctx, body_node, body_inputs, pipeline_vars)
            # Take the first output port's value.
            if out:
                first_val = next(iter(out.values()))
                results.append(first_val.value if isinstance(first_val, PortValue) else first_val)
        return {"out": PortValue(type="list", value=results)}


# ── Helpers ──────────────────────────────────────────────────────────────────
def _tool_ctx(ctx: PipelineContext):
    """Build a ToolContext from a PipelineContext (for builtin tools)."""
    from proharness.tools.base import ToolContext

    return ToolContext(
        store=ctx.store, cost=ctx.cost, settings=ctx.settings, workdir=ctx.workdir,
    )


class _LoopShim:
    """Minimal shim so Harness(loop=...) works for the profile node reuse.
    Only .store/.cost/.settings are accessed by the profile handler."""

    def __init__(self, ctx: PipelineContext):
        self.store = ctx.store
        self.cost = ctx.cost
        self.settings = ctx.settings
        self.router = ctx.router
        self.orchestrator = ctx.orchestrator
