"""Durable bounded import jobs; processing is explicit and cancellation is cooperative."""

from __future__ import annotations

import contextlib
import hashlib
import json
import os
import stat
import time
import uuid
from pathlib import Path

from proharness.memory.secure_files import lock_exclusive, open_regular

SUPPORTED = frozenset({".pdf", ".docx", ".pptx", ".md", ".markdown", ".txt"})


def _read_source(path, limit):
    """Read a regular file without following any path-component symlink."""
    file_fd = None
    try:
        file_fd = open_regular(path)
        info = os.fstat(file_fd)
        if not stat.S_ISREG(info.st_mode):
            raise ValueError("Source changed during planning.")
        if info.st_size > limit:
            raise ValueError("Import exceeds byte bound.")
        with os.fdopen(file_fd, "rb") as stream:
            file_fd = None
            data = stream.read(limit + 1)
        if len(data) != info.st_size:
            raise ValueError("Source changed during planning.")
        return data
    except OSError:
        raise ValueError("Source changed during planning.") from None
    finally:
        if file_fd is not None:
            os.close(file_fd)


class ImportJobs:
    def __init__(self, store):
        self.archive = store if hasattr(store, "ingest") else None
        self.store = store.store if self.archive else store

    @staticmethod
    def preview(paths, recursive=False, max_files=1000, max_bytes=1024**3):
        if not isinstance(recursive, bool):
            raise ValueError("recursive must be a boolean.")
        if isinstance(paths, (str, Path)):
            paths = [paths]
        if not isinstance(paths, (list, tuple)) or any(
            not isinstance(p, (str, Path)) for p in paths
        ):
            raise ValueError("paths must contain explicit file or folder paths.")
        if type(max_files) is not int or type(max_bytes) is not int:
            raise ValueError("Import bounds must be integers.")
        if not paths or not 1 <= int(max_files) <= 10000 or not 1 <= int(max_bytes) <= 10 * 1024**3:
            raise ValueError("Invalid import bounds or empty paths.")
        if len(paths) > int(max_files):
            raise ValueError("Import exceeds input-path bound.")
        max_files, max_bytes = int(max_files), int(max_bytes)
        found = set()
        visited = 0
        skipped = 0

        def safe(path):
            return not any(
                p.is_symlink() or p.name.startswith(".") for p in (path, *path.parents) if p.name
            )

        for value in paths:
            path = Path(os.path.abspath(Path(value).expanduser()))
            if not safe(path):
                skipped += 1
                continue
            if not path.exists():
                raise ValueError("An import source does not exist.")
            if path.is_dir():
                if not recursive:
                    raise ValueError("Folder imports require recursive=True.")
                for base, dirs, names in os.walk(path, followlinks=False):
                    visited += len(dirs) + len(names) + 1
                    if visited > max_files * 20:
                        raise ValueError("Import exceeds discovery bound.")
                    dirs[:] = sorted(
                        d
                        for d in dirs
                        if not d.startswith(".") and not (Path(base) / d).is_symlink()
                    )
                    for name in sorted(names):
                        candidate = Path(base) / name
                        if (
                            safe(candidate)
                            and candidate.is_file()
                            and candidate.suffix.lower() in SUPPORTED
                        ):
                            found.add(str(candidate))
                            if len(found) > max_files:
                                raise ValueError("Import exceeds file-count bound.")
                        else:
                            skipped += 1
            elif path.is_file() and path.suffix.lower() in SUPPORTED:
                found.add(str(path))
            else:
                skipped += 1
            if len(found) > max_files:
                raise ValueError("Import exceeds file-count bound.")
        items, total = [], 0
        for name in sorted(found):
            path = Path(name)
            data = _read_source(path, min(100 * 1024**2, max_bytes - total))
            size = len(data)
            total += size
            items.append(
                {"path": name, "size_bytes": size, "content_hash": hashlib.sha256(data).hexdigest()}
            )
        digest = hashlib.sha256(
            json.dumps(items, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        return {
            "items": items,
            "file_count": len(items),
            "total_bytes": total,
            "manifest_hash": digest,
            "skipped": skipped,
        }

    def _schema(self):
        with self.store.transaction():
            self.store._sqlite.execute(
                "CREATE TABLE IF NOT EXISTS archive_import_jobs (id TEXT PRIMARY KEY, created REAL NOT NULL, sensitive INTEGER NOT NULL, category TEXT NOT NULL, manifest_hash TEXT NOT NULL, idempotency_key TEXT UNIQUE)"
            )
            self.store._sqlite.execute(
                'CREATE TABLE IF NOT EXISTS archive_import_items (job_id TEXT NOT NULL, ordinal INTEGER NOT NULL, path TEXT NOT NULL, size_bytes INTEGER NOT NULL, content_hash TEXT NOT NULL, state TEXT NOT NULL DEFAULT "pending", document_id TEXT, error_code TEXT, attempts INTEGER NOT NULL DEFAULT 0, PRIMARY KEY(job_id, ordinal))'
            )

            self.store._sqlite.execute("CREATE TABLE IF NOT EXISTS archive_import_progress (job_id TEXT NOT NULL, ordinal INTEGER NOT NULL, stage TEXT NOT NULL, updated_at REAL NOT NULL, PRIMARY KEY(job_id,ordinal))")
            self.store._sqlite.execute("CREATE TABLE IF NOT EXISTS archive_import_metadata (job_id TEXT PRIMARY KEY, metadata TEXT NOT NULL)")
            self.store._sqlite.execute("CREATE TABLE IF NOT EXISTS archive_import_index_progress (job_id TEXT PRIMARY KEY, result TEXT NOT NULL)")
            self.store._sqlite.execute("CREATE TABLE IF NOT EXISTS archive_import_control (job_id TEXT PRIMARY KEY, cancel_requested INTEGER NOT NULL DEFAULT 0)")

    def cancel(self, job_id, include_sensitive=False):
        self._schema()
        with self.store.transaction():
            self._job(job_id, include_sensitive)
            self.store._sqlite.execute("INSERT OR REPLACE INTO archive_import_control VALUES (?,1)", (job_id,))
            self.store._sqlite.execute("UPDATE archive_import_items SET state='cancelled' WHERE job_id=? AND state='pending'", (job_id,))
        return self.status(job_id, include_sensitive)

    def _cancelled(self, job_id):
        with self.store._lock:
            row = self.store._sqlite.execute("SELECT cancel_requested FROM archive_import_control WHERE job_id=?", (job_id,)).fetchone()
            return bool(row and row[0])

    def _progress(self, job_id, ordinal, stage):
        # Final progress runs inside the archive's writer transaction. Never
        # commit or roll back that owner's work from this nested callback.
        with self.store._lock:
            owns_transaction = not self.store._sqlite.in_transaction
            try:
                if self._cancelled(job_id):
                    raise InterruptedError("Import cancelled before committing the document.")
                self.store._sqlite.execute("INSERT OR REPLACE INTO archive_import_progress VALUES (?,?,?,?)", (job_id, ordinal, stage, time.time()))
                if owns_transaction:
                    self.store._sqlite.commit()
            except BaseException:
                if owns_transaction:
                    self.store._sqlite.rollback()
                raise

    def create(
        self,
        paths,
        recursive=False,
        max_files=1000,
        max_bytes=1024**3,
        sensitive=False,
        category="personal",
        idempotency_key=None,
        include_sensitive=False,
        metadata=None,
    ):
        if (
            not isinstance(sensitive, bool)
            or not isinstance(include_sensitive, bool)
            or not isinstance(category, str)
            or not 1 <= len(category) <= 128
        ):
            raise ValueError("Invalid sensitivity or category.")
        metadata = metadata or {}
        if not isinstance(metadata, dict) or set(metadata) - {"title", "author", "document_date"} or any(not isinstance(value, str) or len(value) > 512 for value in metadata.values()):
            raise ValueError("Import metadata supports title, author, and document_date strings up to 512 characters.")
        explicit_visibility = sensitive or include_sensitive
        manifest = self.preview(paths, recursive, max_files, max_bytes)
        if not manifest["items"]:
            raise ValueError("Import manifest contains no supported files.")
        self._schema()
        job_id = uuid.uuid4().hex
        with self.store.transaction():
            self.store._sqlite.execute("BEGIN IMMEDIATE")
            if self.store._sqlite.execute(
                "SELECT 1 FROM sqlite_master WHERE name='archive_documents'"
            ).fetchone():
                for item in manifest["items"]:
                    identity = hashlib.sha256(item["path"].encode()).hexdigest()
                    if self.store._sqlite.execute(
                        "SELECT 1 FROM archive_documents WHERE sensitive=1 AND (source_identity=? OR content_hash=?) LIMIT 1",
                        (identity, item["content_hash"]),
                    ).fetchone():
                        sensitive = True
            if idempotency_key is not None:
                if not isinstance(idempotency_key, str) or not 1 <= len(idempotency_key) <= 128:
                    raise ValueError("Invalid idempotency key.")
                prior = self.store._sqlite.execute(
                    "SELECT * FROM archive_import_jobs WHERE idempotency_key=?", (idempotency_key,)
                ).fetchone()
                if prior:
                    prior_metadata = self.store._sqlite.execute("SELECT metadata FROM archive_import_metadata WHERE job_id=?", (prior["id"],)).fetchone()
                    if (json.loads(prior_metadata[0]) if prior_metadata else {}) != metadata:
                        raise ValueError("Idempotency key conflicts with import metadata.")
                    if (
                        prior["manifest_hash"] != manifest["manifest_hash"]
                        or prior["category"] != category
                        or bool(prior["sensitive"]) != bool(sensitive)
                    ):
                        raise ValueError("Idempotency key conflicts with an existing plan.")
                    return self._creation_receipt(prior["id"], sensitive, explicit_visibility)
            self.store._sqlite.execute(
                "INSERT INTO archive_import_jobs VALUES (?,?,?,?,?,?)",
                (
                    job_id,
                    time.time(),
                    int(sensitive),
                    category,
                    manifest["manifest_hash"],
                    idempotency_key,
                ),
            )
            self.store._sqlite.execute("INSERT INTO archive_import_metadata VALUES (?,?)", (job_id, json.dumps(metadata)))
            self.store._sqlite.executemany(
                "INSERT INTO archive_import_items (job_id,ordinal,path,size_bytes,content_hash) VALUES (?,?,?,?,?)",
                [
                    (job_id, i, item["path"], item["size_bytes"], item["content_hash"])
                    for i, item in enumerate(manifest["items"])
                ],
            )
        return self._creation_receipt(job_id, sensitive, explicit_visibility)

    def _creation_receipt(self, job_id, sensitive, explicit_visibility):
        result = self.status(job_id, include_sensitive=bool(sensitive))
        if sensitive and not explicit_visibility:
            return {key: result[key] for key in ("job_id", "sensitive", "counts")}
        return result

    def _has_schema(self):
        with self.store._lock:
            return bool(
                self.store._sqlite.execute(
                    "SELECT 1 FROM sqlite_master WHERE name='archive_import_jobs'"
                ).fetchone()
            )

    def _job(self, job_id, include_sensitive):
        if not isinstance(include_sensitive, bool):
            raise ValueError("include_sensitive must be a boolean.")
        row = self.store._sqlite.execute(
            "SELECT * FROM archive_import_jobs WHERE id=? AND (? OR sensitive=0)",
            (job_id, int(include_sensitive)),
        ).fetchone()
        if (
            row is not None
            and not include_sensitive
            and self.store._sqlite.execute(
                "SELECT 1 FROM sqlite_master WHERE name='archive_documents'"
            ).fetchone()
            and self.store._sqlite.execute(
                "SELECT 1 FROM archive_import_items i JOIN archive_documents d ON (d.content_hash=i.content_hash OR d.source_path=i.path) WHERE i.job_id=? AND d.sensitive=1 LIMIT 1",
                (job_id,),
            ).fetchone()
        ):
            row = None
        if row is None:
            raise ValueError("Import job unavailable.")
        return row

    def status(self, job_id, include_sensitive=False, offset=0, limit=50):
        if type(offset) is not int or type(limit) is not int:
            raise ValueError("Pagination must use integers.")
        if not self._has_schema():
            raise ValueError("Import job unavailable.")
        with self.store._lock:
            job = self._job(job_id, include_sensitive)
            counts = {state: 0 for state in ("pending", "running", "succeeded", "failed")}
            counts.update(
                dict(
                    self.store._sqlite.execute(
                        "SELECT state,COUNT(*) FROM archive_import_items WHERE job_id=? GROUP BY state",
                        (job_id,),
                    ).fetchall()
                )
            )
            total_bytes = self.store._sqlite.execute(
                "SELECT COALESCE(SUM(size_bytes),0) FROM archive_import_items WHERE job_id=?",
                (job_id,),
            ).fetchone()[0]
            errors = dict(
                self.store._sqlite.execute(
                    "SELECT error_code,COUNT(*) FROM archive_import_items WHERE job_id=? AND error_code IS NOT NULL GROUP BY error_code",
                    (job_id,),
                ).fetchall()
            )
            offset, limit = max(0, int(offset)), max(1, min(int(limit), 100))
            items = [
                dict(r)
                for r in self.store._sqlite.execute(
                    "SELECT ordinal,path,state,error_code,document_id FROM archive_import_items WHERE job_id=? ORDER BY ordinal LIMIT ? OFFSET ?",
                    (job_id, limit, offset),
                )
            ]
            has_progress = self.store._sqlite.execute("SELECT 1 FROM sqlite_master WHERE name='archive_import_progress'").fetchone()
            for item in items:
                progress = self.store._sqlite.execute("SELECT stage FROM archive_import_progress WHERE job_id=? AND ordinal=?", (job_id, item["ordinal"])).fetchone() if has_progress else None
                item["stage"] = (progress[0] if progress else "extracting") if item["state"] == "running" else {"pending": "queued", "succeeded": "ready", "failed": "needs_attention", "cancelled": "cancelled"}.get(item["state"], item["state"])
                if item["document_id"]:
                    doc = self.store._sqlite.execute(
                        "SELECT sensitive,deleted_at FROM archive_documents WHERE id=?",
                        (item["document_id"],),
                    ).fetchone()
                    if (
                        doc is None
                        or doc["deleted_at"] is not None
                        or (doc["sensitive"] and not include_sensitive)
                    ):
                        item.clear()
                        item["state"] = "unavailable"
            total_items = sum(counts.values())
            from proharness.memory.bounded_retrieval import index_readiness
            has_index_progress = self.store._sqlite.execute("SELECT 1 FROM sqlite_master WHERE name='archive_import_index_progress'").fetchone()
            last_preparation = self.store._sqlite.execute("SELECT result FROM archive_import_index_progress WHERE job_id=?", (job_id,)).fetchone() if has_index_progress else None
            return {
                "search_index": dict(index_readiness(self.store), scope="archive"),
                "last_search_preparation": json.loads(last_preparation[0]) if last_preparation else None,
                "ingestion_complete": counts["pending"] + counts["running"] == 0,
                "items": items,
                "total_items": total_items,
                "next_offset": offset + limit if offset + limit < total_items else None,
                "job_id": job_id,
                "created_at": job["created"],
                "sensitive": bool(job["sensitive"]),
                "manifest_hash": job["manifest_hash"],
                "counts": counts,
                "total_bytes": total_bytes,
                "errors": errors,
                "complete": counts["pending"] + counts["running"] == 0,
            }

    def list(self, limit=20, include_sensitive=False):
        if type(limit) is not int or not isinstance(include_sensitive, bool):
            raise ValueError("Invalid list options.")
        if not self._has_schema():
            return {"jobs": []}
        with self.store._lock:
            ids = [
                r[0]
                for r in self.store._sqlite.execute(
                    "SELECT id FROM archive_import_jobs WHERE (? OR sensitive=0) ORDER BY created DESC,id LIMIT ?",
                    (int(include_sensitive), max(1, min(int(limit), 100))),
                )
            ]
        jobs = []
        for identity in ids:
            with contextlib.suppress(ValueError):
                result = self.status(identity, include_sensitive)
                jobs.append(
                    {
                        key: value
                        for key, value in result.items()
                        if key not in {"items", "next_offset"}
                    }
                )
        return {"jobs": jobs}

    def run(self, job_id, batch_size=10, include_sensitive=False, retry_errors=False, resume=False):
        if (
            type(batch_size) is not int
            or not isinstance(include_sensitive, bool)
            or not isinstance(retry_errors, bool)
            or not isinstance(resume, bool)
        ):
            raise ValueError("Invalid batch options.")
        if not 1 <= batch_size <= 100:
            raise ValueError("Batch size must be between 1 and 100.")
        if not self._has_schema():
            raise ValueError("Import job unavailable.")
        self._schema()
        with self.store._lock:
            job = dict(self._job(job_id, include_sensitive))
            metadata_row = self.store._sqlite.execute("SELECT metadata FROM archive_import_metadata WHERE job_id=?", (job_id,)).fetchone()
            metadata = json.loads(metadata_row[0]) if metadata_row else {}
            db_file = self.store._sqlite.execute("PRAGMA database_list").fetchone()[2]
        if self.archive is None:
            from proharness.memory.documents import DocumentArchive

            self.archive = DocumentArchive(self.store)
        lock_path = (
            Path(db_file + ".archive-import.lock")
            if db_file
            else self.archive.root / ".archive-import.lock"
        )
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        processed = []
        descriptor = open_regular(lock_path.absolute(), writable=True, create=True)
        with os.fdopen(descriptor, "a+b") as lock:
            try:
                lock_exclusive(lock)
            except BlockingIOError:
                raise ValueError("An import batch is already running.") from None
            # The OS releases this claim on process death. All interrupted items
            # are replayable because archive.ingest deduplicates stable sources.
            with self.store.transaction():
                if resume:
                    self.store._sqlite.execute("DELETE FROM archive_import_control WHERE job_id=?", (job_id,))
                    self.store._sqlite.execute("UPDATE archive_import_items SET state='pending' WHERE job_id=? AND state='cancelled'", (job_id,))
                if self._cancelled(job_id):
                    return self.status(job_id, include_sensitive)
                self.store._sqlite.execute(
                    'UPDATE archive_import_items SET state="pending" WHERE job_id=? AND state="running"',
                    (job_id,),
                )
                if retry_errors:
                    self.store._sqlite.execute(
                        'UPDATE archive_import_items SET state="pending" WHERE job_id=? AND state="failed" AND error_code!="source_drift"',
                        (job_id,),
                    )
                rows = self.store._sqlite.execute(
                    'SELECT * FROM archive_import_items WHERE job_id=? AND state="pending" ORDER BY ordinal LIMIT ?',
                    (job_id, int(batch_size)),
                ).fetchall()
            for item in rows:
                if self._cancelled(job_id):
                    break
                with self.store.transaction():
                    self._job(job_id, include_sensitive)
                    self.store._sqlite.execute(
                        'UPDATE archive_import_items SET state="running",attempts=attempts+1,error_code=NULL WHERE job_id=? AND ordinal=?',
                        (job_id, item["ordinal"]),
                    )
                error, document_id = None, None
                try:
                    check = self.preview(
                        [item["path"]], max_files=1, max_bytes=max(1, item["size_bytes"])
                    )
                    if (
                        not check["items"]
                        or check["items"][0]["content_hash"] != item["content_hash"]
                    ):
                        error = "source_drift"
                    else:
                        self._progress(job_id, item["ordinal"], "extracting")
                        options = {"progress": lambda stage, ordinal=item["ordinal"]: self._progress(job_id, ordinal, stage)} if hasattr(self.archive, "_ingest_bytes") else {}
                        result = self.archive.ingest(
                            item["path"],
                            category=job["category"],
                            sensitive=bool(job["sensitive"]),
                            expected_content_hash=item["content_hash"],
                            **metadata,
                            **options,
                        )
                        document_id = result["document_id"]
                        processed.append(
                            {"document_id": document_id, "created": not result["deduplicated"]}
                        )
                except InterruptedError:
                    error = "cancelled"
                except (FileNotFoundError, OSError):
                    error = "source_unavailable"
                except ValueError as exc:
                    error = (
                        "source_drift"
                        if "source changed" in str(exc).lower() or "bound" in str(exc)
                        else "invalid_document"
                    )
                except Exception:
                    error = "import_failed"
                with self.store.transaction():
                    self.store._sqlite.execute(
                        "UPDATE archive_import_items SET state=?,document_id=?,error_code=? WHERE job_id=? AND ordinal=?",
                        (
                            "cancelled" if error == "cancelled" else "failed" if error else "succeeded",
                            document_id,
                            error,
                            job_id,
                            item["ordinal"],
                        ),
                    )
        output = self.status(job_id, include_sensitive)
        output["processed_items"] = processed
        # Source success is durable even when optional derived preparation fails.
        from proharness.memory.bounded_retrieval import prepare_index_batch
        try:
            output["search_index"] = prepare_index_batch(
                self.store, cancelled=lambda: self._cancelled(job_id))
        except Exception:
            output["search_index"] = {"status": "failed", "processed": 0,
                                      "vector_processed": 0, "pending": None}
        # Persist the bounded attempt separately from live archive readiness.
        # Polling after restart must retain failures without treating stale results as ready.
        output["search_index"].update(scope="archive", ready=output["search_index"]["status"] in {"ready", "not_required"})
        attempt = dict(output["search_index"], updated_at=time.time())
        with self.store.transaction():
            self.store._sqlite.execute("INSERT OR REPLACE INTO archive_import_index_progress VALUES (?,?)",
                                       (job_id, json.dumps(attempt)))
        output["last_search_preparation"] = attempt
        return output


preview_import = ImportJobs.preview
