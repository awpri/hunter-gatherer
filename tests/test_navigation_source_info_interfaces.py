"""Source context survives the actual MCP and HTTP navigation surfaces."""

import asyncio
import json
import sqlite3
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from hunter_gatherer.mcp.server import build_server
from hunter_gatherer.memory import document_navigation
from hunter_gatherer.memory.documents import DocumentArchive
from hunter_gatherer.server.routers import library


def test_navigation_context_transport_history_and_visibility(store, tmp_path):
    path = tmp_path / "record.md"
    path.write_text("# Statement\nThe planned ceremony would host twelve volunteers.\n")
    archive = DocumentArchive(store)
    original = archive.ingest(str(path), title="Ceremony draft", author="Mira Vale",
                              document_date="2032-02-01", category="draft_plan")
    hit = archive.search("ceremony")["hits"][0]
    path.write_text("# Statement\nThe ceremony was cancelled.\n")
    latest = archive.ingest(str(path), title="Ceremony correction", author="Mira Vale",
                            document_date="2032-02-07", category="correction")
    harness = SimpleNamespace(store=store, settings=store.settings,
                              loop=SimpleNamespace(embedder=store.embedder))
    server, _ = build_server(harness=harness, tool_profile="full")
    app = FastAPI()
    app.state.library_harness = harness
    app.include_router(library.router, prefix="/api/library")

    for tool, arguments, route in (
        ("hg_document_outline", {"document_id": original["document_id"]},
         f"/api/library/documents/{original['document_id']}/outline"),
        ("hg_document_window", {"chunk_id": hit["chunk_id"]},
         f"/api/library/chunks/{hit['chunk_id']}/window"),
    ):
        async def call(name=tool, args=arguments):
            return await server._tool_manager.call_tool(name, args, context=None)

        result = asyncio.run(call())
        mcp = result.structured_content
        assert json.loads(result.content[0].text) == mcp
        with TestClient(app) as client:
            response = client.get(route)
            assert response.status_code == 200
            http = response.json()
        assert mcp["source_info"] == http["source_info"]
        assert http["source_info"]["document_id"] == original["document_id"]
        assert http["source_info"]["version"] == 1
        assert http["source_info"]["category"] == "draft_plan"
        assert http["source_info"]["author"] == "Mira Vale"
        assert http["source_info"]["document_date"] == "2032-02-01"
        assert http["source_info"]["latest_document_id"] == latest["document_id"]
        if "chunks" in http:
            assert http["chunks"][0]["quote"] == hit["quote"]
            assert http["chunks"][0]["citation"] == hit["citation"]

    store._sqlite.execute("UPDATE archive_documents SET sensitive=1 WHERE id=?",
                          (latest["document_id"],))
    hidden = asyncio.run(call()).structured_content
    assert latest["document_id"] not in json.dumps(hidden)
    assert "error" in hidden and "source_info" not in hidden
    assert hit["quote"] not in json.dumps(hidden)


def test_locator_reads_one_snapshot_across_external_visibility_change(store, tmp_path, monkeypatch):
    store._sqlite.execute("PRAGMA journal_mode=WAL")
    path = tmp_path / "snapshot.txt"
    path.write_text("Earlier report.")
    archive = DocumentArchive(store)
    old = archive.ingest(str(path))
    chunk = archive._chunk_rows(False)[0]["id"]
    path.write_text("Current report.")
    latest = archive.ingest(str(path))
    original = document_navigation._source_info

    def change_after_anchor(db, reader, identity, row, include_sensitive):
        with sqlite3.connect(store.settings.sqlite_path) as writer:
            writer.execute("UPDATE archive_documents SET sensitive=1 WHERE id=?",
                           (latest["document_id"],))
        return original(db, reader, identity, row, include_sensitive)

    monkeypatch.setattr(document_navigation, "_source_info", change_after_anchor)
    first = document_navigation.document_window(store, chunk)
    assert first["source_info"]["document_id"] == old["document_id"]
    assert first["source_info"]["latest_document_id"] == latest["document_id"]
    monkeypatch.setattr(document_navigation, "_source_info", original)
    second = document_navigation.document_window(store, chunk)
    assert second == document_navigation.document_window(store, "missing")
    assert "source_info" not in second


def test_source_context_preserves_caller_transaction_and_rollback(store, tmp_path):
    path = tmp_path / "transaction.txt"
    path.write_text("The recorded event.")
    archive = DocumentArchive(store)
    doc = archive.ingest(str(path), author="Original author")
    store._sqlite.execute("BEGIN")
    try:
        store._sqlite.execute("UPDATE archive_documents SET author=? WHERE id=?",
                              ("Uncommitted author", doc["document_id"]))
        outline = document_navigation.document_outline(store, doc["document_id"])
        assert outline["source_info"]["author"] == "Uncommitted author"
        assert store._sqlite.in_transaction
    finally:
        store._sqlite.rollback()
    assert document_navigation.document_outline(store, doc["document_id"])["source_info"]["author"] == "Original author"
