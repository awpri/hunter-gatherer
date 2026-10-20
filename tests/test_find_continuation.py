import sqlite3
from types import SimpleNamespace

import pytest

from proharness.memory import find_cursor
from proharness.memory.documents import DocumentArchive
from proharness.memory.output_budget import output_bytes
from proharness.memory.schemas import Memory
from proharness.memory.simple_search import find


def harness(store):
    return SimpleNamespace(store=store, settings=store.settings)


def represented(page):
    return {item["citation"] for name in ("relevant_memories", "supporting_passages", "omitted_references")
            for item in page[name] if item.get("citation")}


def pages(store, query, budget=2048, **kwargs):
    page = find(harness(store), query, max_tokens=budget, **kwargs)
    seen_tokens = set()
    for _ in range(100):
        assert output_bytes(page) <= budget * 4
        yield page
        token = page["continuation"]["next_cursor"]
        if token is None:
            return
        assert token not in seen_tokens
        seen_tokens.add(token)
        page = find(harness(store), cursor=token, max_tokens=budget,
                    include_sensitive=kwargs.get("include_sensitive", False))
    pytest.fail("Find continuation did not terminate")


@pytest.mark.parametrize("policy", ["exhaustive", "bounded"])
def test_seven_documents_reachable(store, tmp_path, policy):
    store.settings.archive_search_policy = policy
    archive = DocumentArchive(store)
    ids = set()
    for index in range(7):
        path = tmp_path / f"manual{index}.txt"
        path.write_text(f"Copper sensor specification number {index}. Unique calibration {index}.")
        ids.add(archive.ingest(path, title=f"Copper sensor {index}")["document_id"])
    results = list(pages(store, "copper sensor", budget=4096))
    refs = set().union(*(represented(page) for page in results))
    catalog = {item["document_id"] for page in results for item in page["source_discovery"]["candidates"]}
    assert catalog == ids
    assert all(any(ref.startswith(f"[doc:{identifier}#") for ref in refs) for identifier in ids)
    assert len(results) > 1


def test_more_than_thirty_memories(store):
    expected = {f"[mem:{store.add(Memory(text=f'Copper sensor calibration fact {index}.')).id}]"
                for index in range(37)}
    results = list(pages(store, "copper sensor"))
    delivered = [ref for page in results for ref in represented(page) if ref.startswith("[mem:")]
    assert set(delivered) == expected
    assert len(delivered) == len(expected)


def test_small_budget_long_bodies_do_not_skip_references(store):
    expected = {f"[mem:{store.add(Memory(text='Copper sensor ' + ('calibration detail ' * 300) + str(index))).id}]"
                for index in range(9)}
    results = list(pages(store, "copper sensor", budget=2048))
    assert set().union(*(represented(page) for page in results)) == expected


def test_sensitive_continuation_requires_same_flag(store):
    for index in range(8):
        store.add(Memory(text=f"Copper sensor private fact {index}", metadata={"sensitive": True}))
    page = find(harness(store), "copper sensor", include_sensitive=True, max_tokens=2048)
    token = page["continuation"]["next_cursor"]
    assert token
    with pytest.raises(ValueError, match="restart Find"):
        find(harness(store), cursor=token)
    assert find(harness(store), cursor=token, include_sensitive=True)["include_sensitive"] is True


@pytest.mark.parametrize("mutation", ["delete", "private", "external"])
def test_changes_stale_public_cursor(store, mutation):
    memories = [store.add(Memory(text=f"Copper sensor fact {index}")) for index in range(8)]
    page = find(harness(store), "copper sensor", max_tokens=2048)
    token = page["continuation"]["next_cursor"]
    assert token
    if mutation == "delete":
        store.delete(memories[-1].id)
    elif mutation == "private":
        memory = memories[-1]
        memory.metadata["sensitive"] = True
        store.update(memory)
    else:
        db_file = store._sqlite.execute("PRAGMA database_list").fetchone()[2]
        with sqlite3.connect(db_file) as db:
            db.execute("UPDATE memories SET text=? WHERE id=?", ("Changed", memories[-1].id))
    with pytest.raises(ValueError, match="restart Find"):
        find(harness(store), cursor=token)


def test_requirement_continuation_even_when_base_offset_zero(store, monkeypatch):
    base = [store.add(Memory(text=f"Copper sensor fact {index}")) for index in range(7)]
    required = store.add(Memory(text="Zirconium alternative coating detail."))
    import proharness.memory.simple_search as search
    project = search.bounded_evidence_packet
    first = [True]
    required_ref = f"[mem:{required.id}]"

    def only_requirement_initially(*args, **kwargs):
        result = project(*args, **kwargs)
        if first[0]:
            first[0] = False
            result["relevant_memories"] = [item for item in result["relevant_memories"]
                                           if item["citation"] == required_ref]
            result["omitted_references"] = [item for item in result["omitted_references"]
                                            if item.get("citation") == required_ref]
        return result

    monkeypatch.setattr(search, "bounded_evidence_packet", only_requirement_initially)
    page = find(harness(store), "copper sensor", requirements=["zirconium alternative coating"], max_tokens=4096)
    all_refs = represented(page)
    token = page["continuation"]["next_cursor"]
    assert token
    assert find_cursor.resume(store, token, False)["offsets"]["memories"] == 0
    for _ in range(20):
        page = find(harness(store), cursor=token, max_tokens=2048)
        all_refs |= represented(page)
        token = page["continuation"]["next_cursor"]
        if token is None:
            break
    assert token is None
    assert {f"[mem:{memory.id}]" for memory in base} <= all_refs
    assert f"[mem:{required.id}]" in all_refs


def test_projection_deferred_requirement_extra_remains_reachable(store, monkeypatch):
    import proharness.memory.simple_search as search

    for index in range(7):
        store.add(Memory(text=f"Copper sensor main fact {index}"))
    required = store.add(Memory(text="Zirconium alternative coating detail."))
    required_ref = f"[mem:{required.id}]"
    project = search.bounded_evidence_packet
    first = [True]

    def defer_requirement_initially(*args, **kwargs):
        result = project(*args, **kwargs)
        if first[0]:
            first[0] = False
            result["relevant_memories"] = [item for item in result["relevant_memories"]
                                           if item["citation"] != required_ref]
            result["omitted_references"] = [item for item in result["omitted_references"]
                                            if item.get("citation") != required_ref]
        return result

    monkeypatch.setattr(search, "bounded_evidence_packet", defer_requirement_initially)
    results = list(pages(store, "copper sensor", budget=4096,
                         requirements=["zirconium alternative coating"]))
    assert required_ref in set().union(*(represented(page) for page in results))


def test_requirement_also_in_base_is_represented_once(store):
    required = store.add(Memory(id="00000000-0000-0000-0000-000000000000",
                                text="Copper sensor zirconium calibration."))
    for index in range(18):
        store.add(Memory(text=f"Copper sensor main calibration {index}."))
    results = list(pages(store, "copper sensor", budget=4096,
                         requirements=["zirconium calibration"]))
    required_ref = f"[mem:{required.id}]"
    refs = [ref for page in results for ref in represented(page)]
    assert refs.count(required_ref) == 1
    assert len(refs) == len(set(refs)) == 19
    assert len(results) >= 3


def test_empty_base_oversized_requirement_reports_budget_and_recovers(store, monkeypatch):
    import proharness.memory.simple_search as search

    required = store.add(Memory(text="Zirconium coating " + "technical detail " * 300))
    required_ref = f"[mem:{required.id}]"
    project = search.bounded_evidence_packet

    def no_requirement_representation_at_small_budget(*args, **kwargs):
        result = project(*args, **kwargs)
        if kwargs["max_tokens"] == 2048:
            result["relevant_memories"] = []
            result["omitted_references"] = []
        return result

    monkeypatch.setattr(search, "bounded_evidence_packet", no_requirement_representation_at_small_budget)
    with pytest.raises(ValueError, match="increase max_tokens"):
        find(harness(store), "quasar navigation", requirements=["zirconium coating"], max_tokens=2048)
    results = list(pages(store, "quasar navigation", budget=4096,
                         requirements=["zirconium coating"]))
    assert required_ref in set().union(*(represented(page) for page in results))
    assert results[-1]["continuation"]["next_cursor"] is None


@pytest.mark.parametrize('policy', ['exhaustive', 'bounded'])
def test_frozen_subject_fixture_reaches_all_seven_expected_sources(store, tmp_path, policy):
    import json
    from pathlib import Path

    from proharness.memory.simple_open import open_sources

    fixture = json.loads((Path(__file__).parent / 'fixtures/find_subject_v1.json').read_text())
    question = next(q for q in fixture['questions'] if len(q['relevant_sources']) == 7)
    store.settings.archive_search_policy = policy
    archive = DocumentArchive(store)
    identities = {}
    for source in fixture['sources']:
        path = tmp_path / (source['key'] + '.md')
        path.write_text(source['text'])
        identities[archive.ingest(path, title=source['title'])['document_id']] = source['key']
    for record in fixture['memories']:
        identities[store.add(Memory(text=record['text'])).id] = record['key']
    candidates = []
    for page in pages(store, question['query'], budget=2048):
        candidates.extend(page['supporting_passages'])
        citations = [ref['citation'] for ref in page['omitted_references'] if ref.get('chunk_id')]
        if citations:
            opened = open_sources(harness(store), refs=citations, max_tokens=16384)
            candidates.extend(opened['sources'])
    assert all(any(identities.get(item.get('document_id')) == unit['source']
                   and all(anchor in item.get('quote', '') for anchor in unit['anchors'])
                   for item in candidates) for unit in question['evidence'])
