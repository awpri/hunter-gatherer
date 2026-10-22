"""Shared test fixtures.

Tests run without Qdrant and without sentence-transformers. They explicitly
select hashing embeddings, which are fast and deterministic for the logic
we're testing; production missing dependencies must never select hashing.

Tests of explicit model adapters can override the factory availability check
locally; no test downloads weights implicitly.
"""
from __future__ import annotations

import os
import pathlib

import pytest

from hunter_gatherer.config import Settings
from hunter_gatherer.memory.embeddings import HashingEmbedder
from hunter_gatherer.memory.store import MemoryStore

# Repo path (the .env file lives here); tests run from a different cwd so the
# runtime `.env` is never read by `Settings()` during the suite.
_REPO_ROOT = pathlib.Path(__file__).resolve().parent.parent


@pytest.fixture(autouse=True)
def _hermetic_settings_env(monkeypatch, tmp_path):
    """Keep tests hermetic against the repo's runtime `.env` file.

    A repo `.env` legitimately sets runtime defaults (e.g.
    HG_LOCAL_ONLY=true, HG_SQLITE_PATH=/abs/path) for the live
    harness. Tests should not inherit those — they construct `Settings(...)`
    explicitly for the behavior under test. We (1) strip every harness-relevant
    env var, and (2) chdir into a tmp dir so pydantic-settings does not find a
    `.env` relative to cwd. Tests that exercise .env loading (test_config_env)
    pass `_env_file` explicitly or set vars after this strip.
    """
    for prefix in ("HG_", "OPENAI_", "ANTHROPIC_", "MESSAGES_", "QDRANT_", "LLAMA_", "SQLITE_", "LOCAL_ONLY"):
        for k in list(os.environ):
            if k.startswith(prefix):
                monkeypatch.delenv(k, raising=False)
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("HG_EMBED_MODEL", "hashing")
    monkeypatch.setenv("HG_VECTOR_BACKEND", "memory")
    monkeypatch.setattr("hunter_gatherer.memory.embeddings._st_available", lambda: False)
    for name in ("HF_HUB_OFFLINE", "TRANSFORMERS_OFFLINE", "HF_DATASETS_OFFLINE"):
        monkeypatch.setenv(name, "1")


@pytest.fixture
def tmp_db(tmp_path) -> str:
    return str(tmp_path / "test.db")


@pytest.fixture
def settings(tmp_db) -> Settings:
    return Settings(sqlite_path=tmp_db, local_only=True)


@pytest.fixture
def embedder() -> HashingEmbedder:
    # Small dim keeps the brute-force backend snappy.
    return HashingEmbedder(dim=128)


@pytest.fixture
def store(settings, embedder) -> MemoryStore:
    s = MemoryStore(settings, embedder=embedder)
    yield s
    s.close()
