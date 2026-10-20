"""Tool protocol + registry.

A Tool is a callable with a JSON-schema spec the model can request. The
registry holds tools by name; the RAG-over-tools selector (selector.py) picks
which tool schemas to inject into a given turn's context.
"""
from __future__ import annotations

import abc
import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from proharness.models.base import ToolSpec


@dataclass
class ToolContext:
    """Per-invocation context handed to every tool. Lets tools reach the store,
    the cost tracker, an agent spawner, etc., without globals."""

    store: Any = None  # proharness.memory.store.MemoryStore
    cost: Any = None  # proharness.cost.CostTracker
    spawn_agent: Callable[..., str] | None = None  # delegate a subtask
    workdir: str = "."
    settings: Any = None
    extra: dict = field(default_factory=dict)


@dataclass
class ToolResult:
    """Output of running a tool. `content` is what the model sees as the tool
    result message; `error` short-circuits to a structured error string."""

    content: str = ""
    error: bool = False
    metadata: dict = field(default_factory=dict)

    @classmethod
    def ok(cls, content: str, **metadata) -> ToolResult:
        return cls(content=content, metadata=metadata)

    @classmethod
    def err(cls, message: str) -> ToolResult:
        return cls(content=message, error=True)


class Tool(abc.ABC):
    """Base class. Subclasses set name/description/parameters and implement run()."""

    name: str = ""
    description: str = ""
    parameters: dict[str, Any] = {"type": "object", "properties": {}, "required": []}

    @abc.abstractmethod
    def run(self, args: dict[str, Any], ctx: ToolContext) -> ToolResult: ...

    def spec(self) -> ToolSpec:
        return ToolSpec(name=self.name, description=self.description, parameters=self.parameters)

    def safe_run(self, raw_args: str, ctx: ToolContext) -> ToolResult:
        """Parse JSON args then run, trapping errors so a bad tool call can't
        crash the agent loop."""
        try:
            args = json.loads(raw_args) if raw_args.strip() else {}
        except json.JSONDecodeError as e:
            return ToolResult.err(f"invalid JSON arguments: {e}")
        try:
            return self.run(args, ctx)
        except Exception as e:  # trap for the agent loop
            return ToolResult.err(f"{type(e).__name__}: {e}")


class Registry:
    """Holds tools by name."""

    def __init__(self):
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> Tool:
        if not tool.name:
            raise ValueError("tool must have a name")
        self._tools[tool.name] = tool
        return tool

    def get(self, name: str) -> Tool | None:
        return self._tools.get(name)

    def all(self) -> list[Tool]:
        return list(self._tools.values())

    def specs(self) -> list[ToolSpec]:
        return [t.spec() for t in self._tools.values()]


def estimate_tokens(text: str) -> int:
    """Cheap token estimate (~4 chars/token) for budget decisions — the same
    rule of thumb the context budgeter uses. Exact counting isn't worth an
    encoding dependency at these margins."""
    return max(1, len(text or "") // 4)
