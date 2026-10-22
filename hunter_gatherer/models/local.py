"""Local LLM client (llama.cpp server).

llama.cpp's server exposes an OpenAI-compatible /v1 API, so this is a thin
specialization of OpenAICompatibleClient that defaults api_key to a dummy
(local servers ignore it) and pins the model id to whatever llama.cpp is
serving.
"""
from __future__ import annotations

from hunter_gatherer.cost import CostTracker
from hunter_gatherer.models.clients import OpenAICompatibleClient


def make_local_client(
    base_url: str,
    model: str = "local",
    *,
    cost: CostTracker | None = None,
) -> OpenAICompatibleClient:
    """Build a client for a llama.cpp (or compatible) local server.

    Start the server with, e.g.:
        ./llama-server -m llama-3.1-8b-instruct.Q4_K_M.gguf --port 8080 \
            --jinja --tools
    `--jinja` + `--tools` enable tool/function calling on supported models.
    """
    return OpenAICompatibleClient(
        base_url=base_url,
        api_key="local-no-key",
        model=model,
        cost=cost,
    )
