"""NAS-backed memory subsystem.

Architecture (see docs/research.md for citations):

    ┌─────────── working memory ────────────┐   in-context scratch (transient)
    │                                       │
    └─────────── store (persistent) ────────┘
        ├─ SQLite  (source of truth: full text + metadata, on NAS volume)
        └─ Qdrant  (dense + sparse vectors + filterable payload, on NAS)

    recall.py      → Park scoring: α·recency + β·importance + γ·relevance
    consolidate.py → Mem0 ADD/UPDATE/DELETE/NOOP on each insert
    reflect.py     → Letta-style sleep-time consolidation (off the hot path)
"""

from proharness.memory.schemas import Memory, MemoryType

__all__ = ["Memory", "MemoryType"]
