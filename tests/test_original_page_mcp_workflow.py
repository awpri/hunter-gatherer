"""Real native MCP page recovery works when a PDF has no extracted passages."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import importlib.util
import json
import struct
from io import BytesIO
from types import SimpleNamespace

import anyio
import pytest
from mcp.client.session import ClientSession
from mcp.shared.memory import create_client_server_memory_streams
from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject

from proharness.mcp.server import build_server
from proharness.memory.documents import DocumentArchive
from proharness.memory.library_collections import save_collection
from proharness.memory.original_page_reference import original_page_citation
from proharness.server.routers.library_sources import CollectionInput


def payload(result):
    assert not result.is_error, result.content
    if result.structured_content is not None:
        return result.structured_content
    assert len(result.content) == 1 and result.content[0].type == "text"
    return json.loads(result.content[0].text)


async def recover_and_inspect(server, collection, reference):
    async with create_client_server_memory_streams() as (client_streams, server_streams), anyio.create_task_group() as group:
        async def serve():
            await server._lowlevel_server.run(*server_streams,
                server._lowlevel_server.create_initialization_options())
        group.start_soon(serve)
        async with ClientSession(*client_streams) as client:
            await client.initialize()
            listed = await client.list_tools()
            assert {"proharness_artifact", "proharness_fetch", "proharness_document_page_image",
                    "proharness_check_attribution"} <= {tool.name for tool in listed.tools}

            recovered = payload(await client.call_tool("proharness_artifact", {
                "action": "collection_refs", "artifact_id": collection["id"], "revision": 1}))
            assert recovered["total_references"] == 1 and recovered["next_offset"] is None
            occurrence = recovered["references"][0]
            assert occurrence["section"] == "evidence" and occurrence["index"] == 0
            assert occurrence["source_reference"] == reference and "quote" not in occurrence
            assert occurrence["availability"] == "unchecked"
            citation = occurrence["citation"]
            assert citation == original_page_citation(reference)

            fetched = payload(await client.call_tool("proharness_fetch", {"refs": [citation]}))
            assert fetched["statuses"] == ["available"] and len(fetched["sources"]) == 1
            source = fetched["sources"][0]
            assert source["kind"] == "original_page" and source["source_reference"] == reference
            assert source["citation"] == citation and source["indices"] == [0]
            assert "quote" not in source and "text" not in source and "passage" not in source
            assert source["availability"] == "unchecked"
            assert source["followup"] == occurrence["followup"]

            followup = source["followup"]
            assert followup["tool"] == "proharness_document_page_image"
            image_result = await client.call_tool(followup["tool"], {**followup["arguments"], "max_side": 512})
            assert not image_result.is_error, image_result.content
            assert [item.type for item in image_result.content] == ["text", "image"]
            image_metadata = image_result.structured_content
            assert image_metadata is not None and json.loads(image_result.content[0].text) == image_metadata
            assert image_metadata["source_reference"] == reference and image_metadata["page_count"] == 2
            assert image_metadata["render"]["ocr"] is False
            image = image_result.content[1]
            assert image.mime_type == "image/png"
            png = base64.b64decode(image.data, validate=True)
            assert png.startswith(b"\x89PNG\r\n\x1a\n")
            assert struct.unpack(">II", png[16:24]) == (image_metadata["width"], image_metadata["height"])
            assert max(image_metadata["width"], image_metadata["height"]) <= 512
            assert image_metadata["image_bytes"] == len(png)
            assert image.data not in image_result.content[0].text and "_image_png" not in str(image_metadata)
            from PIL import Image
            with Image.open(BytesIO(png)) as pixels:
                assert pixels.convert("RGB").getpixel((pixels.width // 2, pixels.height // 2)) == (0, 0, 255)

            attribution = payload(await client.call_tool("proharness_check_attribution", {
                "answer": "The second page has a blue field. " + citation, "verify_support": False}))
            assert attribution["citation_status"] == "inspection_required"
            assert attribution["support_status"] == "unverified" and attribution["cacheable"] is False
            assert attribution["judge_called"] is False
            assert "visual_evidence_requires_image_inspection" in attribution["issues"]
            checked = attribution["citations"][0]
            assert checked["status"] == "inspection_required" and checked["source_reference"] == reference
            assert "quote" not in checked and "passage" not in checked
            assert checked["followup"] == followup
        group.cancel_scope.cancel()


def test_zero_chunk_pdf_collection_recovery_native_image_and_attribution_protocol(store, tmp_path, monkeypatch):
    if not all(importlib.util.find_spec(name) for name in ("pypdfium2", "PIL")):
        pytest.skip("Optional document-view dependencies are unavailable")
    writer = PdfWriter()
    writer.add_blank_page(width=200, height=100)
    second = writer.add_blank_page(width=100, height=200)
    drawing = DecodedStreamObject()
    drawing.set_data(b"0 0 1 rg 0 0 100 200 re f")
    second[NameObject("/Contents")] = writer._add_object(drawing)
    original = BytesIO()
    writer.write(original)
    path = tmp_path / "vector-only-synthetic.pdf"
    path.write_bytes(original.getvalue())
    document = DocumentArchive(store).ingest(str(path), title="Vector-only original")
    assert store._sqlite.execute("SELECT COUNT(*) FROM archive_chunks WHERE document_id=?",
                                 (document["document_id"],)).fetchone()[0] == 0
    reference = {"document_id": document["document_id"], "version": 1,
                 "original_content_hash": hashlib.sha256(original.getvalue()).hexdigest(), "page": 2}
    collection = save_collection(store, CollectionInput(name="Original-page protocol workflow", references=[reference],
        notes="Actor observation pending inspection.", idempotency_key="native-mcp-page-workflow"))
    assert collection["schema_version"] == 2 and collection["references"] == [reference]
    assert "quote" not in json.dumps(collection)
    monkeypatch.setattr("proharness.memory.attribution.make_attribution_judge",
                        lambda *args, **kwargs: pytest.fail("Metadata-only pages must not create a support judge"))
    harness = SimpleNamespace(store=store, settings=store.settings, loop=SimpleNamespace())
    server, _ = build_server(harness=harness)
    asyncio.run(recover_and_inspect(server, collection, reference))
