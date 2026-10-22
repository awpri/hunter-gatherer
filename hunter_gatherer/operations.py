"""Bounded process-local admission and truthful readiness diagnostics."""

import os
import time
from collections import deque
from contextlib import contextmanager
from threading import BoundedSemaphore, Lock

_REGISTRY = Lock()


class Operations:
    def __init__(self, limit=2):
        self.limit = limit
        self._slots = BoundedSemaphore(limit)
        self._lock = Lock()
        self.active = 0
        self.rejected = 0
        self.errors = deque(maxlen=20)

    @contextmanager
    def admit(self, operation):
        if not self._slots.acquire(blocking=False):
            with self._lock:
                self.rejected += 1
            raise ValueError(f"Service busy: {operation} capacity reached; retry later.")
        with self._lock:
            self.active += 1
        try:
            yield
        except Exception as exc:
            with self._lock:
                # Never retain arguments, model output, or exception prose.
                self.errors.append(
                    {"at": time.time(), "operation": operation, "type": type(exc).__name__}
                )
            raise
        finally:
            with self._lock:
                self.active -= 1
            self._slots.release()

    def snapshot(self):
        with self._lock:
            return {
                "limit": self.limit,
                "active": self.active,
                "available": self.limit - self.active,
                "rejected": self.rejected,
                "recent_errors": list(self.errors),
            }


def operations(harness):
    with _REGISTRY:
        if not hasattr(harness, "_operations"):
            limit = int(os.environ.get("HG_DELEGATE_CAPACITY", "2"))
            if not 1 <= limit <= 32:
                raise ValueError("HG_DELEGATE_CAPACITY must be 1–32")
            harness._operations = Operations(limit)
        return harness._operations


def readiness(harness, *, import_worker=None):
    from hunter_gatherer.maintenance import maintenance

    store = harness.store
    database = "unavailable"
    if store._lock.acquire(blocking=False):
        try:
            store._sqlite.execute("SELECT 1").fetchone()
            store.assert_embedder_current()
            database = "ready"
        except Exception:
            database = "error"
        finally:
            store._lock.release()
    else:
        database = "busy"
    embedder = getattr(harness.loop, "embedder", None)
    model = (
        "unavailable"
        if embedder is None
        else ("not_loaded" if hasattr(embedder, "_model") and embedder._model is None else "ready")
    )
    # Readiness never triggers a heavy model load or verifies backup bytes on a probe.
    backup = {"status": "unknown", "reason": "No backup verified by this running service."}
    verified = getattr(harness, "_last_verified_backup", None)
    if verified:
        backup = {"status": "verified", **verified}
    queue = operations(harness).snapshot()
    imports = import_worker.snapshot() if import_worker is not None else {
        "queued": 0, "running": 0, "capacity": 20, "running_capacity": 1,
        "stopping": False, "recent_errors": []}
    ready = database == "ready" and model == "ready" and not imports["stopping"]
    return {
        "status": "ready" if ready else "not_ready",
        "liveness": "alive",
        "database": database,
        "model": model,
        "delegate_capacity": queue,
        "import_queue": imports,
        "backup": backup,
        "maintenance": maintenance(harness).snapshot(),
        "ready": ready,
    }
