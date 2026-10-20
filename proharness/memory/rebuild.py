"""Validate replacement embeddings before a persistent index is touched."""
from __future__ import annotations

import math
from collections.abc import Sequence


def stage_embeddings(embedder, texts: Sequence[str], *, batch: int, dimension: int = 0):
    """Return complete, finite vectors and their resolved dimension.

    Embedders can discover their dimension lazily. A malformed response in any
    batch aborts preparation; callers must not publish earlier batches.
    """
    batch = max(1, int(batch))
    dimension = int(dimension or getattr(embedder, "dim", 0))
    vectors: list[list[float]] = []
    for start in range(0, len(texts), batch):
        chunk = texts[start:start + batch]
        try:
            response = list(embedder.embed_batch(list(chunk)))
        except TypeError as exc:
            raise ValueError("Embedding batch must return one vector per text") from exc
        if len(response) != len(chunk):
            raise ValueError("Embedding batch returned the wrong number of vectors")
        for vector in response:
            try:
                values = list(vector)
                if any(isinstance(value, (str, bytes, bool)) for value in values):
                    raise ValueError("Embedding values must be numbers")
                values = [float(value) for value in values]
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError("Embedding vector must contain finite numeric values") from exc
            resolved = int(getattr(embedder, "dim", 0) or dimension or len(values))
            dimension = dimension or resolved
            if not dimension or resolved != dimension or len(values) != dimension:
                raise ValueError("Embedding vector dimension does not match the target index")
            if any(not math.isfinite(value) or abs(value) > 3.4028234663852886e38 for value in values):
                raise ValueError("Embedding vector must contain finite float32 values")
            vectors.append(values)
    if not dimension:
        # An empty index still needs its schema changed. This probe only resolves
        # the model; it is never stored as a memory or archive chunk.
        probe, dimension = stage_embeddings(embedder, ["embedding dimension probe"], batch=1)
        del probe
    return vectors, dimension
