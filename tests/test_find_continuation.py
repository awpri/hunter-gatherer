import json
import sqlite3
import time
import uuid
from types import SimpleNamespace

import pytest

from hunter_gatherer.memory import find_cursor
from hunter_gatherer.memory.documents import DocumentArchive
from hunter_gatherer.memory.output_budget import output_bytes
from hunter_gatherer.memory.schemas import Memory
from hunter_gatherer.memory.simple_search import find


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


def continue_to_end(store, page, budget=2048, include_sensitive=False, between=None):
    """Follow a search's cursor to its end, calling ``between`` before each page."""
    results = [page]
    for _ in range(100):
        token = results[-1]["continuation"]["next_cursor"]
        if token is None:
            return results
        if between is not None:
            between(results)
        results.append(find(harness(store), cursor=token, max_tokens=budget,
                            include_sensitive=include_sensitive))
        assert output_bytes(results[-1]) <= budget * 4
    pytest.fail("Find continuation did not terminate")


def delivered(results):
    """Every citation represented on any page, repeats included."""
    return [ref for page in results for ref in represented(page)]


def database(store):
    return store._sqlite.execute("PRAGMA database_list").fetchone()[2]


def elsewhere(store, sql, arguments=()):
    """Commit a change through a separate connection, as another process would."""
    with sqlite3.connect(database(store)) as other:
        other.execute(sql, arguments)
    other.close()


def insert_elsewhere(store, text):
    identity = str(uuid.uuid4())
    elsewhere(store, "INSERT INTO memories VALUES (?,?,?,?,?,?,?,?,?,?,?)",
              (identity, text, "semantic", "[]", 5.0, time.time(), 0.0, 0, "other", "{}", None))
    return f"[mem:{identity}]"


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


@pytest.mark.parametrize("mutation", ["forget", "private", "correct",
                                      "forget_elsewhere", "private_elsewhere", "edit_elsewhere"])
def test_changes_between_pages_do_not_end_a_search(store, mutation):
    """An admitted record is rechecked when its page is built; the search goes on.

    Contract change: these writes used to end the cursor ("restart Find").
    """
    memories = [store.add(Memory(text=f"Copper sensor fact {index}")) for index in range(12)]
    first = find(harness(store), "copper sensor", max_tokens=2048)
    target = next(memory for memory in memories if f"[mem:{memory.id}]" not in represented(first))
    ref = f"[mem:{target.id}]"
    if mutation == "forget":
        store.delete(target.id)
    elif mutation == "private":
        target.metadata["sensitive"] = True
        store.update(target)
    elif mutation == "correct":
        correction = store.supersede(target.id, Memory(text="Copper sensor fact, corrected"))
    elif mutation == "forget_elsewhere":
        elsewhere(store, "UPDATE memories SET deleted_at=? WHERE id=?", (time.time(), target.id))
    elif mutation == "private_elsewhere":
        elsewhere(store, "UPDATE memories SET metadata=json_set(metadata,'$.sensitive',json('true')) WHERE id=?",
                  (target.id,))
    else:
        elsewhere(store, "UPDATE memories SET text=? WHERE id=?", ("Copper sensor fact, reworded", target.id))
    results = continue_to_end(store, first)
    refs = delivered(results)
    assert len(refs) == len(set(refs))                                   # nothing repeated
    everything = {f"[mem:{memory.id}]" for memory in memories}
    if mutation == "edit_elsewhere":
        assert set(refs) == everything                                   # nothing skipped
        delivered_text = next(item["text"] for page in results for item in page["relevant_memories"]
                              if item["citation"] == ref)
        assert delivered_text == "Copper sensor fact, reworded"          # as it reads now
        assert not any(page["continuation"].get("withdrawn") for page in results)
    else:
        assert set(refs) == everything - {ref}                          # only the hidden one is missing
        assert sum(page["continuation"].get("withdrawn", 0) for page in results) == 1
    if mutation == "correct":
        # The correction is a new record: it waits for a new search, which says so.
        assert f"[mem:{correction.id}]" not in refs
        assert all(page["continuation"]["added_since_first_page"] for page in results[1:])
        again = set(delivered(list(pages(store, "copper sensor"))))
        assert f"[mem:{correction.id}]" in again and ref not in again


def test_records_added_between_pages_wait_for_a_new_search(store):
    for index in range(12):
        store.add(Memory(text=f"Copper sensor fact {index}"))
    first = find(harness(store), "copper sensor", max_tokens=2048)
    assert "added_since_first_page" not in first["continuation"]
    added = []

    def add_more(results):
        added.append(f"[mem:{store.add(Memory(text='Copper sensor fact, newer')).id}]")
        added.append(insert_elsewhere(store, "Copper sensor fact, from another process"))

    results = continue_to_end(store, first, between=add_more)
    refs = delivered(results)
    assert len(refs) == len(set(refs)) == 12 and not set(added) & set(refs)
    assert all("Not included" in page["continuation"]["added_since_first_page"] for page in results[1:])
    assert set(added) <= set(delivered(list(pages(store, "copper sensor"))))


def test_record_made_sensitive_still_reaches_a_search_that_included_sensitive(store):
    memories = [store.add(Memory(text=f"Copper sensor fact {index}")) for index in range(12)]
    first = find(harness(store), "copper sensor", include_sensitive=True, max_tokens=2048)
    target = next(memory for memory in memories if f"[mem:{memory.id}]" not in represented(first))
    target.metadata["sensitive"] = True
    store.update(target)
    refs = delivered(continue_to_end(store, first, include_sensitive=True))
    assert len(refs) == len(set(refs)) and set(refs) == {f"[mem:{memory.id}]" for memory in memories}


@pytest.mark.parametrize("policy,catalog_index", [("exhaustive", False), ("bounded", False),
                                                  ("exhaustive", True)])
def test_document_changes_between_pages(store, tmp_path, policy, catalog_index):
    """Forgotten, private and superseded sources are withdrawn from passages and leads."""
    store.settings.archive_search_policy = policy
    store.settings.archive_catalog_index = catalog_index
    archive = DocumentArchive(store)
    paths, documents = {}, {}
    for index in range(9):
        paths[index] = tmp_path / f"manual{index}.txt"
        paths[index].write_text(f"Copper sensor specification number {index}. Unique calibration {index}.")
        documents[index] = archive.ingest(paths[index], title=f"Copper sensor {index}")["document_id"]
    first = find(harness(store), "copper sensor", max_tokens=2048)

    def passage_shown(document, results):
        return any(ref.startswith(f"[doc:{document}#") for ref in delivered(results))

    def lead_shown(document, results):
        return [item["document_id"] for page in results
                for item in page["source_discovery"]["candidates"]].count(document)

    later = [index for index in documents if not passage_shown(documents[index], [first])]
    assert len(later) >= 3
    forgotten, private, revised = later[:3]
    archive.forget(documents[forgotten])
    store._sqlite.execute("UPDATE archive_documents SET sensitive=1 WHERE id=?", (documents[private],))
    store._sqlite.commit()
    paths[revised].write_text(f"Copper sensor specification number {revised}. Revised calibration.")
    newer = archive.ingest(paths[revised], title=f"Copper sensor {revised}")["document_id"]
    assert newer != documents[revised]

    results = continue_to_end(store, first)
    refs = delivered(results)
    assert len(refs) == len(set(refs))
    hidden = {documents[forgotten], documents[private], documents[revised], newer}
    for document in documents.values():
        assert passage_shown(document, results) == (document not in hidden)
        # A lead already shown on the first page stays shown; none is shown twice.
        expected = lead_shown(document, [first]) if document in hidden else 1
        assert lead_shown(document, results) == expected
    assert not passage_shown(newer, results) and not lead_shown(newer, results)
    assert sum(page["continuation"].get("withdrawn", 0) for page in results) >= 3
    assert all(page["continuation"]["added_since_first_page"] for page in results[1:])


def test_another_process_writing_between_every_page(store):
    """Additions, forgets and privacy changes elsewhere between every page."""
    memories = [store.add(Memory(text=f"Copper sensor reading {index}")) for index in range(30)]
    first = find(harness(store), "copper sensor", max_tokens=2048)
    hidden, added = set(), set()

    def churn(results):
        shown = set(delivered(results))
        waiting = [memory.id for memory in memories
                   if f"[mem:{memory.id}]" not in shown | hidden]
        added.add(insert_elsewhere(store, "Copper sensor reading from elsewhere, ranked first"))
        if len(waiting) >= 2:
            elsewhere(store, "UPDATE memories SET deleted_at=? WHERE id=?", (time.time(), waiting[0]))
            elsewhere(store, "UPDATE memories SET metadata=json_set(metadata,'$.sensitive',json('true')) "
                      "WHERE id=?", (waiting[-1],))
            hidden.update({f"[mem:{waiting[0]}]", f"[mem:{waiting[-1]}]"})

    results = continue_to_end(store, first, between=churn)
    refs = delivered(results)
    assert len(results) > 3 and hidden
    assert len(refs) == len(set(refs))
    assert set(refs) == {f"[mem:{memory.id}]" for memory in memories} - hidden
    assert not set(refs) & added


def test_more_matches_than_admitted_are_reported(store, monkeypatch):
    monkeypatch.setattr(find_cursor, "ADMITTED", 6)
    for index in range(9):
        store.add(Memory(text=f"Copper sensor fact {index}"))
    results = list(pages(store, "copper sensor"))
    refs = delivered(results)
    assert len(refs) == len(set(refs)) == 6
    assert all(page["continuation"]["admitted_limit_reached"] for page in results)
    assert results[-1]["continuation"]["next_cursor"] is None


def test_cursor_state_keeps_citations_never_text(store, tmp_path):
    for index in range(12):
        store.add(Memory(text=f"Copper sensor zebrafinch note {index}", metadata={"detail": "okapi"}))
    path = tmp_path / "manual.txt"
    path.write_text("Copper sensor narwhal specification.")
    DocumentArchive(store).ingest(path, title="Copper sensor capybara")
    page = find(harness(store), "copper sensor", max_tokens=2048)
    state = find_cursor.resume(store, page["continuation"]["next_cursor"], False)
    saved = json.dumps(state)
    assert not any(word in saved for word in ("zebrafinch", "okapi", "narwhal", "capybara"))
    assert all(citation.startswith("[mem:") for citation in state["admitted"]["memories"])


def cursor_state(**changes):
    state = {"query": "copper", "requirements": [], "include_sensitive": False, "offsets": {},
             "pending": {}, "admitted": {"memories": ["[mem:a]"]}, "limit_reached": [],
             "watermark": [1, 0, 0]}
    return {**state, **changes}


def test_cursor_cache_is_bounded_and_expires(store, monkeypatch):
    clock = [1000.0]
    monkeypatch.setattr(find_cursor.time, "monotonic", lambda: clock[0])
    tokens = [find_cursor.issue(store, cursor_state()) for _ in range(find_cursor._MAX_ENTRIES + 3)]
    assert len(store._find_cursor_cache) == find_cursor._MAX_ENTRIES
    with pytest.raises(ValueError, match="restart Find"):
        find_cursor.resume(store, tokens[0], False)                     # oldest evicted
    clock[0] += 15 * 60 - 1
    assert find_cursor.resume(store, tokens[-1], False)["admitted"] == {"memories": ["[mem:a]"]}
    clock[0] += 1
    with pytest.raises(ValueError, match="restart Find"):
        find_cursor.resume(store, tokens[-1], False)


@pytest.mark.parametrize("changes", [
    {"admitted": {"memories": ["[mem:a]", "[mem:a]"]}},                  # repeated citation
    {"admitted": {"memories": ["[doc:a#b]"]}},                            # wrong kind
    {"admitted": {"memories": [f"[mem:{index}]" for index in range(137)]}},  # over capacity
    {"admitted": {"quotes": ["[mem:a]"]}},
    {"limit_reached": ["memories", "memories"]},
    {"watermark": [1, 2]},
])
def test_cursor_state_is_validated(store, changes):
    with pytest.raises(ValueError, match="Invalid Find continuation state"):
        find_cursor.issue(store, cursor_state(**changes))


def test_requirement_continuation_even_when_base_offset_zero(store, monkeypatch):
    base = [store.add(Memory(text=f"Copper sensor fact {index}")) for index in range(7)]
    required = store.add(Memory(text="Zirconium alternative coating detail."))
    import hunter_gatherer.memory.simple_search as search
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
    # Contract change: the cursor keeps admitted citations, not ranking offsets.
    # None of the base memories was delivered, so all of them remain admitted.
    admitted = find_cursor.resume(store, token, False)["admitted"]["memories"]
    assert {f"[mem:{memory.id}]" for memory in base} <= set(admitted)
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
    import hunter_gatherer.memory.simple_search as search

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
    import hunter_gatherer.memory.simple_search as search

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

    from hunter_gatherer.memory.simple_open import open_sources

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


def test_bookkeeping_between_pages_does_not_end_a_search(store):
    """Saving what was found, or reading it, must not cost the rest of the results."""
    from hunter_gatherer.memory.collection_input import CollectionInput
    from hunter_gatherer.memory.library_collections import save_collection

    memories = [store.add(Memory(text=f"Copper sensor fact {index}")) for index in range(8)]
    page = find(harness(store), "copper sensor", max_tokens=2048)
    token = page["continuation"]["next_cursor"]
    assert token
    save_collection(store, CollectionInput(name="Copper sensors", idempotency_key="keep",
                                           references=[{"memory_id": memories[0].id}]))
    store.touch(memories[1].id)                                  # access counters are not content
    store.set_meta("unrelated_note", "x")
    following = find(harness(store), cursor=token, max_tokens=2048)
    assert represented(following) and not represented(following) & represented(page)
    assert "added_since_first_page" not in following["continuation"]
    # Contract change: new content used to end the cursor. Now it waits for a
    # new search, and the page says so. (A cursor may be used again.)
    added = store.add(Memory(text="Copper sensor fact added later"))
    again = find(harness(store), cursor=token, max_tokens=2048)
    assert represented(again) == represented(following)
    assert f"[mem:{added.id}]" not in represented(again)
    assert again["continuation"]["added_since_first_page"]


def test_a_commit_by_another_process_during_a_search_does_not_fail_it(store, monkeypatch):
    """The page comes from one snapshot; the other writer's record waits for a new search."""
    import hunter_gatherer.memory.simple_search as search

    for index in range(8):
        store.add(Memory(text=f"Copper sensor fact {index}"))
    assert store._sqlite.execute("PRAGMA journal_mode=WAL").fetchone()[0] == "wal"
    db_file = store._sqlite.execute("PRAGMA database_list").fetchone()[2]
    project, written = search.bounded_evidence_packet, []

    def another_process_commits_mid_search(*args, **kwargs):
        if not written:
            with sqlite3.connect(db_file) as other:
                other.execute("INSERT INTO memories VALUES (?,?,?,?,?,?,?,?,?,?,?)", (
                    "external", "Copper sensor fact from elsewhere", "semantic", "[]", 5.0,
                    0.0, 0.0, 0, "other", "{}", None))
            other.close()
            written.append(True)
        return project(*args, **kwargs)

    monkeypatch.setattr(search, "bounded_evidence_packet", another_process_commits_mid_search)
    page = find(harness(store), "copper sensor", max_tokens=2048)
    assert written and "[mem:external]" not in represented(page)     # consistent with its snapshot
    # Contract change: the continuation used to fail as stale. It now completes,
    # without the record committed after its first page began, and says so.
    rest = continue_to_end(store, page)[1:]
    assert rest and "[mem:external]" not in delivered(rest)
    assert all(later["continuation"]["added_since_first_page"] for later in rest)
    assert len(delivered([page, *rest])) == len(set(delivered([page, *rest]))) == 8
    again = find(harness(store), "copper sensor", max_tokens=4096)
    assert any("[mem:external]" in represented(p) for p in [again, *(
        [find(harness(store), cursor=again["continuation"]["next_cursor"], max_tokens=4096)]
        if again["continuation"]["next_cursor"] else [])])
