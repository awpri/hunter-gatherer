"""Privacy-aware library HTTP interface using the MCP archive contracts.

Browsing is read-only. Corrections preserve history; source import only accepts
uploaded bytes, and excerpt publication requires an explicit review decision.
"""
from __future__ import annotations

import inspect
import re
import tempfile
from collections import Counter
from functools import wraps
from pathlib import Path
from typing import Annotated, Literal

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from pydantic import BaseModel, ConfigDict, Field, StrictBool, StrictInt, StrictStr, model_validator

from hunter_gatherer.mcp import tools
from hunter_gatherer.memory.documents import DocumentArchive
from hunter_gatherer.memory.evidence import (
    _kind,
    _metadata_search_text,
    _normal_category,
    _verification,
    is_sensitive_memory,
)
from hunter_gatherer.memory.library_activity import observe_call, poll_activity
from hunter_gatherer.memory.library_board import (
    BoardConflict,
    BoardInput,
    CreateGroupInput,
    VisualInput,
    apply_board_groups,
    board_visibility,
    read_board,
    record_visual,
    save_board,
    visible_board,
)
from hunter_gatherer.memory.library_groups import build_library_groups, group_assignments
from hunter_gatherer.memory.library_health import _read_snapshot
from hunter_gatherer.memory.schemas import Memory

router = APIRouter()
Kind = Literal["fact", "preference", "decision", "plan", "procedure", "instruction", "writing", "source"]
UPLOAD_LIMIT = 25 * 1024 * 1024
_ACTIVE_LIBRARY_OPERATIONS = {"remember", "replace_memory", "forget", "set_core_memory", "ingest_document",
                              "get_memory", "get_document", "get_document_chunk", "propose_import",
                              "review_import_candidate", "evidence", "find"}


class Input(BaseModel):
    model_config = ConfigDict(extra="forbid")


class PrivateInput(Input):
    include_sensitive: StrictBool = False


class FindInput(PrivateInput):
    query: StrictStr | None = Field(default=None, min_length=1, max_length=4000)
    requirements: list[Annotated[StrictStr, Field(min_length=1, max_length=240)]] | None = Field(default=None, max_length=4)
    cursor: StrictStr | None = Field(default=None, min_length=32, max_length=32)
    constraints: dict | None = None
    max_tokens: StrictInt = Field(default=2048, ge=512, le=16384)
    contextual_requirements: StrictBool | None = None


class CitedContextInput(PrivateInput):
    query: StrictStr = Field(min_length=1, max_length=4000)
    requirements: list[Annotated[StrictStr, Field(min_length=1, max_length=240)]] = Field(default_factory=list, max_length=4)
    references: list[dict] = Field(min_length=1, max_length=50)
    max_tokens: StrictInt = Field(default=4096, ge=512, le=16384)


class MemoryInput(VisualInput):
    text: str = Field(min_length=1, max_length=50_000)
    kind: Kind = "fact"
    importance: float = Field(default=5, ge=1, le=10, allow_inf_nan=False)
    tags: list[str] = Field(default_factory=list, max_length=100)
    category: str = Field(default="", max_length=200)
    permanent: StrictBool = True
    sensitive: StrictBool = False


class PinInput(PrivateInput):
    pinned: StrictBool


class ReplaceInput(PrivateInput):
    text: str = Field(min_length=1, max_length=50_000)


class ProposeInput(PrivateInput):
    max_candidates: int = Field(default=20, ge=1, le=100)
    offset: int = Field(default=0, ge=0)


class ReviewInput(PrivateInput):
    decision: Literal["approve", "reject"]
    text: str | None = Field(default=None, min_length=1, max_length=8000)
    kind: Kind | None = None
    importance: float = Field(default=5, ge=1, le=10, allow_inf_nan=False)


class InspectionCheckInput(Input):
    id: StrictStr = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$")
    requirement: StrictStr = Field(min_length=1, max_length=4000)
    detail: StrictStr = Field(min_length=1, max_length=240)
    critical_numeric: StrictBool = False


class InspectionPlanInput(Input):
    page_budget: StrictInt = Field(ge=0, le=8)
    checks: list[InspectionCheckInput] = Field(min_length=1, max_length=12)


class EvidenceInput(PrivateInput):
    memory_discovery: StrictBool = False
    lexical_only: StrictBool = False
    include_context: StrictBool = True
    source_discovery: StrictBool = False
    enrichment_discovery: StrictBool = False
    response_format: Literal["full", "compact", "bounded"] = "full"
    max_tokens: StrictInt | None = Field(default=None, ge=512, le=16384)
    selection_strategy: Literal["relevance", "requirements"] = "relevance"
    query: str = Field(min_length=1, max_length=4000)
    top_k: int = Field(default=5, ge=1, le=20)
    required_fields: list[str] | None = Field(default=None, max_length=20)
    evidence_requirements: list[Annotated[str, Field(min_length=1, max_length=240)]] | None = Field(default=None, max_length=4)
    inspection_plan: InspectionPlanInput | None = None

    @model_validator(mode="after")
    def validate_inspection(self):
        if self.max_tokens is not None and self.response_format != "bounded":
            raise ValueError("max_tokens is only valid with response_format=bounded")
        if self.inspection_plan is not None:
            from hunter_gatherer.memory.evidence_inspection import validate_inspection_plan

            validate_inspection_plan(self.inspection_plan.model_dump(), self.query,
                                     self.evidence_requirements or [])
        return self


def _harness(request: Request):
    harness = getattr(request.app.state, "library_harness", None)
    if harness is None:
        raise HTTPException(503, "Library is not ready.")
    return harness


def _snapshot_read(function=None, *, prepare_search=False):
    if function is None:
        return lambda wrapped: _snapshot_read(wrapped, prepare_search=prepare_search)
    @wraps(function)
    def read(request, *args, **kwargs):
        harness = _harness(request)
        # Initialize a missing archive before pinning the read transaction.
        # Existing archive constructors must preserve this transaction.
        with harness.store._lock:
            archive = DocumentArchive(harness.store, initialize_search=False)
            # Commit derived maintenance before the owned read snapshot:
            # its rollback must not undo the index on every HTTP request.
            # Caller-owned transactions still retain their own boundaries.
            if (prepare_search and harness.store.settings.archive_search_policy == "bounded"
                    and not harness.store._sqlite.in_transaction):
                from hunter_gatherer.memory.bounded_retrieval import prepare_index
                prepare_index(archive)
            with _read_snapshot(harness.store):
                return function(request, *args, **kwargs)
    return read


def _call(function, *args, **kwargs):
    try:
        if function.__name__ in _ACTIVE_LIBRARY_OPERATIONS and args:
            # Lists, graph/group refreshes, overview, health and implicit
            # mutation eligibility reads never produce activity themselves.
            try:
                arguments = dict(inspect.signature(function).bind(*args, **kwargs).arguments)
                arguments.pop("harness", None)
            except (TypeError, ValueError):
                arguments = kwargs
            result = observe_call(args[0], function.__name__, lambda: function(*args, **kwargs),
                                  origin="library", arguments=arguments)
        else:
            result = function(*args, **kwargs)
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(409, "Library operation unavailable; inspect library health.") from exc
    if result.get("error"):
        detail = result["error"]
        code = 404 if "not found" in detail.lower() or "unavailable" in detail.lower() else 409
        raise HTTPException(code, detail)
    return result


@router.get("/activity")
def activity(request: Request, after: int | None = Query(None, ge=0), include_sensitive: bool = False):
    # Bootstrap and empty polls must not initialize an archive or emit events.
    return poll_activity(_harness(request), after=after, include_sensitive=include_sensitive)


def _visible_memories(harness, include_sensitive=False):
    # SQLite eligibility is authoritative; stale vectors never resurrect a row.
    with harness.store._lock:
        rows = harness.store._sqlite.execute(
            "SELECT * FROM memories WHERE deleted_at IS NULL ORDER BY created_at DESC,id"
        ).fetchall()
    for row in rows:
        memory = Memory.from_sql_row(tuple(row))
        if memory.is_retrievable() and (include_sensitive or not is_sensitive_memory(memory, harness.store)):
            yield memory


def _record(harness, memory, include_sensitive=False, *, board_records=None):
    if board_records is None:
        board_records = read_board(harness.store)["records"]
    board_visual = board_records.get("memory:" + memory.id, {})
    visual = record_visual(memory.metadata, board_visual)
    return {**visual, "presentation": visual, **tools._memory_out(harness, memory, include_sensitive),
            "kind": _kind(_normal_category(memory.metadata.get("category")), memory=memory),
            "verification": _verification(memory)}


def _matches(query, text):
    # Deterministic full-library lexical filtering. No model or recall touches.
    terms = re.findall(r"\w+", query.casefold())
    folded = text.casefold()
    return all(term in folded for term in terms)


def _require_memory(harness, memory_id, include_sensitive=False):
    result = tools.get_memory(harness, memory_id, include_sensitive=include_sensitive)
    if not result["found"]:
        raise HTTPException(404, "Memory not found or excluded.")
    return result["memory"]


def _documents(harness, query="", include_sensitive=False):
    archive = DocumentArchive(harness.store, initialize_search=False)
    # The archive's visibility predicate suppresses old versions even when the
    # latest version has been forgotten or its privacy was tightened.
    with harness.store._lock:
        rows = harness.store._sqlite.execute(
            f"SELECT d.* FROM archive_documents d WHERE {archive._visible(include_sensitive)} "
            "ORDER BY d.created_at DESC,d.id"
        ).fetchall()
        for row in rows:
            document = archive._metadata(row)
            description = " ".join(str(document.get(field, "")) for field in
                                   ("title", "author", "category", "document_date"))
            if query and not _matches(query, description):
                # Exact source text is searched without copying whole documents
                # into response previews or exposing excluded versions.
                sections = harness.store._sqlite.execute(
                    "SELECT text FROM archive_sections WHERE document_id=? ORDER BY ordinal", (row["id"],)
                ).fetchall()
                if not _matches(query, description + " " + " ".join(section[0] for section in sections)):
                    continue
            yield document


def _base_library_groups(harness, include_sensitive=False, *, memories=None):
    if memories is None:
        memories = list(_visible_memories(harness, include_sensitive))
    return build_library_groups(harness, memories,
                                list(_documents(harness, include_sensitive=include_sensitive)),
                                include_sensitive=include_sensitive)


def _board_visibility(harness, include_sensitive=False):
    return board_visibility(harness, include_sensitive)


def _library_groups(harness, include_sensitive=False, *, memories=None):
    grouping = _base_library_groups(harness, include_sensitive, memories=memories)
    board = visible_board(read_board(harness.store), set(group_assignments(grouping)),
                          {group["id"] for group in grouping["groups"]}, include_sensitive)
    return apply_board_groups(grouping, board)


@router.get("/board")
@_snapshot_read
def get_board(request: Request, include_sensitive: bool = False):
    harness = _harness(request)
    records, groups = _board_visibility(harness, include_sensitive)
    return visible_board(read_board(harness.store), records, groups, include_sensitive)


def _write_board(request, body, *, create=False):
    harness = _harness(request)
    DocumentArchive(harness.store, initialize_search=False)
    try:
        return save_board(harness.store, body, lambda opted: _board_visibility(harness, opted), create=create)
    except BoardConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(409, str(exc)) from exc


@router.patch("/board")
def patch_board(request: Request, body: BoardInput):
    return _write_board(request, body)


@router.post("/groups")
def create_group(request: Request, body: CreateGroupInput):
    return _write_board(request, body, create=True)


@router.get("/groups")
@_snapshot_read
def list_groups(request: Request, include_sensitive: bool = False):
    return _library_groups(_harness(request), include_sensitive)


@router.get("/overview")
@_snapshot_read
def overview(request: Request, include_sensitive: bool = False):
    harness = _harness(request)
    categories, kinds = Counter(), Counter()
    memory_count, document_count = 0, 0
    for memory in _visible_memories(harness, include_sensitive):
        memory_count += 1
        category = memory.metadata.get("category")
        if isinstance(category, str) and category:
            categories[category] += 1
        kinds[_kind(_normal_category(category), memory=memory)] += 1
    for document in _documents(harness, include_sensitive=include_sensitive):
        document_count += 1
        if document["category"]:
            categories[document["category"]] += 1
    pending = _call(tools.list_import_candidates, harness, limit=1, include_sensitive=include_sensitive)["total"]
    return {"store": tools._store_identity(harness),
            "counts": {"memories": memory_count, "documents": document_count, "pending": pending},
            "categories": [{"name": name, "count": count} for name, count in sorted(categories.items())],
            "kinds": [{"name": name, "count": count} for name, count in sorted(kinds.items())],
            "include_sensitive": include_sensitive,
            "search_mode": "lexical", "formats": ["txt", "md", "pdf", "docx", "pptx"],
            "upload_limit_bytes": UPLOAD_LIMIT,
            "forget_semantics": "Recoverable removal from retrieval; originals and audit history remain."}


@router.get("/memories")
@_snapshot_read
def list_memories(request: Request, query: str = Query("", max_length=4000),
                  kind: Kind | None = None, category: str = "", pinned: bool | None = None,
                  group: str = "",
                  limit: int = Query(30, ge=1, le=100), offset: int = Query(0, ge=0),
                  include_sensitive: bool = False):
    harness = _harness(request)
    memories = list(_visible_memories(harness, include_sensitive))
    assignments = group_assignments(_library_groups(harness, include_sensitive, memories=memories))
    page, total = [], 0
    board_records = read_board(harness.store)["records"]
    for memory in memories:
        group_id = assignments.get("memory:" + memory.id)
        if group and group_id != group:
            continue
        if category and memory.metadata.get("category") != category:
            continue
        if kind and _kind(_normal_category(memory.metadata.get("category")), memory=memory) != kind:
            continue
        if pinned is not None and bool(memory.metadata.get("core")) != pinned:
            continue
        if query and not _matches(query, _metadata_search_text(memory)):
            continue
        if offset <= total < offset + limit:
            page.append({**_record(harness, memory, include_sensitive, board_records=board_records), "group_id": group_id})
        total += 1
    return {"memories": page, "total": total,
            "next_offset": offset + len(page) if offset + len(page) < total else None}


@router.post("/memories")
def remember(request: Request, body: MemoryInput):
    harness = _harness(request)
    metadata = {"kind": body.kind}
    presentation = body.model_dump(include={"icon", "image"}, exclude_none=True)
    if presentation:
        metadata["presentation"] = presentation
    if body.category:
        metadata["category"] = body.category
    candidate = Memory(text=body.text, tags=body.tags, metadata=metadata)
    if not body.sensitive and is_sensitive_memory(candidate, harness.store):
        raise HTTPException(422, "This record requires sensitive=true to store private content.")
    result = _call(tools.remember, harness, body.text,
                   memory_type="procedural" if body.kind == "procedure" else "semantic",
                   importance=body.importance, tags=body.tags, source="library:user", metadata=metadata,
                   permanent=body.permanent, sensitive=body.sensitive)
    if result.get("id"):
        # Creation is an explicit disclosure of the submitted text. Do not
        # return a hidden pre-existing dedup target to a public-only caller.
        found = tools.get_memory(harness, result["id"], include_sensitive=body.sensitive)
        if found["found"]:
            result["memory"] = _record(harness, harness.store.get(result["id"]), body.sensitive)
        else:
            result["id"] = None
    return result


@router.get("/memories/{memory_id}")
@_snapshot_read
def get_memory(request: Request, memory_id: str, include_sensitive: bool = False):
    harness = _harness(request)
    result = _call(tools.get_memory, harness, memory_id, include_sensitive=include_sensitive)
    if not result["found"]:
        raise HTTPException(404, "Memory not found or excluded.")
    assignments = group_assignments(_library_groups(harness, include_sensitive))
    return {**_record(harness, harness.store.get(memory_id), include_sensitive),
            "group_id": assignments.get("memory:" + memory_id)}


@router.post("/memories/{memory_id}/pin")
def pin(request: Request, memory_id: str, body: PinInput):
    harness = _harness(request)
    with harness.store._lock:
        _require_memory(harness, memory_id, body.include_sensitive)
        return _call(tools.set_core_memory, harness, memory_id, pinned=body.pinned)


@router.post("/memories/{memory_id}/replace")
def replace(request: Request, memory_id: str, body: ReplaceInput):
    harness = _harness(request)
    with harness.store._lock:
        _require_memory(harness, memory_id, body.include_sensitive)
        old = harness.store.get(memory_id)
        candidate = Memory(text=body.text, tags=old.tags, metadata=old.metadata)
        if not body.include_sensitive and is_sensitive_memory(candidate, harness.store):
            raise HTTPException(422, "This correction requires include_sensitive=true for private content.")
        result = _call(tools.replace_memory, harness, memory_id, body.text)
        result["memory"] = _record(harness, harness.store.get(result["memory"]["id"]), body.include_sensitive)
        return result


@router.post("/memories/{memory_id}/forget")
def forget(request: Request, memory_id: str, body: PrivateInput):
    harness = _harness(request)
    with harness.store._lock:
        _require_memory(harness, memory_id, body.include_sensitive)
        return _call(tools.forget, harness, memory_id=memory_id)


@router.get("/documents")
@_snapshot_read
def list_documents(request: Request, query: str = Query("", max_length=4000),
                   category: str = "", group: str = "",
                   limit: int = Query(30, ge=1, le=100), offset: int = Query(0, ge=0),
                   include_sensitive: bool = False):
    harness = _harness(request)
    assignments = group_assignments(_library_groups(harness, include_sensitive))
    page, total = [], 0
    for document in _documents(harness, query, include_sensitive):
        group_id = assignments.get("document:" + document["document_id"])
        if group and group_id != group:
            continue
        if category and document["category"] != category:
            continue
        if offset <= total < offset + limit:
            page.append({**document, "group_id": group_id})
        total += 1
    return {"documents": page, "total": total,
            "next_offset": offset + len(page) if offset + len(page) < total else None}


@router.post("/documents/upload")
def upload(request: Request, file: Annotated[UploadFile, File()], title: str = Form("", max_length=1000),
           author: str = Form("", max_length=1000), document_date: str = Form("", max_length=100),
           category: str = Form("personal", max_length=200), sensitive: str = Form("false")):
    if sensitive not in {"true", "false"}:
        raise HTTPException(422, "sensitive must be true or false.")
    filename = Path((file.filename or "").replace("\\", "/")).name
    extension = Path(filename).suffix.lower()
    if extension not in {".txt", ".md", ".pdf", ".docx", ".pptx"}:
        raise HTTPException(415, "Supported uploads are .txt, .md, .pdf, .docx, and .pptx.")
    try:
        with tempfile.TemporaryDirectory(prefix="hunter_gatherer-upload-") as temporary:
            path = Path(temporary) / ("upload" + extension)
            size = 0
            with path.open("wb") as target:
                while block := file.file.read(1024 * 1024):
                    size += len(block)
                    if size > UPLOAD_LIMIT:
                        raise HTTPException(413, "Upload exceeds 25 MiB.")
                    target.write(block)
            if not size:
                raise HTTPException(422, "Upload is empty.")
            harness = _harness(request)
            result = _call(tools.ingest_document, harness, str(path),
                           title=title or Path(filename).stem, author=author, document_date=document_date,
                           category=category, sensitive=sensitive == "true")
            from hunter_gatherer.maintenance import maintenance
            maintenance(harness).wake()
            return result
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(422, "Document import failed; check that the uploaded file is a valid supported document.") from exc
    finally:
        file.file.close()


@router.get("/documents/{document_id}")
@_snapshot_read
def get_document(request: Request, document_id: str, start: int = Query(0, ge=0),
                 max_chars: int = Query(12000, ge=1, le=50000), page: int | None = Query(None, ge=1),
                 include_sensitive: bool = False):
    harness = _harness(request)
    result = _call(tools.get_document, harness, document_id, page=page, start=start,
                   max_chars=max_chars, include_sensitive=include_sensitive)
    assignments = group_assignments(_library_groups(harness, include_sensitive))
    return {**result, "group_id": assignments.get("document:" + document_id)}


@router.get("/chunks/{chunk_id}")
@_snapshot_read
def get_chunk(request: Request, chunk_id: str, include_sensitive: bool = False):
    return _call(tools.get_document_chunk, _harness(request), chunk_id, include_sensitive=include_sensitive)


@router.post("/documents/{document_id}/propose")
def propose(request: Request, document_id: str, body: ProposeInput):
    return _call(tools.propose_import, _harness(request), document_id,
                 max_candidates=body.max_candidates, offset=body.offset, include_sensitive=body.include_sensitive)


@router.get("/review")
@_snapshot_read
def list_review(request: Request, status: Literal["pending", "approved", "rejected", "all"] = "pending",
                limit: int = Query(30, ge=1, le=100), offset: int = Query(0, ge=0),
                document_id: str | None = Query(None, min_length=1, max_length=100),
                include_sensitive: bool = False):
    return _call(tools.list_import_candidates, _harness(request), status=status, limit=limit,
                 offset=offset, document_id=document_id, include_sensitive=include_sensitive)


@router.post("/review/{candidate_id}")
def review(request: Request, candidate_id: str, body: ReviewInput):
    return _call(tools.review_import_candidate, _harness(request), candidate_id,
                 decision=body.decision, text=body.text, kind=body.kind, importance=body.importance,
                 include_sensitive=body.include_sensitive)


@router.get("/health")
def health(request: Request, include_sensitive: bool = False, verify_originals: bool = False):
    return _call(tools.library_health, _harness(request), include_sensitive=include_sensitive,
                 verify_originals=verify_originals)


@router.post("/find")
def find_evidence(request: Request, body: FindInput):
    """Find with an explicit browser opt-in to contextual additional questions."""
    from hunter_gatherer.memory.simple_search import find

    # Find owns preparation and read boundaries. An outer rollback-only snapshot
    # would invalidate continuation and repeatedly discard derived-index work.
    return _call(find, _harness(request), **body.model_dump())


@router.post("/find/context")
def copy_cited_context(request: Request, body: CitedContextInput):
    """Read current exact sources for copying; do not persist a collection."""
    from hunter_gatherer.memory.library_collections import export_cited_context

    return _call(export_cited_context, _harness(request).store, **body.model_dump())


@router.post("/evidence")
@_snapshot_read(prepare_search=True)
def evidence(request: Request, body: EvidenceInput):
    inspection = {"inspection_plan": body.inspection_plan.model_dump()} if body.inspection_plan is not None else {}
    budget = {"max_tokens": body.max_tokens} if body.max_tokens is not None else {}
    harness = _harness(request)
    result = _call(tools.evidence, harness, body.query, top_k=body.top_k,
                 required_fields=body.required_fields, evidence_requirements=body.evidence_requirements,
                 include_sensitive=body.include_sensitive, selection_strategy=body.selection_strategy,
                 response_format=body.response_format, source_discovery=body.source_discovery,
                 enrichment_discovery=body.enrichment_discovery, include_context=body.include_context,
                 lexical_only=body.lexical_only, memory_discovery=body.memory_discovery, **inspection, **budget)
    # The browser uses full evidence. Keep compact/bounded response contracts and
    # their existing budgets intact; routes remain discovery-only navigation.
    if body.response_format == "full" and getattr(harness.settings, "library_find_relationships", False):
        from hunter_gatherer.memory.relationship_retrieval import relationship_candidates
        seeds = [item["citation"] for item in result.get("supporting_passages", [])][:4]
        candidates, metadata = relationship_candidates(
            harness.store, body.query, seeds, include_sensitive=body.include_sensitive)
        if metadata and candidates:
            result["retrieval_paths"] = {**metadata, "paths": [path for _, path in candidates]}
    return result


@router.get("/graph")
@_snapshot_read
def graph(request: Request, query: str = Query("", max_length=4000), category: str = "",
          group: str = "",
          mode: Literal["default", "grouped"] = "default",
          limit: int = Query(150, ge=1, le=500), include_sensitive: bool = False):
    from hunter_gatherer.memory.library_graph import build_library_graph

    harness = _harness(request)
    visible_memories = list(_visible_memories(harness, include_sensitive))
    grouping = _library_groups(harness, include_sensitive, memories=visible_memories)
    assignments = group_assignments(grouping)
    memories = [memory for memory in visible_memories
                if (not category or memory.metadata.get("category") == category)
                and (not group or assignments.get("memory:" + memory.id) == group)
                and (not query or _matches(query, _metadata_search_text(memory)))]
    documents = [document for document in _documents(harness, query, include_sensitive)
                 if (not category or document["category"] == category)
                 and (not group or assignments.get("document:" + document["document_id"]) == group)]
    return build_library_graph(harness, memories, documents, limit=limit, include_sensitive=include_sensitive,
                               grouping=grouping, group=group, show_empty_groups=not query and not category,
                               mode=mode)


from hunter_gatherer.server.routers.library_sources import router as sources_router  # noqa: E402

router.include_router(sources_router)

from hunter_gatherer.server.routers.library_imports import router as imports_router  # noqa: E402

router.include_router(imports_router)
