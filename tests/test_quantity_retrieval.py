"""Question wording changes ranking, never source text or visibility."""
from types import SimpleNamespace

import pytest

from hunter_gatherer.config import Settings
from hunter_gatherer.library import create_library, library_settings
from hunter_gatherer.memory.documents import DocumentArchive
from hunter_gatherer.memory.lexical import TOKENIZER_VERSION, normalize_word
from hunter_gatherer.memory.query_focus import question_terms
from hunter_gatherer.memory.simple_search import find


def ingest(store, tmp_path, name, text, **kwargs):
    path = tmp_path / name
    path.write_bytes(text.encode("utf-8"))
    archive = DocumentArchive(store)
    document = archive.ingest(str(path), **kwargs)
    chunk = store._sqlite.execute("SELECT id FROM archive_chunks WHERE document_id=?",
                                  (document["document_id"],)).fetchone()[0]
    return archive.get_chunk(chunk, include_sensitive=True)


@pytest.mark.parametrize("word,expected", [
    ("items", "item"), ("books", "book"), ("boxes", "box"),
    ("entries", "entry"), ("proposals", "proposal"), ("cafés", "cafe"),
    ("gas", "gas"), ("status", "status"), ("glass", "glass"),
    ("series", "series"), ("species", "species"), ("ms", "ms"),
    ("BIOS", "bios"), ("HTTPS", "https"), ("AX-12", "ax-12"),
    ("AX-120", "ax-120"), ("H2S", "h2s"),
])
def test_normalization_is_conservative(word, expected):
    assert normalize_word(word) == expected


def test_quantity_framing_is_not_a_required_source_word():
    assert question_terms("how many items") == {"item"}
    assert "many" in question_terms("many worlds interpretation")
    assert question_terms("repair cafe") == question_terms("repair café")
    assert question_terms("How many items does each guest bring?") == {"item", "each", "guest", "bring"}


@pytest.mark.parametrize("policy", ["exhaustive", "bounded"])
def test_quantity_question_finds_exact_one_item_quote_first(store, tmp_path, policy):
    store.settings.archive_search_policy = policy
    store.settings.library_find_relevance_policy = "coverage"
    expected = ingest(store, tmp_path, "opening-hours.md",
        "# Repair café — opening hours\n\n"
        "The repair café opens every Saturday from 09:00 to 13:00. "
        "Last intake is at 12:30. Bring one small household item and any parts you already have.\n\n"
        "Volunteer check-in starts at 08:30. The north entrance has step-free access. "
        "Repairs are free; replacement parts are paid for by the owner.")
    other = ingest(store, tmp_path, "volunteer-guide.md",
        "# Repair café — volunteer guide\n\n"
        "Pair first-time volunteers with an experienced repairer. Record the fault, "
        "attempted repair and outcome before returning each item.\n\n"
        "The repair café offers electrical checks, clothing repairs and bicycle adjustments. "
        "Items that cannot be repaired receive a written diagnosis.")
    harness = SimpleNamespace(store=store, settings=store.settings)
    for question in ("how many items can you bring to the repair cafe", "how many items"):
        packet = find(harness, question, max_tokens=8192)
        passages = packet["supporting_passages"]
        assert passages[0]["citation"] == expected["citation"]
        assert passages[0]["quote"] == expected["quote"]
        assert {p["citation"] for p in passages} == {expected["citation"], other["citation"]}
        assert packet["retrieval_assessment"]["answerability"] == "not_determined"


@pytest.mark.parametrize("policy", ["exhaustive", "bounded"])
def test_other_count_nouns_and_visibility(store, tmp_path, policy):
    store.settings.archive_search_policy = policy
    store.settings.library_find_relevance_policy = "coverage"
    expected = ingest(store, tmp_path, "guest-policy.txt", "Only one box per guest.")
    other = ingest(store, tmp_path, "exhibition.txt", "Many boxes are displayed in the exhibition.")
    private = ingest(store, tmp_path, "private-policy.txt", "Guests may bring two boxes.", sensitive=True)
    harness = SimpleNamespace(store=store, settings=store.settings)
    packet = find(harness, "How many boxes may each guest bring?", max_tokens=8192)
    assert packet["supporting_passages"][0]["citation"] == expected["citation"]
    assert private["citation"] not in str(packet)
    visible = find(harness, "How many boxes?", max_tokens=8192, include_sensitive=True)
    assert {p["citation"] for p in visible["supporting_passages"]} == {
        expected["citation"], other["citation"], private["citation"]}


def test_raw_fts_plural_variants_do_not_require_reimport(store, tmp_path):
    hit = ingest(store, tmp_path, "entries.txt", "Three entries fit in the box.")
    archive = DocumentArchive(store)
    # The native FTS index stores unstemmed source words. Query variants must
    # work in both directions without rebuilding or prefix matching IDs.
    for query in ("entry", "entries", "boxes"):
        result = archive.search_indexed(query, 5, False, None, "compact", True)
        assert result["hits"][0]["citation"] == hit["citation"]
    assert not archive.search_indexed("entrypoint", 5, False, None, "compact", True)["hits"]


def test_count_breaks_equal_priority_with_a_repeated_title_noun(store, tmp_path):
    store.settings.library_find_relevance_policy = "coverage"
    expected = ingest(store, tmp_path, "rules.txt", "Each resident may bring one item.", title="Intake rules")
    other = ingest(store, tmp_path, "index.txt", "Many items are shown. Readers browse items online.", title="Items index")
    packet = find(SimpleNamespace(store=store, settings=store.settings), "how many items", max_tokens=8192)
    assert [p["citation"] for p in packet["supporting_passages"]] == [expected["citation"], other["citation"]]


def test_existing_bounded_index_refreshes_only_derived_postings(store, tmp_path):
    hit = ingest(store, tmp_path, "stock.txt", "Two cafés each provide three boxes.")
    archive = DocumentArchive(store)
    assert archive.search_bounded("boxes")["hits"]
    db = store._sqlite
    # Simulate a pre-normalization index without altering authoritative text.
    db.execute("UPDATE archive_rank_terms SET term='boxes' WHERE term='box'")
    db.execute("UPDATE store_meta SET value='old-tokenizer' WHERE key='archive_rank_tokenizer'")
    db.commit()
    assert not db.execute("SELECT 1 FROM archive_rank_dirty").fetchone()
    result = archive.search_bounded("box cafe")
    assert result["hits"][0]["citation"] == hit["citation"]
    assert result["hits"][0]["quote"] == hit["quote"]
    assert db.execute("SELECT value FROM store_meta WHERE key='archive_rank_tokenizer'").fetchone()[0] == TOKENIZER_VERSION
    assert db.execute("SELECT 1 FROM archive_rank_terms WHERE term='box'").fetchone()
    assert not db.execute("SELECT 1 FROM archive_rank_terms WHERE term='boxes'").fetchone()


def test_managed_libraries_choose_coverage_without_changing_advanced_default(tmp_path):
    directory = tmp_path / "library"
    create_library(directory)
    assert library_settings(directory).library_find_relevance_policy == "coverage"
    assert Settings.model_fields["library_find_relevance_policy"].default == "legacy"


def test_focused_page_order_keeps_continuation_complete(store, tmp_path):
    store.settings.library_find_relevance_policy = "coverage"
    store.settings.library_find_compact = True
    harness = SimpleNamespace(store=store, settings=store.settings)
    expected = set()
    for index in range(9):
        count = "Only one small box per guest." if index == 7 else "Boxes are displayed throughout the exhibition."
        hit = ingest(store, tmp_path, f"policy-{index}.txt", count + f" Record {index}. " + "Exhibition context. " * 25)
        expected.add(hit["citation"])
    seen, cursor = [], None
    for _ in range(20):
        packet = find(harness, "How many boxes?" if cursor is None else None,
                      cursor=cursor, max_tokens=2048)
        seen.extend(p["citation"] for p in packet["supporting_passages"])
        seen.extend(p["citation"] for p in packet["omitted_references"]
                    if p.get("stream") == "supporting_passages")
        cursor = packet["continuation"]["next_cursor"]
        if cursor is None:
            break
    assert cursor is None
    assert set(seen) == expected
    assert len(seen) == len(set(seen))
