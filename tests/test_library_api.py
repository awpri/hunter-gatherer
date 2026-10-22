"""Library HTTP lifecycle, privacy and explicit graph integrity contracts."""
from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from hunter_gatherer.memory.documents import DocumentArchive
from hunter_gatherer.memory.schemas import Memory
from hunter_gatherer.server.routers import library


@pytest.fixture
def client(store):
    app = FastAPI()
    app.state.library_harness = SimpleNamespace(store=store, settings=store.settings,
                                              loop=SimpleNamespace(embedder=store.embedder))
    app.include_router(library.router, prefix="/api/library")
    with TestClient(app) as connection:
        yield connection


def _upload(client, text="I organized a neighborhood garden. I plan to add a seed library.", **data):
    response = client.post("/api/library/documents/upload", data=data,
                           files={"file": ("garden.txt", text.encode(), "text/plain")})
    assert response.status_code == 200, response.text
    return response.json()


def _memory(client, text="I organized the garden.", **body):
    response = client.post("/api/library/memories", json={"text": text, **body})
    assert response.status_code == 200, response.text
    return response.json()["id"]


def test_memory_lifecycle_correction_keeps_history_and_pin_filter(client, store):
    identity = _memory(client, kind="plan", category="garden", tags=["garden"])
    assert client.get(f"/api/library/memories/{identity}").json()["kind"] == "plan"
    assert client.post(f"/api/library/memories/{identity}/pin", json={"pinned": True}).json()["pinned"]
    assert client.get("/api/library/memories?pinned=true").json()["total"] == 1
    corrected = client.post(f"/api/library/memories/{identity}/replace",
                            json={"text": "I plan to organize the Zephyr garden."}).json()
    replacement = corrected["memory"]["id"]
    assert replacement != identity
    assert store.get(identity).metadata["superseded_by"] == replacement
    assert store.get(replacement).metadata["supersedes"] == identity
    assert client.get(f"/api/library/memories/{identity}").status_code == 404
    listing = client.get("/api/library/memories?kind=plan&query=Zephyr&category=garden").json()
    assert listing["total"] == 1 and listing["memories"][0]["id"] == replacement
    removed = client.post(f"/api/library/memories/{replacement}/forget", json={}).json()
    assert removed["deletion_mode"] == "recoverable"
    assert store.get(replacement).deleted_at is not None
    assert client.get("/api/library/memories").json()["total"] == 0


def test_private_memory_visibility_and_explicit_mutation_opt_in(client, store):
    public = _memory(client, "A public garden plan.")
    private = _memory(client, "A private unrelated disclosure.", sensitive=True, category="hidden-category")
    overview = client.get("/api/library/overview").json()
    assert overview["counts"]["memories"] == 1
    assert "hidden-category" not in str(overview)
    assert client.get("/api/library/memories").json()["total"] == 1
    assert client.get(f"/api/library/memories/{private}").status_code == 404
    for endpoint, body in (("pin", {"pinned": True}), ("replace", {"text": "Changed private text."}), ("forget", {})):
        assert client.post(f"/api/library/memories/{private}/{endpoint}", json=body).status_code == 404
    assert store.get(private).is_retrievable()
    assert client.post(f"/api/library/memories/{private}/pin",
                       json={"pinned": True, "include_sensitive": True}).status_code == 200
    assert client.get("/api/library/memories?include_sensitive=true").json()["total"] == 2
    rejected = client.post(f"/api/library/memories/{public}/replace", json={"text": "My annual income is private."})
    assert rejected.status_code == 422 and store.get(public).is_retrievable()
    assert client.post("/api/library/memories", json={"text": "My annual income is private."}).status_code == 422


@pytest.mark.parametrize("value", ["true", "false", 1, 0])
def test_body_booleans_are_strict(client, value):
    assert client.post("/api/library/memories", json={"text": "A fact.", "sensitive": value}).status_code == 422
    identity = _memory(client)
    assert client.post(f"/api/library/memories/{identity}/pin", json={"pinned": value}).status_code == 422
    assert client.post(f"/api/library/memories/{identity}/forget", json={"include_sensitive": value}).status_code == 422


def test_evidence_route_forwards_every_selection_field(client, monkeypatch):
    received = {}

    def capture(harness, query, **kwargs):
        received.update(query=query, **kwargs)
        return {"selection_strategy": kwargs["selection_strategy"]}

    monkeypatch.setattr(library.tools, "evidence", capture)
    body = {"query": "Garden design", "top_k": 3, "required_fields": ["dates"],
            "evidence_requirements": ["Design method", "Measured result"],
            "selection_strategy": "requirements", "include_sensitive": True, "response_format": "compact"}
    response = client.post('/api/library/evidence', json=body)
    assert response.status_code == 200, response.text
    assert received == {**body, "source_discovery": False, "enrichment_discovery": False,
                        "include_context": True, "lexical_only": False, "memory_discovery": False}
    assert response.json()['selection_strategy'] == 'requirements'
    response = client.post('/api/library/evidence', json={"query": "Garden"})
    assert response.status_code == 200
    assert received['selection_strategy'] == 'relevance'
    assert received['response_format'] == 'full'


def test_upload_read_pagination_and_explicit_review_only(client, store):
    document = _upload(client, title="Garden", category="service")
    identity = document["document_id"]
    assert Path(document["original_path"]).is_file()
    assert not Path(document["source_path"]).exists()
    assert client.get("/api/library/documents?query=seed").json()["total"] == 1
    assert client.get("/api/library/documents?category=service").json()["total"] == 1
    assert client.get("/api/library/documents?category=writing").json()["total"] == 0
    page = client.get(f"/api/library/documents/{identity}?start=0&max_chars=12").json()
    assert page["next_start"] == 12 and len(page["passages"][0]["text"]) == 12
    proposal = client.post(f"/api/library/documents/{identity}/propose", json={}).json()
    assert store.count() == 0
    candidate = proposal["candidates"][0]
    assert client.get("/api/library/review").json()["total"] >= 1
    decision = client.post(f"/api/library/review/{candidate['candidate_id']}",
                           json={"decision": "approve", "kind": "fact"}).json()
    memory = store.get(decision["memory_id"])
    assert memory.is_permanent and memory.metadata["verification"] == "unverified"
    evidence = client.post("/api/library/evidence", json={"query": "garden"}).json()
    assert evidence["supporting_passages"]
    citation = memory.metadata["evidence_refs"][0]
    assert client.get(f"/api/library/chunks/{citation['chunk_id']}").json()["document_id"] == identity


def test_private_documents_queue_counts_and_health(client, store):
    private = _upload(client, sensitive="true", title="Hidden title", category="hidden")
    identity = private["document_id"]
    assert client.get("/api/library/documents").json()["total"] == 0
    assert client.get(f"/api/library/documents/{identity}").status_code == 404
    assert client.post(f"/api/library/documents/{identity}/propose", json={}).status_code == 404
    proposal = client.post(f"/api/library/documents/{identity}/propose",
                           json={"include_sensitive": True}).json()
    candidate = proposal["candidates"][0]["candidate_id"]
    assert client.get("/api/library/review").json()["total"] == 0
    assert client.get("/api/library/overview").json()["counts"] == {"memories": 0, "documents": 0, "pending": 0}
    assert client.post(f"/api/library/review/{candidate}", json={"decision": "approve"}).status_code == 404
    assert store.count() == 0
    health = client.get("/api/library/health").json()
    assert health["scope"]["documents"] == 0 and "Hidden title" not in str(health)


def test_upload_format_limit_and_temp_cleanup(client, monkeypatch):
    assert client.post("/api/library/documents/upload", files={"file": ("run.py", b"code")}).status_code == 415
    assert client.post("/api/library/documents/upload", files={"file": ("a.txt", b"")}).status_code == 422
    assert client.post("/api/library/documents/upload", data={"sensitive": "yes"},
                       files={"file": ("a.txt", b"text")}).status_code == 422
    monkeypatch.setattr(library, "UPLOAD_LIMIT", 8)
    assert client.post("/api/library/documents/upload", files={"file": ("a.txt", b"123456789")}).status_code == 413
    monkeypatch.setattr(library, "UPLOAD_LIMIT", 25 * 1024 * 1024)
    paths = []

    def reject(harness, path, **kwargs):
        paths.append(path)
        raise ValueError("Invalid document.")

    monkeypatch.setattr(library.tools, "ingest_document", reject)
    response = client.post("/api/library/documents/upload", data={"path": "/etc/passwd"},
                           files={"file": ("../../document.txt", b"test")})
    assert response.status_code == 422
    assert paths and paths[0] != "/etc/passwd" and not Path(paths[0]).exists()


def test_graph_only_explicit_topics_and_valid_current_source_edges(client, store, tmp_path):
    archive = DocumentArchive(store)
    path = tmp_path / "garden.txt"
    path.write_text("I organized the Zephyr garden.")
    doc = archive.ingest(str(path), title="Garden", category="garden")
    chunk = archive.search("garden")["hits"][0]
    other = _upload(client, "An unrelated source.", title="Other")
    correct = store.add(Memory(text="Garden organizer.", tags=["garden"], metadata={"category": "garden",
                               "evidence_refs": [{"document_id": doc["document_id"], "chunk_id": chunk["chunk_id"],
                                                  "quote": "Zephyr garden"}]}))
    wrong = store.add(Memory(text="Garden invented citation.", metadata={"evidence_refs": [
        {"document_id": other["document_id"], "chunk_id": chunk["chunk_id"]}]}))
    invented = store.add(Memory(text="Garden invented quote.", metadata={"evidence_refs": [
        {"document_id": doc["document_id"], "chunk_id": chunk["chunk_id"], "quote": "not actually in source"}]}))
    isolated = store.add(Memory(text="An isolated note."))
    graph = client.get("/api/library/graph").json()
    links = [edge for edge in graph["edges"] if edge["kind"] == "source"]
    assert len(links) == 1 and links[0]["source"] == f"memory:{correct.id}"
    assert links[0]["label"] == "Cites source" and links[0]["citation"] == chunk["citation"]
    topic_ids = {node["id"] for node in graph["nodes"] if node["type"] == "topic"}
    assert {"topic:tag:garden", "topic:category:garden"} <= topic_ids
    assert f"memory:{isolated.id}" in {node["id"] for node in graph["nodes"]}
    assert not any(edge["source"] in {f"memory:{wrong.id}", f"memory:{invented.id}", f"memory:{isolated.id}"}
                   for edge in graph["edges"])
    path.write_text("A corrected garden source.")
    archive.ingest(str(path), title="Garden revision", category="garden")
    historical = client.get("/api/library/graph").json()
    assert not any(edge["kind"] == "source" for edge in historical["edges"])
    assert f"document:{doc['document_id']}" not in {node["id"] for node in historical["nodes"]}


def test_graph_privacy_uses_true_chunk_parent_even_for_wrong_refs_and_deleted_sources(client, store):
    private = _upload(client, "A private garden disclosure.", sensitive="true", title="Hidden title")
    public = _upload(client, "A public garden source.")
    archive = DocumentArchive(store)
    chunk = archive.search("disclosure", include_sensitive=True)["hits"][0]
    linked = store.add(Memory(text="Copied secret.", metadata={"evidence_refs": [{
        "document_id": public["document_id"], "chunk_id": chunk["chunk_id"]}]}))
    for forgotten in (False, True):
        if forgotten:
            archive.forget(private["document_id"])
        graph = client.get("/api/library/graph").json()
        assert f"memory:{linked.id}" not in {node["id"] for node in graph["nodes"]}
        assert "Hidden title" not in str(graph) and "Copied secret" not in str(graph)
        assert client.get("/api/library/overview").json()["counts"]["memories"] == 0


def test_graph_bounds_determinism_filters_and_read_only(client, store):
    for index in range(12):
        store.add(Memory(text=f"Garden note {index}.", tags=[f"tag-{index}"], metadata={"category": "garden"}))
    DocumentArchive(store)
    before = store._sqlite.total_changes
    first = client.get("/api/library/graph?limit=5&category=garden&query=Garden").json()
    second = client.get("/api/library/graph?limit=5&category=garden&query=Garden").json()
    assert first == second and len(first["nodes"]) <= 5 and first["truncated"]
    ids = {node["id"] for node in first["nodes"]}
    assert all(edge["source"] in ids and edge["target"] in ids for edge in first["edges"])
    assert first["scope"]["memories"] == 12
    assert client.get("/api/library/graph?query=nonexistent").json()["nodes"] == []
    assert client.get("/api/library/graph?limit=501").status_code == 422
    assert client.get("/api/library/memories?limit=0").status_code == 422
    assert client.get("/api/library/memories?kind=unknown").status_code == 422
    assert client.get("/api/library/overview").status_code == 200
    assert client.get("/api/library/memories").status_code == 200
    assert client.get("/api/library/documents").status_code == 200
    assert store._sqlite.total_changes == before


def test_archive_constructor_preserves_read_snapshot_and_checks_missing_index(store):
    from hunter_gatherer.memory.library_health import _read_snapshot

    DocumentArchive(store)
    with _read_snapshot(store):
        assert store._sqlite.in_transaction
        DocumentArchive(store)
        assert store._sqlite.in_transaction
    assert not store._sqlite.in_transaction
    store._sqlite.execute("DROP INDEX idx_archive_hash")
    store._sqlite.commit()
    DocumentArchive(store)
    assert store._sqlite.execute("SELECT 1 FROM sqlite_master WHERE name='idx_archive_hash'").fetchone()


def test_archive_constructor_does_not_commit_callers_unrelated_write(store):
    DocumentArchive(store)
    store._sqlite.execute("INSERT INTO store_meta(key,value) VALUES ('library-test-uncommitted','pending')")
    assert store._sqlite.in_transaction
    DocumentArchive(store)
    assert store._sqlite.in_transaction
    store._sqlite.rollback()
    assert store.get_meta("library-test-uncommitted") is None


def test_large_graph_reserves_topic_and_source_nodes(client, store):
    document = _upload(client, "A garden source.", title="Garden source", category="garden")
    archive = DocumentArchive(store)
    chunk = archive.search("garden")["hits"][0]
    for index in range(160):
        store.add(Memory(text=f"A garden record {index}.", tags=["garden"],
                         metadata={"category": "garden", "evidence_refs": [{
                             "document_id": document["document_id"], "chunk_id": chunk["chunk_id"]}]}))
    graph = client.get("/api/library/graph?limit=150&query=record").json()
    assert len(graph["nodes"]) <= 150 and graph["truncated"]
    assert any(node["type"] == "topic" for node in graph["nodes"])
    assert any(node["type"] == "document" for node in graph["nodes"])
    assert {"source", "topic"} <= {edge["kind"] for edge in graph["edges"]}


def test_get_memory_serializes_one_snapshot_across_external_privacy_change(client, store, monkeypatch):
    import json
    import sqlite3

    identity = _memory(client, "A formerly public note.")
    store._sqlite.execute("PRAGMA journal_mode=WAL")
    DocumentArchive(store)
    original_get = store.get
    changed = False

    def get_and_change(memory_id):
        nonlocal changed
        memory = original_get(memory_id)
        if memory_id == identity and not changed:
            changed = True
            with sqlite3.connect(store.settings.sqlite_path) as writer:
                writer.execute("UPDATE memories SET text=?,metadata=? WHERE id=?",
                               ("NEW PRIVATE TEXT", json.dumps({"sensitive": True}), identity))
        return memory

    monkeypatch.setattr(store, "get", get_and_change)
    response = client.get(f"/api/library/memories/{identity}")
    assert response.status_code == 200 and response.json()["text"] == "A formerly public note."
    assert "NEW PRIVATE TEXT" not in response.text
    assert client.get(f"/api/library/memories/{identity}").status_code == 404
