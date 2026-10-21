"""The memory store: SQLite source-of-truth + Qdrant vectors.

Design (see docs/research.md for citations):

  • SQLite holds the full text + all metadata + audit fields. It's the
    source of truth and the cheap backup target (copy one file). Put the file
    on a NAS-mounted volume for remote durability.

  • Qdrant (on the NAS) holds, per memory: a dense vector, a sparse vector
    (BM25-style), and a small filterable payload. We use Qdrant's Query API
    with dense + sparse prefetches fused by RRF (k=2) — the safe default that
    needs no tuning set. Payload fields we filter on (type/tags/created_at) are
    indexed so filtering is true pre-filtering, not post.

  • If Qdrant isn't reachable, we transparently fall back to an in-process
    brute-force vector index (kept in memory + spilled to SQLite). This makes
    the harness usable before the NAS is stood up, and on a laptop offline.

This module never talks to an LLM directly — importance scoring and the
ADD/UPDATE/DELETE reconciliation live in consolidate.py / reflect.py, which
call into the store.
"""
from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager, suppress
from dataclasses import dataclass

from proharness.config import Settings
from proharness.memory.embeddings import Embedder, get_embedder
from proharness.memory.schemas import Memory, MemoryType, matches_filters
from proharness.optional_dependencies import OptionalDependencyError

log = logging.getLogger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS memories (
    id            TEXT PRIMARY KEY,
    text          TEXT NOT NULL,
    type          TEXT NOT NULL,
    tags          TEXT NOT NULL,           -- JSON array
    importance    REAL NOT NULL,
    created_at    REAL NOT NULL,
    last_accessed REAL NOT NULL,
    access_count  INTEGER NOT NULL,
    source        TEXT NOT NULL,
    metadata      TEXT NOT NULL            -- JSON object
);
CREATE INDEX IF NOT EXISTS idx_memories_type        ON memories(type);
CREATE INDEX IF NOT EXISTS idx_memories_created_at  ON memories(created_at);
CREATE INDEX IF NOT EXISTS idx_memories_importance  ON memories(importance);

-- Router outcome-feedback log (idea #1). One row per turn; outcome is the
-- cheap loop-derived signal (good/revised/max_steps/repeat), not LLM
-- introspection. The α-sweep regresses score → outcome; the accumulated rows
-- are also the training set for the documented "train a real router" upgrade.
CREATE TABLE IF NOT EXISTS router_decisions (
    ts         REAL NOT NULL,
    query      TEXT NOT NULL,
    tier       TEXT NOT NULL,            -- local | cheap | cloud
    model      TEXT NOT NULL,
    score      REAL,                     -- embedding similarity in [0,1]; NULL for rule/heuristic
    alpha      REAL NOT NULL,
    outcome    TEXT NOT NULL,            -- good | revised | max_steps | repeat
    in_tokens  INTEGER NOT NULL,
    out_tokens INTEGER NOT NULL,
    reason     TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_router_decisions_ts     ON router_decisions(ts);
CREATE INDEX IF NOT EXISTS idx_router_decisions_outcome ON router_decisions(outcome);

-- Reflection trigger state (idea #2 — Park 2023 importance-threshold trigger).
-- A running sum of the importance scores of newly-inserted memories since the
-- last reflection pass. When it crosses PROHARNESS_REFLECT_IMPORTANCE_THRESHOLD,
-- reflection fires (signal-driven, not schedule-driven). One row, keyed
-- constant; updated on every add().
CREATE TABLE IF NOT EXISTS reflection_state (
    key              TEXT PRIMARY KEY,
    pending_importance  REAL NOT NULL,
    last_reflection_ts  REAL NOT NULL
);

-- Visual pipelines (node-based AI orchestration). The full pipeline definition
-- (nodes, edges, inputs) is stored as JSON in `definition`.
CREATE TABLE IF NOT EXISTS pipelines (
    id          TEXT PRIMARY KEY,
    name        TEXT NOT NULL,
    slug        TEXT NOT NULL UNIQUE,
    description TEXT NOT NULL DEFAULT '',
    definition  TEXT NOT NULL,           -- JSON: {nodes, edges, inputs}
    version     INTEGER NOT NULL DEFAULT 1,
    created_at  REAL NOT NULL,
    updated_at  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pipelines_slug ON pipelines(slug);

-- Self-growing router exemplar set (2026-08-11 #1). Seeded from the constant
-- hand-picked list on first run; confirmed-good cloud-routed queries are
-- promoted into it, turning the static kNN reference set adaptive.
CREATE TABLE IF NOT EXISTS router_exemplars (
    id          TEXT PRIMARY KEY,
    text        TEXT NOT NULL,
    match_count INTEGER NOT NULL DEFAULT 0,
    created_at  REAL NOT NULL
);

-- Recall-usage feedback (2026-08-14 #1). One row per surfaced memory per
-- turn: was it actually *used* (label cited / re-fetched), plus the Park
-- sub-scores at injection time so the weight sweep can re-rank offline.
CREATE TABLE IF NOT EXISTS recall_usage (
    ts         REAL NOT NULL,
    query      TEXT NOT NULL,
    memory_id  TEXT NOT NULL,
    used       INTEGER NOT NULL,
    recency    REAL NOT NULL,
    importance REAL NOT NULL,
    relevance  REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_recall_usage_ts ON recall_usage(ts);

-- Store metadata (2026-08-13 #3): key/value rows, currently the embedder
-- fingerprint guard so an embedder swap can't silently break recall.
CREATE TABLE IF NOT EXISTS store_meta (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

-- Learned parameters (closed-loop persistence): append-only history of every
-- value the self-tuning loops applied (router α, Park weights, trigger
-- baselines). Hydrated on startup with env > stored > default precedence —
-- env always wins so an explicit PROHARNESS_* override disables autotune for
-- that knob; a stored value beats the default so tuning survives restarts.
CREATE TABLE IF NOT EXISTS learned_params (
    key    TEXT NOT NULL,
    value  TEXT NOT NULL,
    ts     REAL NOT NULL,
    source TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_learned_params_key_ts ON learned_params(key, ts);

-- Explicit human feedback (2026-08-31 #3): the gold-label channel the three
-- proxy flywheels (router_decisions, recall_usage, tool calls) never had.
-- rating ±1; optional correction text (routed through fact extraction as a
-- Mem0 contradiction candidate).
CREATE TABLE IF NOT EXISTS turn_feedback (
    ts          REAL NOT NULL,
    session_id  TEXT,
    decision_id INTEGER,
    rating      INTEGER NOT NULL,
    correction  TEXT
);
CREATE INDEX IF NOT EXISTS idx_turn_feedback_ts ON turn_feedback(ts);

-- Durable pipeline runs (2026-08-20 #3 durability): every unattended run gets
-- a row (status running/done/error/orphaned/abandoned) plus per-node
-- checkpoints, so a crashed run resumes from its last completed node, cron
-- restart behavior comes from persisted timestamps instead of memory, and
-- run history is observable.
CREATE TABLE IF NOT EXISTS pipeline_runs (
    id            TEXT PRIMARY KEY,
    pipeline_id   TEXT NOT NULL,
    pipeline_slug TEXT NOT NULL,
    status        TEXT NOT NULL,
    started_at    REAL NOT NULL,
    ended_at      REAL,
    stopped_reason TEXT,
    total_tokens  INTEGER NOT NULL DEFAULT 0,
    total_cost_usd REAL NOT NULL DEFAULT 0,
    error         TEXT,
    trigger       TEXT
);
CREATE INDEX IF NOT EXISTS idx_pipeline_runs_pipeline ON pipeline_runs(pipeline_id, started_at);

CREATE TABLE IF NOT EXISTS pipeline_node_results (
    run_id     TEXT NOT NULL,
    node_id    TEXT NOT NULL,
    kind       TEXT NOT NULL,
    outputs    TEXT,
    latency_ms REAL,
    tokens     INTEGER NOT NULL DEFAULT 0,
    cost_usd   REAL NOT NULL DEFAULT 0,
    error      TEXT,
    ts         REAL NOT NULL,
    PRIMARY KEY (run_id, node_id)
);

-- A-MEM associative links (2026-08-19 #2): each memory references its top-m
-- most-similar neighbors (captured at the dedup-search site), and recall
-- expands one hop along them — Zettelkasten recall-by-association.
CREATE TABLE IF NOT EXISTS memory_links (
    memory_id TEXT NOT NULL,
    link_id   TEXT NOT NULL,
    score     REAL NOT NULL,
    created_at REAL NOT NULL,
    PRIMARY KEY (memory_id, link_id)
);
CREATE INDEX IF NOT EXISTS idx_memory_links_src ON memory_links(memory_id);
"""


class VectorDimensionMismatchError(RuntimeError):
    """The persisted SQLite vector schema needs an explicit migration."""


@dataclass
class VectorSearchHit:
    id: str
    score: float
    payload: dict


class VectorBackend:
    """Abstract vector backend. QdrantBackend and InMemoryBackend implement it."""

    def upsert(self, memories: list[Memory], dense_vecs: list[list[float]]) -> None: ...

    def search(
        self,
        query_dense: list[float],
        query_text: str,
        top_k: int,
        filters: dict | None = None,
    ) -> list[VectorSearchHit]: ...

    def delete(self, ids: list[str]) -> None: ...

    def count(self) -> int: ...

    def close(self) -> None: ...


class InMemoryBackend(VectorBackend):
    """Brute-force cosine. Used as the transparent fallback when Qdrant is
    unavailable. Vectors live in memory; persistence is via SQLite only, so on
    restart they're re-embedded lazily."""

    def __init__(self, embedder: Embedder):
        self._embedder = embedder
        self._ids: list[str] = []
        self._vecs: list[list[float]] = []
        self._payloads: dict[str, dict] = {}
        self._lock = threading.Lock()

    def upsert(self, memories: list[Memory], dense_vecs: list[list[float]]) -> None:
        with self._lock:
            for m, v in zip(memories, dense_vecs, strict=True):
                if m.id in self._payloads:
                    # replace existing
                    try:
                        i = self._ids.index(m.id)
                        self._vecs[i] = v
                    except ValueError:
                        self._ids.append(m.id)
                        self._vecs.append(v)
                else:
                    self._ids.append(m.id)
                    self._vecs.append(v)
                self._payloads[m.id] = m.to_payload()

    def search(self, query_dense, query_text, top_k, filters=None):
        with self._lock:
            scored = []
            qnorm = _norm(query_dense)
            for mid, vec in zip(self._ids, self._vecs, strict=True):
                payload = self._payloads[mid]
                if filters and not _matches(payload, filters):
                    continue
                score = _dot(query_dense, vec) / (qnorm * _norm(vec) + 1e-9)
                scored.append(VectorSearchHit(id=mid, score=score, payload=payload))
            scored.sort(key=lambda h: -h.score)
            return scored[:top_k]

    def delete(self, ids):
        with self._lock:
            idset = set(ids)
            keep = [(i, v) for i, v in zip(self._ids, self._vecs, strict=True) if i not in idset]
            self._ids = [i for i, _ in keep]
            self._vecs = [v for _, v in keep]
            for i in idset:
                self._payloads.pop(i, None)

    def count(self):
        return len(self._ids)

    def close(self):
        pass


def _qdrant_modules():
    """Validate the configured optional backend before opening a library."""
    try:
        from qdrant_client import QdrantClient, models
    except ImportError as exc:
        raise OptionalDependencyError("Qdrant storage", "qdrant", "qdrant-client") from exc
    return QdrantClient, models


class QdrantBackend(VectorBackend):
    """Qdrant over HTTP (typically on the NAS). Uses dense + sparse named
    vectors with RRF fusion. Falls back / raises on connection error; the
    MemoryStore catches and switches to InMemoryBackend."""

    def __init__(self, settings: Settings, embedder: Embedder, dim: int):
        QdrantClient, models = _qdrant_modules()
        self._models = models
        self._client = QdrantClient(url=settings.qdrant_url, api_key=settings.qdrant_api_key or None)
        self._collection = settings.qdrant_collection
        self._settings = settings
        self._embedder = embedder
        self._dim = dim
        self._sparse_cache: dict[str, object] = {}
        self._ensure_collection()

    def _ensure_collection(self):
        models = self._models
        collections = self._client.get_collections().collections
        names = {c.name for c in collections}
        if self._collection not in names:
            self._client.create_collection(
                collection_name=self._collection,
                vectors_config={
                    "dense": models.VectorParams(size=self._dim, distance=models.Distance.COSINE),
                },
                sparse_vectors_config={
                    "sparse": models.SparseVectorParams(index=models.SparseIndexParams()),
                },
            )
            # Payload indexes for fast pre-filtering.
            for field in ("type", "created_at", "importance", "source"):
                with suppress(Exception):
                    self._client.create_payload_index(
                        collection_name=self._collection,
                        field_name=field,
                        field_schema=models.PayloadSchemaType.KEYWORD
                        if field in ("type", "source")
                        else models.PayloadSchemaType.FLOAT,
                    )
            log.info("created Qdrant collection %s", self._collection)

    # ── sparse (BM25-style) embeddings ───────────────────────────────────────
    def _sparse_for(self, text: str):
        """Qdrant SparseVector for hybrid retrieval. Prefer the embedder's
        learned sparse (bge-m3) when available; fall back to the BM25-ish
        deterministic sparse otherwise."""
        models = self._models
        # Learned sparse from the embedder (idea #1): real learned vectors for
        # bge-m3; None for dense-only models like bge-small.
        if self._embedder is not None and hasattr(self._embedder, "embed_sparse"):
            learned = self._embedder.embed_sparse(text)
            if learned is not None and learned.indices:
                return models.SparseVector(indices=learned.indices, values=learned.values)
        return self._sparse_bm25(text)

    def _sparse_bm25(self, text: str):
        """BM25-ish sparse vector: token id → weighted tf. Fallback for the
        sparse half of the RRF-fused hybrid query when the embedder doesn't
        supply learned sparse vectors (e.g. bge-small).

        Token ids are derived from ``hashlib.blake2b`` (NOT Python's built-in
        ``hash()``) so they're deterministic across interpreter restarts —
        Python randomizes string hashes per process (PYTHONHASHSEED), which
        would silently break hybrid retrieval across sessions. If the embedder
        supplies learned sparse vectors (``embed_sparse``), ``_sparse_for``
        uses those instead and this method isn't called.
        """
        models = self._models
        import hashlib
        import re

        tokens = re.findall(r"[a-z0-9]+", text.lower())
        if not tokens:
            return models.SparseVector(indices=[], values=[])
        counts: dict[int, float] = {}
        for tok in tokens:
            # Deterministic 32-bit id from a stable hash, mod a fixed vocab cap.
            tid = int.from_bytes(hashlib.blake2b(tok.encode(), digest_size=4).digest(), "big") % 250_000
            counts[tid] = counts.get(tid, 0.0) + 1.0
        # Log-weight (diminishing returns on repeated tokens).
        indices = sorted(counts)
        values = [1.0 + (counts[i] - 1) * 0.3 for i in indices]
        return models.SparseVector(indices=indices, values=values)

    def upsert(self, memories, dense_vecs):
        models = self._models
        points = []
        for m, dense in zip(memories, dense_vecs, strict=True):
            points.append(
                models.PointStruct(
                    id=m.id,
                    vector={"dense": dense, "sparse": self._sparse_for(m.text)},
                    payload=m.to_payload(),
                )
            )
        self._client.upsert(collection_name=self._collection, points=points, wait=True)

    def search(self, query_dense, query_text, top_k, filters=None):
        models = self._models
        must = []
        if filters:
            for k, v in filters.items():
                if isinstance(v, list):
                    must.append(models.FieldCondition(key=k, match=models.MatchAny(any=v)))
                else:
                    must.append(models.FieldCondition(key=k, match=models.MatchValue(value=v)))
        query_filter = models.Filter(must=must) if must else None

        # Hybrid: dense + sparse prefetches fused by RRF (k=2). Research: RRF
        # is the safe default when you have no tuning set. Sparse comes from
        # the embedder (bge-m3 learned) or the BM25-ish fallback (idea #1).
        result = self._client.query_points(
            collection_name=self._collection,
            prefetch=[
                models.Prefetch(query=self._sparse_for(query_text), using="sparse", limit=top_k * 3),
                models.Prefetch(query=query_dense, using="dense", limit=top_k * 3),
            ],
            query=models.RrfQuery(rrf=models.Rrf()),
            limit=top_k,
            query_filter=query_filter,
        )
        return [
            VectorSearchHit(id=str(p.id), score=p.score, payload=p.payload or {})
            for p in result.points
        ]

    def delete(self, ids):
        # Qdrant needs UUIDs or unsigned ints for point ids; we use UUID strings.
        try:
            self._client.delete(collection_name=self._collection, points_selector=self._models.PointIdsList(points=ids))
        except Exception:
            # Fallback: filter-based delete.
            self._client.delete(
                collection_name=self._collection,
                points_selector=self._models.FilterSelector(
                    filter=self._models.Filter(
                        must=[self._models.FieldCondition(key="id", match=self._models.MatchAny(any=ids))]
                    )
                ),
            )

    def count(self):
        try:
            return self._client.count(collection_name=self._collection, exact=True).count
        except Exception:
            return 0

    def close(self):
        with suppress(Exception):
            self._client.close()


class MemoryStore:
    """The store agents and tools talk to. Combines SQLite + a vector backend.

    For inserts, callers may pass `importance` explicitly; if not given and an
    `importance_scorer` is registered, the scorer (typically a cheap local LLM
    call) is invoked. This keeps the hot path cheap.
    """

    def __init__(
        self,
        settings: Settings,
        embedder: Embedder | None = None,
        importance_scorer=None,  # callable(text, type) -> float in [1,10]
        allow_embedder_mismatch: bool = False,  # rebuild-vectors path bypasses the guard
    ):
        self.settings = settings
        if allow_embedder_mismatch and settings.vector_backend == "qdrant":
            raise RuntimeError(
                "Atomic Qdrant rebuild is not supported. Create and verify a new collection "
                "before switching its alias, or explicitly select vector_backend=memory "
                "for a local in-memory index. The remote collection was not changed."
            )
        self.embedder = embedder or get_embedder(settings)
        if settings.vector_backend == "qdrant":
            _qdrant_modules()
        self.importance_scorer = importance_scorer
        self._sqlite = sqlite3.connect(settings.sqlite_uri(), uri=True, check_same_thread=False)
        self._sqlite.row_factory = sqlite3.Row
        self._lock = threading.RLock()
        # Touch buffer: recall() defers last_accessed/access_count bumps off the
        # hot path (idea #3). Cleared by flush_touches(), called once per turn.
        self._touch_buffer: set[str] = set()
        self._sqlite.executescript(_SCHEMA)
        self._sqlite.commit()
        self._ensure_deleted_at_column()  # tombstone migration for pre-existing DBs
        self.last_vector_rebuild_report: dict | None = None
        try:
            self._check_embedder_fingerprint(allow_mismatch=allow_embedder_mismatch)
            # Remember the database generation observed at opening, not the
            # live model identity: explicit startup mismatch=warn semantics
            # remain unchanged, while a later external migration is detected.
            self._expected_embedder_fingerprint = self.get_meta("embedder_fingerprint")
            if allow_embedder_mismatch:
                # Maintenance must open an old-dimension index without warming,
                # replacing, or silently falling back from its persistent backend.
                if settings.vector_backend == "sqlite_vec":
                    from proharness.memory.vec_sqlite import SqliteVecBackend

                    self._backend = SqliteVecBackend(
                        self, self.embedder, dim=self.embedder.dim, initialize=False,
                    )
                else:
                    self._backend = InMemoryBackend(self.embedder)
            else:
                self._backend = self._init_backend()
                self._warm_cache()
            self.assert_embedder_current()
        except Exception:
            self._sqlite.close()
            raise

    # ── embedder fingerprint guard (2026-08-13 #3) ───────────────────────────
    def _check_embedder_fingerprint(self, *, allow_mismatch: bool) -> None:
        """Persist the live embedder's fingerprint on first use; on mismatch,
        warn (default) or raise per PROHARNESS_EMBEDDER_MISMATCH. Prevents the
        documented bge-small→bge-m3 swap from silently wrecking recall: query
        vectors embedded at a new dim/model cosine-searched against stale
        vectors return noise with no error. The fix path is
        `proharness memory rebuild-vectors`."""
        fp = None
        with suppress(Exception):
            fp = self.embedder.fingerprint() if hasattr(self.embedder, "fingerprint") else None
        if not fp:
            return
        stored = self.get_meta("embedder_fingerprint")
        if stored is None:
            if not allow_mismatch:
                self.set_meta("embedder_fingerprint", fp)
        elif stored != fp and not allow_mismatch:
            msg = (
                f"embedder fingerprint mismatch: store was built with '{stored}' but the "
                f"live embedder is '{fp}'. Queries would silently return noise. "
                "Run `proharness memory rebuild-vectors` to re-embed from the SQLite "
                "source of truth, or set PROHARNESS_EMBEDDER_MISMATCH=warn to bypass."
            )
            mode = getattr(self.settings, "embedder_mismatch", "warn")
            if mode == "error":
                raise RuntimeError(msg)
            log.warning(msg)

    def assert_embedder_current(self) -> None:
        """Reject an already-open connection after an external model migration.

        This catches completed rebuilds before reads or writes use stale
        embeddings. It is not online migration coordination: stop writers
        during a rebuild, then reconnect them with the selected model.
        """
        with self._lock:
            if self.get_meta("embedder_fingerprint") != self._expected_embedder_fingerprint:
                raise RuntimeError(
                    "Embedding index changed since this store was opened. "
                    "Reconnect ProHarness with the migrated embedding model before reading or writing; "
                    "stop all writers before running another embedding migration."
                )

    def get_meta(self, key: str) -> str | None:
        row = self._sqlite.execute("SELECT value FROM store_meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._lock:
            self._sqlite.execute("INSERT OR REPLACE INTO store_meta (key, value) VALUES (?,?)", (key, value))
            self._sqlite.commit()

    # ── durable pipeline runs (checkpoints + history) ────────────────────────
    def start_pipeline_run(self, run_id: str, pipeline_id: str, pipeline_slug: str,
                           trigger: str = "") -> None:
        with self._lock:
            self._sqlite.execute(
                "INSERT INTO pipeline_runs (id, pipeline_id, pipeline_slug, status, started_at, trigger) "
                "VALUES (?,?,?,?,?,?)",
                (run_id, pipeline_id, pipeline_slug, "running", time.time(), trigger),
            )
            self._sqlite.commit()

    def finish_pipeline_run(self, run_id: str, status: str, *, stopped_reason: str = "",
                            total_tokens: int = 0, total_cost_usd: float = 0.0,
                            error: str | None = None) -> None:
        with self._lock:
            self._sqlite.execute(
                "UPDATE pipeline_runs SET status = ?, ended_at = ?, stopped_reason = ?, "
                "total_tokens = ?, total_cost_usd = ?, error = ? WHERE id = ?",
                (status, time.time(), stopped_reason, int(total_tokens),
                 float(total_cost_usd), error, run_id),
            )
            self._sqlite.commit()

    def checkpoint_pipeline_node(self, run_id: str, node_id: str, kind: str,
                                 outputs: dict, latency_ms: float, tokens: int,
                                 cost_usd: float, error: str | None = None) -> None:
        import json as _json

        with self._lock:
            self._sqlite.execute(
                "INSERT OR REPLACE INTO pipeline_node_results "
                "(run_id, node_id, kind, outputs, latency_ms, tokens, cost_usd, error, ts) "
                "VALUES (?,?,?,?,?,?,?,?,?)",
                (run_id, node_id, kind, _json.dumps(outputs, default=str),
                 float(latency_ms), int(tokens), float(cost_usd), error, time.time()),
            )
            self._sqlite.commit()

    def pipeline_node_checkpoints(self, run_id: str) -> dict[str, dict]:
        """node_id → outputs dict for every completed node in the run."""
        import json as _json

        rows = self._sqlite.execute(
            "SELECT node_id, outputs FROM pipeline_node_results "
            "WHERE run_id = ? AND error IS NULL",
            (run_id,),
        ).fetchall()
        out: dict[str, dict] = {}
        for r in rows:
            with suppress(Exception):
                out[r[0]] = _json.loads(r[1]) if r[1] else {}
        return out

    def fetch_pipeline_runs(self, limit: int = 50, pipeline_id: str | None = None) -> list[dict]:
        if pipeline_id:
            rows = self._sqlite.execute(
                "SELECT id, pipeline_id, pipeline_slug, status, started_at, ended_at, "
                "stopped_reason, total_tokens, total_cost_usd, error, trigger "
                "FROM pipeline_runs WHERE pipeline_id = ? ORDER BY started_at DESC LIMIT ?",
                (pipeline_id, int(limit)),
            ).fetchall()
        else:
            rows = self._sqlite.execute(
                "SELECT id, pipeline_id, pipeline_slug, status, started_at, ended_at, "
                "stopped_reason, total_tokens, total_cost_usd, error, trigger "
                "FROM pipeline_runs ORDER BY started_at DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
        keys = ("id", "pipeline_id", "pipeline_slug", "status", "started_at", "ended_at",
                "stopped_reason", "total_tokens", "total_cost_usd", "error", "trigger")
        return [dict(zip(keys, tuple(r), strict=True)) for r in rows]

    def last_pipeline_run_start(self, pipeline_id: str) -> float | None:
        """Most recent run start (any status) — the persisted trigger state so
        cron/interval restart behavior is deliberate, not accidental."""
        row = self._sqlite.execute(
            "SELECT MAX(started_at) FROM pipeline_runs WHERE pipeline_id = ?",
            (pipeline_id,),
        ).fetchone()
        return float(row[0]) if row and row[0] is not None else None

    def orphaned_pipeline_runs(self) -> list[dict]:
        """Runs left 'running' by a dead process — resumable via checkpoints."""
        rows = self._sqlite.execute(
            "SELECT id, pipeline_id, pipeline_slug FROM pipeline_runs WHERE status = 'running'"
        ).fetchall()
        return [{"id": r[0], "pipeline_id": r[1], "pipeline_slug": r[2]} for r in rows]

    # ── answer cache (2026-09-01 #1) ────────────────────────────────────────
    def _ensure_answer_cache_table(self) -> None:
        self._sqlite.execute(
            "CREATE TABLE IF NOT EXISTS answer_cache ("
            "query TEXT PRIMARY KEY, answer TEXT NOT NULL, "
            "cited_mem_ids TEXT NOT NULL, ts REAL NOT NULL, "
            "hits INTEGER NOT NULL DEFAULT 0)"
        )
        self._sqlite.commit()

    def get_cached_answer(self, query: str) -> tuple | None:
        """(query, answer, cited_json, ts, hits) for an exact query, or None."""
        with suppress(Exception):
            self._ensure_answer_cache_table()
            row = self._sqlite.execute(
                "SELECT query, answer, cited_mem_ids, ts, hits FROM answer_cache WHERE query = ?",
                (query,),
            ).fetchone()
            return tuple(row) if row else None
        return None

    def all_cached_answers(self, limit: int = 256) -> list[tuple]:
        """Recent cache entries (newest first) for semantic lookup scans."""
        with suppress(Exception):
            self._ensure_answer_cache_table()
            rows = self._sqlite.execute(
                "SELECT query, answer, cited_mem_ids, ts, hits FROM answer_cache "
                "ORDER BY ts DESC LIMIT ?",
                (int(limit),),
            ).fetchall()
            return [tuple(r) for r in rows]
        return []

    def upsert_cached_answer(self, query: str, answer: str, cited_json: str,
                             ts: float, *, cap: int = 256) -> None:
        """Insert/replace a cache entry; LRU-prune beyond cap."""
        with suppress(Exception), self._lock:
            self._ensure_answer_cache_table()
            self._sqlite.execute(
                "INSERT OR REPLACE INTO answer_cache (query, answer, cited_mem_ids, ts, hits) "
                "VALUES (?,?,?,?,0)",
                (query, answer, cited_json, ts),
            )
            if cap > 0:
                self._sqlite.execute(
                    "DELETE FROM answer_cache WHERE ts < ?",
                    (self._cache_cutoff_ts(cap),),
                )
            self._sqlite.commit()

    def _cache_cutoff_ts(self, cap: int) -> float:
        """The ts of the cap-th newest entry (entries strictly older prune).
        -inf when at-or-under cap — nothing to prune."""
        row = self._sqlite.execute(
            "SELECT ts FROM answer_cache ORDER BY ts DESC LIMIT 1 OFFSET ?", (cap - 1,)
        ).fetchone()
        return float(row[0]) if row else float("-inf")

    def bump_cached_answer(self, query: str) -> None:
        with suppress(Exception):
            self._sqlite.execute(
                "UPDATE answer_cache SET hits = hits + 1 WHERE query = ?", (query,)
            )
            self._sqlite.commit()

    def delete_cached_answer(self, query: str) -> None:
        with suppress(Exception):
            self._sqlite.execute("DELETE FROM answer_cache WHERE query = ?", (query,))
            self._sqlite.commit()

    def _invalidate_cached_memory(self, memory_id: str) -> None:
        """Invalidate cited answers inside the caller's memory-write transaction.

        Direct forget/purge/update paths must obey the same cache coherence as
        reflection. The optional cache table does not exist before first use.
        """
        exists = self._sqlite.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='answer_cache'",
        ).fetchone()
        if exists:
            self._sqlite.execute(
                "DELETE FROM answer_cache WHERE EXISTS ("
                "SELECT 1 FROM json_each(CASE WHEN json_valid(cited_mem_ids) "
                "THEN cited_mem_ids ELSE '[]' END) WHERE value = ?)",
                (memory_id,),
            )

    def answer_cache_stats(self) -> dict:
        with suppress(Exception):
            self._ensure_answer_cache_table()
            row = self._sqlite.execute(
                "SELECT COUNT(*), COALESCE(SUM(hits), 0) FROM answer_cache"
            ).fetchone()
            return {"entries": int(row[0]), "total_hits": int(row[1])}
        return {"entries": 0, "total_hits": 0}

    # ── sqlite-vec backend SQL (2026-08-12 #3) ───────────────────────────────
    # The vec0 column type takes a literal dimension (DDL cannot use "?"
    # placeholders), so creation uses a whitelist of pre-written literal DDL
    # statements keyed by supported embedding dimension — no interpolation.
    # Keys: 384 = bge-small (default), 1024 = bge-m3, 768 = MiniLM/nomic,
    # 64/128/256/512 = deterministic/test embedders.
    _VEC_DDL: dict[int, str] = {
        64: "CREATE VIRTUAL TABLE IF NOT EXISTS memory_vec USING vec0(id TEXT PRIMARY KEY, embedding float[64])",
        128: "CREATE VIRTUAL TABLE IF NOT EXISTS memory_vec USING vec0(id TEXT PRIMARY KEY, embedding float[128])",
        256: "CREATE VIRTUAL TABLE IF NOT EXISTS memory_vec USING vec0(id TEXT PRIMARY KEY, embedding float[256])",
        384: "CREATE VIRTUAL TABLE IF NOT EXISTS memory_vec USING vec0(id TEXT PRIMARY KEY, embedding float[384])",
        512: "CREATE VIRTUAL TABLE IF NOT EXISTS memory_vec USING vec0(id TEXT PRIMARY KEY, embedding float[512])",
        768: "CREATE VIRTUAL TABLE IF NOT EXISTS memory_vec USING vec0(id TEXT PRIMARY KEY, embedding float[768])",
        1024: "CREATE VIRTUAL TABLE IF NOT EXISTS memory_vec USING vec0(id TEXT PRIMARY KEY, embedding float[1024])",
    }

    def vec_ensure_table(self, dim: int) -> None:
        """Create the vec0 table for a supported dim (literal DDL whitelist;
        unsupported dims raise so the store falls back to in-memory)."""
        ddl = self._VEC_DDL.get(int(dim))
        if ddl is None:
            raise ValueError(
                f"sqlite_vec backend: no pre-built vec0 DDL for embedding dim {dim} "
                f"(supported: {sorted(self._VEC_DDL)}); falling back to the in-memory backend"
            )
        with self._lock:
            previous = self.vec_table_dimension()
            if previous is not None and previous != int(dim):
                raise VectorDimensionMismatchError(
                    f"SQLite vector dimension is {previous}, but the embedder uses {dim}. "
                    "Run `proharness memory rebuild-vectors` before using this model."
                )
            self._sqlite.execute(ddl)
            self._sqlite.commit()

    def vec_table_dimension(self) -> int | None:
        row = self._sqlite.execute(
            "SELECT sql FROM sqlite_master WHERE name = 'memory_vec' AND type = 'table'"
        ).fetchone()
        if row is None:
            return None
        match = re.search(r"embedding\s+float\[(\d+)\]", row[0] or "", re.IGNORECASE)
        if match is None:
            raise VectorDimensionMismatchError("Unrecognized SQLite memory vector schema; restore a verified backup")
        return int(match.group(1))

    def vec_upsert(self, rows: list[tuple]) -> None:
        """Atomically insert/update (id, embedding_json) pairs in vec0.

        vec0 does not implement SQLite's INSERT OR REPLACE conflict policy for
        its text primary key. Native UPDATE followed by INSERT for missing IDs
        supports repeated metadata updates and embedding replacement safely.
        """
        with self._lock:
            self._sqlite.execute("SAVEPOINT memory_vec_upsert")
            try:
                for memory_id, vector_json in rows:
                    updated = self._sqlite.execute(
                        "UPDATE memory_vec SET embedding = ? WHERE id = ?",
                        (vector_json, memory_id),
                    )
                    if updated.rowcount == 0:
                        self._sqlite.execute(
                            "INSERT INTO memory_vec(id, embedding) VALUES (?, ?)",
                            (memory_id, vector_json),
                        )
                self._sqlite.execute("RELEASE SAVEPOINT memory_vec_upsert")
            except Exception:
                self._sqlite.execute("ROLLBACK TO SAVEPOINT memory_vec_upsert")
                self._sqlite.execute("RELEASE SAVEPOINT memory_vec_upsert")
                raise

    def vec_delete(self, ids: list[str]) -> None:
        with self._lock:
            self._sqlite.executemany("DELETE FROM memory_vec WHERE id = ?", [(i,) for i in ids])
            self._sqlite.commit()

    def vec_search(self, query_json: str, k: int) -> list[tuple]:
        """(id, distance) rows, best-first (L2 on normalized vectors)."""
        if k > 4096:
            # vec0 KNN has a 4096-neighbor limit. Filter backfill may need to
            # pass that boundary; an exact scan preserves completeness there.
            return [tuple(r) for r in self._sqlite.execute(
                "SELECT id, vec_distance_L2(embedding, ?) AS distance FROM memory_vec "
                "ORDER BY distance LIMIT ?", (query_json, int(k)),
            ).fetchall()]
        return [tuple(r) for r in self._sqlite.execute(
            "SELECT id, distance FROM memory_vec WHERE embedding MATCH ? AND k = ? ORDER BY distance",
            (query_json, int(k)),
        ).fetchall()]

    def vec_count(self) -> int:
        row = self._sqlite.execute("SELECT COUNT(*) FROM memory_vec").fetchone()
        return int(row[0]) if row else 0

    # ── model-arms bandit (2026-09-01 #3) ────────────────────────────────────
    def _ensure_model_arms_table(self) -> None:
        with suppress(Exception):
            self._sqlite.execute(
                "CREATE TABLE IF NOT EXISTS model_arms ("
                "tier TEXT NOT NULL, model TEXT NOT NULL, "
                "pulls INTEGER NOT NULL DEFAULT 0, "
                "reward_sum REAL NOT NULL DEFAULT 0, "
                "cost_sum REAL NOT NULL DEFAULT 0, "
                "PRIMARY KEY (tier, model))"
            )
            self._sqlite.commit()

    def fetch_model_arms(self, tier: str) -> list[tuple]:
        """(model, pulls, reward_sum, cost_sum) rows for a tier."""
        with suppress(Exception):
            self._ensure_model_arms_table()
            rows = self._sqlite.execute(
                "SELECT model, pulls, reward_sum, cost_sum FROM model_arms WHERE tier = ?",
                (tier,),
            ).fetchall()
            return [tuple(r) for r in rows]
        return []

    def upsert_model_arm(self, tier: str, model: str, reward: float, cost: float) -> None:
        """Record one pull (reward + cost) for a (tier, model) arm."""
        with suppress(Exception), self._lock:
            self._ensure_model_arms_table()
            self._sqlite.execute(
                "INSERT INTO model_arms (tier, model, pulls, reward_sum, cost_sum) "
                "VALUES (?,?,1,?,?) ON CONFLICT(tier, model) DO UPDATE SET "
                "pulls = pulls + 1, reward_sum = reward_sum + excluded.reward_sum, "
                "cost_sum = cost_sum + excluded.cost_sum",
                (tier, model, reward, cost),
            )
            self._sqlite.commit()

    def model_arms_table(self, tier: str | None = None) -> list[dict]:
        """All arm statistics (observability / the λ sweep)."""
        with suppress(Exception):
            self._ensure_model_arms_table()
            if tier:
                rows = self._sqlite.execute(
                    "SELECT tier, model, pulls, reward_sum, cost_sum FROM model_arms "
                    "WHERE tier = ? ORDER BY pulls DESC", (tier,)).fetchall()
            else:
                rows = self._sqlite.execute(
                    "SELECT tier, model, pulls, reward_sum, cost_sum FROM model_arms "
                    "ORDER BY tier, pulls DESC").fetchall()
            keys = ("tier", "model", "pulls", "reward_sum", "cost_sum")
            return [dict(zip(keys, tuple(r), strict=True)) for r in rows]
        return []

    # ── learned parameters (append-only; closed-loop persistence) ───────────
    def set_learned_param(self, key: str, value, source: str = "autotune") -> None:
        """Append a tuned value to the history log. Never overwrites — the
        latest row per key wins at hydration, and the full history is kept
        (the audit trail of what the loops learned and when)."""
        import json
        import time as _time

        raw = value if isinstance(value, str) else json.dumps(value)
        with self._lock:
            self._sqlite.execute(
                "INSERT INTO learned_params (key, value, ts, source) VALUES (?,?,?,?)",
                (key, raw, _time.time(), source),
            )
            self._sqlite.commit()

    def latest_learned_param(self, key: str) -> str | None:
        """Newest stored value for a key, or None when never tuned."""
        row = self._sqlite.execute(
            "SELECT value FROM learned_params WHERE key = ? ORDER BY ts DESC LIMIT 1", (key,)
        ).fetchone()
        return row[0] if row else None

    def learned_param_history(self, key: str, limit: int = 50) -> list[tuple]:
        """(ts, value, source) rows for a key, newest first — the tuning
        audit trail."""
        rows = self._sqlite.execute(
            "SELECT ts, value, source FROM learned_params WHERE key = ? ORDER BY ts DESC LIMIT ?",
            (key, int(limit)),
        ).fetchall()
        return [(r[0], r[1], r[2]) for r in rows]

    def _init_backend(self) -> VectorBackend:
        choice = getattr(self.settings, "vector_backend", "qdrant")
        if choice == "memory":
            return InMemoryBackend(self.embedder)
        if choice == "sqlite_vec":
            # Local-only single-file deployment (2026-08-12 #3): indexed ANN in
            # the same SQLite DB, no external service. Falls back to in-memory
            # when the optional sqlite-vec package isn't installed or the
            # embedding dim has no pre-built vec0 DDL.
            try:
                from proharness.memory.vec_sqlite import SqliteVecBackend

                # The production embedder resolves its dim lazily on first
                # embed — warm it so the backend is chosen with a real dim
                # (otherwise dim=0 silently falls back to in-memory).
                if not self.embedder.dim:
                    with suppress(Exception):
                        self.embedder.embed("warmup")
                b = SqliteVecBackend(self, self.embedder, dim=self.embedder.dim)
                log.info("using sqlite-vec backend (local-only, dim=%d)", self._dim_of(b))
                return b
            except VectorDimensionMismatchError:
                raise
            except Exception as e:
                log.warning("sqlite-vec unavailable (%s); using in-memory vector backend", e)
                return InMemoryBackend(self.embedder)
        try:
            b = QdrantBackend(self.settings, self.embedder, dim=self.embedder.dim)
            log.info("using Qdrant backend at %s", self.settings.qdrant_url)
            return b
        except OptionalDependencyError:
            # Installing a smaller dependency set must not select a different
            # backend than the one configured by an existing library.
            raise
        except Exception as e:
            log.warning("Qdrant unavailable (%s); using in-memory vector backend", e)
            return InMemoryBackend(self.embedder)

    @staticmethod
    def _dim_of(backend) -> int:
        return getattr(backend, "_dim", 0)

    def _warm_cache(self):
        """If we fell back to in-memory, pre-populate vectors from SQLite so
        recall still works."""
        if isinstance(self._backend, InMemoryBackend):
            rows = list(self._iter_sql())
            if rows:
                texts = [r.text for r in rows]
                vecs = self.embedder.embed_batch(texts)
                self._backend.upsert(rows, vecs)

    # ── writes ───────────────────────────────────────────────────────────────
    def _ensure_deleted_at_column(self) -> None:
        """In-place migration: pre-tombstone DBs lack the deleted_at column
        (durable-delete safety). Idempotent."""
        with self._lock:
            cols = {r[1] for r in self._sqlite.execute("PRAGMA table_info(memories)").fetchall()}
            if cols and "deleted_at" not in cols:
                self._sqlite.execute("ALTER TABLE memories ADD COLUMN deleted_at REAL")
                self._sqlite.commit()

    def add(self, memory: Memory) -> Memory:
        """Insert (or upsert) a memory. Does NOT do Mem0-style dedup — see
        consolidate.py for that. Use this when you know the memory is new."""
        self.assert_embedder_current()
        self._ensure_deleted_at_column()
        with self._lock:
            if memory.importance == 0.0 and self.importance_scorer:
                try:
                    memory.importance = float(self.importance_scorer(memory.text, memory.type))
                except Exception:
                    memory.importance = 5.0
            self.assert_embedder_current()
            self._sqlite.execute(
                "INSERT OR REPLACE INTO memories VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                memory.to_sql_row(),
            )
            self._invalidate_cached_memory(memory.id)
            self._accumulate_importance(memory.importance)
            self._sqlite.commit()
            self._backend.upsert([memory], self.embedder.embed_batch([memory.text]))
        return memory

    def update(self, memory: Memory) -> None:
        self.add(memory)  # INSERT OR REPLACE handles upsert.

    def supersede(self, old_id: str, replacement: Memory) -> Memory:
        """Explicitly correct a fact with an atomic SQLite history transition.

        Prepare embeddings and stage the new vector first. Until the SQLite
        insert commits, retrieval rejects that orphan vector. SQL failures leave
        the old fact active and never leave an unlinked replacement record.
        """
        self.assert_embedder_current()
        if replacement.id == old_id or not replacement.is_retrievable():
            raise ValueError("Replacement must be a new, active memory")
        replacement.metadata = {**replacement.metadata, "supersedes": old_id}
        vectors = self.embedder.embed_batch([replacement.text])
        with self._lock:
            self.assert_embedder_current()
            if self.get(replacement.id) is not None:
                raise ValueError("Replacement memory ID already exists")
            self._backend.upsert([replacement], vectors)
            try:
                self._sqlite.execute("BEGIN IMMEDIATE")
                old = self.get(old_id)
                if old is None or not old.is_retrievable():
                    raise ValueError("Active memory not found")
                self._sqlite.execute(
                    "INSERT INTO memories VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    replacement.to_sql_row(),
                )
                old.metadata = {**old.metadata, "superseded_by": replacement.id,
                                "valid_to": time.time()}
                self._sqlite.execute(
                    "UPDATE memories SET metadata = ? WHERE id = ?",
                    (json.dumps(old.metadata), old.id),
                )
                self._invalidate_cached_memory(old.id)
                self._accumulate_importance(replacement.importance)
                self._sqlite.commit()
            except Exception:
                self._sqlite.rollback()
                with suppress(Exception):
                    self._backend.delete([replacement.id])
                raise
        return replacement

    def delete(self, memory_id: str) -> None:
        """SOFT delete (durable-delete safety): tombstone the row (deleted_at)
        and remove its vectors so recall stops surfacing it — the full text +
        metadata stay in SQLite, recoverable via :meth:`restore`. One bad
        reflection pass can no longer destroy memories. :meth:`purge` is the
        only hard delete."""
        self._ensure_deleted_at_column()
        with self._lock:
            self._sqlite.execute(
                "UPDATE memories SET deleted_at = ? WHERE id = ? AND deleted_at IS NULL",
                (time.time(), memory_id),
            )
            self._invalidate_cached_memory(memory_id)
            self._sqlite.commit()
        self._backend.delete([memory_id])

    def purge(self, memory_id: str) -> bool:
        """HARD delete (row + vectors). Returns True if a row was removed.
        Reserved for explicit erasure (privacy); everything else tombstones."""
        with self._lock:
            cur = self._sqlite.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
            self._invalidate_cached_memory(memory_id)
            self._sqlite.commit()
        self._backend.delete([memory_id])
        return cur.rowcount > 0

    def restore(self, memory_id: str) -> Memory | None:
        """Clear a tombstone and re-index the memory. Returns the restored
        Memory, or None when the id doesn't exist (or was purged)."""
        self.assert_embedder_current()
        self._ensure_deleted_at_column()
        with self._lock:
            self.assert_embedder_current()
            self._sqlite.execute(
                "UPDATE memories SET deleted_at = NULL WHERE id = ?", (memory_id,)
            )
            self._sqlite.commit()
        mem = self.get(memory_id)
        if mem is not None:
            self._backend.upsert([mem], self.embedder.embed_batch([mem.text]))
        return mem

    def touch(self, memory_id: str, *, defer: bool = False) -> None:
        """Mark a memory as accessed (bumps last_accessed + access_count).

        With ``defer=True`` the bump is buffered and applied once by
        :meth:`flush_touches` — used by recall() to keep the hot path
        read-only (idea #3). Recency scoring is hourly-granular, so sub-second
        deferral changes nothing mathematically.
        """
        if defer:
            self._touch_buffer.add(memory_id)
            return
        with self._lock:
            self._sqlite.execute(
                "UPDATE memories SET last_accessed = ?, access_count = access_count + 1 WHERE id = ?",
                (time.time(), memory_id),
            )
            self._sqlite.commit()

    def flush_touches(self) -> int:
        """Apply all deferred touches in a single transaction. Returns the
        number of memories touched. Safe to call when the buffer is empty."""
        if not self._touch_buffer:
            return 0
        now = time.time()
        ids = list(self._touch_buffer)
        self._touch_buffer.clear()
        with self._lock:
            self._sqlite.executemany(
                "UPDATE memories SET last_accessed = ?, access_count = access_count + 1 WHERE id = ?",
                [(now, i) for i in ids],
            )
            self._sqlite.commit()
        return len(ids)

    # ── reads ────────────────────────────────────────────────────────────────
    def get(self, memory_id: str) -> Memory | None:
        """Id lookup — returns tombstoned rows too (restore + audit need
        them). Use Memory.is_retrievable for any user-facing retrieval."""
        row = self._sqlite.execute("SELECT * FROM memories WHERE id = ?", (memory_id,)).fetchone()
        if row is None:
            return None
        return Memory.from_sql_row(tuple(row))

    def list_all(self, limit: int = 200, type_filter: MemoryType | None = None) -> list[Memory]:
        """Live (non-tombstoned) memories, newest first."""
        if type_filter:
            rows = self._sqlite.execute(
                "SELECT * FROM memories WHERE deleted_at IS NULL AND type = ? ORDER BY created_at DESC LIMIT ?",
                (type_filter, limit),
            ).fetchall()
        else:
            rows = self._sqlite.execute(
                "SELECT * FROM memories WHERE deleted_at IS NULL ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [Memory.from_sql_row(tuple(r)) for r in rows]

    def vector_search(
        self,
        query: str,
        top_k: int | None = None,
        filters: dict | None = None,
        *,
        include_superseded: bool = False,
    ) -> list[VectorSearchHit]:
        """Vector/lexical search validated against this store's live records.

        Backend payloads can be stale or belong to another database sharing a
        remote collection. Only local eligible IDs and fresh SQLite payloads
        leave this boundary. Widening refills slots removed by validation.
        """
        self.assert_embedder_current()
        k = self.settings.recall_topk if top_k is None else int(top_k)
        if k <= 0:
            return []
        qv = self.embedder.embed(query)
        from proharness.memory.retrieval_context import capture_query_vector
        capture_query_vector(query, qv, self.embedder.fingerprint())
        fetch_k = k
        maximum = None
        while True:
            self.assert_embedder_current()
            raw = self._backend.search(qv, query, fetch_k, filters=filters)
            hits: list[VectorSearchHit] = []
            seen: set[str] = set()
            for hit in raw:
                if hit.id in seen:
                    continue
                seen.add(hit.id)
                memory = self.get(hit.id)
                if memory is not None and memory.is_retrievable(
                    filters, include_superseded=include_superseded,
                ):
                    hits.append(VectorSearchHit(hit.id, hit.score, memory.to_payload()))
            if len(hits) >= k or len(raw) < fetch_k:
                return hits[:k]
            if maximum is None:
                maximum = max(self.count(), self._backend.count())
            if fetch_k >= maximum:
                return hits[:k]
            fetch_k = min(maximum, fetch_k * 2)

    def count(self) -> int:
        """Live (non-tombstoned) memory count."""
        return self._sqlite.execute(
            "SELECT COUNT(*) FROM memories WHERE deleted_at IS NULL"
        ).fetchone()[0]

    def tombstoned(self, limit: int = 200) -> list[Memory]:
        """Tombstoned-but-recoverable memories, newest-deleted first (audit)."""
        self._ensure_deleted_at_column()
        rows = self._sqlite.execute(
            "SELECT * FROM memories WHERE deleted_at IS NOT NULL ORDER BY deleted_at DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [Memory.from_sql_row(tuple(r)) for r in rows]

    # ── snapshots (belt-and-suspenders around destructive passes) ────────────
    def snapshot(self, label: str = "manual", *, keep: int = 5) -> str | None:
        """Consistent point-in-time copy via SQLite's online backup API into
        ``<db>.snapshots/<ts>_<label>.db``, pruned to the newest ``keep``.
        Taken around destructive passes (reflection) so a bad pass is
        recoverable beyond tombstones too. Returns the snapshot path."""
        import sqlite3 as _sqlite3
        from pathlib import Path as _Path

        with suppress(Exception):
            src = _Path(self.settings.sqlite_path)
            snap_dir = src.parent / (src.name + ".snapshots")
            snap_dir.mkdir(parents=True, exist_ok=True)
            dest_path = snap_dir / f"{int(time.time() * 1000)}_{label}.db"
            dest = _sqlite3.connect(dest_path)
            try:
                with self._lock:
                    self._sqlite.backup(dest)
            finally:
                dest.close()
            snaps = sorted(snap_dir.glob("*.db"))
            for old in snaps[:-keep] if len(snaps) > keep else []:
                with suppress(Exception):
                    old.unlink()
            return str(dest_path)
        return None

    # ── reflection trigger state (idea #2 — Park 2023) ───────────────────────
    def _accumulate_importance(self, importance: float) -> None:
        """Add `importance` to the running pending-importance counter. Called
        on every add(); reflection fires when the counter crosses the
        configured threshold (see should_reflect / reset_reflection_state)."""
        if importance <= 0:
            return
        row = self._sqlite.execute(
            "SELECT pending_importance, last_reflection_ts FROM reflection_state WHERE key = 'main'"
        ).fetchone()
        now = time.time()
        if row is None:
            self._sqlite.execute(
                "INSERT INTO reflection_state (key, pending_importance, last_reflection_ts) VALUES ('main', ?, ?)",
                (float(importance), now),
            )
        else:
            self._sqlite.execute(
                "UPDATE reflection_state SET pending_importance = ? WHERE key = 'main'",
                (float(row[0]) + float(importance),),
            )

    def pending_importance(self) -> float:
        """The cumulative importance inserted since the last reflection pass."""
        row = self._sqlite.execute(
            "SELECT pending_importance FROM reflection_state WHERE key = 'main'"
        ).fetchone()
        return float(row[0]) if row else 0.0

    def last_reflection_ts(self) -> float:
        row = self._sqlite.execute(
            "SELECT last_reflection_ts FROM reflection_state WHERE key = 'main'"
        ).fetchone()
        return float(row[0]) if row else 0.0

    def reset_reflection_state(self) -> None:
        """Zero the pending-importance counter; stamp last_reflection_ts to now.
        Called after a reflection pass completes."""
        now = time.time()
        with self._lock:
            # Upsert: zero the counter whether or not the row exists yet.
            self._sqlite.execute(
                "INSERT INTO reflection_state (key, pending_importance, last_reflection_ts) "
                "VALUES ('main', 0, ?) "
                "ON CONFLICT(key) DO UPDATE SET pending_importance = 0, last_reflection_ts = ?",
                (now, now),
            )
            self._sqlite.commit()

    # ── recent episodic window (idea #3 — rehearsal/replay) ──────────────────
    def recent_episodic(self, n: int = 3) -> list[Memory]:
        """The N most-recent retrievable episodic memories by created_at. Used by the
        rehearsal window (idea #3): always prepended to recall so recent
        high-importance context doesn't drift when it isn't query-relevant."""
        if n <= 0:
            return []
        rows = self._sqlite.execute(
            "SELECT * FROM memories WHERE deleted_at IS NULL AND type = 'episodic' "
            "ORDER BY created_at DESC",
        )
        memories = []
        for row in rows:
            memory = Memory.from_sql_row(tuple(row))
            if memory.is_retrievable():
                memories.append(memory)
                if len(memories) >= int(n):
                    break
        return memories

    # ── router-decision log (idea #1) ────────────────────────────────────────
    def log_router_decision(self, decision) -> int | None:
        """Persist a RouterDecision row (idea #1). Called by the loop at turn
        end. Returns the row id (the handle explicit feedback joins on —
        2026-08-31 #3), or None on failure."""
        from proharness.cost import RouterDecision  # local import; avoid cycle at module load

        if not isinstance(decision, RouterDecision):
            raise TypeError("expected RouterDecision")
        with self._lock:
            cur = self._sqlite.execute(
                "INSERT INTO router_decisions "
                "(ts, query, tier, model, score, alpha, outcome, in_tokens, out_tokens, reason) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                (
                    decision.ts, decision.query, decision.tier, decision.model,
                    decision.score, decision.alpha, decision.outcome,
                    decision.in_tokens, decision.out_tokens, decision.reason,
                ),
            )
            self._sqlite.commit()
            return cur.lastrowid

    # ── explicit turn feedback (2026-08-31 #3) ───────────────────────────────
    def log_turn_feedback(self, rating: int, *, session_id: str | None = None,
                          decision_id: int | None = None, correction: str | None = None) -> None:
        """Persist a human gold label (±1) for a turn; optional correction
        text is later routed through fact extraction as a Mem0 contradiction
        candidate."""
        import time as _time

        if type(rating) is not int or rating not in (-1, 1):
            raise ValueError("rating must be exactly integer +1 or -1")
        with self._lock:
            self._sqlite.execute(
                "INSERT INTO turn_feedback (ts, session_id, decision_id, rating, correction) "
                "VALUES (?,?,?,?,?)",
                (_time.time(), session_id, decision_id, int(rating), correction),
            )
            self._sqlite.commit()

    def router_tuning_observations(self, limit: int = 10_000) -> tuple[list, str]:
        """Scored completed observations with gold labels joined by exact row ID.

        Snapshot content (including latest relevant feedback row ID) fingerprints
        pending-to-completed transitions and edits; pending/no-score rows and
        feedback without a matching scored decision cannot trigger a tune.
        Caller holds the store lock/transaction when atomically consuming it.
        """
        import hashlib

        from proharness.cost import RouterDecision

        rows = self._sqlite.execute(
            "SELECT rowid,ts,query,tier,model,score,alpha,outcome,in_tokens,out_tokens,reason "
            "FROM router_decisions WHERE score IS NOT NULL ORDER BY rowid DESC LIMIT ?", (limit,)).fetchall()
        decisions, snapshot = [], []
        for row in rows:
            feedback = self._sqlite.execute(
                "SELECT rowid,rating FROM turn_feedback WHERE decision_id=? AND rating IN (-1,1) "
                "ORDER BY rowid DESC LIMIT 1", (row[0],)).fetchone()
            outcome = ("good" if feedback[1] == 1 else "repeat") if feedback else row[7]
            if outcome not in {"good", "revised", "max_steps", "repeat"}:
                continue
            decisions.append(RouterDecision(ts=row[1], query=row[2], tier=row[3], model=row[4],
                                            score=row[5], alpha=row[6], outcome=outcome,
                                            in_tokens=row[8], out_tokens=row[9], reason=row[10]))
            snapshot.append([row[0], row[5], outcome, list(feedback) if feedback else None])
        fingerprint = hashlib.sha256(json.dumps(snapshot, sort_keys=True).encode()).hexdigest()
        return decisions, fingerprint

    def fetch_turn_feedback(self, limit: int = 1000) -> list[dict]:
        """Recent feedback rows (newest first): {ts, session_id, decision_id,
        rating, correction} — the gold-label join for the tuning sweeps."""
        rows = self._sqlite.execute(
            "SELECT ts, session_id, decision_id, rating, correction FROM turn_feedback "
            "ORDER BY ts DESC LIMIT ?",
            (int(limit),),
        ).fetchall()
        return [dict(zip(("ts", "session_id", "decision_id", "rating", "correction"),
                         tuple(r), strict=True)) for r in rows]

    def decision_row(self, decision_id: int) -> dict | None:
        """One router_decisions row by id (the feedback join target)."""
        row = self._sqlite.execute(
            "SELECT ts, query, tier, model, score, alpha, outcome, in_tokens, out_tokens, reason "
            "FROM router_decisions WHERE rowid = ?",
            (int(decision_id),),
        ).fetchone()
        if row is None:
            return None
        keys = ("ts", "query", "tier", "model", "score", "alpha", "outcome",
                "in_tokens", "out_tokens", "reason")
        return dict(zip(keys, tuple(row), strict=True))

    def fetch_router_decisions(self, limit: int = 1000, since: float | None = None) -> list:
        """Return recent RouterDecision rows (newest first). Used by the α-sweep."""
        from proharness.cost import RouterDecision

        if since is not None:
            rows = self._sqlite.execute(
                "SELECT ts, query, tier, model, score, alpha, outcome, in_tokens, out_tokens, reason "
                "FROM router_decisions WHERE ts >= ? ORDER BY ts DESC LIMIT ?",
                (since, limit),
            ).fetchall()
        else:
            rows = self._sqlite.execute(
                "SELECT ts, query, tier, model, score, alpha, outcome, in_tokens, out_tokens, reason "
                "FROM router_decisions ORDER BY ts DESC LIMIT ?",
                (limit,),
            ).fetchall()
        out = []
        for r in rows:
            out.append(RouterDecision(
                ts=r[0], query=r[1], tier=r[2], model=r[3], score=r[4], alpha=r[5],
                outcome=r[6], in_tokens=r[7], out_tokens=r[8], reason=r[9],
            ))
        return out

    def iter_all(self) -> Iterator[Memory]:
        yield from self._iter_sql()

    def _iter_sql(self) -> Iterator[Memory]:
        for row in self._sqlite.execute("SELECT * FROM memories WHERE deleted_at IS NULL"):
            yield Memory.from_sql_row(tuple(row))

    # ── pipeline persistence ───────────────────────────────────────────────
    def list_pipelines(self) -> list:
        """Return all saved pipelines (newest first)."""
        from proharness.pipelines.schemas import Pipeline

        rows = self._sqlite.execute(
            "SELECT id, name, slug, description, definition, version, created_at, updated_at "
            "FROM pipelines ORDER BY updated_at DESC"
        ).fetchall()
        return [Pipeline.from_sql_row(tuple(r)) for r in rows]

    def get_pipeline(self, pipeline_id: str):
        """Return a pipeline by id, or None."""
        from proharness.pipelines.schemas import Pipeline

        row = self._sqlite.execute(
            "SELECT id, name, slug, description, definition, version, created_at, updated_at "
            "FROM pipelines WHERE id = ?",
            (pipeline_id,),
        ).fetchone()
        return Pipeline.from_sql_row(tuple(row)) if row else None

    def get_pipeline_by_slug(self, slug: str):
        """Return a pipeline by slug, or None."""
        from proharness.pipelines.schemas import Pipeline

        row = self._sqlite.execute(
            "SELECT id, name, slug, description, definition, version, created_at, updated_at "
            "FROM pipelines WHERE slug = ?",
            (slug,),
        ).fetchone()
        return Pipeline.from_sql_row(tuple(row)) if row else None

    def save_pipeline(self, pipeline) -> None:
        """Insert or update a pipeline (upsert by id)."""
        import time as _time

        pipeline.updated_at = _time.time()
        with self._lock:
            self._sqlite.execute(
                "INSERT OR REPLACE INTO pipelines "
                "(id, name, slug, description, definition, version, created_at, updated_at) "
                "VALUES (?,?,?,?,?,?,?,?)",
                pipeline.to_sql_row(),
            )
            self._sqlite.commit()

    def delete_pipeline(self, pipeline_id: str) -> bool:
        """Delete a pipeline by id. Returns True if a row was deleted."""
        with self._lock:
            cur = self._sqlite.execute("DELETE FROM pipelines WHERE id = ?", (pipeline_id,))
            self._sqlite.commit()
            return cur.rowcount > 0

    # ── router exemplars (2026-08-11 #1 — self-growing kNN reference set) ───
    def count_router_exemplars(self) -> int:
        row = self._sqlite.execute("SELECT COUNT(*) FROM router_exemplars").fetchone()
        return int(row[0]) if row else 0

    def seed_router_exemplars(self, texts: list[str]) -> int:
        """Seed the exemplar set from the constant list when the table is empty.
        Returns the number seeded (0 if already seeded)."""
        import time as _time
        import uuid as _uuid

        with self._lock:
            if self.count_router_exemplars() > 0:
                return 0
            now = _time.time()
            self._sqlite.executemany(
                "INSERT OR IGNORE INTO router_exemplars (id, text, match_count, created_at) VALUES (?,?,0,?)",
                [(str(_uuid.uuid4()), t, now) for t in texts],
            )
            self._sqlite.commit()
            return len(texts)

    def list_router_exemplars(self) -> list[tuple[str, str, int]]:
        """Return (id, text, match_count) for every exemplar."""
        rows = self._sqlite.execute(
            "SELECT id, text, match_count FROM router_exemplars ORDER BY created_at"
        ).fetchall()
        return [(r[0], r[1], int(r[2])) for r in rows]

    def add_router_exemplar(self, text: str, cap: int = 200) -> str | None:
        """Add an exemplar, evicting the least-matched oldest when at cap.
        Returns the new id (or None if text is empty)."""
        import time as _time
        import uuid as _uuid

        text = text.strip()
        if not text:
            return None
        new_id = str(_uuid.uuid4())
        with self._lock:
            count = self.count_router_exemplars()
            if cap > 0 and count >= cap:
                # Evict biased toward low match_count, oldest first.
                self._sqlite.execute(
                    "DELETE FROM router_exemplars WHERE id = "
                    "(SELECT id FROM router_exemplars ORDER BY match_count ASC, created_at ASC LIMIT 1)"
                )
            self._sqlite.execute(
                "INSERT INTO router_exemplars (id, text, match_count, created_at) VALUES (?,?,0,?)",
                (new_id, text, _time.time()),
            )
            self._sqlite.commit()
        return new_id

    def bump_router_exemplar(self, exemplar_id: str) -> None:
        """Record that an exemplar matched a promoted query (drives eviction bias)."""
        with self._lock:
            self._sqlite.execute(
                "UPDATE router_exemplars SET match_count = match_count + 1 WHERE id = ?",
                (exemplar_id,),
            )
            self._sqlite.commit()

    # ── A-MEM associative links (2026-08-19 #2) ─────────────────────────────
    def set_links(self, memory_id: str, links: list[tuple[str, float]]) -> None:
        """Replace memory_id's outgoing links with the given (link_id, score)
        pairs. Written at the consolidation search site — the neighborhood is
        computed for dedup anyway, so persistence is one INSERT per store."""
        import time as _time

        with self._lock:
            self._sqlite.execute("DELETE FROM memory_links WHERE memory_id = ?", (memory_id,))
            if links:
                self._sqlite.executemany(
                    "INSERT OR REPLACE INTO memory_links (memory_id, link_id, score, created_at) VALUES (?,?,?,?)",
                    [(memory_id, lid, float(s), _time.time()) for lid, s in links],
                )
            self._sqlite.commit()

    def get_links(self, memory_id: str) -> list[str]:
        """Outgoing link ids, best-score first."""
        rows = self._sqlite.execute(
            "SELECT link_id FROM memory_links WHERE memory_id = ? ORDER BY score DESC",
            (memory_id,),
        ).fetchall()
        return [r[0] for r in rows]

    def link_count(self) -> int:
        row = self._sqlite.execute("SELECT COUNT(*) FROM memory_links").fetchone()
        return int(row[0]) if row else 0

    # ── belief-revision pair candidates (2026-08-20 #1) ─────────────────────
    def contradiction_candidates(self, *, floor: float = 0.72, ceiling: float = 0.90,
                                 max_pairs: int = 20) -> list[tuple]:
        """Semantic-memory pairs in the paraphrase band (floor ≤ cosine <
        ceiling) that insert-time dedup never saw — each returned as
        (older, newer, score), neither already superseded. The reflection-time
        contradiction sweep judges these and marks supersession."""
        semantic = [m for m in self.list_all(limit=10_000, type_filter="semantic")
                    if m.is_retrievable() and not m.is_permanent]
        seen_pairs: set[frozenset] = set()
        out: list[tuple] = []
        for m in semantic:
            try:
                hits = self.vector_search(m.text, top_k=5, filters={"type": "semantic"})
            except Exception:
                continue
            for h in hits:
                if h.id == m.id:
                    continue
                if not (floor <= h.score < ceiling):
                    continue
                other = next((x for x in semantic if x.id == h.id), None)
                if other is None:
                    continue
                key = frozenset((m.id, h.id))
                if key in seen_pairs:
                    continue
                seen_pairs.add(key)
                pair = (m, other) if m.created_at <= other.created_at else (other, m)
                out.append((pair[0], pair[1], h.score))
                if len(out) >= max_pairs:
                    return out
        return out

    # ── recall-usage feedback (2026-08-14 #1) ───────────────────────────────
    def _ensure_recall_usage_columns(self) -> None:
        """Migration: older DBs created recall_usage without the via_link
        (2026-08-19 #2), stale (2026-08-20 #1), and via_rewrite (2026-09-17 #2)
        columns; add them in place when absent."""
        with self._lock:
            cols = {r[1] for r in self._sqlite.execute("PRAGMA table_info(recall_usage)").fetchall()}
            if not cols:
                return
            if "via_link" not in cols:
                self._sqlite.execute("ALTER TABLE recall_usage ADD COLUMN via_link INTEGER NOT NULL DEFAULT 0")
            if "stale" not in cols:
                self._sqlite.execute("ALTER TABLE recall_usage ADD COLUMN stale INTEGER NOT NULL DEFAULT 0")
            if "via_rewrite" not in cols:
                self._sqlite.execute("ALTER TABLE recall_usage ADD COLUMN via_rewrite INTEGER NOT NULL DEFAULT 0")
            self._sqlite.commit()

    def log_recall_usage(self, rows: list[tuple]) -> None:
        """Persist per-surfaced-memory usage rows.
        Each row: (query, memory_id, used[0/1], recency, importance, relevance
        [, via_link[, stale[, via_rewrite]]]) — the sub-scores at injection
        time, so the Park-weight sweep can re-rank each query's candidates
        offline. via_link marks A-MEM link-expanded hits (2026-08-19 #2);
        stale a superseded memory still explicitly requested (2026-08-20 #1);
        via_rewrite a follow-up hit grounded by the rewritten-query variant
        (2026-09-17 #2)."""
        import time as _time

        self._ensure_recall_usage_columns()
        now = _time.time()
        with self._lock:
            self._sqlite.executemany(
                "INSERT INTO recall_usage (ts, query, memory_id, used, recency, importance, relevance, via_link, stale, via_rewrite) "
                "VALUES (?,?,?,?,?,?,?,?,?,?)",
                [
                    (now, r[0], r[1], int(r[2]), float(r[3]), float(r[4]), float(r[5]),
                     int(r[6]) if len(r) > 6 else 0, int(r[7]) if len(r) > 7 else 0,
                     int(r[8]) if len(r) > 8 else 0)
                    for r in rows
                ],
            )
            self._sqlite.commit()

    def fetch_recall_usage(self, limit: int = 10_000) -> list[tuple]:
        """Return usage rows (newest first): (query, memory_id, used, recency,
        importance, relevance, via_link, stale, via_rewrite)."""
        self._ensure_recall_usage_columns()
        rows = self._sqlite.execute(
            "SELECT query, memory_id, used, recency, importance, relevance, via_link, stale, via_rewrite "
            "FROM recall_usage ORDER BY ts DESC LIMIT ?",
            (limit,),
        ).fetchall()
        return [(r[0], r[1], int(r[2]), float(r[3]), float(r[4]), float(r[5]),
                 int(r[6] or 0), int(r[7] or 0), int(r[8] or 0)) for r in rows]

    def count_recall_usage(self) -> int:
        row = self._sqlite.execute("SELECT COUNT(*) FROM recall_usage").fetchone()
        return int(row[0]) if row else 0

    # ── vector rebuild (2026-08-13 #3) ──────────────────────────────────────
    def rebuild_vectors(self, batch: int = 64) -> int:
        """Atomically migrate local memory and live document-chunk embeddings.

        Every embedding is staged and validated before an index is changed.
        SQLite source rows, archived originals, and tombstones are preserved.
        Remote Qdrant collections require a separate verified collection/alias
        migration; this method never clears or partially rebuilds one.
        """
        from proharness.memory.rebuild import stage_embeddings
        from proharness.memory.vec_sqlite import SqliteVecBackend

        if self.settings.vector_backend == "qdrant" or not isinstance(
            self._backend, (InMemoryBackend, SqliteVecBackend)
        ):
            raise RuntimeError(
                "Atomic Qdrant rebuild is not supported. Create and verify a new collection "
                "before switching its alias, or explicitly select vector_backend=memory "
                "for a local in-memory index. The remote collection was not changed."
            )
        sqlite_backend = isinstance(self._backend, SqliteVecBackend)
        with self._lock:
            old_dimension = self.vec_table_dimension()
            if not sqlite_backend and (self.settings.vector_backend == "sqlite_vec" or old_dimension is not None):
                raise RuntimeError(
                    "The persistent SQLite vector index is unavailable. Install sqlite-vec "
                    "and select vector_backend=sqlite_vec before rebuilding; no vectors were changed."
                )
            snapshot = sorted(tuple(row) for row in self._sqlite.execute(
                "SELECT * FROM memories WHERE deleted_at IS NULL ORDER BY id"
            ).fetchall())
            archive_exists = self._sqlite.execute(
                "SELECT 1 FROM sqlite_master WHERE name = 'archive_chunks' AND type = 'table'"
            ).fetchone() is not None
            archive_sql = (
                "SELECT c.* FROM archive_chunks c JOIN archive_documents d ON d.id = c.document_id "
                "WHERE d.deleted_at IS NULL ORDER BY c.id"
            )
            archive_snapshot = self._sqlite.execute(archive_sql).fetchall() if archive_exists else []
            old_fingerprint = self.get_meta("embedder_fingerprint")
        memories = [Memory.from_sql_row(row) for row in snapshot]
        fingerprint = self.embedder.fingerprint() if hasattr(self.embedder, "fingerprint") else None
        if not isinstance(fingerprint, str) or not fingerprint:
            raise ValueError("A stable embedder fingerprint is required before rebuilding")
        vectors, dimension = stage_embeddings(
            self.embedder, [m.text for m in memories] + [row["text"] for row in archive_snapshot],
            batch=batch,
        )
        if self.embedder.fingerprint() != fingerprint:
            raise ValueError("Embedder fingerprint changed while preparing vectors; retry the rebuild")
        if sqlite_backend and dimension not in self._VEC_DDL:
            raise ValueError(f"Unsupported SQLite vector dimension {dimension}; index was not changed")
        memory_vectors, archive_vectors = vectors[:len(memories)], vectors[len(memories):]
        replacement = self._backend
        if not sqlite_backend:
            replacement = InMemoryBackend(self.embedder)
            replacement.upsert(memories, memory_vectors)
        # MemoryStore writes and SQLite backend operations acquire the store
        # lock first. Keep that order while publishing a replacement index.
        with self._lock, self._backend._lock:
            try:
                self._sqlite.execute("BEGIN IMMEDIATE")
                current = sorted(tuple(row) for row in self._sqlite.execute(
                    "SELECT * FROM memories WHERE deleted_at IS NULL ORDER BY id"
                ).fetchall())
                current_archive_exists = self._sqlite.execute(
                    "SELECT 1 FROM sqlite_master WHERE name = 'archive_chunks' AND type = 'table'"
                ).fetchone() is not None
                current_archive = self._sqlite.execute(archive_sql).fetchall() if current_archive_exists else []
                if (current != snapshot or current_archive_exists != archive_exists or
                        [tuple(row) for row in current_archive] != [tuple(row) for row in archive_snapshot] or
                        self.get_meta("embedder_fingerprint") != old_fingerprint or
                        self.vec_table_dimension() != old_dimension):
                    raise RuntimeError("Store changed while embeddings were prepared; retry the rebuild")
                if sqlite_backend:
                    self._sqlite.execute("DROP TABLE IF EXISTS memory_vec")
                    self._sqlite.execute(self._VEC_DDL[dimension])
                    self._sqlite.executemany(
                        "INSERT INTO memory_vec(id, embedding) VALUES (?, ?)",
                        [(memory.id, json.dumps(vector)) for memory, vector in
                         zip(memories, memory_vectors, strict=True)],
                    )
                if archive_snapshot:
                    self._sqlite.executemany(
                        "UPDATE archive_chunks SET vector = ?, embedder_fingerprint = ? WHERE id = ?",
                        [(json.dumps(vector), fingerprint, row["id"]) for row, vector in
                         zip(archive_snapshot, archive_vectors, strict=True)],
                    )
                self._sqlite.execute(
                    "INSERT OR REPLACE INTO store_meta (key, value) VALUES (?, ?)",
                    ("embedder_fingerprint", fingerprint),
                )
                self._sqlite.commit()
            except Exception:
                self._sqlite.rollback()
                raise
            self._expected_embedder_fingerprint = fingerprint
            if sqlite_backend:
                self._backend._dim = dimension
                self._backend._embedder = self.embedder
            else:
                self._backend = replacement
            self.last_vector_rebuild_report = {
                "memories": len(memories), "document_chunks": len(archive_snapshot),
                "dimension": dimension, "previous_dimension": old_dimension,
                "fingerprint": fingerprint,
            }
        return len(memories)

    def close(self) -> None:
        with self._lock:
            self._backend.close()
            self._sqlite.close()

    def __enter__(self) -> MemoryStore:
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    @contextmanager
    def transaction(self):
        with self._lock:
            try:
                yield
                self._sqlite.commit()
            except Exception:
                self._sqlite.rollback()
                raise


# ── helpers ───────────────────────────────────────────────────────────────────
def _norm(v: list[float]) -> float:
    return math.sqrt(sum(x * x for x in v))


def _dot(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b, strict=True))


def _matches(payload: dict, filters: dict) -> bool:
    return matches_filters(payload, filters)
