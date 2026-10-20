"""Portable recovery preserves source evidence and refuses incomplete restores."""
from __future__ import annotations

import hashlib
import json
import sqlite3
from contextlib import closing
from pathlib import Path

import pytest

from proharness.backup import create_backup, restore_backup, verify_backup
from proharness.memory.documents import DocumentArchive
from proharness.memory.import_jobs import ImportJobs
from proharness.memory.schemas import Memory
from proharness.memory.store import MemoryStore


@pytest.mark.parametrize("state", ["pending", "running", "failed", "cancelled"])
def test_managed_import_uploads_survive_restore(store, tmp_path, state):
    upload = Path(str(store.settings.sqlite_path) + ".archive") / "staging" / ("a" * 32) / "upload.txt"
    upload.parent.mkdir(parents=True)
    upload.write_text("An upload waiting for durable import.")
    job = ImportJobs(store).create([str(upload)], category="writing", metadata={"title": "Draft", "author": "Author"})
    store._sqlite.execute("UPDATE archive_import_items SET state=? WHERE job_id=?", (state, job["job_id"]))
    store._sqlite.commit()
    bundle, target = tmp_path / "bundle", tmp_path / "restored.db"
    assert create_backup(store.settings.sqlite_path, bundle)["staged_uploads"] == 1
    upload.unlink()
    restore_backup(bundle, target)
    with closing(sqlite3.connect(target)) as conn:
        restored_path, restored_state = conn.execute("SELECT path,state FROM archive_import_items").fetchone()
        assert restored_state == state
        assert Path(restored_path).is_relative_to(Path(str(target) + ".archive/staging"))
        assert Path(restored_path).read_text() == "An upload waiting for durable import."
        items = [{"path": restored_path, "size_bytes": 35, "content_hash": hashlib.sha256(Path(restored_path).read_bytes()).hexdigest()}]
        items[0]["size_bytes"] = Path(restored_path).stat().st_size
        expected = hashlib.sha256(json.dumps(items, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
        assert conn.execute("SELECT manifest_hash,category FROM archive_import_jobs").fetchone() == (expected, "writing")
        assert conn.execute("SELECT COUNT(*) FROM archive_import_metadata").fetchone()[0] == 1
    assert create_backup(target, tmp_path / "second")["staged_uploads"] == 1


def test_managed_import_backup_rejects_changed_bytes_and_missing_mapping(store, tmp_path):
    upload = Path(str(store.settings.sqlite_path) + ".archive") / "staging" / ("b" * 32) / "upload.txt"
    upload.parent.mkdir(parents=True)
    upload.write_text("original")
    ImportJobs(store).create([str(upload)])
    bundle = tmp_path / "bundle"
    create_backup(store.settings.sqlite_path, bundle)
    manifest = _manifest(bundle)
    manifest["imports"] = []
    _write_manifest(bundle, manifest)
    with pytest.raises(ValueError):
        verify_backup(bundle)
    upload.write_text("changed")
    with pytest.raises(ValueError):
        create_backup(store.settings.sqlite_path, tmp_path / "invalid")


def test_legacy_backup_format_remains_readable(tmp_path):
    source, bundle = tmp_path / "source.db", tmp_path / "bundle"
    _simple_db(source)
    create_backup(source, bundle)
    manifest = _manifest(bundle)
    manifest["format_version"] = 1
    manifest.pop("imports")
    manifest.pop("staging_root")
    _write_manifest(bundle, manifest)
    assert verify_backup(bundle)["format_version"] == 1
    assert restore_backup(bundle, tmp_path / "restored.db")["restored"]


def test_interrupted_managed_import_rebases_source_without_duplicate(store, tmp_path, embedder):
    upload = Path(str(store.settings.sqlite_path) + ".archive") / "staging" / ("c" * 32) / "upload.txt"
    upload.parent.mkdir(parents=True)
    upload.write_text("An interrupted but already archived upload.")
    job = ImportJobs(store).create([str(upload)], metadata={"title": "Recovered title"})
    doc = DocumentArchive(store).ingest(str(upload), title="Recovered title")
    store._sqlite.execute("UPDATE archive_import_items SET state='running' WHERE job_id=?", (job["job_id"],))
    store._sqlite.commit()
    bundle, target = tmp_path / "bundle", tmp_path / "restored.db"
    create_backup(store.settings.sqlite_path, bundle)
    restore_backup(bundle, target)
    upload.unlink()
    with MemoryStore(store.settings.model_copy(update={"sqlite_path": str(target)}), embedder=embedder) as recovered:
        ImportJobs(recovered).run(job["job_id"], resume=True)
        rows = recovered._sqlite.execute("SELECT id,source_path,source_identity,title FROM archive_documents").fetchall()
        assert len(rows) == 1 and rows[0]["id"] == doc["document_id"]
        assert rows[0]["source_identity"] == hashlib.sha256(rows[0]["source_path"].encode()).hexdigest()
        assert rows[0]["title"] == "Recovered title"


def _manifest(bundle):
    return json.loads((bundle / "manifest.json").read_text())


def _write_manifest(bundle, data):
    (bundle / "manifest.json").write_text(json.dumps(data))


def _simple_db(path):
    with closing(sqlite3.connect(path)) as conn:
        conn.execute("CREATE TABLE facts (text TEXT)")
        conn.execute("INSERT INTO facts VALUES ('recovery works')")
        conn.commit()


def test_roundtrip_preserves_facts_learned_state_originals_versions_and_evidence(store, tmp_path):
    fact = store.add(Memory(text="I founded the neighborhood garden.", metadata={"retention": "permanent"}))
    forgotten = store.add(Memory(text="Old private detail."))
    store.delete(forgotten.id)
    store.set_meta("learned_classifier", '{"weights":[0.25,0.75]}')
    archive = DocumentArchive(store)
    source = tmp_path / "essay.md"
    source.write_text("# Service\nI organized twenty volunteers for our garden.\n")
    first = archive.ingest(str(source), sensitive=True)
    source.write_text("# Service\nI organized thirty volunteers for our garden.\n")
    second = archive.ingest(str(source), sensitive=True)
    archive.forget(first["document_id"])
    originals = {d["document_id"]: Path(d["original_path"]).read_bytes() for d in (first, second)}
    with store._lock:
        rows_before = [tuple(row) for row in store._sqlite.execute("SELECT * FROM archive_chunks ORDER BY id")]
    bundle, target = tmp_path / "backup", tmp_path / "recovered.db"
    report = create_backup(store.settings.sqlite_path, bundle)
    assert report["documents"] == report["originals"] == 2
    assert report["table_counts"]["memories"] == 2
    assert "Qdrant" in " ".join(report["limitations"])
    json.dumps(report)
    # Recovery must stand alone once original documents and their archive vanish.
    source.unlink()
    for doc in (first, second):
        Path(doc["original_path"]).unlink()
    result = restore_backup(bundle, target)
    assert result["restored"] and result["database"] == str(target)
    json.dumps(result)
    with closing(sqlite3.connect(target)) as conn:
        assert conn.execute("SELECT text FROM memories WHERE id=?", (fact.id,)).fetchone()[0] == fact.text
        assert conn.execute("SELECT deleted_at FROM memories WHERE id=?", (forgotten.id,)).fetchone()[0] is not None
        assert conn.execute("SELECT value FROM store_meta WHERE key='learned_classifier'").fetchone()[0] == '{"weights":[0.25,0.75]}'
        assert conn.execute("SELECT * FROM archive_chunks ORDER BY id").fetchall() == rows_before
        restored = conn.execute("SELECT id,original_path,sensitive,deleted_at FROM archive_documents").fetchall()
    for doc_id, original_path, sensitive, deleted_at in restored:
        assert Path(original_path).is_relative_to(Path(str(target) + ".archive"))
        assert Path(original_path).read_bytes() == originals[doc_id]
        assert sensitive and deleted_at is not None
    assert verify_backup(bundle)["ok"]  # Restore never edits its bundle.
    assert create_backup(target, tmp_path / "second-backup")["documents"] == 2


def test_snapshot_includes_committed_wal_and_excludes_later_writes(tmp_path):
    source, bundle = tmp_path / "live.db", tmp_path / "backup"
    with closing(sqlite3.connect(source)) as conn:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.execute("CREATE TABLE facts (text TEXT)")
        conn.execute("INSERT INTO facts VALUES ('committed in WAL')")
        conn.commit()
        assert Path(str(source) + "-wal").stat().st_size > 0
        create_backup(source, bundle)
        conn.execute("INSERT INTO facts VALUES ('later change')")
        conn.commit()
    target = tmp_path / "restored.db"
    restore_backup(bundle, target)
    with closing(sqlite3.connect(target)) as conn:
        assert conn.execute("SELECT text FROM facts").fetchall() == [("committed in WAL",)]


def test_same_original_is_stored_once_and_all_documents_mapped(store, tmp_path):
    archive = DocumentArchive(store)
    for name in ("one.txt", "two.txt"):
        source = tmp_path / name
        source.write_text("The same source bytes.")
        archive.ingest(str(source))
    result = create_backup(store.settings.sqlite_path, tmp_path / "backup")
    assert result["documents"] == 2 and result["originals"] == 1


@pytest.mark.parametrize("damage", ["database", "original", "missing_original", "extra_file", "mapping", "version"])
def test_corruption_fails_before_target_publication(store, tmp_path, damage):
    source = tmp_path / "essay.txt"
    source.write_text("A preserved personal essay.")
    DocumentArchive(store).ingest(str(source))
    bundle, target = tmp_path / "backup", tmp_path / "restored.db"
    create_backup(store.settings.sqlite_path, bundle)
    manifest = _manifest(bundle)
    original = bundle / manifest["documents"][0]["path"]
    if damage == "database":
        with (bundle / "memory.db").open("ab") as stream:
            stream.write(b"corruption")
    elif damage == "original":
        original.write_bytes(b"changed")
    elif damage == "missing_original":
        original.unlink()
    elif damage == "extra_file":
        (bundle / "unexpected").write_text("not in manifest")
    elif damage == "mapping":
        manifest["documents"][0]["document_id"] = "unknown"
        _write_manifest(bundle, manifest)
    else:
        manifest["format_version"] = 999
        _write_manifest(bundle, manifest)
    with pytest.raises(ValueError):
        restore_backup(bundle, target)
    assert not target.exists() and not Path(str(target) + ".archive").exists()


@pytest.mark.parametrize("relative", ["../outside", "/etc/passwd", "originals/../../outside", "originals\\outside", "./memory.db"])
def test_manifest_path_traversal_rejected(tmp_path, relative):
    source, bundle = tmp_path / "source.db", tmp_path / "backup"
    _simple_db(source)
    create_backup(source, bundle)
    manifest = _manifest(bundle)
    manifest["files"][relative] = manifest["files"]["memory.db"]
    _write_manifest(bundle, manifest)
    with pytest.raises(ValueError, match="path"):
        verify_backup(bundle)


def test_symlink_original_and_bundle_rejected(store, tmp_path):
    source = tmp_path / "essay.txt"
    source.write_text("Personal essay.")
    doc = DocumentArchive(store).ingest(str(source))
    original = Path(doc["original_path"])
    original.unlink()
    original.symlink_to(source)
    bundle = tmp_path / "backup"
    with pytest.raises(ValueError, match="Symlink"):
        create_backup(store.settings.sqlite_path, bundle)
    assert not bundle.exists()
    original.unlink()
    original.write_bytes(source.read_bytes())
    create_backup(store.settings.sqlite_path, bundle)
    bundled = bundle / _manifest(bundle)["documents"][0]["path"]
    bundled.unlink()
    bundled.symlink_to(original)
    with pytest.raises(ValueError, match="Symlink"):
        restore_backup(bundle, tmp_path / "target.db")


@pytest.mark.parametrize("collision", ["database", "archive", "wal", "dangling_symlink"])
def test_restore_refuses_existing_targets_without_modification(tmp_path, collision):
    source, bundle, target = tmp_path / "source.db", tmp_path / "backup", tmp_path / "target.db"
    _simple_db(source)
    create_backup(source, bundle)
    occupied = {"database": target, "archive": Path(str(target) + ".archive"),
                "wal": Path(str(target) + "-wal"), "dangling_symlink": target}[collision]
    if collision == "archive":
        occupied.mkdir()
        (occupied / "sentinel").write_text("keep")
    elif collision == "dangling_symlink":
        occupied.symlink_to(tmp_path / "missing")
    else:
        occupied.write_text("keep")
    with pytest.raises((ValueError, FileExistsError)):
        restore_backup(bundle, target)
    if collision == "archive":
        assert (occupied / "sentinel").read_text() == "keep"
    elif collision == "dangling_symlink":
        assert occupied.is_symlink()
    else:
        assert occupied.read_text() == "keep"


def test_backup_refuses_unmanaged_original_and_removes_staging(store, tmp_path):
    source = tmp_path / "essay.txt"
    source.write_text("Personal essay.")
    doc = DocumentArchive(store).ingest(str(source))
    with store._lock:
        store._sqlite.execute("UPDATE archive_documents SET original_path=? WHERE id=?", (str(source), doc["document_id"]))
        store._sqlite.commit()
    bundle = tmp_path / "backup"
    with pytest.raises(ValueError, match="managed archive"):
        create_backup(store.settings.sqlite_path, bundle)
    assert not bundle.exists()
    assert not list(tmp_path.glob(".proharness-backup-*"))


def test_valid_hash_does_not_hide_database_corruption(tmp_path):
    source, bundle = tmp_path / "source.db", tmp_path / "backup"
    _simple_db(source)
    create_backup(source, bundle)
    data = b"not a SQLite file"
    (bundle / "memory.db").write_bytes(data)
    manifest = _manifest(bundle)
    manifest["files"]["memory.db"] = {"sha256": hashlib.sha256(data).hexdigest(), "size": len(data)}
    _write_manifest(bundle, manifest)
    with pytest.raises(ValueError, match="database"):
        verify_backup(bundle)


def test_restore_failure_cleans_only_its_created_archive(tmp_path, monkeypatch):
    source, bundle, target = tmp_path / "source.db", tmp_path / "backup", tmp_path / "target.db"
    _simple_db(source)
    create_backup(source, bundle)

    def conflict(src, dst):
        Path(dst).write_text("another process owns this")
        raise FileExistsError("concurrent creation")

    monkeypatch.setattr("proharness.backup.os.link", conflict)
    with pytest.raises(FileExistsError):
        restore_backup(bundle, target)
    assert target.read_text() == "another process owns this"
    assert not Path(str(target) + ".archive").exists()
    assert not list(tmp_path.glob(".proharness-restore-*"))


def test_cannot_restore_inside_verified_backup(tmp_path):
    source, bundle = tmp_path / "source.db", tmp_path / "backup"
    _simple_db(source)
    create_backup(source, bundle)
    with pytest.raises(ValueError, match="outside"):
        restore_backup(bundle, bundle / "new.db")
    assert verify_backup(bundle)["ok"]


def test_sqlite_vec_live_store_recovers_search_without_reembedding(tmp_path):
    pytest.importorskip("sqlite_vec")
    from proharness.config import Settings
    from proharness.memory.embeddings import HashingEmbedder
    from proharness.memory.store import MemoryStore
    from proharness.memory.vec_sqlite import SqliteVecBackend

    source, target, bundle = tmp_path / "source.db", tmp_path / "target.db", tmp_path / "backup"
    embedder = HashingEmbedder(dim=128)
    settings = Settings(sqlite_path=str(source), vector_backend="sqlite_vec", embed_dim=128)
    with MemoryStore(settings, embedder=embedder) as store:
        assert isinstance(store._backend, SqliteVecBackend)
        fact = store.add(Memory(text="I led a community garden project."))
        document = tmp_path / "essay.md"
        document.write_text("# Leadership\nI coordinated twenty volunteers in our garden.")
        archived = DocumentArchive(store).ingest(str(document))
        report = create_backup(source, bundle)
        assert report["virtual_tables"]
    restore_backup(bundle, target)
    restored_settings = settings.model_copy(update={"sqlite_path": str(target)})
    with MemoryStore(restored_settings, embedder=embedder) as store:
        assert isinstance(store._backend, SqliteVecBackend)
        assert store.vector_search("community garden", top_k=1)[0].id == fact.id
        from proharness.memory.lexical_sqlite import lexical_candidates
        assert lexical_candidates(store, 'community', 10) == [fact.id]
        assert DocumentArchive(store).search_indexed('volunteers')['hits'][0]['document_id'] == archived['document_id']
        doc = DocumentArchive(store).get_document(archived["document_id"])
        assert doc["source_path"] == archived["source_path"]
        assert doc["source_uri"] == archived["source_uri"]
        assert Path(doc["original_path"]).read_text() == document.read_text()
        assert doc["passages"]


@pytest.mark.parametrize('sql', [
    'CREATE TRIGGER evil AFTER UPDATE ON archive_documents BEGIN DELETE FROM archive_chunks; END',
    'CREATE TRIGGER archive_fts_title AFTER UPDATE OF original_path ON archive_documents BEGIN DELETE FROM archive_chunks; END',
    'CREATE TRIGGER archive_fts_title AFTER UPDATE OF title ON archive_documents BEGIN DELETE FROM archive_chunks; END',
])
def test_archive_relocation_rejects_unknown_or_forged_trigger(store, tmp_path, sql):
    archive = DocumentArchive(store)
    source = tmp_path / 'document.txt'
    source.write_text('Original preserved evidence.')
    archive.ingest(str(source))
    store._sqlite.execute('DROP TRIGGER archive_fts_title')
    store._sqlite.execute(sql)
    store._sqlite.commit()
    with pytest.raises(ValueError, match='archive_documents triggers'):
        create_backup(store.settings.sqlite_path, tmp_path / 'backup')


def test_known_title_trigger_archive_restore_without_fts_or_vec_extension(store, tmp_path):
    from proharness.memory.document_search import ARCHIVE_FTS_TITLE_TRIGGER_SQL
    archive = DocumentArchive(store)
    source = tmp_path / 'document.txt'
    source.write_text('Restored sienna pigment evidence.')
    doc = archive.ingest(str(source))
    sql = store._sqlite.execute("SELECT sql FROM sqlite_schema WHERE name='archive_fts_title'").fetchone()[0]
    assert sql == ARCHIVE_FTS_TITLE_TRIGGER_SQL
    # Opening a plain connection with trusted_schema disabled during restore
    # must not execute this title-only trigger while relocating originals.
    bundle, target = tmp_path / 'backup', tmp_path / 'restored.db'
    report = create_backup(store.settings.sqlite_path, bundle)
    assert 'archive_chunks_fts' in report['virtual_tables']
    restore_backup(bundle, target)
    with sqlite3.connect(target) as conn:
        assert conn.execute('SELECT count(*) FROM archive_chunks').fetchone()[0] == 1
        assert conn.execute('SELECT title FROM archive_chunks_fts').fetchone()[0] == source.stem
        original = conn.execute('SELECT original_path FROM archive_documents WHERE id=?', (doc['document_id'],)).fetchone()[0]
        assert Path(original).read_text() == source.read_text()


@pytest.mark.parametrize("dirty", [False, True])
def test_bounded_derived_index_and_pending_work_survive_plain_restore(store, tmp_path, dirty):
    from proharness.memory.bounded_retrieval import DOCUMENT_TRIGGERS, ensure_index, sync_index
    archive = DocumentArchive(store)
    source = tmp_path / "document.txt"
    source.write_text("Preserved azure pigment evidence.")
    doc = archive.ingest(str(source))
    ensure_index(store._sqlite)
    sync_index(store._sqlite)
    if dirty:
        store._sqlite.execute("UPDATE archive_documents SET title=? WHERE id=?", ("New title", doc["document_id"]))
    store._sqlite.commit()
    tables = ["archive_rank_stats", "archive_rank_terms", "archive_rank_dirty"]
    before = {table: [tuple(row) for row in store._sqlite.execute(f"SELECT * FROM {table} ORDER BY 1,2" if table != "archive_rank_dirty" else f"SELECT * FROM {table} ORDER BY 1")] for table in tables}
    assert bool(before["archive_rank_dirty"]) is dirty
    bundle, target = tmp_path / "backup", tmp_path / "restored.db"
    report = create_backup(store.settings.sqlite_path, bundle)
    assert all(table in report["table_counts"] for table in tables)
    restore_backup(bundle, target)
    with sqlite3.connect(target) as conn:
        conn.execute("PRAGMA trusted_schema=OFF")
        for table in tables:
            order = "1" if table == "archive_rank_dirty" else "1,2"
            assert conn.execute(f"SELECT * FROM {table} ORDER BY {order}").fetchall() == before[table]
        triggers = dict(conn.execute("SELECT name,sql FROM sqlite_schema WHERE type='trigger' AND name LIKE 'archive_rank_doc_%'"))
        assert triggers == DOCUMENT_TRIGGERS
        original = conn.execute("SELECT original_path FROM archive_documents WHERE id=?", (doc["document_id"],)).fetchone()[0]
        assert Path(original).read_text() == source.read_text()


@pytest.mark.parametrize("name", ["archive_rank_doc_insert", "archive_rank_doc_delete", "archive_rank_doc_update"])
def test_forged_bounded_document_trigger_is_rejected(store, tmp_path, name):
    from proharness.memory.bounded_retrieval import ensure_index
    archive = DocumentArchive(store)
    source = tmp_path / "document.txt"
    source.write_text("Original preserved evidence.")
    archive.ingest(str(source))
    ensure_index(store._sqlite)
    store._sqlite.execute(f"DROP TRIGGER {name}")
    # A trusted-looking name cannot authorize a trigger that fires on relocation.
    store._sqlite.execute(f"CREATE TRIGGER {name} AFTER UPDATE OF original_path ON archive_documents BEGIN DELETE FROM archive_rank_stats; END")
    store._sqlite.commit()
    with pytest.raises(ValueError, match="archive_documents triggers"):
        create_backup(store.settings.sqlite_path, tmp_path / "backup")
    assert not (tmp_path / "backup").exists()
