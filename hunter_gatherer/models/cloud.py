"""Cloud model client factory.

Builds the right client for a model id based on config. Any model listed in
`settings.messages_api_models` (or, as a back-compat fallback, starting with
`claude-`) routes through the Anthropic Messages client at
`settings.messages_api_base_url` — this covers Anthropic, Z.ai/Zhipu GLM, and
Bedrock. Anything else uses the OpenAI-compatible client.
"""
from __future__ import annotations

from hunter_gatherer.config import Settings
from hunter_gatherer.cost import CostTracker
from hunter_gatherer.models.base import LLMClient
from hunter_gatherer.models.clients import AnthropicClient, OpenAICompatibleClient


def make_cloud_client(
    model: str, settings: Settings, *, cost: CostTracker | None = None
) -> LLMClient | None:
    """Build a cloud client for `model`, or None if no credentials.

    Routing: the Anthropic Messages client is used when the model is in the
    configured `messages_api_models` set (e.g. "GLM-5.2", "claude-...") or, as a
    back-compat fallback, when the id starts with `claude-`. Otherwise the
    OpenAI-compatible client is used. This lets any Messages-compatible
    endpoint (Z.ai/Zhipu, Bedrock, Anthropic itself) be wired in without code.
    """
    # Prefer the vendor-neutral messages_api_key; fall back to the legacy
    # anthropic_api_key alias so existing configs keep working.
    api_key = settings.messages_api_key or settings.anthropic_api_key
    uses_messages_api = model.lower() in settings.messages_api_model_set or model.startswith("claude-")
    if uses_messages_api:
        if not api_key:
            return None
        return AnthropicClient(
            api_key=api_key,
            model=model,
            base_url=settings.messages_api_base_url,
            cost=cost,
        )
    # Default: OpenAI-compatible cloud endpoint.
    if not settings.has_openai:
        return None
    return OpenAICompatibleClient(
        base_url="https://api.openai.com/v1",
        api_key=settings.openai_api_key,
        model=model,
        cost=cost,
    )


def make_local_client_from_settings(
    settings: Settings, *, cost: CostTracker | None = None
) -> LLMClient:
    from hunter_gatherer.models.local import make_local_client

    return make_local_client(settings.llama_base_url, settings.local_model, cost=cost)
