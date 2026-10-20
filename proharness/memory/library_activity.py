"""Ephemeral activity from actual tool calls, with current privacy at delivery.

Only operation names, opaque record identifiers, and validated traversal links
are retained. No prompts, quotes, paths, database writes, or background work.
"""
from __future__ import annotations

import hashlib
import re
import threading
import time
import uuid
from collections import deque
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from proharness.memory.evidence import _source_refs, is_sensitive_memory
from proharness.memory.library_health import _PRIVATE, _read_snapshot
from proharness.memory.schemas import Memory

MAX_EVENTS = 256
RETENTION_SECONDS = 120
MAX_POLL_EVENTS = 64
MAX_RECORDS = 64
_REGISTRY_LOCK = threading.Lock()
_IDENTITY = re.compile(r"[\w-]{1,128}\Z")


def _id(kind, value):
    return f"{kind}:{value}" if isinstance(value, str) and _IDENTITY.fullmatch(value) else None


@dataclass
class _Operation:
    id: str
    name: str
    origin: str
    private: bool = False
    guards: tuple[str, ...] = ()
    before: tuple | None = None


@dataclass
class _Event:
    seq: int
    operation: _Operation
    phase: str
    timestamp: float
    record_ids: tuple[str, ...] = ()
    # Internal link validation retains a chunk digest, never its source text.
    links: tuple[tuple[str, str, str, str], ...] = ()
    changed: bool = False


class ActivityRing:
    def __init__(self, *, clock=None):
        self.epoch = uuid.uuid4().hex
        self.cursor = 0
        self.events = deque(maxlen=MAX_EVENTS)
        self._lock = threading.RLock()
        self._clock = clock or time.time

    def _append(self, operation, phase, *, record_ids=(), links=(), changed=False):
        with self._lock:
            now = self._clock()
            self._expire(now)
            self.cursor += 1
            self.events.append(_Event(self.cursor, operation, phase, now,
                                      tuple(record_ids)[:MAX_RECORDS], tuple(links)[:MAX_RECORDS], changed))

    def _expire(self, now):
        while self.events and self.events[0].timestamp < now - RETENTION_SECONDS:
            self.events.popleft()

    def start(self, name, origin, *, private=False, before=None):
        operation = _Operation(uuid.uuid4().hex, name, origin, private=private, before=before)
        self._append(operation, "start")
        return operation

    def finish(self, operation, *, error=False, record_ids=(), links=(), changed=False, private=False):
        with self._lock:
            operation.private = operation.private or private
            operation.guards = tuple(record_ids)[:MAX_RECORDS]
            self._append(operation, "error" if error else "complete", record_ids=record_ids,
                         links=links, changed=bool(changed and not error))

    def read(self, after):
        with self._lock:
            self._expire(self._clock())
            if after is None or after > self.cursor:
                return self.epoch, self.cursor, []
            candidates = [event for event in self.events if event.seq > after][:MAX_POLL_EVENTS]
            # Advance over suppressed events too. A viewer must never repeatedly
            # receive a hidden event or infer it from a stuck cursor.
            cursor = candidates[-1].seq if candidates else self.cursor
            return self.epoch, cursor, list(candidates)


def activity_ring(harness):
    with _REGISTRY_LOCK:
        ring = getattr(harness, "_library_activity", None)
        if ring is None:
            ring = ActivityRing()
            harness._library_activity = ring
        return ring


class _Visibility:
    """Small request-local caches, using only existing catalog tables."""
    def __init__(self, store):
        self.store = store
        self.records = {}
        self.chunks = {}
        self.memories = {}
        self.documents = {}
        self.private_documents = None
        self.tables = {row[0] for row in store._sqlite.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name IN "
            "('memories','archive_documents','archive_chunks','import_candidates')")}

    def status(self, identity):
        if identity in self.records:
            return self.records[identity]
        kind, value = identity.split(":", 1)
        private, visible = False, False
        if kind == "memory" and "memories" in self.tables:
            row = self.store._sqlite.execute("SELECT * FROM memories WHERE id=?", (value,)).fetchone()
            if row:
                memory = Memory.from_sql_row(tuple(row))
                self.memories[identity] = memory
                private = is_sensitive_memory(memory, self.store)
                # Also honor a tightened current version of a historical source.
                for ref in _source_refs(memory):
                    document = _id("document", ref.get("document_id"))
                    chunk = self.chunk(ref.get("chunk_id"))
                    parents = {document, _id("document", chunk[0]) if chunk else None} - {None}
                    private = private or any(self.status(parent)[0] for parent in parents)
                visible = memory.is_retrievable()
        elif kind == "document" and "archive_documents" in self.tables:
            row = self.store._sqlite.execute("SELECT * FROM archive_documents WHERE id=?", (value,)).fetchone()
            if row:
                self.documents[identity] = row
                if self.private_documents is None:
                    # Old copies and version chains can inherit privacy across
                    # several identities/hashes, even with incomplete old flags.
                    self.private_documents = {item[0] for item in self.store._sqlite.execute(_PRIVATE + "SELECT id FROM private")}
                private = value in self.private_documents
                visible = row["deleted_at"] is None and not self.store._sqlite.execute(
                    "SELECT 1 FROM archive_documents WHERE source_identity=? AND version>? LIMIT 1",
                    (row["source_identity"], row["version"])).fetchone()
        self.records[identity] = (bool(private), bool(visible))
        return self.records[identity]

    def chunk(self, identity):
        if not isinstance(identity, str) or "archive_chunks" not in self.tables:
            return None
        if identity not in self.chunks:
            row = self.store._sqlite.execute("SELECT document_id,text FROM archive_chunks WHERE id=?",
                                            (identity,)).fetchone()
            self.chunks[identity] = tuple(row) if row else None
        return self.chunks[identity]


def _private_call(harness, arguments, operation=None):
    private = arguments.get("include_sensitive") is True or arguments.get("sensitive") is True
    before = None
    with _read_snapshot(harness.store):
        reader = _Visibility(harness.store)
        for kind, key in (("memory", "memory_id"), ("document", "document_id")):
            identity = _id(kind, arguments.get(key))
            if identity:
                private = private or reader.status(identity)[0]
                if kind == "memory" and identity in reader.memories:
                    memory = reader.memories[identity]
                    before = (memory.is_retrievable(), bool(memory.metadata.get("core")), memory.is_permanent)
        candidate_id = arguments.get("candidate_id")
        if isinstance(candidate_id, str) and "import_candidates" in reader.tables:
            row = harness.store._sqlite.execute("SELECT document_id FROM import_candidates WHERE id=?",
                                                (candidate_id,)).fetchone()
            if row:
                private = private or reader.status("document:" + row[0])[0]
        chunk = reader.chunk(arguments.get("chunk_id"))
        if chunk:
            private = private or reader.status("document:" + chunk[0])[0]
        if operation == "ingest_document" and isinstance(arguments.get("path"), str) and "archive_documents" in reader.tables:
            # Import deduplication can still tighten existing source privacy.
            # Retain only an opaque ID and previous flag, never the supplied path.
            source_identity = hashlib.sha256(str(Path(arguments["path"]).expanduser().resolve()).encode()).hexdigest()
            row = harness.store._sqlite.execute(
                "SELECT id,sensitive FROM archive_documents WHERE source_identity=? ORDER BY version DESC LIMIT 1",
                (source_identity,)).fetchone()
            if row:
                before = ("document", row["id"], bool(row["sensitive"]))
                private = private or reader.status("document:" + row["id"])[0]
        # Catch labeled or legacy sensitive creations before announcing a start.
        if isinstance(arguments.get("text"), str):
            candidate = Memory(text=arguments["text"], tags=arguments.get("tags") or [],
                               metadata=arguments.get("metadata") or {})
            private = private or is_sensitive_memory(candidate, harness.store)
    return private, before


def _extract(harness, operation, result):
    """Read actual result shapes; never treat caller arguments as success IDs."""
    ids, references = set(), []
    name = operation.name

    def add(kind, value):
        identity = _id(kind, value)
        if identity and len(ids) < MAX_RECORDS:
            ids.add(identity)
        return identity

    def memory(record, key="id", refs=None):
        if not isinstance(record, dict):
            return
        identity = add("memory", record.get(key))
        source_refs = refs if refs is not None else record.get("metadata", {}).get("evidence_refs", [])
        if identity and isinstance(source_refs, list):
            references.extend((identity, ref) for ref in source_refs[:MAX_RECORDS] if isinstance(ref, dict))

    if name == "board":
        for identity in result.get("changed_record_ids", [])[:MAX_RECORDS]:
            if isinstance(identity, str) and ":" in identity:
                kind, value = identity.split(":", 1)
                if kind in {"memory", "document"}:
                    add(kind, value)
    elif name in {"recall", "search"}:
        for record in result.get("hits", [])[:MAX_RECORDS]:
            memory(record, refs=[] if name == "search" else None)
    elif name == "profile":
        for record in result.get("profile", [])[:MAX_RECORDS]:
            memory(record)
        for record in result.get("core", {}).get("memories", [])[:MAX_RECORDS]:
            memory(record, refs=[])
    elif name == "core_memory":
        for record in result.get("memories", [])[:MAX_RECORDS]:
            memory(record, refs=[])
    elif name in {"get_memory", "replace_memory"}:
        if result.get("found") is True or result.get("replaced") is True:
            memory(result.get("memory"))
    elif name in {"remember", "set_core_memory"}:
        if name == "set_core_memory" or result.get("stored") is True:
            add("memory", result.get("id"))
    elif name == "forget" and result.get("deleted") is True:
        add("memory", result.get("memory_id"))
    elif name in {"evidence", "find"}:
        for record in result.get("relevant_memories", [])[:MAX_RECORDS]:
            memory(record, key="memory_id", refs=record.get("provenance", {}).get("sources", []))
        for passage in result.get("supporting_passages", [])[:MAX_RECORDS]:
            add("document", passage.get("document_id"))
    elif name == "fetch":
        # Only bodies actually included within the output budget were read.
        for source in result.get("sources", [])[:MAX_RECORDS]:
            if source.get("kind") == "memory":
                memory(source, refs=[])
            elif source.get("kind") == "document_chunk":
                add("document", source.get("document_id"))
    elif name == "context_packet":
        # Animate only evidence actually delivered within the packet budget.
        # A source link needs both the returned memory reference and exact
        # returned passage; omitted candidates cannot light up the graph.
        units = result.get("units", [])[:MAX_RECORDS]
        passages = {unit.get("id"): unit.get("content", {}) for unit in units
                    if isinstance(unit, dict) and unit.get("kind") == "passage"}
        for unit in units:
            if not isinstance(unit, dict):
                continue
            content = unit.get("content", {})
            if unit.get("kind") == "memory":
                refs = [passages[ref["chunk_id"]] for ref in content.get("source_refs", [])
                        if isinstance(ref, dict) and ref.get("chunk_id") in passages
                        and ref.get("document_id") == passages[ref["chunk_id"]].get("document_id")]
                memory(content, key="memory_id", refs=refs)
            elif unit.get("kind") == "passage":
                add("document", content.get("document_id"))
    elif name == "import_documents" and result.get("action") == "run":
        # Only the current bounded batch performed document work. Manifest
        # status rows (including old successful batches) are not fresh reads.
        for item in result.get("processed_items", [])[:MAX_RECORDS]:
            if isinstance(item, dict):
                add("document", item.get("document_id"))
    elif name == "search_documents":
        for hit in result.get("hits", [])[:MAX_RECORDS]:
            add("document", hit.get("document_id"))
    elif name == "document_outline":
        if result.get("sections"):
            add("document", result.get("document_id"))
    elif name == "document_window":
        for hit in result.get("chunks", [])[:MAX_RECORDS]:
            add("document", hit.get("document_id"))
    elif name in {"ingest_document", "reextract_document", "get_document", "get_document_chunk", "forget_document",
                  "propose_import", "review_import_candidate", "get_import_candidate"}:
        add("document", result.get("document_id"))
        if name in {"review_import_candidate", "get_import_candidate"} and result.get("memory_id"):
            memory(result, key="memory_id", refs=result.get("evidence_refs", []))

    links = set()
    with _read_snapshot(harness.store):
        reader = _Visibility(harness.store)
        # remember returns only its actual stored ID. Its metadata refs were
        # validated by ingestion; inspect that returned row to animate precisely
        # those relationships, without treating submitted arguments as results.
        if name == "remember" and result.get("stored") is True:
            identity = _id("memory", result.get("id"))
            if identity:
                reader.status(identity)
                current = reader.memories.get(identity)
                if current is not None:
                    references.extend((identity, ref) for ref in _source_refs(current)[:MAX_RECORDS])
        for source, ref in references[:MAX_RECORDS]:
            target = _id("document", ref.get("document_id"))
            chunk_id = ref.get("chunk_id")
            if not target or ref.get("available") is False or not isinstance(chunk_id, str):
                continue
            chunk = reader.chunk(chunk_id)
            quote = ref.get("quote")
            if (not chunk or chunk[0] != ref.get("document_id") or
                    not isinstance(quote, str) or not quote or quote not in chunk[1] or
                    not reader.status(source)[1] or not reader.status(target)[1]):
                continue
            add("document", ref["document_id"])
            links.add((source, target, chunk_id, hashlib.sha256(chunk[1].encode()).hexdigest()))
        private = any(reader.status(identity)[0] for identity in ids)
        name = operation.name
        memory_id = _id("memory", result.get("id"))
        new_id = _id("memory", result.get("memory", {}).get("id"))
        document_id = _id("document", result.get("document_id"))
        changed = False
        if name == "board":
            changed = result.get("changed") is True
        elif name == "remember":
            changed = result.get("stored") is True and result.get("decision") == "ADD" and bool(
                memory_id and reader.status(memory_id)[1])
        elif name == "replace_memory":
            changed = result.get("replaced") is True and bool(new_id and reader.status(new_id)[1])
        elif name == "set_core_memory" and memory_id and operation.before:
            reader.status(memory_id)
            current = reader.memories.get(memory_id)
            changed = bool(current and operation.before[1:] !=
                           (bool(current.metadata.get("core")), current.is_permanent))
        elif name == "forget":
            target = _id("memory", result.get("memory_id"))
            if target:
                reader.status(target)
                current = reader.memories.get(target)
                changed = result.get("deleted") is True and current is not None and current.deleted_at is not None
                if operation.before is not None:
                    changed = changed and operation.before[0]
        elif name == "forget_document":
            changed = isinstance(result.get("forgotten_versions"), int) and result["forgotten_versions"] > 0
        elif name == "import_documents" and result.get("action") == "run":
            changed = any(
                item.get("created") is True and
                (identity := _id("document", item.get("document_id"))) in ids and
                reader.status(identity)[1]
                for item in result.get("processed_items", [])[:MAX_RECORDS]
                if isinstance(item, dict)
            )
        elif name in {"ingest_document", "reextract_document"}:
            changed = result.get("deduplicated") is False and bool(document_id and reader.status(document_id)[1])
            if (result.get("deduplicated") is True and document_id and operation.before and
                    operation.before[:2] == ("document", result.get("document_id"))):
                reader.status(document_id)
                current = reader.documents.get(document_id)
                changed = bool(current and bool(current["sensitive"]) != operation.before[2])
        elif name == "propose_import":
            changed = isinstance(result.get("created_count"), int) and result["created_count"] > 0
        elif name == "review_import_candidate":
            changed = result.get("idempotent") is False and result.get("status") in {"approved", "rejected"}
        elif name == "consolidate":
            changed = any(isinstance(result.get(key), int) and result[key] > 0
                          for key in ("created", "deleted", "deduped", "decayed"))
    return sorted(ids), sorted(links), changed, private


def observe_call(harness, operation, call, *, origin="agent", arguments=None):
    """Observer failure cannot change a result, exception, or invocation count."""
    ring = token = None
    with suppress(Exception):
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,63}", operation) or origin not in {"agent", "library"}:
            raise ValueError("Invalid activity operation")
        ring = activity_ring(harness)
        private, before = _private_call(harness, arguments or {}, operation)
        token = ring.start(operation, origin, private=private, before=before)
    try:
        result = call()
    except Exception:
        if token is not None:
            with suppress(Exception):
                ring.finish(token, error=True)
        raise
    if token is not None:
        try:
            failed = not isinstance(result, dict) or bool(result.get("error")) or result.get("found") is False
            if failed:
                ring.finish(token, error=True)
            else:
                ids, links, changed, private = _extract(harness, token, result)
                ring.finish(token, record_ids=ids, links=links, changed=changed, private=private)
        except Exception:
            # Close an already announced operation even when attribution could
            # not be inspected. This carries no result IDs or write assertion
            # and preserves the private classification captured before the call.
            with suppress(Exception):
                ring.finish(token)
    return result


def poll_activity(harness, *, after=None, include_sensitive=False):
    if type(include_sensitive) is not bool:
        raise TypeError("include_sensitive must be a boolean")
    if after is not None and (type(after) is not int or after < 0):
        raise ValueError("after must be a nonnegative integer")
    ring = activity_ring(harness)
    epoch, cursor, pending = ring.read(after)
    response = {"epoch": epoch, "cursor": cursor, "events": []}
    if not pending:
        return response
    with _read_snapshot(harness.store):
        reader = _Visibility(harness.store)
        for event in pending:
            operation = event.operation
            identities = set(operation.guards) | set(event.record_ids)
            if not include_sensitive and (operation.private or any(reader.status(value)[0] for value in identities)):
                continue
            visible = {value for value in event.record_ids if reader.status(value)[1]}
            links = []
            for source, target, chunk_id, digest in event.links:
                if source not in visible or target not in visible:
                    continue
                chunk = reader.chunk(chunk_id)
                if chunk and "document:" + chunk[0] == target and hashlib.sha256(chunk[1].encode()).hexdigest() == digest:
                    links.append({"source": source, "target": target})
            response["events"].append({"seq": event.seq, "operation_id": operation.id,
                                       "phase": event.phase, "operation": operation.name, "origin": operation.origin,
                                       "record_ids": sorted(visible), "links": links,
                                       "changed": event.changed, "timestamp": event.timestamp})
    return response
