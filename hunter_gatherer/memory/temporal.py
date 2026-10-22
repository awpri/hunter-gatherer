"""Evidence-linked temporal claims; availability is never a truth judgment."""
from __future__ import annotations

import hashlib
import json
import math
import time
import uuid
from datetime import datetime


def timestamp(value, name="timestamp"):
    if value is None:
        return None
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a finite epoch or timezone-aware ISO timestamp")
    if isinstance(value, str):
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                raise ValueError("timezone required")
            value = dt.timestamp()
        except (ValueError, OverflowError) as exc:
            raise ValueError(f"Invalid {name}; timezone required") from exc
    if not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"Invalid {name}")
    return float(value)


def label(value, name):
    if not isinstance(value, str) or not value.strip() or len(value) > 500:
        raise ValueError(f"{name} must be a nonempty bounded string")
    return value.strip()


class TemporalMemory:
    def __init__(self, store):
        self.store = store
        with store._lock:
            schema = """
            CREATE TABLE IF NOT EXISTS temporal_groups (
              scope TEXT, subject TEXT, predicate TEXT, revision INTEGER NOT NULL,
              PRIMARY KEY(scope,subject,predicate));
            CREATE TABLE IF NOT EXISTS temporal_claims (
              id TEXT PRIMARY KEY, scope TEXT, subject TEXT, predicate TEXT,
              value TEXT, evidence TEXT, valid_from REAL, valid_to REAL,
              recorded_at REAL NOT NULL, kind TEXT, replaces TEXT, revision INTEGER);
            CREATE INDEX IF NOT EXISTS temporal_claim_scope ON temporal_claims(scope,subject,predicate);
            CREATE TABLE IF NOT EXISTS temporal_resolutions (
              id TEXT PRIMARY KEY, scope TEXT, subject TEXT, predicate TEXT,
              retained TEXT, reason TEXT, recorded_at REAL, revision INTEGER);
            CREATE TABLE IF NOT EXISTS temporal_requests (
              scope TEXT, request_key TEXT, fingerprint TEXT, result TEXT,
              PRIMARY KEY(scope,request_key));
            """
            owns_transaction = not store._sqlite.in_transaction
            try:
                if owns_transaction:
                    store._sqlite.execute("BEGIN IMMEDIATE")
                for statement in schema.split(";"):
                    if statement.strip():
                        store._sqlite.execute(statement)
                if owns_transaction:
                    store._sqlite.commit()
            except Exception:
                if owns_transaction:
                    store._sqlite.rollback()
                raise

    def _evidence(self, ids, include_sensitive=False):
        from hunter_gatherer.memory.retrieval_feedback import current_memory

        result = []
        for memory_id in ids:
            mem = current_memory(self.store, memory_id, include_sensitive,
                                 include_superseded=True)
            if mem is None:
                return None
            from hunter_gatherer.memory.workspace_scope import scope_visible

            memory_scope = mem.metadata.get("workspace_id") or mem.metadata.get("scope") or "global"
            try:
                visible = scope_visible(self.store, memory_scope, include_sensitive)
            except ValueError:
                visible = False
            if not visible:
                return None
            result.append({"memory_id": mem.id, "citation": f"[mem:{mem.id[:6]}]"})
        return result

    def _public(self, row, include_sensitive=False):
        evidence = self._evidence(json.loads(row["evidence"]), include_sensitive)
        if evidence is None:
            return None
        return {**{k: row[k] for k in ("id", "scope", "subject", "predicate", "valid_from", "valid_to",
                                      "recorded_at", "kind", "replaces", "revision")},
                "value": json.loads(row["value"]), "evidence": evidence}

    def _revision(self, scope, subject, predicate):
        row = self.store._sqlite.execute(
            "SELECT revision FROM temporal_groups WHERE scope=? AND subject=? AND predicate=?",
            (scope, subject, predicate)).fetchone()
        return row[0] if row else 0

    def mutate(self, action, *, scope, subject, predicate, idempotency_key, expected_revision,
               value=None, evidence_ids=None, valid_from=None, valid_to=None, kind="assertion",
               replaces=None, retained_ids=None, reason=None, include_sensitive=False):
        scope, subject, predicate = (label(v, n) for v, n in
                                     ((scope, "scope"), (subject, "subject"), (predicate, "predicate")))
        key = label(idempotency_key, "idempotency_key")
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("expected_revision must be a nonnegative integer")
        if type(include_sensitive) is not bool:
            raise ValueError("include_sensitive must be boolean")
        start, end = timestamp(valid_from, "valid_from"), timestamp(valid_to, "valid_to")
        if end is not None and start is None:
            raise ValueError("valid_to requires valid_from")
        if end is not None and end <= start:
            raise ValueError("valid_to must follow valid_from")
        if action not in {"record", "resolve"}:
            raise ValueError("Invalid mutation action")
        if action == "record":
            if kind not in {"assertion", "change", "correction"}:
                raise ValueError("Invalid claim kind")
            if not isinstance(evidence_ids, list) or not evidence_ids or len(evidence_ids) > 50 or any(
                    not isinstance(v, str) or not v for v in evidence_ids):
                raise ValueError("evidence_ids must contain 1..50 memory IDs")
            evidence_ids = sorted(set(evidence_ids))
            if kind != "assertion" and not replaces:
                raise ValueError("change/correction requires replaces")
            if kind == "assertion" and replaces is not None:
                raise ValueError("assertion cannot replace a claim")
            if kind == "change" and start is None:
                raise ValueError("change requires known effective date")
            encoded = json.dumps(value, allow_nan=False, sort_keys=True)
            if len(encoded) > 8000:
                raise ValueError("value exceeds 8000 characters")
        else:
            if not isinstance(retained_ids, list) or not retained_ids or len(retained_ids) > 50 or any(
                    not isinstance(v, str) or not v for v in retained_ids):
                raise ValueError("retained_ids must contain 1..50 IDs")
            retained_ids = sorted(set(retained_ids))
            reason = label(reason, "reason")
        payload = dict(action=action, scope=scope, subject=subject, predicate=predicate,
                       expected_revision=expected_revision, value=value, evidence_ids=evidence_ids,
                       valid_from=start, valid_to=end, kind=kind, replaces=replaces,
                       retained_ids=retained_ids, reason=reason, include_sensitive=include_sensitive)
        fingerprint = hashlib.sha256(json.dumps(payload, sort_keys=True, allow_nan=False).encode()).hexdigest()
        db = self.store._sqlite
        with self.store._lock:
            db.execute("BEGIN IMMEDIATE")
            try:
                from hunter_gatherer.memory.workspace_scope import scope_visible

                if not scope_visible(self.store, scope, include_sensitive):
                    prior = db.execute("SELECT fingerprint FROM temporal_requests WHERE scope=? AND request_key=?",
                                       (scope, key)).fetchone()
                    if prior and prior["fingerprint"] == fingerprint:
                        db.rollback()
                        return {"state": "unknown", "scope": scope,
                                "reason": "Scope unavailable without sensitive opt-in"}
                    raise ValueError("Scope unavailable without sensitive opt-in")
                prior = db.execute("SELECT * FROM temporal_requests WHERE scope=? AND request_key=?", (scope, key)).fetchone()
                if prior:
                    if prior["fingerprint"] != fingerprint:
                        raise ValueError("Idempotency key reused with different payload")
                    result = json.loads(prior["result"])
                else:
                    revision = self._revision(scope, subject, predicate)
                    if revision != expected_revision:
                        raise ValueError(f"Revision conflict: expected {expected_revision}, current {revision}")
                    rows = db.execute("SELECT * FROM temporal_claims WHERE scope=? AND subject=? AND predicate=?",
                                      (scope, subject, predicate)).fetchall()
                    by_id = {r["id"]: r for r in rows}
                    if action == "record":
                        if self._evidence(evidence_ids, include_sensitive) is None:
                            raise ValueError("Evidence unavailable or sensitive")
                        if replaces:
                            parent = by_id.get(replaces)
                            if parent is None or self._public(parent, include_sensitive) is None:
                                raise ValueError("Replaced claim unavailable or scope mismatch")
                            if kind == "change" and parent["valid_from"] is not None and start <= parent["valid_from"]:
                                raise ValueError("Change must follow predecessor effective date")
                            if kind == "correction" and (start, end) != (parent["valid_from"], parent["valid_to"]):
                                raise ValueError("Correction preserves predecessor interval; use change for new effective dates")
                        claim_id = uuid.uuid4().hex
                        db.execute("INSERT INTO temporal_claims VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                                   (claim_id, scope, subject, predicate, encoded, json.dumps(evidence_ids),
                                    start, end, time.time(), kind, replaces, revision + 1))
                        result = {"claim_id": claim_id, "revision": revision + 1, "scope": scope}
                    else:
                        if any(i not in by_id or self._public(by_id[i], include_sensitive) is None for i in retained_ids):
                            raise ValueError("Retained claim unavailable or scope mismatch")
                        resolution_id = uuid.uuid4().hex
                        db.execute("INSERT INTO temporal_resolutions VALUES (?,?,?,?,?,?,?,?)",
                                   (resolution_id, scope, subject, predicate, json.dumps(retained_ids), reason,
                                    time.time(), revision + 1))
                        result = {"resolution_id": resolution_id, "revision": revision + 1,
                                  "scope": scope, "retained_ids": retained_ids}
                    db.execute("INSERT INTO temporal_groups VALUES (?,?,?,?) ON CONFLICT(scope,subject,predicate) "
                               "DO UPDATE SET revision=excluded.revision", (scope, subject, predicate, revision + 1))
                    db.execute("INSERT INTO temporal_requests VALUES (?,?,?,?)", (scope, key, fingerprint, json.dumps(result)))
                db.commit()
            except Exception:
                db.rollback()
                raise
            # Re-read source visibility, including cached retry responses. Never cache prose.
            ids = [result["claim_id"]] if "claim_id" in result else result["retained_ids"]
            visible = []
            for i in ids:
                row = db.execute("SELECT * FROM temporal_claims WHERE id=?", (i,)).fetchone()
                if row and self._public(row, include_sensitive):
                    visible.append(i)
            if len(visible) != len(ids):
                return {"state": "unknown", "revision": result["revision"], "scope": scope,
                        "reason": "Referenced evidence no longer available"}
            return result

    def query(self, *, scope, subject=None, predicate=None, as_of=None, known_at=None,
              include_sensitive=False, limit=50, history=False):
        scope = label(scope, "scope")
        if type(limit) is not int or not 1 <= limit <= 200:
            raise ValueError("limit must be 1..200")
        if type(include_sensitive) is not bool:
            raise ValueError("include_sensitive must be boolean")
        now = time.time()
        effective = timestamp(as_of, "as_of") if as_of is not None else now
        known = timestamp(known_at, "known_at") if known_at is not None else now
        clauses, params = ["scope=?", "recorded_at<=?"], [scope, known]
        for name, v in (("subject", subject), ("predicate", predicate)):
            if v is not None:
                clauses.append(f"{name}=?")
                params.append(label(v, name))
        with self.store._lock:
            from hunter_gatherer.memory.workspace_scope import scope_visible

            if not scope_visible(self.store, scope, include_sensitive):
                return {"state": "unknown", "claims": [], "uncertain_claims": [], "resolutions": [],
                        "omissions": 0, "as_of": effective, "known_at": known, "scope": scope, "revision": None}
            rows = self.store._sqlite.execute("SELECT * FROM temporal_claims WHERE " + " AND ".join(clauses)
                                             + " ORDER BY recorded_at,id", params).fetchall()
            public = {r["id"]: self._public(r, include_sensitive) for r in rows}
            visible = [r for r in rows if public[r["id"]] is not None]
            suppressed, ends = set(), {}
            for r in visible:
                if r["replaces"] and r["kind"] == "correction":
                    suppressed.add(r["replaces"])
                elif r["replaces"] and r["kind"] == "change":
                    ends[r["replaces"]] = min(ends.get(r["replaces"], float("inf")), r["valid_from"])
            resolutions = self.store._sqlite.execute(
                "SELECT * FROM temporal_resolutions WHERE scope=? AND recorded_at<=? ORDER BY recorded_at,revision",
                (scope, known)).fetchall()
            audit = []
            for resolution in resolutions:
                retained = json.loads(resolution["retained"])
                # Invalid/private retained evidence cannot reveal a resolution or hide alternatives.
                if not all(public.get(i) for i in retained):
                    continue
                if subject is not None and resolution["subject"] != subject:
                    continue
                if predicate is not None and resolution["predicate"] != predicate:
                    continue
                audit.append({"id": resolution["id"], "retained_ids": retained,
                              "reason": resolution["reason"], "recorded_at": resolution["recorded_at"],
                              "revision": resolution["revision"]})
                suppressed.update(r["id"] for r in visible if
                                  (r["subject"], r["predicate"]) == (resolution["subject"], resolution["predicate"])
                                  and r["revision"] < resolution["revision"] and r["id"] not in retained)
            candidates, uncertain, expired = [], [], []
            for r in visible:
                if r["id"] in suppressed and not history:
                    continue
                if history:
                    candidates.append(public[r["id"]])
                    continue
                if r["valid_from"] is None:
                    uncertain.append(public[r["id"]])
                    continue
                end = min(r["valid_to"] if r["valid_to"] is not None else float("inf"),
                          ends.get(r["id"], float("inf")))
                if r["valid_from"] <= effective < end:
                    candidates.append(public[r["id"]])
                elif end <= effective:
                    expired.append(public[r["id"]])
            keys = {}
            for c in candidates:
                keys.setdefault((c["subject"], c["predicate"]), set()).add(json.dumps(c["value"], sort_keys=True))
            state = "conflicting" if any(len(v) > 1 for v in keys.values()) else (
                "unknown" if uncertain else "available" if candidates else "unknown" if not expired else "stale")
            if history:
                state = "available" if candidates else "unknown"
            output = candidates or uncertain or expired
            return {"state": state, "claims": output[:limit], "uncertain_claims": uncertain[:limit] if candidates else [],
                    "omissions": max(0, len(output) - limit), "as_of": effective, "known_at": known,
                    "scope": scope, "resolutions": audit[-limit:] if history else [],
                    "revision": self._revision(scope, subject, predicate) if subject and predicate else None}

    def history(self, memory_id, *, include_sensitive=False, limit=30):
        memory_id = label(memory_id, "memory_id")
        if type(include_sensitive) is not bool:
            raise ValueError("include_sensitive must be boolean")
        if type(limit) is not int or not 1 <= limit <= 100:
            raise ValueError("limit must be 1..100")
        with self.store._lock:
            pending, seen, result = [memory_id], set(), []
            while pending and len(seen) < limit:
                current = pending.pop(0)
                if current in seen:
                    continue
                seen.add(current)
                evidence = self._evidence([current], include_sensitive)
                if evidence is None:
                    continue  # No traversing hidden/deleted records to reveal descendants.
                mem = self.store.get(current)
                from hunter_gatherer.memory.workspace_scope import scope_visible

                memory_scope = mem.metadata.get("workspace_id") or mem.metadata.get("scope") or "global"
                if not scope_visible(self.store, memory_scope, include_sensitive):
                    continue
                result.append({"memory_id": current, "text": mem.text, "source": mem.source,
                               "created_at": mem.created_at, "evidence": evidence,
                               "superseded": mem.is_superseded})
                for name in ("supersedes", "superseded_by"):
                    nxt = mem.metadata.get(name)
                    if isinstance(nxt, str) and nxt not in seen:
                        pending.append(nxt)
            return {"state": "available" if result else "unknown", "memories": result,
                    "bounded": bool(pending), "visited": len(seen)}
