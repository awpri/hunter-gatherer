"""Cooperative derived-index upkeep; no model, source extraction, or fact edits.

Diagnostics are aggregate and process-local. Exactly one coordinator belongs
 to each shared Harness, regardless of how many endpoints use it.
"""
from __future__ import annotations

import math
import threading
from datetime import UTC, datetime

from proharness.memory.catalog_index import refresh_catalog_batch
from proharness.memory.bounded_retrieval import index_readiness, prepare_index_batch

_FACTORY_LOCK = threading.Lock()


def _number(settings, name, default, minimum, maximum):
    try:
        value = float(getattr(settings, name, default))
        return max(minimum, min(maximum, value)) if math.isfinite(value) else default
    except (ValueError, TypeError):
        return default


class MaintenanceCoordinator:
    def __init__(self, harness):
        self._harness = harness
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._wake = threading.Event()
        self._thread = None
        self._state = {"scope": "process_local", "status": "not_started", "runs": 0,
                       "checked": 0, "processed": 0, "pending": None,
                       "last_run": None, "recent_failure": None}

    def _enabled(self):
        return bool(getattr(self._harness.settings, "library_maintenance_enabled", True))

    def start(self):
        """Start once; initial grace period lets startup and foreground reads finish."""
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            if not self._enabled():
                self._state["status"] = "disabled"
                return
            self._stop.clear()
            self._state["status"] = "waiting"
            self._thread = threading.Thread(target=self._work, name="proharness-maintenance", daemon=True)
            self._thread.start()

    def stop(self):
        """Cancel and join before the shared store is closed; safe to call repeatedly."""
        with self._lock:
            self._stop.set()
            self._wake.set()
            thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join()
        with self._lock:
            if self._thread is thread:
                self._state["status"] = "stopped" if self._enabled() else "disabled"

    def wake(self):
        """Request prompt bounded upkeep after an import; never blocks callers."""
        self._wake.set()

    def snapshot(self):
        with self._lock:
            return {**self._state, "search_index": index_readiness(self._harness.store), "enabled": self._enabled(),
                    "running": self._thread is not None and self._thread.is_alive(),
                    "catalog_index_enabled": bool(getattr(self._harness.settings, "archive_catalog_index", False))}

    def _work(self):
        settings = self._harness.settings
        interval = _number(settings, "library_maintenance_interval_seconds", 300, 1, 86400)
        if self._stop.wait(min(5, interval)):
            return
        while not self._stop.is_set():
            if not self._enabled():
                with self._lock:
                    self._state["status"] = "disabled"
                return
            batch_size = int(_number(settings, "library_maintenance_batch_size", 32, 1, 128))
            failure = None
            try:
                search_index = prepare_index_batch(self._harness.store, batch_size, self._stop.is_set)
            except Exception:
                search_index = {"status": "failed", "pending": None}
                failure = {"code": "search_index_prepare_failed", "at": datetime.now(UTC).isoformat()}
            try:
                result = refresh_catalog_batch(self._harness.store, batch_size, self._stop.is_set)
            except Exception:
                # Exception strings can contain source labels, SQL, or paths.
                result = {"status": "failed", "checked": 0, "processed": 0, "pending": None}
                failure = {"code": "catalog_refresh_failed", "at": datetime.now(UTC).isoformat()}
            with self._lock:
                self._state.update(result)
                self._state["last_search_preparation"] = search_index
                self._state["last_run"] = datetime.now(UTC).isoformat()
                self._state["runs"] += 1
                self._state["recent_failure"] = failure
            delay = min(1, interval) if search_index.get("pending") or search_index["status"] == "busy" else interval
            self._wake.wait(delay)
            self._wake.clear()
            if self._stop.is_set():
                return


def maintenance(harness):
    """Get the single lazily allocated coordinator for this shared Harness."""
    with _FACTORY_LOCK:
        coordinator = getattr(harness, "_maintenance_coordinator", None)
        if coordinator is None:
            coordinator = MaintenanceCoordinator(harness)
            harness._maintenance_coordinator = coordinator
        return coordinator
