"""Quote-first Ask shares exact source checks without creating collections."""
import json
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from proharness.memory.documents import DocumentArchive
from proharness.memory.library_collections import export_cited_context
from proharness.memory.output_budget import output_bytes
from proharness.memory.schemas import Memory
from proharness.memory.simple_search import find
from proharness.server.routers import library


@pytest.fixture
def harness(store):
    return SimpleNamespace(store=store, settings=store.settings)


@pytest.fixture
def client(harness):
    app = FastAPI()
    app.state.library_harness = harness
    app.include_router(library.router, prefix="/api/library")
    with TestClient(app) as connection:
        yield connection


def source(store, tmp_path, text="The bag was blue.", name="bag.txt", **kwargs):
    path = tmp_path / name
    path.write_text(text)
    archive = DocumentArchive(store)
    did = archive.ingest(str(path), **kwargs)["document_id"]
    chunk = store._sqlite.execute("SELECT id FROM archive_chunks WHERE document_id=?", (did,)).fetchone()[0]
    return path, archive.get_chunk(chunk, include_sensitive=True)


def reference(hit):
    return {key: hit[key] for key in ("document_id", "chunk_id")}


def test_copy_exact_sources_without_persisting_an_artifact(client, store, tmp_path):
    _, hit = source(store, tmp_path)
    memory = store.add(Memory(text="I designed the bag.", source="conversation"))
    before = store._sqlite.total_changes
    tables = list(store._sqlite.execute("SELECT name FROM sqlite_master ORDER BY name"))
    response = client.post("/api/library/find/context", json={
        "query": "What did the bag look like?", "requirements": ["Who designed the bag?"],
        "references": [reference(hit), {"memory_id": memory.id}]})
    assert response.status_code == 200, response.text
    result = response.json()
    assert hit["quote"] in result["content"] and hit["citation"] in result["content"]
    assert memory.text in result["content"] and f"[mem:{memory.id}]" in result["content"]
    assert result["collection_id"] is None and result["revision"] is None
    assert "Collection:" not in result["content"]
    assert not result["truncated"] and not result["unresolved"]
    assert store._sqlite.total_changes == before
    assert list(store._sqlite.execute("SELECT name FROM sqlite_master ORDER BY name")) == tables
    assert result["budget"]["serialized_bytes"] == output_bytes(result)


@pytest.mark.parametrize("restriction", ["private", "deleted", "source_private"])
def test_copy_revalidates_whole_selection_and_withholds_dependent_prose(store, tmp_path, restriction):
    path, hit = source(store, tmp_path, "Secret bag blueprints.")
    memory = store.add(Memory(text="Derived bag claim.", metadata={
        "evidence_refs": [{**reference(hit), "quote": hit["quote"]}]}))
    if restriction == "private":
        memory.metadata["sensitive"] = True
        store.update(memory)
    elif restriction == "source_private":
        path.write_text("The latest secret bag design.")
        DocumentArchive(store).ingest(str(path), sensitive=True)
    else:
        DocumentArchive(store).forget(hit["document_id"])
    response = export_cited_context(store, query="Secret question about bag blueprints",
        requirements=["Secret additional question"], references=[reference(hit), {"memory_id": memory.id}])
    assert response["unresolved"] and not response["truncated"]
    assert all(value not in json.dumps(response) for value in (
        "Secret", "Derived bag", memory.id, hit["document_id"], hit["chunk_id"]))
    if restriction != "deleted":
        allowed = export_cited_context(store, query="bag", references=[reference(hit), {"memory_id": memory.id}],
                                       include_sensitive=True)
        assert hit["quote"] in allowed["content"] and memory.text in allowed["content"]


def test_copy_retains_historical_text_and_extraction_warning(store, tmp_path):
    path, hit = source(store, tmp_path)
    store._sqlite.execute("UPDATE archive_documents SET warnings=? WHERE id=?",
                          (json.dumps(["Inspect original for a potentially misread digit"]), hit["document_id"]))
    store._sqlite.commit()
    path.write_text("The bag was red in the new edition.")
    DocumentArchive(store).ingest(str(path))
    result = export_cited_context(store, query="bag", references=[reference(hit)])
    assert hit["quote"] in result["content"] and "bag was red" not in result["content"]
    assert '"is_latest": false' in result["content"]
    assert "potentially misread digit" in result["content"]


def test_copy_empty_archive_and_invalid_inputs_are_bounded(client, store):
    before = store._sqlite.total_changes
    ref = {"document_id": "a" * 32, "chunk_id": "b" * 32}
    unavailable = client.post("/api/library/find/context", json={"query": "secret", "references": [ref]})
    assert unavailable.status_code == 200 and unavailable.json()["unresolved"]
    assert "secret" not in unavailable.json()["content"]
    assert store._sqlite.total_changes == before
    for change in ({"references": [ref] * 51}, {"references": [{**ref, "quote": "invented"}]},
                   {"references": []}, {"include_sensitive": 1}, {"max_tokens": 511},
                   {"requirements": ["q"] * 5}, {"query": " "}, {"unresolved": ["Unsupported gap"]}):
        response = client.post("/api/library/find/context", json={"query": "bag", "references": [ref], **change})
        assert response.status_code == 422, response.text


def test_copy_budget_omits_whole_quotes_and_reports_count(store):
    large = store.add(Memory(text="Long exact source " * 600))
    small = store.add(Memory(text="The bag was blue."))
    result = export_cited_context(store, query="bag", max_tokens=768,
        references=[{"memory_id": large.id}, {"memory_id": small.id}])
    assert result["truncated"] and result["omitted_references"] == 1
    assert small.text in result["content"] and "Long exact source" not in result["content"]
    assert "recovery" not in result  # no nonexistent collection to reopen
    assert output_bytes(result) <= 768 * 4


def test_contextual_additional_question_keeps_subject_without_and_filter(harness):
    bag = harness.store.add(Memory(text="Arden designed the bag illustration."))
    harness.store.add(Memory(text="My role was editor on the unrelated film."))
    plain = find(harness, query="Arden bag illustration", requirements=["What was my role?"], max_tokens=8192)
    scoped = find(harness, query="Arden bag illustration", requirements=["What was my role?"],
                  contextual_requirements=True, max_tokens=8192)
    baseline, check = plain["requirement_checks"][0], scoped["requirement_checks"][0]
    assert baseline["query"] == check["query"] == "What was my role?"
    assert "search_query" not in baseline
    assert check["search_query"] == "Arden bag illustration\nWhat was my role?"
    # The bag quote need not literally contain 'role' to remain a candidate.
    assert f"[mem:{bag.id}]" in check["retrieval_assessment"]["candidate_citations"]
    assert f"[mem:{bag.id}]" not in baseline["retrieval_assessment"]["candidate_citations"]


def test_context_option_is_pinned_through_continuation_and_http(harness, client):
    for index in range(16):
        harness.store.add(Memory(text=f"Arden bag illustration detail {index}: " + "Exact source description. " * 12))
    packet = client.post("/api/library/find", json={"query": "Arden bag illustration",
        "requirements": ["What was my role?"], "contextual_requirements": True, "max_tokens": 4096}).json()
    token = packet["continuation"]["next_cursor"]
    assert token
    resumed = find(harness, cursor=token, max_tokens=4096)
    assert resumed["requirement_checks"][0]["search_query"] == packet["requirement_checks"][0]["search_query"]
    first = {item["citation"] for item in packet["relevant_memories"]}
    assert not first.intersection(item["citation"] for item in resumed["relevant_memories"])
    assert client.post("/api/library/find", json={"cursor": token, "contextual_requirements": False}).status_code == 422
    assert client.post("/api/library/find", json={"query": "bag", "contextual_requirements": "true"}).status_code == 422
