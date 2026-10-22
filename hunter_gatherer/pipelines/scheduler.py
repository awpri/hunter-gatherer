"""Pipeline triggers + the unattended scheduler (2026-08-20 #3, made durable).

Pipelines run only when explicitly invoked — until now. A ``trigger`` field
gives them three unattended modes: 5-field cron, an interval, or a Park-style
``importance_threshold`` that fires when enough important memories accumulate
(the same signal-first pattern the reflection trigger established — cron is
the clock fallback, importance is the default-worthy signal).

Durability (this revision): every fire gets a ``pipeline_runs`` row plus
per-node ``pipeline_node_results`` checkpoints, so

  - ``fire()`` runs in a single-worker pool, NOT the ticker thread — an
    eight-minute pipeline no longer starves every other trigger;
  - trigger state (last fire time) is read from the runs table, so restart
    behavior is deliberate: cron fires at most once per matching minute,
    intervals resume their cadence from the persisted timestamp;
  - runs orphaned by a dead process are detected at startup and RESUMED from
    their last completed node checkpoint (abandoned when the pipeline is
    gone).

Park et al.'s agents proactively plan and react over their memory stream —
the seeded ``daily-brief`` template (recall → summarize → store + notify) is
that loop made real: the harness acting on its own memory without being
asked.
"""
from __future__ import annotations

import contextlib
import json
import logging
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

log = logging.getLogger(__name__)


# ── cron matching (5-field, hand-rolled) ─────────────────────────────────────
def _field_matches(expr: str, value: int, lo: int, hi: int) -> bool:
    for part in expr.split(","):
        part = part.strip()
        if not part:
            continue
        step = 1
        if "/" in part:
            part, _, step_s = part.partition("/")
            try:
                step = int(step_s)
            except ValueError:
                step = 1
        if part == "*":
            start, end = lo, hi
        elif "-" in part:
            a, _, b = part.partition("-")
            try:
                start, end = int(a), int(b)
            except ValueError:
                continue
        else:
            try:
                start = end = int(part)
            except ValueError:
                continue
        if start <= value <= end and (value - start) % max(1, step) == 0:
            return True
    return False


def cron_matches(expr: str, now: time.struct_time) -> bool:
    """5-field cron: minute hour day-of-month month day-of-week."""
    fields = expr.split()
    if len(fields) != 5:
        return False
    minute, hour, dom, mon, dow = fields
    return (
        _field_matches(minute, now.tm_min, 0, 59)
        and _field_matches(hour, now.tm_hour, 0, 23)
        and _field_matches(dom, now.tm_mday, 1, 31)
        and _field_matches(mon, now.tm_mon, 1, 12)
        and _field_matches(dow, (now.tm_wday + 1) % 7, 0, 6)  # cron: 0=Sunday
    )


# ── store-backed node checkpoint (the interpreter's durability seam) ─────────
class _StoreCheckpoint:
    def __init__(self, store, run_id: str):
        self.store = store
        self.run_id = run_id

    def completed_nodes(self) -> dict[str, dict]:
        return self.store.pipeline_node_checkpoints(self.run_id)

    def save_node(self, node_id: str, kind: str, outputs: dict,
                  latency_ms: float, tokens: int, cost_usd: float,
                  error: str | None = None) -> None:
        self.store.checkpoint_pipeline_node(
            self.run_id, node_id, kind, outputs, latency_ms, tokens, cost_usd, error
        )


# ── the scheduler ────────────────────────────────────────────────────────────
class PipelineScheduler:
    """Minute ticker evaluating pipeline triggers; fires run in a dedicated
    worker (never the ticker thread) with per-node checkpoints persisted."""

    def __init__(self, loop, *, on_event=None, tick_seconds: float = 60.0,
                 seed_daily_brief: bool = True):
        self.loop = loop
        self.on_event = on_event  # optional callable(kind, data) for the UI/WS
        self.tick_seconds = tick_seconds
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._pool: ThreadPoolExecutor | None = None
        self._importance_baseline: float | None = None
        self.runs = 0
        self._memory_events: list[tuple] = []  # (ts, text, kind) — 2026-09-09 #3
        # Bridge the process-local belief-event bus into this scheduler's
        # queue (2026-09-09 #3).
        from hunter_gatherer.memory.events import add_listener

        add_listener(lambda ts, text, kind: self._memory_events.append((ts, text, kind)))
        self.last_run_at: float | None = None
        if seed_daily_brief:
            with contextlib.suppress(Exception):
                self._seed_daily_brief()

    # ── lifecycle ──────────────────────────────────────────────────────────
    def start(self) -> None:
        if self._thread is not None:
            return
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="pipeline-run")
        # Snapshot the importance baseline so the threshold trigger measures
        # accumulation *since scheduling started* (it re-syncs after each fire
        # and drifts harmlessly when reflection resets the counter).
        self._importance_baseline = self._pending_importance()
        with contextlib.suppress(Exception):
            self._resume_orphans()
        self._thread = threading.Thread(target=self._run, daemon=True, name="pipeline-scheduler")
        self._thread.start()
        log.info("pipeline scheduler started (%.0fs tick)", self.tick_seconds)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None
        if self._pool is not None:
            self._pool.shutdown(wait=False, cancel_futures=True)
            self._pool = None

    def _run(self) -> None:
        while not self._stop.wait(self.tick_seconds):
            with contextlib.suppress(Exception):
                self.tick()

    def _resume_orphans(self) -> None:
        """Runs left 'running' by a dead process: resume from checkpoints
        when the pipeline still exists, abandon otherwise."""
        resumed = abandoned = 0
        for orphan in self.loop.store.orphaned_pipeline_runs():
            pipeline = self.loop.store.get_pipeline(orphan["pipeline_id"])
            if pipeline is None:
                self.loop.store.finish_pipeline_run(
                    orphan["id"], "abandoned", stopped_reason="pipeline deleted",
                )
                abandoned += 1
            else:
                self._submit_fire(pipeline, resume_run_id=orphan["id"])
                resumed += 1
        if resumed or abandoned:
            log.info("orphaned runs: %d resumed, %d abandoned", resumed, abandoned)

    # ── belief-triggered pipelines (2026-09-09 #3) ──────────────────────────
    # memory_match triggers: fire when a *belief changes* — the event-driven
    # half of Park's plan-and-react. Consolidation/supersession call
    # note_memory_event(); the minute tick matches events against registered
    # triggers (cosine ≥ floor vs the trigger's cached query embedding) and
    # fires matches with debounce.
    def note_memory_event(self, text: str, kind: str) -> None:
        """Record a belief change (ADD/UPDATE/SUPERSede) for trigger matching."""
        self._memory_events.append((time.time(), text[:500], kind))
        del self._memory_events[:-50]  # bounded queue

    def _drain_memory_events(self) -> int:
        """Evaluate queued belief events against memory_match triggers."""
        fired = 0
        if not self._memory_events:
            return 0
        events, self._memory_events = self._memory_events, []
        for pipeline in self.loop.store.list_pipelines():
            t = pipeline.trigger or {}
            match = t.get("memory_match")
            if not match:
                continue
            floor = float(match.get("min_relevance", 0.6))
            kinds = set(match.get("on", ["add", "update", "supersede"]))
            debounce = float(match.get("debounce_minutes", 5)) * 60
            last = self._last_fired(pipeline.id) or 0.0
            for _ts, text, kind in events:
                if kind not in kinds or (time.time() - last) < debounce:
                    continue
                with contextlib.suppress(Exception):
                    emb = self.loop.embedder
                    sim = _cos(emb.embed(match.get("query", "")), emb.embed(text))
                    if sim >= floor:
                        self._submit_fire(pipeline)
                        fired += 1
                        break
        return fired

    # ── triggering ─────────────────────────────────────────────────────────
    def _pending_importance(self) -> float:
        with contextlib.suppress(Exception):
            return float(self.loop.store.pending_importance())
        return 0.0

    def _last_fired(self, pipeline_id: str) -> float | None:
        """Persisted trigger state — the newest run row for the pipeline, so
        restart behavior is deliberate (not whatever an in-memory dict had)."""
        with contextlib.suppress(Exception):
            return self.loop.store.last_pipeline_run_start(pipeline_id)
        return None

    def due(self, pipeline, now: float | None = None) -> bool:
        """Is this pipeline's trigger due? Pure — no side effects."""
        t = pipeline.trigger
        if not t:
            return False
        now = now if now is not None else time.time()
        last = self._last_fired(pipeline.id)
        if "cron" in t:
            if not cron_matches(str(t["cron"]), time.localtime(now)):
                return False
            # At most one fire per matching minute, restart-safe: fire only
            # when the last fire predates the current minute.
            return last is None or int(last // 60) < int(now // 60)
        if "interval_minutes" in t:
            interval = max(0.5, float(t["interval_minutes"])) * 60
            return last is None or (now - last) >= interval
        if "importance_threshold" in t:
            base = self._importance_baseline
            if base is None:
                return False
            return (self._pending_importance() - base) >= float(t["importance_threshold"])
        return False

    def tick(self, *, now: float | None = None) -> int:
        """Evaluate every triggered pipeline; SUBMIT the due ones to the run
        pool (the ticker never blocks on an eight-minute pipeline). Returns
        the number of runs submitted."""
        now = now if now is not None else time.time()
        submitted = 0
        for pipeline in self.loop.store.list_pipelines():
            if pipeline.trigger and self.due(pipeline, now):
                self._submit_fire(pipeline)
                submitted += 1
        submitted += self._drain_memory_events()  # 2026-09-09 #3
        return submitted

    # ── firing ─────────────────────────────────────────────────────────────
    def _submit_fire(self, pipeline, *, resume_run_id: str | None = None) -> str:
        """Hand the fire to the worker pool; returns the run id immediately."""
        run_id = resume_run_id or str(uuid.uuid4())
        if self._pool is not None:
            self._pool.submit(self._fire_sync, pipeline, run_id)
        else:  # not started (tests / direct use) — run inline
            self._fire_sync(pipeline, run_id)
        return run_id

    def fire(self, pipeline) -> dict:
        """Public synchronous fire (returns the run outcome). The scheduler
        tick uses the pool-backed path; use this for direct invocation."""
        return self._fire_sync(pipeline, str(uuid.uuid4()))

    def _fire_sync(self, pipeline, run_id: str) -> dict:
        store = self.loop.store
        with contextlib.suppress(Exception):
            store.start_pipeline_run(run_id, pipeline.id, pipeline.slug,
                                     trigger=json.dumps(pipeline.trigger or {}))
        from hunter_gatherer.memory.consolidate import Consolidator
        from hunter_gatherer.memory.schemas import Memory
        from hunter_gatherer.pipelines.interpreter import PipelineInterpreter
        from hunter_gatherer.pipelines.nodes import PipelineContext

        ctx = PipelineContext.from_loop(self.loop)
        result = PipelineInterpreter(pipeline, ctx).run(
            inputs={}, checkpoint=_StoreCheckpoint(store, run_id))
        self.runs += 1
        self.last_run_at = time.time()
        self._importance_baseline = self._pending_importance()
        status = "done" if result.stopped_reason in ("done", "empty", "budget_exceeded") else "error"
        with contextlib.suppress(Exception):
            store.finish_pipeline_run(
                run_id, status, stopped_reason=result.stopped_reason,
                total_tokens=result.total_tokens, total_cost_usd=result.total_cost_usd,
                error=result.error,
            )
        summary = (
            f"Pipeline '{pipeline.name}' ran unattended: {len(result.transcript)} nodes, "
            f"stopped={result.stopped_reason}, tokens={result.total_tokens}, "
            f"outputs={list(result.outputs)[:5]}"
        )
        with contextlib.suppress(Exception):
            Consolidator(store=store, settings=self.loop.settings).store_with_consolidation(
                Memory(text=summary, type="episodic", importance=4.0, source="pipeline-scheduler",
                       tags=["pipeline-run"], metadata={"pipeline": pipeline.slug, "run_id": run_id})
            )
        if self.on_event is not None:
            with contextlib.suppress(Exception):
                self.on_event("pipeline_run", {
                    "pipeline": pipeline.slug, "name": pipeline.name, "run_id": run_id,
                    "stopped_reason": result.stopped_reason,
                    "nodes": len(result.transcript), "tokens": result.total_tokens,
                })
        log.info("%s", summary)
        return {"run_id": run_id, "stopped_reason": result.stopped_reason,
                "nodes": len(result.transcript), "tokens": result.total_tokens,
                "outputs": result.outputs}

    # ── daily-brief template ───────────────────────────────────────────────
    def _seed_daily_brief(self) -> None:
        """Ensure the daily-brief pipeline exists — Park's plan-and-react loop
        made real: recall the recent stream, summarize via the local tier,
        store + notify. Trigger: importance-first would fire too often for a
        brief; the cron default is 08:00 daily."""
        store = self.loop.store
        if store.get_pipeline_by_slug("daily_brief"):
            return
        from hunter_gatherer.pipelines.schemas import Pipeline

        store.save_pipeline(Pipeline.from_dict({
            "name": "Daily Brief",
            "slug": "daily_brief",
            "description": "Summarize the last day of memories into a brief and store it.",
            "inputs": [],
            "nodes": [
                {"id": "recall", "kind": "memory.recall", "config": {"query": "recent events and facts from the last day", "top_k": 8}},
                {"id": "summarize", "kind": "model.invoke",
                 "config": {"tier": "local", "system": "Summarize the following memories into a concise daily brief: key developments, open threads, anything needing attention. Reply with the brief only.",
                             "temperature": 0.2}},
                {"id": "store", "kind": "memory.store", "config": {"importance": 4.0, "type": "episodic"}},
            ],
            "edges": [
                {"source": "recall", "target": "summarize", "source_port": "out", "target_port": "prompt"},
                {"source": "summarize", "target": "store", "source_port": "out", "target_port": "text"},
            ],
            "token_budget": 4000,
            "trigger": {"cron": "0 8 * * *"},
        }))
        log.info("seeded daily-brief pipeline (cron 0 8 * * *)")


def _cos(a, b) -> float:
    import math

    dot = sum(x * y for x, y in zip(a, b, strict=True))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)
