#!/usr/bin/env python3
"""Measure several assistants using one library at once, then check its integrity.

Everything runs against a new, fictional library in a temporary directory; no
existing library is opened. Two deployment shapes are exercised:

  threads    one process, one shared store (how ``hunt serve`` handles clients)
  processes  one process per client, each with its own connection and vector
             index (how several stdio ``hunt-mcp --library`` clients behave)

Each client runs a mixed workload through the public tool handlers: Find, Find
continuation, Open, profile, remember, replace and forget, including deliberate
races on a small set of shared "hot" memories. Afterwards the database is
inspected for outcomes that must never happen whatever the timing:

  resurrected           a forget that was acknowledged, yet the memory is active
  forked_history        one memory replaced by two different corrections
  duplicates            two active memories with identical text
  lost_writes           an acknowledged remember that does not exist
  loosened              a sensitive memory, or its correction, no longer sensitive
  index_drift           the in-memory vector index disagrees with the live records
  repeated              a continuation page repeating a source from the first page
  hidden_delivered      a continuation page delivering a memory whose forget or
                        correction had been acknowledged before that page was asked for
  log_bytes_while_idle  bytes waiting in the write-ahead log when every client
                        has finished its work but is still connected
  log_bytes_at_rest     the same once every client has closed
  main_file_missing     records absent from a copy of ``library.db`` taken by
                        itself at that point

h/g chooses the journal mode from where a library lives, so by default this
measures what a library in the temporary directory gets by itself (``auto``).
``--journal delete,wal`` pins each mode in turn to compare them.

Latency is wall-clock time seen by the client. Results depend on the machine
and its load; compare configurations measured in the same session, and treat
the integrity counts, not the timings, as the correctness evidence. A clean run
is evidence about this workload only. It is not a proof.

    python scripts/benchmark_concurrency.py --mode both
    python scripts/benchmark_concurrency.py --mode processes --journal delete,wal --clients 8
"""
from __future__ import annotations

import argparse
import contextlib
import json
import multiprocessing
import os
import random
import shutil
import sqlite3
import statistics
import sys
import tempfile
import threading
import time
from pathlib import Path

TOPICS = ["copper", "quartz", "heron", "lantern", "basalt", "otter", "willow", "ember",
          "saffron", "glacier", "harbor", "juniper", "kestrel", "marble", "nectar", "onyx"]
VERBS = ["records", "prefers", "repairs", "measures", "stores", "maps", "restores", "labels"]


def _sentence(rng: random.Random, serial: str) -> str:
    return (f"Fictional {rng.choice(TOPICS)} team {rng.choice(VERBS)} the {rng.choice(TOPICS)} "
            f"{rng.choice(TOPICS)} sample number {serial}.")


def build_library(directory: Path, memories: int, documents: int, journal: str, seed: int) -> list[str]:
    """Create a fictional keyword library and return the IDs of its hot memories."""
    from hunter_gatherer.library import create_library
    from hunter_gatherer.memory.documents import DocumentArchive
    from hunter_gatherer.memory.schemas import Memory
    from hunter_gatherer.memory.store import MemoryStore

    rng = random.Random(seed)
    create_library(directory)
    if journal != "auto":
        # Also set directly, so the script can measure code that predates the automatic choice.
        with contextlib.closing(sqlite3.connect(directory / "library.db")) as connection:
            connection.execute(f"PRAGMA journal_mode={journal}")
    settings = _settings(directory, journal)
    sources = directory.parent / (directory.name + "-sources")
    sources.mkdir()
    with MemoryStore(settings) as store:
        for index in range(memories):
            store.add(Memory(text=_sentence(rng, f"m{index}")))
        hot = [store.add(Memory(text=f"Fictional shared decision {index} about {rng.choice(TOPICS)}.")).id
               for index in range(8)]
        archive = DocumentArchive(store)
        for index in range(documents):
            path = sources / f"note-{index}.md"
            first = " ".join(_sentence(rng, f"d{index}a{n}") for n in range(6))
            second = " ".join(_sentence(rng, f"d{index}b{n}") for n in range(6))
            path.write_text(f"# Fictional field note {index}\n\n{first}\n\n## Details\n\n{second}\n")
            archive.ingest(str(path), title=f"Field note {index}")
    return hot


def slow_embedder(inner, delay: float):
    """The neural embedder with a stand-in model that takes ``delay`` per call.

    Only the model is simulated. The embedder around it is the real one, so a
    text it has embedded before costs nothing and model calls take turns, as
    with bge-small. The stand-in sleeps without the interpreter lock, as native
    model code does, and returns the library's own keyword vectors.
    """
    from array import array

    from hunter_gatherer.memory.embeddings import SentenceTransformerEmbedder

    class StandInModel:
        def encode(self, texts, **_options):
            time.sleep(delay)
            return [array("f", vector) for vector in inner.embed_batch(list(texts))]

    class SlowEmbedder(SentenceTransformerEmbedder):
        def embed_sparse(self, text):
            return inner.embed_sparse(text)

        def fingerprint(self):
            return inner.fingerprint()

    embedder = SlowEmbedder("stand-in model")
    embedder._model, embedder.dim = StandInModel(), inner.dim
    return embedder


def _settings(directory: Path, journal: str):
    """The library's settings, with the journal mode pinned unless it is left to h/g."""
    from hunter_gatherer.library import library_settings

    settings = library_settings(directory)
    return settings if journal == "auto" else settings.model_copy(update={"sqlite_journal": journal})


def open_harness(directory: Path, embed_ms: float, journal: str = "auto"):
    from hunter_gatherer.library import build_library_harness

    harness = build_library_harness(_settings(directory, journal))
    if embed_ms > 0:
        slow = slow_embedder(harness.store.embedder, embed_ms / 1000)
        harness.store.embedder = slow
        harness.loop.embedder = slow
    return harness


def classify(error: BaseException) -> str:
    text = str(error).lower()
    if isinstance(error, sqlite3.OperationalError) and ("locked" in text or "busy" in text):
        return "database_locked"
    if "library changed during find" in text:
        return "find_interrupted"
    if "find cursor" in text:
        return "cursor_stale"
    if "no active transaction" in text or "within a transaction" in text:
        return "saw_another_threads_transaction"
    return f"error:{type(error).__name__}"


def run_client(harness, client: int, operations: int, hot: list[str], seed: int) -> dict:
    """One assistant's mixed workload. Returns timings, outcomes and what it was told."""
    from hunter_gatherer.mcp.tools import call_tool

    rng = random.Random(seed * 1000 + client)
    timings: dict[str, list[float]] = {}
    outcomes: dict[str, int] = {}
    # "hidden" records when a forget or correction was acknowledged; "continued"
    # records when each continuation was asked for and the memories it delivered.
    told = {"remembered": [], "forgotten": [], "sensitive": [], "corrections": [],
            "hidden": [], "continued": [], "repeated": 0}
    mine: list[str] = []
    citations: list[str] = []

    def note(outcome: str) -> None:
        outcomes[outcome] = outcomes.get(outcome, 0) + 1

    def timed(name: str, tool: str, arguments: dict):
        started = time.perf_counter()
        try:
            result = call_tool(harness, tool, arguments)
        except Exception as error:  # every failure is counted, never hidden
            timings.setdefault(name, []).append(time.perf_counter() - started)
            note(f"{name}:{classify(error)}")
            return None
        timings.setdefault(name, []).append(time.perf_counter() - started)
        if isinstance(result, dict) and result.get("error_code") == "memory_conflict":
            note(f"{name}:conflict_reported")
        elif isinstance(result, dict) and result.get("error_code") == "library_busy":
            note(f"{name}:library_busy")  # waited the full time for another process
        elif isinstance(result, dict) and result.get("error"):
            note(f"{name}:refused")
        else:
            note(f"{name}:ok")
        return result

    for step in range(operations):
        roll = rng.random()
        if roll < 0.40:
            result = timed("find", "hg_find", {"query": f"{rng.choice(TOPICS)} {rng.choice(TOPICS)} sample",
                                                "max_tokens": 2048})
            if result:
                citations = [item["citation"] for key in ("relevant_memories", "supporting_passages")
                             for item in result.get(key, []) if item.get("citation")][:4]
                cursor = (result.get("continuation") or {}).get("next_cursor")
                if cursor and rng.random() < 0.5:
                    asked = time.time()
                    more = timed("find_more", "hg_find", {"cursor": cursor, "max_tokens": 2048})
                    if more:
                        later = _represented(more)
                        told["repeated"] += len(later & _represented(result))
                        told["continued"].append([asked, sorted(ref[5:-1] for ref in later
                                                                 if ref.startswith("[mem:"))])
        elif roll < 0.55:
            if citations:
                timed("open", "hg_open", {"refs": citations[:2]})
        elif roll < 0.60:
            timed("profile", "hg_memory", {"action": "profile", "query": rng.choice(TOPICS)})
        elif roll < 0.76:
            sensitive = rng.random() < 0.2
            result = timed("remember", "hg_memory", {
                "action": "remember", "text": _sentence(rng, f"c{client}s{step}"), "sensitive": sensitive})
            if result and result.get("stored") and result.get("id"):
                mine.append(result["id"])
                told["remembered"].append(result["id"])
                if sensitive:
                    told["sensitive"].append(result["id"])
        elif roll < 0.80:
            # The same fact saved by every client: exactly one record should result.
            timed("remember_same", "hg_memory", {
                "action": "remember", "text": f"Fictional shared observation {step % 5} about {TOPICS[step % 16]}."})
        elif roll < 0.84:
            # Re-import a hot memory as private while others forget or correct it.
            target = rng.choice(hot)
            text = _hot_text(harness, target)
            if text:
                timed("remember_private_copy", "hg_memory", {"action": "remember", "text": text, "sensitive": True})
        elif roll < 0.90:
            target = rng.choice(hot) if rng.random() < 0.5 or not mine else rng.choice(mine)
            result = timed("replace", "hg_memory", {
                "action": "replace", "memory_id": target, "include_sensitive": True,
                "text": f"Fictional correction by client {client} at step {step}."})
            if result and result.get("replaced"):
                new_id = result["memory"]["id"]
                told["corrections"].append([target, new_id])
                told["hidden"].append([target, time.time()])
                if target in mine:
                    mine[mine.index(target)] = new_id
                    if target in told["sensitive"]:
                        told["sensitive"].append(new_id)
        else:
            target = rng.choice(hot) if rng.random() < 0.5 or not mine else mine.pop(rng.randrange(len(mine)))
            result = timed("forget", "hg_memory", {"action": "forget", "memory_id": target,
                                                    "include_sensitive": True})
            if result and result.get("deleted"):
                told["forgotten"].append(target)
                told["hidden"].append([target, time.time()])
    return {"timings": timings, "outcomes": outcomes, "told": told}


def _represented(page: dict) -> set[str]:
    """Citations a Find page delivered, as bodies or as exact references."""
    return {item["citation"] for key in ("relevant_memories", "supporting_passages", "omitted_references")
            for item in page.get(key, []) if item.get("citation")}


def _hidden_delivered(results: list[dict]) -> int:
    """Continuation deliveries of memories already acknowledged as forgotten or corrected.

    An acknowledgement means the change had committed, so a page asked for
    afterwards reads a snapshot that includes it. Any client's acknowledgement
    counts, across threads and processes alike (one system clock).
    """
    hidden: dict[str, float] = {}
    for result in results:
        for identity, at in result["told"]["hidden"]:
            hidden[identity] = min(at, hidden.get(identity, at))
    return sum(1 for result in results for asked, identities in result["told"]["continued"]
               for identity in identities if identity in hidden and hidden[identity] < asked)


def _hot_text(harness, memory_id: str) -> str | None:
    record = harness.store.get(memory_id)
    return record.text if record is not None else None


def _index_drift(harness) -> int | None:
    """Records whose presence in this client's in-memory index is wrong.

    Measured once every client has finished, after one more search: a store
    that can notice other writers gets its chance to catch up first.
    """
    store = harness.store
    backend = getattr(store, "_backend", None)
    ids = getattr(backend, "_ids", None)
    if ids is None:
        return None
    store.vector_search("fictional sample", top_k=1)  # lets a store catch up, if it can
    live = {row[0] for row in store._sqlite.execute("SELECT id FROM memories WHERE deleted_at IS NULL")}
    return len(live.symmetric_difference(set(backend._ids)))


def _process_main(directory: str, client: int, operations: int, hot: list[str], seed: int,
                  embed_ms: float, journal: str, ready, go, done, settle, output) -> None:
    harness = open_harness(Path(directory), embed_ms, journal)
    try:
        ready.put(client)
        go.wait()
        result = run_client(harness, client, operations, hot, seed)
        done.put(time.perf_counter())
        settle.wait()
        result["index"] = _index_drift(harness)
        output.put(result)
    finally:
        harness.loop.close()


def _log_bytes(directory: Path) -> int:
    log = directory / "library.db-wal"
    return log.stat().st_size if log.exists() else 0


def run_mode(mode: str, directory: Path, clients: int, operations: int, hot: list[str],
             seed: int, embed_ms: float, journal: str = "auto") -> tuple[list[dict], float]:
    """Run every client, and note what waits in the log while they are idle but connected."""
    if mode == "threads":
        harness = open_harness(directory, embed_ms, journal)
        results: list[dict] = []
        barrier = threading.Barrier(clients + 1)

        def work(client: int) -> None:
            barrier.wait()
            results.append(run_client(harness, client, operations, hot, seed))

        threads = [threading.Thread(target=work, args=(client,)) for client in range(clients)]
        for thread in threads:
            thread.start()
        barrier.wait()
        started = time.perf_counter()
        for thread in threads:
            thread.join()
        elapsed = time.perf_counter() - started
        results[0]["log_bytes_while_idle"] = _log_bytes(directory)
        results[0]["index"] = _index_drift(harness)
        harness.loop.close()
        return results, elapsed
    context = multiprocessing.get_context("spawn")
    ready, done, output = context.Queue(), context.Queue(), context.Queue()
    go, settle = context.Event(), context.Event()
    workers = [context.Process(target=_process_main, args=(
        str(directory), client, operations, hot, seed, embed_ms, journal, ready, go, done, settle, output))
        for client in range(clients)]
    for worker in workers:
        worker.start()
    for _ in workers:
        ready.get(timeout=300)
    started = time.perf_counter()
    go.set()
    for _ in workers:
        done.get(timeout=3600)
    elapsed = time.perf_counter() - started
    idle_log = _log_bytes(directory)
    settle.set()
    results = [output.get(timeout=300) for _ in workers]
    results[0]["log_bytes_while_idle"] = idle_log
    for worker in workers:
        worker.join(60)
    return results, elapsed


def check_integrity(directory: Path, results: list[dict]) -> dict:
    """Inspect the finished library for outcomes no interleaving may produce."""
    database = directory / "library.db"
    # Before anything opens it again: what was left beside the main file, and
    # what a copy of the main file alone contains.
    log_bytes = _log_bytes(directory)
    with tempfile.TemporaryDirectory(prefix="hg-benchmark-copy-") as scratch:
        shutil.copyfile(database, Path(scratch) / "library.db")
        with contextlib.closing(sqlite3.connect(Path(scratch) / "library.db")) as copy:
            copied = {row[0] for row in copy.execute("SELECT id FROM memories")}
    connection = sqlite3.connect(database)
    rows = connection.execute("SELECT id, text, type, metadata, deleted_at FROM memories").fetchall()
    connection.close()
    records = {row[0]: {"text": row[1], "type": row[2], "meta": json.loads(row[3] or "{}"),
                        "deleted": row[4] is not None} for row in rows}
    active = {key: value for key, value in records.items()
              if not value["deleted"] and not value["meta"].get("superseded_by")}
    forgotten = {identity for result in results for identity in result["told"]["forgotten"]}
    remembered = {identity for result in results for identity in result["told"]["remembered"]}
    sensitive = {identity for result in results for identity in result["told"]["sensitive"]}
    successors: dict[str, set[str]] = {}
    for identity, record in records.items():
        if isinstance(record["meta"].get("supersedes"), str):
            successors.setdefault(record["meta"]["supersedes"], set()).add(identity)
    texts: dict[tuple, int] = {}
    for record in active.values():
        key = (record["text"], record["type"])
        texts[key] = texts.get(key, 0) + 1
    # A correction of a sensitive record must itself be sensitive.
    chain_sensitive = set(sensitive)
    changed = True
    while changed:
        changed = False
        for old, news in successors.items():
            if old in chain_sensitive and not news <= chain_sensitive:
                chain_sensitive |= news
                changed = True
    drift = [result["index"] for result in results if result.get("index") is not None]
    return {
        "resurrected": sum(1 for identity in forgotten if identity in records and not records[identity]["deleted"]),
        "forked_history": sum(1 for news in successors.values() if len(news) > 1),
        "duplicates": sum(count - 1 for count in texts.values() if count > 1),
        "lost_writes": sum(1 for identity in remembered if identity not in records),
        "loosened": sum(1 for identity in chain_sensitive
                        if identity in records and not records[identity]["meta"].get("sensitive")),
        "index_drift": sum(drift) if drift else None,
        "repeated": sum(result["told"]["repeated"] for result in results),
        "hidden_delivered": _hidden_delivered(results),
        "log_bytes_while_idle": sum(result.get("log_bytes_while_idle", 0) for result in results),
        "log_bytes_at_rest": log_bytes, "main_file_missing": len(set(records) - copied),
        "records": len(records), "active": len(active),
    }


def summarize(results: list[dict], elapsed: float) -> dict:
    timings: dict[str, list[float]] = {}
    outcomes: dict[str, int] = {}
    for result in results:
        for name, values in result["timings"].items():
            timings.setdefault(name, []).extend(values)
        for name, count in result["outcomes"].items():
            outcomes[name] = outcomes.get(name, 0) + count
    total = sum(len(values) for values in timings.values())

    def spread(values: list[float]) -> dict:
        ordered = sorted(values)
        return {"count": len(ordered),
                "p50_ms": round(1000 * statistics.median(ordered), 1),
                "p95_ms": round(1000 * ordered[min(len(ordered) - 1, int(len(ordered) * 0.95))], 1),
                "max_ms": round(1000 * ordered[-1], 1)}

    failures = {name: count for name, count in outcomes.items()
                if not name.endswith((":ok", ":conflict_reported", ":refused"))}
    return {"operations": total, "seconds": round(elapsed, 2),
            "per_second": round(total / elapsed, 1) if elapsed else None,
            "latency": {name: spread(values) for name, values in sorted(timings.items())},
            "conflicts_reported": sum(count for name, count in outcomes.items() if name.endswith(":conflict_reported")),
            "failures": dict(sorted(failures.items()))}


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--mode", default="both", choices=("threads", "processes", "both"))
    parser.add_argument("--journal", default="auto",
                        help="auto (what h/g chooses here), or comma-separated modes to pin: delete,wal")
    parser.add_argument("--clients", type=int, default=4)
    parser.add_argument("--operations", type=int, default=150, help="operations per client")
    parser.add_argument("--memories", type=int, default=1000)
    parser.add_argument("--documents", type=int, default=120)
    parser.add_argument("--embed-ms", type=float, default=0.0,
                        help="simulated model time per call, inside the real neural embedder and its "
                             "text cache; 0 uses plain hashing")
    parser.add_argument("--seed", type=int, default=7)
    parser.add_argument("--json", action="store_true", help="print machine-readable results only")
    args = parser.parse_args()
    if not 1 <= args.clients <= 32 or not 1 <= args.operations <= 100_000:
        parser.error("clients must be 1-32 and operations 1-100000")
    modes = ["threads", "processes"] if args.mode == "both" else [args.mode]
    journals = [value.strip().lower() for value in args.journal.split(",") if value.strip()]
    if any(value not in {"auto", "delete", "wal"} for value in journals):
        parser.error("journal must be auto, delete or wal, separated by commas")
    import hunter_gatherer

    report = {"parameters": {key: getattr(args, key) for key in
                             ("clients", "operations", "memories", "documents", "embed_ms", "seed")},
              "code": str(Path(hunter_gatherer.__file__).resolve().parent),
              "python": sys.version.split()[0], "sqlite": sqlite3.sqlite_version,
              "load_average_at_start": [round(value, 1) for value in os.getloadavg()],
              "runs": []}
    for journal in journals:
        for mode in modes:
            with tempfile.TemporaryDirectory(prefix="hg-benchmark-") as temporary:
                # Library paths must not pass through a symlink (macOS temp dirs do).
                directory = Path(temporary).resolve() / "library"
                hot = build_library(directory, args.memories, args.documents, journal, args.seed)
                results, elapsed = run_mode(mode, directory, args.clients, args.operations, hot,
                                            args.seed, args.embed_ms, journal)
                with open(directory / "library.db", "rb") as stream:
                    in_effect = "wal" if stream.read(20)[18:19] == b"\x02" else "delete"
                run = {"mode": mode, "journal": journal, "journal_in_effect": in_effect,
                       **summarize(results, elapsed), "integrity": check_integrity(directory, results)}
                report["runs"].append(run)
                if not args.json:
                    _print_run(run)
    if args.json:
        print(json.dumps(report, indent=2))
    violations = sum(value for run in report["runs"] for key, value in run["integrity"].items()
                     if key not in {"records", "active"} and value)
    return 1 if violations else 0


def _print_run(run: dict) -> None:
    print(f"\n{run['mode']} / journal={run['journal']} (in effect: {run['journal_in_effect']}): "
          f"{run['operations']} operations in "
          f"{run['seconds']}s ({run['per_second']}/s), {run['conflicts_reported']} conflicts reported")
    for name, values in run["latency"].items():
        print(f"  {name:<22} n={values['count']:<5} p50={values['p50_ms']:>8}ms "
              f"p95={values['p95_ms']:>8}ms max={values['max_ms']:>8}ms")
    print("  failures:", run["failures"] or "none")
    print("  integrity:", run["integrity"])


if __name__ == "__main__":
    raise SystemExit(main())
