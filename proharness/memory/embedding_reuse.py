"""Store-local reuse of exact archive embedding inputs; never a content cache."""
from __future__ import annotations

import hashlib
import json
import math
from dataclasses import dataclass, field

from proharness.memory.document_visibility import visible_document_clause


class EmbeddingReuseRetryError(RuntimeError):
    """A staged donor changed; retry preparation outside the write transaction."""


@dataclass
class PreparedEmbeddings:
    vectors: list[list[float]]
    fingerprint: str
    stats: dict
    _donors: list[tuple] = field(default_factory=list, repr=False)
    _dimension: int = field(default=0, repr=False)
    _embedder: object = field(default=None, repr=False)


def _hash(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _fingerprint(embedder):
    value = embedder.fingerprint() if hasattr(embedder, "fingerprint") else ""
    return value if isinstance(value, str) and value.strip() else ""


def _dimension(embedder):
    value = (embedder.embedding_dimension() if hasattr(embedder, "embedding_dimension")
             else getattr(embedder, "dim", 0))
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 0


def _vector(value, dimension):
    if not isinstance(value, (list, tuple)) or not value:
        raise ValueError("Embedding vector must be a nonempty numeric sequence")
    if any(isinstance(x, (str, bytes, bool)) for x in value):
        raise ValueError("Embedding values must be numbers")
    try:
        values = [float(x) for x in value]
    except (ValueError, TypeError, OverflowError) as exc:
        raise ValueError("Embedding values must be finite float32 numbers") from exc
    if dimension and len(values) != dimension:
        raise ValueError("Embedding vector dimension does not match the target index")
    if any(not math.isfinite(x) or abs(x) > 3.4028234663852886e38 for x in values):
        raise ValueError("Embedding values must be finite float32 numbers")
    return values


def _initialize(store):
    with store.transaction():
        db = store._sqlite
        exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='archive_chunk_embedding_keys'").fetchone()
        db.execute("CREATE TABLE IF NOT EXISTS archive_chunk_embedding_keys(chunk_id TEXT PRIMARY KEY,text_hash TEXT NOT NULL)")
        db.execute("CREATE INDEX IF NOT EXISTS archive_embedding_hash ON archive_chunk_embedding_keys(text_hash)")
        db.execute("CREATE TABLE IF NOT EXISTS archive_embedding_dirty(chunk_id TEXT PRIMARY KEY)")
        db.execute("CREATE TRIGGER IF NOT EXISTS archive_embedding_insert AFTER INSERT ON archive_chunks BEGIN INSERT OR IGNORE INTO archive_embedding_dirty VALUES(new.id); END")
        db.execute("CREATE TRIGGER IF NOT EXISTS archive_embedding_text AFTER UPDATE OF id,text ON archive_chunks BEGIN DELETE FROM archive_chunk_embedding_keys WHERE chunk_id=old.id; DELETE FROM archive_embedding_dirty WHERE chunk_id=old.id; INSERT OR IGNORE INTO archive_embedding_dirty VALUES(new.id); END")
        db.execute("CREATE TRIGGER IF NOT EXISTS archive_embedding_delete AFTER DELETE ON archive_chunks BEGIN DELETE FROM archive_chunk_embedding_keys WHERE chunk_id=old.id; DELETE FROM archive_embedding_dirty WHERE chunk_id=old.id; END")
        if not exists:
            db.execute("INSERT OR IGNORE INTO archive_embedding_dirty SELECT id FROM archive_chunks")


def _sync(store, progress):
    while True:
        if progress:
            progress("indexing")
        with store.transaction():
            store.assert_embedder_current()
            rows = store._sqlite.execute("SELECT p.chunk_id,c.text FROM archive_embedding_dirty p LEFT JOIN archive_chunks c ON c.id=p.chunk_id LIMIT 128").fetchall()
            if not rows:
                return
            for row in rows:
                if row[1] is not None:
                    store._sqlite.execute("INSERT OR REPLACE INTO archive_chunk_embedding_keys VALUES(?,?)", (row[0], _hash(row[1])))
                else:
                    store._sqlite.execute("DELETE FROM archive_chunk_embedding_keys WHERE chunk_id=?", (row[0],))
                store._sqlite.execute("DELETE FROM archive_embedding_dirty WHERE chunk_id=?", (row[0],))


def _donor_sql(include_sensitive):
    return ("SELECT c.id,c.text,c.vector,c.embedder_fingerprint FROM archive_chunk_embedding_keys k "
            "JOIN archive_chunks c ON c.id=k.chunk_id JOIN archive_documents d ON d.id=c.document_id "
            "JOIN archive_sections s ON s.id=c.section_id AND s.document_id=c.document_id "
            "WHERE k.text_hash=? AND c.embedder_fingerprint=? AND c.vector IS NOT NULL "
            "AND c.start>=0 AND c.end>c.start AND c.end<=length(s.text) "
            "AND substr(s.text,c.start+1,c.end-c.start)=c.text AND "
            + visible_document_clause(include_sensitive, latest=False))


def prepare_embeddings(store, texts, *, enabled=True, include_sensitive=False, progress=None):
    """Stage complete vectors; misses are inferred outside store transactions."""
    texts = list(texts)
    store.assert_embedder_current()
    embedder = store.embedder
    fingerprint = _fingerprint(embedder)
    dimension = _dimension(embedder)
    unique = list(dict.fromkeys(texts))
    stats = dict(total_chunks=len(texts), embedded_unique=0, reused_unique=0,
                 duplicates=len(texts)-len(unique), invalid_donors=0, embedded_texts=0)
    found, donors = {}, []
    if enabled and fingerprint and dimension and texts:
        _initialize(store)
        _sync(store, progress)
        sql = _donor_sql(include_sensitive) + " LIMIT 32"
        for text in unique:
            if progress:
                progress("indexing")
            with store.transaction():
                store.assert_embedder_current()
                rows = store._sqlite.execute(sql, (_hash(text), fingerprint)).fetchall()
            for row in rows:
                if row[1] != text:
                    continue
                try:
                    values = _vector(json.loads(row[2]), dimension)
                except (ValueError, TypeError, OverflowError):
                    stats["invalid_donors"] += 1
                    continue
                found[text] = values
                donors.append((row[0], text, row[2], fingerprint))
                break
    stats["reused_unique"] = len(found)
    missing = [text for text in (unique if enabled else texts) if text not in found]
    inferred = []
    for offset in range(0, len(missing), 64):
        if progress:
            progress("indexing")
        batch = missing[offset:offset+64]
        response = list(embedder.embed_batch(batch))
        if len(response) != len(batch):
            raise ValueError("Embedding batch returned the wrong number of vectors")
        resolved = _dimension(embedder)
        if dimension and resolved and resolved != dimension:
            raise ValueError("Embedding dimension changed during preparation")
        dimension = dimension or resolved
        for vector in response:
            values = _vector(list(vector), dimension)
            dimension = dimension or len(values)
            inferred.append(values)
    stats["embedded_unique"] = len(set(missing))
    stats["embedded_texts"] = len(missing)
    if enabled:
        found.update(zip(missing, inferred, strict=True))
        vectors = [list(found[text]) for text in texts]
    else:
        vectors = inferred
    store.assert_embedder_current()
    if store.embedder is not embedder or _fingerprint(embedder) != fingerprint:
        raise EmbeddingReuseRetryError("Embedder changed during preparation; retry import")
    final_dimension = _dimension(embedder)
    if dimension and final_dimension and final_dimension != dimension:
        raise EmbeddingReuseRetryError("Embedding dimension changed during preparation; retry import")
    return PreparedEmbeddings(vectors, fingerprint, stats, donors, dimension, embedder)


def validate_reuse(store, prepared, *, include_sensitive=False):
    """Recheck donor dependencies inside the caller's final write transaction."""
    store.assert_embedder_current()
    if store.embedder is not prepared._embedder or _fingerprint(store.embedder) != prepared.fingerprint:
        raise EmbeddingReuseRetryError("Embedder changed during preparation; retry import")
    dimension = getattr(store.embedder, "dim", 0)
    if isinstance(dimension, int) and dimension > 0 and prepared._dimension and dimension != prepared._dimension:
        raise EmbeddingReuseRetryError("Embedding dimension changed during preparation; retry import")
    sql = _donor_sql(include_sensitive) + " AND c.id=?"
    for chunk_id, text, vector, fingerprint in prepared._donors:
        row = store._sqlite.execute(sql, (_hash(text), fingerprint, chunk_id)).fetchone()
        if row is None or row[1] != text or row[2] != vector or row[3] != fingerprint:
            raise EmbeddingReuseRetryError("Embedding donor changed or access was restricted; retry import")
