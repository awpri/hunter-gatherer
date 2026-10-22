"""Memory domain types.

We follow the cognitive-science taxonomy used by MemGPT/Letta/Mem0:
    - working     : transient, in-context (not persisted here)
    - semantic    : facts / preferences / knowledge ("the user lives in Berlin")
    - episodic    : events / interactions ("on 2025-03-01 we discussed X")
    - procedural  : how-to / skills / recipes ("deploy via `make release`")
"""
from __future__ import annotations

import time
import uuid
from dataclasses import dataclass, field
from typing import Literal

MemoryType = Literal["semantic", "episodic", "procedural"]


@dataclass
class Memory:
    """A single memory record. Source-of-truth lives in SQLite; the dense +
    sparse vectors and a small filterable payload live in Qdrant."""

    id: str = field(default_factory=lambda: str(uuid.uuid4()))
    text: str = ""
    type: MemoryType = "semantic"
    tags: list[str] = field(default_factory=list)
    # Park scoring inputs:
    importance: float = 5.0  # 1..10, LLM-scored at storage time (cheap local model)
    created_at: float = field(default_factory=time.time)
    last_accessed: float = field(default_factory=time.time)
    access_count: int = 0
    # Origin / provenance:
    source: str = "user"  # 'user' | 'agent' | 'reflection' | 'skill:<name>'
    # Optional structured payload (anything you want to filter on later).
    metadata: dict = field(default_factory=dict)
    # Soft-delete tombstone (durable-delete safety): None while live; a
    # timestamp once deleted. store.delete() tombstones (the vector leaves the
    # index so recall stops surfacing it) but the SQLite row — full text +
    # metadata — remains recoverable via store.restore(). purge() is the only
    # hard delete.
    deleted_at: float | None = None

    @property
    def is_consolidated(self) -> bool:
        """True for memories produced by reflection (sleep-time consolidation).

        These distill many episodic memories into a single semantic one, so
        reflection treats them as *stability* anchors: they get an importance
        boost when created and decay slower than raw episodic memories
        (idea #1 — anti-forgetting policy, Tadros et al. 2022).
        """
        return self.source == "reflection" and bool(self.metadata.get("summarized_from"))

    @property
    def is_superseded(self) -> bool:
        """True once the belief-revision sweep (2026-08-20 #1) marked this
        memory contradicted-by-a-later-one. Superseded memories keep their
        audit trail (metadata.superseded_by + valid_to) but leave the recall
        block — temporal validity instead of a destructive delete."""
        return bool(self.metadata.get("superseded_by"))

    @property
    def is_permanent(self) -> bool:
        """Approved records are exempt from automatic forgetting/reconciliation.

        Explicit corrections, deletion and erasure remain available. Retention
        is stored in metadata so existing databases need no schema migration.
        """
        return self.metadata.get("retention") == "permanent"

    def is_retrievable(
        self, filters: dict | None = None, *, include_superseded: bool = False,
    ) -> bool:
        """The shared visibility rule for profiles, search and linked recall.

        SQLite is authoritative: a stale vector or graph edge must never make
        a forgotten fact visible. Superseded facts require explicit historical
        access; ``get`` remains an audit lookup independent of this predicate.
        Filters use scalar equality or a list of accepted scalar values.
        """
        if self.deleted_at is not None or (self.is_superseded and not include_superseded):
            return False
        return matches_filters(self.to_payload(), filters or {})

    # ── payload that gets stored alongside the vector in Qdrant ──────────────
    def to_payload(self) -> dict:
        """Small, indexable payload stored with the vector. Keeps the heavy
        text in SQLite (cheaper, easier to back up)."""
        return {
            "id": self.id,
            "type": self.type,
            "tags": self.tags,
            "importance": self.importance,
            "created_at": self.created_at,
            "last_accessed": self.last_accessed,
            "access_count": self.access_count,
            "source": self.source,
            "preview": self.text[:160],  # short snippet for fast display
        }

    # ── row that gets stored in SQLite ───────────────────────────────────────
    def to_sql_row(self) -> tuple:
        import json

        return (
            self.id,
            self.text,
            self.type,
            json.dumps(self.tags),
            self.importance,
            self.created_at,
            self.last_accessed,
            self.access_count,
            self.source,
            json.dumps(self.metadata),
            self.deleted_at,
        )

    @classmethod
    def from_sql_row(cls, row: tuple) -> Memory:
        import json

        row = tuple(row)
        # deleted_at is the optional 11th column (absent in pre-tombstone DBs
        # until the store's in-place migration adds it).
        deleted_at = row[10] if len(row) > 10 else None
        (
            id_,
            text,
            type_,
            tags_json,
            importance,
            created_at,
            last_accessed,
            access_count,
            source,
            metadata_json,
        ) = row[:10]
        return cls(
            id=id_,
            text=text,
            type=type_,
            tags=json.loads(tags_json) if tags_json else [],
            importance=importance,
            created_at=created_at,
            last_accessed=last_accessed,
            access_count=access_count,
            source=source,
            metadata=json.loads(metadata_json) if metadata_json else {},
            deleted_at=deleted_at,
        )


def matches_filters(payload: dict, filters: dict) -> bool:
    """Match vector payload filters consistently across retrieval routes."""
    for key, value in filters.items():
        actual = payload.get(key)
        if isinstance(value, list):
            if actual not in value:
                return False
        elif actual != value:
            return False
    return True


@dataclass
class RecallHit:
    """A recalled memory with its computed Park score and sub-scores."""

    memory: Memory
    score: float
    recency: float
    importance: float
    relevance: float
    # True when this hit entered via A-MEM one-hop link expansion rather than
    # direct query similarity (2026-08-19 #2) — flagged in the recall_usage
    # flywheel so `hunt memory tune` measures whether expansion pays.
    via_link: bool = False
    # True when the rewritten-query variant (2026-09-17 #2) supplied the
    # winning relevance — the follow-up grounding signal for memory tune.
    via_rewrite: bool = False

    def __lt__(self, other: RecallHit) -> bool:
        return self.score < other.score


@dataclass
class ConsolidationDecision:
    """Outcome of the Mem0-style insert dedup check."""

    op: Literal["ADD", "UPDATE", "DELETE", "NOOP"]
    target_id: str | None = None  # existing memory touched, if any
    reason: str = ""
