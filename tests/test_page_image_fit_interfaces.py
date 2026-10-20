"""Public fitting opt-in is strict and preserves native/HTTP image provenance."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from io import BytesIO
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from test_document_page_image_transport import _png, _wire_call

from proharness.mcp.server import build_server
from proharness.mcp.tools import call_tool
from proharness.server.routers.library_sources import router


@pytest.fixture
def interfaces(store, monkeypatch):
    harness = SimpleNamespace(store=store, settings=store.settings, loop=SimpleNamespace())
    server, _ = build_server(harness=harness)
    app = FastAPI()
    app.state.library_harness = harness
    app.include_router(router, prefix="/api/library")
    calls = []
    png = _png(1024, 768)
    reference = {"document_id": "synthetic", "version": 2,
                 "original_content_hash": "a" * 64, "page": 1}

    def image_backend(actual_store, document_id, **arguments):
        assert actual_store is store and document_id == "synthetic"
        calls.append(arguments)
        fitted = arguments.get("fit_to_byte_limit", False)
        render = {"renderer": "synthetic-test", "max_side": 1024 if fitted else 2048,
                  "scale": 1.0, "ocr": False}
        if fitted:
            render.update(requested_max_side=2048, fit_to_byte_limit=True,
                          downsampling_retry=True, attempt_count=3,
                          attempts=[{"max_side": 2048, "width": 2048, "height": 1536,
                                     "outcome": "image_too_large"},
                                    {"max_side": 1536, "width": 1536, "height": 1152,
                                     "outcome": "image_too_large"},
                                    {"max_side": 1024, "width": 1024, "height": 768,
                                     "outcome": "success"}],
                          resolution_notice="Reduced resolution; verify small details against the original.")
        return {**reference, "source_reference": reference, "page_count": 2,
                "width": 1024, "height": 768, "image_bytes": len(png), "mime_type": "image/png",
                "render": render, "_image_png": png, "_source_path": "/private/synthetic-source.pdf"}

    monkeypatch.setattr("proharness.memory.document_page_image.document_page_image", image_backend)
    return harness, server, app, calls, png, reference


@pytest.mark.parametrize("flag", [None, False, True])
def test_native_schema_and_boolean_forwarding(interfaces, flag):
    _, server, _, calls, png, reference = interfaces
    arguments = {"document_id": "synthetic", "page": 1}
    if flag is not None:
        arguments["fit_to_byte_limit"] = flag
    tool, result = asyncio.run(_wire_call(server, arguments))
    schema = tool.input_schema["properties"]["fit_to_byte_limit"]
    assert schema["type"] == "boolean" and schema["default"] is False
    assert not result.is_error
    assert calls == [{"page": 1, "max_side": 2048, "fit_to_byte_limit": bool(flag),
                      "include_sensitive": False}]
    metadata = result.structured_content
    assert json.loads(result.content[0].text) == metadata
    assert metadata["source_reference"] == reference
    assert base64.b64decode(result.content[1].data, validate=True) == png
    assert "/private/synthetic-source.pdf" not in result.content[0].text and "_source_path" not in metadata
    if flag:
        assert metadata["render"]["requested_max_side"] == 2048
        assert metadata["render"]["max_side"] == 1024
        assert metadata["render"]["downsampling_retry"] is True
        assert metadata["render"]["attempt_count"] == 3
        assert len(metadata["render"]["attempts"]) == 3
        assert metadata["render"]["resolution_notice"]


@pytest.mark.parametrize("flag", ["true", "false", 1, 0, None, [], {}])
def test_native_rejects_non_boolean_fit_before_backend(interfaces, flag):
    _, server, _, calls, _, _ = interfaces
    _, result = asyncio.run(_wire_call(server, {"document_id": "synthetic", "page": 1,
                                               "fit_to_byte_limit": flag}))
    assert result.is_error and calls == []
    assert all(content.type == "text" for content in result.content)


@pytest.mark.parametrize("flag", [None, "false", "true"])
def test_http_literal_forwarding_and_json_metadata(interfaces, flag):
    _, _, app, calls, png, reference = interfaces
    params = {"include_sensitive": "true"}
    if flag is not None:
        params["fit_to_byte_limit"] = flag
    with TestClient(app) as client:
        response = client.get("/api/library/documents/synthetic/pages/1/image", params=params)
    assert response.status_code == 200
    expected = {"page": 1, "max_side": 2048, "include_sensitive": True}
    if flag == "true":
        expected["fit_to_byte_limit"] = True
    assert calls == [expected]
    assert response.headers["cache-control"] == "no-store"
    assert response.headers["x-content-type-options"] == "nosniff"
    payload = response.json()
    assert payload["source_reference"] == reference
    assert base64.b64decode(payload["image"]["data"], validate=True) == png
    assert "/private/synthetic-source.pdf" not in response.text and "_source_path" not in payload
    if flag == "true":
        render = payload["render"]
        assert (render["requested_max_side"], render["max_side"]) == (2048, 1024)
        assert render["fit_to_byte_limit"] is True and render["downsampling_retry"] is True
        assert render["attempt_count"] == len(render["attempts"]) == 3
        assert render["resolution_notice"]


@pytest.mark.parametrize("params", [
    {"fit_to_byte_limit": "1"}, {"fit_to_byte_limit": "0"},
    {"fit_to_byte_limit": "True"}, {"fit_to_byte_limit": "yes"},
    {"fit_to_byte_limit": ""}, {"fit_to_byte_limit": "null"},
    {"fit_to_byte_limit": "true", "include_sensitive": "1"},
])
def test_http_rejects_nonliteral_fit_and_preserves_privacy_validation(interfaces, params):
    _, _, app, calls, _, _ = interfaces
    with TestClient(app) as client:
        response = client.get("/api/library/documents/synthetic/pages/1/image", params=params)
    assert response.status_code == 422 and calls == []
    assert "image" not in response.json()


def test_capability_reports_opt_in_without_rendering(interfaces):
    harness, _, _, calls, _, _ = interfaces
    capability = call_tool(harness, "proharness_status", {})["capabilities"]["document_page_image"]
    assert capability["default_fit_to_byte_limit"] is False
    assert capability["byte_limit_fitting"] == {
        "explicit_opt_in": True, "max_attempts": 4, "retry_max_sides": [1536, 1024, 512],
        "retry_trigger": "image_too_large", "legibility": "not_verified"}
    assert calls == [] and capability["runtime_check"] == "not_performed"


def test_real_noise_pdf_fit_through_native_mcp_and_http(store, tmp_path):
    pytest.importorskip("pypdfium2")
    image_module = pytest.importorskip("PIL.Image")
    from test_page_image_fit_worker import noisy_pdf

    from proharness.memory.documents import DocumentArchive

    # Fixed synthetic raster exercises actual PDFium and bounded PNG encoding.
    source = tmp_path / "deterministic-noise.pdf"
    original = noisy_pdf()
    source.write_bytes(original)
    document = DocumentArchive(store).ingest(str(source))
    did = document["document_id"]
    expected_reference = {"document_id": did, "version": document["version"],
                          "original_content_hash": hashlib.sha256(original).hexdigest(), "page": 1}
    harness = SimpleNamespace(store=store, settings=store.settings, loop=SimpleNamespace())
    server, _ = build_server(harness=harness)
    app = FastAPI()
    app.state.library_harness = harness
    app.include_router(router, prefix="/api/library")
    arguments = {"document_id": did, "page": 1, "max_side": 2048}
    before = store._sqlite.total_changes
    _, default_native = asyncio.run(_wire_call(server, arguments))
    assert default_native.is_error and default_native.structured_content["error_code"] == "image_too_large"
    assert all(content.type == "text" for content in default_native.content)
    _, fitted_native = asyncio.run(_wire_call(server, {**arguments, "fit_to_byte_limit": True}))
    assert not fitted_native.is_error, fitted_native.structured_content
    native_png = base64.b64decode(next(content.data for content in fitted_native.content
                                       if content.type == "image"), validate=True)
    with TestClient(app) as client:
        url = f"/api/library/documents/{did}/pages/1/image"
        default_http = client.get(url)
        assert default_http.status_code == 413 and default_http.json()["error_code"] == "image_too_large"
        assert "image" not in default_http.json()
        fitted_http = client.get(url, params={"fit_to_byte_limit": "true"})
    assert fitted_http.status_code == 200, fitted_http.text
    assert fitted_http.headers["cache-control"] == "no-store"
    assert fitted_http.headers["x-content-type-options"] == "nosniff"
    http_packet = fitted_http.json()
    http_png = base64.b64decode(http_packet["image"]["data"], validate=True)
    assert native_png == http_png
    for packet, png in ((fitted_native.structured_content, native_png), (http_packet, http_png)):
        assert packet["source_reference"] == expected_reference
        assert packet["original_content_hash"] == document["content_hash"]
        assert len(png) == packet["image_bytes"] <= 2 * 1024 * 1024
        with image_module.open(BytesIO(png)) as image:
            image.load()
            assert image.format == "PNG" and image.size == (packet["width"], packet["height"])
            assert image.width * image.height <= 4_000_000
        render = packet["render"]
        assert render["requested_max_side"] == 2048 and render["max_side"] < 2048
        assert render["fit_to_byte_limit"] is True and render["downsampling_retry"] is True
        assert 2 <= render["attempt_count"] == len(render["attempts"]) <= 4
        assert [attempt["max_side"] for attempt in render["attempts"]] == [2048, 1536, 1024, 512][:render["attempt_count"]]
        assert all(attempt["outcome"] == "image_too_large" for attempt in render["attempts"][:-1])
        assert render["attempts"][-1] == {"max_side": render["max_side"], "width": packet["width"],
                                          "height": packet["height"], "outcome": "success"}
        assert render["resolution_notice"] and max(packet["width"], packet["height"]) <= render["max_side"]
        assert str(source) not in json.dumps(packet)
    assert store._sqlite.total_changes == before
    assert source.read_bytes() == original
