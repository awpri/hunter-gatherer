"""One bounded, lazy local reranker child per service process.

No model libraries are imported by the service. Requests never queue: a busy
worker, missing model, or failed child is a safe retrieval-policy fallback.
"""
from __future__ import annotations

import atexit
import json
import math
import os
import re
import selectors
import signal
import subprocess
import sys
import threading
import time
from contextlib import suppress
from pathlib import Path

MAX_PAIRS = 20
MAX_WINDOWS = 40
WINDOW_MODE = "document_windows_v1"
_WINDOW_SKIPS = frozenset({"query_too_long", "window_budget_exceeded", "tokenization_unavailable"})
MAX_REQUEST_BYTES = 1024 * 1024
MAX_RESPONSE_BYTES = 16 * 1024
MAX_TEXT_BYTES = 64 * 1024
MAX_MANIFEST_FILES = 64
WORKER = Path(__file__).with_name("recall_reranker_worker.py")
_CODES = frozenset({"busy", "unavailable", "timeout", "invalid_output", "model_mismatch"})


class RerankerUnavailable(Exception):
    """Public errors carry a fixed code, never child exception text or paths."""

    def __init__(self, code: str):
        self.code = code if code in _CODES else "unavailable"
        super().__init__(self.code)


def _manifest(files):
    if not isinstance(files, list) or not 1 <= len(files) <= MAX_MANIFEST_FILES:
        raise RerankerUnavailable("model_mismatch")
    normalized = []
    seen = set()
    for item in files:
        if not isinstance(item, dict):
            raise RerankerUnavailable("model_mismatch")
        name, digest, size = (item.get(k) for k in ("file", "sha256", "bytes"))
        if (not isinstance(name, str) or not re.fullmatch(r"[A-Za-z0-9_.-]{1,128}", name)
                or name in {".", ".."} or name in seen or name.endswith(".py")
                or not isinstance(digest, str) or not re.fullmatch(r"[0-9a-f]{64}", digest)
                or type(size) is not int or not 0 <= size <= 2 * 1024**3):
            raise RerankerUnavailable("model_mismatch")
        seen.add(name)
        normalized.append({"file": name, "sha256": digest, "bytes": size})
    return sorted(normalized, key=lambda item: item["file"])


def _bounded_seconds(value, maximum):
    if not _finite_number(value) or value <= 0:
        raise RerankerUnavailable("unavailable")
    return min(float(value), maximum)


def _finite_number(value):
    return (type(value) in (float, int) and -sys.float_info.max <= value <= sys.float_info.max
            and math.isfinite(value))


def _window_result(result, texts):
    """Validate every document-window field without trusting child annotations."""
    required = {"scores", "truncated_count", "mode", "windows", "window_count", "cpu_ms"}
    skipped = "skipped_reason" in result
    if (set(result) != required | ({"skipped_reason"} if skipped else set())
            or result.get("mode") != WINDOW_MODE
            or type(result.get("truncated_count")) is not int or result["truncated_count"] != 0
            or type(result.get("window_count")) is not int or not 0 <= result["window_count"] <= MAX_WINDOWS
            or not _finite_number(result.get("cpu_ms")) or result["cpu_ms"] < 0):
        raise RerankerUnavailable("invalid_output")
    scores, windows = result["scores"], result["windows"]
    if not isinstance(scores, list) or not isinstance(windows, list):
        raise RerankerUnavailable("invalid_output")
    if skipped:
        reason = result["skipped_reason"]
        if (not isinstance(reason, str) or reason not in _WINDOW_SKIPS
                or scores or windows or result["window_count"]):
            raise RerankerUnavailable("invalid_output")
        return {"scores": [], "truncated_count": 0, "mode": WINDOW_MODE,
                "windows": [], "window_count": 0, "skipped_reason": reason}
    if len(scores) != len(texts) or len(windows) != len(texts) or any(not _finite_number(score) for score in scores):
        raise RerankerUnavailable("invalid_output")
    count, normalized = 0, []
    for text, score, spans in zip(texts, scores, windows, strict=True):
        if not isinstance(spans, list) or not spans or len(spans) > MAX_WINDOWS:
            raise RerankerUnavailable("invalid_output")
        count += len(spans)
        if count > MAX_WINDOWS:
            raise RerankerUnavailable("invalid_output")
        covered, previous_start, converted = 0, -1, []
        for index, span in enumerate(spans):
            if not isinstance(span, dict) or set(span) != {"start", "end", "pair_tokens", "score"}:
                raise RerankerUnavailable("invalid_output")
            start, end, tokens, window_score = (span[key] for key in ("start", "end", "pair_tokens", "score"))
            if (any(type(value) is not int for value in (start, end, tokens))
                    or not 1 <= tokens <= 512 or not _finite_number(window_score)):
                raise RerankerUnavailable("invalid_output")
            if not text:
                if len(spans) != 1 or start != 0 or end != 0:
                    raise RerankerUnavailable("invalid_output")
            elif (not 0 <= start < end <= len(text) or (index == 0 and start != 0)
                  or (index > 0 and start <= previous_start) or start > covered or end <= covered):
                raise RerankerUnavailable("invalid_output")
            covered, previous_start = max(covered, end), start
            converted.append({"start": start, "end": end, "pair_tokens": tokens, "score": float(window_score)})
        if covered != len(text) or max(span["score"] for span in spans) != score:
            raise RerankerUnavailable("invalid_output")
        normalized.append(converted)
    if count != result["window_count"]:
        raise RerankerUnavailable("invalid_output")
    return {"scores": [float(score) for score in scores], "truncated_count": 0,
            "mode": WINDOW_MODE, "windows": normalized, "window_count": count}


class RecallRerankerRuntime:
    def __init__(self):
        self._admission = threading.Lock()
        self._state = threading.Lock()
        self._process = None
        self._identity = None
        self._timer = None
        self._generation = 0
        self._disabled = False
        self._request_id = 0
        self._cancelled = False

    def _stop_locked(self):
        process = self._process
        self._generation += 1
        if self._timer is not None:
            self._timer.cancel()
            self._timer = None
        if process is None:
            return
        # Own the entire new session, including any child descendants.
        failed = False
        process.poll()  # Reap an already-exited child before group cleanup.
        try:
            if os.name == "posix":
                with suppress(ProcessLookupError):
                    os.killpg(process.pid, signal.SIGKILL)
            elif process.poll() is None:
                process.kill()
        except OSError:
            failed = True
        try:
            process.wait(timeout=2)
        except (OSError, subprocess.SubprocessError):
            failed = True
        else:
            if failed and os.name == "posix":
                # macOS sandbox may deny signaling an unreaped zombie group.
                # Retry after reaping; ESRCH establishes the group is gone.
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                    failed = False
                except ProcessLookupError:
                    failed = False
                except OSError:
                    pass
        if failed:
            # Retain ownership and prohibit replacement after uncertain cleanup.
            self._disabled = True
            return
        self._process = self._identity = None
        for stream in (process.stdin, process.stdout):
            if stream is not None:
                with suppress(OSError):
                    stream.close()

    def close(self):
        """Unload immediately; safe during scoring and reusable afterwards."""
        with self._state:
            if self._admission.locked():
                self._cancelled = True
            self._stop_locked()

    def _discard(self, process):
        with self._state:
            if self._process is process:
                self._stop_locked()

    def _idle(self, process, generation):
        # Admission prevents an expired timer unloading an admitted request.
        if not self._admission.acquire(blocking=False):
            with self._state:
                if self._process is process and self._generation == generation:
                    timer = threading.Timer(0.05, self._idle, args=(process, generation))
                    timer.daemon = True
                    self._timer = timer
                    timer.start()
            return
        try:
            with self._state:
                if self._process is process and self._generation == generation:
                    self._stop_locked()
        finally:
            self._admission.release()

    def _spawn_locked(self, identity):
        if self._disabled:
            raise RerankerUnavailable("unavailable")
        env = os.environ.copy()
        env.update({"HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1",
                    "HF_DATASETS_OFFLINE": "1", "HF_HUB_DISABLE_TELEMETRY": "1",
                    "TOKENIZERS_PARALLELISM": "false", "OMP_NUM_THREADS": "2",
                    "MKL_NUM_THREADS": "2", "OPENBLAS_NUM_THREADS": "2"})
        process = subprocess.Popen(
            [sys.executable, "-I", str(WORKER)], stdin=subprocess.PIPE,
            stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, bufsize=0,
            env=env, close_fds=True, start_new_session=(os.name == "posix"),
        )
        self._process, self._identity = process, identity
        try:
            os.set_blocking(process.stdin.fileno(), False)
            os.set_blocking(process.stdout.fileno(), False)
        except BaseException:
            self._stop_locked()
            raise
        return process

    def _exchange(self, process, payload, deadline):
        self._request_id += 1
        request_id = self._request_id
        payload = {**payload, "request_id": request_id}
        data = json.dumps(payload, ensure_ascii=True, allow_nan=False,
                          separators=(",", ":")).encode("utf-8") + b"\n"
        if len(data) > MAX_REQUEST_BYTES:
            raise RerankerUnavailable("unavailable")
        offset = 0
        output = bytearray()
        with selectors.DefaultSelector() as selector:
            selector.register(process.stdin, selectors.EVENT_WRITE)
            while offset < len(data):
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RerankerUnavailable("timeout")
                if process.stdin.closed or process.stdout.closed:
                    raise RerankerUnavailable("unavailable")
                if not selector.select(min(remaining, 0.05)):
                    if process.poll() is not None:
                        raise RerankerUnavailable("unavailable")
                    continue
                try:
                    offset += os.write(process.stdin.fileno(), data[offset:])
                except BlockingIOError:
                    continue
            selector.unregister(process.stdin)
            selector.register(process.stdout, selectors.EVENT_READ)
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise RerankerUnavailable("timeout")
                if process.stdin.closed or process.stdout.closed:
                    raise RerankerUnavailable("unavailable")
                if not selector.select(min(remaining, 0.05)):
                    if process.poll() is not None:
                        raise RerankerUnavailable("unavailable")
                    continue
                try:
                    chunk = os.read(process.stdout.fileno(), MAX_RESPONSE_BYTES + 1 - len(output))
                except BlockingIOError:
                    continue
                if not chunk:
                    raise RerankerUnavailable("unavailable")
                output.extend(chunk)
                if len(output) > MAX_RESPONSE_BYTES:
                    raise RerankerUnavailable("invalid_output")
                if b"\n" in output:
                    if output.count(b"\n") != 1 or not output.endswith(b"\n"):
                        raise RerankerUnavailable("invalid_output")
                    break
        try:
            result = json.loads(output)
        except (ValueError, UnicodeError):
            raise RerankerUnavailable("invalid_output") from None
        if not isinstance(result, dict):
            raise RerankerUnavailable("invalid_output")
        response_id = result.pop("request_id", None)
        if type(response_id) is not int or response_id != request_id:
            raise RerankerUnavailable("invalid_output")
        if "error" in result:
            code = result["error"]
            raise RerankerUnavailable(code if isinstance(code, str) and code in
                                      {"unavailable", "model_mismatch"} else "invalid_output")
        return result

    def score(self, model_path: str, model_files: list[dict], query: str, texts: list[str], *,
              timeout_seconds=15, idle_seconds=60, mode="whole") -> dict:
        if not self._state.acquire(blocking=False):
            raise RerankerUnavailable("busy")
        try:
            if not self._admission.acquire(blocking=False):
                raise RerankerUnavailable("busy")
            self._cancelled = False
        finally:
            self._state.release()
        process = None
        started = time.monotonic()
        try:
            timeout = _bounded_seconds(timeout_seconds, 60)
            idle = _bounded_seconds(idle_seconds, 300)
            deadline = started + timeout
            if (not isinstance(mode, str) or mode not in ("whole", WINDOW_MODE)
                    or not isinstance(model_path, str) or len(model_path) > 4096
                    or not Path(model_path).is_absolute()
                    or not isinstance(query, str) or len(query.encode("utf-8")) > MAX_TEXT_BYTES
                    or not isinstance(texts, list) or not 1 <= len(texts) <= MAX_PAIRS
                    or any(not isinstance(t, str) or len(t.encode("utf-8")) > MAX_TEXT_BYTES for t in texts)):
                raise RerankerUnavailable("unavailable")
            manifest = _manifest(model_files)
            identity = (str(Path(model_path).resolve()), json.dumps(manifest, sort_keys=True))
            with self._state:
                if self._disabled or self._cancelled:
                    raise RerankerUnavailable("unavailable")
                if self._timer is not None:
                    self._timer.cancel()
                    self._timer = None
                self._generation += 1
                warm = (self._process is not None and self._identity == identity
                        and self._process.poll() is None)
                if not warm:
                    self._stop_locked()
                    process = self._spawn_locked(identity)
                else:
                    process = self._process
            load_started = time.monotonic()
            if not warm:
                ready = self._exchange(process, {"model_path": identity[0], "model_files": manifest}, deadline)
                if set(ready) != {"ready"} or ready["ready"] is not True:
                    raise RerankerUnavailable("invalid_output")
            load_ms = 0.0 if warm else (time.monotonic() - load_started) * 1000
            request = {"query": query, "texts": texts}
            if mode == WINDOW_MODE:
                request["mode"] = WINDOW_MODE
            result = self._exchange(process, request, deadline)
            if mode == WINDOW_MODE:
                output = _window_result(result, texts)
            else:
                if any(field in result for field in ("mode", "windows", "window_count")):
                    raise RerankerUnavailable("invalid_output")
                scores, truncated = result.get("scores"), result.get("truncated_count")
                if (not isinstance(scores, list)
                        or type(truncated) is not int or not 0 <= truncated <= len(texts)):
                    raise RerankerUnavailable("invalid_output")
                if truncated:
                    if scores or result.get("skipped_reason") != "input_truncated":
                        raise RerankerUnavailable("invalid_output")
                elif ("skipped_reason" in result or len(scores) != len(texts)
                        or any(not _finite_number(v) for v in scores)):
                    raise RerankerUnavailable("invalid_output")
                output = {"scores": [float(v) for v in scores], "truncated_count": truncated,
                          **({"skipped_reason": "input_truncated"} if truncated else {})}
            cpu_ms = result.get("cpu_ms", 0.0)
            if not _finite_number(cpu_ms) or cpu_ms < 0:
                raise RerankerUnavailable("invalid_output")
            if time.monotonic() >= deadline:
                raise RerankerUnavailable("timeout")
            with self._state:
                if self._process is not process or self._cancelled:
                    raise RerankerUnavailable("unavailable")
                timer = threading.Timer(idle, self._idle, args=(process, self._generation))
                timer.daemon = True
                self._timer = timer
                timer.start()
            return {**output, "warm": warm, "elapsed_ms": round((time.monotonic() - started) * 1000, 2),
                    "load_ms": round(load_ms, 2), "cpu_ms": float(cpu_ms)}
        except RerankerUnavailable:
            self._discard(process)
            raise
        except (OSError, ValueError, TypeError, subprocess.SubprocessError):
            self._discard(process)
            raise RerankerUnavailable("unavailable") from None
        except BaseException:
            self._discard(process)
            raise
        finally:
            self._admission.release()


runtime = RecallRerankerRuntime()
atexit.register(runtime.close)
score = runtime.score
