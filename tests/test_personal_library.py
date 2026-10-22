"""Real-library onboarding stays isolated and preserves exact evidence."""
from __future__ import annotations

import asyncio
import json
import shutil
import socket
import sqlite3
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

from hunter_gatherer import cli
from hunter_gatherer.backup import create_backup
from hunter_gatherer.library import MANIFEST, SEMANTIC_MODEL, build_library_harness, create_library, import_files, library_settings
from hunter_gatherer.memory.documents import DocumentArchive
from hunter_gatherer.memory.embeddings import HashingEmbedder
from hunter_gatherer.memory.simple_search import find
from hunter_gatherer.memory.store import MemoryStore
from hunter_gatherer.optional_dependencies import OptionalDependencyError


def test_empty_library_is_offline_and_ignores_ambient_configuration(tmp_path, monkeypatch):
    old = tmp_path / "live-proharness.db"
    old.write_bytes(b"untouched private database")
    for prefix in ("HG", "PROHARNESS"):
        monkeypatch.setenv(f"{prefix}_SQLITE_PATH", str(old))
        monkeypatch.setenv(f"{prefix}_VECTOR_BACKEND", "qdrant")
        monkeypatch.setenv(f"{prefix}_EMBED_MODEL", "not-a-model")
    monkeypatch.setenv("OPENAI_API_KEY", "fictional-test-secret")
    (tmp_path / ".env").write_text("HG_SQLITE_PATH=wrong.db\n", encoding="utf-8")
    monkeypatch.setattr(socket.socket, "connect", lambda *a, **k: pytest.fail("network access"))
    monkeypatch.setattr("hunter_gatherer.memory.embeddings._st_available", lambda: pytest.fail("neural package probe"))
    target = tmp_path / "my library"
    manifest = create_library(target)
    assert manifest["database"] == "library.db" and manifest["search_mode"] == "keyword"
    assert str(tmp_path) not in (target / MANIFEST).read_text(encoding="utf-8")
    settings = library_settings(target)
    assert settings.sqlite_path == str(target / "library.db")
    assert settings.embed_model == "hashing" and settings.vector_backend == "memory"
    assert settings.local_only and settings.embed_local_files_only
    assert not settings.openai_api_key and not settings.messages_api_key and not settings.qdrant_url
    with MemoryStore(settings) as store:
        assert store._sqlite.execute("SELECT COUNT(*) FROM memories").fetchone()[0] == 0
        assert store._sqlite.execute("SELECT COUNT(*) FROM archive_documents").fetchone()[0] == 0
        result = find(SimpleNamespace(store=store, settings=settings), query="Any papers about auroras?", max_tokens=2048)
        assert not result["supporting_passages"] and not result["relevant_memories"]
    assert old.read_bytes() == b"untouched private database"


def test_library_import_search_restart_backup_restore_retains_citations(tmp_path):
    target = tmp_path / "working library"
    create_library(target)
    source = tmp_path / "observatory.md"
    exact = "# Observatory schedule\n\nThe telescope opens on Friday at 21:30 UTC.\nWeather closures may cancel observing.\n"
    source.write_text(exact, encoding="utf-8")
    result = import_files(target, [str(source)])
    assert result["counts"]["succeeded"] == 1 and result["complete"]
    settings = library_settings(target)
    with MemoryStore(settings) as store:
        result = find(SimpleNamespace(store=store, settings=settings), query="When does the telescope open?", max_tokens=4096)
        passage = result["supporting_passages"][0]
        assert "Friday at 21:30 UTC" in passage["quote"]
        citation, chunk_id, document_id = passage["citation"], passage["chunk_id"], passage["document_id"]
    bundle = tmp_path / "backup"
    create_backup(target / "library.db", bundle)
    # Restoration must be independent of the original selected source and the
    # original library directory, not accidentally read their absolute paths.
    source.unlink()
    shutil.rmtree(target)
    restored = tmp_path / "restored library"
    manifest = create_library(restored, from_backup=bundle)
    assert manifest["search_mode"] == "keyword"
    with MemoryStore(library_settings(restored)) as store:
        archive = DocumentArchive(store)
        chunk = archive.get_chunk(chunk_id)
        assert chunk["citation"] == citation and chunk["document_id"] == document_id
        assert "Friday at 21:30 UTC" in chunk["quote"]
        from hunter_gatherer.memory.document_originals import load_document_original
        original = load_document_original(store, document_id)
        assert original["_original_bytes"] == exact.encode("utf-8")


def test_private_import_requires_explicit_visibility(tmp_path):
    target = tmp_path / "library"
    create_library(target)
    source = tmp_path / "private.txt"
    source.write_text("Private telescope access code: sapphire owl.", encoding="utf-8")
    assert import_files(target, [source], sensitive=True)["counts"]["succeeded"] == 1
    settings = library_settings(target)
    with MemoryStore(settings) as store:
        harness = SimpleNamespace(store=store, settings=settings)
        assert not find(harness, query="telescope access code")["supporting_passages"]
        assert find(harness, query="telescope access code", include_sensitive=True)["supporting_passages"]


def test_no_overwrites_and_manifest_misbindings(tmp_path):
    target = tmp_path / "library"
    original = create_library(target)
    before = (target / "library.db").read_bytes()
    with pytest.raises(ValueError, match="empty directory"):
        create_library(target)
    assert (target / "library.db").read_bytes() == before
    for change in ({"database": "../private.db"}, {"embedding": "st:wrong"},
                   {"library_id": "11d437a7-d7d5-4a58-b8b0-e5590184b976"}):
        (target / MANIFEST).write_text(json.dumps({**original, **change}), encoding="utf-8")
        with pytest.raises(ValueError):
            library_settings(target)
        assert (target / "library.db").read_bytes() == before
    with pytest.raises(ValueError, match="explicit"):
        create_library("")
    with pytest.raises(ValueError, match="initialized"):
        library_settings(tmp_path / "missing")


def test_refuses_directory_and_managed_archive_symlinks(tmp_path):
    target = tmp_path / "library"
    create_library(target)
    linked = tmp_path / "redirect"
    linked.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="symlinks"):
        library_settings(linked)
    with pytest.raises(ValueError, match="symlinks"):
        create_library(linked / "new")
    archive = target / "library.db.archive"
    archive.mkdir(exist_ok=True)
    outside = tmp_path / "outside"
    outside.mkdir()
    (archive / "originals").symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symlinks"):
        library_settings(target)
    assert list(outside.iterdir()) == []


def test_semantic_opt_in_never_silently_replaces_or_downloads(tmp_path, monkeypatch):
    absent = tmp_path / "missing model library"
    with pytest.raises(OptionalDependencyError, match="semantic"):
        create_library(absent, semantic=True)
    assert not absent.exists()
    with pytest.raises(ValueError, match="requires --semantic"):
        create_library(absent, download_model=True)
    attempted = []

    def no_cached_model(settings):
        attempted.append(settings.embed_local_files_only)
        class Missing:
            def embed(self, text):
                raise OSError("not cached")
        return Missing()

    monkeypatch.setattr("hunter_gatherer.memory.embeddings.get_embedder", no_cached_model)
    with pytest.raises(ValueError, match="--download-model"):
        create_library(absent, semantic=True)
    assert attempted == [True] and not absent.exists()
    with pytest.raises(ValueError):
        create_library(absent, semantic=True, download_model=True)
    assert attempted == [True, False] and not absent.exists()


def test_cached_semantic_initialization_and_restart_keep_model_identity(tmp_path, monkeypatch):
    attempts = []
    class CachedModel(HashingEmbedder):
        def fingerprint(self):
            return f"st:{SEMANTIC_MODEL}"
    def cached(settings):
        attempts.append(settings.embed_local_files_only)
        return CachedModel()
    monkeypatch.setattr("hunter_gatherer.memory.embeddings.get_embedder", cached)
    target = tmp_path / "semantic"
    manifest = create_library(target, semantic=True, download_model=True)
    assert attempts == [False] and manifest["search_mode"] == "semantic"
    settings = library_settings(target)
    assert settings.embed_local_files_only and settings.embed_model == SEMANTIC_MODEL


def test_library_runtime_ignores_agent_skills_and_all_six_tools_still_work(tmp_path, monkeypatch):
    target = tmp_path / "library"
    create_library(target)
    unrelated = tmp_path / "user-skills"
    unrelated.mkdir()
    # This frontmatter is invalid for the inherited agent skill parser and
    # previously stopped a library server even though its tools never use it.
    (unrelated / "SKILL.md").write_text("---\n- invalid-list-frontmatter\n---\nUnused.\n", encoding="utf-8")
    monkeypatch.setattr("hunter_gatherer.loop.default_skill_dirs", lambda: [str(unrelated)])
    monkeypatch.setattr("hunter_gatherer.loop.AgentLoop.__init__", lambda *a, **k: pytest.fail("library initialized the agent executor"))
    harness = build_library_harness(library_settings(target))
    source = tmp_path / "weather.txt"
    source.write_text("The telescope closes during thunderstorms.", encoding="utf-8")
    try:
        assert not hasattr(harness.loop, "router") and not hasattr(harness.loop, "skills")
        from hunter_gatherer.operations import readiness
        from hunter_gatherer.mcp.tools import call_tool
        assert readiness(harness)["ready"]
        assert call_tool(harness, "hg_status", {})["store"]["database"] == str(target / "library.db")
        remembered = call_tool(harness, "hg_memory", {"action": "remember", "text": "My telescope has a blue case."})
        assert remembered["stored"]
        profile = call_tool(harness, "hg_memory", {"action": "profile"})
        assert isinstance(profile, dict) and not profile.get("error")
        imported = call_tool(harness, "hg_import_documents", {"action": "create", "paths": [str(source)]})
        imported = call_tool(harness, "hg_import_documents", {"action": "run", "job_id": imported["job_id"]})
        assert imported["counts"]["succeeded"] == 1
        found = call_tool(harness, "hg_find", {"query": "When does the telescope close?"})
        passage = found["supporting_passages"][0]
        opened = call_tool(harness, "hg_open", {"refs": [passage["citation"]]})
        assert opened["sources"][0]["quote"] == passage["quote"]
        saved = call_tool(harness, "hg_collection", {"action": "save", "idempotency_key": "runtime-test",
            "collection": {"name": "Weather sources", "references": [{"document_id": passage["document_id"], "chunk_id": passage["chunk_id"]}]}})
        assert saved["id"]
        exported = call_tool(harness, "hg_collection", {"action": "export", "collection_id": saved["id"]})
        assert "thunderstorms" in json.dumps(exported)
    finally:
        harness.loop.close()
    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        harness.store._sqlite.execute("SELECT 1")


def test_semantic_runtime_warms_before_store_and_readiness(tmp_path, monkeypatch):
    target = tmp_path / "semantic library"
    events = []
    class CachedModel(HashingEmbedder):
        _model = None
        def fingerprint(self):
            return f"st:{SEMANTIC_MODEL}"
        def embed(self, text):
            events.append("embed")
            self._model = object()
            return super().embed(text)
    monkeypatch.setattr("hunter_gatherer.memory.embeddings.get_embedder", lambda settings: CachedModel())
    create_library(target, semantic=True)
    events.clear()
    real_store = MemoryStore
    def inspect_order(*args, **kwargs):
        assert events == ["embed"], "Writable store opened before model preflight"
        return real_store(*args, **kwargs)
    monkeypatch.setattr("hunter_gatherer.memory.store.MemoryStore", inspect_order)
    harness = build_library_harness(library_settings(target))
    try:
        from hunter_gatherer.operations import readiness
        assert readiness(harness)["ready"]
        assert harness.settings.embed_local_files_only
    finally:
        harness.loop.close()


def test_semantic_runtime_passes_local_only_to_model_loader(monkeypatch, tmp_path):
    from hunter_gatherer.library import _settings
    from hunter_gatherer.memory.embeddings import get_embedder
    observed = []
    class FakeModel:
        def __init__(self, model, **options):
            observed.append((model, options))
        def get_embedding_dimension(self):
            return 384
        def encode(self, *args, **kwargs):
            raise TypeError("dense-only test model")
    monkeypatch.setitem(sys.modules, "sentence_transformers", SimpleNamespace(SentenceTransformer=FakeModel))
    monkeypatch.setattr("hunter_gatherer.memory.embeddings._st_available", lambda: True)
    embedder = get_embedder(_settings(tmp_path, "semantic"))
    assert embedder.embedding_dimension() == 384
    assert observed == [(SEMANTIC_MODEL, {"local_files_only": True})]


def test_damaged_backup_and_bad_import_do_not_mutate_libraries(tmp_path):
    target = tmp_path / "library"
    create_library(target)
    before = (target / "library.db").read_bytes()
    with pytest.raises(ValueError, match="does not exist"):
        import_files(target, [tmp_path / "missing.md"])
    assert (target / "library.db").read_bytes() == before
    bundle = tmp_path / "backup"
    create_backup(target / "library.db", bundle)
    (bundle / "memory.db").write_bytes(b"corrupted")
    destination = tmp_path / "restore"
    with pytest.raises(ValueError):
        create_library(destination, from_backup=bundle)
    assert not destination.exists()
    assert (target / "library.db").read_bytes() == before


def test_cli_onboarding_and_two_server_restarts_do_not_use_ambient_store(tmp_path, monkeypatch, capsys):
    target = tmp_path / "cli library"
    monkeypatch.setattr(cli, "get_settings", lambda: pytest.fail("ambient configuration"))
    assert cli.main(["init", str(target), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["search_mode"] == "keyword"
    source = tmp_path / "survey.txt"
    source.write_text("The lunar survey begins at 09:00 on Monday.", encoding="utf-8")
    assert cli.main(["import", str(source), "--library", str(target), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["counts"]["succeeded"] == 1
    assert cli.main(["ask", "When does the lunar survey begin?", "--library", str(target), "--json"]) == 0
    answer = json.loads(capsys.readouterr().out)
    passage = answer["pages"][0]["supporting_passages"][0]
    assert "09:00 on Monday" in passage["quote"]
    assert cli.main(["open", passage["citation"], "--library", str(target), "--json"]) == 0
    assert "09:00 on Monday" in capsys.readouterr().out
    bundle = tmp_path / "cli backup"
    assert cli.main(["backup", "create", str(bundle), "--library", str(target)]) == 0
    assert json.loads(capsys.readouterr().out)["ok"]
    restored = tmp_path / "cli restored"
    assert cli.main(["init", str(restored), "--from-backup", str(bundle), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["search_mode"] == "keyword"
    assert cli.main(["open", passage["citation"], "--library", str(restored), "--json"]) == 0
    assert "09:00 on Monday" in capsys.readouterr().out
    owners = []
    # Windows creates the event loop's local wake-up socket before network is
    # denied, matching the packaged-demo tests on every supported platform.
    with asyncio.Runner() as runner:
        runner.get_loop()
        monkeypatch.setattr(socket.socket, "connect", lambda *a, **k: pytest.fail("network access"))
        def inspect_app(app, **options):
            owner = app.state.library_harness
            assert owner.settings.sqlite_path == str(target / "library.db")
            assert options == {"host": "127.0.0.1", "port": 8767, "proxy_headers": False}
            from hunter_gatherer.mcp.server import build_server
            from hunter_gatherer.mcp.profiles import LIBRARY_TOOL_NAMES
            server, _ = build_server(harness=owner, tool_profile="library")
            assert {tool.name for tool in runner.run(server.list_tools())} == set(LIBRARY_TOOL_NAMES)
            response = runner.run(server.call_tool("hg_find", {"query": "lunar survey begin"}))
            assert not response.is_error and "09:00" in str(response.structured_content)
            owners.append(owner)
        monkeypatch.setattr("uvicorn.run", inspect_app)
        assert cli.main(["serve", str(target)]) == 0
        assert cli.main(["serve", str(target)]) == 0
    assert len(owners) == 2
    for owner in owners:
        with pytest.raises(sqlite3.ProgrammingError, match="closed"):
            owner.store._sqlite.execute("SELECT 1")
