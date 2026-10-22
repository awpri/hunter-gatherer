"""SQLite coordination state. Actor labels and workspaces are not authentication.

One transaction commits each mutation, receipt, immutable revision, and event.
Leases fence only this server's writes, never external actions. Reads revalidate
all evidence without initializing the document archive.
"""

from __future__ import annotations

import hashlib
import json
import secrets
import time
import uuid

_SCHEMA = """
CREATE TABLE IF NOT EXISTS coordination_objects (
 id TEXT PRIMARY KEY, workspace_id TEXT NOT NULL, kind TEXT NOT NULL,
 revision INTEGER NOT NULL, data TEXT NOT NULL);
CREATE INDEX IF NOT EXISTS coordination_scope ON coordination_objects(workspace_id,kind,id);
CREATE TABLE IF NOT EXISTS coordination_versions (
 object_id TEXT NOT NULL, revision INTEGER NOT NULL, workspace_id TEXT NOT NULL,
 kind TEXT NOT NULL, data TEXT NOT NULL, PRIMARY KEY(object_id,revision));
CREATE TABLE IF NOT EXISTS coordination_receipts (
 workspace_id TEXT NOT NULL, actor TEXT NOT NULL, request_key TEXT NOT NULL,
 fingerprint TEXT NOT NULL, response TEXT NOT NULL,
 PRIMARY KEY(workspace_id,actor,request_key));
CREATE TABLE IF NOT EXISTS coordination_events (
 sequence INTEGER PRIMARY KEY AUTOINCREMENT, workspace_id TEXT NOT NULL,
 kind TEXT NOT NULL, object_id TEXT NOT NULL, revision INTEGER NOT NULL,
 action TEXT NOT NULL, recorded_at REAL NOT NULL);
CREATE INDEX IF NOT EXISTS coordination_event_scope ON coordination_events(workspace_id,sequence);
"""
_MAX_BYTES = 32768
_MAX_NEW_PAGE_ORIGINALS = 4
_PAGE_PREFLIGHT_SECONDS = 30


class CoordinationError(ValueError):
    """A stable domain error with a concise machine-readable code."""

    def __init__(self, code, message):
        self.code = code
        super().__init__(f"{code}: {message}")


def _json(value):
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def _bounded(value, name="payload", size=_MAX_BYTES):
    try:
        encoded = _json(value)
    except (ValueError, TypeError) as exc:
        raise CoordinationError("invalid", f"{name} must be finite JSON") from exc
    if len(encoded.encode()) > size:
        raise CoordinationError("invalid", f"{name} exceeds {size} bytes")
    return json.loads(encoded)


def _text(value, name, maximum=4096, required=True):
    if not isinstance(value, str) or len(value) > maximum or (required and not value.strip()):
        raise CoordinationError(
            "invalid", f"{name} must be a nonempty string of at most {maximum} characters"
        )
    return value


def _limit(value):
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= 100:
        raise CoordinationError("invalid", "limit must be 1..100")
    return value


class CoordinationStore:
    def __init__(self, store, clock=None):
        self.store = store
        self.db = store._sqlite
        self.clock = clock or time.time
        with store._lock:
            self.db.executescript(_SCHEMA)
            self.db.commit()

    def _row(self, workspace_id, object_id, kind=None, revision=None):
        _text(workspace_id, "workspace_id", 128)
        _text(object_id, "object_id", 128)
        if revision is not None and (
            isinstance(revision, bool) or not isinstance(revision, int) or revision < 1
        ):
            raise CoordinationError("invalid", "revision must be a positive integer")
        if revision is None:
            row = self.db.execute(
                "SELECT * FROM coordination_objects WHERE id=? AND workspace_id=?",
                (object_id, workspace_id),
            ).fetchone()
        else:
            row = self.db.execute(
                "SELECT * FROM coordination_versions WHERE object_id=? AND revision=? AND workspace_id=?",
                (object_id, revision, workspace_id),
            ).fetchone()
        if not row or (kind and row["kind"] != kind):
            raise CoordinationError("not_found", "object unavailable in this workspace")
        return json.loads(row["data"])

    def _workspace(self, workspace_id):
        return self._row(workspace_id, workspace_id, "workspace")

    def _save(self, data, action, new=False):
        data["updated_at"] = self.clock()
        data["revision"] = 1 if new else data["revision"] + 1
        encoded = _json(_bounded(data, "coordination object", 65536))
        self.db.execute(
            "INSERT INTO coordination_objects VALUES (?,?,?,?,?) ON CONFLICT(id) DO UPDATE SET revision=excluded.revision,data=excluded.data",
            (data["id"], data["workspace_id"], data["kind"], data["revision"], encoded),
        )
        self.db.execute(
            "INSERT INTO coordination_versions VALUES (?,?,?,?,?)",
            (data["id"], data["revision"], data["workspace_id"], data["kind"], encoded),
        )
        self.db.execute(
            "INSERT INTO coordination_events(workspace_id,kind,object_id,revision,action,recorded_at) VALUES(?,?,?,?,?,?)",
            (
                data["workspace_id"],
                data["kind"],
                data["id"],
                data["revision"],
                action,
                self.clock(),
            ),
        )
        return data

    def _new(self, kind, workspace_id, actor, **fields):
        return {
            "id": uuid.uuid4().hex,
            "kind": kind,
            "workspace_id": workspace_id,
            "actor": actor,
            "created_at": self.clock(),
            "sensitive": False,
            "references": [],
            **fields,
        }

    def _expect(self, obj, revision):
        if (
            isinstance(revision, bool)
            or not isinstance(revision, int)
            or revision != obj["revision"]
        ):
            raise CoordinationError(
                "revision_conflict", "expected_revision does not match current revision"
            )

    def _table(self, name):
        return bool(
            self.db.execute(
                "SELECT 1 FROM sqlite_master WHERE type='table' AND name=?", (name,)
            ).fetchone()
        )

    def _chunk(self, ref, document_states=None):
        if not all(
            self._table(name)
            for name in ("archive_chunks", "archive_documents", "archive_sections")
        ):
            return None
        row = self.db.execute(
            "SELECT c.id,c.document_id,c.text,c.start,c.end,s.text AS section_text,d.sensitive,d.deleted_at FROM archive_chunks c JOIN archive_documents d ON c.document_id=d.id JOIN archive_sections s ON s.id=c.section_id AND s.document_id=c.document_id WHERE c.id=?",
            (ref["chunk_id"],),
        ).fetchone()
        if (
            not row
            or row["deleted_at"] is not None
            or ref.get("document_id", row["document_id"]) != row["document_id"]
        ):
            return None
        from hunter_gatherer.memory.document_visibility import document_reference_states
        if document_states is None:
            document_states = document_reference_states(self.db, chunk_ids=[ref["chunk_id"]])
        state = document_states.get(row["document_id"])
        if not state or not state["available"]:
            return None
        if (
            row["start"] < 0
            or row["end"] < row["start"]
            or row["section_text"][row["start"] : row["end"]] != row["text"]
        ):
            return None
        if "quote" in ref and (
            not isinstance(ref["quote"], str) or not ref["quote"] or ref["quote"] not in row["text"]
        ):
            return None
        return {**dict(row), "sensitive": state["sensitive"]}

    def _reference_state(self, workspace_id, references, seen=None):
        """Return available,sensitive. Missing sources stay unavailable with opt-in."""
        from hunter_gatherer.memory.evidence import _source_refs, is_sensitive_memory
        from hunter_gatherer.memory.retrieval_feedback import current_memory
        from hunter_gatherer.memory.workspace_scope import scope_visible

        seen = set() if seen is None else set(seen)
        sensitive = False
        if len(seen) > 32:
            return False, False
        if not isinstance(references, list):
            return False, False
        # Resolve all direct and memory-derived document dependencies together;
        # recursive legacy privacy is not recomputed for every selected chunk.
        from hunter_gatherer.memory.document_visibility import document_reference_states
        from hunter_gatherer.memory.original_file_reference import (
            original_reference_row,
            valid_original_reference,
        )
        memories, sources, chunks, documents, scopes = {}, {}, [], [], {}
        for ref in references:
            if valid_original_reference(ref):
                documents.append(ref["document_id"])
                continue
            if isinstance(ref, dict) and any(key in ref for key in ("page", "version", "original_content_hash")):
                return False, False
            if (not isinstance(ref, dict)
                    or sum(key in ref for key in ("memory_id", "chunk_id", "artifact_id", "uri", "path")) != 1
                    or any(not isinstance(ref[key], str) or not ref[key]
                           for key in ("memory_id", "chunk_id") if key in ref)):
                return False, False
            if "chunk_id" in ref:
                chunks.append(ref["chunk_id"])
            if "memory_id" in ref:
                identity = ref["memory_id"]
                if identity not in memories:
                    memories[identity] = current_memory(self.store, identity, include_sensitive=True)
                    sources[identity] = _source_refs(memories[identity]) if memories[identity] else []
                for source in sources[identity]:
                    if source.get("chunk_id"):
                        chunks.append(source["chunk_id"])
                    if source.get("document_id"):
                        documents.append(source["document_id"])
        document_states = (document_reference_states(self.db, document_ids=documents, chunk_ids=chunks)
                           if documents or chunks else {})
        for ref in references:
            if valid_original_reference(ref):
                page = original_reference_row(self.db, ref, document_states=document_states)
                if page is None:
                    return False, sensitive
                sensitive |= bool(page["sensitive"])
                continue
            if (
                not isinstance(ref, dict)
                or sum(k in ref for k in ("memory_id", "chunk_id", "artifact_id", "uri", "path"))
                != 1
            ):
                return False, sensitive
            if "memory_id" in ref:
                mem = memories[ref["memory_id"]]
                if not mem or not mem.is_retrievable():
                    return False, sensitive
                if mem.metadata.get("workspace_id") not in (None, "", workspace_id):
                    return False, sensitive
                scope = mem.metadata.get("scope")
                if scope is not None:
                    if not isinstance(scope, str):
                        return False, sensitive
                    if scope not in scopes:
                        try:
                            scopes[scope] = (scope_visible(self.store, scope, True),
                                             scope_visible(self.store, scope, False))
                        except (ValueError, TypeError, KeyError, IndexError, RecursionError):
                            return False, sensitive
                    available_scope, public_scope = scopes[scope]
                    if not available_scope:
                        return False, sensitive
                    sensitive |= not public_scope
                if "quote" in ref and (
                    not isinstance(ref["quote"], str)
                    or not ref["quote"]
                    or ref["quote"] not in mem.text
                ):
                    return False, sensitive
                # Labels/legacy disclosures are local; cited-source privacy is
                # already batched below, including nonpropagated closures.
                sensitive |= is_sensitive_memory(mem)
                # Validate current source availability as well as sensitivity.
                for source in sources[ref["memory_id"]]:
                    if source.get("chunk_id"):
                        chunk = self._chunk(source, document_states)
                        if chunk is None:
                            return False, sensitive
                        sensitive |= bool(chunk["sensitive"])
                    if source.get("document_id") and not source.get("chunk_id"):
                        if not self._table("archive_documents"):
                            return False, sensitive
                        d = document_states.get(source["document_id"])
                        if not d or not d["available"]:
                            return False, sensitive
                        sensitive |= bool(d["sensitive"])
            elif "chunk_id" in ref:
                chunk = self._chunk(ref, document_states)
                if chunk is None:
                    return False, sensitive
                sensitive |= bool(chunk["sensitive"])
            elif "artifact_id" in ref:
                marker = (ref["artifact_id"], ref.get("revision"))
                if marker in seen:
                    return False, sensitive
                try:
                    artifact = self._row(
                        workspace_id, ref["artifact_id"], "artifact", ref.get("revision")
                    )
                except CoordinationError:
                    return False, sensitive
                available, private = self._reference_state(
                    workspace_id, artifact.get("references", []), seen | {marker}
                )
                if not available:
                    return False, sensitive
                # The latest artifact can tighten privacy on all earlier versions.
                current = self._row(workspace_id, ref["artifact_id"], "artifact")
                sensitive |= bool(artifact.get("sensitive") or current.get("sensitive") or private)
            # uri/path references intentionally are descriptors only.
        return True, sensitive

    def _refs(self, workspace_id, refs, include_sensitive):
        if not isinstance(refs, list) or len(refs) > 100:
            raise CoordinationError("invalid", "references must be a list of at most 100 objects")
        refs = _bounded(refs, "references", 32768)
        from hunter_gatherer.memory.original_file_reference import valid_original_reference
        for ref in refs:
            if valid_original_reference(ref):
                continue
            if isinstance(ref, dict) and any(key in ref for key in ("page", "version", "original_content_hash")):
                raise CoordinationError("invalid", "original references require exact document/version/hash and optional page fields")
            if (
                not isinstance(ref, dict)
                or sum(k in ref for k in ("memory_id", "chunk_id", "artifact_id", "uri", "path"))
                != 1
            ):
                raise CoordinationError(
                    "invalid",
                    "each reference requires memory_id/chunk_id/artifact_id/uri/path or an exact original-file/page identity",
                )
            allowed = {
                "memory_id",
                "chunk_id",
                "artifact_id",
                "revision",
                "document_id",
                "quote",
                "uri",
                "path",
            }
            if set(ref) - allowed:
                raise CoordinationError("invalid", "reference contains unsupported fields")
            for key in ("memory_id", "chunk_id", "artifact_id", "uri", "path"):
                if key in ref:
                    _text(ref[key], key)
            if "artifact_id" in ref and (
                isinstance(ref.get("revision"), bool)
                or not isinstance(ref.get("revision"), int)
                or ref["revision"] < 1
            ):
                raise CoordinationError(
                    "invalid", "artifact references require a positive revision"
                )
        available, sensitive = self._reference_state(workspace_id, refs)
        if not available or (sensitive and not include_sensitive):
            raise CoordinationError(
                "reference_unavailable",
                "reference unavailable; sensitive sources require explicit include_sensitive",
            )
        return refs, sensitive

    def _view(self, obj, include_sensitive=False):
        if not isinstance(include_sensitive, bool):
            raise CoordinationError("invalid", "include_sensitive must be boolean")
        # Whole dependent prose is withheld because it may paraphrase private refs.
        available, private = self._reference_state(obj["workspace_id"], obj.get("references", []))
        current = self._row(obj["workspace_id"], obj["id"], obj["kind"])
        workspace_private = self._workspace(obj["workspace_id"]).get("sensitive", False)
        if obj.get("task_id"):
            task = self._row(obj["workspace_id"], obj["task_id"], "task")
            task_available, task_private = self._reference_state(
                obj["workspace_id"], task.get("references", [])
            )
            available = available and task_available
            private = private or task_private or task.get("sensitive", False)
        if obj["kind"] == "message" and obj["expires_at"] <= self.clock():
            available = False
        if not available or (
            (obj.get("sensitive") or current.get("sensitive") or private or workspace_private)
            and not include_sensitive
        ):
            return {
                "id": obj["id"],
                "workspace_id": obj["workspace_id"],
                "kind": obj["kind"],
                "revision": obj["revision"],
                "redacted": True,
                "reason": "source_unavailable_or_sensitive",
            }
        value = json.loads(_json(obj))
        value.pop("claim_token", None)
        if obj["kind"] == "handoff":
            value["offer_expired"] = (
                obj["status"] == "offered" and obj["expires_at"] <= self.clock()
            )
        return value

    def _receipt_view(self, response, include_sensitive):
        response = json.loads(_json(response))
        for key in ("workspace", "task", "artifact", "message", "handoff"):
            if isinstance(response.get(key), dict) and "id" in response[key]:
                response[key] = self._view(response[key], include_sensitive)
                if response[key].get("redacted"):
                    response.pop("claim_token", None)
        return response

    def _preflight_original_pages(self, action, kwargs, workspace_id, actor, request_key):
        """Verify NEW original identities outside the writer.

        Existing artifact selections and exact retry receipts need only current
        catalog validation. They remain editable if originals later disappear.
        Files verify the archived bytes; one highest-page render per document verifies a page range;
        temporary bytes/images are discarded and never persisted here.
        """
        from hunter_gatherer.memory.document_page_image import document_page_image
        from hunter_gatherer.memory.document_visibility import document_reference_states
        from hunter_gatherer.memory.original_file_reference import (
            original_reference_row,
            valid_original_reference,
        )

        deadline = time.monotonic() + _PAGE_PREFLIGHT_SECONDS
        selected = []
        def collect(value):
            if isinstance(value, dict):
                for key, child in value.items():
                    if key == "references" and isinstance(child, list):
                        if len(child) > 100:
                            raise CoordinationError("invalid", "references must be a list of at most 100 objects")
                        _bounded(child, "references", 32768)
                        for ref in child:
                            if isinstance(ref, dict) and any(field in ref for field in ("page", "version", "original_content_hash")):
                                if not valid_original_reference(ref):
                                    raise CoordinationError("invalid", "Invalid exact original-file/page reference.")
                                selected.append(ref)
                    elif key != "content":
                        collect(child)
            elif isinstance(value, list):
                for child in value:
                    collect(child)
        collect(kwargs)
        if not selected:
            return
        with self.store._lock:
            receipt = self.db.execute(
                "SELECT fingerprint FROM coordination_receipts WHERE workspace_id=? AND actor=? AND request_key=?",
                (workspace_id, actor, request_key)).fetchone()
            if receipt:
                # _write performs exact fingerprint matching and fresh visibility.
                return
            self._workspace(_text(workspace_id, "workspace_id", 128))
            retained = set()
            if action == "artifact_publish" and kwargs.get("artifact_id"):
                previous = self._row(workspace_id, kwargs["artifact_id"], "artifact")
                self._expect(previous, kwargs.get("expected_revision"))
                self._write_visibility(previous, kwargs)
                retained = {tuple(sorted(ref.items())) for ref in previous.get("references", [])
                            if valid_original_reference(ref)}
            new = [ref for ref in selected if tuple(sorted(ref.items())) not in retained]
            states = document_reference_states(self.db, document_ids=[ref["document_id"] for ref in new])
            for ref in new:
                row = original_reference_row(self.db, ref, document_states=states)
                if row is None or (row["sensitive"] and not kwargs.get("include_sensitive", False)):
                    raise CoordinationError("reference_unavailable", "Original reference unavailable or excluded.")
        grouped = {}
        for ref in new:
            key = (ref["document_id"], ref["version"], ref["original_content_hash"])
            if key not in grouped or ref.get("page", 0) > grouped[key].get("page", 0):
                grouped[key] = ref
        if len(grouped) > _MAX_NEW_PAGE_ORIGINALS:
            raise CoordinationError("invalid", "Add original references in batches of at most 4 new originals per publication.")
        for ref in grouped.values():
            if time.monotonic() >= deadline:
                raise CoordinationError("reference_unavailable", "Original verification exceeded 30 seconds; add references in smaller batches.")
            if "page" in ref:
                result = document_page_image(self.store, ref["document_id"], page=ref["page"], max_side=512,
                                             include_sensitive=kwargs.get("include_sensitive", False), _deadline=deadline)
            else:
                from hunter_gatherer.memory.document_originals import load_document_original

                result = load_document_original(self.store, ref["document_id"],
                                                include_sensitive=kwargs.get("include_sensitive", False))
                result.pop("_original_bytes", None)
                result["source_reference"] = {key: result.get(key) for key in ref}
            if time.monotonic() >= deadline:
                raise CoordinationError("reference_unavailable", "Original verification exceeded 30 seconds; add references in smaller batches.")
            if result.get("error") or result.get("source_reference") != ref:
                raise CoordinationError("reference_unavailable", "Exact original bytes or selected page could not be verified.")

    def _write(self, action, kwargs, operation):
        kwargs = _bounded({k: v for k, v in kwargs.items() if v is not None}, "request", 65536)
        for key in ("include_sensitive", "sensitive"):
            if key in kwargs and not isinstance(kwargs[key], bool):
                raise CoordinationError("invalid", f"{key} must be boolean")
        actor = _text(kwargs.get("actor"), "actor", 128)
        if action == "workspace_create" and "workspace_id" in kwargs:
            raise CoordinationError(
                "invalid", "workspace creation cannot specify an existing scope"
            )
        workspace_id = kwargs.get("workspace_id", "")
        request_key = _text(kwargs.get("idempotency_key"), "idempotency_key", 256)
        fingerprint = hashlib.sha256(
            _json({"action": action, "arguments": kwargs}).encode()
        ).hexdigest()
        self._preflight_original_pages(action, kwargs, workspace_id, actor, request_key)
        with self.store._lock:
            try:
                self.db.execute("BEGIN IMMEDIATE")
                prior = self.db.execute(
                    "SELECT fingerprint,response FROM coordination_receipts WHERE workspace_id=? AND actor=? AND request_key=?",
                    (workspace_id, actor, request_key),
                ).fetchone()
                if prior:
                    if prior["fingerprint"] != fingerprint:
                        raise CoordinationError(
                            "idempotency_conflict", "key was used for different arguments"
                        )
                    result = self._receipt_view(
                        json.loads(prior["response"]), kwargs.get("include_sensitive", False)
                    )
                    result["replayed"] = True
                    self.db.commit()
                    return result
                if action != "workspace_create":
                    self._workspace(_text(workspace_id, "workspace_id", 128))
                result = operation(kwargs)
                self.db.execute(
                    "INSERT INTO coordination_receipts VALUES(?,?,?,?,?)",
                    (workspace_id, actor, request_key, fingerprint, _json(result)),
                )
                result = self._receipt_view(result, kwargs.get("include_sensitive", False))
                self.db.commit()
                return result
            except BaseException:
                self.db.rollback()
                raise

    def execute(self, action, **kwargs):
        operation = getattr(self, "_op_" + action, None)
        if not operation:
            raise CoordinationError("invalid", "unknown coordination action")
        return self._write(action, kwargs, operation)

    def __getattr__(self, action):
        # Public mutation convenience API; wire schemas remain explicit.
        if action.startswith("_") or not hasattr(type(self), "_op_" + action):
            raise AttributeError(action)
        return lambda **kwargs: self.execute(action, **kwargs)

    def _get(self, kind, workspace_id, object_id, include_sensitive=False, revision=None):
        with self.store._lock:
            try:
                self._workspace(workspace_id)
                obj = self._row(workspace_id, object_id, kind, revision)
            except CoordinationError:
                return {"found": False}
            return {"found": True, kind: self._view(obj, include_sensitive)}

    def workspace_get(self, workspace_id, include_sensitive=False):
        return self._get("workspace", workspace_id, workspace_id, include_sensitive)

    def task_get(self, workspace_id, task_id, include_sensitive=False):
        return self._get("task", workspace_id, task_id, include_sensitive)

    def artifact_get(self, workspace_id, artifact_id, revision=None, include_sensitive=False):
        return self._get("artifact", workspace_id, artifact_id, include_sensitive, revision)

    def handoff_get(self, workspace_id, handoff_id, include_sensitive=False):
        return self._get("handoff", workspace_id, handoff_id, include_sensitive)

    def _list(
        self, kind, workspace_id, limit=20, after_id="", include_sensitive=False, recipient=None
    ):
        limit = _limit(limit)
        with self.store._lock:
            self._workspace(workspace_id)
            rows = self.db.execute(
                "SELECT data FROM coordination_objects WHERE workspace_id=? AND kind=? AND id>? ORDER BY id",
                (workspace_id, kind, after_id),
            )
            results = []
            more = False
            for row in rows:
                data = json.loads(row["data"])
                if recipient is not None and (
                    data["recipient"] != recipient
                    or data["expires_at"] <= self.clock()
                    or data["status"] == "acknowledged"
                ):
                    continue
                if len(results) == limit:
                    more = True
                    break
                results.append(self._view(data, include_sensitive))
            return {kind + "s": results, "next_cursor": results[-1]["id"] if more else None}

    def task_list(self, workspace_id, limit=20, after_id="", include_sensitive=False):
        return self._list("task", workspace_id, limit, after_id, include_sensitive)

    def artifact_list(self, workspace_id, limit=20, after_id="", include_sensitive=False):
        return self._list("artifact", workspace_id, limit, after_id, include_sensitive)

    def workspace_list(self, limit=20, after_id="", include_sensitive=False):
        limit = _limit(limit)
        with self.store._lock:
            rows = self.db.execute(
                "SELECT data FROM coordination_objects WHERE kind='workspace' AND id>? ORDER BY id LIMIT ?",
                (after_id, limit + 1),
            ).fetchall()
            items = [self._view(json.loads(r["data"]), include_sensitive) for r in rows[:limit]]
            return {
                "workspaces": items,
                "next_cursor": items[-1]["id"] if len(rows) > limit else None,
            }

    def message_inbox(
        self, workspace_id, recipient, limit=20, after_id="", include_sensitive=False
    ):
        _text(recipient, "recipient", 128)
        return self._list("message", workspace_id, limit, after_id, include_sensitive, recipient)

    def task_history(
        self, workspace_id, task_id, limit=20, before_revision=None, include_sensitive=False
    ):
        with self.store._lock:
            self._row(workspace_id, task_id, "task")
            limit = _limit(limit)
            rows = self.db.execute(
                "SELECT data FROM coordination_versions WHERE object_id=? AND workspace_id=? AND revision<? ORDER BY revision DESC LIMIT ?",
                (task_id, workspace_id, before_revision or 2**63 - 1, limit + 1),
            ).fetchall()
            items = [self._view(json.loads(r["data"]), include_sensitive) for r in rows[:limit]]
            return {
                "history": items,
                "next_before_revision": items[-1]["revision"] if len(rows) > limit else None,
            }

    def changes(self, workspace_id, after_cursor=0, limit=50):
        _limit(limit)
        with self.store._lock:
            self._workspace(workspace_id)
            latest = self.db.execute(
                "SELECT COALESCE(MAX(sequence),0) FROM coordination_events WHERE workspace_id=?",
                (workspace_id,),
            ).fetchone()[0]
            if (
                isinstance(after_cursor, bool)
                or not isinstance(after_cursor, int)
                or after_cursor < 0
                or after_cursor > latest
            ):
                return {
                    "error": "invalid_cursor",
                    "snapshot_required": True,
                    "latest_cursor": latest,
                }
            if (
                after_cursor
                and not self.db.execute(
                    "SELECT 1 FROM coordination_events WHERE sequence=? AND workspace_id=?",
                    (after_cursor, workspace_id),
                ).fetchone()
            ):
                return {
                    "error": "invalid_cursor",
                    "snapshot_required": True,
                    "latest_cursor": latest,
                }
            rows = self.db.execute(
                "SELECT * FROM coordination_events WHERE workspace_id=? AND sequence>? ORDER BY sequence LIMIT ?",
                (workspace_id, after_cursor, limit + 1),
            ).fetchall()
            events = [dict(r) for r in rows[:limit]]
            # Events contain identifiers/revisions/actions only, never copied prose/tokens.
            return {
                "changes": events,
                "next_cursor": events[-1]["sequence"] if events else after_cursor,
                "has_more": len(rows) > limit,
                "latest_cursor": latest,
            }

    def snapshot(self, workspace_id, limit=20, include_sensitive=False):
        with self.store._lock:
            return {
                "workspace": self.workspace_get(workspace_id, include_sensitive),
                "tasks": self.task_list(workspace_id, limit, include_sensitive=include_sensitive),
                "artifacts": self.artifact_list(
                    workspace_id, limit, include_sensitive=include_sensitive
                ),
                "latest_cursor": self.changes(workspace_id, limit=1)["latest_cursor"],
                "messages": "Use message_inbox with an explicit recipient; snapshot never exposes all recipients.",
            }

    def _op_workspace_create(self, k):
        obj = self._new(
            "workspace",
            "",
            k["actor"],
            name=_text(k.get("name"), "name", 256),
            description=_text(k.get("description", ""), "description", 4096, False),
            sensitive=bool(k.get("sensitive", False)),
        )
        obj["workspace_id"] = obj["id"]
        if obj["sensitive"] and not k.get("include_sensitive"):
            raise CoordinationError(
                "sensitive_opt_in", "sensitive writes require include_sensitive"
            )
        return {
            "workspace": self._save(obj, "created", True),
            "boundary": "trusted_local_client_labels_not_authenticated_tenants",
        }

    def _op_workspace_update(self, k):
        obj = self._workspace(k["workspace_id"])
        self._expect(obj, k.get("expected_revision"))
        for field in ("name", "description"):
            if field in k:
                obj[field] = _text(k[field], field, 4096, field == "name")
        if "sensitive" in k:
            obj["sensitive"] = bool(k["sensitive"])
        if obj["sensitive"] and not k.get("include_sensitive"):
            raise CoordinationError(
                "sensitive_opt_in", "sensitive writes require include_sensitive"
            )
        return {"workspace": self._save(obj, "updated")}

    def _contract(self, k):
        objective = _text(k.get("objective"), "objective", 8192)
        constraints = k.get("constraints", [])
        if not isinstance(constraints, list) or len(constraints) > 50:
            raise CoordinationError("invalid", "constraints must be at most 50 strings")
        for c in constraints:
            _text(c, "constraint", 2048)
        criteria = k.get("acceptance_criteria", [])
        if not isinstance(criteria, list) or not 1 <= len(criteria) <= 50:
            raise CoordinationError(
                "invalid", "acceptance_criteria requires 1..50 {id,description} objects"
            )
        ids = set()
        for c in criteria:
            if not isinstance(c, dict):
                raise CoordinationError("invalid", "criterion must be an object")
            cid = _text(c.get("id"), "criterion id", 128)
            _text(c.get("description"), "criterion description", 2048)
            if cid in ids:
                raise CoordinationError("invalid", "duplicate criterion id")
            ids.add(cid)
        return objective, constraints, _bounded(criteria)

    def _dependencies(self, workspace_id, task_id, dependencies):
        if (
            not isinstance(dependencies, list)
            or len(dependencies) > 50
            or any(not isinstance(d, str) for d in dependencies)
            or len(set(dependencies)) != len(dependencies)
        ):
            raise CoordinationError("invalid", "dependencies must be at most 50 distinct task IDs")

        def visit(tid, seen):
            if tid == task_id:
                raise CoordinationError("dependency_cycle", "dependency creates a cycle")
            if tid in seen:
                return
            data = self._row(workspace_id, tid, "task")
            for child in data["dependencies"]:
                visit(child, seen | {tid})

        for dep in dependencies:
            _text(dep, "dependency", 128)
            visit(dep, set())
        return dependencies

    def _op_task_create(self, k):
        objective, constraints, criteria = self._contract(k)
        refs, private = self._refs(
            k["workspace_id"], k.get("references", []), k.get("include_sensitive", False)
        )
        obj = self._new(
            "task",
            k["workspace_id"],
            k["actor"],
            objective=objective,
            constraints=constraints,
            acceptance_criteria=criteria,
            status="pending",
            dependencies=[],
            owner=None,
            fence=0,
            lease_expires_at=None,
            claim_token=None,
            checkpoint=None,
            checkpoint_count=0,
            references=refs,
            sensitive=bool(private or k.get("sensitive", False)),
        )
        self._write_visibility(obj, k)
        obj["dependencies"] = self._dependencies(
            k["workspace_id"], obj["id"], k.get("dependencies", [])
        )
        return {"task": self._save(obj, "created", True)}

    def _write_visibility(self, obj, k):
        available, private = self._reference_state(obj["workspace_id"], obj.get("references", []))
        if not available or (
            (
                obj.get("sensitive")
                or private
                or self._workspace(obj["workspace_id"]).get("sensitive")
            )
            and not k.get("include_sensitive", False)
        ):
            raise CoordinationError(
                "reference_unavailable",
                "object or sources unavailable; private content requires opt-in",
            )
        obj["sensitive"] = bool(obj.get("sensitive") or private)

    def _task(self, k, claimed=False):
        obj = self._row(k["workspace_id"], k.get("task_id"), "task")
        self._expect(obj, k.get("expected_revision"))
        self._write_visibility(obj, k)
        if obj["status"] in ("completed", "cancelled"):
            raise CoordinationError("terminal_task", "task is terminal")
        if claimed:
            self._claim_check(obj, k)
        return obj

    def _claim_check(self, obj, k):
        if (
            obj.get("owner") != k["actor"]
            or not obj.get("claim_token")
            or k.get("claim_token") != obj["claim_token"]
            or k.get("fence") != obj["fence"]
            or (obj.get("lease_expires_at") or 0) <= self.clock()
        ):
            raise CoordinationError(
                "stale_claim", "current unexpired claim token and fence required"
            )

    def _ttl(self, k, key="lease_seconds", default=300, maximum=3600):
        ttl = k.get(key, default)
        if isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or not 1 <= ttl <= maximum:
            raise CoordinationError("invalid", f"{key} must be 1..{maximum}")
        return ttl

    def _op_task_claim(self, k):
        obj = self._task(k)
        if obj.get("claim_token") and (obj.get("lease_expires_at") or 0) > self.clock():
            raise CoordinationError("claim_conflict", "task already has an active claim")
        for dep in obj["dependencies"]:
            if self._row(k["workspace_id"], dep, "task")["status"] != "completed":
                raise CoordinationError(
                    "dependency_incomplete", "required dependency not completed"
                )
        obj.update(
            owner=k["actor"],
            claim_token=secrets.token_urlsafe(32),
            fence=obj["fence"] + 1,
            lease_expires_at=self.clock() + self._ttl(k),
            status="working",
        )
        return {
            "task": self._save(obj, "claimed"),
            "claim_token": obj["claim_token"],
            "fence": obj["fence"],
        }

    def _op_task_renew(self, k):
        obj = self._task(k, True)
        obj["lease_expires_at"] = self.clock() + self._ttl(k)
        return {
            "task": self._save(obj, "renewed"),
            "claim_token": obj["claim_token"],
            "fence": obj["fence"],
        }

    def _op_task_release(self, k):
        obj = self._task(k, True)
        obj.update(owner=None, claim_token=None, lease_expires_at=None, status="pending")
        return {"task": self._save(obj, "released")}

    def _op_task_update(self, k):
        obj = self._task(k, True)
        objective, constraints, criteria = self._contract(
            {
                "objective": k.get("objective", obj["objective"]),
                "constraints": k.get("constraints", obj["constraints"]),
                "acceptance_criteria": k.get("acceptance_criteria", obj["acceptance_criteria"]),
            }
        )
        obj.update(
            objective=objective,
            constraints=constraints,
            acceptance_criteria=criteria,
            dependencies=self._dependencies(
                k["workspace_id"], obj["id"], k.get("dependencies", obj["dependencies"])
            ),
        )
        if "sensitive" in k:
            obj["sensitive"] = bool(k["sensitive"])
            self._write_visibility(obj, k)
        return {"task": self._save(obj, "updated")}

    def _packet(self, value):
        packet = _bounded(value, "checkpoint/packet", 16384)
        allowed = {
            "completed_steps",
            "findings",
            "blockers",
            "next_action",
            "attempted_side_effects",
            "references",
            "verification_notes",
        }
        if not isinstance(packet, dict) or set(packet) - allowed:
            raise CoordinationError("invalid", "checkpoint/packet has unsupported fields")
        for field in ("completed_steps", "findings", "blockers", "attempted_side_effects"):
            if field in packet:
                if not isinstance(packet[field], list) or len(packet[field]) > 50:
                    raise CoordinationError("invalid", f"{field} must be at most 50 strings")
                for item in packet[field]:
                    _text(item, field, 4096)
        for field in ("next_action", "verification_notes"):
            if field in packet:
                _text(packet[field], field, 4096, False)
        return packet

    def _op_task_checkpoint(self, k):
        obj = self._task(k, True)
        packet = self._packet(k.get("checkpoint"))
        refs, private = self._refs(
            k["workspace_id"], packet.get("references", []), k.get("include_sensitive", False)
        )
        obj["references"] = _bounded(obj["references"] + refs, "cumulative references", 32768)
        obj.update(
            checkpoint=packet,
            checkpoint_count=obj["checkpoint_count"] + 1,
            sensitive=bool(obj["sensitive"] or private),
            status="blocked" if packet.get("blockers") else "working",
        )
        return {"task": self._save(obj, "checkpointed"), "checkpoint_revision": obj["revision"]}

    def _op_task_complete(self, k):
        obj = self._task(k, True)
        for dependency in obj["dependencies"]:
            if self._row(k["workspace_id"], dependency, "task")["status"] != "completed":
                raise CoordinationError(
                    "dependency_incomplete", "required dependency not completed"
                )
        outcomes = _bounded(k.get("criterion_results"), "criterion_results", 16384)
        if not isinstance(outcomes, list):
            raise CoordinationError("invalid", "criterion_results must be a list")
        required = {c["id"] for c in obj["acceptance_criteria"]}
        if {x.get("criterion_id") for x in outcomes if isinstance(x, dict)} != required or len(
            outcomes
        ) != len(required):
            raise CoordinationError(
                "criteria_incomplete", "every criterion needs exactly one result"
            )
        refs = []
        for result in outcomes:
            if set(result) - {"criterion_id", "waiver", "references", "verification_notes"}:
                raise CoordinationError("invalid", "unsupported criterion result fields")
            if result.get("waiver"):
                if result.get("references"):
                    checked, private = self._refs(
                        k["workspace_id"], result["references"], k.get("include_sensitive", False)
                    )
                    refs += checked
                    obj["sensitive"] |= private
                _text(result["waiver"], "explicit waiver", 4096)
            else:
                evidence = result.get("references", [])
                if not evidence or not any(
                    "memory_id" in r or "chunk_id" in r or "artifact_id" in r
                    for r in evidence
                    if isinstance(r, dict)
                ):
                    raise CoordinationError(
                        "criteria_incomplete",
                        "criterion needs stored evidence or an explicit waiver; external descriptors alone are not evidence",
                    )
                checked, private = self._refs(
                    k["workspace_id"], evidence, k.get("include_sensitive", False)
                )
                refs += checked
                obj["sensitive"] |= private
            if "verification_notes" in result:
                _text(result["verification_notes"], "verification_notes", 4096, False)
        obj["references"] = _bounded(obj["references"] + refs, "cumulative references", 32768)
        obj.update(
            status="completed",
            completion={
                "criterion_results": outcomes,
                "verification": "actor_reported; server checks reference integrity, not truth or actual execution",
                "completed_by": k["actor"],
                "completed_at": self.clock(),
            },
            claim_token=None,
            lease_expires_at=None,
        )
        return {
            "task": self._save(obj, "completed"),
            "integrity_checked": True,
            "truth_verified": False,
        }

    def _op_handoff_offer(self, k):
        task = self._task(k, True)
        recipient = _text(k.get("recipient"), "recipient", 128)
        if recipient == k["actor"]:
            raise CoordinationError("invalid", "recipient must differ from current owner")
        packet = self._packet(k.get("packet", task.get("checkpoint") or {}))
        refs, private = self._refs(
            k["workspace_id"], packet.get("references", []), k.get("include_sensitive", False)
        )
        obj = self._new(
            "handoff",
            k["workspace_id"],
            k["actor"],
            task_id=task["id"],
            recipient=recipient,
            packet=packet,
            task_fence=task["fence"],
            task_revision=task["revision"] + 1,
            status="offered",
            expires_at=self.clock() + self._ttl(k, "expires_seconds", 300, 86400),
            references=task["references"] + refs,
            sensitive=bool(task["sensitive"] or private),
        )
        task["handoff_id"] = obj["id"]
        self._save(task, "handoff_offered")
        return {"handoff": self._save(obj, "offered", True), "task": task}

    def _offer(self, k):
        offer = self._row(k["workspace_id"], k.get("handoff_id"), "handoff")
        self._expect(offer, k.get("expected_revision"))
        self._write_visibility(offer, k)
        if offer["recipient"] != k["actor"]:
            raise CoordinationError(
                "recipient_mismatch", "only addressed recipient may accept or reject"
            )
        if offer["status"] != "offered" or offer["expires_at"] <= self.clock():
            raise CoordinationError("handoff_unavailable", "offer rejected, accepted, or expired")
        return offer

    def _op_handoff_accept(self, k):
        offer = self._offer(k)
        task = self._row(k["workspace_id"], offer["task_id"], "task")
        self._expect(task, k.get("expected_task_revision"))
        self._write_visibility(task, k)
        if (
            task["status"] in ("completed", "cancelled")
            or task["fence"] != offer["task_fence"]
            or task["revision"] != offer["task_revision"]
            or task.get("handoff_id") != offer["id"]
        ):
            raise CoordinationError("stale_handoff", "task ownership changed since offer")
        task.update(
            owner=k["actor"],
            claim_token=secrets.token_urlsafe(32),
            fence=task["fence"] + 1,
            lease_expires_at=self.clock() + self._ttl(k),
            status="working",
            checkpoint=offer["packet"],
            references=offer["references"],
            sensitive=offer["sensitive"],
        )
        offer.update(status="accepted", accepted_at=self.clock())
        self._save(offer, "accepted")
        return {
            "handoff": offer,
            "task": self._save(task, "handoff_accepted"),
            "claim_token": task["claim_token"],
            "fence": task["fence"],
        }

    def _op_handoff_reject(self, k):
        offer = self._offer(k)
        offer.update(status="rejected", reason=_text(k.get("reason"), "reason", 2048))
        return {"handoff": self._save(offer, "rejected")}

    def _op_artifact_publish(self, k):
        content = _bounded(k.get("content"), "content", 32768)
        refs, private = self._refs(
            k["workspace_id"], k.get("references", []), k.get("include_sensitive", False)
        )
        task_id = k.get("task_id")
        if task_id:
            parent = self._row(k["workspace_id"], task_id, "task")
            self._write_visibility(parent, k)
            refs = _bounded(parent["references"] + refs, "references", 32768)
            private = bool(private or parent["sensitive"])
        if k.get("artifact_id"):
            obj = self._row(k["workspace_id"], k["artifact_id"], "artifact")
            self._expect(obj, k.get("expected_revision"))
            self._write_visibility(obj, k)
            new = False
        else:
            obj = self._new("artifact", k["workspace_id"], k["actor"])
            new = True
        obj.update(
            name=_text(k.get("name"), "name", 256),
            content=content,
            content_hash=hashlib.sha256(_json(content).encode()).hexdigest(),
            producer=k["actor"],
            task_id=task_id,
            references=refs,
            sensitive=bool(obj.get("sensitive") or private or k.get("sensitive", False)),
            verification_notes=_text(
                k.get("verification_notes", ""), "verification_notes", 4096, False
            ),
            verification="actor_reported",
        )
        self._write_visibility(obj, k)
        return {"artifact": self._save(obj, "published", new)}

    def _op_message_send(self, k):
        refs, private = self._refs(
            k["workspace_id"], k.get("references", []), k.get("include_sensitive", False)
        )
        obj = self._new(
            "message",
            k["workspace_id"],
            k["actor"],
            sender=k["actor"],
            recipient=_text(k.get("recipient"), "recipient", 128),
            body=_text(k.get("body"), "body", 8192),
            status="unread",
            expires_at=self.clock() + self._ttl(k, "expires_seconds", 86400, 604800),
            references=refs,
            sensitive=bool(private or k.get("sensitive", False)),
            authorization="message data grants no execution authority",
        )
        self._write_visibility(obj, k)
        return {"message": self._save(obj, "sent", True)}

    def _op_message_ack(self, k):
        obj = self._row(k["workspace_id"], k.get("message_id"), "message")
        self._expect(obj, k.get("expected_revision"))
        self._write_visibility(obj, k)
        if obj["recipient"] != k["actor"]:
            raise CoordinationError("recipient_mismatch", "only recipient may acknowledge")
        if obj["expires_at"] <= self.clock():
            raise CoordinationError("message_expired", "message has expired")
        obj.update(status="acknowledged", acknowledged_at=self.clock())
        return {"message": self._save(obj, "acknowledged")}
