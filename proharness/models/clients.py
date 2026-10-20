"""Concrete LLM clients.

`OpenAICompatibleClient` is the workhorse: it talks to any OpenAI-compatible
endpoint (api.openai.com, llama.cpp's /v1, vLLM, Ollama's /v1, etc.). This
covers local + OpenAI cloud in one class.

`AnthropicClient` is a thin native client because Anthropic's OpenAI shim is
less featureful for tool calling.

Each client records usage on the provided CostTracker so token/cost accounting
is automatic.
"""
from __future__ import annotations

import json
from collections.abc import Iterator
from typing import Any

import httpx

from proharness.cost import CostTracker
from proharness.models.base import ChatResult, Message, ToolCall, ToolSpec, Usage


class OpenAICompatibleClient:
    """Talks to any OpenAI-compatible /v1 endpoint: OpenAI cloud, llama.cpp,
    vLLM, Ollama, LM Studio, etc.

    `model` is the model id to send. `cost` is optional accounting.
    """

    def __init__(
        self,
        base_url: str,
        api_key: str,
        model: str,
        *,
        cost: CostTracker | None = None,
        timeout: float = 120.0,
    ):
        self.base_url = base_url.rstrip("/")
        self.api_key = api_key or "sk-no-key-required"  # local servers often ignore this
        self.model = model
        self.cost = cost
        self._http = httpx.Client(timeout=timeout)
        self._retry_max = 3  # bounded retry on 429/5xx/timeout (2026-09-07 #2)

    def _post_with_retry(self, url: str, *, json_payload: dict, headers: dict):
        """Bounded retry with exponential backoff + jitter on 429/5xx/timeout,
        honoring Retry-After (2026-09-07 #2). Raises the last error on
        exhaustion so the router's failure ladder can degrade tiers."""
        import random
        import time as _time

        last_exc: Exception | None = None
        for attempt in range(self._retry_max):
            try:
                resp = self._http.post(url, json=json_payload, headers=headers)
                if resp.status_code in (429, 500, 502, 503, 504):
                    retry_after = resp.headers.get("Retry-After")
                    delay = float(retry_after) if retry_after and retry_after.isdigit() \
                        else (0.5 * (2 ** attempt) + random.uniform(0, 0.25))
                    last_exc = RuntimeError(f"HTTP {resp.status_code}")
                    if attempt + 1 < self._retry_max:
                        _time.sleep(min(delay, 8.0))
                        continue
                resp.raise_for_status()
                return resp
            except (httpx.TimeoutException, httpx.TransportError) as e:
                last_exc = e
                if attempt + 1 < self._retry_max:
                    _time.sleep(0.5 * (2 ** attempt) + random.uniform(0, 0.25))
                    continue
                raise
        raise last_exc or RuntimeError("retry exhausted")

    def chat(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        temperature: float = 0.2,
        max_tokens: int | None = None,
    ) -> ChatResult:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [m.to_openai() for m in messages],
            "temperature": temperature,
        }
        if tools:
            payload["tools"] = [t.to_openai() for t in tools]
        if max_tokens is not None:
            payload["max_tokens"] = max_tokens

        resp = self._post_with_retry(
            f"{self.base_url}/chat/completions",
            json_payload=payload,
            headers={"Authorization": f"Bearer {self.api_key}"},
        )
        data = resp.json()
        return self._parse(data)

    def stream(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        temperature: float = 0.2,
    ) -> Iterator[str]:
        payload: dict[str, Any] = {
            "model": self.model,
            "messages": [m.to_openai() for m in messages],
            "temperature": temperature,
            "stream": True,
        }
        if tools:
            payload["tools"] = [t.to_openai() for t in tools]
        with self._http.stream(
            "POST",
            f"{self.base_url}/chat/completions",
            json=payload,
            headers={"Authorization": f"Bearer {self.api_key}"},
        ) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                if not line or not line.startswith("data: "):
                    continue
                chunk = line[len("data: "):]
                if chunk.strip() == "[DONE]":
                    break
                try:
                    obj = json.loads(chunk)
                except json.JSONDecodeError:
                    continue
                choices = obj.get("choices") or []
                if choices:
                    delta = choices[0].get("delta") or {}
                    if delta.get("content"):
                        yield delta["content"]

    def _parse(self, data: dict[str, Any]) -> ChatResult:
        choice = data["choices"][0]
        msg = choice["message"]
        tool_calls = [
            ToolCall(
                id=tc["id"],
                name=tc["function"]["name"],
                arguments=tc["function"].get("arguments", "{}"),
            )
            for tc in msg.get("tool_calls") or []
        ]
        message = Message(
            role="assistant",
            content=msg.get("content") or "",
            tool_calls=tool_calls,
        )
        usage = Usage(
            in_tokens=(data.get("usage") or {}).get("prompt_tokens", 0),
            out_tokens=(data.get("usage") or {}).get("completion_tokens", 0),
        )
        if self.cost is not None:
            self.cost.record(self.model, usage.in_tokens, usage.out_tokens)
        return ChatResult(message=message, usage=usage, finish_reason=choice.get("finish_reason", "stop"))


class AnthropicClient:
    """Native Anthropic client (messages API). Used when an Anthropic model is
    configured and we want full tool-calling fidelity.

    Converts our OpenAI-style Message/Tool types to Anthropic's format.
    """

    def __init__(
        self,
        api_key: str,
        model: str,
        *,
        base_url: str = "https://api.anthropic.com/v1",
        cost: CostTracker | None = None,
        timeout: float = 120.0,
    ):
        self.api_key = api_key
        self.model = model
        self.cost = cost
        self._http = httpx.Client(timeout=timeout)
        self._base = base_url.rstrip("/")

    def chat(
        self,
        messages: list[Message],
        tools: list[ToolSpec] | None = None,
        temperature: float = 0.2,
        max_tokens: int | None = None,
    ) -> ChatResult:
        system, conv = _split_system(messages)
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": max_tokens or 4096,
            "messages": conv,
            "temperature": temperature,
        }
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = [
                {
                    "name": t.name,
                    "description": t.description,
                    "input_schema": t.parameters,
                }
                for t in tools
            ]
        resp = self._http.post(
            f"{self._base}/messages",
            json=payload,
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
            },
        )
        resp.raise_for_status()
        data = resp.json()
        return self._parse(data)

    def stream(self, messages, tools=None, temperature=0.2) -> Iterator[str]:
        """Stream Anthropic text deltas via the messages API (SSE).

        Yields content-block text deltas. Mirrors the OpenAI impl's contract
        (tool calls are not streamed — the agent only uses stream() for the
        final-answer turn)."""
        system, conv = _split_system(messages)
        payload: dict[str, Any] = {
            "model": self.model,
            "max_tokens": 4096,
            "messages": conv,
            "temperature": temperature,
            "stream": True,
        }
        if system:
            payload["system"] = system
        if tools:
            payload["tools"] = [
                {"name": t.name, "description": t.description, "input_schema": t.parameters}
                for t in tools
            ]
        with self._http.stream(
            "POST",
            f"{self._base}/messages",
            json=payload,
            headers={
                "x-api-key": self.api_key,
                "anthropic-version": "2023-06-01",
            },
        ) as resp:
            resp.raise_for_status()
            for line in resp.iter_lines():
                if not line or not line.startswith("data: "):
                    continue
                chunk = line[len("data: "):]
                try:
                    obj = json.loads(chunk)
                except json.JSONDecodeError:
                    continue
                # Anthropic SSE event types:
                #  - content_block_delta: {delta: {type: "text_delta", text: "..."}}
                #  - message_stop: terminal
                if obj.get("type") == "content_block_delta":
                    delta = obj.get("delta") or {}
                    if delta.get("type") == "text_delta" and delta.get("text"):
                        yield delta["text"]
                elif obj.get("type") == "message_stop":
                    break

    def _parse(self, data: dict[str, Any]) -> ChatResult:
        content_blocks = data.get("content") or []
        text_parts: list[str] = []
        tool_calls: list[ToolCall] = []
        for block in content_blocks:
            if block["type"] == "text":
                text_parts.append(block["text"])
            elif block["type"] == "tool_use":
                tool_calls.append(
                    ToolCall(
                        id=block["id"],
                        name=block["name"],
                        arguments=json.dumps(block.get("input") or {}),
                    )
                )
        usage = Usage(
            in_tokens=(data.get("usage") or {}).get("input_tokens", 0),
            out_tokens=(data.get("usage") or {}).get("output_tokens", 0),
        )
        if self.cost is not None:
            self.cost.record(self.model, usage.in_tokens, usage.out_tokens)
        return ChatResult(
            message=Message(role="assistant", content="".join(text_parts), tool_calls=tool_calls),
            usage=usage,
            finish_reason=data.get("stop_reason", "stop"),
        )


def _split_system(messages: list[Message]) -> tuple[str, list[dict[str, Any]]]:
    """Anthropic wants `system` as a top-level field, not a message."""
    sys_parts = [m.content for m in messages if m.role == "system"]
    conv: list[dict[str, Any]] = []
    for m in messages:
        if m.role == "system":
            continue
        if m.role == "tool":
            # Anthropic: tool results go as user messages with tool_result blocks.
            conv.append(
                {
                    "role": "user",
                    "content": [
                        {"type": "tool_result", "tool_use_id": m.tool_call_id, "content": m.content}
                    ],
                }
            )
        elif m.role == "assistant" and m.tool_calls:
            blocks: list[dict[str, Any]] = []
            if m.content:
                blocks.append({"type": "text", "text": m.content})
            for tc in m.tool_calls:
                blocks.append(
                    {
                        "type": "tool_use",
                        "id": tc.id,
                        "name": tc.name,
                        "input": json.loads(tc.arguments) if tc.arguments else {},
                    }
                )
            conv.append({"role": "assistant", "content": blocks})
        else:
            conv.append({"role": m.role, "content": m.content})
    return "\n\n".join(sys_parts), conv
