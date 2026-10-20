"""Read-only catalog diagnostics, never evidence of a source's truth.

No archive constructor, embedder, backend, repair, schema initialization, or
network call is used. Counts describe the visible current scope, even when the
returned issue list is limited. Catalog paths and metadata are untrusted.
"""
from __future__ import annotations

import errno
import hashlib
import json
import math
import os
import re
import sqlite3
import stat
from collections import Counter
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

from proharness.memory.evidence import _source_refs, is_sensitive_memory
from proharness.memory.schemas import Memory

_HASH = re.compile(r"[0-9a-f]{64}\Z")
_SUFFIX = re.compile(r"(?:\.[a-z0-9_-]{1,24})?\Z")
_MAX_METADATA = 65_536
_MAX_VECTOR = 1_048_576
_MAX_FILE = 100 * 1024 * 1024
_MAX_HASH_BYTES = 256 * 1024 * 1024
_PRIVATE = (
    "WITH RECURSIVE private(id,source_identity,content_hash) AS ("
    "SELECT id,source_identity,content_hash FROM archive_documents WHERE sensitive<>0 "
    "UNION SELECT d.id,d.source_identity,d.content_hash FROM archive_documents d "
    "JOIN private p ON d.source_identity=p.source_identity OR d.content_hash=p.content_hash) "
)


@contextmanager
def _read_snapshot(store):
    """Pin all catalog/privacy reads together without ending a caller's work."""
    with store._lock:
        db = store._sqlite
        owns_transaction = not db.in_transaction
        if owns_transaction:
            db.execute("BEGIN")
        try:
            yield db
        finally:
            # Only our read transaction is ended. Never commit or roll back a
            # transaction that predates this diagnostic call, even on failure.
            if owns_transaction:
                db.rollback()


def _json(raw, expected):
    if not isinstance(raw, str) or len(raw) > _MAX_METADATA:
        return None
    try:
        result = json.loads(raw)
    except (ValueError, RecursionError):
        return None
    return result if isinstance(result, expected) else None


def _dated(value):
    if not isinstance(value, str) or not value.strip():
        return "missing_source_date"
    value = value.strip()
    try:
        if re.fullmatch(r"\d{4}", value):
            date(int(value), 1, 1)
        elif re.fullmatch(r"\d{4}-\d{2}", value):
            date.fromisoformat(value + "-01")
        elif re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
            date.fromisoformat(value)
        else:
            datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return "unparseable_source_date"
    return None


def _managed_path(db_file, raw, digest):
    if not isinstance(digest, str) or not _HASH.fullmatch(digest):
        return None, "invalid_content_hash"
    if not db_file:
        return None, "original_location_unavailable"
    if not isinstance(raw, str) or len(raw) > 4096 or "\x00" in raw:
        return None, "unsafe_original_path"
    path = Path(raw)
    suffix = path.suffix
    expected = Path(os.path.abspath(db_file + ".archive")) / "originals" / digest[:2] / (digest + suffix)
    # Exact spelling rejects traversal, unexpected locations and relative paths
    # before making any filesystem call on catalog-provided components.
    if not _SUFFIX.fullmatch(suffix) or raw != str(expected):
        return None, "unsafe_original_path"
    return expected, None


def _inspect_original(path, *, verify, budget):
    """Traverse with directory descriptors so symlink swaps cannot escape root."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0)
    parent = None
    stream_fd = None
    read_bytes = 0
    try:
        parent = os.open(path.anchor, flags | os.O_DIRECTORY)
        for part in path.parts[1:-1]:
            try:
                info = os.stat(part, dir_fd=parent, follow_symlinks=False)
                if stat.S_ISLNK(info.st_mode):
                    return "unsafe_original_path", None, 0
                next_fd = os.open(part, flags | os.O_DIRECTORY, dir_fd=parent)
            except OSError as exc:
                if exc.errno in (errno.ELOOP, errno.ENOTDIR):
                    return "unsafe_original_path", None, 0
                raise
            os.close(parent)
            parent = next_fd
        info = os.stat(path.name, dir_fd=parent, follow_symlinks=False)
        if not stat.S_ISREG(info.st_mode):
            return "unsafe_original_path", None, 0
        if not verify:
            return None, None, 0
        if info.st_size > min(_MAX_FILE, budget):
            return "original_verification_budget_exceeded", None, 0
        stream_fd = os.open(path.name, flags, dir_fd=parent)
        opened = os.fstat(stream_fd)
        if not stat.S_ISREG(opened.st_mode):
            return "unsafe_original_path", None, 0
        if opened.st_size > min(_MAX_FILE, budget):
            return "original_verification_budget_exceeded", None, 0
        digest = hashlib.sha256()
        cap = min(_MAX_FILE, budget)
        while read_bytes < cap:
            data = os.read(stream_fd, min(1024 * 1024, cap - read_bytes))
            if not data:
                break
            read_bytes += len(data)
            digest.update(data)
        # Detect a concurrently growing file without reading beyond the cap.
        after = os.fstat(stream_fd)
        if after.st_size > read_bytes or after.st_mtime_ns != opened.st_mtime_ns:
            return "original_changed_during_verification", None, read_bytes
        return None, digest.hexdigest(), read_bytes
    except FileNotFoundError:
        return "missing_original", None, read_bytes
    except OSError as exc:
        code = "unsafe_original_path" if exc.errno in (errno.ELOOP, errno.ENOTDIR) else "original_unreadable"
        return code, None, read_bytes
    finally:
        if stream_fd is not None:
            os.close(stream_fd)
        if parent is not None:
            os.close(parent)


def library_health(store, *, include_sensitive=False, verify_originals=False, limit=100) -> dict:
    """Diagnose visible current records without changing the store or originals.

    ``limit`` bounds the combined issue list, not the scan or totals. Optional
    SHA-256 verification streams at most 100 MiB per original and 256 MiB total.
    Privacy inherits across all versions/copies, including deleted sources.
    All catalog checks share a SQLite read snapshot; a caller's existing
    transaction is reused and never committed or rolled back here.
    Only ``_sqlite`` and ``_lock`` are required from the store, allowing CLI
    callers to provide a read-only adapter without initializing a runtime.
    """
    if type(include_sensitive) is not bool or type(verify_originals) is not bool:
        raise ValueError("include_sensitive and verify_originals must be booleans")
    if type(limit) is not int or not 1 <= limit <= 1000:
        raise ValueError("limit must be an integer between 1 and 1000")
    counts = Counter()
    report = {
        "read_only": True, "diagnostics_only": True, "include_sensitive": include_sensitive,
        "scope": {"documents": 0, "chunks": 0, "memories": 0, "evidence_refs": 0,
                  "selection": "current active visible documents and active visible memories",
                  "historical_and_deleted_excluded": True,
                  "consistency": "single SQLite read snapshot"},
        "vector_indexes": {"archive_chunks": "persisted chunk vectors",
                           "memory_vec": "not_present", "external": "not_inspected"},
        "issues": [], "warnings": [
            "Diagnostic consistency does not verify claims or authenticity.",
            "Missing or unparseable source dates are reported; no expiry policy or age threshold is assumed.",
            "Vector fingerprints are compared with the persisted index fingerprint; no configured model or external index is inspected.",
            "Records whose privacy cannot be established are omitted without excluded-record counts.",
            "Catalog visibility is evaluated within one SQLite snapshot; original files are inspected separately and may change concurrently.",
        ],
        "verification": {"requested": verify_originals, "checked_originals": 0,
                         "bytes_read": 0, "max_file_bytes": _MAX_FILE, "max_total_bytes": _MAX_HASH_BYTES},
    }

    def issue(code, *, document_id=None, chunk_id=None, memory_id=None, ref_index=None):
        counts[code] += 1
        if len(report["issues"]) < limit:
            item = {"code": code}
            for key, value in (("document_id", document_id), ("chunk_id", chunk_id), ("memory_id", memory_id)):
                if value is not None:
                    item[key] = str(value)[:128]
            if ref_index is not None:
                item["ref_index"] = ref_index
            report["issues"].append(item)

    with _read_snapshot(store) as db:
        tables = {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        archived = "archive_documents" in tables
        report["archive_available"] = archived
        fp = None
        if "store_meta" in tables:
            row = db.execute("SELECT value FROM store_meta WHERE key='embedder_fingerprint'").fetchone()
            fp = row[0] if row else None
        if not fp:
            report["warnings"].append("Persisted embedding fingerprint unavailable; fingerprint comparison skipped.")
        dimension = None
        schema = db.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name='memory_vec'").fetchone()
        match = re.search(r"embedding\s+float\[(\d+)\]", schema[0] or "", re.I) if schema else None
        hashing = re.fullmatch(r"hashing:(\d+)", fp) if isinstance(fp, str) else None
        if match or hashing:
            dimension = int((match or hashing).group(1))
        else:
            report["warnings"].append("Persisted vector dimension unavailable; dimensional consistency cannot be established.")
        memory_vectors_available = False
        if "memory_vec" in tables:
            try:
                # Some read-only CLI connections have no sqlite-vec extension.
                # Probe without loading libraries, initializing a model or
                # selecting vector data from private records.
                db.execute("SELECT id FROM memory_vec LIMIT 0").fetchall()
                memory_vectors_available = True
                report["vector_indexes"]["memory_vec"] = "persisted membership checked"
            except sqlite3.DatabaseError:
                report["vector_indexes"]["memory_vec"] = "unavailable"
                report["warnings"].append("Persistent local memory vector table is unavailable to this connection; memory vector diagnostics skipped.")
        else:
            report["warnings"].append("No persistent local memory vector table exists; in-memory and external memory vectors are not inspected.")
        db_file = next((row[2] for row in db.execute("PRAGMA database_list") if row[1] == "main"), "")
        current = "d.deleted_at IS NULL AND NOT EXISTS (SELECT 1 FROM archive_documents n WHERE n.source_identity=d.source_identity AND n.version>d.version)"
        prefix = "" if include_sensitive else _PRIVATE
        visible = current + ("" if include_sensitive else " AND NOT EXISTS (SELECT 1 FROM private p WHERE p.id=d.id)")
        private_ids = {row[0] for row in db.execute(_PRIVATE + "SELECT id FROM private")} if archived and not include_sensitive else set()
        duplicate_hashes = set()
        if archived:
            duplicate_hashes = {row[0] for row in db.execute(
                prefix + "SELECT content_hash FROM archive_documents d WHERE " + visible
                + " GROUP BY content_hash HAVING count(DISTINCT source_identity)>1")}
            docs = db.execute(prefix + "SELECT d.id,substr(d.content_hash,1,65),substr(d.original_path,1,4097),substr(d.document_date,1,200),"
                              "substr(d.warnings,1,65537),d.extraction_status FROM archive_documents d WHERE "
                              + visible + " ORDER BY d.id")
            verified = {}
            for doc in docs:
                document_id, digest, original, source_date, warnings, extraction = doc
                report["scope"]["documents"] += 1
                kwargs = {"document_id": document_id}
                if digest in duplicate_hashes:
                    issue("duplicate_current_content", **kwargs)
                code = _dated(source_date)
                if code:
                    issue(code, **kwargs)
                parsed = _json(warnings, list)
                if parsed is None or any(not isinstance(w, str) for w in parsed):
                    issue("invalid_extraction_warnings", **kwargs)
                elif parsed:
                    issue("extraction_warnings", **kwargs)
                    if any(re.search(r"OCR|scanned|no searchable|little or no", w, re.I) for w in parsed):
                        issue("ocr_review_needed", **kwargs)
                if extraction != "extracted":
                    issue("extraction_needs_review", **kwargs)
                path, code = _managed_path(db_file, original, digest)
                if not code:
                    cache_key = str(path)
                    if cache_key not in verified:
                        verification = report["verification"]
                        checked = _inspect_original(path, verify=verify_originals,
                                                    budget=_MAX_HASH_BYTES - verification["bytes_read"])
                        verified[cache_key] = checked
                        verification["bytes_read"] += checked[2]
                        if checked[1] is not None:
                            verification["checked_originals"] += 1
                    code, actual, _ = verified[cache_key]
                    if actual is not None and actual != digest:
                        code = "original_checksum_mismatch"
                if code:
                    issue(code, **kwargs)
            if "archive_chunks" not in tables or "archive_sections" not in tables:
                report["warnings"].append("Archive chunk/section tables unavailable; passage diagnostics are incomplete.")
            else:
                chunks = db.execute(prefix + "SELECT c.id,c.document_id,c.start,c.end,substr(c.text,1,65537),"
                                    "length(c.text),substr(c.vector,1,1048577),c.embedder_fingerprint,"
                                    "s.document_id,length(s.text),substr(s.text,c.start+1,max(0,min(65537,c.end-c.start))) "
                                    "FROM archive_chunks c JOIN archive_documents d ON d.id=c.document_id "
                                    "LEFT JOIN archive_sections s ON s.id=c.section_id WHERE " + visible
                                    + " ORDER BY c.document_id,c.ordinal,c.id")
                for row in chunks:
                    cid, did, start, end, text, text_len, vector, fingerprint, parent, section_len, span = row
                    report["scope"]["chunks"] += 1
                    kwargs = {"document_id": did, "chunk_id": cid}
                    if parent is None:
                        issue("chunk_missing_section", **kwargs)
                    elif parent != did:
                        issue("chunk_section_document_mismatch", **kwargs)
                    elif (type(start) is not int or type(end) is not int or start < 0 or end <= start
                          or end > section_len or end - start != text_len or text_len > _MAX_METADATA or span != text):
                        issue("chunk_span_mismatch", **kwargs)
                    if vector is None or vector == "":
                        issue("chunk_vector_missing", **kwargs)
                    else:
                        try:
                            values = json.loads(vector) if len(vector) <= _MAX_VECTOR else None
                            valid = isinstance(values, list) and bool(values) and all(
                                type(v) in (int, float) and math.isfinite(v) and abs(v) <= 3.4028234663852886e38 for v in values)
                        except (ValueError, TypeError, RecursionError, OverflowError):
                            valid = False
                        if not valid:
                            issue("chunk_vector_invalid", **kwargs)
                        else:
                            if dimension and len(values) != dimension:
                                issue("chunk_vector_dimension_mismatch", **kwargs)
                            if fp and fingerprint != fp:
                                issue("chunk_vector_fingerprint_mismatch", **kwargs)
                empty = db.execute(prefix + "SELECT d.id FROM archive_documents d WHERE " + visible
                                   + " AND NOT EXISTS (SELECT 1 FROM archive_chunks c WHERE c.document_id=d.id) ORDER BY d.id")
                for row in empty:
                    issue("document_has_no_chunks", document_id=row[0])
        else:
            report["warnings"].append("Archive not initialized; no document tables were created.")

        if "memories" in tables:
            for row in db.execute("SELECT id,substr(text,1,65537),substr(metadata,1,65537),"
                                  "substr(tags,1,65537),source FROM memories WHERE deleted_at IS NULL ORDER BY id"):
                mid, text, raw_meta, raw_tags, source = row
                metadata, tags = _json(raw_meta, dict), _json(raw_tags, list)
                if (metadata is None or tags is None or any(not isinstance(tag, str) for tag in tags)
                        or not isinstance(text, str) or len(text) > _MAX_METADATA):
                    if include_sensitive:
                        issue("memory_metadata_unavailable", memory_id=mid)
                    # Unable to establish visibility; fail closed, no excluded count.
                    continue
                if metadata.get("superseded_by"):
                    continue
                memory = Memory(id=mid, text=text, source=source, tags=tags, metadata=metadata)
                refs = _source_refs(memory)
                if not include_sensitive:
                    sensitive = is_sensitive_memory(memory) or any(
                        r.get("document_id") in private_ids for r in refs if isinstance(r.get("document_id"), str))
                    if not sensitive and "archive_chunks" in tables:
                        for r in refs:
                            if isinstance(r.get("chunk_id"), str):
                                parent = db.execute("SELECT document_id FROM archive_chunks WHERE id=?", (r["chunk_id"],)).fetchone()
                                if parent and parent[0] in private_ids:
                                    sensitive = True
                                    break
                    if sensitive:
                        continue
                report["scope"]["memories"] += 1
                if memory_vectors_available and db.execute("SELECT 1 FROM memory_vec WHERE id=? LIMIT 1", (mid,)).fetchone() is None:
                    issue("memory_vector_missing", memory_id=mid)
                raw_refs = metadata.get("evidence_refs", metadata.get("sources", metadata.get("evidence", [])))
                if isinstance(raw_refs, dict):
                    raw_refs = [raw_refs]
                if not isinstance(raw_refs, list):
                    issue("invalid_evidence_refs", memory_id=mid)
                    continue
                for index, ref in enumerate(raw_refs):
                    report["scope"]["evidence_refs"] += 1
                    kwargs = {"memory_id": mid, "ref_index": index}
                    if not isinstance(ref, dict) or not all(isinstance(ref.get(k), str) and ref[k] for k in ("document_id", "chunk_id")):
                        issue("invalid_evidence_ref", **kwargs)
                        continue
                    if not archived or "archive_chunks" not in tables:
                        issue("evidence_source_missing", **kwargs)
                        continue
                    doc = db.execute("SELECT deleted_at,version,(SELECT MAX(version) FROM archive_documents n "
                                     "WHERE n.source_identity=d.source_identity) FROM archive_documents d WHERE id=?",
                                     (ref["document_id"],)).fetchone()
                    chunk = db.execute("SELECT document_id,substr(text,1,65537),start,end,section_id FROM archive_chunks WHERE id=?",
                                       (ref["chunk_id"],)).fetchone()
                    if doc is None or chunk is None:
                        issue("evidence_source_missing", **kwargs)
                        continue
                    if chunk[0] != ref["document_id"]:
                        issue("evidence_document_mismatch", **kwargs)
                        continue
                    if doc[0] is not None:
                        issue("evidence_source_deleted", **kwargs)
                        continue
                    if doc[1] != doc[2]:
                        issue("evidence_source_stale", **kwargs)
                    section = (db.execute("SELECT page,section FROM archive_sections WHERE id=?", (chunk[4],)).fetchone()
                               if "archive_sections" in tables else None)
                    if section is None:
                        issue("evidence_section_missing", **kwargs)
                    quote = ref.get("quote")
                    if quote is not None and (not isinstance(quote, str) or not quote.strip() or quote not in chunk[1]):
                        issue("evidence_quote_mismatch", **kwargs)
                    location = ref.get("location")
                    if location is not None:
                        if not isinstance(location, dict):
                            issue("evidence_location_mismatch", **kwargs)
                        elif "archive_sections" in tables:
                            expected = {"page": section[0] if section else None, "section": section[1] if section else None}
                            mismatch = any(k not in {"start", "end", "page", "section"} for k in location)
                            mismatch |= any(type(location[k]) is not type(v) or location[k] != v
                                            for k, v in expected.items() if k in location)
                            if "start" in location or "end" in location:
                                start, end = location.get("start"), location.get("end")
                                valid_span = (type(start) is int and type(end) is int
                                              and type(chunk[2]) is int and type(chunk[3]) is int
                                              and chunk[2] <= start < end <= chunk[3])
                                mismatch |= not valid_span
                                if valid_span and quote is not None:
                                    mismatch |= chunk[1][start - chunk[2]:end - chunk[2]] != quote
                            if mismatch:
                                issue("evidence_location_mismatch", **kwargs)

    report["counts"] = dict(sorted(counts.items()))
    report["total_issues"] = sum(counts.values())
    report["returned_issues"] = len(report["issues"])
    report["truncated"] = report["total_issues"] > report["returned_issues"]
    return report
