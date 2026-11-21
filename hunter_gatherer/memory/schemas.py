"""Memory domain types.

We follow the cognitive-science taxonomy used by MemGPT/Letta/Mem0:
    - working     : transient, in-context (not persisted here)
    - semantic    : facts / preferences / knowledge ("the user lives in Berlin")
    - episodic    : events / interactions ("on 2025-03-01 we discussed X")
    - procedural  : how-to / skills / recipes ("deploy via `make release`")
"""
from __future__ import annotations

import hashlib
import json
import time
import uuid
from dataclasses import dataclass, field
from typing import Literal

MemoryType = Literal["semantic", "episodic", "procedural"]

# Metadata that only tightens. A writer that never saw these flags must not be
# able to drop them, and a correction must not be more visible than its source.
PROTECTIVE_FLAGS = ("sensitive", "private", "excluded", "application_excluded")

_CONFLICT_MESSAGES = {
    "missing": "Active memory not found",
    "deleted": "Active memory not found: it was forgotten after it was read",
    "superseded": "Active memory not found: it was already replaced by a newer correction",
    "changed": "Memory changed after it was read; read it again before writing",
}


class MemoryConflictError(ValueError):
    """A memory write was refused instead of overwriting a newer decision.

    ``reason`` is ``missing`` (no such record), ``deleted`` (forgotten),
    ``superseded`` (already replaced by a correction) or ``changed`` (edited
    since the writer read it). ``current_id`` names the active record at the
    head of a replacement chain when one exists. Nothing was written.

    This subclasses ValueError so callers that already report invalid requests
    keep working; callers that can reconcile should inspect ``reason``.
    """

    def __init__(self, memory_id: str, reason: str, *, current_id: str | None = None,
                 message: str | None = None):
        if reason not in _CONFLICT_MESSAGES:
            raise ValueError(f"Unknown memory conflict reason: {reason!r}")
        self.memory_id = memory_id
        self.reason = reason
        self.current_id = current_id
        super().__init__(message or _CONFLICT_MESSAGES[reason])


def stored_token(row: tuple) -> str:
    """Digest of the persisted fields that define a record's content and state.

    Access counters are excluded so reading a memory never looks like an edit.
    Tokens are always computed from values read back from SQLite, so both sides
    of a comparison share one serialization.
    """
    row = tuple(row)
    deleted_at = row[10] if len(row) > 10 else None
    payload = json.dumps([row[1], row[2], row[3], row[4], row[8], row[9], deleted_at],
                         separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8", "surrogatepass")).hexdigest()


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

    # ── what this reader saw ─────────────────────────────────────────────────
    @property
    def _stored_token(self) -> str | None:
        """Digest of the stored row this record was read from.

        The store compares it with the current row before accepting a write,
        so a stale snapshot cannot undo a newer forget, correction or privacy
        change. A copy made by hand has none and is treated as unread.

        Reading keeps the row and hashes nothing: a search turns every record
        into an object and writes none of them. The digest is worked out the
        first time a write asks, from the row as it was read, so edits made to
        this object since then do not change what it claims to have seen.
        """
        seen = self.__dict__.get("_seen")
        if type(seen) is tuple:
            seen = self.__dict__["_seen"] = stored_token(seen)
        return seen

    @_stored_token.setter
    def _stored_token(self, token: str | None) -> None:
        self.__dict__["_seen"] = token

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
        memory = cls(
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
        # What this reader saw, kept as read. ``_stored_token`` turns it into a
        # digest only if a write needs one. Not a dataclass field: copies made
        # by hand carry nothing and are treated as unread.
        memory._seen = row
        return memory


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
    target_token: str | None = field(default=None, repr=False)  # stored row used by the judge
