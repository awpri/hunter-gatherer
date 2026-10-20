"""Source-reader and evidence-collection HTTP routes."""
from __future__ import annotations

import base64
from typing import Any, Literal

from fastapi import APIRouter, HTTPException, Query, Request, Response
from fastapi import Path as APIPath
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, StrictBool, StrictInt, StrictStr, model_validator

from proharness.coordination import CoordinationError
from proharness.memory import library_collections as collections
from proharness.memory.collection_input import (  # noqa: F401 -- compatibility exports
    CollectionInput,
    CollectionReviewInput,
)
from proharness.memory.document_navigation import document_outline, document_window
from proharness.memory.documents import DocumentArchive
from proharness.memory.library_board import BoardInput
from proharness.memory.library_health import _read_snapshot

router = APIRouter()


class PrivateInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    include_sensitive: StrictBool = False


class EnrichmentAttachInput(PrivateInput):
    expected_version: StrictInt
    expected_content_hash: StrictStr
    producer: dict[str, Any]
    records: list[dict[str, Any]]
    idempotency_key: StrictStr
    schema_version: StrictInt = 1


def _enrichment_call(request, action, document_id, **arguments):
    from proharness.mcp.document_tools import enrichment
    from proharness.memory.document_enrichment import EnrichmentError

    try:
        return enrichment(_harness(request), action, document_id, **arguments)
    except EnrichmentError as exc:
        raise HTTPException(exc.status_code, {"error": str(exc), "error_code": exc.code}) from exc


@router.post("/documents/{document_id}/enrichments")
def attach_enrichment(request: Request, document_id: str, body: EnrichmentAttachInput):
    return _enrichment_call(request, "attach", document_id, **body.model_dump())


@router.get("/documents/{document_id}/enrichments")
def list_enrichments(request: Request, document_id: str,
                     offset: int = Query(0, ge=0), limit: int = Query(10, ge=1, le=50),
                     max_tokens: int = Query(4096, ge=512, le=16384),
                     include_sensitive: Literal["true", "false"] = Query("false")):
    allowed = {"offset", "limit", "max_tokens", "include_sensitive"}
    if set(request.query_params) - allowed:
        from proharness.memory.document_enrichment import EnrichmentError

        error = EnrichmentError("Unsupported arguments for list")
        raise HTTPException(error.status_code, {"error": str(error), "error_code": error.code})
    return _enrichment_call(request, "list", document_id, offset=offset, limit=limit,
                            max_tokens=max_tokens, include_sensitive=include_sensitive == "true")


@router.post("/documents/{document_id}/enrichments/{batch_id}/revoke")
def revoke_enrichment(request: Request, document_id: str, batch_id: str, body: PrivateInput):
    return _enrichment_call(request, "revoke", document_id, batch_id=batch_id,
                            include_sensitive=body.include_sensitive)


def _harness(request):
    from proharness.server.routers.library import _harness as get_harness

    return get_harness(request)


def _call(function, *args, **kwargs):
    from proharness.server.routers.library import _call as call

    return call(function, *args, **kwargs)


class ReextractInput(PrivateInput):
    backend: Literal["native", "docling"] = "native"
    ocr_engine: Literal["easyocr", "tesseract"] | None = None
    ocr_orientation: Literal["auto", "preserve"] | None = None
    pdf_backend: Literal["pdfium"] | None = None

    @model_validator(mode="after")
    def validate_ocr_backend(self):
        from proharness.memory.documents import _validate_extraction_options
        _validate_extraction_options(self.backend, self.ocr_engine, self.ocr_orientation, self.pdf_backend)
        return self


@router.get("/documents/{document_id}/proposal-progress")
def proposal_progress(request: Request, document_id: str, include_sensitive: bool = False):
    from proharness.memory.import_review import document_review_progress

    return _call(document_review_progress, _harness(request).store, document_id,
                 include_sensitive=include_sensitive)


@router.post("/documents/{document_id}/reextract")
def reextract(request: Request, document_id: str, body: ReextractInput):
    store = _harness(request).store
    archive = DocumentArchive(store)
    with store._lock:
        _visible_document(store, document_id, body.include_sensitive)
    options = {"ocr_engine": body.ocr_engine} if body.ocr_engine is not None else {}
    if body.ocr_orientation is not None:
        options["ocr_orientation"] = body.ocr_orientation
    if body.pdf_backend is not None:
        options["pdf_backend"] = body.pdf_backend
    return _call(archive.reextract, document_id, backend=body.backend,
                 include_sensitive=body.include_sensitive, **options)


@router.post("/group-aliases/preview")
def preview_group_aliases(request: Request, body: BoardInput):
    from proharness.memory.library_board import apply_board_groups, read_board, visible_board
    from proharness.memory.library_group_aliases import preview_aliases
    from proharness.memory.library_groups import group_assignments
    from proharness.server.routers.library import _base_library_groups

    harness = _harness(request)
    DocumentArchive(harness.store, initialize_search=False)
    with _read_snapshot(harness.store):
        board = read_board(harness.store)
        if body.revision != board["revision"]:
            raise HTTPException(409, "Board changed; reload before reviewing aliases.")
        grouping = _base_library_groups(harness, body.include_sensitive)
        projection = visible_board(board, set(group_assignments(grouping)),
                                   {group["id"] for group in grouping["groups"]}, body.include_sensitive)
        manual = apply_board_groups(grouping, {**projection, "aliases": {}})
        try:
            return preview_aliases(manual, projection, body.aliases)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc


def _collection_call(function, *args, **kwargs):
    try:
        result = function(*args, **kwargs)
    except CoordinationError as exc:
        status = 409 if exc.code in {"revision_conflict", "idempotency_conflict"} else 404 if exc.code in {"not_found", "reference_unavailable"} else 422
        raise HTTPException(status, str(exc)) from exc
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, str(exc)) from exc
    if result is None:
        raise HTTPException(404, "Collection not found or excluded.")
    return result


@router.get("/documents/{document_id}/outline")
def outline(request: Request, document_id: str, offset: int = Query(0, ge=0), limit: int = Query(30, ge=1, le=100),
            max_tokens: int = Query(2048, ge=512, le=16384), include_sensitive: bool = False):
    return _call(document_outline, _harness(request).store, document_id, offset=offset, limit=limit,
                 max_tokens=max_tokens, include_sensitive=include_sensitive)


@router.get("/chunks/{chunk_id}/window")
def window(request: Request, chunk_id: str, before: int = Query(1, ge=0, le=10), after: int = Query(1, ge=0, le=10),
           max_tokens: int = Query(4096, ge=512, le=16384), include_sensitive: bool = False,
           table_cell_offset: int | None = Query(None, ge=0, le=1024),
           table_cell_limit: int = Query(32, ge=1, le=64)):
    return _call(document_window, _harness(request).store, chunk_id, before=before, after=after,
                 max_tokens=max_tokens, include_sensitive=include_sensitive,
                 table_cell_offset=table_cell_offset, table_cell_limit=table_cell_limit)


def _visible_document(store, document_id, include_sensitive):
    row = store._sqlite.execute("SELECT d.* FROM archive_documents d WHERE d.id=? AND "
                               + DocumentArchive._visible(include_sensitive, latest=False), (document_id,)).fetchone()
    if row is None:
        raise HTTPException(404, "Document not found or excluded.")
    # Apply newest-version privacy to explicit historical originals as well.
    latest = store._sqlite.execute("SELECT sensitive,deleted_at FROM archive_documents WHERE source_identity=? ORDER BY version DESC LIMIT 1",
                                  (row["source_identity"],)).fetchone()
    if latest["deleted_at"] is not None or (latest["sensitive"] and not include_sensitive):
        raise HTTPException(404, "Document not found or excluded.")
    return row


@router.get("/documents/{document_id}/history")
def history(request: Request, document_id: str, include_sensitive: bool = False):
    store = _harness(request).store
    DocumentArchive(store, initialize_search=False)
    with _read_snapshot(store):
        current = _visible_document(store, document_id, include_sensitive)
        rows = store._sqlite.execute("SELECT d.* FROM archive_documents d WHERE source_identity=? AND "
                                    + DocumentArchive._visible(include_sensitive, latest=False) + " ORDER BY version DESC",
                                    (current["source_identity"],)).fetchall()
        archive = DocumentArchive(store, initialize_search=False)
        return {"document_id": document_id, "versions": [
            {**archive._metadata(row), "extraction": archive.extraction_info(row["id"])}
            if hasattr(archive, "extraction_info") else archive._metadata(row) for row in rows]}


@router.get("/documents/{document_id}/original")
def original(request: Request, document_id: str, include_sensitive: bool = False):
    from proharness.memory.document_originals import load_document_original

    result = load_document_original(_harness(request).store, document_id,
                                    include_sensitive=include_sensitive)
    if result.get("error"):
        raise HTTPException(result["http_status"], result["error"])
    extension = result["source_extension"]
    return Response(result["_original_bytes"], media_type=result["media_type"],
                    headers={"Content-Disposition": f'attachment; filename="source-{document_id}{extension}"',
                             "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@router.get("/documents/{document_id}/pages/{page}/image")
def page_image(request: Request, document_id: str, page: int = APIPath(..., ge=1),
               max_side: int = Query(2048, ge=512, le=3072),
               fit_to_byte_limit: Literal["true", "false"] = Query("false"),
               include_sensitive: Literal["true", "false"] = Query("false")):
    from proharness.memory.document_page_image import document_page_image

    options = {"fit_to_byte_limit": True} if fit_to_byte_limit == "true" else {}
    result = document_page_image(_harness(request).store, document_id, page=page,
                                 max_side=max_side, include_sensitive=include_sensitive == "true", **options)
    metadata = {key: value for key, value in result.items() if not key.startswith("_")}
    if not result.get("error"):
        metadata["image"] = {"mime_type": "image/png",
                             "data": base64.b64encode(result["_image_png"]).decode("ascii")}
    return JSONResponse(metadata, status_code=result.get("http_status", 200),
                        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@router.get("/collections")
def list_collections(request: Request, limit: int = Query(30, ge=1, le=100), after_id: str = "", include_sensitive: bool = False):
    store = _harness(request).store
    with _read_snapshot(store):
        return _collection_call(collections.list_collections, store, limit=limit, after_id=after_id,
                                include_sensitive=include_sensitive)


@router.post("/collections")
def create_collection(request: Request, body: CollectionInput):
    if body.expected_revision is not None:
        raise HTTPException(422, "New collections have no expected_revision.")
    return _collection_call(collections.save_collection, _harness(request).store, body)


@router.put("/collections/{collection_id}")
def save_collection(request: Request, collection_id: str, body: CollectionInput):
    if body.expected_revision is None:
        raise HTTPException(422, "Updating a collection requires expected_revision.")
    return _collection_call(collections.save_collection, _harness(request).store, body, collection_id)


@router.get("/collections/{collection_id}")
def get_collection(request: Request, collection_id: str, revision: int | None = Query(None, ge=1), include_sensitive: bool = False):
    store = _harness(request).store
    with _read_snapshot(store):
        return _collection_call(collections.get_collection, store, collection_id, revision=revision,
                                include_sensitive=include_sensitive)


@router.get("/collections/{collection_id}/export")
def export_collection(request: Request, collection_id: str, format: Literal["markdown", "json"] = "markdown",
                      max_tokens: int = Query(4096, ge=512, le=16384), revision: int | None = Query(None, ge=1),
                      include_sensitive: bool = False, response_format: Literal["full", "compact"] = "full"):
    store = _harness(request).store
    with _read_snapshot(store):
        return _collection_call(collections.export_collection, store, collection_id, format=format,
                                max_tokens=max_tokens, include_sensitive=include_sensitive, revision=revision,
                                response_format=response_format, public_tools=True)


@router.get("/collections/{collection_id}/references")
def collection_references(request: Request, collection_id: str, revision: int = Query(..., ge=1),
                          offset: int = Query(0, ge=0), limit: int = Query(32, ge=1, le=70),
                          max_tokens: int = Query(2048, ge=512, le=16384),
                          include_sensitive: bool = False):
    from proharness.memory.collection_references import get_collection_references

    return _collection_call(get_collection_references, _harness(request).store, collection_id,
                            revision=revision, offset=offset, limit=limit, max_tokens=max_tokens,
                            include_sensitive=include_sensitive)


@router.get("/regions/{record_id}/image")
def visual_region_image(request: Request, record_id: str,
                        view: Literal["region", "page"] = Query("region"),
                        include_sensitive: Literal["true", "false"] = Query("false")):
    from proharness.memory.visual_regions import region_image
    result = region_image(_harness(request).store, record_id, view=view,
                          include_sensitive=include_sensitive == "true")
    metadata = {key: value for key, value in result.items() if not key.startswith("_")}
    if not result.get("error"):
        metadata["image"] = {"mime_type": "image/png", "data": base64.b64encode(result["_image_png"]).decode("ascii")}
    return JSONResponse(metadata, status_code=result.get("http_status", 200),
                        headers={"Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})
