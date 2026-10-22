"""RAG-over-tools selector.

RAG-MCP finding (May 2025): dumping all tool schemas into the prompt collapses
selection accuracy (13.62%) because of distractors; keyword pre-filtering gets
18.20%; RAG-over-tools (embed tool descriptions, retrieve top-k by query) gets
43.13% — roughly tripling accuracy while HALVING prompt tokens.

So on every turn we:
  1. Embed each tool's name+description once (cached).
  2. Embed the latest user message.
  3. Retrieve top-k tool specs by cosine similarity.
  4. Inject only those into the model's context.

A small set of "always-on" tools (memory_recall, memory_store) bypass the
selector so the agent can always manage its own memory.
"""
from __future__ import annotations

import math
from dataclasses import dataclass, field

from hunter_gatherer.memory.embeddings import Embedder
from hunter_gatherer.models.base import ToolSpec
from hunter_gatherer.tools.base import Registry

# Tools that should always be available regardless of query similarity.
# tool_result_fetch (2026-08-23 #1): the escape hatch for condensed results
# must always be reachable — the agent escalates to the raw payload only when
# a digest provably wasn't enough.
ALWAYS_ON = {"memory_recall", "memory_store", "tool_result_fetch", "evidence", "get_document_chunk", "fetch"}


@dataclass
class ToolSelector:
    registry: Registry
    embedder: Embedder
    top_k: int = 6
    store: object = None  # optional hunter_gatherer.memory.store.MemoryStore (procedural boost)
    _cache: dict[str, list[float]] = field(default_factory=dict)

    def _tool_text(self, spec: ToolSpec) -> str:
        # Combine name + description + the names of required parameters. The
        # parameter names carry a lot of signal for selection.
        params = list((spec.parameters.get("properties") or {}).keys())
        return f"{spec.name}: {spec.description} params={params}"

    def _ensure_cached(self):
        if not self._cache:
            specs = self.registry.specs()
            texts = [self._tool_text(s) for s in specs]
            vecs = self.embedder.embed_batch(texts)
            for s, v in zip(specs, vecs, strict=True):
                self._cache[s.name] = v

    def select(self, query: str) -> list[ToolSpec]:
        """Return the tool specs relevant to `query`, plus always-on tools.

        Procedural boost (idea #3, 2026-08-05): if a procedural memory matches
        the query, its constituent tools are lifted into the selection (after
        always-on) so the agent reaches for the proven sequence faster.
        """
        self._ensure_cached()
        qv = self.embedder.embed(query)

        scored = []
        for tool in self.registry.all():
            if tool.name in ALWAYS_ON:
                continue
            vec = self._cache.get(tool.name)
            if vec is None:
                continue
            scored.append((_cosine(qv, vec), tool))
        scored.sort(key=lambda x: -x[0])

        chosen: list[ToolSpec] = []
        seen = set()
        # Always-on first, then top_k most relevant.
        for tool in self.registry.all():
            if tool.name in ALWAYS_ON and tool.name not in seen:
                chosen.append(tool.spec())
                seen.add(tool.name)
        # Procedural boost (idea #3): lift tools from matching procedural memories.
        if self.store is not None:
            try:
                from hunter_gatherer.memory.procedural import boost_for_query

                for name in boost_for_query(self.store, self.embedder, query):
                    if name not in seen:
                        tool = self.registry.get(name)
                        if tool is not None:
                            chosen.append(tool.spec())
                            seen.add(name)
            except Exception:
                pass  # procedural boost is a nicety; never break selection
        for _, tool in scored[: self.top_k]:
            if tool.name not in seen:
                chosen.append(tool.spec())
                seen.add(tool.name)
        return chosen

    def explain(self, query: str) -> list[tuple[str, float]]:
        """For observability: name + similarity score for the top candidates."""
        self._ensure_cached()
        qv = self.embedder.embed(query)
        out = []
        for tool in self.registry.all():
            vec = self._cache.get(tool.name)
            if vec is None:
                continue
            out.append((tool.name, _cosine(qv, vec)))
        out.sort(key=lambda x: -x[1])
        return out[: self.top_k + len(ALWAYS_ON)]


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    if na == 0 or nb == 0:
        return 0.0
    return dot / (na * nb)
