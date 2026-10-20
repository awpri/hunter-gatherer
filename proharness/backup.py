"""Portable, verified backups of the SQLite source of truth and archive originals.

These functions deliberately do not initialize a model, router, or vector service.
External vector indexes are derived state and are not included. Restore into a
fresh location, then rebuild an external index before connecting it to that store.
Hashes detect accidental corruption; they do not authenticate an untrusted backup.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import stat
import tempfile
import time
from contextlib import closing
from pathlib import Path, PurePosixPath

FORMAT_VERSION = 2
_DATABASE = "memory.db"
_MANIFEST = "manifest.json"
_HASH = re.compile(r"[0-9a-f]{64}\Z")
_LIMITATIONS = [
    "External vector indexes (including Qdrant) are not included. Rebuild them from the restored store before use; do not connect an old shared index.",
    "SQLite tables, including embedded chunk vectors and learned state, are preserved. Model files, credentials, environment configuration, and external source files are not included.",
    "Checksums detect corruption, not authenticity. Restore only backups from a trusted source.",
]


def _path(value: str | Path) -> Path:
    path = Path(os.path.abspath(os.fspath(value)))
    # Check before resolve(), which would conceal a symlink supplied as input.
    for item in (path, *path.parents):
        if item.is_symlink():
            raise ValueError(f"Symlinks are not permitted: {item}")
    return path


def _fresh(path: Path) -> None:
    if os.path.lexists(path):
        raise FileExistsError(f"Destination must be fresh: {path}")


def _readonly(path: Path) -> sqlite3.Connection:
    conn = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA trusted_schema=OFF")
    return conn


def _regular_open(path: Path):
    _path(path)
    fd = os.open(path, os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    if not stat.S_ISREG(os.fstat(fd).st_mode):
        os.close(fd)
        raise ValueError(f"Expected a regular file: {path}")
    return os.fdopen(fd, "rb")


def _digest(path: Path) -> dict:
    digest, size = hashlib.sha256(), 0
    with _regular_open(path) as stream:
        while data := stream.read(1024 * 1024):
            digest.update(data)
            size += len(data)
    return {"sha256": digest.hexdigest(), "size": size}


def _copy(source: Path, target: Path) -> dict:
    target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    digest, size = hashlib.sha256(), 0
    with _regular_open(source) as src, target.open("xb") as dest:
        os.chmod(target, 0o600)
        while data := src.read(1024 * 1024):
            dest.write(data)
            digest.update(data)
            size += len(data)
        dest.flush()
        os.fsync(dest.fileno())
    return {"sha256": digest.hexdigest(), "size": size}


def _relative(value: object) -> str:
    if not isinstance(value, str) or not value or "\\" in value or "\x00" in value:
        raise ValueError("Invalid bundle path")
    path = PurePosixPath(value)
    if path.is_absolute() or ".." in path.parts or path.as_posix() != value:
        raise ValueError(f"Unsafe bundle path: {value!r}")
    if value != _DATABASE and not re.fullmatch(r"originals/[0-9a-f]{2}/[0-9a-f]{64}(?:\.[a-z0-9_-]{1,24})?", value) and not re.fullmatch(r"staging/[0-9a-f]{32}/upload\.(?:txt|md|pdf|docx|pptx)", value):
        raise ValueError(f"Unexpected bundle path: {value!r}")
    return value


def _import_items(path):
    with closing(_readonly(path)) as conn:
        if not conn.execute("SELECT 1 FROM sqlite_schema WHERE type='table' AND name='archive_import_items'").fetchone():
            return []
        return [dict(row) for row in conn.execute("SELECT job_id,ordinal,path,size_bytes,content_hash,state FROM archive_import_items ORDER BY job_id,ordinal")]


def _staged_items(path, staging_root):
    return [item for item in _import_items(path)
            if item["state"] in {"pending", "running", "failed", "cancelled"}
            and Path(item["path"]).is_relative_to(staging_root)]


def _reject_import_triggers(path):
    with closing(_readonly(path)) as conn:
        if conn.execute("SELECT 1 FROM sqlite_schema WHERE type='trigger' AND tbl_name IN ('archive_import_items','archive_import_jobs')").fetchone():
            raise ValueError("Cannot relocate managed uploads with import-table triggers")


def _database_info(path: Path) -> tuple[dict, list[dict]]:
    try:
        with closing(_readonly(path)) as conn:
            check = [row[0] for row in conn.execute("PRAGMA quick_check")]
            if check != ["ok"]:
                raise ValueError("SQLite integrity check failed")
            if conn.execute("PRAGMA foreign_key_check").fetchone() is not None:
                raise ValueError("SQLite foreign-key check failed")
            tables = conn.execute("SELECT name, sql FROM sqlite_schema WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall()
            names = {row[0] for row in tables}
            counts, virtual = {}, []
            for name, sql in tables:
                if (sql or "").lstrip().upper().startswith("CREATE VIRTUAL TABLE"):
                    virtual.append(name)
                    continue  # No extension needs to be loaded to recover its underlying tables.
                quoted = '"' + name.replace('"', '""') + '"'
                counts[name] = conn.execute(f"SELECT COUNT(*) FROM {quoted}").fetchone()[0]
            fingerprint = None
            if "store_meta" in names:
                row = conn.execute("SELECT value FROM store_meta WHERE key='embedder_fingerprint'").fetchone()
                fingerprint = row[0] if row else None
            docs = []
            if "archive_documents" in names:
                docs = [dict(row) for row in conn.execute("SELECT id,content_hash,original_path FROM archive_documents ORDER BY id")]
            # Relocation must not execute user-defined triggers. Known derived
            # index triggers are either relocation-inert or suspended below;
            # verify their entire SQL rather than trusting attacker-chosen names.
            from proharness.memory.bounded_retrieval import DOCUMENT_TRIGGERS
            from proharness.memory.catalog_index import DOCUMENT_TRIGGERS as CATALOG_TRIGGERS
            from proharness.memory.document_search import ARCHIVE_FTS_TITLE_TRIGGER_SQL
            allowed = {"archive_fts_title": ARCHIVE_FTS_TITLE_TRIGGER_SQL, **DOCUMENT_TRIGGERS, **CATALOG_TRIGGERS}
            for trigger in conn.execute("SELECT name,sql FROM sqlite_schema WHERE type='trigger' AND tbl_name='archive_documents'"):
                if trigger['name'] not in allowed or trigger['sql'] != allowed[trigger['name']]:
                    raise ValueError("Cannot relocate an archive with archive_documents triggers")
            return {"table_counts": counts, "virtual_tables": sorted(virtual),
                    "embedder_fingerprint": fingerprint,
                    "sqlite_user_version": conn.execute("PRAGMA user_version").fetchone()[0]}, docs
    except sqlite3.DatabaseError as exc:
        raise ValueError(f"Invalid backup database: {exc}") from exc


def _inventory(root: Path) -> set[str]:
    files = set()
    for current, dirs, names in os.walk(root, followlinks=False):
        for name in dirs + names:
            entry = Path(current) / name
            if entry.is_symlink():
                raise ValueError(f"Symlink in bundle: {entry}")
            if not entry.is_dir() and not entry.is_file():
                raise ValueError(f"Non-regular entry in bundle: {entry}")
        files.update((Path(current) / name).relative_to(root).as_posix() for name in names)
    return files


def _load_and_verify(root: Path) -> tuple[dict, dict]:
    if not root.is_dir():
        raise ValueError("Backup must be a directory bundle")
    with _regular_open(root / _MANIFEST) as stream:
        data = stream.read(16 * 1024 * 1024 + 1)
    if len(data) > 16 * 1024 * 1024:
        raise ValueError("Backup manifest is too large")
    try:
        manifest = json.loads(data)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("Invalid backup manifest JSON") from exc
    if not isinstance(manifest, dict) or type(manifest.get("format_version")) is not int or manifest.get("format_version") not in {1, FORMAT_VERSION}:
        raise ValueError("Unsupported backup format version")
    if manifest["format_version"] == FORMAT_VERSION and ("imports" not in manifest or "staging_root" not in manifest):
        raise ValueError("Incomplete managed-upload backup manifest")
    files, documents = manifest.get("files"), manifest.get("documents")
    imports = manifest.get("imports", [])
    if not isinstance(imports, list):
        raise ValueError("Invalid managed import inventory")
    if not isinstance(files, dict) or not isinstance(documents, list) or _DATABASE not in files:
        raise ValueError("Incomplete backup manifest")
    for name, expected in files.items():
        _relative(name)
        if not isinstance(expected, dict) or not _HASH.fullmatch(str(expected.get("sha256", ""))):
            raise ValueError("Invalid file checksum in manifest")
        if type(expected.get("size")) is not int or expected["size"] < 0:
            raise ValueError("Invalid file size in manifest")
    if _inventory(root) != set(files) | {_MANIFEST}:
        raise ValueError("Bundle files do not match the manifest")
    for name, expected in files.items():
        if _digest(root / name) != expected:
            raise ValueError(f"Backup checksum mismatch: {name}")
    info, rows = _database_info(root / _DATABASE)
    if manifest.get("database") != info:
        raise ValueError("Database metadata does not match the manifest")
    by_id = {row["id"]: row for row in rows}
    seen, referenced = set(), set()
    for doc in documents:
        if not isinstance(doc, dict) or not isinstance(doc.get("document_id"), str):
            raise ValueError("Invalid document entry")
        doc_id, name = doc["document_id"], _relative(doc.get("path"))
        if doc_id in seen or doc_id not in by_id or name == _DATABASE or name not in files:
            raise ValueError("Invalid document mapping")
        digest = by_id[doc_id]["content_hash"]
        if not _HASH.fullmatch(str(digest)) or files[name]["sha256"] != digest or doc.get("content_hash") != digest:
            raise ValueError("Original does not match the document content hash")
        if not name.startswith("originals/") or PurePosixPath(name).parts[1] != digest[:2] or not PurePosixPath(name).name.startswith(digest):
            raise ValueError("Original has an inconsistent content-addressed path")
        seen.add(doc_id)
        referenced.add(name)
    staged_referenced, staged_seen = set(), set()
    if imports or "staging_root" in manifest:
        staging_root = manifest.get("staging_root")
        if not isinstance(staging_root, str) or len(staging_root) > 4096 or "\x00" in staging_root or not Path(staging_root).is_absolute() or Path(staging_root).name != "staging" or not str(Path(staging_root).parent).endswith(".archive"):
            raise ValueError("Invalid managed staging root")
        staging_root = Path(staging_root)
        if imports:
            _reject_import_triggers(root / _DATABASE)
        staged_rows = {(item["job_id"], item["ordinal"]): item for item in _staged_items(root / _DATABASE, staging_root)}
        for entry in imports:
            if not isinstance(entry, dict) or not isinstance(entry.get("job_id"), str) or type(entry.get("ordinal")) is not int:
                raise ValueError("Invalid managed import mapping")
            marker = (entry["job_id"], entry["ordinal"])
            name = _relative(entry.get("path"))
            row = staged_rows.get(marker)
            if marker in staged_seen or row is None or not name.startswith("staging/") or name not in files or entry.get("source_path") != row["path"] or str(staging_root.parent / name) != row["path"]:
                raise ValueError("Invalid managed import mapping")
            if not _HASH.fullmatch(str(row["content_hash"])) or files[name] != {"sha256": row["content_hash"], "size": row["size_bytes"]}:
                raise ValueError("Staged upload does not match its immutable import manifest")
            staged_seen.add(marker)
            staged_referenced.add(name)
        if staged_seen != set(staged_rows):
            raise ValueError("Managed import inventory is incomplete")
    if seen != set(by_id) or referenced | staged_referenced != set(files) - {_DATABASE}:
        raise ValueError("Document/original inventory is incomplete")
    report = {"ok": True, "format_version": manifest["format_version"], "bundle": str(root),
              "database": str(root / _DATABASE), "documents": len(documents),
              "originals": len(referenced), **info, "limitations": list(_LIMITATIONS)}
    report["staged_uploads"] = len(staged_referenced)
    return manifest, report


def verify_backup(bundle_path: str | Path) -> dict:
    """Verify every byte, SQLite integrity, and document mapping; raise on damage."""
    return _load_and_verify(_path(bundle_path))[1]


def create_backup(db_path: str | Path, bundle_path: str | Path) -> dict:
    """Snapshot a live SQLite database and managed originals into a new bundle.

    Every document version, including forgotten/sensitive records, is preserved.
    Managed originals and resumable staged uploads are copied. Missing or damaged
    bytes fail the backup instead of silently making it partial. External sources
    are descriptors only and are never traversed by the backup.
    """
    source, destination = _path(db_path), _path(bundle_path)
    _fresh(destination)
    if not source.is_file():
        raise ValueError("Source database must be an existing regular file")
    if not destination.parent.is_dir():
        raise ValueError("Backup parent directory must already exist")
    stage = Path(tempfile.mkdtemp(prefix=".proharness-backup-", dir=destination.parent))
    published = False
    try:
        with closing(_readonly(source)) as src, closing(sqlite3.connect(stage / _DATABASE)) as dest:
            src.backup(dest)
            dest.execute("PRAGMA journal_mode=DELETE")
        os.chmod(stage / _DATABASE, 0o600)
        info, documents = _database_info(stage / _DATABASE)
        files, mappings = {_DATABASE: _digest(stage / _DATABASE)}, []
        staging_root = Path(str(source) + ".archive") / "staging"
        import_mappings = []
        for item in _staged_items(stage / _DATABASE, staging_root):
            path = _path(item["path"])
            relative = _relative(path.relative_to(staging_root.parent).as_posix())
            if relative not in files:
                files[relative] = _copy(path, stage / relative)
            if files[relative] != {"sha256": item["content_hash"], "size": item["size_bytes"]}:
                raise ValueError("Staged upload does not match its immutable import manifest")
            import_mappings.append({"job_id": item["job_id"], "ordinal": item["ordinal"],
                                    "source_path": item["path"], "path": relative})
        originals_root = Path(str(source) + ".archive") / "originals"
        for doc in documents:
            digest = doc["content_hash"]
            if not isinstance(digest, str) or not _HASH.fullmatch(digest):
                raise ValueError("Invalid document content hash")
            original = _path(doc["original_path"])
            suffix = original.suffix.lower()
            if suffix and not re.fullmatch(r"\.[a-z0-9_-]{1,24}", suffix):
                raise ValueError("Unsupported original file suffix")
            if original != originals_root / digest[:2] / (digest + suffix):
                raise ValueError("Document original is outside its managed archive")
            relative = f"originals/{digest[:2]}/{digest}{suffix}"
            if relative not in files:
                files[relative] = _copy(original, stage / relative)
            if files[relative]["sha256"] != digest:
                raise ValueError("Managed original content hash mismatch")
            mappings.append({"document_id": doc["id"], "content_hash": digest, "path": relative})
        manifest = {"format_version": FORMAT_VERSION, "created_at": time.time(),
                    "database": info, "documents": mappings, "files": files,
                    "staging_root": str(staging_root), "imports": import_mappings}
        with (stage / _MANIFEST).open("x", encoding="utf-8") as stream:
            json.dump(manifest, stream, sort_keys=True, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        _load_and_verify(stage)
        # Reserve the destination exclusively. No rename can replace an existing
        # directory, even if another process creates it after the initial check.
        destination.mkdir(mode=0o700)
        published = True
        for entry in sorted(stage.iterdir(), key=lambda item: item.name == _MANIFEST):
            os.rename(entry, destination / entry.name)
        return verify_backup(destination)
    except Exception:
        if published:
            shutil.rmtree(destination)
        raise
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def restore_backup(bundle_path: str | Path, target_db_path: str | Path) -> dict:
    """Validate a bundle, relocate originals, and publish to a fresh database.

    The target database, its sidecars, and its sibling archive must not exist.
    Database publication happens last using an exclusive hard link; a failed
    restore removes only files created by this invocation.
    """
    bundle, target = _path(bundle_path), _path(target_db_path)
    if target.is_relative_to(bundle):
        raise ValueError("Restore target must be outside the backup bundle")
    archive = Path(str(target) + ".archive")
    for path in (target, archive, *(Path(str(target) + suffix) for suffix in ("-wal", "-shm", "-journal"))):
        _fresh(path)
    if not target.parent.is_dir():
        raise ValueError("Restore parent directory must already exist")
    manifest, report = _load_and_verify(bundle)
    stage = Path(tempfile.mkdtemp(prefix=".proharness-restore-", dir=target.parent))
    archive_created = False
    database_created = False
    try:
        for name, expected in manifest["files"].items():
            if _copy(bundle / name, stage / name) != expected:
                raise ValueError("Backup changed during restore")
        with closing(sqlite3.connect(stage / _DATABASE)) as conn:
            conn.execute("PRAGMA trusted_schema=OFF")
            # Only the already verified staging database is changed. Suspending
            # catalog triggers preserves manifest row counts during relocation;
            # invalidating the signature forces a fresh index before reuse.
            from proharness.memory.catalog_index import DOCUMENT_TRIGGERS as CATALOG_TRIGGERS
            catalog_triggers = [name for name, in conn.execute(
                "SELECT name FROM sqlite_schema WHERE type='trigger'") if name in CATALOG_TRIGGERS]
            for name in catalog_triggers:
                conn.execute(f'DROP TRIGGER "{name}"')
            if conn.execute("SELECT 1 FROM sqlite_schema WHERE name='archive_catalog_meta'").fetchone():
                conn.execute("UPDATE archive_catalog_meta SET value='invalidated-by-restore' WHERE key='signature'")
            for doc in manifest["documents"]:
                conn.execute("UPDATE archive_documents SET original_path=? WHERE id=?",
                             (str(archive / doc["path"]), doc["document_id"]))
            relocations = {entry["source_path"]: str(archive / entry["path"]) for entry in manifest.get("imports", [])}
            affected_jobs = set()
            tables = {row[0] for row in conn.execute("SELECT name FROM sqlite_schema WHERE type='table'")}
            for old, new in relocations.items():
                affected_jobs.update(row[0] for row in conn.execute("SELECT DISTINCT job_id FROM archive_import_items WHERE path=?", (old,)))
                conn.execute("UPDATE archive_import_items SET path=? WHERE path=?", (new, old))
                if "archive_documents" in tables:
                    previous_identity = hashlib.sha256(old.encode()).hexdigest()
                    if conn.execute("SELECT 1 FROM archive_documents WHERE source_path=? AND source_identity<>?", (old, previous_identity)).fetchone():
                        raise ValueError("Managed source identity cannot be safely relocated")
                    conn.execute("UPDATE archive_documents SET source_path=?,source_identity=? WHERE source_path=?",
                                 (new, hashlib.sha256(new.encode()).hexdigest(), old))
            for job in affected_jobs:
                items = [{"path": row[0], "size_bytes": row[1], "content_hash": row[2]} for row in conn.execute(
                    "SELECT path,size_bytes,content_hash FROM archive_import_items WHERE job_id=? ORDER BY path", (job,))]
                digest = hashlib.sha256(json.dumps(items, sort_keys=True, separators=(",", ":")).encode()).hexdigest()
                conn.execute("UPDATE archive_import_jobs SET manifest_hash=? WHERE id=?", (digest, job))
            for name in catalog_triggers:
                conn.execute(CATALOG_TRIGGERS[name])
            conn.commit()
        restored_info, restored_docs = _database_info(stage / _DATABASE)
        if restored_info != manifest["database"]:
            raise ValueError("Restored database metadata changed unexpectedly")
        locations = {doc["document_id"]: str(archive / doc["path"]) for doc in manifest["documents"]}
        if any(doc["original_path"] != locations[doc["id"]] for doc in restored_docs):
            raise ValueError("Restored document paths did not relocate")
        for path in (target, archive, *(Path(str(target) + suffix) for suffix in ("-wal", "-shm", "-journal"))):
            _fresh(path)
        archive.mkdir(mode=0o700)
        archive_created = True
        if (stage / "originals").exists():
            os.rename(stage / "originals", archive / "originals")
        if (stage / "staging").exists():
            os.rename(stage / "staging", archive / "staging")
        os.link(stage / _DATABASE, target)  # Atomic exclusive publication; never overwrite.
        database_created = True
        return {**report, "database": str(target), "archive": str(archive), "restored": True}
    except Exception:
        if database_created:
            target.unlink()
        if archive_created:
            shutil.rmtree(archive)
        raise
    finally:
        shutil.rmtree(stage, ignore_errors=True)
