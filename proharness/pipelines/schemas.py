"""Pipeline domain types: Pipeline, Node, Edge, Variable, PortValue.

Serialized as JSON for persistence + the React Flow canvas. Each node has a
``kind`` (determines the executor) and a ``config`` dict (node-specific params);
input/output ports are derived from the kind via the port registry in nodes.py.
"""
from __future__ import annotations

import re
import time
import uuid
from dataclasses import dataclass, field
from typing import Any, Literal

# ── Port types ───────────────────────────────────────────────────────────────
# The set of types values can carry along edges. The interpreter auto-adapts
# between compatible types (e.g. text→json wraps in a string; json→text does
# json.dumps; list→text joins with newlines).
PortType = Literal["text", "json", "list", "memory[]", "file", "any"]


@dataclass
class PortValue:
    """A runtime value flowing along an edge, tagged with its type."""

    type: PortType
    value: Any  # str | list | dict | etc.

    @classmethod
    def text(cls, v: str) -> PortValue:
        return cls(type="text", value=v)

    @classmethod
    def list_of(cls, items: list[str]) -> PortValue:
        return cls(type="list", value=items)

    @classmethod
    def json_val(cls, v: Any) -> PortValue:
        return cls(type="json", value=v)


@dataclass
class Variable:
    """An external input a caller fills in when invoking a pipeline."""

    name: str  # e.g. "topic"
    type: PortType = "text"
    default: str = ""
    description: str = ""

    def to_dict(self) -> dict:
        return {"name": self.name, "type": self.type, "default": self.default, "description": self.description}

    @classmethod
    def from_dict(cls, d: dict) -> Variable:
        return cls(name=d["name"], type=d.get("type", "text"), default=d.get("default", ""), description=d.get("description", ""))


@dataclass
class Node:
    """A single node in the pipeline graph. ``kind`` selects the executor;
    ``config`` holds its parameters; ``position`` is UI metadata for React Flow."""

    id: str
    kind: str  # "text", "variable", "template", "model.invoke", "memory.store", etc.
    config: dict[str, Any] = field(default_factory=dict)
    position: dict[str, float] = field(default_factory=lambda: {"x": 0, "y": 0})

    def to_dict(self) -> dict:
        return {"id": self.id, "kind": self.kind, "config": self.config, "position": self.position}

    @classmethod
    def from_dict(cls, d: dict) -> Node:
        return cls(id=d["id"], kind=d["kind"], config=d.get("config", {}), position=d.get("position", {"x": 0, "y": 0}))


@dataclass
class Edge:
    """A typed connection between two nodes' ports."""

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    source: str = ""  # node id
    source_port: str = "out"  # output port name on source
    target: str = ""  # node id
    target_port: str = "in"  # input port name on target

    def to_dict(self) -> dict:
        return {"id": self.id, "source": self.source, "source_port": self.source_port, "target": self.target, "target_port": self.target_port}

    @classmethod
    def from_dict(cls, d: dict) -> Edge:
        return cls(id=d.get("id", str(uuid.uuid4())), source=d["source"], source_port=d.get("source_port", "out"), target=d["target"], target_port=d.get("target_port", "in"))


def _slugify(name: str) -> str:
    """Make a tool/URL-safe slug from a name."""
    slug = re.sub(r"[^a-zA-Z0-9]+", "_", name.strip().lower()).strip("_")
    return slug or "pipeline"


@dataclass
class Pipeline:
    """A complete pipeline definition. Source-of-truth is SQLite; the React Flow
    canvas serializes/deserializes via to_dict/from_dict."""

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    name: str = ""
    slug: str = ""
    description: str = ""
    inputs: list[Variable] = field(default_factory=list)
    nodes: list[Node] = field(default_factory=list)
    edges: list[Edge] = field(default_factory=list)
    # Optional per-run token budget (2026-08-12 #2). When set, the interpreter
    # stops scheduling new nodes once the run's token spend crosses it, and
    # gracefully degrades model.invoke tiers (cloud→cheap→local) as the budget
    # tightens past 80% consumed. None/0 = unlimited.
    token_budget: int | None = None
    # Optional unattended trigger (2026-08-20 #3), one of:
    #   {"cron": "m h dom mon dow"}          — 5-field cron (minute ticker)
    #   {"interval_minutes": 60}             — every N minutes
    #   {"importance_threshold": 30}         — Park-style: fire when enough
    #                                          important memories accumulated
    #                                          since the last fire (the same
    #                                          signal-first pattern as the
    #                                          reflection trigger)
    trigger: dict | None = None
    version: int = 1
    created_at: float = field(default_factory=time.time)
    updated_at: float = field(default_factory=time.time)

    def __post_init__(self) -> None:
        if not self.slug:
            self.slug = _slugify(self.name) if self.name else _slugify(self.id)

    # ── serialization ─────────────────────────────────────────────────────
    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "slug": self.slug,
            "description": self.description,
            "inputs": [v.to_dict() for v in self.inputs],
            "nodes": [n.to_dict() for n in self.nodes],
            "edges": [e.to_dict() for e in self.edges],
            "token_budget": self.token_budget,
            "trigger": self.trigger,
            "version": self.version,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Pipeline:
        return cls(
            id=d.get("id", str(uuid.uuid4())),
            name=d.get("name", ""),
            slug=d.get("slug", ""),
            description=d.get("description", ""),
            inputs=[Variable.from_dict(v) for v in d.get("inputs", [])],
            nodes=[Node.from_dict(n) for n in d.get("nodes", [])],
            edges=[Edge.from_dict(e) for e in d.get("edges", [])],
            token_budget=d.get("token_budget"),
            trigger=d.get("trigger"),
            version=d.get("version", 1),
            created_at=d.get("created_at", time.time()),
            updated_at=d.get("updated_at", time.time()),
        )

    # ── SQL persistence (mirrors the Memory to_sql_row pattern) ────────────
    def to_sql_row(self) -> tuple:
        import json

        return (
            self.id,
            self.name,
            self.slug,
            self.description,
            json.dumps(self.to_dict()),
            self.version,
            self.created_at,
            self.updated_at,
        )

    @classmethod
    def from_sql_row(cls, row: tuple) -> Pipeline:
        import json

        # row: (id, name, slug, description, definition_json, version, created_at, updated_at)
        definition = json.loads(row[4]) if row[4] else {}
        return cls(
            id=row[0],
            name=row[1],
            slug=row[2],
            description=row[3],
            inputs=[Variable.from_dict(v) for v in definition.get("inputs", [])],
            nodes=[Node.from_dict(n) for n in definition.get("nodes", [])],
            edges=[Edge.from_dict(e) for e in definition.get("edges", [])],
            token_budget=definition.get("token_budget"),
            trigger=definition.get("trigger"),
            version=row[5],
            created_at=row[6],
            updated_at=row[7],
        )


def run_pipeline(
    pipeline: Pipeline,
    ctx: Any,
    inputs: dict[str, Any] | None = None,
    *,
    on_event: Any | None = None,
    cancel_event: Any | None = None,
) -> Any:
    """Convenience: build an interpreter and run, returning RunResult."""
    from proharness.pipelines.interpreter import PipelineInterpreter

    interp = PipelineInterpreter(pipeline, ctx)
    return interp.run(inputs=inputs or {}, on_event=on_event, cancel_event=cancel_event)
