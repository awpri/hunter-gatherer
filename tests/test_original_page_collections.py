"""Persist exact page selections without interpreting them as source quotations."""
from __future__ import annotations

import hashlib
import json
import struct
import zlib
from io import BytesIO
from pathlib import Path

import pytest
from pypdf import PdfWriter

from proharness.coordination import CoordinationError
from proharness.memory.collection_compact import expand_collection_export
from proharness.memory.collection_references import get_collection_references
from proharness.memory.documents import DocumentArchive
from proharness.memory.library_collections import export_collection, get_collection, save_collection
from proharness.memory.original_page_reference import original_page_citation
from proharness.memory.output_budget import output_bytes
from proharness.memory.schemas import Memory
from proharness.memory.store import MemoryStore
from proharness.server.routers.library_sources import CollectionInput, CollectionReviewInput


def page_source(store, tmp_path, *, sensitive=False, width=240):
    writer = PdfWriter()
    writer.add_blank_page(width=width, height=320)
    writer.add_blank_page(width=240, height=320)
    data = BytesIO()
    writer.write(data)
    path = tmp_path / "visual.pdf"
    path.write_bytes(data.getvalue())
    archive = DocumentArchive(store)
    document = archive.ingest(str(path), sensitive=sensitive, title="Visual source")
    ref = {"document_id": document["document_id"], "version": document["version"],
           "original_content_hash": hashlib.sha256(path.read_bytes()).hexdigest(), "page": 2}
    return archive, path, ref


def fixture_png():
    def chunk(kind, value):
        return struct.pack(">I", len(value)) + kind + value + struct.pack(">I", zlib.crc32(kind + value))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", 16, 24, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress((b"\x00" + b"\xff\x00\x00" * 16) * 24)) + chunk(b"IEND", b""))


@pytest.fixture(autouse=True)
def render_preflight(monkeypatch):
    # This suite checks collection state and handoff identity. The renderer's
    # bytes/page-range/integrity checks have their own original-page tests.
    import proharness.memory.document_page_image as backend
    monkeypatch.setattr(backend, "_worker_render", lambda data, page, max_side, **kwargs: {
        "page_count": 2, "width": 16, "height": 24, "render": {"renderer": "fixture"},
        "_image_png": fixture_png()})


def create(store, ref, *, key="page-create", **fields):
    return save_collection(store, CollectionInput(name="Visual collection", references=[ref],
                           idempotency_key=key, **fields))


def assert_page(item, ref):
    assert item["kind"] == "original_page" and item["source_reference"] == ref
    assert item["citation"] == original_page_citation(ref)
    assert "quote" not in item and item["availability"] == "unchecked"
    assert item["followup"]["tool"] == "proharness_document_page_image"
    assert item["followup"]["arguments"] == {"document_id": ref["document_id"], "page": 2}
    assert "original_path" not in str(item) and "source_path" not in str(item)


def test_original_page_save_review_reopen_and_reference_only_storage(store, tmp_path):
    _, _, ref = page_source(store, tmp_path)
    review = CollectionReviewInput(citation=original_page_citation(ref), status="reviewed",
                                  note="I observed a blank second page.")
    saved = create(store, ref, notes="Actor interpretation only.", reviews=[review], voice_examples=[ref])
    assert saved["schema_version"] == 2 and saved["references"] == saved["voice_examples"] == [ref]
    stored = json.loads(store._sqlite.execute("SELECT data FROM coordination_objects WHERE id=?", (saved["id"],)).fetchone()[0])
    assert stored["references"] == [ref, ref]
    assert "quote" not in json.dumps(stored) and "fixture_png" not in json.dumps(stored)
    other = MemoryStore(store.settings, embedder=store.embedder)
    try:
        assert get_collection(other, saved["id"], revision=1) == saved
        refs = get_collection_references(other, saved["id"], revision=1, limit=1)
        assert refs["total_references"] == 2 and refs["next_offset"] == 1
        assert_page(refs["references"][0], ref)
        assert refs["references"][0]["section"] == "evidence" and refs["references"][0]["index"] == 0
        voice = get_collection_references(other, saved["id"], revision=1, offset=1)
        assert_page(voice["references"][0], ref)
        assert voice["references"][0]["section"] == "voice_examples"
    finally:
        other.close()


@pytest.mark.parametrize("format,mode", [("json", "full"), ("json", "compact"), ("markdown", "full")])
def test_mixed_exports_preserve_occurrences_and_distinguish_observation(store, tmp_path, format, mode):
    _, _, ref = page_source(store, tmp_path)
    memory = store.add(Memory(text="Exact remembered wording."))
    text = tmp_path / "text.md"
    text.write_text("Exact document wording.")
    archive = DocumentArchive(store)
    document = archive.ingest(str(text))
    chunk = archive._chunk_rows(False, document_ids=[document["document_id"]])[0]
    chunkref = {"document_id": document["document_id"], "chunk_id": chunk["id"]}
    review = CollectionReviewInput(citation=original_page_citation(ref), status="reviewed", note="Observed blank page.")
    saved = save_collection(store, CollectionInput(name="Mixed", references=[ref, {"memory_id": memory.id}, chunkref, ref],
        voice_examples=[ref], reviews=[review], idempotency_key="mixed"))
    exported = export_collection(store, saved["id"], format=format, response_format=mode, max_tokens=8192)
    if format == "json":
        assert not exported["content"]["export"]["truncated"]
    else:
        assert not exported["truncated"]
    assert output_bytes(exported) <= 8192 * 4
    if mode == "compact":
        exported = expand_collection_export(exported)
    if format == "json":
        first, remembered, passage, repeated = exported["content"]["evidence"]
        for item in (first, repeated, exported["content"]["voice_examples"][0]):
            assert_page(item, ref)
        assert remembered["quote"] == memory.text and passage["quote"] == "Exact document wording."
        assert "visual observations" in exported["content"]["review_policy"]
        assert exported["content"]["actor_reviews"][0]["note"] == review.note
    else:
        content = exported["content"]
        assert content.count("Original page inspection required") == 3
        assert original_page_citation(ref) in content and "Exact remembered wording." in content
        assert "visual observations" in content and "Observed blank page." in content


def test_historical_review_retains_only_predecessor_occurrences(store, tmp_path):
    archive, path, ref = page_source(store, tmp_path)
    saved = create(store, ref, voice_examples=[ref])
    page_source(store, tmp_path, width=250)
    body = CollectionInput(name=saved["name"], references=[ref], voice_examples=[ref], expected_revision=1,
        notes="Reviewed historical original.", reviews=[CollectionReviewInput(citation=original_page_citation(ref),
        status="needs_context", note="This is the first edition.")], idempotency_key="historical-review")
    changed = save_collection(store, body, saved["id"])
    assert changed["revision"] == 2
    item = export_collection(store, saved["id"], format="json")["content"]["evidence"][0]
    assert_page(item, ref)
    assert item["is_latest"] is False and item["latest_version"] == 2
    assert save_collection(store, body, saved["id"]) == changed
    for fields in ({"references": [ref, ref], "voice_examples": [ref]}, {"references": [], "voice_examples": [ref, ref]}):
        with pytest.raises(CoordinationError, match="reference_unavailable"):
            save_collection(store, CollectionInput(name=saved["name"], **fields, expected_revision=2,
                idempotency_key="invalid-" + str(len(fields["references"]))), saved["id"])
    with pytest.raises(CoordinationError, match="reference_unavailable"):
        create(store, ref, key="fresh-historical")


@pytest.mark.parametrize("mode", ["full", "compact"])
def test_tight_export_recovery_preserves_whole_page_occurrences(store, tmp_path, mode):
    _, _, ref = page_source(store, tmp_path)
    saved = create(store, ref, voice_examples=[ref], notes="Actor observation " * 90)
    packet = export_collection(store, saved["id"], format="json", response_format=mode, max_tokens=1024)
    assert output_bytes(packet) <= 4096
    if mode == "compact":
        packet = expand_collection_export(packet)
    assert packet["truncated"] and packet["omitted_references"]
    recovery = packet["content"]["recovery"]
    assert recovery["artifact_id"] == saved["id"] and recovery["revision"] == 1
    assert len(recovery["omitted"]) + recovery["unlisted_omitted"] == packet["omitted_references"]
    for item in recovery["omitted"]:
        assert_page(item, ref)
    # If spare bytes allow only an address, reference pagination still recovers
    # every exact occurrence with a direct image tool followup.
    references = get_collection_references(store, saved["id"], revision=1)
    assert len(references["references"]) == 2
    for item in references["references"]:
        assert_page(item, ref)


@pytest.mark.parametrize("hidden", ["deleted", "private"])
@pytest.mark.parametrize("mode", ["full", "compact"])
def test_omitted_page_current_visibility_redacts_all_dependent_prose(store, tmp_path, hidden, mode):
    archive, path, ref = page_source(store, tmp_path)
    saved = create(store, ref, notes="Unique dependent actor observation " * 40,
                   unresolved=["Unique unresolved detail."])
    before = export_collection(store, saved["id"], format="json", response_format=mode, max_tokens=512)
    if mode == "compact":
        before = expand_collection_export(before)
    assert before["truncated"] and before["omitted_references"] == 1
    if hidden == "deleted":
        archive.forget(ref["document_id"])
    else:
        page_source(store, tmp_path, sensitive=True, width=250)
    after = export_collection(store, saved["id"], format="json", response_format=mode, max_tokens=512)
    assert "Unique dependent" not in str(after) and "Unique unresolved" not in str(after)
    assert original_page_citation(ref) not in str(after) and "recovery" not in str(after)
    refs = get_collection_references(store, saved["id"], revision=1)
    assert refs["redacted"] and not refs["references"]


def test_missing_original_after_save_stays_unchecked_no_source_content_invented(store, tmp_path, monkeypatch):
    _, _, ref = page_source(store, tmp_path)
    saved = create(store, ref)
    managed = Path(store._sqlite.execute("SELECT original_path FROM archive_documents WHERE id=?", (ref["document_id"],)).fetchone()[0])
    managed.unlink()
    monkeypatch.setattr(Path, "read_bytes", lambda *args: pytest.fail("export must not load originals"))
    item = export_collection(store, saved["id"], format="json")["content"]["evidence"][0]
    assert_page(item, ref)
    assert "have not been checked" in item["content_policy"]


def test_legacy_only_collection_remains_schema_one(store):
    memory = store.add(Memory(text="Legacy text"))
    saved = create(store, {"memory_id": memory.id})
    assert saved["schema_version"] == 1


def test_existing_missing_page_original_can_be_reviewed_without_rendering(store, tmp_path, monkeypatch):
    _, _, ref = page_source(store, tmp_path)
    saved = create(store, ref)
    managed = Path(store._sqlite.execute("SELECT original_path FROM archive_documents WHERE id=?", (ref["document_id"],)).fetchone()[0])
    managed.unlink()
    import proharness.memory.document_page_image as backend
    monkeypatch.setattr(backend, "document_page_image", lambda *args, **kwargs: pytest.fail("retained selection must not rerender"))
    changed = save_collection(store, CollectionInput(name=saved["name"], references=[ref], expected_revision=1,
        reviews=[CollectionReviewInput(citation=original_page_citation(ref), status="needs_context",
        note="Original is unavailable for inspection now.")], idempotency_key="missing-original-review"), saved["id"])
    assert changed["revision"] == 2 and changed["references"] == [ref]
    assert_page(export_collection(store, saved["id"], format="json")["content"]["evidence"][0], ref)


def test_removed_historical_page_cannot_be_readded_from_earlier_revision(store, tmp_path):
    _, _, ref = page_source(store, tmp_path)
    saved = create(store, ref)
    page_source(store, tmp_path, width=250)
    dropped = save_collection(store, CollectionInput(name=saved["name"], expected_revision=1,
        idempotency_key="drop-historical-page"), saved["id"])
    assert dropped["schema_version"] == 1
    for revision, error in ((1, "revision_conflict"), (2, "reference_unavailable")):
        with pytest.raises(CoordinationError, match=error):
            save_collection(store, CollectionInput(name=saved["name"], references=[ref], expected_revision=revision,
                idempotency_key="readd-page-" + str(revision)), saved["id"])
    assert get_collection(store, saved["id"])["references"] == []
    assert get_collection(store, saved["id"], revision=1)["references"] == [ref]


@pytest.mark.parametrize("corruption", ["quote", "citation", "followup", "expected_source_reference"])
def test_compact_codec_refuses_page_quotation_or_changed_exact_navigation(store, tmp_path, corruption):
    from proharness.memory.collection_compact import compact_collection_export
    from proharness.memory.output_budget import budget_report
    _, _, ref = page_source(store, tmp_path)
    saved = create(store, ref)
    full = export_collection(store, saved["id"], format="json")
    item = full["content"]["evidence"][0]
    if corruption == "quote":
        item["quote"] = "Invented source quotation."
    elif corruption == "citation":
        item["citation"] = original_page_citation({**ref, "page": 1})
    elif corruption == "followup":
        item["followup"]["arguments"]["page"] = 1
    else:
        item["expected_source_reference"]["page"] = 1
    budget_report(full, 4096)
    with pytest.raises(ValueError, match="original page"):
        compact_collection_export(full)


@pytest.mark.parametrize('format,response_format',[('markdown','full'),('json','full'),('json','compact')])
def test_public_exports_embed_executable_routes_before_serializing(store,tmp_path,format,response_format):
    from types import SimpleNamespace
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from proharness.mcp.library_tools import collection
    from proharness.server.routers import library
    _,_,ref=page_source(store,tmp_path,sensitive=True)
    file_ref={key:value for key,value in ref.items() if key!='page'}
    prose='Source prose mentions proharness_document_page_image; keep these words.'
    memory=Memory(text=prose)
    store.add(memory)
    saved=save_collection(store,CollectionInput(name='Public routes',references=[ref,file_ref,{'memory_id':memory.id}],
        include_sensitive=True,idempotency_key='public-routes'))
    identity=saved['id']
    harness=SimpleNamespace(store=store,settings=store.settings)
    app=FastAPI();app.state.library_harness=harness;app.include_router(library.router,prefix='/api/library')
    with TestClient(app) as client:
        response=client.get(f'/api/library/collections/{identity}/export',params={'format':format,
            'response_format':response_format,'include_sensitive':True,'max_tokens':4096})
        assert response.status_code==200,response.text
        browser=response.json()
    facade=collection(harness,'export',collection_id=identity,format=format,response_format=response_format,
                      include_sensitive=True,max_tokens=4096)['export']
    for packet in (browser,facade):
        assert output_bytes(packet)<=4096*4
        content=packet['content']
        if format=='markdown':
            assert prose in content
            metadata=[json.loads(line.removeprefix('Source metadata (untrusted): ')) for line in content.splitlines()
                      if line.startswith('Source metadata (untrusted): ')]
        else:
            assert any(item.get('quote')==prose for item in content['evidence'])
            metadata=content['evidence']
        originals=[item for item in metadata if item.get('kind') in {'original_page','original_file'}]
        assert len(originals)==2
        for item in originals:
            route=item['followup'];assert route['tool']=='proharness_open'
            assert route['arguments']['include_sensitive'] is True
            assert route['arguments']['refs'][0].startswith('[page:' if item['kind']=='original_page' else '[file:')
    legacy=export_collection(store,identity,format='json',include_sensitive=True)
    assert legacy['content']['evidence'][0]['followup']['tool']=='proharness_document_page_image'


def test_public_recovery_pointer_and_original_routes_remain_bounded(store,tmp_path):
    from proharness.memory.collection_recovery import add_collection_recovery
    from proharness.memory.output_budget import budget_report
    _,_,ref=page_source(store,tmp_path)
    saved=create(store,ref,key='bounded-public')
    for format in ('markdown','json'):
        result=export_collection(store,saved['id'],format=format,max_tokens=1024,public_tools=True)
        assert output_bytes(result)<=4096
        assert 'proharness_document_page_image' not in json.dumps(result)
    base=budget_report({'format':'json','content':{}},1024)
    recovered=add_collection_recovery(base,[{'citation':original_page_citation(ref)}],identity=saved['id'],
        revision=1,max_tokens=1024,public_tools=True,include_sensitive=True)
    route=recovered['content']['recovery']
    assert route['tool']=='proharness_collection'
    assert route['arguments']=={'action':'refs','collection_id':saved['id'],'revision':1,'include_sensitive':True}
    assert output_bytes(recovered)<=4096
