"""The base library stays useful without silently replacing configured models."""
from __future__ import annotations

import builtins
import os
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from hunter_gatherer.config import Settings
from hunter_gatherer.memory.embeddings import HashingEmbedder, SentenceTransformerEmbedder, get_embedder
from hunter_gatherer.memory.store import MemoryStore
from hunter_gatherer.optional_dependencies import OptionalDependencyError


def _block_import(monkeypatch, package):
    original = builtins.__import__

    def importing(name, *args, **kwargs):
        if name.split(".", 1)[0] == package:
            raise ModuleNotFoundError(f"No module named '{package}'", name=package)
        return original(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", importing)


def test_missing_semantic_dependency_never_creates_or_changes_library(tmp_path):
    path = tmp_path / "existing.db"
    with MemoryStore(Settings(sqlite_path=str(path)), embedder=HashingEmbedder()):
        pass
    before = path.read_bytes()
    for target in (path, tmp_path / "new.db"):
        configured = Settings(sqlite_path=str(target), embed_model="BAAI/bge-small-en-v1.5")
        with pytest.raises(OptionalDependencyError, match=r"\[semantic\]"):
            MemoryStore(configured)
    assert path.read_bytes() == before
    assert not (tmp_path / "new.db").exists()


def test_configured_semantic_adapter_remains_lazy(monkeypatch):
    monkeypatch.setattr("hunter_gatherer.memory.embeddings._st_available", lambda: True)
    _block_import(monkeypatch, "sentence_transformers")
    embedder = get_embedder(Settings(embed_model="configured-model"))
    assert isinstance(embedder, SentenceTransformerEmbedder)
    assert embedder.fingerprint() == "st:configured-model"
    with pytest.raises(OptionalDependencyError, match=r"\[semantic\]"):
        embedder.embed("a question")


def test_missing_qdrant_dependency_never_selects_memory_backend(monkeypatch, tmp_path):
    _block_import(monkeypatch, "qdrant_client")
    path = tmp_path / "qdrant.db"
    settings = Settings(sqlite_path=str(path), vector_backend="qdrant")
    with pytest.raises(OptionalDependencyError, match=r"\[qdrant\]"):
        MemoryStore(settings, embedder=HashingEmbedder())
    assert not path.exists()


def test_openai_speech_explains_its_own_optional_extra(monkeypatch):
    from hunter_gatherer.voice.tts import OpenAITTS

    _block_import(monkeypatch, "openai")
    with pytest.raises(OptionalDependencyError, match=r"\[tts-openai\]"):
        OpenAITTS(api_key="fictional-test-key").speak("Hello")


def test_doctor_does_not_promise_a_fallback_for_missing_qdrant(monkeypatch, tmp_path, capsys):
    from hunter_gatherer import cli

    _block_import(monkeypatch, "qdrant_client")
    monkeypatch.setattr(cli, "get_settings", lambda: Settings(sqlite_path=str(tmp_path / "doctor.db")))
    assert cli.main(["doctor"]) == 0
    output = capsys.readouterr().out
    assert "qdrant extra" in output
    assert "will use in-memory" not in output


def test_demo_and_library_start_without_heavy_dependencies(tmp_path):
    # A new process prevents the test runner's imports/stubs from concealing a
    # transitive dependency on model, cloud SDK, voice, or external vector code.
    script = textwrap.dedent("""
        import importlib.abc
        import socket
        import sys
        from pathlib import Path

        blocked = {
            'sentence_transformers', 'transformers', 'torch', 'qdrant_client',
            'openai', 'anthropic', 'faster_whisper', 'silero_vad', 'sounddevice',
            'torchaudio', 'piper', 'docling', 'numpy',
        }
        class NoOptionalPackages(importlib.abc.MetaPathFinder):
            def find_spec(self, fullname, path=None, target=None):
                if fullname.split('.', 1)[0] in blocked:
                    raise ModuleNotFoundError('Optional dependency blocked: ' + fullname,
                                              name=fullname)
        sys.meta_path.insert(0, NoOptionalPackages())
        def no_network(*args, **kwargs):
            raise AssertionError('Offline base demo attempted a network connection')
        socket.socket.connect = no_network

        from hunter_gatherer import cli
        from hunter_gatherer.mcp.tools import call_tool
        from hunter_gatherer.models.clients import AnthropicClient, OpenAICompatibleClient
        import uvicorn

        # Chat provider adapters use the base HTTP client, not provider SDKs.
        for client in (
            AnthropicClient(api_key='fictional', model='example'),
            OpenAICompatibleClient('http://localhost:1', 'fictional', 'example'),
        ):
            client._http.close()

        def inspect_app(app, **options):
            owner = app.state.library_harness
            result = call_tool(owner, 'hg_find', {'query': 'Lantern weekly review schedule'})
            assert 'Tuesday' in str(result), result
            assert owner.loop.embedder.fingerprint() == 'hashing:384'
            assert owner.settings.vector_backend == 'memory'
            assert options['host'] == '127.0.0.1'
        uvicorn.run = inspect_app
        target = Path(sys.argv[1])
        assert cli.main(['demo', 'create', str(target)]) == 0
        assert cli.main(['demo', 'serve', str(target)]) == 0
        assert cli.main(['demo', 'serve', str(target)]) == 0
        assert not blocked.intersection(sys.modules)
    """)
    env = dict(os.environ)
    # Use the imported package, whether pytest targets a checkout or an installed
    # candidate wheel; never accidentally fall back to another personal install.
    import hunter_gatherer

    env["PYTHONPATH"] = str(Path(hunter_gatherer.__file__).resolve().parent.parent)
    result = subprocess.run(
        [sys.executable, "-B", "-c", script, str(tmp_path / "minimal demo")],
        capture_output=True, text=True, env=env, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr
