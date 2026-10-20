"""Native MCP image content and bounded HTTP transport use synthetic originals."""
from __future__ import annotations

import asyncio
import base64
import importlib.util
import json
import struct
import zlib
from types import SimpleNamespace

import anyio
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from mcp.client.session import ClientSession
from mcp.shared.memory import create_client_server_memory_streams
from pypdf import PdfWriter

from proharness.mcp.server import build_server
from proharness.mcp.tools import serialize_result
from proharness.memory.documents import DocumentArchive
from proharness.memory.library_activity import poll_activity
from proharness.server.routers.library_sources import router
from proharness.tools.archive import ARCHIVE_TOOL_NAMES

NAME = "proharness_document_page_image"


def _png(width=16, height=12):
    def chunk(kind, payload):
        return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", zlib.crc32(kind + payload))
    pixels = b"".join(b"\0" + b"\xff\xff\xff" * width for _ in range(height))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(pixels)) + chunk(b"IEND", b""))


@pytest.fixture
def image_transport(store, tmp_path, monkeypatch):
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=100)
    writer.add_blank_page(width=100, height=200)
    source = tmp_path / "synthetic-original.pdf"
    with source.open("wb") as stream:
        writer.write(stream)
    document = DocumentArchive(store).ingest(str(source))
    harness = SimpleNamespace(store=store, settings=store.settings, loop=SimpleNamespace())
    server, _ = build_server(harness=harness)
    app = FastAPI()
    app.state.library_harness = harness
    app.include_router(router, prefix="/api/library")
    png = _png()

    def render(data, page, max_side):
        assert data.startswith(b"%PDF")
        if page > 2:
            return {"error": "Requested page is outside the PDF page range.",
                    "error_code": "page_out_of_range", "http_status": 422}
        return {"page_count": 2, "width": 16, "height": 12, "_image_png": png,
                "render": {"renderer": "synthetic-test", "ocr": False}}

    monkeypatch.setattr("proharness.memory.document_page_image._worker_render", render)
    return document, harness, server, app, png


async def _wire_call(server, arguments):
    # Exercise actual initialize/list/call protocol handlers and ClientSession,
    # rather than only calling a handler closure or inspecting its return type.
    async with create_client_server_memory_streams() as (client_streams, server_streams), anyio.create_task_group() as group:
        async def serve():
            await server._lowlevel_server.run(*server_streams,
                server._lowlevel_server.create_initialization_options())
        group.start_soon(serve)
        async with ClientSession(*client_streams) as client:
            await client.initialize()
            listed = await client.list_tools()
            tool = next(tool for tool in listed.tools if tool.name == NAME)
            result = await client.call_tool(NAME, arguments)
        group.cancel_scope.cancel()
    return tool, result


def test_native_mcp_wire_image_and_metadata(image_transport):
    document, harness, server, _, png = image_transport
    args = {"document_id": document["document_id"], "page": 2}
    before = harness.store._sqlite.total_changes
    tool, result = asyncio.run(_wire_call(server, args))
    assert not result.is_error
    assert [content.type for content in result.content] == ["text", "image"]
    image = result.content[1]
    assert image.mime_type == "image/png"
    assert base64.b64decode(image.data, validate=True) == png
    metadata = result.structured_content
    assert json.loads(result.content[0].text) == metadata
    assert metadata["document_id"] == document["document_id"]
    assert metadata["version"] == 1 and metadata["page"] == 2
    assert metadata["page_count"] == 2
    assert struct.unpack(">II", png[16:24]) == (metadata["width"], metadata["height"])
    assert metadata["image_bytes"] == len(png)
    assert metadata["source_reference"]["original_content_hash"] == document["content_hash"]
    assert not any(key.startswith("_") for key in metadata)
    assert image.data not in result.content[0].text
    assert "image" not in metadata
    assert tool.annotations.read_only_hint and tool.annotations.idempotent_hint
    assert tool.annotations.destructive_hint is False and tool.annotations.open_world_hint is False
    assert set(tool.input_schema["required"]) == {"document_id", "page"}
    assert tool.input_schema["properties"]["page"]["minimum"] == 1
    assert tool.input_schema["properties"]["max_side"]["minimum"] == 512
    assert tool.input_schema["properties"]["max_side"]["maximum"] == 3072
    assert harness.store._sqlite.total_changes == before
    activity = json.dumps(poll_activity(harness, after=0, include_sensitive=True))
    assert image.data not in activity and "_image_png" not in activity
    assert "document_page_image" not in ARCHIVE_TOOL_NAMES


@pytest.mark.parametrize("changes", [{"page": 0}, {"page": True}, {"page": "1"},
    {"max_side": 511}, {"max_side": 3073}, {"include_sensitive": "false"},
    {"include_sensitive": 1}])
def test_mcp_sdk_rejects_invalid_options(image_transport, changes):
    document, _, server, _, _ = image_transport
    _, result = asyncio.run(_wire_call(server, {"document_id": document["document_id"], "page": 1, **changes}))
    assert result.is_error
    assert not any(content.type == "image" for content in result.content)


def test_http_json_png_and_original_shared_loader(image_transport, monkeypatch):
    document, _, _, app, png = image_transport
    did = document["document_id"]
    from proharness.memory import document_originals
    original_loader = document_originals.load_document_original
    observed = []
    def loader(*args, **kwargs):
        observed.append(args[1])
        return original_loader(*args, **kwargs)
    monkeypatch.setattr(document_originals, "load_document_original", loader)
    with TestClient(app) as client:
        response = client.get(f"/api/library/documents/{did}/pages/1/image")
        assert response.status_code == 200
        assert response.headers["cache-control"] == "no-store"
        payload = response.json()
        assert payload["image"]["mime_type"] == "image/png"
        assert base64.b64decode(payload["image"]["data"], validate=True) == png
        assert payload["image_bytes"] == len(png)
        assert not any(key.startswith("_") for key in payload)
        original = client.get(f"/api/library/documents/{did}/original")
        assert original.status_code == 200 and original.content.startswith(b"%PDF")
        assert original.headers["cache-control"] == "no-store"
        assert original.headers["content-disposition"] == f'attachment; filename="source-{did}.pdf"'
    assert observed == [did]


def test_http_errors_have_status_and_no_image(image_transport):
    document, _, _, app, _ = image_transport
    did = document["document_id"]
    with TestClient(app) as client:
        for path, params, status in [
            (f"documents/{did}/pages/3/image", {}, 422),
            ("documents/unknown/pages/1/image", {}, 404),
            (f"documents/{did}/pages/0/image", {}, 422),
            (f"documents/{did}/pages/1/image", {"max_side": 3073}, 422),
            (f"documents/{did}/pages/1/image", {"include_sensitive": "1"}, 422),
        ]:
            response = client.get("/api/library/" + path, params=params)
            assert response.status_code == status
            assert "image" not in response.json() and "_image_png" not in response.text
        missing = client.get("/api/library/documents/unknown/pages/1/image")
        assert missing.headers["cache-control"] == "no-store"


def test_mcp_private_historical_and_deleted_errors_never_contain_image(image_transport, tmp_path):
    document, harness, server, app, _ = image_transport
    did = document["document_id"]
    # Tighten current source privacy by changing the original into a new version.
    writer = PdfWriter()
    writer.add_blank_page(width=300, height=100)
    source = tmp_path / "synthetic-original.pdf"
    with source.open("wb") as stream:
        writer.write(stream)
    current = DocumentArchive(harness.store).ingest(str(source), sensitive=True)
    args = {"document_id": did, "page": 1}
    _, denied = asyncio.run(_wire_call(server, args))
    assert denied.is_error and all(content.type == "text" for content in denied.content)
    assert denied.structured_content["http_status"] == 404
    _, allowed = asyncio.run(_wire_call(server, {**args, "include_sensitive": True}))
    assert not allowed.is_error and allowed.structured_content["version"] == 1
    with TestClient(app) as client:
        base = f"/api/library/documents/{did}/pages/1/image"
        assert client.get(base).status_code == 404
        assert client.get(base, params={"include_sensitive": "true"}).status_code == 200
    DocumentArchive(harness.store).forget(current["document_id"])
    _, deleted = asyncio.run(_wire_call(server, {**args, "include_sensitive": True}))
    assert deleted.is_error and all(content.type == "text" for content in deleted.content)


@pytest.mark.parametrize("key", ["_image_png", "_original_bytes", "_original_identity"])
def test_text_serialization_fails_closed_for_private_payloads(key):
    with pytest.raises(ValueError, match="native transport"):
        serialize_result({key: b"secret", "document_id": "synthetic"})
    assert json.loads(serialize_result({"result": "ordinary text"})) == {"result": "ordinary text"}


def test_real_renderer_png_reaches_mcp_and_http(store, tmp_path):
    if not all(importlib.util.find_spec(name) for name in ("pypdfium2", "PIL")):
        pytest.skip("Optional document-view dependencies are unavailable")
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=100)
    source = tmp_path / "real-render-synthetic.pdf"
    with source.open("wb") as stream:
        writer.write(stream)
    document = DocumentArchive(store).ingest(str(source))
    harness = SimpleNamespace(store=store, settings=store.settings, loop=SimpleNamespace())
    server, _ = build_server(harness=harness)
    args = {"document_id": document["document_id"], "page": 1, "max_side": 512}
    _, result = asyncio.run(_wire_call(server, args))
    assert not result.is_error, result.structured_content
    png = base64.b64decode(next(content.data for content in result.content if content.type == "image"), validate=True)
    width, height = struct.unpack(">II", png[16:24])
    assert (width, height) == (result.structured_content["width"], result.structured_content["height"])
    assert max(width, height) <= 512 and width * height <= 4_000_000
    assert len(png) <= 2 * 1024 * 1024
    app = FastAPI()
    app.state.library_harness = harness
    app.include_router(router, prefix="/api/library")
    with TestClient(app) as client:
        response = client.get(f"/api/library/documents/{document['document_id']}/pages/1/image", params={"max_side": 512})
        assert response.status_code == 200
        payload = response.json()
        assert base64.b64decode(payload["image"]["data"], validate=True) == png


def test_status_discovers_page_image_without_checking_runtime_or_rendering(store, monkeypatch):
    from proharness.mcp.tools import call_tool

    def unavailable(*args, **kwargs):
        pytest.fail("Capability discovery must not load originals or launch rendering")
    monkeypatch.setattr("proharness.memory.document_page_image.load_document_original", unavailable)
    monkeypatch.setattr("proharness.memory.document_page_image._worker_render", unavailable)
    harness = SimpleNamespace(store=store, settings=store.settings, loop=SimpleNamespace())
    before = store._sqlite.total_changes
    result = call_tool(harness, "proharness_status", {})
    capability = result["capabilities"]["document_page_image"]
    assert capability["tool"] == NAME and capability["format"] == "pdf"
    assert capability["runtime_check"] == "not_performed"
    assert capability["explicit_request_required"] is True
    assert "document-view" in capability["setup"] and "PROHARNESS_PAGE_IMAGE_PYTHON" in capability["setup"]
    assert capability["page_numbering"] == "one_based"
    assert capability["default_max_side"] == 2048
    assert capability["max_side_bounds"] == [512, 3072]
    assert capability["max_pixels"] == 4_000_000 and capability["max_png_bytes"] == 2 * 1024 * 1024
    assert "External image-capable" in capability["client_requirement"]
    assert "internal text-only" in capability["client_requirement"]
    assert "document_id/version/content hash/page" in capability["source_reference"]
    assert store._sqlite.total_changes == before
