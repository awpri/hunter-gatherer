"""Legacy catalog privacy must agree across navigation and saved evidence."""
import json

import pytest

from proharness.coordination import CoordinationError, CoordinationStore
from proharness.memory.collection_compact import expand_collection_export
from proharness.memory.collection_references import get_collection_references
from proharness.memory.document_originals import load_document_original
from proharness.memory.document_visibility import document_reference_states
from proharness.memory.documents import DocumentArchive
from proharness.memory.library_collections import (
    export_collection, get_collection, list_collections, save_collection,
)
from proharness.memory.reference_fetch import fetch_references
from proharness.memory.schemas import Memory
from proharness.server.routers.library_sources import CollectionInput


def ingest(archive, path, text):
    path.write_text(text)
    identity = archive.ingest(str(path))["document_id"]
    hit = archive.get_chunk(archive._chunk_rows(False, document_ids=[identity])[0]["id"])
    return identity, hit


def save(store, hit):
    ref = {"document_id": hit["document_id"], "chunk_id": hit["chunk_id"]}
    return save_collection(store, CollectionInput(name="Legacy garden collection",
        purpose="Dependent garden purpose", references=[ref], voice_examples=[ref],
        notes="Dependent garden notes", idempotency_key="save"))


def exports(store, saved, include_sensitive=False):
    for format, response_format in (("json", "full"), ("json", "compact"), ("markdown", "full")):
        result = export_collection(store, saved["id"], revision=1, format=format,
            response_format=response_format, max_tokens=4096, include_sensitive=include_sensitive)
        yield expand_collection_export(result) if response_format == "compact" else result


@pytest.mark.parametrize("transitive", [False, True])
@pytest.mark.parametrize("tombstone", [False, True])
def test_legacy_source_hash_closure_redacts_all_collection_paths(store, tmp_path, transitive, tombstone):
    archive = DocumentArchive(store)
    a, hit = ingest(archive, tmp_path / "a.md", "Legacy garden exact original wording.")
    b, _ = ingest(archive, tmp_path / "b.md", hit["quote"])
    private = b
    if transitive:
        ingest(archive, tmp_path / "b.md", "Legacy garden bridge edition.")
        private, _ = ingest(archive, tmp_path / "c.md", "Legacy garden bridge edition.")
    saved = save(store, hit)
    decoy, _ = ingest(archive, tmp_path / "decoy.md", "Unrelated public garden remains visible.")
    # Reproduce older catalogs where ingestion did not propagate private flags.
    store._sqlite.execute("UPDATE archive_documents SET sensitive=1,deleted_at=? WHERE id=?",
                          (42 if tombstone else None, private))
    store._sqlite.commit()
    assert load_document_original(store, a)["error_code"] == "document_unavailable"
    states = document_reference_states(store._sqlite, document_ids=[a, decoy])
    assert states[a] == {"available": True, "sensitive": True}
    assert states[decoy] == {"available": True, "sensitive": False}
    got = get_collection(store, saved["id"], revision=1)
    assert got["redacted"] is True
    listed = list_collections(store)
    assert listed["collections"] == []
    recovered = get_collection_references(store, saved["id"], revision=1)
    assert recovered["redacted"] is True and recovered["references"] == []
    fetched = fetch_references(store, [hit["citation"]])
    assert fetched["statuses"] == ["unavailable"] and fetched["sources"] == []
    results = [got, listed, recovered, fetched, *exports(store, saved)]
    for result in results:
        serialized = json.dumps(result)
        for forbidden in (hit["quote"], a, hit["chunk_id"], "Dependent garden", "Legacy garden collection"):
            assert forbidden not in serialized
    assert "error" in archive.get_document(a)
    assert "error" in archive.get_chunk(hit["chunk_id"])
    assert a not in [item["document_id"] for item in archive.list_documents()["documents"]]
    assert decoy in [item["document_id"] for item in archive.list_documents()["documents"]]
    for search in (archive.search_legacy, archive.search_indexed):
        assert a not in [item["document_id"] for item in search("garden")["hits"]]

    # Opt-in restores a valid selected original, even if a different copied
    # source is a sensitive tombstone. It never changes the original identity.
    assert get_collection(store, saved["id"], include_sensitive=True)["references"][0]["document_id"] == a
    assert len(list_collections(store, include_sensitive=True)["collections"]) == 1
    assert get_collection_references(store, saved["id"], revision=1, include_sensitive=True)["total_references"] == 2
    assert fetch_references(store, [hit["citation"]], include_sensitive=True)["sources"][0]["quote"] == hit["quote"]
    for result in exports(store, saved, True):
        assert hit["quote"] in json.dumps(result)
    assert load_document_original(store, a, include_sensitive=True)["document_id"] == a
    assert archive.get_chunk(hit["chunk_id"], include_sensitive=True)["quote"] == hit["quote"]
    assert archive.get_document(a, include_sensitive=True)["document_id"] == a
    # New publication also cannot bypass the nonpropagated sensitivity.
    with pytest.raises(CoordinationError, match="reference_unavailable"):
        save_collection(store, CollectionInput(name="Denied", references=[{
            "document_id": a, "chunk_id": hit["chunk_id"]}], idempotency_key="denied"))


@pytest.mark.parametrize("deleted", ["selected", "latest"])
@pytest.mark.parametrize("include_sensitive", [False, True])
def test_deleted_selected_or_latest_family_never_revives_historical_evidence(store, tmp_path, deleted, include_sensitive):
    archive = DocumentArchive(store)
    source = tmp_path / "history.md"
    old, hit = ingest(archive, source, "Historical garden exact quotation.")
    saved = save(store, hit)
    latest, _ = ingest(archive, source, "New public garden edition.")
    # Visible historical citations remain usable with a newer public edition.
    assert archive.get_chunk(hit["chunk_id"])["is_latest"] is False
    assert fetch_references(store, [hit["citation"]])["statuses"] == ["available"]
    assert get_collection_references(store, saved["id"], revision=1)["total_references"] == 2
    for result in exports(store, saved):
        assert hit["quote"] in json.dumps(result)
    # Simulate a legacy partial tombstone instead of forget(), which deletes
    # every family edition and would conceal the latest-family failure.
    store._sqlite.execute("UPDATE archive_documents SET deleted_at=42 WHERE id=?",
                          (old if deleted == "selected" else latest,))
    store._sqlite.commit()
    assert get_collection(store, saved["id"], include_sensitive=include_sensitive)["redacted"] is True
    assert list_collections(store, include_sensitive=include_sensitive)["collections"] == []
    assert get_collection_references(store, saved["id"], revision=1,
        include_sensitive=include_sensitive)["redacted"] is True
    assert fetch_references(store, [hit["citation"]], include_sensitive=include_sensitive)["statuses"] == ["unavailable"]
    assert "error" in archive.get_chunk(hit["chunk_id"], include_sensitive=include_sensitive)
    assert "error" in archive.get_document(old, include_sensitive=include_sensitive)
    for result in exports(store, saved, include_sensitive):
        assert hit["quote"] not in json.dumps(result) and "Dependent garden" not in json.dumps(result)


def test_coordination_batches_direct_chunk_privacy_once(store, tmp_path, monkeypatch):
    archive = DocumentArchive(store)
    _, first = ingest(archive, tmp_path / "a.md", "First garden source.")
    _, second = ingest(archive, tmp_path / "b.md", "Second garden source.")
    import proharness.memory.document_visibility as visibility
    calls = []
    actual = visibility.document_reference_states
    monkeypatch.setattr(visibility, "document_reference_states",
        lambda *args, **kwargs: calls.append(kwargs) or actual(*args, **kwargs))
    coordinator = CoordinationStore(store)
    workspace = coordinator.workspace_create(actor="test", name="Batch", idempotency_key="batch")["workspace"]["id"]
    refs = [{"document_id": hit["document_id"], "chunk_id": hit["chunk_id"]} for hit in (first, second)]
    assert coordinator._reference_state(workspace, refs * 20) == (True, False)
    assert len(calls) == 1
    assert set(calls[0]["chunk_ids"]) == {first["chunk_id"], second["chunk_id"]}
    assert set(document_reference_states(store._sqlite, chunk_ids=[first["chunk_id"], second["chunk_id"]])) == {
        first["document_id"], second["document_id"]}


@pytest.mark.parametrize("document_key", [False, True])
def test_legacy_privacy_redacts_collection_of_memory_derived_from_copied_chunk(store, tmp_path, document_key):
    archive = DocumentArchive(store)
    _, hit = ingest(archive, tmp_path / "a.md", "Garden original used by a derived memory.")
    copied, _ = ingest(archive, tmp_path / "b.md", hit["quote"])
    source = {"chunk_id": hit["chunk_id"], "quote": hit["quote"]}
    if document_key:
        source["document_id"] = hit["document_id"]
    memory = store.add(Memory(text="Derived garden observation", metadata={"evidence_refs": [source]}))
    saved = save_collection(store, CollectionInput(name="Derived collection",
        references=[{"memory_id": memory.id}], notes="Dependent derived notes", idempotency_key="derived"))
    store._sqlite.execute("UPDATE archive_documents SET sensitive=1,deleted_at=42 WHERE id=?", (copied,))
    store._sqlite.commit()
    assert get_collection(store, saved["id"])["redacted"] is True
    assert list_collections(store)["collections"] == []
    assert get_collection_references(store, saved["id"], revision=1)["redacted"] is True
    assert fetch_references(store, [f"[mem:{memory.id}]"])["statuses"] == ["unavailable"]
    assert get_collection(store, saved["id"], include_sensitive=True)["references"] == [{"memory_id": memory.id}]
    for result in exports(store, saved):
        assert memory.text not in json.dumps(result) and "Dependent derived notes" not in json.dumps(result)
    for result in exports(store, saved, True):
        assert memory.text in json.dumps(result)


@pytest.mark.parametrize("initial_private", [False, True])
def test_scoped_memory_private_writes_and_live_retry_never_expose_collection_prose(store, initial_private):
    coordinator = CoordinationStore(store)
    scope = coordinator.workspace_create(actor="test", name="Memory scope",
        sensitive=initial_private, include_sensitive=initial_private, idempotency_key="scope")["workspace"]
    memory = store.add(Memory(text="Scoped garden observation", metadata={"scope": scope["id"]}))
    fields = {"name": "Scoped collection", "purpose": "Scoped dependent purpose",
              "references": [{"memory_id": memory.id}], "notes": "Scoped dependent notes"}
    if initial_private:
        with pytest.raises(CoordinationError, match="reference_unavailable"):
            save_collection(store, CollectionInput(**fields, idempotency_key="denied-private"))
    original_body = CollectionInput(**fields, include_sensitive=initial_private, idempotency_key="scoped")
    saved = save_collection(store, original_body)
    assert saved["sensitive"] is initial_private
    if not initial_private:
        coordinator.workspace_update(workspace_id=scope["id"], actor="test",
            expected_revision=scope["revision"], sensitive=True, include_sensitive=True,
            idempotency_key="tighten")
    # An exact receipt retry rechecks current scope privacy instead of returning
    # previously public actor notes. Explicit opt-in permits the same revision.
    retried = save_collection(store, original_body)
    assert bool(retried.get("redacted")) is (not initial_private)
    with pytest.raises(CoordinationError, match="reference_unavailable"):
        save_collection(store, CollectionInput(**fields, idempotency_key="new-private"))
    got = get_collection(store, saved["id"])
    assert got["redacted"] is True
    assert list_collections(store)["collections"] == []
    recovered = get_collection_references(store, saved["id"], revision=1)
    assert recovered["redacted"] is True
    fetched = fetch_references(store, [f"[mem:{memory.id}]"])
    assert fetched["statuses"] == ["unavailable"]
    for result in (got, recovered, fetched, *exports(store, saved)):
        for forbidden in (memory.text, "Scoped collection", "Scoped dependent"):
            assert forbidden not in json.dumps(result)
    assert get_collection(store, saved["id"], include_sensitive=True)["notes"] == fields["notes"]
    assert len(list_collections(store, include_sensitive=True)["collections"]) == 1
    assert get_collection_references(store, saved["id"], revision=1, include_sensitive=True)["total_references"] == 1
    for result in exports(store, saved, True):
        assert memory.text in json.dumps(result) and fields["notes"] in json.dumps(result)


@pytest.mark.parametrize("malformation", ["invalid_type", "empty_label", "invalid_json", "missing_parent"])
def test_malformed_scope_remains_unavailable_even_with_opt_in(store, malformation):
    coordinator = CoordinationStore(store)
    scope = coordinator.workspace_create(actor="test", name="Scope", idempotency_key="scope")["workspace"]
    memory = store.add(Memory(text="Scoped malformed source", metadata={"scope": scope["id"]}))
    saved = save_collection(store, CollectionInput(name="Scoped collection",
        references=[{"memory_id": memory.id}], notes="Scoped dependent notes", idempotency_key="save"))
    if malformation in ("invalid_type", "empty_label"):
        memory.metadata["scope"] = [] if malformation == "invalid_type" else ""
        store.update(memory)
    elif malformation == "invalid_json":
        store._sqlite.execute("UPDATE coordination_objects SET data='not-json' WHERE id=?", (scope["id"],))
        store._sqlite.commit()
    else:
        row = store._sqlite.execute("SELECT data FROM coordination_objects WHERE id=?", (scope["id"],)).fetchone()
        data = json.loads(row[0])
        data["parent_workspace_id"] = "missing-required-parent"
        store._sqlite.execute("UPDATE coordination_objects SET data=? WHERE id=?", (json.dumps(data), scope["id"]))
        store._sqlite.commit()
    for include_sensitive in (False, True):
        assert get_collection(store, saved["id"], include_sensitive=include_sensitive)["redacted"] is True
        assert list_collections(store, include_sensitive=include_sensitive)["collections"] == []
        assert get_collection_references(store, saved["id"], revision=1,
            include_sensitive=include_sensitive)["redacted"] is True
        for result in exports(store, saved, include_sensitive):
            assert memory.text not in json.dumps(result) and "Scoped dependent notes" not in json.dumps(result)
