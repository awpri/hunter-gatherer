"""hunter/gatherer CLI.

Subcommands:
    hunt chat          interactive REPL (text)
    hunt voice         interactive voice session
    hunt run "<task>"  one-shot run
    hunt consolidate   run sleep-time memory consolidation
    hunt memory ...    inspect the memory store
    hunt cost          show session cost (per-session: use inside chat)
    hunt doctor        check config / connectivity

Entry point declared in pyproject.toml as `hunter_gatherer = hunter_gatherer.cli:main`.
"""
from __future__ import annotations

import argparse
import contextlib
import json
import logging
import sys
import time

from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from hunter_gatherer.config import Settings, get_settings

console = Console()
err_console = Console(stderr=True)


# ── event rendering ───────────────────────────────────────────────────────────
def _render_event(event) -> None:
    """Pretty-print a LoopEvent to the console (non-final events only)."""
    from hunter_gatherer.loop import LoopEvent

    if not isinstance(event, LoopEvent):
        return
    if event.kind == "route":
        d = event.data
        style = {"local": "cyan", "cheap": "yellow", "cloud": "magenta"}.get(d.get("tier"), "white")
        console.print(f"  [dim]route →[/dim] [{style}]{d.get('tier')}[/{style}] ({d.get('model')}) [dim]{d.get('reason')}[/dim]")
    elif event.kind == "tool":
        d = event.data
        preview = (d.get("result_preview") or "")[:80].replace("\n", " ")
        console.print(f"  [dim]tool[/dim] [blue]{d.get('name')}[/blue] [dim]→[/dim] {preview}")
    elif event.kind == "skill":
        console.print(f"  [dim]skill[/dim] [green]{event.data.get('name')}[/green] active")
    elif event.kind == "cost":
        d = event.data
        console.print(f"  [dim]cost[/dim] {d.get('tokens')} tokens · ${d.get('usd'):.4f}")


# ── subcommands ───────────────────────────────────────────────────────────────
def cmd_chat(args, settings: Settings):
    from hunter_gatherer.loop import AgentLoop

    with AgentLoop(settings, workdir=args.workdir) as loop:
        console.print(Panel.fit(
            "[bold]hunter/gatherer[/bold] chat — type your message. "
            "/exit or Ctrl-D to quit. /cost for session totals.",
            border_style="cyan",
        ))
        while True:
            try:
                line = console.input("[bold cyan]›[/bold cyan] ").strip()
            except (EOFError, KeyboardInterrupt):
                console.print("\n[dim]bye[/dim]")
                break
            if not line:
                continue
            if line in ("/exit", "/quit"):
                break
            if line == "/cost":
                console.print(loop.cost.report())
                continue
            if line == "/mem":
                cmd_memory_list(loop.store, limit=10)
                continue
            try:
                result = loop.run(line, on_event=_render_event)
                console.print()  # blank line before answer
                console.print(Panel(result.answer, border_style="green", title="assistant"))
            except Exception as e:
                err_console.print(f"[red]error:[/red] {e}")


def cmd_voice(args, settings: Settings):
    from hunter_gatherer.loop import AgentLoop
    from hunter_gatherer.voice import VoiceSession, speak

    with AgentLoop(settings, workdir=args.workdir) as loop:
        console.print(Panel.fit(
            "[bold]hunter/gatherer[/bold] voice — press Enter to talk, Enter again to stop. "
            "Ctrl-C to quit.",
            border_style="cyan",
        ))
        while True:
            try:
                console.input("[bold cyan]⏎ to record…[/bold cyan] ")
            except (EOFError, KeyboardInterrupt):
                console.print("\n[dim]bye[/dim]")
                break
            try:
                with VoiceSession(settings) as vs:
                    vs.start()
                    console.input("[bold cyan]⏎ to stop…[/bold cyan] ")
                    text = vs.stop()
            except Exception as e:
                err_console.print(f"[red]voice error:[/red] {e}")
                continue
            if not text:
                console.print("[dim](nothing heard)[/dim]")
                continue
            console.print(f"[bold]you:[/bold] {text}")
            result = loop.run(text, on_event=_render_event)
            console.print()
            console.print(Panel(result.answer, border_style="green", title="assistant"))
            if settings.tts != "none":
                speak(result.answer, settings)


def cmd_run(args, settings: Settings):
    from hunter_gatherer.loop import AgentLoop

    with AgentLoop(settings, workdir=args.workdir) as loop:
        result = loop.run(args.task, on_event=_render_event)
        console.print()
        console.print(Panel(result.answer, border_style="green", title="result"))
        if args.show_cost:
            console.print(loop.cost.report())


def cmd_consolidate(args, settings: Settings):
    from hunter_gatherer.loop import AgentLoop

    with AgentLoop(settings) as loop:
        report = loop.consolidate()
        console.print("[green]consolidation complete[/green]")
        console.print(f"  summarized: {report.summarized} episodic memories → {len(report.created)} semantic")
        console.print(f"  deduped:    {report.deduped}")
        console.print(f"  decayed:    {report.decayed}")
        console.print(f"  deleted:    {report.deleted}")


def cmd_memory(args, settings: Settings):
    from hunter_gatherer.memory.store import MemoryStore

    with MemoryStore(settings, allow_embedder_mismatch=args.mem_cmd == "rebuild-vectors") as store:
        if args.mem_cmd == "list":
            cmd_memory_list(store, limit=args.limit)
        elif args.mem_cmd == "search":
            from hunter_gatherer.memory.recall import format_recall, recall

            # Ad-hoc search has no AgentLoop to flush; apply touches eagerly.
            hits = recall(store, args.query, top_k=args.limit, defer_touch=False)
            console.print(format_recall(hits) if hits else "[dim](no matches)[/dim]")
        elif args.mem_cmd == "add":
            from hunter_gatherer.memory.consolidate import Consolidator
            from hunter_gatherer.memory.schemas import Memory

            mem = Memory(text=args.text, type=args.type, importance=args.importance, tags=args.tags or [], source="cli")
            cons = Consolidator(store=store, settings=settings)
            stored = cons.store_with_consolidation(mem)
            if stored is None:
                console.print("[yellow]redundant — already in memory[/yellow]")
            else:
                console.print(f"[green]stored[/green] {stored.id}: {stored.text}")
        elif args.mem_cmd == "count":
            console.print(str(store.count()))
        elif args.mem_cmd == "tune":
            # Park-weight sweep from logged recall usage (2026-08-14 #1).
            from hunter_gatherer.memory.tune import format_sweep, sweep_weights

            rows = store.fetch_recall_usage(limit=10_000)
            if not rows:
                console.print("[dim](no recall-usage rows yet — run some turns first)[/dim]")
            else:
                current = (settings.recency_weight, settings.importance_weight, settings.relevance_weight)
                result = sweep_weights(rows, current, top_k=getattr(args, "top_k", 5))
                console.print(format_sweep(result))
                if getattr(args, "apply", False):
                    from hunter_gatherer.memory.tune import apply_with_guardrails, recommend_weights

                    recommended, _n, note = recommend_weights(
                        rows, current, top_k=getattr(args, "top_k", 5),
                        min_rows=getattr(args, "min_rows", 200),
                    )
                    action = apply_with_guardrails(
                        recommended, current,
                        max_step=settings.recall_weight_max_step,
                    )
                    if action.applied:
                        settings.recency_weight, settings.importance_weight, settings.relevance_weight = action.weights
                        # Closed-loop persistence: append to learned_params so
                        # the next boot rehydrates (env > stored > default).
                        from hunter_gatherer.memory.params import persist_learned_param

                        for key, value in zip(
                            ("recency_weight", "importance_weight", "relevance_weight"),
                            action.weights, strict=True,
                        ):
                            persist_learned_param(store, key, value, source="memory-tune-cli")
                        console.print(
                            f"[green]applied + persisted[/green] "
                            f"(recency={action.weights[0]:.2f}, importance={action.weights[1]:.2f}, "
                            f"relevance={action.weights[2]:.2f}) — survives restarts; "
                            f"set HG_*_WEIGHT to pin"
                        )
                    else:
                        console.print(f"[yellow]not applied[/yellow]: {action.note} ({note})")
                # Half-Life θ (2026-08-31 #1) + relevance floor (2026-08-31 #2):
                # report both; --apply persists the guardrailed θ + floor.
                from hunter_gatherer.memory.tune import (
                    fit_halflife_theta,
                    guardrail_theta,
                    sweep_relevance_floor,
                )

                theta, theta_rep = fit_halflife_theta(store, rows)
                floor, floor_rep = sweep_relevance_floor(rows)
                console.print(f"  HLR θ fit: {theta_rep}")
                console.print(f"  relevance-floor sweep: {floor_rep}")
                if getattr(args, "apply", False):
                    from hunter_gatherer.memory.params import persist_learned_param

                    guarded = guardrail_theta(theta, list(settings.recall_halflife_theta))
                    if guarded is not None and any(abs(t) > 1e-9 for t in guarded):
                        settings.recall_halflife_theta = guarded
                        persist_learned_param(store, "recall_halflife_theta", guarded,
                                              source="memory-tune-cli")
                        console.print(f"[green]θ applied + persisted[/green]: "
                                      f"{[round(t, 3) for t in guarded]} (θ=0 is the legacy curve)")
                    if floor > 0.0 and floor_rep.get("gain", 0) > 0:
                        settings.recall_relevance_floor = floor
                        persist_learned_param(store, "recall_relevance_floor", floor,
                                              source="memory-tune-cli")
                        console.print(f"[green]floor applied + persisted[/green]: {floor} "
                                      "(0 disables; distractors below it abstain)")
        elif args.mem_cmd == "drop":
            store.delete(args.id)
            console.print(f"[red]deleted[/red] {args.id}")
        elif args.mem_cmd == "rebuild-vectors":
            # 2026-08-13 #3: re-embed everything from the SQLite source of
            # truth under the live embedder — the migration path for the
            # documented bge-small → bge-m3 swap.
            from rich.progress import Progress

            with Progress() as progress:
                task = progress.add_task("re-embedding", total=store.count())
                done = 0
                # rebuild in batches for progress feedback
                requested_batch = getattr(args, "batch", None)
                batch = max(1, settings.rebuild_batch if requested_batch is None else requested_batch)
                total = store.rebuild_vectors(batch=batch)
                done = total
                progress.update(task, completed=done)
            report = store.last_vector_rebuild_report
            console.print(f"[green]rebuilt vectors[/green] for {total} memories and "
                          f"{report['document_chunks']} document chunks "
                          f"(fingerprint updated)")
        else:
            console.print("[dim]usage: hunt memory {list|search|add|count|tune|rebuild-vectors|drop}[/dim]")


def cmd_memory_list(store, limit: int = 20):
    from hunter_gatherer.memory.recall import RecallHit  # noqa: F401 (type hint only)
    table = Table(title=f"memories (showing up to {limit})")
    table.add_column("type", style="cyan")
    table.add_column("importance", justify="right")
    table.add_column("text", overflow="fold")
    table.add_column("source", style="dim")
    for m in store.list_all(limit=limit):
        table.add_row(m.type, f"{m.importance:.1f}", m.text[:100], m.source)
    console.print(table)


def cmd_router(args, settings: Settings):
    """`hunter_gatherer router ...` — router observability + α tuning (idea #1)."""
    from hunter_gatherer.memory.store import MemoryStore

    with MemoryStore(settings) as store:
        if args.router_cmd == "log":
            decisions = store.fetch_router_decisions(limit=args.limit)
            if not decisions:
                console.print("[dim](no router decisions logged yet)[/dim]")
                return
            table = Table(title=f"router decisions (showing up to {args.limit})")
            for col in ("tier", "outcome", "score", "α", "tokens", "query"):
                table.add_column(col)
            for d in decisions:
                table.add_row(
                    d.tier, d.outcome,
                    f"{d.score:.2f}" if d.score is not None else "-",
                    f"{d.alpha:.2f}",
                    str(d.in_tokens),
                    d.query[:60],
                )
            console.print(table)
        elif args.router_cmd == "tune":
            from hunter_gatherer.models.router_tune import format_sweep, sweep

            decisions = store.fetch_router_decisions(limit=5000)
            result = sweep(decisions, current_alpha=settings.router_alpha)
            console.print(Panel.fit("[bold]Router α-sweep[/bold]", border_style="cyan"))
            console.print(format_sweep(result))
        elif args.router_cmd == "train":
            # Fit the logistic router head from logged outcomes (2026-08-19 #1).
            from hunter_gatherer.models.router_classifier import (
                heldout_comparison,
                save_classifier,
                train_from_decisions,
            )

            console.print(Panel.fit("[bold]Router classifier training[/bold]", border_style="cyan"))
            decisions = store.fetch_router_decisions(limit=50_000)
            clf, report = train_from_decisions(decisions)
            console.print(f"  labeled rows: {report['rows']} (+{report['positive']} / −{report['negative']})")
            if clf is None:
                console.print(f"[yellow]not trained[/yellow]: {report.get('note', 'insufficient data')}")
                return 1
            console.print(f"  train accuracy: {report['train_accuracy']:.3f}")
            cmp = heldout_comparison(decisions)
            if cmp:
                verdict = "holds" if cmp["classifier"] >= cmp["knn"] else "REGRESSION"
                console.print(
                    f"  held-out ({cmp['heldout']} rows): classifier {cmp['classifier']:.3f} "
                    f"vs kNN {cmp['knn']:.3f} — [green]{verdict}[/green]"
                    if verdict == "holds" else
                    f"  held-out ({cmp['heldout']} rows): classifier {cmp['classifier']:.3f} "
                    f"vs kNN {cmp['knn']:.3f} — [red]{verdict}[/red]"
                )
                if cmp["classifier"] < cmp["knn"]:
                    console.print("[yellow]classifier NOT persisted[/yellow] (A/B gate: must beat the kNN baseline)")
                    return 1
            else:
                console.print("  [dim]held-out comparison skipped (needs ≥20 labeled rows)[/dim]")
            save_classifier(store, clf)
            console.print("[green]persisted[/green] — the router consults it (confidence-gated) on next start")
        else:
            console.print("[dim]usage: hunter_gatherer router {log|tune|train}[/dim]")


def _cmd_eval_memory(settings: Settings) -> int:
    """The longitudinal memory benchmark (2026-08-23 #3)."""
    import os
    import tempfile
    import uuid

    from hunter_gatherer.eval.memory_bench import run_memory_bench
    from hunter_gatherer.eval.report import format_memory_report

    console.print(Panel.fit("[bold]hunter/gatherer longitudinal memory benchmark[/bold]", border_style="cyan"))
    bench_db = os.path.join(tempfile.gettempdir(), f"hunter_gatherer-membench-{uuid.uuid4().hex}.db")
    bench_settings = settings.model_copy(update={"sqlite_path": bench_db})
    try:
        # Recall-only mode: model-free, deterministic, CI-safe. Full mode
        # (AgentLoop answers + local-tier judge) is a documented extension —
        # run_memory_bench(full=True) — once a model backend is configured.
        report = run_memory_bench(bench_settings)
        console.print(format_memory_report(report))
        # The two 2026-08-28 checks: consolidation faithfulness (gate the
        # summarizer before it deletes its sources) + MMR selection armor.
        # Each gets its own fresh DB — they plant their own fixtures.
        from hunter_gatherer.eval.memory_bench import (
            run_distractor_check,
            run_diversity_check,
            run_faithfulness_check,
        )

        ok = report.all_passed
        for name, check in (("faithfulness", run_faithfulness_check),
                            ("diversity", run_diversity_check),
                            ("distractor-gating", run_distractor_check)):
            check_db = os.path.join(tempfile.gettempdir(),
                                    f"hunter_gatherer-memcheck-{name}-{uuid.uuid4().hex}.db")
            passed, note = check(bench_settings.model_copy(update={"sqlite_path": check_db}))
            with contextlib.suppress(FileNotFoundError):
                os.remove(check_db)
            mark = "[green]✓[/green]" if passed else "[red]✗[/red]"
            console.print(f"  {mark} {name}: {note}")
            ok = ok and passed
        return 0 if ok else 1
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.remove(bench_db)


def _cmd_eval_judges(settings: Settings) -> int:
    """The judge calibration bench (2026-09-17 #3)."""
    console.print(Panel.fit("[bold]hunter/gatherer judge calibration[/bold]", border_style="cyan"))
    from hunter_gatherer.eval.judges import run_judges
    from hunter_gatherer.eval.report import format_judges_report

    # LLM judges run against the local tier when one is reachable; a
    # connection probe avoids a hang (they're skipped honestly otherwise).
    llm = None
    with contextlib.suppress(Exception):
        from hunter_gatherer.models.local import make_local_client

        candidate = make_local_client(settings.llama_base_url, settings.local_model)
        from hunter_gatherer.models.base import Message

        candidate.chat([Message(role="user", content="ping")], max_tokens=4)
        llm = candidate
    report = run_judges(settings, llm=llm)
    console.print(format_judges_report(report))
    if getattr(_cmd_eval_judges, "apply", False) or getattr(settings, "judges_apply_thresholds", False):
        from hunter_gatherer.memory.params import persist_learned_param

        sweep = report.threshold_sweep
        best = sweep.get("contradiction_cosine_best")
        if best and best != sweep.get("contradiction_cosine_current"):
            persist_learned_param(None, "contradiction_cosine", best, source="judges-bench") \
                if False else console.print(
                f"[yellow]note[/yellow]: sweep recommends contradiction_cosine={best} — set it "
                "in .env (learned_params hydration for cosine bands is a documented follow-up)")
    return 0 if report.all_passed else 1


def _cmd_eval_ab(settings: Settings) -> int:
    """The A/B research-claim layer (2026-08-13 #1)."""
    import os
    import tempfile
    import uuid

    from hunter_gatherer.eval.ab import run_ab
    from hunter_gatherer.eval.report import format_ab_report

    console.print(Panel.fit("[bold]hunter/gatherer A/B research-claim regression[/bold]", border_style="cyan"))
    eval_db = os.path.join(tempfile.gettempdir(), f"hunter_gatherer-ab-{uuid.uuid4().hex}.db")
    ab_settings = settings.model_copy(update={"sqlite_path": eval_db})
    try:
        from hunter_gatherer.loop import AgentLoop

        loop = AgentLoop(ab_settings)
        try:
            report = run_ab(
                ab_settings,
                store=loop.store,
                embedder=loop.embedder,
                selector=loop.selector,
                registry=loop.registry,
            )
        finally:
            loop.close()
        console.print(format_ab_report(report))
        return 0 if report.all_passed else 1
    finally:
        with contextlib.suppress(FileNotFoundError):
            os.remove(eval_db)


def cmd_pipelines(args, settings: Settings):
    """`hunter_gatherer pipelines ...` — pipeline management (2026-08-13 #2)."""
    from hunter_gatherer.memory.store import MemoryStore

    with MemoryStore(settings) as store:
        if args.pipeline_cmd == "list":
            pipelines = store.list_pipelines()
            if not pipelines:
                console.print("[dim](no saved pipelines)[/dim]")
                return
            table = Table(title=f"pipelines ({len(pipelines)})")
            for col in ("name", "slug", "nodes", "v", "updated"):
                table.add_column(col)
            for p in pipelines:
                table.add_row(p.name, p.slug, str(len(p.nodes)), str(p.version),
                              time.strftime("%Y-%m-%d %H:%M", time.localtime(p.updated_at)))
            console.print(table)
        elif args.pipeline_cmd == "runs":
            runs = store.fetch_pipeline_runs(limit=getattr(args, "limit", 20))
            if not runs:
                console.print("[dim](no pipeline runs recorded)[/dim]")
                return
            table = Table(title=f"pipeline runs (showing up to {getattr(args, 'limit', 20)})")
            for col in ("pipeline", "status", "reason", "tokens", "started"):
                table.add_column(col)
            for r in runs:
                table.add_row(
                    r["pipeline_slug"], r["status"], r["stopped_reason"] or "-",
                    str(r["total_tokens"]),
                    time.strftime("%m-%d %H:%M:%S", time.localtime(r["started_at"])),
                )
            console.print(table)
        elif args.pipeline_cmd == "promote":
            # Manual graduation of a procedural memory into a pipeline.
            from hunter_gatherer.pipelines.from_procedural import promote_if_due

            mem = store.get(args.memory_id)
            if mem is None or mem.type != "procedural":
                console.print(f"[red]not a procedural memory:[/red] {args.memory_id}")
                return 1
            slug = promote_if_due(mem, store, threshold=1)  # manual = force
            if slug:
                console.print(f"[green]promoted[/green] → pipeline '{slug}' (auto-registered as MCP tool)")
                return 0
            already = mem.metadata.get("promoted_pipeline")
            if already:
                console.print(f"[yellow]already promoted[/yellow] as '{already}'")
            else:
                console.print("[red]promotion failed[/red] (no synthesizable tools in sequence)")
            return 1
        else:
            console.print("[dim]usage: hunter_gatherer pipelines {list|promote}[/dim]")


def cmd_eval(args, settings: Settings):
    """`hunt eval` — layered regression suite (idea #2).

    Default: behavior layer only (model-free, free, deterministic). With
    --answers: also drives the AgentLoop for tasks with answer checks (costed).
    With --ab: the A/B research-claim layer (2026-08-13 #1) — verifies each
    documented decision still beats its rejected alternative.
    With --archive: isolated source-retrieval checks using configured embeddings.
    """
    if getattr(args, "archive", False):
        if any(getattr(args, flag, False) for flag in ("ab", "memory", "judges", "answers", "filter")):
            raise ValueError("--archive cannot be combined with another suite, --answers, or --filter")
        from hunter_gatherer.eval.archive_bench import run_archive_bench

        report = run_archive_bench(settings, scenarios_path=args.scenarios, top_k=args.top_k)
        print(json.dumps(report, indent=2, ensure_ascii=False))
        return 0 if report["passed"] else 1
    if getattr(args, "scenarios", None) is not None:
        raise ValueError("--scenarios requires --archive")
    if getattr(args, "ab", False):
        return _cmd_eval_ab(settings)
    if getattr(args, "memory", False):
        return _cmd_eval_memory(settings)
    if getattr(args, "judges", False):
        return _cmd_eval_judges(settings)

    from hunter_gatherer.eval.report import format_report
    from hunter_gatherer.eval.runner import run_eval

    console.print(Panel.fit("[bold]hunter/gatherer eval[/bold]", border_style="cyan"))
    # Use an isolated SQLite for the eval session so it doesn't touch the
    # user's real memory DB.
    import os
    import tempfile
    import uuid

    # A unique path in the temp dir; the store creates the file itself.
    eval_db = os.path.join(tempfile.gettempdir(), f"hunter_gatherer-eval-{uuid.uuid4().hex}.db")
    eval_settings = settings.model_copy(update={"sqlite_path": eval_db})
    try:
        report = run_eval(
            settings=eval_settings,
            answers=args.answers,
            judge=None,  # judge wiring is a documented extension point
            task_filter=args.filter,
        )
        console.print(format_report(report, answers_mode=args.answers, judge_mode=False))
        if args.verbose:
            console.print()
            table = Table(title="per-task detail")
            for col in ("id", "behavior", "answer", "tier"):
                table.add_column(col)
            for tr in report.results:
                table.add_row(
                    tr.task.id,
                    "✓" if tr.behavior.passed else "✗",
                    "—" if tr.answer.skipped else ("✓" if tr.answer.passed else "✗"),
                    tr.task.difficulty,
                )
            console.print(table)
        return 0 if report.behavior_pass == report.behavior_total else 1
    finally:
        with contextlib.suppress(OSError):
            os.unlink(eval_db)


def cmd_library_health(args, settings: Settings):
    """Inspect an existing library without booting models or a writable store."""
    import sqlite3
    import threading
    from pathlib import Path
    from types import SimpleNamespace

    from hunter_gatherer.memory.library_health import library_health

    database = Path(args.database or settings.sqlite_path).expanduser()
    if args.database is None and not database.is_absolute():
        database = Path.home() / ".hunter-gatherer" / database
    database = database.resolve(strict=True)
    connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)
    try:
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA query_only=ON")
        connection.execute("BEGIN")
        reader = SimpleNamespace(_sqlite=connection, _lock=threading.RLock())
        report = library_health(reader, include_sensitive=args.include_sensitive,
                                verify_originals=args.verify_originals, limit=args.limit)
        print(json.dumps(report, indent=2, ensure_ascii=False))
    finally:
        connection.close()


def cmd_serve(args, settings: Settings):
    """`hunt serve` — boot the FastAPI + WebSocket UI server (idea #1).

    The server owns one AgentLoop and exposes REST + WS endpoints for the UI,
    the MCP tool interface at /mcp, and the OpenAPI docs at /docs. This is the
    documented "server-first" path: every wrapper (pywebview, browser, phone)
    is a window on this same server.
    """
    import uvicorn

    from hunter_gatherer.server.app import build_app

    # CLI flags override the settings-derived host/port.
    host = args.host or settings.ui_host
    port = args.port or settings.ui_port
    settings.ui_host, settings.ui_port = host, port
    app = build_app(settings)
    console.print(
        Panel.fit(
            f"[bold]hunter/gatherer server[/bold]\n"
            f"  UI:     http://{host}:{port}/\n"
            f"  API:    http://{host}:{port}/api/*\n"
            f"  WS:     ws://{host}:{port}/ws/stream\n"
            f"  MCP:    http://{host}:{port}/mcp\n"
            f"  docs:   http://{host}:{port}/docs\n"
            f"  LAN:    {'on (token required)' if settings.ui_lan_mode else 'off (loopback only)'}",
            border_style="cyan",
        )
    )
    uvicorn.run(app, host=host, port=port, log_level="info" if args.verbose else "warning", proxy_headers=False)


def cmd_doctor(args, settings: Settings):
    """Check configuration and connectivity."""
    console.print(Panel.fit("[bold]hunter/gatherer doctor[/bold]", border_style="cyan"))
    table = Table(title="config")
    table.add_column("check")
    table.add_column("status")
    # Cloud creds
    table.add_row("openai api key", "[green]set[/green]" if settings.has_openai else "[dim]not set (cloud disabled)[/dim]")
    table.add_row("anthropic api key", "[green]set[/green]" if settings.has_anthropic else "[dim]not set[/dim]")
    # Local LLM
    table.add_row("local llama url", settings.llama_base_url)
    table.add_row("router α", f"{settings.router_alpha}")
    # Qdrant
    try:
        from qdrant_client import QdrantClient

        c = QdrantClient(url=settings.qdrant_url)
        collections = [x.name for x in c.get_collections().collections]
        c.close()
        table.add_row("qdrant", f"[green]reachable[/green] collections={collections}")
    except ImportError:
        table.add_row("qdrant", "not installed — add the qdrant extra from your h/g checkout if configured")
    except Exception as e:
        table.add_row("qdrant", f"[yellow]unreachable ({type(e).__name__})[/yellow] — will use in-memory backend")
    # SQLite
    try:
        import sqlite3

        conn = sqlite3.connect(settings.sqlite_uri(), uri=True)
        conn.execute("SELECT 1")
        conn.close()
        table.add_row("sqlite", f"[green]ok[/green] {settings.sqlite_path}")
    except Exception as e:
        table.add_row("sqlite", f"[red]fail: {e}[/red]")
    # Embeddings
    try:
        from hunter_gatherer.memory.embeddings import get_embedder

        emb = get_embedder(settings)
        table.add_row("embedder", f"{type(emb).__name__} (dim={getattr(emb, 'dim', '?')})")
    except Exception as e:
        table.add_row("embedder", f"[red]fail: {e}[/red]")
    # Voice
    try:
        import faster_whisper  # noqa: F401

        table.add_row("faster-whisper", "[green]installed[/green]")
    except Exception:
        table.add_row("faster-whisper", "[dim]not installed — optional voice extra from your h/g checkout[/dim]")
    console.print(table)


def cmd_backup(args, settings):
    """Portable archive operations do not construct a model or vector client."""
    import json

    from hunter_gatherer.backup import create_backup, restore_backup, verify_backup

    if args.backup_action == "create":
        if getattr(args, "library", None) is not None:
            from hunter_gatherer.library import library_settings
            database = library_settings(args.library).sqlite_path
        elif args.database:
            database = args.database
        else:
            from pathlib import Path
            database = Path(settings.sqlite_path).expanduser()
            if not database.is_absolute():
                database = Path.home() / ".hunter-gatherer" / database
        report = create_backup(database, args.bundle)
    elif args.backup_action == "verify":
        report = verify_backup(args.bundle)
    else:
        report = restore_backup(args.bundle, args.database)
    print(json.dumps(report, indent=2))


def cmd_documents(args, settings):
    """Plan without a database, then explicitly create/run resumable batches."""
    from hunter_gatherer.memory.import_jobs import ImportJobs
    from hunter_gatherer.memory.store import MemoryStore

    action = args.document_action
    if action == "preview":
        result = ImportJobs.preview(args.paths, recursive=args.recursive,
                                    max_files=args.max_files, max_bytes=args.max_bytes)
    elif action in {"status", "list"}:
        # Metadata inspection neither initializes models nor creates a store.
        import sqlite3
        import threading
        from pathlib import Path
        from types import SimpleNamespace

        database = Path(args.database or settings.sqlite_path).expanduser().resolve()
        with contextlib.closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
            connection.row_factory = sqlite3.Row
            jobs = ImportJobs(SimpleNamespace(
                _sqlite=connection, _lock=threading.RLock(), settings=settings,
            ))
            if action == "status":
                result = jobs.status(args.job_id, include_sensitive=args.include_sensitive,
                                     offset=args.offset, limit=args.limit)
            else:
                result = jobs.list(limit=args.limit, include_sensitive=args.include_sensitive)
    else:
        if args.database:
            settings = settings.model_copy(update={"sqlite_path": args.database})
        with MemoryStore(settings) as store:
            jobs = ImportJobs(store)
            if action == "create":
                result = jobs.create(args.paths, recursive=args.recursive,
                                     max_files=args.max_files, max_bytes=args.max_bytes,
                                     sensitive=args.sensitive, category=args.category,
                                     idempotency_key=args.idempotency_key,
                                     include_sensitive=args.include_sensitive)
            else:
                result = jobs.run(args.job_id, batch_size=args.batch_size,
                                  include_sensitive=args.include_sensitive, retry_errors=args.retry_errors)
    # Plain JSON is intentionally unwrapped for scripts and exact source paths.
    print(json.dumps(result, ensure_ascii=False, indent=2))


def cmd_demo(args, _settings=None):
    """An explicit synthetic sandbox, never the ambient configured user store."""
    from hunter_gatherer.demo import create_demo, demo_settings

    if args.demo_action == "create":
        result = create_demo(args.directory)
        print(json.dumps({"fictional": True, "database": result["database"],
                          "documents": len(result["documents"]), "questions": result["questions"]}, indent=2))
        return
    import uvicorn

    from hunter_gatherer.mcp.server import _http_options, build_server
    from hunter_gatherer.mcp.tools import Harness
    from hunter_gatherer.server.library_app import build_library_app

    settings = demo_settings(args.directory)
    harness = Harness.build(settings)
    try:
        server, _ = build_server(harness=harness, tool_profile="library")
        app = build_library_app(harness, server, http_options=_http_options("127.0.0.1", args.port))
        uvicorn.run(app, host="127.0.0.1", port=args.port, proxy_headers=False)
    finally:
        harness.loop.close()


# ── arg parsing ───────────────────────────────────────────────────────────────
def build_parser() -> argparse.ArgumentParser:
    from hunter_gatherer.library import cmd_library_import, cmd_library_init, cmd_library_serve
    from hunter_gatherer.terminal import cmd_evidence

    def bounded_integer(lower, upper):
        def parse(value):
            try:
                parsed = int(value)
            except ValueError as exc:
                raise argparse.ArgumentTypeError("must be an integer") from exc
            if not lower <= parsed <= upper:
                raise argparse.ArgumentTypeError(f"must be between {lower} and {upper}")
            return parsed
        return parse

    def question(value):
        if not value.strip() or len(value) > 4000:
            raise argparse.ArgumentTypeError("question must contain 1 to 4000 characters")
        return value.strip()

    p = argparse.ArgumentParser(prog="hunt", description="hunter/gatherer — a local evidence library.")
    p.add_argument("--verbose", "-v", action="store_true", help="enable debug logging")
    p.add_argument("--workdir", default=".", help="working directory for file tools")
    sub = p.add_subparsers(dest="cmd", required=True)

    init = sub.add_parser("init", help="create an empty, isolated personal library")
    init.add_argument("directory", help="new or empty directory for this library")
    init.add_argument("--semantic", action="store_true", help="use the optional BGE model instead of keyword search")
    init.add_argument("--download-model", action="store_true", help="allow the initial semantic model download")
    init.add_argument("--from-backup", metavar="BUNDLE", help="restore a verified backup into a new library directory")
    init.add_argument("--json", action="store_true")
    init.set_defaults(func=cmd_library_init)

    serve = sub.add_parser("serve", help="serve a personal library's browser interface and MCP")
    serve.add_argument("directory", help="library directory created by hunt init")
    serve.add_argument("--port", type=bounded_integer(1, 65535), default=8767)
    serve.set_defaults(func=cmd_library_serve)

    imports = sub.add_parser("import", help="import selected files into a personal library")
    imports.add_argument("paths", nargs="+", help="files or directories explicitly selected for import")
    imports.add_argument("--library", required=True, metavar="DIR")
    imports.add_argument("--recursive", action="store_true")
    imports.add_argument("--sensitive", action="store_true", help="keep these sources private unless explicitly requested")
    imports.add_argument("--max-files", type=bounded_integer(1, 10000), default=1000)
    imports.add_argument("--max-bytes", type=bounded_integer(1, 10 * 1024**3), default=1024**3)
    imports.add_argument("--json", action="store_true")
    imports.set_defaults(func=cmd_library_import)

    for name, help_text in (("ask", "find source quotes for a question (no generated answer)"),
                            ("open", "read exact cited sources or their surrounding context")):
        command = sub.add_parser(name, help=help_text)
        library = command.add_mutually_exclusive_group()
        library.add_argument("--library", metavar="DIR", help="personal library created with hunt init; ignores ambient configuration")
        library.add_argument("--database", metavar="FILE", help="existing library database; defaults to configured store")
        library.add_argument("--demo-library", metavar="DIR", help="isolated fictional demo; ignores personal configuration")
        library.add_argument("--server", metavar="URL", help="use an already-running local library, e.g. http://127.0.0.1:8767; ignores personal configuration")
        command.add_argument("--json", action="store_true", help="lossless structured output for scripts and AI; diagnostics use stderr")
        command.add_argument("--max-tokens", type=bounded_integer(512, 16384), default=4096,
                             help="approximate output budget per page, 512–16384 (default: 4096)")
        command.add_argument("--include-sensitive", action="store_true", help="explicitly include private records")
        if name == "ask":
            command.add_argument("question", type=question, help="natural-language question in quotes")
            command.add_argument("--pages", type=bounded_integer(1, 20), default=1,
                                 help="maximum search pages to retrieve in this session (default: 1)")
        else:
            command.add_argument("refs", nargs="+", metavar="CITATION", help="exact quoted citation(s) from Ask or MCP")
            command.add_argument("--view", choices=["text", "context", "outline"], default="text")
            command.add_argument("--offset", type=bounded_integer(0, 1000000), default=0,
                                 help="section offset for outline view")
        command.set_defaults(func=cmd_evidence)

    setup = sub.add_parser("setup", help="inspect optional capabilities and show install commands; downloads nothing")
    setup.add_argument("--json", action="store_true", help="structured capability report")
    def setup_command(args, _settings):
        from hunter_gatherer.setup import cmd_setup
        return cmd_setup(args)
    setup.set_defaults(func=setup_command)

    demo = sub.add_parser("demo", help="create or serve an isolated fictional offline library")
    demo_sub = demo.add_subparsers(dest="demo_action", required=True)
    for action in ("create", "serve"):
        command = demo_sub.add_parser(action)
        command.add_argument("directory", help="explicit isolated demo directory")
        if action == "serve":
            from hunter_gatherer.mcp.server import _listen_port

            command.add_argument("--port", type=_listen_port, default=8767)
    demo.set_defaults(func=cmd_demo)

    sub.add_parser("chat", help="interactive text REPL").set_defaults(func=cmd_chat)
    sub.add_parser("voice", help="interactive voice session").set_defaults(func=cmd_voice)

    documents = sub.add_parser("documents", help="preview and resume bulk document imports")
    document_sub = documents.add_subparsers(dest="document_action", required=True)
    for action in ("preview", "create"):
        command = document_sub.add_parser(action, help="inspect selected files" if action == "preview" else "save an import manifest; imports nothing yet")
        command.add_argument("paths", nargs="+", help="selected files or folders")
        command.add_argument("--recursive", action="store_true", help="include supported files below selected folders")
        command.add_argument("--max-files", type=int, default=1000)
        command.add_argument("--max-bytes", type=int, default=1024**3)
        if action == "create":
            command.add_argument("--database", help="target database; defaults to configured store")
            command.add_argument("--sensitive", action="store_true")
            command.add_argument("--include-sensitive", action="store_true", help="allow private job details on a repeated create request")
            command.add_argument("--category", default="personal")
            command.add_argument("--idempotency-key", help="reuse for an exact create retry")
    for action in ("status", "list", "run"):
        command = document_sub.add_parser(action)
        command.add_argument("--database", help="target database; defaults to configured store")
        command.add_argument("--include-sensitive", action="store_true")
        if action != "list":
            command.add_argument("job_id")
        if action == "run":
            command.add_argument("--batch-size", type=int, default=10, help="maximum files processed by this call")
            command.add_argument("--retry-errors", action="store_true", help="retry eligible failed files; changed sources need a new job")
        else:
            command.add_argument("--limit", type=int, default=50)
            if action == "status":
                command.add_argument("--offset", type=int, default=0)
    documents.set_defaults(func=cmd_documents)

    backups = sub.add_parser("backup", help="portable SQLite and document-original backup/restore")
    backup_sub = backups.add_subparsers(dest="backup_action", required=True)
    create = backup_sub.add_parser("create", help="create and verify a new backup directory")
    create.add_argument("bundle", help="new destination directory")
    source = create.add_mutually_exclusive_group()
    source.add_argument("--database", help="source database; defaults to configured store")
    source.add_argument("--library", metavar="DIR", help="personal library to back up")
    verify = backup_sub.add_parser("verify", help="verify bundle checksums and integrity")
    verify.add_argument("bundle")
    restore = backup_sub.add_parser("restore", help="restore into a fresh database and archive location")
    restore.add_argument("bundle")
    restore.add_argument("--database", required=True, help="new database path; never overwrites")
    backups.set_defaults(func=cmd_backup)

    run_p = sub.add_parser("run", help="one-shot run")
    run_p.add_argument("task", help="the task to perform")
    run_p.add_argument("--show-cost", action="store_true")
    run_p.set_defaults(func=cmd_run)

    sub.add_parser("consolidate", help="run sleep-time memory consolidation").set_defaults(func=cmd_consolidate)

    router_p = sub.add_parser("router", help="router observability + α tuning")
    router_sub = router_p.add_subparsers(dest="router_cmd", required=True)
    rlp = router_sub.add_parser("log", help="show recent router decisions")
    rlp.add_argument("--limit", type=int, default=20)
    router_sub.add_parser("tune", help="sweep α against logged outcomes; recommend a value")
    router_sub.add_parser("train", help="fit the logistic router head from logged outcomes (2026-08-19 #1)")
    router_p.set_defaults(func=cmd_router)

    eval_p = sub.add_parser("eval", help="run the layered regression suite (idea #2)")
    eval_p.add_argument("--answers", action="store_true",
                        help="also run the answer layer (costed; needs a model)")
    eval_p.add_argument("--ab", action="store_true",
                        help="run the A/B research-claim layer (2026-08-13 #1) instead")
    eval_p.add_argument("--memory", action="store_true",
                        help="run the longitudinal memory benchmark (2026-08-23 #3) instead")
    eval_p.add_argument("--judges", action="store_true",
                        help="run the judge calibration bench (2026-09-17 #3) instead")
    eval_p.add_argument("--archive", action="store_true",
                        help="evaluate exact archive evidence using isolated synthetic source documents")
    eval_p.add_argument("--scenarios", default=None,
                        help="custom inline JSON scenario file for --archive")
    eval_p.add_argument("--top-k", type=int, default=5,
                        help="evidence retrieval limit for --archive (1–20)")
    eval_p.add_argument("--filter", default=None,
                        help="only run tasks whose id contains this substring")
    eval_p.add_argument("--verbose", "-v", action="store_true", help="show per-task detail")
    eval_p.set_defaults(func=cmd_eval)

    health_p = sub.add_parser("library-health", help="read-only library diagnostics; never initializes models")
    health_p.add_argument("--database", default=None, help="existing SQLite database; defaults to configured store")
    health_p.add_argument("--include-sensitive", action="store_true")
    health_p.add_argument("--verify-originals", action="store_true", help="also verify checksums of managed originals")
    health_p.add_argument("--limit", type=int, default=100, help="maximum reported issues (1–1000)")
    health_p.set_defaults(func=cmd_library_health)

    mem = sub.add_parser("memory", help="inspect/manage memory")
    mem_sub = mem.add_subparsers(dest="mem_cmd", required=True)
    lp = mem_sub.add_parser("list")
    lp.add_argument("--limit", type=int, default=20)
    sp = mem_sub.add_parser("search")
    sp.add_argument("query")
    sp.add_argument("--limit", type=int, default=5)
    ap = mem_sub.add_parser("add")
    ap.add_argument("text")
    ap.add_argument("--type", default="semantic", choices=["semantic", "episodic", "procedural"])
    ap.add_argument("--importance", type=float, default=5.0)
    ap.add_argument("--tags", nargs="*", default=[])
    mem_sub.add_parser("count")
    tune_p = mem_sub.add_parser("tune", help="sweep Park weights against logged recall usage")
    tune_p.add_argument("--top-k", type=int, default=5, dest="top_k")
    tune_p.add_argument("--min-rows", type=int, default=200, dest="min_rows")
    tune_p.add_argument("--apply", action="store_true", help="apply the recommendation within guardrails")
    dp = mem_sub.add_parser("drop")
    dp.add_argument("id")
    rb = mem_sub.add_parser("rebuild-vectors", help="atomically re-embed local memories and document chunks")
    rb.add_argument("--batch", type=int, default=None, dest="batch")
    mem.set_defaults(func=cmd_memory)

    sub.add_parser("doctor", help="check config / connectivity").set_defaults(func=cmd_doctor)

    pipe_p = sub.add_parser("pipelines", help="pipeline management")
    pipe_sub = pipe_p.add_subparsers(dest="pipeline_cmd")
    pipe_sub.add_parser("list", help="list saved pipelines")
    runs_p = pipe_sub.add_parser("runs", help="list recorded pipeline runs (durable history)")
    runs_p.add_argument("--limit", type=int, default=20)
    promo_p = pipe_sub.add_parser("promote", help="graduate a procedural memory into a pipeline")
    promo_p.add_argument("memory_id", help="id of the procedural memory to promote")
    pipe_p.set_defaults(func=cmd_pipelines)

    serve_p = sub.add_parser("advanced-serve", help="experimental agent workbench server; uses configured providers")
    serve_p.add_argument("--host", default=None, help="bind host (default: from settings)")
    serve_p.add_argument("--port", type=int, default=None, help="bind port (default: 8765)")
    serve_p.set_defaults(func=cmd_serve)
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.WARNING,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    # Apply workdir from top-level flag if the subcommand supports it.
    try:
        # Demo startup bypasses both cached settings and user environment/.env.
        isolated = (args.cmd in {"demo", "setup", "init", "serve", "import"}
                    or getattr(args, "library", None) is not None or bool(getattr(args, "demo_library", None))
                    or (args.cmd == "backup" and args.backup_action in {"verify", "restore"})
                    or getattr(args, "server", None) is not None)
        settings = None if isolated else get_settings()
        return args.func(args, settings) or 0
    except KeyboardInterrupt:
        return 130
    except BrokenPipeError:
        # Normal when a consumer such as head closes a pipeline early.
        return 0
    except Exception as e:
        if getattr(args, "json", False):
            print(json.dumps({"error": {"type": type(e).__name__, "message": str(e)}}), file=sys.stderr)
        else:
            from hunter_gatherer.terminal import safe_text
            err_console.print(f"error: {type(e).__name__}: {safe_text(e)}", markup=False, highlight=False)
        if args.verbose and not getattr(args, "json", False):
            raise
        return 1


if __name__ == "__main__":
    sys.exit(main())
