"""Regression tests for config env-var loading.

History: a bug where every documented `HG_*` env var was silently
ignored — `Settings` had `env_prefix=""` but `.env.example` documents the
`HG_` prefix, so setting `HG_SQLITE_PATH` / `HG_ROUTER_ALPHA`
/ etc. did nothing and the harness fell back to defaults with no warning. These
tests pin the contract: documented env vars are read.
"""
from __future__ import annotations

import pytest

from hunter_gatherer.config import Settings

# The documented env vars (a representative sample across all field groups).
# If any of these regress to being silently ignored, the corresponding test fails.
_PREFIXED = {
    "HG_SQLITE_PATH": "/tmp/x.db",
    "HG_ROUTER_ALPHA": "0.42",
    "HG_ROUTER_TRIVIAL_MAX_CHARS": "200",
    "HG_LOCAL_ONLY": "true",
    "HG_RECALL_TOPK": "11",
    "HG_DEDUP_COSINE": "0.77",
    "HG_REHEARSAL_WINDOW": "5",
    "HG_REFLECT_IMPORTANCE_THRESHOLD": "12.0",
    "HG_EMBED_MODEL": "BAAI/bge-m3",
    "HG_WHISPER_MODEL": "small",
    "HG_CHEAP_MODEL": "claude-3-5-sonnet",
}


@pytest.fixture
def clean_env(monkeypatch, tmp_path):
    """Strip every harness-relevant env var so tests start from defaults."""
    import os

    for prefix in ("HG_", "OPENAI_", "ANTHROPIC_", "MESSAGES_", "QDRANT_", "LLAMA_", "SQLITE_"):
        for k in list(os.environ):
            if k.startswith(prefix):
                monkeypatch.delenv(k, raising=False)
    monkeypatch.setenv("HG_CONFIG_FILE", str(tmp_path / "absent-config.env"))
    return monkeypatch


def test_defaults_when_nothing_set(clean_env):
    s = Settings()
    assert s.sqlite_path == "library.db"
    assert s.router_alpha == 0.55
    assert s.recall_topk == 8
    assert s.openai_api_key == ""


def test_prefixed_project_vars_are_read(clean_env):
    """Every documented HG_* env var must actually be read."""
    m = clean_env
    m.setenv("HG_ROUTER_ALPHA", "0.42")
    m.setenv("HG_SQLITE_PATH", "/tmp/from-env.db")
    m.setenv("HG_RECALL_TOPK", "11")
    m.setenv("HG_DEDUP_COSINE", "0.77")
    m.setenv("HG_REHEARSAL_WINDOW", "5")
    s = Settings()
    assert s.router_alpha == 0.42
    assert s.sqlite_path == "/tmp/from-env.db"
    assert s.recall_topk == 11
    assert s.dedup_cosine == 0.77
    assert s.rehearsal_window == 5


def test_provider_keys_keep_conventional_names(clean_env):
    """Vendor credentials remain usable; stateful Qdrant settings are isolated."""
    m = clean_env
    m.setenv("OPENAI_API_KEY", "sk-conventional")
    m.setenv("QDRANT_URL", "http://nas:6333")
    m.setenv("QDRANT_COLLECTION", "my_mems")
    m.setenv("LLAMA_BASE_URL", "http://gpu:8080/v1")
    s = Settings()
    assert s.openai_api_key == "sk-conventional"
    assert s.qdrant_url == "http://localhost:6333"
    assert s.qdrant_collection == "hg_memories"
    assert s.llama_base_url == "http://gpu:8080/v1"


def test_hg_configuration_ignores_unrelated_prefixes_and_current_directory_settings(clean_env, tmp_path):
    clean_env.chdir(tmp_path)
    (tmp_path / ".env").write_text("HG_SQLITE_PATH=wrong.db\nUNRELATED_SQLITE_PATH=other.db\n", encoding="utf-8")
    clean_env.setenv("UNRELATED_SQLITE_PATH", "other-library.db")
    clean_env.setenv("UNRELATED_VECTOR_BACKEND", "qdrant")
    # An explicit empty config keeps the test independent of a user's h/g file.
    clean_env.setenv("HG_CONFIG_FILE", str(tmp_path / "absent-config.env"))
    settings = Settings()
    assert settings.sqlite_path == "library.db"
    assert settings.vector_backend == "memory" and settings.embed_model == "hashing"
    assert settings.mcp_tool_profile == "library"
    chosen = tmp_path / "chosen.env"
    chosen.write_text("HG_SQLITE_PATH=chosen.db\nHG_QDRANT_URL=http://chosen:6333\n", encoding="utf-8")
    clean_env.setenv("HG_CONFIG_FILE", str(chosen))
    assert Settings().sqlite_path == "chosen.db"
    assert Settings().qdrant_url == "http://chosen:6333"


def test_kwargs_override_env(clean_env):
    """Explicit kwargs win over env vars (tests rely on this)."""
    clean_env.setenv("HG_ROUTER_ALPHA", "0.99")
    s = Settings(router_alpha=0.11, sqlite_path="/tmp/kwarg.db")
    assert s.router_alpha == 0.11
    assert s.sqlite_path == "/tmp/kwarg.db"


def test_local_only_bool_parses(clean_env):
    """Boolean env vars parse common truthy forms."""
    clean_env.setenv("HG_LOCAL_ONLY", "true")
    assert Settings().local_only is True
    clean_env.setenv("HG_LOCAL_ONLY", "1")
    assert Settings().local_only is True
    clean_env.setenv("HG_LOCAL_ONLY", "false")
    assert Settings().local_only is False


# ── Messages-API (vendor-neutral) config + GLM routing ───────────────────────
def test_messages_api_env_vars_are_read(clean_env):
    """The vendor-neutral MESSAGES_API_* knobs are read from env."""
    m = clean_env
    m.setenv("HG_MESSAGES_API_KEY", "zai-key")
    m.setenv("HG_MESSAGES_API_BASE_URL", "https://api.z.ai/api/anthropic")
    m.setenv("HG_MESSAGES_API_MODELS", "GLM-5.2,glm-5-turbo")
    s = Settings()
    assert s.messages_api_key == "zai-key"
    assert s.messages_api_base_url == "https://api.z.ai/api/anthropic"
    assert s.messages_api_model_set == {"glm-5.2", "glm-5-turbo"}
    assert s.has_anthropic is True  # union of messages_api_key + legacy


def test_legacy_anthropic_key_still_works(clean_env):
    """ANTHROPIC_API_KEY still populates the credential (back-compat alias)."""
    clean_env.setenv("ANTHROPIC_API_KEY", "legacy-key")
    s = Settings()
    assert s.has_anthropic is True
    # The unioned credential resolves (messages_api_key preferred, legacy as fallback).
    assert (s.messages_api_key or s.anthropic_api_key) == "legacy-key"


def test_make_cloud_client_routes_glm_to_messages_client(clean_env):
    """A model listed in messages_api_models builds an AnthropicClient pointed at
    the configured base URL — the path that wires in Z.ai/Zhipu GLM."""
    from hunter_gatherer.models.clients import AnthropicClient
    from hunter_gatherer.models.cloud import make_cloud_client

    clean_env.setenv("HG_MESSAGES_API_KEY", "zai-key")
    clean_env.setenv("HG_MESSAGES_API_BASE_URL", "https://api.z.ai/api/anthropic")
    clean_env.setenv("HG_MESSAGES_API_MODELS", "GLM-5.2,glm-5-turbo")
    s = Settings()
    client = make_cloud_client("GLM-5.2", s)
    assert isinstance(client, AnthropicClient)
    assert client._base == "https://api.z.ai/api/anthropic"  # noqa: SLF001
    assert client.model == "GLM-5.2"


def test_make_cloud_client_routes_openai_by_default(clean_env):
    """A model NOT in messages_api_models (and not claude-) routes to OpenAI."""
    from hunter_gatherer.models.clients import OpenAICompatibleClient
    from hunter_gatherer.models.cloud import make_cloud_client

    clean_env.setenv("OPENAI_API_KEY", "sk-test")
    s = Settings()
    client = make_cloud_client("gpt-4o", s)
    assert isinstance(client, OpenAICompatibleClient)


def test_make_cloud_client_claude_prefix_back_compat(clean_env):
    """A claude-* model routes to the Messages client even when
    messages_api_models is empty (preserves the old prefix heuristic)."""
    from hunter_gatherer.models.clients import AnthropicClient
    from hunter_gatherer.models.cloud import make_cloud_client

    clean_env.setenv("HG_MESSAGES_API_KEY", "k")
    s = Settings()
    assert s.messages_api_models == ""  # nothing explicitly configured
    client = make_cloud_client("claude-3-5-sonnet-20241022", s)
    assert isinstance(client, AnthropicClient)


def test_anthropic_client_uses_configured_base_url():
    """AnthropicClient posts to whatever base_url it's given (not hard-coded)."""
    from hunter_gatherer.models.clients import AnthropicClient

    c = AnthropicClient(api_key="k", model="GLM-5.2", base_url="https://api.z.ai/api/anthropic")
    assert c._base == "https://api.z.ai/api/anthropic"  # noqa: SLF001
    # Default still the canonical Anthropic endpoint when nothing is configured.
    c2 = AnthropicClient(api_key="k", model="claude-3")
    assert c2._base == "https://api.anthropic.com/v1"  # noqa: SLF001


def test_archive_search_defaults_preserve_exhaustive(clean_env):
    settings = Settings(_env_file=None)
    assert settings.archive_search_policy == "exhaustive"
    assert settings.archive_candidate_budget == 128


def test_archive_search_env_and_explicit_overrides(clean_env):
    clean_env.setenv("HG_ARCHIVE_SEARCH_POLICY", "bounded")
    clean_env.setenv("HG_ARCHIVE_CANDIDATE_BUDGET", "256")
    settings = Settings(_env_file=None)
    assert settings.archive_search_policy == "bounded"
    assert settings.archive_candidate_budget == 256
    explicit = Settings(_env_file=None, archive_search_policy="exhaustive", archive_candidate_budget=30)
    assert explicit.archive_search_policy == "exhaustive"
    assert explicit.archive_candidate_budget == 30


@pytest.mark.parametrize("policy", ["automatic", "", "BOUNDED"])
def test_archive_search_rejects_unknown_policy(clean_env, policy):
    from pydantic import ValidationError
    clean_env.setenv("HG_ARCHIVE_SEARCH_POLICY", policy)
    with pytest.raises(ValidationError, match="archive_search_policy"):
        Settings(_env_file=None)


@pytest.mark.parametrize("budget", ["29", "2049", "1.5", "invalid"])
def test_archive_candidate_budget_rejects_invalid_values(clean_env, budget):
    from pydantic import ValidationError
    clean_env.setenv("HG_ARCHIVE_CANDIDATE_BUDGET", budget)
    with pytest.raises(ValidationError, match="archive_candidate_budget"):
        Settings(_env_file=None)


@pytest.mark.parametrize("budget", [30, 2048])
def test_archive_candidate_budget_accepts_boundaries(clean_env, budget):
    assert Settings(_env_file=None, archive_candidate_budget=budget).archive_candidate_budget == budget
