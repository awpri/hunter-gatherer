"""Derived preparation is bounded, durable and separate from source success."""
import sqlite3

from proharness.memory.bounded_retrieval import index_readiness, prepare_index_batch
from proharness.memory.documents import DocumentArchive
from proharness.memory.import_jobs import ImportJobs


def archive(store, tmp_path):
    store.settings.archive_search_policy = "bounded"
    result = DocumentArchive(store)
    path = tmp_path / "history.txt"
    path.write_text("Roman imperial history and public affairs. " * 300)
    result.ingest(path)
    return result


def test_bounded_batches_persist_and_readiness_never_writes(store, tmp_path):
    owner = archive(store, tmp_path)
    assert index_readiness(store)["status"] == "not_installed"
    result = prepare_index_batch(store, 2)
    assert result["processed"] == 2 and result["pending"]
    done = store._sqlite.execute("SELECT count(*) FROM archive_rank_stats").fetchone()[0]
    assert done == 2
    # An independently reopened connection sees committed progress.
    with sqlite3.connect(store.settings.sqlite_path) as db:
        assert db.execute("SELECT count(*) FROM archive_rank_stats").fetchone()[0] == done
    before = store._sqlite.total_changes
    assert index_readiness(store)["lexical_pending"]
    assert store._sqlite.total_changes == before
    while prepare_index_batch(store, 2)["pending"]:
        pass
    assert index_readiness(store)["ready"]
    assert owner.search("Roman imperial history")["hits"]


def test_cancelled_batch_rollback_busy_transaction_and_restart(store, tmp_path):
    archive(store, tmp_path)
    prepare_index_batch(store, 1)
    before = [tuple(r) for r in store._sqlite.execute("SELECT * FROM archive_rank_stats")]
    calls = 0
    def cancelled():
        nonlocal calls
        calls += 1
        return calls >= 4
    assert prepare_index_batch(store, 2, cancelled)["status"] == "cancelled"
    assert [tuple(r) for r in store._sqlite.execute("SELECT * FROM archive_rank_stats")] == before
    store._sqlite.execute("BEGIN")
    assert prepare_index_batch(store)["status"] == "busy"
    assert store._sqlite.in_transaction
    store._sqlite.rollback()
    assert prepare_index_batch(store, 2)["processed"] == 2


def test_import_source_success_separate_from_preparation_failure(store, tmp_path, monkeypatch):
    store.settings.archive_search_policy = "bounded"
    path = tmp_path / "history.txt"
    path.write_text("Public history evidence.")
    jobs = ImportJobs(store)
    job = jobs.create([path])
    def fail(*args, **kwargs):
        raise RuntimeError("private path and title")
    monkeypatch.setattr("proharness.memory.bounded_retrieval.prepare_index_batch", fail)
    result = jobs.run(job["job_id"])
    assert result["counts"]["succeeded"] == 1
    assert result["search_index"]["status"] == "failed"
    assert "private" not in str(result["search_index"])


def test_default_exhaustive_never_installs_weighted_index(store):
    assert prepare_index_batch(store)["status"] == "not_required"
    assert index_readiness(store)["ready"]
    assert not store._sqlite.execute("SELECT 1 FROM sqlite_master WHERE name='archive_rank_stats'").fetchone()


def test_import_prepares_small_job_and_read_only_diagnostics(store, tmp_path):
    store.settings.archive_search_policy = "bounded"
    path = tmp_path / "history.txt"
    path.write_text("Roman public history source.")
    jobs = ImportJobs(store)
    result = jobs.run(jobs.create([path])["job_id"])
    assert result["counts"]["succeeded"] == 1
    assert result["search_index"]["status"] == "ready"
    def authorize(action, *args):
        return sqlite3.SQLITE_DENY if action in (sqlite3.SQLITE_INSERT, sqlite3.SQLITE_UPDATE, sqlite3.SQLITE_DELETE) else sqlite3.SQLITE_OK
    store._sqlite.set_authorizer(authorize)
    try:
        assert index_readiness(store)["ready"]
    finally:
        store._sqlite.set_authorizer(None)


def test_busy_external_writer_defers_without_losing_progress(store, tmp_path):
    archive(store, tmp_path)
    prepare_index_batch(store, 1)
    timeout = store._sqlite.execute("PRAGMA busy_timeout").fetchone()[0]
    with sqlite3.connect(store.settings.sqlite_path) as other:
        other.execute("BEGIN IMMEDIATE")
        assert prepare_index_batch(store, 2)["status"] == "busy"
        other.rollback()
    assert not store._sqlite.in_transaction
    assert store._sqlite.execute("PRAGMA busy_timeout").fetchone()[0] == timeout
    assert prepare_index_batch(store, 2)["processed"] == 2


def test_search_pending_preparation_keeps_complete_results(store, tmp_path):
    owner = archive(store, tmp_path)
    prepare_index_batch(store, 1)
    assert index_readiness(store)["lexical_pending"]
    result = owner.search("Roman imperial history", explain=True)
    assert result["hits"] and result["candidate_selection"]["ranking_complete"]
    assert not index_readiness(store)["lexical_pending"]


def test_unavailable_native_acceleration_retains_queue_without_actionable_pending(store, tmp_path, monkeypatch):
    from proharness.memory.document_search import ensure_vectors
    store.settings.archive_search_policy = "bounded"
    owner = DocumentArchive(store, initialize_search=False)
    path = tmp_path / "source.txt"
    path.write_text("Roman public affairs and history.")
    owner.ingest(path)
    # Simulate a database whose vector queue was created on another host.
    ensure_vectors(store._sqlite)
    monkeypatch.setattr("proharness.memory.document_search.load_vec", lambda db: False)
    queued = store._sqlite.execute("SELECT count(*) FROM archive_vector_dirty").fetchone()[0]
    assert queued == 1
    result = prepare_index_batch(store)
    assert result["status"] == "ready" and result["pending"] is False
    assert result["vector_pending"] and result["vector_acceleration"] == "unavailable"
    assert result["vector_processed"] == 0
    readiness = index_readiness(store)
    assert readiness["ready"] and readiness["vector_pending"]
    assert readiness["vector_acceleration"] == "unavailable"
    assert store._sqlite.execute("SELECT count(*) FROM archive_vector_dirty").fetchone()[0] == queued
    # Complete lexical search still works; native work can resume when available.
    assert owner.search("Roman public affairs")["hits"]
    monkeypatch.undo()
    resumed = prepare_index_batch(store)
    if resumed["vector_acceleration"] == "available":
        assert resumed["vector_processed"] == queued and not resumed["vector_pending"]


def test_polling_and_recreated_manager_keep_failure_separate_from_live_readiness(store, tmp_path, monkeypatch):
    store.settings.archive_search_policy = "bounded"
    path = tmp_path / "source.txt"
    path.write_text("Roman public history source.")
    jobs = ImportJobs(store)
    job_id = jobs.create([path])["job_id"]
    def fail(*args, **kwargs):
        raise RuntimeError("private source path")
    with monkeypatch.context() as patch:
        patch.setattr("proharness.memory.bounded_retrieval.prepare_index_batch", fail)
        jobs.run(job_id)
    with sqlite3.connect(store.settings.sqlite_path) as reopened:
        import json
        durable = json.loads(reopened.execute("SELECT result FROM archive_import_index_progress WHERE job_id=?", (job_id,)).fetchone()[0])
        assert durable["status"] == "failed"
    pending = ImportJobs(store).status(job_id)
    assert pending["ingestion_complete"] and pending["complete"]
    assert not pending["search_index"]["ready"]
    assert pending["last_search_preparation"]["status"] == "failed"
    assert "private" not in str(pending["last_search_preparation"])
    while prepare_index_batch(store)["pending"]:
        pass
    recovered = ImportJobs(store).list()["jobs"][0]
    assert recovered["search_index"]["ready"]
    assert recovered["last_search_preparation"]["status"] == "failed"


def test_ready_first_query_has_zero_bulk_preparation(store, tmp_path, monkeypatch):
    """Small paired comparison: import-ready first query versus explicit baseline.

    Count hydrated lexical/vector rows directly; timings alone cannot establish
    that a first search avoided deferred indexing.
    """
    import time

    import proharness.memory.bounded_retrieval as bounded
    import proharness.memory.document_search as native
    store.settings.archive_search_policy = "bounded"
    path = tmp_path / "source.txt"
    path.write_text("Roman imperial history and public affairs. " * 300)
    jobs = ImportJobs(store)
    result = jobs.run(jobs.create([path])["job_id"])
    while not jobs.status(result["job_id"])["search_index"]["ready"]:
        prepare_index_batch(store, 2)
    counters = {"lexical_rows": 0, "vector_dirty_rows": 0}
    original_sync, original_vectors = bounded.sync_index, native.sync_vectors
    def sync(db, **kwargs):
        count = original_sync(db, **kwargs) or 0
        counters["lexical_rows"] += count
        return count
    def vectors(db, *args, **kwargs):
        counters["vector_dirty_rows"] += db.execute("SELECT count(*) FROM archive_vector_dirty").fetchone()[0]
        return original_vectors(db, *args, **kwargs)
    monkeypatch.setattr(bounded, "sync_index", sync)
    monkeypatch.setattr(native, "sync_vectors", vectors)
    owner = jobs.archive
    started = time.perf_counter()
    first = owner.search("Roman imperial history")
    first_ms = (time.perf_counter() - started) * 1000
    bounded.prepare_index(owner)
    started = time.perf_counter()
    baseline = owner.search("Roman imperial history")
    baseline_ms = (time.perf_counter() - started) * 1000
    assert [h["chunk_id"] for h in first["hits"]] == [h["chunk_id"] for h in baseline["hits"]]
    assert counters == {"lexical_rows": 0, "vector_dirty_rows": 0}
    print({"ready_first_ms": first_ms, "prepared_baseline_ms": baseline_ms, **counters})


def test_missing_native_index_cannot_report_ready_before_initial_conversion(store, tmp_path):
    import pytest
    archive(store, tmp_path)
    while prepare_index_batch(store)["pending"]:
        pass
    if index_readiness(store)["vector_acceleration"] != "available":
        pytest.skip("native vector extension unavailable")
    # A lexical-only installation must not hide native conversion work.
    with store.transaction():
        for row in store._sqlite.execute("SELECT name FROM sqlite_master WHERE type='trigger' AND name LIKE 'archive_vec_%'").fetchall():
            store._sqlite.execute('DROP TRIGGER "' + row[0] + '"')
        store._sqlite.execute("DROP TABLE archive_vector_dirty")
        store._sqlite.execute("DROP TABLE archive_chunk_vectors")
    assert not index_readiness(store)["ready"]
    assert index_readiness(store)["vector_pending"]
    while prepare_index_batch(store)["pending"]:
        pass
    assert index_readiness(store)["ready"]


def test_exhaustive_native_preparation_is_bounded_and_ready_first_semantic_query_is_warm(store, tmp_path, monkeypatch):
    import pytest

    import proharness.memory.document_search as native
    class Semantic:
        def fingerprint(self):
            return "readiness-synthetic-v1"

        def embed(self, text):
            return [1., 0., 0.]

        def embed_batch(self, texts):
            return [self.embed(text) for text in texts]

    monkeypatch.setattr(store, "embedder", Semantic())
    assert store.settings.archive_search_policy == "exhaustive"
    owner = DocumentArchive(store)
    path = tmp_path / "source.txt"
    path.write_text("Roman imperial history and public affairs. " * 300)
    owner.ingest(path)
    if not owner._native_vectors:
        pytest.skip("native vector extension unavailable")
    assert index_readiness(store)["status"] == "pending"
    queued = store._sqlite.execute("SELECT count(*) FROM archive_vector_dirty").fetchone()[0]
    first_batch = prepare_index_batch(store, 2)
    assert first_batch["processed"] == 0
    assert first_batch["vector_processed"] == min(2, queued)
    while prepare_index_batch(store, 2)["pending"]:
        pass
    assert index_readiness(store)["ready"]
    assert not store._sqlite.execute("SELECT 1 FROM sqlite_master WHERE name='archive_rank_stats'").fetchone()
    dirty_counts = []
    original = native.sync_vectors
    def sync(db):
        dirty_counts.append(db.execute("SELECT count(*) FROM archive_vector_dirty").fetchone()[0])
        return original(db)
    monkeypatch.setattr(native, "sync_vectors", sync)
    first = owner.search("Roman history", explain=True)
    prepare_index_batch(store)
    baseline = owner.search("Roman history", explain=True)
    assert first["hits"] and [h["chunk_id"] for h in first["hits"]] == [h["chunk_id"] for h in baseline["hits"]]
    assert dirty_counts == [0, 0]


def test_exhaustive_unavailable_native_queue_does_not_spin(store, tmp_path, monkeypatch):
    owner = DocumentArchive(store)
    path = tmp_path / "source.txt"
    path.write_text("Roman public history source.")
    owner.ingest(path)
    from proharness.memory.document_search import ensure_vectors
    ensure_vectors(store._sqlite)
    monkeypatch.setattr("proharness.memory.document_search.load_vec", lambda db: False)
    monkeypatch.setattr("proharness.memory.bounded_retrieval._native_vector_available", lambda db: False)
    result = prepare_index_batch(store, 1)
    assert result["status"] == "ready" and not result["pending"]
    assert result["vector_pending"] and result["vector_processed"] == 0
    assert index_readiness(store)["ready"]
    assert index_readiness(store)["vector_acceleration"] == "unavailable"
    assert not store._sqlite.execute("SELECT 1 FROM sqlite_master WHERE name='archive_rank_stats'").fetchone()
