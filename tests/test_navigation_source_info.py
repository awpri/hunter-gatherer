"""Stored labels must survive navigation without displacing exact evidence."""

from pathlib import Path

import pytest

import proharness.memory.document_navigation as navigation
from proharness.memory.documents import DocumentArchive
from proharness.memory.output_budget import output_bytes


def _source(store, tmp_path, monkeypatch, *, title="Research plan", author="Ada Author"):
    monkeypatch.setattr(
        "proharness.memory.documents._extract",
        lambda path: (
            [
                {"page": i + 1, "section": f"Page {i + 1}", "text": f"Exact event 2006: {i} 雪\n"}
                for i in range(5)
            ],
            [],
            "application/pdf",
        ),
    )
    path = tmp_path / "source.txt"
    path.write_text("first version")
    archive = DocumentArchive(store)
    document = archive.ingest(
        str(path), title=title, author=author, document_date="2001-02-03", category="plan"
    )
    chunks = archive._chunk_rows(False)
    return archive, document, path, chunks


def _without_context(monkeypatch, function, *args, **kwargs):
    with monkeypatch.context() as context:
        context.setattr(navigation, "_decorate", lambda result, info, max_tokens: None)
        return function(*args, **kwargs)


def test_navigation_retains_stored_metadata_and_exact_quotes(store, tmp_path, monkeypatch):
    archive, document, _, chunks = _source(store, tmp_path, monkeypatch)
    window = navigation.document_window(store, chunks[2]["id"])
    outline = navigation.document_outline(store, document["document_id"])
    expected = {
        "document_id": document["document_id"],
        "version": 1,
        "title": "Research plan",
        "author": "Ada Author",
        "document_date": "2001-02-03",
        "category": "plan",
        "labels_policy": "Untrusted stored labels, not verified facts.",
    }
    assert window["source_info"] == outline["source_info"] == expected
    assert [chunk["location"]["page"] for chunk in window["chunks"]] == [2, 3, 4]
    for chunk in window["chunks"]:
        assert chunk["document_id"] == expected["document_id"]
        assert chunk["version"] == expected["version"]
        assert chunk["quote"] == archive.get_chunk(chunk["chunk_id"])["quote"]
        assert "2006" in chunk["quote"]  # Stored document_date is not an inferred event date.
        assert chunk["citation"] == f"[doc:{document['document_id']}#{chunk['chunk_id']}]"
    assert window["previous_chunk_id"] == chunks[1]["id"]
    assert window["next_chunk_id"] == chunks[3]["id"]
    for section in outline["sections"]:
        assert "text" not in section
        passage = archive.get_document(**section["followup"]["arguments"])["passages"][0]
        assert passage["section_id"] == section["section_id"]


def test_each_historical_version_keeps_own_labels_and_current_locator(store, tmp_path, monkeypatch):
    archive, original, path, original_chunks = _source(store, tmp_path, monkeypatch)
    documents = [original]
    chunks = [original_chunks[2]["id"]]
    for version in (2, 3):
        path.write_text(f"version {version}")
        documents.append(archive.ingest(
            str(path), title=f"Title {version}", author=f"Author {version}",
            document_date=f"200{version}", category=f"Category {version}",
        ))
        chunks.append(archive._chunk_rows(False)[2]["id"])
    for version, (document, chunk_id) in enumerate(zip(documents, chunks, strict=True), 1):
        window = navigation.document_window(store, chunk_id)
        outline = navigation.document_outline(store, document["document_id"])
        info = window["source_info"]
        assert info == outline["source_info"]
        assert info["document_id"] == document["document_id"]
        assert info["version"] == version
        assert info["title"] == ("Research plan" if version == 1 else f"Title {version}")
        assert info["author"] == ("Ada Author" if version == 1 else f"Author {version}")
        assert all(entry["version"] == version for entry in window["chunks"])
        if version < 3:
            assert info["latest_document_id"] == documents[-1]["document_id"]
            assert not outline["is_latest"]
        else:
            assert "latest_document_id" not in info
            assert outline["is_latest"]


@pytest.mark.parametrize("hidden", ["sensitive", "deleted"])
def test_hidden_newest_has_no_locator_or_fallback_to_older_public(store, tmp_path, monkeypatch, hidden):
    archive, original, path, chunks = _source(store, tmp_path, monkeypatch)
    path.write_text("second public version")
    middle = archive.ingest(str(path), title="Middle public label")
    path.write_text("third hidden version")
    latest = archive.ingest(
        str(path), title="Hidden newest title", author="Hidden newest author",
    )
    # Legacy catalogs may have missed ingestion's family propagation. Readers
    # must still apply the latest restriction to all historical source labels.
    if hidden == "deleted":
        store._sqlite.execute(
            "UPDATE archive_documents SET deleted_at=1 WHERE id=?", (latest["document_id"],)
        )
    else:
        store._sqlite.execute(
            "UPDATE archive_documents SET sensitive=1 WHERE id=?", (latest["document_id"],)
        )
    for read, identity in (
        (navigation.document_outline, original["document_id"]),
        (navigation.document_window, chunks[2]["id"]),
    ):
        result = read(store, identity)
        assert result == read(store, "missing")
        assert "source_info" not in result
        assert middle["document_id"] not in str(result)
        assert latest["document_id"] not in str(result)
        assert "Hidden newest" not in str(result)
        opted_in = read(store, identity, include_sensitive=True)
        if hidden == "sensitive":
            assert opted_in["source_info"]["latest_document_id"] == latest["document_id"]
        else:
            assert opted_in == read(store, "missing", include_sensitive=True)
            assert "source_info" not in opted_in
    hidden_result = navigation.document_outline(store, latest["document_id"])
    assert hidden_result == navigation.document_outline(store, "missing")
    assert "source_info" not in hidden_result


@pytest.mark.parametrize("hidden", ["sensitive", "deleted"])
def test_family_privacy_changes_exclude_historical_labels(store, tmp_path, monkeypatch, hidden):
    archive, original, path, chunks = _source(store, tmp_path, monkeypatch)
    path.write_text("new private/deleted version")
    latest = archive.ingest(str(path), sensitive=hidden == "sensitive")
    if hidden == "deleted":
        archive.forget(latest["document_id"])
    for function, identity in (
        (navigation.document_outline, original["document_id"]),
        (navigation.document_window, chunks[2]["id"]),
    ):
        assert function(store, identity) == function(store, "missing")
        assert "source_info" not in function(store, identity)


@pytest.mark.parametrize("max_tokens", [512, 1024, 4096])
@pytest.mark.parametrize("kind", ["outline", "window"])
def test_metadata_never_displaces_baseline_sources(store, tmp_path, monkeypatch, max_tokens, kind):
    _, document, _, chunks = _source(store, tmp_path, monkeypatch)
    store._sqlite.execute(
        "UPDATE archive_documents SET title=?,author=?,document_date=?,category=? WHERE id=?",
        ("雪" * 200, "雪" * 200, "雪" * 100, "雪" * 128, document["document_id"]),
    )
    if kind == "outline":
        function, identity, arguments, selected = (
            navigation.document_outline, document["document_id"], {}, "sections"
        )
    else:
        function, identity, arguments, selected = (
            navigation.document_window, chunks[2]["id"], {"before": 2, "after": 2}, "chunks"
        )
    arguments.update(max_tokens=max_tokens, store_identity={"identity": "雪" * 35})
    baseline = _without_context(monkeypatch, function, store, identity, **arguments)
    result = function(store, identity, **arguments)
    assert result[selected] == baseline[selected]
    for key in ("previous_chunk_id", "next_chunk_id", "next_offset", "omitted", "omitted_counts"):
        assert result.get(key) == baseline.get(key)
    assert result["content_policy"] == baseline["content_policy"]
    assert result["store"] == baseline["store"]
    assert output_bytes(result) <= max_tokens * 4
    if "source_info" in result:
        info = result["source_info"]
        for field, length in (("title", 200), ("author", 200), ("document_date", 100), ("category", 128)):
            if field in info:
                assert info[field] == "雪" * length
            else:
                assert field in info["omitted_fields"]
        if info.get("omitted_fields"):
            assert info["omitted_reason"] == "output_budget"
    else:
        assert result["source_info_omitted"] == "output_budget"


@pytest.mark.parametrize("title", [b"invalid title", "雪" * 20000], ids=["invalid-bytes", "oversized-unicode"])
def test_malformed_and_oversized_labels_are_unknown_with_whole_field_warnings(store, tmp_path, monkeypatch, title):
    _, document, _, chunks = _source(store, tmp_path, monkeypatch)
    store._sqlite.execute(
        "UPDATE archive_documents SET title=?,author=?,document_date=?,category=? WHERE id=?",
        (title, "雪" * 20000, "雪" * 20000, "雪" * 20000, document["document_id"]),
    )
    for function, identity in (
        (navigation.document_outline, document["document_id"]),
        (navigation.document_window, chunks[2]["id"]),
    ):
        result = function(store, identity, max_tokens=4096)
        info = result["source_info"]
        assert all(info[field] is None for field in ("title", "author", "document_date", "category"))
        assert set(info["source_metadata_warnings"]) == {
            "title_unavailable", "author_unavailable", "document_date_unavailable", "category_unavailable"
        }
        baseline = _without_context(monkeypatch, function, store, identity, max_tokens=512)
        minimum = function(store, identity, max_tokens=512)
        selected = "sections" if function is navigation.document_outline else "chunks"
        assert minimum[selected] == baseline[selected]
        assert output_bytes(minimum) <= 2048


def test_saturated_empty_outline_preserves_previously_valid_response(store, tmp_path, monkeypatch):
    _, document, _, _ = _source(store, tmp_path, monkeypatch)
    # Find the exact framing boundary, including fixed-point budget-report bytes.
    lower, upper, baseline = 0, 2048, None
    while lower <= upper:
        size = (lower + upper) // 2
        try:
            candidate = _without_context(
                monkeypatch, navigation.document_outline, store, document["document_id"],
                offset=5, max_tokens=512, store_identity={"identity": "x" * size},
            )
        except ValueError:
            upper = size - 1
        else:
            baseline = candidate
            lower = size + 1
    assert baseline is not None
    assert output_bytes(baseline) == 2048
    result = navigation.document_outline(
        store, document["document_id"], offset=5, max_tokens=512, store_identity=baseline["store"]
    )
    assert result == baseline
    assert "source_info" not in result


def test_navigation_context_is_read_only_and_does_not_read_source_files(store, tmp_path, monkeypatch):
    _, document, _, chunks = _source(store, tmp_path, monkeypatch)
    writes = store._sqlite.total_changes

    def no_read(*args, **kwargs):
        pytest.fail("Navigation read an external source file")

    monkeypatch.setattr(Path, "read_bytes", no_read)
    monkeypatch.setattr(Path, "read_text", no_read)
    for function, identity in (
        (navigation.document_outline, document["document_id"]),
        (navigation.document_window, chunks[2]["id"]),
    ):
        assert function(store, identity)["source_info"]["author"] == "Ada Author"
        assert not store._sqlite.in_transaction
    assert store._sqlite.total_changes == writes


def test_corrupt_neighbor_does_not_gain_access_through_shared_metadata(store, tmp_path, monkeypatch):
    _, document, _, chunks = _source(store, tmp_path, monkeypatch)
    store._sqlite.execute(
        "UPDATE archive_chunks SET text=? WHERE id=?", ("tampered neighbor", chunks[1]["id"])
    )
    result = navigation.document_window(store, chunks[2]["id"])
    assert result["source_info"]["document_id"] == document["document_id"]
    assert result["unavailable_neighbor_count"] == 1
    assert result["previous_chunk_id"] is None
    assert chunks[1]["id"] not in str(result)
    assert "tampered neighbor" not in str(result)
    assert navigation.document_window(store, chunks[1]["id"]) == navigation.document_window(store, "missing")
