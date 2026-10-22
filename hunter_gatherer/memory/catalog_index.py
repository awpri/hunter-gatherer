"""Derived exact-token catalog postings; stale/unavailable indexes fall back.

Only metadata is indexed. Original files, source prose, vectors and models are
never read here. Callers prepare outside read snapshots; discovery is read-only.
"""
import hashlib
import json
import sqlite3

from hunter_gatherer.memory.document_visibility import visible_document_clause
from hunter_gatherer.memory.library_health import _read_snapshot

_OBJECTS = frozenset({"archive_catalog_state", "archive_catalog_terms", "archive_catalog_dirty",
                      "archive_catalog_meta", "archive_catalog_insert", "archive_catalog_update", "archive_catalog_delete"})
DOCUMENT_TRIGGERS = {
    'archive_catalog_insert': 'CREATE TRIGGER archive_catalog_insert AFTER INSERT ON archive_documents BEGIN INSERT OR IGNORE INTO archive_catalog_dirty VALUES(new.rowid); END',
    'archive_catalog_update': 'CREATE TRIGGER archive_catalog_update AFTER UPDATE ON archive_documents BEGIN INSERT OR IGNORE INTO archive_catalog_dirty VALUES(old.rowid); INSERT OR IGNORE INTO archive_catalog_dirty VALUES(new.rowid); END',
    'archive_catalog_delete': 'CREATE TRIGGER archive_catalog_delete AFTER DELETE ON archive_documents BEGIN INSERT OR IGNORE INTO archive_catalog_dirty VALUES(old.rowid); END',
}
_DDL = (
    "CREATE TABLE IF NOT EXISTS archive_catalog_state (document_rowid INTEGER PRIMARY KEY, valid INTEGER NOT NULL, omitted INTEGER NOT NULL)",
    "CREATE TABLE IF NOT EXISTS archive_catalog_terms (token TEXT NOT NULL, document_rowid INTEGER NOT NULL, PRIMARY KEY(token,document_rowid)) WITHOUT ROWID",
    "CREATE INDEX IF NOT EXISTS archive_catalog_terms_document ON archive_catalog_terms(document_rowid)",
    "CREATE TABLE IF NOT EXISTS archive_catalog_dirty (document_rowid INTEGER PRIMARY KEY)",
    "CREATE TABLE IF NOT EXISTS archive_catalog_meta (key TEXT PRIMARY KEY, value TEXT NOT NULL)",
)


def _signature():
    from hunter_gatherer.memory.document_discovery import _LIMITS
    from hunter_gatherer.memory.evidence import _STOP_WORDS

    contract = ["catalog-exact-token-v1:nfc-casefold-word-min2", _LIMITS, sorted(_STOP_WORDS)]
    return hashlib.sha256(json.dumps(contract, sort_keys=True).encode()).hexdigest()


def _objects(db):
    return {row[0] for row in db.execute("SELECT name FROM sqlite_master WHERE name LIKE 'archive_catalog_%'")}


def _triggers_valid(db, *, require_all=False):
    found = dict(db.execute("SELECT name,sql FROM sqlite_schema WHERE type='trigger' AND name LIKE 'archive_catalog_%'"))
    return (not require_all or found.keys() >= DOCUMENT_TRIGGERS.keys()) and all(
        name not in found or found[name] == sql for name, sql in DOCUMENT_TRIGGERS.items())


def _hash(token):
    # Bounded keys preserve the Python matcher's exact token semantics rather
    # than silently substituting a different FTS tokenizer. Not encryption.
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def prepare_catalog_index(store):
    """Prepare derived rows atomically, without committing caller transactions."""
    from hunter_gatherer.memory.document_discovery import (
        _MATCH_FIELDS,
        _candidate,
        _catalog_rows,
        _tokens,
    )

    if not getattr(store.settings, "archive_catalog_index", False):
        return False
    with store._lock:
        db = store._sqlite
        if db.in_transaction or not db.execute("SELECT 1 FROM sqlite_master WHERE name='archive_documents'").fetchone():
            return False
        try:
            if _ready(db):
                return True
        except sqlite3.Error:
            pass
        if not _triggers_valid(db):
            return False
        db.execute("SAVEPOINT catalog_prepare")
        try:
            complete = _objects(db) >= _OBJECTS
            for statement in _DDL:
                db.execute(statement)
            for statement in DOCUMENT_TRIGGERS.values():
                db.execute(statement.replace("CREATE TRIGGER ", "CREATE TRIGGER IF NOT EXISTS ", 1))
            prior = db.execute("SELECT value FROM archive_catalog_meta WHERE key='signature'").fetchone()
            if not complete or prior is None or prior[0] != _signature():
                db.execute("DELETE FROM archive_catalog_terms")
                db.execute("DELETE FROM archive_catalog_state")
                db.execute("INSERT OR IGNORE INTO archive_catalog_dirty SELECT rowid FROM archive_documents")
            while batch := db.execute("SELECT document_rowid FROM archive_catalog_dirty LIMIT 128").fetchall():
                ids = [row[0] for row in batch]
                rows = {row["catalog_rowid"]: row for row in _catalog_rows(
                    store, include_sensitive=True, row_ids=ids, all_records=True)}
                for identity in ids:
                    db.execute("DELETE FROM archive_catalog_terms WHERE document_rowid=?", (identity,))
                    db.execute("DELETE FROM archive_catalog_state WHERE document_rowid=?", (identity,))
                    row = rows.get(identity)
                    if row is not None:
                        candidate = _candidate(row, include_sensitive=True)
                        valid = candidate is not None
                        omitted = valid and bool(candidate.get("source_metadata_warnings"))
                        db.execute("INSERT INTO archive_catalog_state VALUES(?,?,?)", (identity, int(valid), int(omitted)))
                        if valid:
                            tokens = set().union(*(_tokens(candidate[field] or "") for field in _MATCH_FIELDS))
                            db.executemany("INSERT INTO archive_catalog_terms VALUES(?,?)", [(_hash(token), identity) for token in tokens])
                    db.execute("DELETE FROM archive_catalog_dirty WHERE document_rowid=?", (identity,))
            db.execute("INSERT OR REPLACE INTO archive_catalog_meta VALUES('signature',?)", (_signature(),))
            db.execute("RELEASE catalog_prepare")
            return True
        except (sqlite3.Error, ValueError, TypeError, UnicodeError):
            db.execute("ROLLBACK TO catalog_prepare")
            db.execute("RELEASE catalog_prepare")
            return False


def _ready(db):
    if not _objects(db) >= _OBJECTS or not _triggers_valid(db, require_all=True):
        return False
    signature = db.execute("SELECT value FROM archive_catalog_meta WHERE key='signature'").fetchone()
    return (signature is not None and signature[0] == _signature()
            and db.execute("SELECT 1 FROM archive_catalog_dirty LIMIT 1").fetchone() is None
            and db.execute("SELECT (SELECT count(*) FROM archive_catalog_state)=(SELECT count(*) FROM archive_documents)").fetchone()[0])


def indexed_discovery(store, token_queries, limit, include_sensitive, *, offset=None):
    """Return the same catalog result fields as scanning, or None to scan."""
    from hunter_gatherer.memory.document_discovery import _candidate, _catalog_rows, _match

    terms = json.dumps([[number, _hash(token)] for number, query in enumerate(token_queries) for token in query])
    try:
        with _read_snapshot(store) as db:
            if not _ready(db):
                return None
            clause = visible_document_clause(include_sensitive)
            counts = db.execute(
                "SELECT count(*),coalesce(sum(i.valid=0),0),coalesce(sum(i.omitted),0) "
                "FROM archive_documents d JOIN archive_catalog_state i ON i.document_rowid=d.rowid WHERE " + clause).fetchone()
            ranking_sql = (
                "WITH q AS (SELECT json_extract(value,'$[0]') AS n,json_extract(value,'$[1]') AS token FROM json_each(?)),"
                "matches AS MATERIALIZED (SELECT d.rowid AS rid,d.id,q.n,t.token FROM q JOIN archive_catalog_terms t ON t.token=q.token "
                "JOIN archive_documents d ON d.rowid=t.document_rowid WHERE " + clause + "),"
                "per_query AS (SELECT rid,n,count(*) AS hits FROM matches GROUP BY rid,n),"
                "ranks AS (SELECT rid,max(hits) AS best FROM per_query GROUP BY rid),"
                "totals AS (SELECT rid,id,count(DISTINCT token) AS combined FROM matches GROUP BY rid,id) "
                "SELECT r.rid,t.id,r.best,t.combined,count(*) OVER () AS total FROM ranks r JOIN totals t ON t.rid=r.rid "
                "ORDER BY r.best DESC,t.combined DESC,t.id LIMIT ? OFFSET ?")
            ranked = db.execute(ranking_sql, (terms, limit, offset or 0)).fetchall()
            rows = {row["catalog_rowid"]: row for row in _catalog_rows(
                store, include_sensitive=include_sensitive, row_ids=[row["rid"] for row in ranked])} if ranked else {}
            candidates = []
            for rank in ranked:
                row = rows.get(rank["rid"])
                candidate = _candidate(row, include_sensitive=include_sensitive) if row is not None else None
                if candidate is None or _match(candidate, token_queries) != (rank["best"], rank["combined"]):
                    return None
                candidates.append(candidate)
            matching = ranked[0]["total"] if ranked else 0
            if not ranked and offset:
                first = db.execute(ranking_sql, (terms, 1, 0)).fetchone()
                matching = first["total"] if first else 0
            paging = {}
            if offset is not None:
                from hunter_gatherer.memory.document_discovery import _discovery_paging
                paging = _discovery_paging(offset, limit, matching, len(candidates))
            return {**paging, "candidates": candidates, "scanned_documents": counts[0],
                    "invalid_identity_documents": counts[1], "documents_with_omitted_metadata": counts[2],
                    "matching_documents": matching, "omitted_documents": matching - len(candidates)}
    except sqlite3.Error:
        return None


def indexed_browse(store, tokens, limit, offset, include_sensitive, without_passages):
    from hunter_gatherer.memory.document_discovery import _candidate, _catalog_rows, _match

    try:
        with _read_snapshot(store) as db:
            if not _ready(db):
                return None
            clause = visible_document_clause(include_sensitive)
            if without_passages:
                clause += " AND NOT EXISTS (SELECT 1 FROM archive_chunks c WHERE c.document_id=d.id)"
            counts = db.execute(
                "SELECT count(*),coalesce(sum(i.valid=0),0),coalesce(sum(i.omitted),0) "
                "FROM archive_documents d JOIN archive_catalog_state i ON i.document_rowid=d.rowid WHERE " + clause).fetchone()
            match = (" AND EXISTS (SELECT 1 FROM archive_catalog_terms t WHERE t.document_rowid=d.rowid "
                     "AND t.token IN (SELECT value FROM json_each(?)))") if tokens else ""
            arguments = [json.dumps([_hash(token) for token in tokens])] if tokens else []
            base = "FROM archive_documents d JOIN archive_catalog_state i ON i.document_rowid=d.rowid WHERE i.valid=1 AND " + clause + match
            total = db.execute("SELECT count(*) " + base, arguments).fetchone()[0]
            ranked = db.execute("SELECT d.rowid AS rid " + base + " ORDER BY d.created_at DESC,d.id LIMIT ? OFFSET ?",
                                [*arguments, limit, offset]).fetchall()
            rows = {row["catalog_rowid"]: row for row in _catalog_rows(
                store, include_sensitive=include_sensitive, row_ids=[row["rid"] for row in ranked])} if ranked else {}
            documents = []
            for rank in ranked:
                row = rows.get(rank["rid"])
                candidate = _candidate(row, include_sensitive=include_sensitive) if row is not None else None
                if candidate is None or tokens and _match(candidate, [tokens]) is None:
                    return None
                documents.append(candidate)
            return {"documents": documents, "total": total,
                    "next_offset": offset + limit if offset + limit < total else None,
                    "scanned_documents": counts[0], "invalid_identity_documents": counts[1],
                    "documents_with_omitted_metadata": counts[2]}
    except sqlite3.Error:
        return None


class _MaintenanceCancelled(Exception):
    pass


def refresh_catalog_batch(store, batch_size=32, cancelled=lambda: False):
    """Refresh bounded dirty rows in an existing compatible opt-in index.

    Never installs tables, changes originals, rebuilds the index, or commits a
    caller transaction. Busy readers/writers defer work. Diagnostics contain
    no identities, labels, paths, or exception messages.
    """
    from hunter_gatherer.memory.document_discovery import (
        _MATCH_FIELDS,
        _candidate,
        _catalog_rows,
        _tokens,
    )

    result = {"status": "disabled", "checked": 0, "processed": 0, "pending": None}
    if not getattr(store.settings, "archive_catalog_index", False):
        return result
    if cancelled():
        return dict(result, status="cancelled")
    if not store._lock.acquire(blocking=False):
        return dict(result, status="busy")
    try:
        db = store._sqlite
        if db.in_transaction:
            return dict(result, status="busy")
        if not _objects(db) >= _OBJECTS:
            return dict(result, status="not_installed")
        prior = db.execute("SELECT value FROM archive_catalog_meta WHERE key='signature'").fetchone()
        if prior is None or prior[0] != _signature() or not _triggers_valid(db, require_all=True):
            return dict(result, status="needs_rebuild")
        limit = max(1, min(128, int(batch_size)))
        timeout = db.execute("PRAGMA busy_timeout").fetchone()[0]
        db.execute("PRAGMA busy_timeout=50")
        try:
            db.execute("SAVEPOINT catalog_maintenance")
            try:
                # Reserve the writer before reading IDs to avoid another
                # connection changing a row between hydration and refresh.
                db.execute("DELETE FROM archive_catalog_dirty WHERE document_rowid IS NULL")
                batch = db.execute("SELECT document_rowid FROM archive_catalog_dirty LIMIT ?", (limit + 1,)).fetchall()
                ids = [row[0] for row in batch[:limit]]
                rows = {row["catalog_rowid"]: row for row in _catalog_rows(
                    store, include_sensitive=True, row_ids=ids, all_records=True)} if ids else {}
                for identity in ids:
                    if cancelled():
                        raise _MaintenanceCancelled()
                    db.execute("DELETE FROM archive_catalog_terms WHERE document_rowid=?", (identity,))
                    db.execute("DELETE FROM archive_catalog_state WHERE document_rowid=?", (identity,))
                    row = rows.get(identity)
                    if row is not None:
                        candidate = _candidate(row, include_sensitive=True)
                        valid = candidate is not None
                        omitted = valid and bool(candidate.get("source_metadata_warnings"))
                        db.execute("INSERT INTO archive_catalog_state VALUES(?,?,?)", (identity, int(valid), int(omitted)))
                        if valid:
                            tokens = set().union(*(_tokens(candidate[field] or "") for field in _MATCH_FIELDS))
                            db.executemany("INSERT INTO archive_catalog_terms VALUES(?,?)", [(_hash(token), identity) for token in tokens])
                    db.execute("DELETE FROM archive_catalog_dirty WHERE document_rowid=?", (identity,))
                if cancelled():
                    raise _MaintenanceCancelled()
                db.execute("RELEASE catalog_maintenance")
                return dict(result, status="refreshed" if ids else "idle", checked=len(ids),
                            processed=len(ids), pending=len(batch) > limit)
            except BaseException:
                db.execute("ROLLBACK TO catalog_maintenance")
                db.execute("RELEASE catalog_maintenance")
                raise
        finally:
            db.execute("PRAGMA busy_timeout=" + str(timeout))
    except _MaintenanceCancelled:
        return dict(result, status="cancelled")
    except sqlite3.OperationalError as exc:
        if getattr(exc, "sqlite_errorcode", None) in (sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED):
            return dict(result, status="busy")
        raise
    finally:
        store._lock.release()
