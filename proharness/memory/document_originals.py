"""Read-only access to exact managed originals, with live visibility checks.

Private keys contain bytes/identity for trusted adapters; never JSON serialize
these keys. Catalog paths and renderer/source content are untrusted data.
"""
from __future__ import annotations

from contextlib import closing
import errno
import hashlib
import os
from pathlib import Path
import re
import sqlite3
import stat

from proharness.memory.documents import _archive_schema_ready
from proharness.memory.library_health import _managed_path, _read_snapshot

MAX_ORIGINAL_BYTES = 100 * 1024 * 1024
_NOT_FOUND = "Document not found or excluded."


def error(code, message, status=409):
    return {"error": message, "error_code": code, "http_status": status}


def _valid_input(document_id, include_sensitive):
    if not isinstance(document_id, str) or not document_id or len(document_id) > 100 or "\x00" in document_id:
        return error("invalid_input", "document_id must be a nonempty string of at most 100 characters.", 422)
    if type(include_sensitive) is not bool:
        return error("invalid_input", "include_sensitive must be boolean.", 422)
    return None


def _row(db, document_id, include_sensitive):
    if not _archive_schema_ready(db):
        return error("archive_unavailable", "Document archive unavailable.", 404)
    cursor = db.execute("SELECT id,version,source_identity,content_hash,original_path,media_type,source_path,sensitive,deleted_at "
                        "FROM archive_documents WHERE id=? AND length(source_identity)<=4096 "
                        "AND length(source_path)<=4096 AND length(original_path)<=4096 "
                        "AND length(content_hash)<=64 AND length(media_type)<=120", (document_id,))
    values = cursor.fetchone()
    row = dict(zip((c[0] for c in cursor.description), values)) if values is not None else None
    if row is None or row["deleted_at"] is not None or (row["sensitive"] and not include_sensitive):
        return error("document_unavailable", _NOT_FOUND, 404)
    latest_values = db.execute("SELECT sensitive,deleted_at FROM archive_documents WHERE source_identity=? ORDER BY version DESC LIMIT 1",
                               (row["source_identity"],)).fetchone()
    latest = dict(zip(("sensitive", "deleted_at"), latest_values)) if latest_values is not None else None
    if latest is None or latest["deleted_at"] is not None or (latest["sensitive"] and not include_sensitive):
        return error("document_unavailable", _NOT_FOUND, 404)
    # Match attribution/ingestion's transitive source/hash privacy closure.
    # Tombstones remain seeds: forgetting a private copy cannot declassify
    # an older public version or another source containing the same bytes.
    if not include_sensitive and db.execute(
        "WITH RECURSIVE related(id,source_identity,content_hash,sensitive) AS ("
        "SELECT d.id,d.source_identity,d.content_hash,d.sensitive FROM archive_documents d WHERE d.id=? "
        "UNION SELECT d.id,d.source_identity,d.content_hash,d.sensitive "
        "FROM archive_documents d JOIN related r "
        "ON d.source_identity=r.source_identity OR d.content_hash=r.content_hash) "
        "SELECT 1 FROM related WHERE sensitive<>0 LIMIT 1", (document_id,),
    ).fetchone():
        return error("document_unavailable", _NOT_FOUND, 404)
    # Bound/type-check labels before using them as an identity or response.
    if type(row["version"]) is not int or row["version"] < 1 or not isinstance(row["source_identity"], str) or len(row["source_identity"]) > 4096:
        return error("archive_unavailable", "Document metadata unavailable.")
    if not isinstance(row["media_type"], str) or len(row["media_type"]) > 120 or not re.fullmatch(r"[a-zA-Z0-9.+-]+/[a-zA-Z0-9.+-]+", row["media_type"]):
        return error("archive_unavailable", "Document metadata unavailable.")
    return dict(row)


def _identity(row):
    return tuple(row[k] for k in ("id", "version", "source_identity", "content_hash", "original_path", "media_type", "source_path"))


def _db_file(db):
    return next((row[2] for row in db.execute("PRAGMA database_list") if row[1] == "main"), "")


def _read_managed(path):
    """Open every ancestor and leaf without following symlinks, then bound reads."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    parent = stream = None
    try:
        parent = os.open(path.anchor, flags | os.O_DIRECTORY)
        for part in path.parts[1:-1]:
            info = os.stat(part, dir_fd=parent, follow_symlinks=False)
            if not stat.S_ISDIR(info.st_mode):
                return error("unsafe_original_path", "Managed original unavailable.")
            following = os.open(part, flags | os.O_DIRECTORY, dir_fd=parent)
            os.close(parent)
            parent = following
        info = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode):
            return error("unsafe_original_path", "Managed original unavailable.")
        stream = os.open(path.name, flags, dir_fd=parent)
        before = os.fstat(stream)
        if not stat.S_ISREG(before.st_mode):
            return error("unsafe_original_path", "Managed original unavailable.")
        if before.st_size > MAX_ORIGINAL_BYTES:
            return error("original_too_large", "Managed original exceeds the 100 MiB limit.", 413)
        parts, count = [], 0
        while count <= MAX_ORIGINAL_BYTES:
            part = os.read(stream, min(1024 * 1024, MAX_ORIGINAL_BYTES + 1 - count))
            if not part:
                break
            count += len(part)
            parts.append(part)
        after = os.fstat(stream)
        if count > MAX_ORIGINAL_BYTES:
            return error("original_too_large", "Managed original exceeds the 100 MiB limit.", 413)
        if after.st_size != count or after.st_size != before.st_size or after.st_mtime_ns != before.st_mtime_ns or after.st_ctime_ns != before.st_ctime_ns:
            return error("original_changed", "Managed original changed during verification.")
        return b"".join(parts)
    except FileNotFoundError:
        return error("original_unavailable", "Managed original unavailable.", 404)
    except OSError as exc:
        return error("unsafe_original_path" if exc.errno in (errno.ELOOP, errno.ENOTDIR) else "original_unavailable", "Managed original unavailable.")
    finally:
        if stream is not None:
            os.close(stream)
        if parent is not None:
            os.close(parent)


def reauthorize_document_original(store, document_id, identity, *, include_sensitive=False):
    """Read fresh committed visibility, independent of a caller's pinned snapshot."""
    invalid = _valid_input(document_id, include_sensitive)
    if invalid:
        return invalid
    try:
        with store._lock:
            filename = _db_file(store._sqlite)
        if not filename:
            return error("original_location_unavailable", "Managed original location unavailable.")
        uri = Path(filename).absolute().as_uri() + "?mode=ro"
        with closing(sqlite3.connect(uri, uri=True, timeout=1)) as db:
            db.execute("BEGIN")
            row = _row(db, document_id, include_sensitive)
            if "error" in row:
                return row
            if _identity(row) != identity:
                return error("original_changed", "Document original metadata changed; retry.")
        return None
    except (sqlite3.Error, AttributeError, OSError, TypeError, ValueError):
        return error("archive_unavailable", "Document archive unavailable.")


def load_document_original(store, document_id, *, include_sensitive=False):
    """Return verified exact-version bytes without creating schema or an archive."""
    invalid = _valid_input(document_id, include_sensitive)
    if invalid:
        return invalid
    try:
        with _read_snapshot(store) as db:
            row = _row(db, document_id, include_sensitive)
            if "error" in row:
                return row
            path, code = _managed_path(_db_file(db), row["original_path"], row["content_hash"])
            if code:
                return error(code, "Managed original unavailable.")
            identity = _identity(row)
        data = _read_managed(path)
        if isinstance(data, dict):
            return data
        if hashlib.sha256(data).hexdigest() != row["content_hash"]:
            return error("original_integrity_failed", "Managed original failed integrity verification.")
        changed = reauthorize_document_original(store, document_id, identity, include_sensitive=include_sensitive)
        if changed:
            return changed
        suffix = Path(row["source_path"]).suffix.lower() if isinstance(row["source_path"], str) else ""
        if not re.fullmatch(r"(?:\.[a-z0-9_-]{1,24})?", suffix):
            suffix = ""
        return {"document_id": row["id"], "version": row["version"],
                "original_content_hash": row["content_hash"], "media_type": row["media_type"],
                "source_extension": suffix, "_original_bytes": data, "_original_identity": identity}
    except (sqlite3.Error, AttributeError, OSError, TypeError, ValueError):
        return error("archive_unavailable", "Document archive unavailable.")
