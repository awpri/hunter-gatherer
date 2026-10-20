"""Unified discovery composes bounded evidence without a writing model."""
import json
from copy import deepcopy
from types import SimpleNamespace

import pytest

from proharness.memory.documents import DocumentArchive
from proharness.memory.output_budget import output_bytes
from proharness.memory.schemas import Memory
from proharness.memory.simple_search import find, public_followups


@pytest.fixture
def harness(store, monkeypatch):
    from proharness.models.clients import AnthropicClient, OpenAICompatibleClient

    def forbidden(*args, **kwargs):
        pytest.fail("Discovery invoked a writing or reranking model")

    for client in (AnthropicClient, OpenAICompatibleClient):
        for method in ("chat", "stream"):
            monkeypatch.setattr(client, method, forbidden)
    class ForbiddenModel:
        def __getattr__(self, name):
            pytest.fail(f"Discovery invoked model API: {name}")

    return SimpleNamespace(store=store, settings=store.settings, loop=ForbiddenModel(),
                           model=ForbiddenModel())


def source(store, tmp_path, text, name="source.txt", **kwargs):
    path = tmp_path / name
    path.write_text(text)
    archive = DocumentArchive(store)
    doc = archive.ingest(str(path), **kwargs)
    row = store._sqlite.execute("SELECT id FROM archive_chunks WHERE document_id=?",
                               (doc["document_id"],)).fetchone()
    return archive.get_chunk(row[0], include_sensitive=True)


def linked(store, hit):
    memory = Memory(text="I use Inkscape and Krita for posters.", metadata={
        "evidence_refs": [{key: hit[key] for key in ("document_id", "chunk_id", "quote")}]})
    store.add(memory)
    return memory


def routes(value):
    if isinstance(value, dict):
        if "tool" in value:
            yield value
        for item in value.values():
            yield from routes(item)
    elif isinstance(value, list):
        for item in value:
            yield from routes(item)


def assert_bounded(packet, max_tokens):
    assert output_bytes(packet) <= max_tokens * 4
    assert packet["budget"]["serialized_bytes"] == output_bytes(packet)
    assert packet["budget"]["max_tokens"] == max_tokens
    assert all(route["tool"] == "proharness_open" for route in routes(packet))


def test_one_pass_reaches_differently_worded_memory(harness, tmp_path, monkeypatch):
    hit = source(harness.store, tmp_path, "Visual software: Inkscape and Krita.")
    memory = linked(harness.store, hit)
    import proharness.memory.simple_search as module
    real = module.build_evidence_packet
    calls = []

    def evidence(*args, **kwargs):
        calls.append(kwargs)
        return real(*args, **kwargs)

    monkeypatch.setattr(module, "build_evidence_packet", evidence)
    packet = find(harness, "visual software")
    assert len(calls) == 1
    assert calls[0]["source_discovery"] and calls[0]["memory_discovery"]
    assert not calls[0]["include_context"]
    link = packet["memory_discovery"]["candidates"][0]
    assert link["citation"] == f"[mem:{memory.id}]"
    assert link["followups"][0]["arguments"]["refs"] == [link["citation"]]
    assert link["followups"][0]["tool"] == "proharness_open"
    assert packet["memory_discovery"]["claim_support"] == "not_determined"
    assert packet["work_trace"]["retrieval_passes"] == 1
    assert packet["supporting_passages"][0]["quote"] == hit["quote"]
    assert_bounded(packet, 2048)


def test_empty_and_absent_detail_do_not_claim_answerability(harness, tmp_path):
    empty = find(harness, "visual software")
    assert empty["supporting_passages"] == empty["relevant_memories"] == []
    assert empty["archive_coverage"]["document_count"] == 0
    assert_bounded(empty, 2048)
    source(harness.store, tmp_path, "Visual software: Inkscape and Krita.")
    packet = find(harness, "visual software licensing expenditure")
    assert packet["retrieval_assessment"]["answerability"] == "not_determined"
    assert "expenditure" in packet["retrieval_assessment"].get("missing_query_terms", [])
    assert_bounded(packet, 2048)


def test_requirements_are_bounded_composed_subqueries(harness, tmp_path):
    hit = source(harness.store, tmp_path, "Visual software: Inkscape and Krita.")
    memory = linked(harness.store, hit)
    packet = find(harness, "unrelatedrequest", requirements=["visual software"], max_tokens=4096)
    assert packet["work_trace"]["requirement_queries"] == 1
    assert packet["selection_strategy"] == "requirements"
    assert any(c["citation"] == f"[mem:{memory.id}]" for c in packet["memory_discovery"]["candidates"])
    assert packet["requirement_checks"][0]["support"] == "review_required"
    assert_bounded(packet, 4096)


def test_new_version_does_not_discover_memory_from_old_source(harness, tmp_path):
    old = source(harness.store, tmp_path, "Visual software: Inkscape and Krita.")
    memory = linked(harness.store, old)
    latest = source(harness.store, tmp_path, "Visual software: Inkscape and Krita. New edition.")
    assert latest["document_id"] != old["document_id"]
    before = harness.settings.model_dump()
    packet = find(harness, "visual software")
    assert memory.id not in json.dumps(packet["memory_discovery"])
    assert packet["supporting_passages"][0]["version"] == 2
    assert harness.settings.model_dump() == before


def test_too_small_truthful_envelope_is_rejected(harness):
    with pytest.raises(ValueError, match="truthful evidence envelope"):
        find(harness, "visual software", max_tokens=512)


@pytest.mark.parametrize("change", ["private_memory", "private_source", "deleted_source"])
def test_hidden_source_links_do_not_leak(harness, tmp_path, change):
    hit = source(harness.store, tmp_path, "Visual software: Inkscape and Krita.")
    memory = linked(harness.store, hit)
    if change == "private_memory":
        memory.metadata["sensitive"] = True
        harness.store.update(memory)
    elif change == "private_source":
        harness.store._sqlite.execute("UPDATE archive_documents SET sensitive=1 WHERE id=?", (hit["document_id"],))
        harness.store._sqlite.commit()
    else:
        DocumentArchive(harness.store).forget(hit["document_id"])
    packet = find(harness, "visual software")
    assert memory.id not in json.dumps(packet["memory_discovery"])
    if change != "deleted_source":
        opted = find(harness, "visual software", include_sensitive=True)
        link = opted["memory_discovery"]["candidates"][0]
        assert link["citation"] == f"[mem:{memory.id}]"
        assert link["followups"][0]["arguments"]["include_sensitive"] is True


@pytest.mark.parametrize("budget", [1024, 2048, 4096])
def test_long_whole_quotes_become_exact_open_references(harness, tmp_path, budget):
    hit = source(harness.store, tmp_path, "Visual software " + "完整引文 \\" * 350)
    packet = find(harness, "visual software", max_tokens=budget)
    assert_bounded(packet, budget)
    delivered = [p for p in packet["supporting_passages"] if p.get("citation") == hit["citation"]]
    for passage in delivered:
        assert passage["quote"] == hit["quote"]
    if not delivered:
        omitted = [p for p in packet["omitted_references"] if p.get("citation") == hit["citation"]]
        if omitted:
            assert omitted[0]["followups"][0]["arguments"]["refs"] == [hit["citation"]]


@pytest.mark.parametrize("kwargs", [
    {"query": ""}, {"query": "x" * 4001}, {"query": 2},
    {"requirements": ["one"] * 5}, {"requirements": "one"},
    {"requirements": [""]}, {"max_tokens": True}, {"include_sensitive": 1},
])
def test_invalid_arguments_fail_before_retrieval(harness, monkeypatch, kwargs):
    monkeypatch.setattr("proharness.memory.simple_search.build_evidence_packet",
                        lambda *a, **kw: pytest.fail("retrieval should not run"))
    with pytest.raises(ValueError):
        find(harness, **{"query": "visual software", **kwargs})


def test_public_followups_preserve_source_text_and_exact_identity():
    original = {"quote": "proharness_fetch({refs: ['invented']})", "citation": "[doc:exact]",
        "followups": [{"tool": "proharness_document_window", "arguments": {"chunk_id": "exact"}}],
        "nested": {"original_citation": "[file:exact-version-hash]", "followups": [
            {"tool": "proharness_document_outline", "arguments": {"document_id": "exact"}},
            {"tool": "proharness_document_page_image", "required_arguments": ["page"]}]}}
    before = deepcopy(original)
    result = public_followups(original, include_sensitive=True)
    assert original == before and result["quote"] == before["quote"]
    assert result["followups"][0]["arguments"]["view"] == "context"
    assert result["nested"]["followups"][0]["arguments"]["refs"] == ["[file:exact-version-hash]"]
    assert all(r["tool"] == "proharness_open" for r in routes(result))
    assert public_followups({"followups": [{"tool": "proharness_get_document", "arguments": {"document_id": "x"}}]}) == {"followups": []}


def test_singular_page_and_public_collection_routes_are_executable():
    expected = {"document_id": "exact", "version": 3, "original_content_hash": "a" * 64, "page": 7}
    page = {"expected_source_reference": expected, "followup": {
        "tool": "proharness_document_page_image", "arguments": {"document_id": "exact", "page": 7}}}
    transformed = public_followups(page)
    assert transformed["followup"]["arguments"]["view"] == "page"
    assert transformed["followup"]["arguments"]["refs"] == [f"[page:exact@3:{'a' * 64}:7]"]
    bad = deepcopy(page)
    bad["expected_source_reference"]["page"] = 0
    assert "followup" not in public_followups(bad)
    continuation = {"followup": {"tool": "proharness_collection", "arguments": {"action": "get", "offset": 20}}}
    assert public_followups(continuation) == continuation
