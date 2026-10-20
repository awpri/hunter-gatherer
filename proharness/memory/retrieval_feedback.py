"""Explicit feedback on an exact external retrieval, without storing its prose.

Receipts contain IDs, score components and a query digest. Feedback labels are
observations supplied by the caller, never an automatic judgement of truth.
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid

from proharness.memory.evidence import is_sensitive_memory

_SCHEMA = """
CREATE TABLE IF NOT EXISTS external_retrievals (
 id TEXT PRIMARY KEY, created_at REAL NOT NULL, query_key TEXT NOT NULL,
 candidates TEXT NOT NULL, include_sensitive INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS external_retrieval_feedback (
 retrieval_id TEXT PRIMARY KEY, request_key TEXT NOT NULL UNIQUE,
 fingerprint TEXT NOT NULL, response TEXT NOT NULL, created_at REAL NOT NULL
);
"""


def _initialize(store):
    with store._lock:
        if store._sqlite.in_transaction:
            raise ValueError("Retrieval receipts require a clean transaction boundary")
        store._sqlite.executescript(_SCHEMA)


def current_memory(store, memory_id: str, include_sensitive: bool = False, *, include_superseded: bool = False, memory=None):
    """Resolve an active fact and its current source references, fail closed."""
    memory = store.get(memory_id) if memory is None else memory
    if memory is not None and memory.id != memory_id:
        return None
    if memory is None or not memory.is_retrievable(include_superseded=include_superseded):
        return None
    if not include_sensitive and is_sensitive_memory(memory, store):
        return None
    workspace = memory.metadata.get("workspace_id")
    if workspace:
        from proharness.memory.workspace_scope import scope_visible
        if not isinstance(workspace, str) or not scope_visible(store, workspace, include_sensitive):
            return None
    refs = memory.metadata.get("evidence_refs", memory.metadata.get("sources", memory.metadata.get("evidence", [])))
    if isinstance(refs, dict):
        refs = [refs]
    if not isinstance(refs, list):
        return None
    if refs:
        from proharness.memory.archive_context import _ExistingArchiveReader

        with store._lock:
            tables = {row[0] for row in store._sqlite.execute(
                "SELECT name FROM sqlite_master WHERE type='table'"
            )}
            if not {"archive_documents", "archive_chunks", "archive_sections"} <= tables:
                return None
            archive = _ExistingArchiveReader(store)
            for ref in refs:
                if not isinstance(ref, dict) or not isinstance(ref.get("chunk_id"), str):
                    return None
                chunk = archive.get_chunk(ref["chunk_id"], include_sensitive=include_sensitive)
                if (chunk.get("error") or not chunk.get("chunk_id") or
                        ref.get("document_id") not in (None, chunk.get("document_id"))):
                    return None
                quote = ref.get("quote")
                if quote is not None and (not isinstance(quote, str) or
                                          not quote.strip() or quote not in chunk.get("quote", "")):
                    return None
    return memory


def issue_retrieval(store, query: str, hits: list[dict], *, include_sensitive: bool = False) -> str:
    _initialize(store)
    receipt_id = str(uuid.uuid4())
    candidates = [{"id": hit["id"], "recency": float(hit.get("recency", 0)),
                   "importance": float(hit.get("importance", 0)),
                   "relevance": float(hit.get("relevance", 0))} for hit in hits]
    with store._lock:
        store._sqlite.execute(
            "INSERT INTO external_retrievals VALUES (?,?,?,?,?)",
            (receipt_id, time.time(), hashlib.sha256(query.encode()).hexdigest(),
             json.dumps(candidates, allow_nan=False), int(include_sensitive)),
        )
        store._sqlite.commit()
    return receipt_id


def retrieval_feedback(store, retrieval_id: str, useful_ids: list[str],
                       not_useful_ids: list[str], idempotency_key: str,
                       include_sensitive: bool = False) -> dict:
    if not isinstance(include_sensitive, bool):
        raise ValueError("include_sensitive must be a boolean")
    for name, ids in (("useful_ids", useful_ids), ("not_useful_ids", not_useful_ids)):
        if (not isinstance(ids, list) or len(ids) > 100 or
                any(not isinstance(value, str) or not value for value in ids) or len(ids) != len(set(ids))):
            raise ValueError(f"{name} must contain at most 100 distinct record IDs")
    if set(useful_ids) & set(not_useful_ids) or not (useful_ids or not_useful_ids):
        raise ValueError("Provide at least one label, with no contradictory labels")
    if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key.strip()) <= 160:
        raise ValueError("idempotency_key must be 1–160 characters")
    payload = {"retrieval_id": retrieval_id, "useful_ids": sorted(useful_ids),
               "not_useful_ids": sorted(not_useful_ids)}
    fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()
    _initialize(store)
    with store._lock:
        store._sqlite.execute("BEGIN IMMEDIATE")
        try:
            row = store._sqlite.execute("SELECT * FROM external_retrievals WHERE id=?", (retrieval_id,)).fetchone()
            if not row:
                raise ValueError("Retrieval receipt not found")
            candidates = {hit["id"]: hit for hit in json.loads(row["candidates"])}
            labels = {key: 1 for key in useful_ids} | {key: 0 for key in not_useful_ids}
            if not set(labels) <= set(candidates):
                raise ValueError("Feedback must reference records returned in this retrieval")
            if any(current_memory(store, key, include_sensitive) is None for key in labels):
                raise ValueError("A referenced record is unavailable in the current privacy scope")
            old = store._sqlite.execute(
                "SELECT * FROM external_retrieval_feedback WHERE retrieval_id=? OR request_key=?",
                (retrieval_id, idempotency_key),
            ).fetchone()
            if old:
                if old["request_key"] != idempotency_key or old["fingerprint"] != fingerprint:
                    raise ValueError("Feedback conflict: this receipt or request key already has different feedback")
                result = json.loads(old["response"])
            else:
                for memory_id, used in labels.items():
                    hit = candidates[memory_id]
                    store._sqlite.execute("INSERT INTO recall_usage (ts, query, memory_id, used, recency, importance, relevance) VALUES (?,?,?,?,?,?,?)", (
                        time.time(), "external:" + row["query_key"], memory_id, used,
                        hit["recency"], hit["importance"], hit["relevance"],
                    ))
                result = {"recorded": True, "retrieval_id": retrieval_id, "labeled": len(labels),
                          "note": "Caller-reported usefulness, not independent verification."}
                store._sqlite.execute("INSERT INTO external_retrieval_feedback VALUES (?,?,?,?,?)", (
                    retrieval_id, idempotency_key, fingerprint, json.dumps(result), time.time(),
                ))
            store._sqlite.commit()
            return result
        except Exception:
            store._sqlite.rollback()
            raise
