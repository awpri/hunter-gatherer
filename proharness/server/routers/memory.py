"""Memory CRUD + search endpoints. Wraps MemoryStore + recall."""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

router = APIRouter()


class MemoryOut(BaseModel):
    id: str
    text: str
    type: str
    tags: list[str]
    importance: float
    created_at: float
    last_accessed: float
    access_count: int
    source: str
    metadata: dict


class MemoryIn(BaseModel):
    text: str
    type: str = "semantic"
    importance: float = 5.0
    tags: list[str] = []


class MemoryPatch(BaseModel):
    text: str | None = None
    importance: float | None = None
    tags: list[str] | None = None


def _store(request: Request):
    return request.app.state.loop.store


def _settings(request: Request):
    return request.app.state.settings


def _to_out(m) -> MemoryOut:
    return MemoryOut(
        id=m.id, text=m.text, type=m.type, tags=m.tags, importance=m.importance,
        created_at=m.created_at, last_accessed=m.last_accessed, access_count=m.access_count,
        source=m.source, metadata=m.metadata,
    )


@router.get("", response_model=list[MemoryOut])
def list_memories(
    request: Request,
    limit: int = Query(200, ge=1, le=2000),
    type: str | None = Query(None),
):
    store = _store(request)
    mems = store.list_all(limit=limit, type_filter=type if type else None)
    return [_to_out(m) for m in mems]


@router.get("/count")
def count(request: Request):
    return {"count": _store(request).count()}


@router.get("/search")
def search(
    request: Request,
    q: str = Query(..., description="Search query"),
    limit: int = Query(8, ge=1, le=50),
):
    """Raw hybrid vector search (dense + sparse, RRF-fused)."""
    hits = _store(request).vector_search(q, top_k=limit)
    return {
        "query": q,
        "count": len(hits),
        "hits": [
            {"id": h.id, "score": round(h.score, 3),
             "preview": h.payload.get("preview", ""), "type": h.payload.get("type"),
             "importance": h.payload.get("importance")}
            for h in hits
        ],
    }


@router.get("/recall")
def recall(
    request: Request,
    q: str = Query(..., description="What you want to remember"),
    limit: int = Query(5, ge=1, le=20),
):
    """Park-scored recall (recency + importance + relevance)."""
    from proharness.memory.recall import recall as _recall

    hits = _recall(_store(request), q, top_k=limit, touch=False, defer_touch=False)
    return {
        "query": q,
        "count": len(hits),
        "hits": [
            {"id": h.memory.id, "text": h.memory.text, "type": h.memory.type,
             "score": round(h.score, 3), "recency": round(h.recency, 3),
             "importance": round(h.importance, 3), "relevance": round(h.relevance, 3)}
            for h in hits
        ],
    }


@router.post("", response_model=MemoryOut)
def add_memory(request: Request, body: MemoryIn):
    """Store a memory with Mem0 ADD/UPDATE/DELETE reconciliation."""
    from proharness.memory.consolidate import Consolidator
    from proharness.memory.schemas import Memory

    store = _store(request)
    mem = Memory(text=body.text, type=body.type, importance=body.importance,
                 tags=body.tags, source="ui")
    cons = Consolidator(store=store, settings=_settings(request))
    decision = cons.decide(mem)
    stored = cons.apply(mem, decision)
    if stored is None:
        raise HTTPException(200, detail=f"redundant — {decision.reason}")
    return _to_out(stored)


@router.get("/{memory_id}", response_model=MemoryOut)
def get_memory(request: Request, memory_id: str):
    m = _store(request).get(memory_id)
    # Tombstoned memories are 404 over REST — the soft-delete row is an
    # internal recovery mechanism (store.restore), not a visible state.
    if m is None or m.deleted_at is not None:
        raise HTTPException(404, "memory not found")
    return _to_out(m)


@router.patch("/{memory_id}", response_model=MemoryOut)
def patch_memory(request: Request, memory_id: str, body: MemoryPatch):
    store = _store(request)
    m = store.get(memory_id)
    if m is None or m.deleted_at is not None:
        raise HTTPException(404, "memory not found")
    if body.text is not None:
        m.text = body.text
    if body.importance is not None:
        m.importance = body.importance
    if body.tags is not None:
        m.tags = body.tags
    store.update(m)
    return _to_out(m)


@router.delete("/{memory_id}")
def delete_memory(request: Request, memory_id: str):
    _store(request).delete(memory_id)
    return {"deleted": memory_id}


@router.get("/reflection/gauge")
def reflection_gauge(request: Request):
    """The pending-importance vs threshold gauge for the UI."""
    store = _store(request)
    settings = _settings(request)
    return {
        "pending": store.pending_importance(),
        "threshold": settings.reflect_importance_threshold,
        "last_reflection_ts": store.last_reflection_ts(),
        "ratio": (store.pending_importance() / settings.reflect_importance_threshold
                  if settings.reflect_importance_threshold > 0 else 0),
    }
