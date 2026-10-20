"""Embeddings for memory recall + tool selection.

Default: BAAI/bge-small-en-v1.5 (384-dim) via sentence-transformers, run
in-process (not over the LAN) for lowest per-call latency. Research finding:
embedding a single query is CPU-bound, so co-locate it with the agent runtime.

A lightweight hashing fallback is provided so the harness runs before
sentence-transformers is installed; it's decent for the tool-selector's
top-k but should be replaced for real semantic memory recall.

Sparse vectors (idea #1, 2026-07-31): the Embedder protocol has an optional
``embed_sparse`` method. When the embedder supplies learned sparse vectors
(e.g. ``BAAI/bge-m3`` produces dense + sparse natively), the memory store
uses those for the sparse half of the RRF-fused hybrid query instead of the
store's BM25-ish fallback. Swap to bge-m3 via ``PROHARNESS_EMBED_MODEL`` —
the dim bump (384 → 1024) is handled automatically; sparse becomes real
instead of decorative.
"""
from __future__ import annotations

import hashlib
import math
import re
import threading
from collections import OrderedDict
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

from proharness.config import Settings


@dataclass
class SparseVector:
    """A sparse vector: parallel index/value lists. Maps 1:1 to Qdrant's
    ``SparseVector`` but is provider-agnostic so the Embedder protocol doesn't
    depend on the qdrant client."""

    indices: list[int]
    values: list[float]

    @classmethod
    def empty(cls) -> SparseVector:
        return cls(indices=[], values=[])


@runtime_checkable
class Embedder(Protocol):
    dim: int

    def embed(self, text: str) -> list[float]: ...

    def embed_batch(self, texts: list[str]) -> list[list[float]]: ...

    def embed_sparse(self, text: str) -> SparseVector | None:
        """Learned sparse vector for hybrid retrieval, or None if this embedder
        doesn't produce one (the store falls back to BM25-ish sparse)."""
        ...

    def fingerprint(self) -> str:
        """Stable identity of the embedder (model + dim). The store persists
        this on first use and guards against mismatches so an embedder swap
        can't silently break recall (2026-08-13 #3)."""
        ...


class SentenceTransformerEmbedder:
    """Production embedder: sentence-transformers + bge-small (or whatever the
    config names). Loaded lazily so importing proharness.memory is cheap.

    For multi-vector models that produce learned sparse vectors (notably
    ``BAAI/bge-m3``), ``embed_sparse`` returns the model's sparse output so the
    store's hybrid retrieval uses real learned sparse instead of the BM25-ish
    fallback. For dense-only models it returns None.
    """

    _CACHE_LIMIT = 512

    def __init__(self, model_name: str):
        self._model_name = model_name
        self._model = None
        self.dim = 0
        self._supports_sparse = False  # set on _load
        self._inference_lock = threading.RLock()
        # Instance/model-scoped LRU: hashes and immutable floats, never prose.
        self._dense_cache: OrderedDict[bytes, tuple[float, ...]] = OrderedDict()

    def _load(self):
        with self._inference_lock:
            return self._load_locked()

    def _load_locked(self):
        if self._model is None:
            # Imported lazily; sentence-transformers is a heavy dep.
            from sentence_transformers import SentenceTransformer

            self._model = SentenceTransformer(self._model_name)
            # Newer sentence-transformers renamed this method; support both.
            get_dim = getattr(
                self._model,
                "get_embedding_dimension",
                getattr(self._model, "get_sentence_embedding_dimension", None),
            )
            self.dim = int(get_dim() if get_dim else 384)
            # bge-m3 exposes sparse vectors via the 'sparse' modality; detect it
            # by probing the model for a sparse-output capability.
            self._supports_sparse = self._detect_sparse_support()
        return self._model

    def embedding_dimension(self) -> int:
        """Resolve the configured model dimension without encoding a source chunk."""
        self._load()
        return self.dim

    def _detect_sparse_support(self) -> bool:
        """True if this model produces learned sparse vectors (e.g. bge-m3)."""
        # sentence-transformers ≥3 represents bge-m3 as a model whose first
        # module is a SparseTextEmbedding. Probe by attempting a sparse encode.
        try:
            out = self._model.encode(
                ["probe"], return_sparse=True, convert_to_numpy=False,
                show_progress_bar=False,
            )
            first = out[0] if out else None
            return bool(first and "sparse" in first)
        except Exception:
            return False

    def embed(self, text: str) -> list[float]:
        return self.embed_batch([text])[0]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []
        with self._inference_lock:
            model = self._load()
            keys = [hashlib.sha256(text.encode("utf-8")).digest() for text in texts]
            vectors = {}
            missing = {}
            for key, text in zip(keys, texts, strict=True):
                if key in self._dense_cache:
                    vectors[key] = self._dense_cache[key]
                    self._dense_cache.move_to_end(key)
                elif key not in missing:
                    missing[key] = text
            if missing:
                encoded = model.encode(list(missing.values()), normalize_embeddings=True,
                                       convert_to_numpy=True, show_progress_bar=False)
                for key, vector in zip(missing, encoded, strict=True):
                    values = tuple(float(value) for value in vector.tolist())
                    vectors[key] = values
                    self._dense_cache[key] = values
                    self._dense_cache.move_to_end(key)
                    while len(self._dense_cache) > self._CACHE_LIMIT:
                        self._dense_cache.popitem(last=False)
            # Batch-local vectors survive eviction for batches larger than the
            # LRU. Independent lists isolate caller mutation and duplicate rows.
            return [list(vectors[key]) for key in keys]

    def embed_sparse(self, text: str) -> SparseVector | None:
        """Learned sparse vector (bge-m3), or None for dense-only models."""
        with self._inference_lock:
            return self._embed_sparse_locked(text)

    def _embed_sparse_locked(self, text: str) -> SparseVector | None:
        if not self._supports_sparse:
            return None
        try:
            out = self._model.encode([text], return_sparse=True, convert_to_numpy=False,
                                     show_progress_bar=False)
            sparse = out[0].get("sparse")
            if not sparse:
                return None
            # sparse is {token_id: weight}; convert to the parallel-list form.
            # Keys may be int or str depending on the sentence-transformers
            # version; normalize to int indices with float values.
            items = [(int(k), float(v)) for k, v in sparse.items()]
            items.sort(key=lambda kv: kv[0])
            indices = [k for k, _ in items]
            values = [v for _, v in items]
            return SparseVector(indices=indices, values=values)
        except Exception:
            return None

    def fingerprint(self) -> str:
        # Model name only: dim is derived from the model deterministically, and
        # self.dim is 0 before the lazy _load() — including it would make the
        # fingerprint unstable pre-load.
        return f"st:{self._model_name}"


class HashingEmbedder:
    """Cheap fallback embedder: hashed character n-grams → sparse-ish dense vec.

    Not semantically meaningful, but deterministic and good enough to exercise
    the recall/tool-selection code paths before sentence-transformers is
    installed. Do NOT use for production memory recall.
    """

    def __init__(self, dim: int = 384):
        self.dim = dim

    def embed(self, text: str) -> list[float]:
        return self.embed_batch([text])[0]

    def embed_batch(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for t in texts:
            vec = [0.0] * self.dim
            tokens = _tokens(t)
            for tok in tokens:
                for n in (3, 4, 5):
                    gram = tok[:n] if len(tok) >= n else tok
                    h = int(hashlib.md5(gram.encode()).hexdigest(), 16)
                    vec[h % self.dim] += 1.0
            # L2 normalize.
            norm = math.sqrt(sum(v * v for v in vec)) or 1.0
            out.append([v / norm for v in vec])
        return out

    def embed_sparse(self, text: str) -> SparseVector | None:
        """Deterministic BM25-ish sparse so the fallback path also exercises
        the hybrid retrieval machinery. Mirrors the store's stable-hash logic."""
        tokens = _tokens(text)
        if not tokens:
            return SparseVector.empty()
        counts: dict[int, float] = {}
        for tok in tokens:
            tid = int.from_bytes(hashlib.blake2b(tok.encode(), digest_size=4).digest(), "big") % 250_000
            counts[tid] = counts.get(tid, 0.0) + 1.0
        indices = sorted(counts)
        values = [1.0 + (counts[i] - 1) * 0.3 for i in indices]
        return SparseVector(indices=indices, values=values)

    def fingerprint(self) -> str:
        return f"hashing:{self.dim}"


def _tokens(text: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", text.lower())


def _st_available() -> bool:
    """True if sentence-transformers is genuinely importable."""
    try:
        import sentence_transformers  # noqa: F401

        return True
    except Exception:
        return False


def get_embedder(settings: Settings | None = None) -> Embedder:
    """Build the configured embedder. Falls back to HashingEmbedder if
    sentence-transformers isn't importable (e.g. during tests / first run)."""
    settings = settings or Settings()
    # Explicit offline demonstration mode; do not probe/import model packages.
    if settings.embed_model == "hashing":
        return HashingEmbedder(dim=384)
    if _st_available():
        return SentenceTransformerEmbedder(settings.embed_model)
    return HashingEmbedder(dim=384)
