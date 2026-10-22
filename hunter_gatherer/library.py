"""Explicit, isolated personal libraries for the quote-first application.

The directory manifest is configuration, not environment discovery. Opening a
library never reads .env, old application configuration or cloud credentials.
Backups use the existing verified SQLite/originals bundle and relocate original
paths on restore; the small library manifest only contains portable names.
"""
from __future__ import annotations

import contextlib
import json
import os
import shlex
import sqlite3
import stat
import subprocess
import sys
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from hunter_gatherer.config import Settings

if TYPE_CHECKING:
    from hunter_gatherer.memory.embeddings import Embedder
    from hunter_gatherer.memory.store import MemoryStore

MANIFEST = "hunter-gatherer-library.json"
LIBRARY_SCHEMA = 1
SEMANTIC_MODEL = "BAAI/bge-small-en-v1.5"
_EMBEDDINGS = {"keyword": "hashing:384", "semantic": f"st:{SEMANTIC_MODEL}"}


def _safe_path(value: str | Path) -> Path:
    if not value or not str(value).strip():
        raise ValueError("An explicit library directory is required")
    path = Path(os.path.abspath(Path(value).expanduser()))
    for item in (path, *path.parents):
        try:
            info = item.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or getattr(info, "st_file_attributes", 0) & 0x400:
            raise ValueError("Library paths must not contain symlinks or reparse points")
    return path


def _empty_target(value: str | Path) -> Path:
    target = _safe_path(value)
    if target.exists() and (not target.is_dir() or any(target.iterdir())):
        raise ValueError("Library target must be absent or an empty directory; existing data is never replaced")
    return target


def _settings(target: Path, mode: str) -> Settings:
    return Settings.model_construct(
        sqlite_path=str(target / "library.db"),
        vector_backend="memory", embed_model="hashing" if mode == "keyword" else SEMANTIC_MODEL,
        embed_local_files_only=True, embedder_mismatch="error", local_only=True,
        openai_api_key="", anthropic_api_key="", messages_api_key="",
        qdrant_url="", qdrant_api_key="", library_maintenance_enabled=False,
        recall_reranker_enabled=False, library_find_relevance_policy="coverage",
    )


def _manifest(mode: str, identity: str) -> dict:
    return {
        "schema_version": LIBRARY_SCHEMA, "application": "hunter-gatherer",
        "library_id": identity, "database": "library.db",
        "search_mode": mode, "embedding": _EMBEDDINGS[mode],
    }


def _write_manifest(target: Path, manifest: dict) -> None:
    with (target / MANIFEST).open("x", encoding="utf-8") as stream:
        json.dump(manifest, stream, indent=2)
        stream.write("\n")
    os.chmod(target / MANIFEST, 0o600)


def _database_metadata(database: Path) -> dict[str, str]:
    # The path was checked before opening. Read-only mode cannot create a
    # missing database or migrate an unrelated database selected by mistake.
    if not database.is_file():
        raise ValueError("Not an initialized h/g library database")
    try:
        with contextlib.closing(sqlite3.connect(database.as_uri() + "?mode=ro", uri=True)) as connection:
            connection.execute("PRAGMA trusted_schema=OFF")
            metadata = dict(connection.execute("SELECT key,value FROM store_meta"))
            columns = [row[1] for row in connection.execute("PRAGMA table_info(memories)")]
            if columns[:3] != ["id", "text", "type"]:
                raise ValueError("Invalid library database schema")
            return metadata
    except sqlite3.DatabaseError as exc:
        raise ValueError("Not an initialized h/g library database") from exc


def library_settings(directory: str | Path) -> Settings:
    """Validate an existing directory without initializing a model or store."""
    target = _safe_path(directory)
    path = _safe_path(target / MANIFEST)
    try:
        if not path.is_file() or path.stat().st_size > 16_384:
            raise ValueError("Missing or invalid library manifest")
        manifest = json.loads(path.read_text(encoding="utf-8"))
        if (not isinstance(manifest, dict)
                or type(manifest.get("schema_version")) is not int
                or manifest.get("schema_version") != LIBRARY_SCHEMA
                or manifest.get("application") != "hunter-gatherer"
                or manifest.get("database") != "library.db"
                or manifest.get("search_mode") not in _EMBEDDINGS
                or manifest.get("embedding") != _EMBEDDINGS[manifest["search_mode"]]
                or str(uuid.UUID(manifest.get("library_id", ""))) != manifest.get("library_id")):
            raise ValueError("Invalid h/g library manifest")
    except (OSError, ValueError, TypeError, AttributeError) as exc:
        raise ValueError("Not an initialized h/g library directory; create one with hunt init DIR") from exc
    database = _safe_path(target / "library.db")
    for suffix in ("-wal", "-shm", "-journal"):
        _safe_path(Path(str(database) + suffix))
    archive = _safe_path(Path(str(database) + ".archive"))
    # Refuse pre-existing redirected managed originals before an import or
    # source-open operation can traverse them. An idle library owner can still
    # mutate their files: this is not isolation from a hostile local OS user.
    if archive.exists():
        if not archive.is_dir():
            raise ValueError("Invalid managed archive directory")
        for base, dirs, names in os.walk(archive, followlinks=False):
            for name in dirs + names:
                _safe_path(Path(base) / name)
    metadata = _database_metadata(database)
    if (metadata.get("library_id") != manifest["library_id"]
            or metadata.get("embedder_fingerprint") != manifest["embedding"]):
        raise ValueError("Library manifest does not match its database identity or embedding model")
    return _settings(target, manifest["search_mode"])


def create_library(directory: str | Path, *, semantic: bool = False,
                   download_model: bool = False, from_backup: str | Path | None = None) -> dict:
    """Create a fresh library, or restore a verified backup into a fresh one.

    Keyword search works offline with the base installation. Semantic creation
    validates the explicitly selected model before creating the database.
    Network model downloads occur only with the explicit download_model flag.
    Partial filesystem failures leave files for inspection; retries never erase
    the partial directory or replace user data.
    """
    if type(semantic) is not bool or type(download_model) is not bool:
        raise ValueError("Semantic and download options must be booleans")
    if download_model and not semantic:
        raise ValueError("--download-model requires --semantic")
    if from_backup is not None and (semantic or download_model):
        raise ValueError("A restored library keeps its original search mode; do not combine --from-backup and --semantic")
    target = _empty_target(directory)
    if from_backup is not None:
        from hunter_gatherer.backup import restore_backup, verify_backup

        report = verify_backup(from_backup)
        fingerprint = report["embedder_fingerprint"]
        mode = next((mode for mode, expected in _EMBEDDINGS.items() if expected == fingerprint), None)
        if mode is None:
            raise ValueError("Backup uses an unsupported embedding model; restore it with explicit advanced configuration")
        target = _empty_target(directory)
        target.mkdir(parents=True, exist_ok=True, mode=0o700)
        restore_backup(from_backup, target / "library.db")
        with contextlib.closing(sqlite3.connect(target / "library.db")) as connection:
            connection.execute("PRAGMA trusted_schema=OFF")
            row = connection.execute("SELECT value FROM store_meta WHERE key='library_id'").fetchone()
            identity = row[0] if row else str(uuid.uuid4())
            try:
                if str(uuid.UUID(identity)) != identity:
                    raise ValueError
            except (ValueError, AttributeError) as exc:
                raise ValueError("Restored library has an invalid identity") from exc
            if row is None:
                connection.execute("INSERT INTO store_meta VALUES ('library_id',?)", (identity,))
                connection.commit()
        result = _manifest(mode, identity)
        _write_manifest(target, result)
        return result

    from hunter_gatherer.memory.documents import DocumentArchive
    from hunter_gatherer.memory.embeddings import get_embedder
    from hunter_gatherer.memory.store import MemoryStore

    mode = "semantic" if semantic else "keyword"
    settings = _settings(target, mode)
    startup_settings = settings.model_copy(update={"embed_local_files_only": not download_model})
    embedder = get_embedder(startup_settings)
    if semantic:
        try:
            embedder.embed("Initialize local semantic search")
        except OSError as exc:
            raise ValueError("Semantic model is not available locally. Install the semantic extra and explicitly use --semantic --download-model to download it.") from exc
    target = _empty_target(directory)
    target.mkdir(parents=True, exist_ok=True, mode=0o700)
    # Reserve the database exclusively, so a concurrent creator cannot have
    # its existing file opened or migrated by this invocation.
    descriptor = os.open(target / "library.db", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    os.close(descriptor)
    identity = str(uuid.uuid4())
    with MemoryStore(settings, embedder=embedder) as store:
        store.set_meta("library_id", identity)
        DocumentArchive(store)
    result = _manifest(mode, identity)
    _write_manifest(target, result)
    return result


def import_files(directory: str | Path, paths, *, recursive=False, sensitive=False,
                 max_files=1000, max_bytes=1024**3) -> dict:
    """Import explicitly selected files using the shared bounded import jobs."""
    from hunter_gatherer.memory.import_jobs import ImportJobs
    from hunter_gatherer.memory.store import MemoryStore

    settings = library_settings(directory)
    # Plan before opening the writable store; invalid inputs do not start a job.
    preview = ImportJobs.preview(paths, recursive=recursive, max_files=max_files, max_bytes=max_bytes)
    if not preview["items"]:
        raise ValueError("No supported files selected (.txt, .md, .pdf, .docx, .pptx)")
    with MemoryStore(settings) as store:
        jobs = ImportJobs(store)
        result = jobs.create(paths, recursive=recursive, sensitive=sensitive,
                             include_sensitive=sensitive, max_files=max_files, max_bytes=max_bytes)
        while result["counts"].get("pending", 0):
            result = jobs.run(result["job_id"], batch_size=100, include_sensitive=sensitive)
        return result


@dataclass
class LibraryRuntime:
    """Resources owned by the library server, without an agent executor."""

    store: MemoryStore
    embedder: Embedder

    def close(self):
        self.store.close()


def build_library_harness(settings: Settings):
    """Create one warm library store without loading agent skills or clients.

    The six library tools operate on attributed records and exact sources. They
    do not need the inherited agent router, shell/file tools, sessions or skills.
    Explicit full-profile/agent launchers retain their separate Harness.build.
    """
    from hunter_gatherer.cost import CostTracker
    from hunter_gatherer.mcp.tools import Harness
    from hunter_gatherer.memory.embeddings import get_embedder
    from hunter_gatherer.memory.store import MemoryStore

    embedder = get_embedder(settings)
    if settings.embed_model != "hashing":
        # Load before opening the writable store: missing cached model files
        # cannot leave a partially migrated database, and readiness means warm.
        embedder.embed("Initialize local semantic search")
    store = MemoryStore(settings, embedder=embedder)
    return Harness(loop=LibraryRuntime(store=store, embedder=embedder), store=store,
                   cost=CostTracker(settings=settings), settings=settings)


def cmd_library_init(args, _settings=None):
    with contextlib.redirect_stdout(sys.stderr):
        manifest = create_library(args.directory, semantic=args.semantic,
                                  download_model=args.download_model, from_backup=args.from_backup)
    if args.json:
        print(json.dumps(manifest, indent=2))
    else:
        from hunter_gatherer.terminal import safe_text
        mode = "Semantic search (local model)" if manifest["search_mode"] == "semantic" else "Keyword search (no model download)"
        print(f"Library ready: {safe_text(str(_safe_path(args.directory)))}\n{mode}.")
        quoted = subprocess.list2cmdline([str(args.directory)]) if os.name == "nt" else shlex.quote(str(args.directory))
        print(f"Open the library UI: hunt serve {safe_text(quoted)}")
        print(f"Import files: hunt import --library {safe_text(quoted)} FILE")


def cmd_library_serve(args, _settings=None):
    import uvicorn

    from hunter_gatherer.mcp.server import _http_options, build_server
    from hunter_gatherer.server.library_app import build_library_app

    settings = library_settings(args.directory)
    harness = build_library_harness(settings)
    try:
        server, _ = build_server(harness=harness, tool_profile="library")
        app = build_library_app(harness, server, http_options=_http_options("127.0.0.1", args.port))
        print(f"hunter/gatherer: http://127.0.0.1:{args.port} · MCP: http://127.0.0.1:{args.port}/mcp/library", file=sys.stderr)
        uvicorn.run(app, host="127.0.0.1", port=args.port, proxy_headers=False)
    finally:
        harness.loop.close()


def cmd_library_import(args, _settings=None):
    with contextlib.redirect_stdout(sys.stderr):
        result = import_files(args.library, args.paths, recursive=args.recursive, sensitive=args.sensitive,
                              max_files=args.max_files, max_bytes=args.max_bytes)
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        counts = result["counts"]
        succeeded, failed = counts.get("succeeded", 0), counts.get("failed", 0)
        print(f"Imported {succeeded} file{'s' if succeeded != 1 else ''}." + (f" {failed} failed; use --json for details." if failed else ""))
        if not failed:
            from hunter_gatherer.terminal import safe_text
            quoted = subprocess.list2cmdline([str(args.library)]) if os.name == "nt" else shlex.quote(str(args.library))
            print(f'Ask: hunt ask "Your question" --library {safe_text(quoted)}')
    return 1 if result["counts"].get("failed", 0) or result["counts"].get("cancelled", 0) else 0
