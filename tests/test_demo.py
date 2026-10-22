import json
from pathlib import Path

import pytest

from hunter_gatherer.demo import create_demo, demo_settings
from hunter_gatherer.memory.document_originals import load_document_original
from hunter_gatherer.memory.documents import DocumentArchive
from hunter_gatherer.memory.embeddings import HashingEmbedder
from hunter_gatherer.memory.store import MemoryStore


def test_demo_preserves_versions_corrections_and_unknown_outcome(tmp_path):
    target = tmp_path / "demo with spaces"
    manifest = create_demo(target)
    first, second, review = manifest["documents"]
    assert first["version"] == 1 and second["version"] == 2
    assert second["previous_version_id"] == first["document_id"]
    with MemoryStore(demo_settings(target), embedder=HashingEmbedder()) as store:
        for entry in manifest["documents"]:
            assert load_document_original(store, entry["document_id"])["_original_bytes"] == Path(entry["input"]).read_bytes()
        old = store.get(manifest["memories"]["original_owner"])
        new = store.get(manifest["memories"]["current_owner"])
        assert old.is_superseded and new.metadata["supersedes"] == old.id
        for memory, document in ((old, first), (new, second)):
            ref = memory.metadata["evidence_refs"][0]
            hit = DocumentArchive(store).get_chunk(ref["chunk_id"])
            assert ref["document_id"] == document["document_id"]
            assert ref["quote"] == hit["quote"]
        assert new.metadata["valid_from"] == "2030-04-08"
        hits = DocumentArchive(store).search("completion time measurements")["hits"]
        assert hits
        assert any("No before-and-after" in hit["quote"] for hit in hits)
    assert f"'{target}' --port 8767" in (target / "START-HERE.md").read_text()
    assert len(json.loads((target / "manifest.json").read_text())["questions"]) == 3


def test_demo_refuses_existing_data_and_symlinks(tmp_path):
    target = tmp_path / "occupied"
    target.mkdir()
    marker = target / "real-store.db"
    marker.write_bytes(b"do not touch")
    with pytest.raises(ValueError, match="empty"):
        create_demo(target)
    assert marker.read_bytes() == b"do not touch"
    link = tmp_path / "link"
    link.symlink_to(target, target_is_directory=True)
    with pytest.raises(ValueError, match="symlink"):
        create_demo(link)
    with pytest.raises(ValueError, match="explicit"):
        create_demo("")
    with pytest.raises(ValueError, match="initialized"):
        demo_settings(tmp_path / "missing")


def test_demo_settings_ignore_environment_and_dotenv(tmp_path, monkeypatch):
    monkeypatch.setenv("HG_SQLITE_PATH", str(tmp_path / "real.db"))
    monkeypatch.setenv("HG_VECTOR_BACKEND", "qdrant")
    monkeypatch.setenv("OPENAI_API_KEY", "fictional-test-key")
    (tmp_path / ".env").write_text("HG_EMBED_MODEL=external-model\n")
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("hunter_gatherer.memory.store.get_embedder", lambda *_: pytest.fail("must inject local embedder"))
    target = tmp_path / "demo"
    create_demo(target)
    settings = demo_settings(target)
    assert settings.sqlite_path == str(target / "demo.db")
    assert settings.embed_model == "hashing" and settings.vector_backend == "memory"
    assert settings.local_only and not settings.openai_api_key and not settings.messages_api_key
    assert not (tmp_path / "real.db").exists()
    with pytest.raises(ValueError, match="empty"):
        create_demo(target)
