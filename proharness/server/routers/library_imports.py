"""Durable uploads and a bounded single-lane import worker for the shared store."""
from __future__ import annotations

import contextlib
import os
import queue
import threading
import uuid
from collections import deque
from pathlib import Path
from typing import Annotated

from fastapi import APIRouter, File, Form, HTTPException, Query, Request, UploadFile
from pydantic import Field, StrictBool

from proharness.maintenance import maintenance
from proharness.memory.documents import DocumentArchive
from proharness.memory.import_jobs import ImportJobs
from proharness.server.routers.library_sources import PrivateInput, _harness

router = APIRouter()
_SETUP_LOCK = threading.Lock()
_UPLOAD_LIMIT = 25 * 1024 * 1024


class RunInput(PrivateInput):
    retry_errors: StrictBool = False
    resume: StrictBool = False
    batch_size: int = Field(default=10, ge=1, le=100)


class ImportWorker:
    """One daemon lane; durable manifests remain resumable after process exit."""

    def __init__(self, store, after_batch=None):
        self.store = store
        self.after_batch = after_batch
        self.jobs = ImportJobs(store)
        self.lock = threading.RLock()
        self.pending = queue.Queue(maxsize=20)
        self.active = set()
        self.running = set()
        self.errors = deque(maxlen=20)
        self.authorized = {}
        self.thread = None
        self.stopping = False

    def snapshot(self):
        """Process-local counts only; never initialize or read durable manifests."""
        with self.lock:
            return {"queued": self.pending.qsize(), "running": len(self.running),
                    "capacity": self.pending.maxsize, "running_capacity": 1,
                    "stopping": self.stopping, "recent_errors": list(self.errors)}

    def _schema(self):
        with self.store.transaction():
            self.store._sqlite.execute("CREATE TABLE IF NOT EXISTS archive_import_dispatch (job_id TEXT PRIMARY KEY,state TEXT NOT NULL,error_code TEXT)")

    def _write_dispatch(self, identity, state, error=None):
        with self.store.transaction():
            self.store._sqlite.execute("INSERT OR REPLACE INTO archive_import_dispatch VALUES (?,?,?)", (identity, state, error))

    def status(self, identity, *, include_sensitive=False, offset=0, limit=50):
        result = self.jobs.status(identity, include_sensitive=include_sensitive, offset=offset, limit=limit)
        with self.store._lock:
            exists = self.store._sqlite.execute("SELECT 1 FROM sqlite_master WHERE name='archive_import_dispatch'").fetchone()
            row = self.store._sqlite.execute("SELECT state,error_code FROM archive_import_dispatch WHERE job_id=?", (identity,)).fetchone() if exists else None
        with self.lock:
            state = row["state"] if row else "idle"
            if state in {"queued", "running"} and identity not in self.active:
                state = "needs_resume"
        result.update(worker_state=state, worker_error=row["error_code"] if row else None)
        return result

    def enqueue(self, identity, body):
        # Check access on each explicitly authorized run before queueing any work.
        self.jobs.status(identity, include_sensitive=body.include_sensitive)
        with self.lock:
            if self.stopping:
                raise HTTPException(503, "Import worker is shutting down; resume after restart.")
            if identity in self.active:
                raise HTTPException(409, "This import job is already queued or running.")
            if self.pending.full():
                raise HTTPException(429, "Import queue is full; retry after a job finishes.")
            self._schema()
            self._write_dispatch(identity, "queued")
            self.active.add(identity)
            self.authorized[identity] = body.include_sensitive
            self.pending.put_nowait((identity, body.model_dump()))
            if self.thread is None or not self.thread.is_alive():
                self.thread = threading.Thread(target=self._work, name="proharness-library-import", daemon=True)
                self.thread.start()
        return self.status(identity, include_sensitive=body.include_sensitive)

    def _work(self):
        while True:
            with self.lock:
                if self.stopping or self.pending.empty():
                    self.thread = None
                    return
                identity, options = self.pending.get_nowait()
                self.running.add(identity)
            try:
                self._write_dispatch(identity, "running")
                self.jobs.run(identity, **options)
                self._write_dispatch(identity, "idle")
                if self.after_batch is not None:
                    # Upkeep cannot turn an already committed import into a failure.
                    with contextlib.suppress(Exception):
                        self.after_batch()
            except Exception as exc:
                code = "batch_conflict" if isinstance(exc, ValueError) and "already running" in str(exc) else "batch_failed"
                with self.lock:
                    self.errors.append(code)
                # Process/store shutdown preserves durable state for resume.
                with contextlib.suppress(Exception):
                    self._write_dispatch(identity, "needs_attention", code)
            finally:
                with self.lock:
                    self.active.discard(identity)
                    self.running.discard(identity)
                    self.authorized.pop(identity, None)
                self.pending.task_done()

    def shutdown(self):
        with self.lock:
            self.stopping = True
            thread = self.thread
            authorized = dict(self.authorized)
        for identity, private in authorized.items():
            with contextlib.suppress(Exception):
                self.jobs.cancel(identity, include_sensitive=private)
        # Queued work will never be consumed once stopping is set. Release its
        # in-memory ownership and explicitly preserve durable resume state.
        with self.lock:
            while not self.pending.empty():
                identity, _ = self.pending.get_nowait()
                with contextlib.suppress(Exception):
                    self._write_dispatch(identity, "needs_resume")
                self.active.discard(identity)
                self.authorized.pop(identity, None)
                self.pending.task_done()
        if thread:
            thread.join(timeout=5)
            if thread.is_alive():
                with self.lock:
                    if "shutdown_timeout" not in self.errors:
                        self.errors.append("shutdown_timeout")
                # Parsers cannot be forcibly stopped. Record resumability before
                # the owning server closes its connection; cancellation checks
                # prevent a completed extraction from committing its document.
                for identity in authorized:
                    with contextlib.suppress(Exception):
                        self._write_dispatch(identity, "needs_resume")


def _worker(request):
    with _SETUP_LOCK:
        value = getattr(request.app.state, "library_import_worker", None)
        if value is None:
            harness = _harness(request)
            value = ImportWorker(harness.store, after_batch=maintenance(harness).wake)
            request.app.state.library_import_worker = value
            request.app.router.add_event_handler("shutdown", value.shutdown)
        return value


def _job_call(function, *args, **kwargs):
    try:
        return function(*args, **kwargs)
    except HTTPException:
        raise
    except ValueError as exc:
        message = str(exc)
        code = 404 if "unavailable" in message.lower() else 409 if "conflict" in message.lower() or "already running" in message.lower() else 422
        raise HTTPException(code, message) from exc
    except (OSError, RuntimeError) as exc:
        raise HTTPException(409, "Import operation unavailable; inspect the saved job and retry.") from exc


@router.get("/imports")
def list_jobs(request: Request, limit: int = Query(20, ge=1, le=100), include_sensitive: bool = False):
    worker = _worker(request)
    result = _job_call(worker.jobs.list, limit=limit, include_sensitive=include_sensitive)
    for job in result["jobs"]:
        status = _job_call(worker.status, job["job_id"], include_sensitive=include_sensitive, limit=1)
        job.update(worker_state=status["worker_state"], worker_error=status["worker_error"])
    return result


@router.get("/imports/{job_id}")
def get_job(request: Request, job_id: str, offset: int = Query(0, ge=0), limit: int = Query(50, ge=1, le=100), include_sensitive: bool = False):
    return _job_call(_worker(request).status, job_id, include_sensitive=include_sensitive, offset=offset, limit=limit)


@router.post("/imports/{job_id}/run", status_code=202)
def run_job(request: Request, job_id: str, body: RunInput):
    return _job_call(_worker(request).enqueue, job_id, body)


@router.post("/imports/{job_id}/cancel")
def cancel_job(request: Request, job_id: str, body: PrivateInput):
    worker = _worker(request)
    _job_call(worker.jobs.cancel, job_id, include_sensitive=body.include_sensitive)
    return _job_call(worker.status, job_id, include_sensitive=body.include_sensitive)


@router.post("/imports", status_code=202)
def upload_job(request: Request, file: Annotated[UploadFile, File()], title: str = Form("", max_length=512),
               author: str = Form("", max_length=512), document_date: str = Form("", max_length=100),
               category: str = Form("personal", min_length=1, max_length=128), sensitive: str = Form("false")):
    if sensitive not in {"true", "false"}:
        raise HTTPException(422, "sensitive must be true or false.")
    filename = Path((file.filename or "").replace("\\", "/")).name
    extension = Path(filename).suffix.lower()
    if extension not in {".txt", ".md", ".pdf", ".docx", ".pptx"}:
        raise HTTPException(415, "Supported uploads are .txt, .md, .pdf, .docx, and .pptx.")
    worker = _worker(request)
    root = DocumentArchive(worker.store, initialize_search=False).root
    directory = root / "staging" / uuid.uuid4().hex
    path = directory / ("upload" + extension)
    planned = False
    try:
        directory.mkdir(parents=True, mode=0o700)
        with path.open("xb") as target:
            os.chmod(path, 0o600)
            size = 0
            while block := file.file.read(1024 * 1024):
                size += len(block)
                if size > _UPLOAD_LIMIT:
                    raise HTTPException(413, "Upload exceeds 25 MiB.")
                target.write(block)
        if not size:
            raise HTTPException(422, "Upload is empty.")
        job = _job_call(worker.jobs.create, [str(path)], sensitive=sensitive == "true",
                        include_sensitive=sensitive == "true", category=category,
                        metadata={"title": title or Path(filename).stem[:512], "author": author, "document_date": document_date})
        planned = True
        identity = job["job_id"]
        # Dedup against private source history may tighten a nominally public job.
        # Do not implicitly authorize private execution or return a hidden manifest.
        if job["sensitive"] and sensitive != "true":
            return {"job_id": identity, "sensitive": True, "counts": job["counts"],
                    "worker_state": "needs_attention", "worker_error": "sensitive_opt_in_required"}
        return _job_call(worker.enqueue, identity, RunInput(include_sensitive=sensitive == "true"))
    except OSError as exc:
        raise HTTPException(409, "Uploaded source could not be staged; retry after checking available storage.") from exc
    finally:
        file.file.close()
        if not planned:
            path.unlink(missing_ok=True)
            if directory.exists():
                directory.rmdir()
