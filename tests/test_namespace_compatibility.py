"""Rebranding preserves selected historical stores and exact serialized evidence."""
from copy import deepcopy
import asyncio
import hashlib
import json
from types import SimpleNamespace

import pytest

from hunter_gatherer.memory.collection_compact import compact_collection_export, expand_collection_export
from hunter_gatherer.memory.documents import DocumentArchive
from hunter_gatherer.memory.evidence_compact import compact_evidence_packet, expand_evidence_packet
from hunter_gatherer.memory import library_collections
from hunter_gatherer.memory.original_file_reference import original_reference_citation
from hunter_gatherer.memory.output_budget import budget_report
from hunter_gatherer.memory.reference_fetch import fetch_references
from hunter_gatherer.memory.schemas import Memory
from hunter_gatherer.memory.store import MemoryStore
from hunter_gatherer.server.routers.library_sources import CollectionInput
from hunter_gatherer.tools.archive import _internal_followups


def test_legacy_collection_store_reopens_without_rewriting_exact_sources(store, tmp_path, monkeypatch):
    """A synthetic pre-rename workspace remains visible after restart, then editable."""
    text = "ProHarness archived this exact wording: café, not a renamed quotation."
    source = tmp_path / "source.md"
    source.write_text(text, encoding="utf-8")
    archive = DocumentArchive(store)
    document = archive.ingest(str(source), title="ProHarness source label")
    chunk_id = store._sqlite.execute("SELECT id FROM archive_chunks WHERE document_id=?",
                                    (document["document_id"],)).fetchone()[0]
    hit = archive.get_chunk(chunk_id)
    memory = store.add(Memory(text="ProHarness was the previous name.", source="proharness://legacy-note"))
    references = [{"document_id": document["document_id"], "chunk_id": chunk_id}, {"memory_id": memory.id}]
    with monkeypatch.context() as legacy:
        legacy.setattr(library_collections, "_MARKER", "proharness:personal-library:evidence-collections:v1")
        saved = library_collections.save_collection(store, CollectionInput(
            name="Historical collection", references=references, idempotency_key="before-rename"))
    workspaces = list(store._sqlite.execute("SELECT id,data FROM coordination_objects WHERE kind='workspace'"))
    assert len(workspaces) == 1
    assert json.loads(workspaces[0][1])["description"].startswith("proharness:")
    before = {table: list(store._sqlite.execute(f"SELECT * FROM {table}")) for table in
              ("memories", "archive_documents", "archive_chunks", "coordination_objects")}
    fingerprint = store.get_meta("embedder_fingerprint")
    with MemoryStore(store.settings, embedder=store.embedder) as reopened:
        assert library_collections.get_collection(reopened, saved["id"]) == saved
        assert library_collections.list_collections(reopened)["collections"] == [saved]
        found = fetch_references(reopened, [hit["citation"], f"[mem:{memory.id}]"], max_tokens=8192)
        assert found["statuses"] == ["available", "available"]
        assert found["sources"][0]["quote"] == text
        assert found["sources"][1]["text"] == memory.text
        assert found["sources"][0]["source_uri"] == f"hg://documents/{document['document_id']}"
        assert reopened.get_meta("embedder_fingerprint") == fingerprint
        for table, rows in before.items():
            assert list(reopened._sqlite.execute(f"SELECT * FROM {table}")) == rows
        updated = library_collections.save_collection(reopened, CollectionInput(
            name="Historical collection", references=references, expected_revision=1,
            notes="A new review.", idempotency_key="after-rename"), saved["id"])
        assert updated["revision"] == 2 and updated["id"] == saved["id"]
        assert list(reopened._sqlite.execute("SELECT id,data FROM coordination_objects WHERE kind='workspace'")) == workspaces
        assert library_collections.get_collection(reopened, saved["id"], revision=1) == saved
    assert hashlib.sha256(source.read_bytes()).hexdigest() == document["content_hash"]


def test_new_collection_uses_only_current_workspace_marker(store):
    memory = store.add(Memory(text="An exact remembered sentence."))
    library_collections.save_collection(store, CollectionInput(
        name="New collection", references=[{"memory_id": memory.id}], idempotency_key="new"))
    data = json.loads(store._sqlite.execute("SELECT data FROM coordination_objects WHERE kind='workspace'").fetchone()[0])
    assert data["description"] == "hg:personal-library:evidence-collections:v1"


def test_legacy_evidence_format_expands_losslessly_and_new_output_is_branded():
    citation = "[doc:" + "a" * 32 + "#" + "b" * 32 + "]"
    original = {"supporting_passages": [{"citation": citation, "quote": "ProHarness source text.",
        "context": "ProHarness source text.", "source_uri": "proharness://documents/" + "a" * 32,
        "followup": {"tool": "proharness_open", "arguments": {"refs": [citation]}}}]}
    compact = compact_evidence_packet(original)
    assert compact["compact_format"]["version"] == "hg_evidence_compact_v1"
    compact["compact_format"]["version"] = "proharness_evidence_compact_v1"
    snapshot = deepcopy(compact)
    assert expand_evidence_packet(compact) == original
    assert compact == snapshot
    for version in ("other_evidence_compact_v1", "proharness_evidence_compact_v2", None, []):
        compact["compact_format"]["version"] = version
        with pytest.raises(ValueError, match="Unsupported"):
            expand_evidence_packet(compact)


def _full_collection_with_original(kind, use_open):
    reference = {"document_id": "a" * 32, "version": 2, "original_content_hash": "b" * 64}
    if kind == "original_page":
        reference["page"] = 3
    citation = original_reference_citation(reference)
    is_file = kind == "original_file"
    if use_open:
        followup = {"tool": "proharness_open", "arguments": {
            "refs": [citation], "view": "outline" if is_file else "page",
            "max_tokens": 4096, "include_sensitive": False}}
    else:
        followup = {"tool": "proharness_get_document" if is_file else "proharness_document_page_image",
                    "arguments": {"document_id": reference["document_id"],
                                  "max_chars" if is_file else "page": 2000 if is_file else 3}}
    item = {"kind": kind, "source_reference": reference, "expected_source_reference": deepcopy(reference),
            "citation": citation, "availability": "unchecked", "followup": followup,
            "source_uri": "proharness://documents/" + reference["document_id"]}
    metadata = {"collection_id": "historical-collection", "revision": 4,
                "truncated": False, "omitted_references": 0, "content_policy": "Exact references only."}
    content = {"name": "ProHarness collection", "purpose": "", "requirements": [], "evidence": [item],
               "voice_examples": [], "actor_notes": "", "actor_reviews": [], "unresolved": [],
               "review_policy": "User assessments only.", "export": deepcopy(metadata)}
    return budget_report({"format": "json", **metadata, "content": content, "unresolved": []}, 4096)


@pytest.mark.parametrize("kind", ["original_file", "original_page"])
@pytest.mark.parametrize("use_open", [False, True])
def test_legacy_collection_exports_preserve_original_followups_and_citations(kind, use_open):
    original = _full_collection_with_original(kind, use_open)
    compact = compact_collection_export(original)
    assert compact["content"]["compact_format"]["version"] == "hg_collection_export_compact_v1"
    compact["content"]["compact_format"]["version"] = "proharness_collection_export_compact_v1"
    budget_report(compact, 4096)
    snapshot = deepcopy(compact)
    assert expand_collection_export(compact) == original
    assert compact == snapshot
    # Accepting the historical namespace does not broaden valid tools or coordinates.
    damaged = deepcopy(compact)
    damaged["content"]["evidence"][0]["followup"]["tool"] = "proharness_run_shell"
    budget_report(damaged, 4096)
    with pytest.raises(ValueError, match="Invalid original"):
        expand_collection_export(damaged)
    damaged = deepcopy(compact)
    damaged["content"]["evidence"][0]["source_reference"]["version"] = 99
    budget_report(damaged, 4096)
    with pytest.raises(ValueError, match="Invalid original"):
        expand_collection_export(damaged)


def test_internal_followups_strip_current_prefix_without_changing_source_material():
    payload = {"followups": [{"tool": "hg_get_document", "arguments": {"document_id": "a"}},
                             {"tool": "hg_memory", "arguments": {}},
                             {"tool": "not_hg_get_document", "arguments": {}}],
               "nested": {"retry": {"tool": "hg_document_window", "arguments": {}}},
               "quote": "hg_get_document is source text.",
               "provenance": {"followup": {"tool": "hg_get_document", "arguments": {}}},
               "known_details": {"retry": {"tool": "hg_get_document", "arguments": {}}}}
    original = deepcopy(payload)
    _internal_followups(payload)
    assert payload["followups"][0]["tool"] == "get_document"
    assert payload["nested"]["retry"]["tool"] == "document_window"
    assert payload["followups"][1:] == original["followups"][1:]
    for field in ("quote", "provenance", "known_details"):
        assert payload[field] == original[field]


def _default_mcp_server(store):
    from hunter_gatherer.mcp.server import build_server
    from hunter_gatherer.mcp.tools import Harness

    harness = Harness(store=store, settings=store.settings,
                      loop=SimpleNamespace(), cost=SimpleNamespace())
    # Deliberately omit tool_profile: an ordinary startup must advertise the
    # compact public API without requiring the caller to override the default.
    return build_server(harness=harness)[0]


def test_default_mcp_catalog_contains_only_the_six_current_public_names(store):
    server = _default_mcp_server(store)
    names = {tool.name for tool in asyncio.run(server.list_tools())}
    assert names == {"hg_find", "hg_open", "hg_memory", "hg_collection",
                     "hg_import_documents", "hg_status"}


def _read_resource(server, uri):
    parts = asyncio.run(server.read_resource(uri))
    assert len(parts) == 1 and parts[0].mime_type == "application/json"
    return json.loads(parts[0].content)


@pytest.mark.parametrize("restriction", ["private", "deleted"])
def test_legacy_document_and_chunk_uris_share_current_visibility_checks(store, tmp_path, restriction):
    source = tmp_path / "legacy-resource.md"
    text = "The fictional Snowdrop audit recorded three amber valves."
    title = "Fictional Snowdrop resource record"
    source.write_text(text, encoding="utf-8")
    archive = DocumentArchive(store)
    document = archive.ingest(str(source), title=title)
    chunk_id = store._sqlite.execute("SELECT id FROM archive_chunks WHERE document_id=?",
                                    (document["document_id"],)).fetchone()[0]
    server = _default_mcp_server(store)
    resource_paths = [f"documents/{document['document_id']}", f"chunks/{chunk_id}"]
    changes = store._sqlite.total_changes
    for resource_path in resource_paths:
        current = _read_resource(server, "hg://" + resource_path)
        legacy = _read_resource(server, "proharness://" + resource_path)
        assert current == legacy
        assert text in json.dumps(current) and document["document_id"] in json.dumps(current)
    assert store._sqlite.total_changes == changes

    if restriction == "private":
        # Privacy of a newer version must also withhold old resource bookmarks.
        source.write_text("Private replacement for the Snowdrop audit.", encoding="utf-8")
        private = archive.ingest(str(source), title="Private Snowdrop title", sensitive=True)
        private_chunk = store._sqlite.execute("SELECT id FROM archive_chunks WHERE document_id=?",
                                            (private["document_id"],)).fetchone()[0]
        resource_paths.extend([f"documents/{private['document_id']}", f"chunks/{private_chunk}"])
    else:
        archive.forget(document["document_id"])
    changes = store._sqlite.total_changes
    for resource_path in resource_paths:
        current = _read_resource(server, "hg://" + resource_path)
        legacy = _read_resource(server, "proharness://" + resource_path)
        assert current == legacy
        assert "Snowdrop" not in json.dumps(current)
        assert current.get("error"), "Hidden resources must report unavailable, not an empty successful source"
    assert store._sqlite.total_changes == changes


@pytest.mark.parametrize("explicit_full", [False, True])
def test_managed_mcp_stdio_uses_library_runtime_unless_full_profile_is_explicit(
        tmp_path, monkeypatch, explicit_full):
    import hunter_gatherer.library as personal
    import hunter_gatherer.mcp.server as service
    from hunter_gatherer.mcp.tools import Harness
    from mcp.server.mcpserver.server import MCPServer

    directory = tmp_path / "managed-library"
    personal.create_library(directory)
    original_builder = personal.build_library_harness
    built, routes, advertised = [], [], []

    def selected_builder(settings, route):
        routes.append(route)
        harness = original_builder(settings)
        built.append(harness)
        return harness

    def library_builder(settings):
        return selected_builder(settings, "library")

    def full_builder(settings):
        assert explicit_full, "Default managed MCP must not initialize the agent runtime"
        # Avoid running the optional agent: only verify the explicit routing.
        return selected_builder(settings, "full")

    def run_without_stdio(server, transport, **_kwargs):
        assert transport == "stdio"
        advertised.append({tool.name for tool in asyncio.run(server.list_tools())})

    monkeypatch.setattr(personal, "build_library_harness", library_builder)
    monkeypatch.setattr(Harness, "build", full_builder)
    monkeypatch.setattr(MCPServer, "run", run_without_stdio)
    monkeypatch.setattr(service, "Settings", lambda: pytest.fail("Managed startup read general configuration"))
    arguments = ["--library", str(directory)] + (["--tool-profile", "full"] if explicit_full else [])
    service.main(arguments)
    assert routes == ["full" if explicit_full else "library"]
    assert len(advertised) == len(built) == 1
    if explicit_full:
        assert "hg_delegate" in advertised[0] and len(advertised[0]) > 6
    else:
        assert advertised[0] == {"hg_find", "hg_open", "hg_memory", "hg_collection",
                                 "hg_import_documents", "hg_status"}
    import sqlite3

    with pytest.raises(sqlite3.ProgrammingError, match="closed"):
        built[0].store._sqlite.execute("SELECT 1")
