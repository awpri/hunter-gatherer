"""Memory store round-trip: SQLite source-of-truth + vector backend.

Verifies add/get/list/delete/touch + vector_search behave correctly with the
in-memory backend (Qdrant stubbed out at import time).
"""
from __future__ import annotations

import time

from proharness.memory.schemas import Memory


def test_add_and_get(store):
    m = Memory(text="hello world", type="semantic", importance=5.0)
    store.add(m)
    fetched = store.get(m.id)
    assert fetched is not None
    assert fetched.text == "hello world"
    assert fetched.type == "semantic"


def test_list_all_orders_by_recency(store):
    older = Memory(text="older", type="semantic", created_at=time.time() - 100)
    newer = Memory(text="newer", type="semantic", created_at=time.time())
    store.add(older)
    store.add(newer)
    listed = store.list_all()
    # Newest first.
    assert listed[0].text == "newer"
    assert listed[1].text == "older"


def test_list_all_filters_by_type(store):
    store.add(Memory(text="s", type="semantic"))
    store.add(Memory(text="e", type="episodic"))
    semantic = store.list_all(type_filter="semantic")
    assert all(m.type == "semantic" for m in semantic)
    assert len(semantic) == 1


def test_delete_removes_from_both_backends(store):
    """Soft delete (durability): vectors leave the index (recall can't
    surface it) and the live count drops; the row tombstones recoverable via
    restore(); purge() is the hard erasure."""
    m = Memory(text="to be deleted", type="semantic")
    store.add(m)
    assert store.count() == 1
    store.delete(m.id)
    assert store.count() == 0
    assert not any(h.id == m.id for h in store.vector_search("to be deleted", top_k=5))
    tomb = store.get(m.id)
    assert tomb is not None and tomb.deleted_at is not None  # tombstoned, not gone
    assert store.restore(m.id).deleted_at is None
    assert store.count() == 1
    store.delete(m.id)
    assert store.purge(m.id) and store.get(m.id) is None


def test_touch_bumps_access_count(store):
    m = Memory(text="access me", type="semantic")
    store.add(m)
    assert store.get(m.id).access_count == 0
    store.touch(m.id)
    fetched = store.get(m.id)
    assert fetched.access_count == 1
    assert fetched.last_accessed >= m.last_accessed


def test_vector_search_returns_relevant(store):
    """With the hashing embedder, shared tokens boost cosine. So a query
    sharing tokens with a stored memory should rank it highly."""
    store.add(Memory(text="proharness router design", type="semantic"))
    store.add(Memory(text="banana bread recipe", type="semantic"))
    hits = store.vector_search("proharness router", top_k=2)
    assert len(hits) >= 1
    assert hits[0].payload["preview"].startswith("proharness router")


def test_vector_search_filters_by_type(store):
    store.add(Memory(text="proharness router", type="semantic"))
    store.add(Memory(text="proharness router event", type="episodic"))
    hits = store.vector_search("proharness router", top_k=5, filters={"type": "semantic"})
    assert all(h.payload["type"] == "semantic" for h in hits)


def test_count(store):
    assert store.count() == 0
    store.add(Memory(text="one", type="semantic"))
    store.add(Memory(text="two", type="semantic"))
    assert store.count() == 2


def test_to_sql_row_roundtrip():
    """Schema serialization survives a SQL round-trip."""

    m = Memory(
        text="x", type="episodic", importance=3.5, tags=["a", "b"],
        source="test", metadata={"k": "v"},
    )
    row = m.to_sql_row()
    m2 = Memory.from_sql_row(row)
    assert m2.text == m.text
    assert m2.type == m.type
    assert m2.importance == m.importance
    assert m2.tags == ["a", "b"]
    assert m2.source == "test"
    assert m2.metadata == {"k": "v"}
