"""sqlite-vec local-only VectorBackend (2026-08-12 #3).

Dense vectors in a ``vec0`` virtual table inside the *same* SQLite database
that holds source-of-truth text + metadata — a true single-node, zero-service
deployment (no Qdrant container) with native exact nearest-neighbor search. Select via ``PROHARNESS_VECTOR_BACKEND=sqlite_vec``.

All SQL lives in MemoryStore's ``vec_*`` helpers (literal-DDL whitelist +
parameterized statements); this module is logic-only — extension loading,
serialization, and the L2→cosine score conversion. Distances: plain vec0
reports L2; embeddings are L2-normalized, so L2 ranking == cosine ranking and
cos_sim = 1 − d²/2.
"""
from __future__ import annotations

import json
import threading

from proharness.memory.embeddings import Embedder
from proharness.memory.schemas import Memory
from proharness.memory.store import VectorBackend, VectorSearchHit


class SqliteVecBackend(VectorBackend):
    """Dense vectors in sqlite-vec, payloads joined from the memories table."""

    def __init__(self, store, embedder: Embedder, *, dim: int, initialize: bool = True):
        import sqlite_vec  # optional dep; ImportError propagates to the fallback

        dim = int(dim or embedder.dim)
        store._sqlite.enable_load_extension(True)  # noqa: SLF001 — backend owns loading
        try:
            sqlite_vec.load(store._sqlite)  # noqa: SLF001
        finally:
            store._sqlite.enable_load_extension(False)  # noqa: SLF001
        if initialize:
            store.vec_ensure_table(dim)  # literal-DDL whitelist; raises on odd dims
        self._store = store
        self._embedder = embedder
        self._dim = dim
        self._lock = threading.RLock()
        from proharness.memory.lexical_sqlite import ensure_memory_fts
        self._fts_available = ensure_memory_fts(store)

    def upsert(self, memories: list[Memory], dense_vecs: list[list[float]]) -> None:
        rows = [(m.id, json.dumps(v)) for m, v in zip(memories, dense_vecs, strict=True)]
        with self._store._lock, self._lock:
            self._store.vec_upsert(rows)

    def delete(self, ids: list[str]) -> None:
        with self._store._lock, self._lock:
            self._store.vec_delete(ids)

    def search_dense(self, query_dense, query_text, top_k, filters=None) -> list[VectorSearchHit]:
        k = int(top_k)
        if k <= 0:
            return []
        query_json = json.dumps(query_dense)
        with self._store._lock, self._lock:
            maximum = self._store.vec_count()
            fetch_k = min(k, maximum)
            if not fetch_k:
                return []
            while True:
                rows = self._store.vec_search(query_json, fetch_k)
                hits: list[VectorSearchHit] = []
                for mid, distance in rows:
                    mem = self._store.get(mid)
                    # Supersession policy is enforced by MemoryStore, allowing
                    # explicit historical/downrank searches through that API.
                    if mem is None or not mem.is_retrievable(filters, include_superseded=True):
                        continue
                    score = max(0.0, 1.0 - (float(distance) ** 2) / 2.0)
                    hits.append(VectorSearchHit(id=mid, score=score, payload=mem.to_payload()))
                if len(hits) >= k or len(rows) < fetch_k or fetch_k >= maximum:
                    return hits[:k]
                # Filtering only the first k distances loses valid matches
                # behind other memory types. Keep widening until complete.
                fetch_k = min(maximum, fetch_k * 2)

    def search(self, query_dense, query_text, top_k, filters=None) -> list[VectorSearchHit]:
        """Fuse dense/FTS ranks, with bounded heuristic relevance for Park scoring.

        RRF determines candidate order; its tiny raw values are not cosine
        similarities. Relevance retains cosine strength and lexical query-token
        coverage (not confidence), attenuated by at most 20% for rank position.
        """
        import re

        from proharness.memory.lexical_sqlite import lexical_candidates

        k = int(top_k)
        if k <= 0:
            return []
        if not self._fts_available:
            return self.search_dense(query_dense, query_text, k, filters)
        with self._store._lock, self._lock:
            budget = max(k, min(256, k * 4))
            dense = self.search_dense(query_dense, query_text, budget, filters)
            lexical = lexical_candidates(self._store, query_text, budget, filters)
            scores = {hit.id: hit.score for hit in dense}
            ranks = {}
            for channel in ([hit.id for hit in dense], lexical):
                for rank, mid in enumerate(channel, 1):
                    ranks[mid] = ranks.get(mid, 0.0) + 1.0 / (60 + rank)
            query_tokens = set(re.findall(r'[^\W_]+', query_text.lower(), re.UNICODE))
            coverage = {}
            if query_tokens:
                for mid in lexical:
                    mem = self._store.get(mid)
                    if mem is not None:
                        tokens = set(re.findall(r'[^\W_]+', mem.text.lower(), re.UNICODE))
                        coverage[mid] = len(tokens & query_tokens) / len(query_tokens)
            hits = []
            for mid in sorted(ranks, key=lambda mid: (-ranks[mid], -coverage.get(mid, 0), mid)):
                mem = self._store.get(mid)
                if mem is None or not mem.is_retrievable(filters, include_superseded=True):
                    continue
                lexical_score = 0.0
                if mid in coverage:
                    lexical_score = 0.85 * coverage[mid]
                relevance = max(scores.get(mid, 0.0), lexical_score)
                relevance *= 0.8 + 0.2 * min(1.0, ranks[mid] * 61)
                hits.append(VectorSearchHit(mid, relevance, mem.to_payload()))
            return hits[:k]

    def count(self) -> int:
        with self._store._lock, self._lock:
            return self._store.vec_count()

    def close(self) -> None:
        pass  # the SQLite connection is owned by the MemoryStore
