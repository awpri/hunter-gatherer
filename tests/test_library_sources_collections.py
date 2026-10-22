"""HTTP source/collection citations, immutable revisions and privacy contracts."""
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


def ingest(store, tmp_path, text="I built the garden.", sensitive=False):
    source = tmp_path / "original.md"
    source.write_text(text)
    archive = DocumentArchive(store)
    result = archive.ingest(str(source), sensitive=sensitive)
    hit = archive.search("garden", include_sensitive=sensitive)["hits"][0]
    return source, result["document_id"], hit


def create(client, refs, **extra):
    body = {"name": "Garden evidence", "purpose": "Application draft", "references": refs,
            "idempotency_key": "create-garden", **extra}
    return client.post("/api/library/collections", json=body)


def test_evidence_requirements_http_parity_and_bounds(client, store):
    store.add(Memory(text="I built a garden."))
    response = client.post("/api/library/evidence", json={"query": "garden", "evidence_requirements": ["Who built the garden?", "What water savings were measured?"]})
    assert response.status_code == 200, response.text
    assert len(response.json()["requirement_checks"]) == 2
    assert client.post("/api/library/evidence", json={"query": "garden", "evidence_requirements": ["q"] * 5}).status_code == 422


def test_reader_exact_original_history_window_and_private_versions(client, store, tmp_path):
    source, identity, hit = ingest(store, tmp_path)
    response = client.get(f"/api/library/documents/{identity}/original")
    assert response.status_code == 200 and response.content == source.read_bytes()
    assert response.headers["cache-control"] == "no-store"
    assert client.get(f"/api/library/documents/{identity}/outline").json()["document_id"] == identity
    window = client.get(f"/api/library/chunks/{hit['chunk_id']}/window").json()
    assert hit["citation"] in str(window)
    source.write_text("The garden is private now.")
    newest = DocumentArchive(store).ingest(str(source), sensitive=True)["document_id"]
    assert client.get(f"/api/library/documents/{identity}/original").status_code == 404
    assert client.get(f"/api/library/documents/{identity}/history").status_code == 404
    versions = client.get(f"/api/library/documents/{identity}/history?include_sensitive=true").json()["versions"]
    assert {row["document_id"] for row in versions} == {identity, newest}
    assert client.get(f"/api/library/documents/{identity}/original?include_sensitive=true").content == b"I built the garden."


def test_collections_reference_only_revisions_export_conflict_and_unavailability(client, store, tmp_path):
    _, identity, hit = ingest(store, tmp_path)
    ref = {"document_id": identity, "chunk_id": hit["chunk_id"]}
    response = create(client, [ref], voice_examples=[ref], unresolved=["No measured outcome recorded."])
    assert response.status_code == 200, response.text
    collection = response.json()
    assert collection["references"] == [ref] and "quote" not in str(collection["references"])
    assert create(client, [ref], voice_examples=[ref], unresolved=["No measured outcome recorded."]).json() == collection
    body = {"name": "Revised garden evidence", "references": [ref], "expected_revision": 1,
            "idempotency_key": "update-garden"}
    revised = client.put(f"/api/library/collections/{collection['id']}", json=body)
    assert revised.status_code == 200, revised.text
    assert revised.json()["revision"] == 2
    assert client.get(f"/api/library/collections/{collection['id']}?revision=1").json()["name"] == "Garden evidence"
    body["idempotency_key"] = "stale-update"
    assert client.put(f"/api/library/collections/{collection['id']}", json=body).status_code == 409
    exported = client.get(f"/api/library/collections/{collection['id']}/export?format=json&revision=1").json()
    assert exported["content"]["evidence"][0]["quote"] == hit["quote"]
    assert exported["content"]["voice_examples"][0]["citation"] == hit["citation"]
    assert exported["unresolved"] == ["No measured outcome recorded."]
    assert exported["budget"]["serialized_bytes"] <= 4096 * 4
    DocumentArchive(store).forget(identity)
    hidden = client.get(f"/api/library/collections/{collection['id']}").json()
    assert hidden["redacted"] and "Garden" not in str(hidden)
    unavailable = client.get(f"/api/library/collections/{collection['id']}/export?include_sensitive=true").json()
    assert unavailable["unresolved"] and "Garden" not in str(unavailable["content"])
    assert client.get("/api/library/collections").json()["collections"] == []


def test_collection_privacy_stale_ref_and_original_tamper(client, store, tmp_path):
    source, identity, hit = ingest(store, tmp_path, sensitive=True)
    ref = {"document_id": identity, "chunk_id": hit["chunk_id"]}
    assert create(client, [ref]).status_code == 404
    created = create(client, [ref], include_sensitive=True).json()
    assert created["sensitive"]
    assert client.get("/api/library/collections").json()["collections"] == []
    assert client.get(f"/api/library/collections/{created['id']}").json()["redacted"]
    assert client.get(f"/api/library/collections/{created['id']}/export?include_sensitive=true").json()["content"]
    source.write_text("A newer garden source.")
    DocumentArchive(store).ingest(str(source), sensitive=True)
    assert create(client, [ref], include_sensitive=True, idempotency_key="stale-source").status_code == 404
    with store._lock:
        path = store._sqlite.execute("SELECT original_path FROM archive_documents WHERE id=?", (identity,)).fetchone()[0]
    from pathlib import Path
    Path(path).write_text("tampered")
    assert client.get(f"/api/library/documents/{identity}/original?include_sensitive=true").status_code == 409


def test_grouped_graph_budget_counts_records_without_hubs(client, store):
    for index in range(8):
        store.add(Memory(text=f"Garden {index}", tags=[f"topic-{index}"]))
    response = client.get("/api/library/graph?mode=grouped&limit=5").json()
    assert len(response["nodes"]) == 5
    assert all(node["type"] != "topic" for node in response["nodes"])
    assert response["counts"] == {"matching_records": 8, "shown_records": 5, "shown_matching_records": 5, "hidden_topics": 8}
    assert response["truncated"]


def test_collection_survives_store_restart_and_read_does_not_initialize(client, store, settings, embedder, tmp_path):
    before = store._sqlite.total_changes
    assert client.get("/api/library/collections").json() == {"collections": [], "next_cursor": None}
    assert store._sqlite.total_changes == before
    _, identity, hit = ingest(store, tmp_path)
    result = create(client, [{"document_id": identity, "chunk_id": hit["chunk_id"]}]).json()
    from hunter_gatherer.memory.library_collections import get_collection, list_collections
    from hunter_gatherer.memory.store import MemoryStore
    other = MemoryStore(settings, embedder=embedder)
    try:
        assert get_collection(other, result["id"])["revision"] == 1
        assert list_collections(other)["collections"][0]["references"] == result["references"]
    finally:
        other.close()


@pytest.mark.parametrize('format', ['json', 'markdown'])
@pytest.mark.parametrize('source_kind', ['plan', 'writing', 'fact'])
def test_export_preserves_authored_labels_and_document_context(client, store, tmp_path, format, source_kind):
    memory = store.add(Memory(text='I coordinated two volunteers.', source='user',
                              metadata={'kind': source_kind, 'category': 'application_draft'},
                              created_at=1234.0))
    path = tmp_path / 'proposal.md'
    path.write_text('I coordinated two volunteers.')
    archive = DocumentArchive(store)
    identity = archive.ingest(str(path), title='A drafted statement', category='application_draft',
                              author='Example author', document_date='2031-02-03')['document_id']
    hit = archive.search('volunteers')['hits'][0]
    created = create(client, [{'memory_id': memory.id}, {'document_id': identity, 'chunk_id': hit['chunk_id']}]).json()
    result = client.get(f"/api/library/collections/{created['id']}/export?format={format}").json()
    assert not result['truncated']
    if format == 'json':
        remembered, document = result['content']['evidence']
        assert remembered['source_kind'] == source_kind
        assert remembered['type'] == 'semantic' and remembered['recorded_at'] == 1234.0
        assert document['title'] == 'A drafted statement'
        assert document['category'] == 'application_draft'
        assert document['author'] == 'Example author' and document['document_date'] == '2031-02-03'
        assert document['quote'] == hit['quote'] and document['location'] == hit['location']
    else:
        text = result['content']
        assert f'"source_kind": "{source_kind}"' in text
        for value in ('A drafted statement', 'application_draft', 'Example author', '2031-02-03'):
            assert value in text
        assert '"location":' in text and '"version":' in text
        assert 'Source metadata (untrusted)' in text
        assert memory.text in text and hit['quote'] in text


@pytest.mark.parametrize('format', ['json', 'markdown'])
def test_export_omits_whole_evidence_item_with_metadata_and_keeps_privacy(client, store, format):
    memory = store.add(Memory(text='Lengthy record. ' * 1000,
                              metadata={'kind': 'plan', 'category': 'unique_source_label'}))
    collection = create(client, [{'memory_id': memory.id}]).json()
    result = client.get(f"/api/library/collections/{collection['id']}/export?format={format}&max_tokens=512").json()
    assert result['truncated'] and result['omitted_references'] == 1
    assert result['budget']['serialized_bytes'] <= 2048
    assert 'Lengthy record' not in str(result['content'])
    assert 'unique_source_label' not in str(result['content'])
    memory.metadata.update(sensitive=True, kind='private_source_label')
    store.update(memory)
    excluded = client.get(f"/api/library/collections/{collection['id']}/export?format={format}").json()
    assert 'private_source_label' not in str(excluded) and 'Lengthy record' not in str(excluded)


def test_export_omits_whole_quotes_and_preserves_historical_coordinates(client, store, tmp_path):
    source, identity, hit = ingest(store, tmp_path, text="garden " * 600)
    ref = {"document_id": identity, "chunk_id": hit["chunk_id"]}
    saved = create(client, [ref]).json()
    exported = client.get(f"/api/library/collections/{saved['id']}/export?format=json&max_tokens=512").json()
    assert exported["truncated"] and exported["omitted_references"] == 1
    assert exported["content"]["evidence"] == []
    assert exported["budget"]["serialized_bytes"] <= 2048
    source.write_text("New garden wording.")
    DocumentArchive(store).ingest(str(source))
    historic = client.get(f"/api/library/collections/{saved['id']}/export?format=json").json()["content"]["evidence"][0]
    assert historic["quote"] == hit["quote"] and historic["citation"] == hit["citation"]
    assert historic["is_latest"] is False


def test_collection_memory_replacement_and_private_source_tightening(client, store, tmp_path):
    memory = store.add(Memory(text="A garden preference."))
    saved = create(client, [{"memory_id": memory.id}]).json()
    store.delete(memory.id)
    assert "A garden preference." not in client.get(f"/api/library/collections/{saved['id']}/export?include_sensitive=true").json()["content"]
    source, identity, hit = ingest(store, tmp_path)
    saved = create(client, [{"document_id": identity, "chunk_id": hit["chunk_id"]}], idempotency_key="public-source").json()
    source.write_text("Private garden replacement.")
    DocumentArchive(store).ingest(str(source), sensitive=True)
    assert client.get(f"/api/library/collections/{saved['id']}?revision=1").json()["redacted"]
    assert client.get(f"/api/library/collections/{saved['id']}/export?include_sensitive=true").json()["content"]


def test_native_reextract_http_creates_version_without_changing_old_quotes(client, store, tmp_path):
    _, identity, hit = ingest(store, tmp_path)
    response = client.post(f"/api/library/documents/{identity}/reextract", json={"backend": "native"})
    assert response.status_code == 200, response.text
    new_id = response.json()["document_id"]
    assert new_id != identity
    old = client.get(f"/api/library/chunks/{hit['chunk_id']}").json()
    assert old["quote"] == hit["quote"] and old["is_latest"] is False
    history = client.get(f"/api/library/documents/{identity}/history").json()
    assert len(history["versions"]) == 2 and all("extraction" in version for version in history["versions"])
    assert client.post(f"/api/library/documents/{identity}/reextract", json={"backend": "unknown"}).status_code == 422


def test_original_rejects_catalog_path_escape(client, store, tmp_path):
    _, identity, _ = ingest(store, tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("garden")
    with store._lock:
        store._sqlite.execute("UPDATE archive_documents SET original_path=? WHERE id=?", (str(outside), identity))
        store._sqlite.commit()
    assert client.get(f"/api/library/documents/{identity}/original").status_code == 409



def test_reextract_http_private_deleted_and_stale_sources(client, store, tmp_path):
    source, identity, _ = ingest(store, tmp_path, sensitive=True)
    assert client.post(f"/api/library/documents/{identity}/reextract", json={"backend": "native"}).status_code == 404
    accepted = client.post(f"/api/library/documents/{identity}/reextract", json={"backend": "native", "include_sensitive": True})
    assert accepted.status_code == 200, accepted.text
    assert client.post(f"/api/library/documents/{identity}/reextract", json={"include_sensitive": True}).status_code == 422
    DocumentArchive(store).forget(accepted.json()["document_id"])
    assert client.post(f"/api/library/documents/{identity}/reextract", json={"include_sensitive": True}).status_code == 404


def test_proposal_progress_and_review_document_filter_are_private(client, store, tmp_path):
    _, identity, _ = ingest(store, tmp_path)
    progress = client.get(f"/api/library/documents/{identity}/proposal-progress")
    assert progress.status_code == 200 and progress.json()["started"] is False
    assert progress.json()["counts"] == {"pending": 0, "approved": 0, "rejected": 0}
    assert client.get(f"/api/library/review?document_id={identity}&status=all").json()["total"] == 0
    DocumentArchive(store).forget(identity)
    assert client.get(f"/api/library/documents/{identity}/proposal-progress").status_code == 404


def test_collection_notes_reviews_exact_dependencies_revisions_and_privacy(client, store, tmp_path):
    _, identity, hit = ingest(store, tmp_path)
    reference = {"document_id": identity, "chunk_id": hit["chunk_id"]}
    review = {"citation": hit["citation"], "status": "needs_context", "note": "Confirm the project dates."}
    saved = create(client, [reference], notes="Draft outline; no outcome number confirmed.", reviews=[review]).json()
    fetched = client.get(f"/api/library/collections/{saved['id']}").json()
    assert fetched["notes"] == saved["notes"] and fetched["reviews"] == [review]
    exported = client.get(f"/api/library/collections/{saved['id']}/export?format=json").json()["content"]
    assert exported["actor_notes"] == saved["notes"] and exported["actor_reviews"] == [review]
    assert "not verified" in exported["review_policy"]
    markdown = client.get(f"/api/library/collections/{saved['id']}/export").json()["content"]
    assert "## User notes" in markdown and "needs_context" in markdown
    response = create(client, [reference], idempotency_key="invalid-review", reviews=[{**review, "citation": "[mem:arbitrary]"}])
    assert response.status_code == 422
    assert create(client, [reference], idempotency_key="duplicate-review", reviews=[review, review]).status_code == 422
    revised = client.put(f"/api/library/collections/{saved['id']}", json={"name": saved["name"], "references": [reference],
        "reviews": [{**review, "status": "reviewed"}], "notes": "Reviewed only the wording.", "expected_revision": 1,
        "idempotency_key": "notes-review-update"})
    assert revised.status_code == 200, revised.text
    assert client.get(f"/api/library/collections/{saved['id']}?revision=1").json()["reviews"] == [review]
    DocumentArchive(store).forget(identity)
    redacted = client.get(f"/api/library/collections/{saved['id']}/export?revision=1&include_sensitive=true").json()
    assert "Unresolved" in redacted["content"] and "Draft outline" not in str(redacted)


def test_collection_publication_rechecks_latest_under_writer_transaction(client, store, tmp_path, monkeypatch):
    source, identity, hit = ingest(store, tmp_path)
    from hunter_gatherer.memory import library_collections
    previous = library_collections._CollectionCoordinationStore.execute

    def supersede_then_publish(self, action, **kwargs):
        if action == "artifact_publish":
            source.write_text("New garden edition.")
            DocumentArchive(store).ingest(str(source))
        return previous(self, action, **kwargs)

    monkeypatch.setattr(library_collections._CollectionCoordinationStore, "execute", supersede_then_publish)
    response = create(client, [{"document_id": identity, "chunk_id": hit["chunk_id"]}])
    assert response.status_code == 404
    assert client.get('/api/library/collections').json()["collections"] == []


def test_collection_exact_retry_after_public_supersession_returns_existing_revision(client, store, tmp_path):
    source, identity, hit = ingest(store, tmp_path)
    reference = {"document_id": identity, "chunk_id": hit["chunk_id"]}
    created = create(client, [reference]).json()
    source.write_text("A newer garden source.")
    DocumentArchive(store).ingest(str(source))
    retried = create(client, [reference])
    assert retried.status_code == 200 and retried.json() == created
    assert len(client.get('/api/library/collections').json()["collections"]) == 1
