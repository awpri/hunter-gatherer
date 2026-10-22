"""LLM client protocol + shared message/tool types.

All providers (cloud, local) implement `LLMClient` so the rest of the harness
is provider-agnostic. The OpenAI-compatible API surface (chat completions with
tool/function calling) is the lowest common denominator across OpenAI,
Anthropic (via their OpenAI shim or native), and llama.cpp's server, so we
model the protocol on it.
"""
from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol, runtime_checkable


@dataclass
class Message:
    """A single chat message. `content` is text; `tool_calls` is for assistant
    turns that requested tool execution; `tool_call_id` is for tool-result turns."""

    role: Literal["system", "user", "assistant", "tool"]
    content: str = ""
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None  # set when role == "tool"
    name: str | None = None  # tool name, when role == "tool"

    def to_openai(self) -> dict[str, Any]:
        d: dict[str, Any] = {"role": self.role, "content": self.content}
        if self.tool_calls:
            d["tool_calls"] = [tc.to_openai() for tc in self.tool_calls]
        if self.tool_call_id is not None:
            d["tool_call_id"] = self.tool_call_id
        if self.name is not None:
            d["name"] = self.name
        return d


@dataclass
class ToolCall:
    """A tool invocation the model wants us to execute."""

    id: str
    name: str
    arguments: str  # raw JSON string, as returned by the model

    def to_openai(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "type": "function",
            "function": {"name": self.name, "arguments": self.arguments},
        }


@dataclass
class ToolSpec:
    """JSON-schema function spec handed to the model."""

    name: str
    description: str
    parameters: dict[str, Any]  # JSON schema

    def to_openai(self) -> dict[str, Any]:
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }


@dataclass
class Usage:
    in_tokens: int = 0
    out_tokens: int = 0


@dataclass
class ChatResult:
    """Result of a single chat completion."""

    message: Message
    usage: Usage = field(default_factory=Usage)
    finish_reason: str = "stop"


@runtime_checkable
class LLMClient(Protocol):
    """A single-model client."""

    model: str

    def chat(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        temperature: float = 0.2,
        max_tokens: int | None = None,
    ) -> ChatResult:
        ...

    def stream(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        temperature: float = 0.2,
    ) -> Iterator[str]:
        """Yield content tokens. Tool calls are not streamed."""
        ...
