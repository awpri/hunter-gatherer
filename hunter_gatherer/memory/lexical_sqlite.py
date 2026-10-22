"""Persistent memory FTS candidates; SQLite rows remain authoritative."""
from __future__ import annotations

import logging
import re
import sqlite3

from hunter_gatherer.memory.schemas import Memory

log = logging.getLogger(__name__)


def ensure_memory_fts(store) -> bool:
    """Create/backfill once, atomically with triggers, including concurrent opens."""
    db = store._sqlite
    with store._lock:
        db.execute("SAVEPOINT memory_fts_init")
        try:
            # Obtain the writer lock before checking migration state.
            db.execute("UPDATE store_meta SET value=value WHERE key='memory_fts_version'")
            exists = db.execute("SELECT 1 FROM sqlite_master WHERE name='memory_fts_members'").fetchone()
            db.execute("CREATE VIRTUAL TABLE IF NOT EXISTS memory_fts USING fts5(text, content='memories', content_rowid='rowid', tokenize='unicode61')")
            db.execute("CREATE TABLE IF NOT EXISTS memory_fts_members(rowid INTEGER PRIMARY KEY)")
            if not exists:
                # v2 marker migration also repairs older REPLACE-trigger postings.
                for name in ('insert', 'delete', 'update', 'replace'):
                    db.execute(f"DROP TRIGGER IF EXISTS memory_fts_{name}")
            # A marker makes FTS delete idempotent across REPLACE's implicit
            # DELETE (which fires only with recursive_triggers=ON) and UPSERT.
            db.execute("""CREATE TRIGGER IF NOT EXISTS memory_fts_replace BEFORE INSERT ON memories BEGIN
                INSERT INTO memory_fts(memory_fts,rowid,text)
                    SELECT 'delete',m.rowid,m.text FROM memories m JOIN memory_fts_members f ON f.rowid=m.rowid WHERE m.id=new.id;
                DELETE FROM memory_fts_members WHERE rowid IN (SELECT rowid FROM memories WHERE id=new.id);
                END""")
            db.execute("""CREATE TRIGGER IF NOT EXISTS memory_fts_insert AFTER INSERT ON memories BEGIN
                INSERT INTO memory_fts(rowid,text) VALUES(new.rowid,new.text);
                INSERT OR REPLACE INTO memory_fts_members(rowid) VALUES(new.rowid); END""")
            db.execute("""CREATE TRIGGER IF NOT EXISTS memory_fts_delete AFTER DELETE ON memories BEGIN
                INSERT INTO memory_fts(memory_fts,rowid,text) SELECT 'delete',old.rowid,old.text WHERE EXISTS(SELECT 1 FROM memory_fts_members WHERE rowid=old.rowid);
                DELETE FROM memory_fts_members WHERE rowid=old.rowid; END""")
            db.execute("""CREATE TRIGGER IF NOT EXISTS memory_fts_update AFTER UPDATE ON memories
                WHEN old.text IS NOT new.text OR old.rowid != new.rowid
                     OR NOT EXISTS(SELECT 1 FROM memory_fts_members WHERE rowid=old.rowid)
                BEGIN
                INSERT INTO memory_fts(memory_fts,rowid,text) SELECT 'delete',old.rowid,old.text WHERE EXISTS(SELECT 1 FROM memory_fts_members WHERE rowid=old.rowid);
                DELETE FROM memory_fts_members WHERE rowid=old.rowid;
                INSERT INTO memory_fts(rowid,text) VALUES(new.rowid,new.text);
                INSERT OR REPLACE INTO memory_fts_members(rowid) VALUES(new.rowid); END""")
            if not exists:
                db.execute("INSERT INTO memory_fts(memory_fts) VALUES('rebuild')")
                db.execute("INSERT OR REPLACE INTO memory_fts_members(rowid) SELECT rowid FROM memories")
            db.execute("RELEASE SAVEPOINT memory_fts_init")
            return True
        except sqlite3.OperationalError as exc:
            db.execute("ROLLBACK TO SAVEPOINT memory_fts_init")
            db.execute("RELEASE SAVEPOINT memory_fts_init")
            if 'no such module: fts5' not in str(exc).lower():
                raise
            log.warning("SQLite FTS5 unavailable; memory retrieval uses dense candidates")
            return False
        except Exception:
            db.execute("ROLLBACK TO SAVEPOINT memory_fts_init")
            db.execute("RELEASE SAVEPOINT memory_fts_init")
            raise


def lexical_candidates(store, query: str, limit: int, filters=None):
    # Quoted tokens prevent MATCH syntax/operators and malformed punctuation.
    tokens = list(dict.fromkeys(re.findall(r'[^\W_]+', query, re.UNICODE)))[:64]
    if not tokens or limit <= 0:
        return []
    match = ' OR '.join('"' + token + '"' for token in tokens)
    db = store._sqlite
    def eligible(*row):
        return int(Memory.from_sql_row(row).is_retrievable(filters, include_superseded=True))
    db.create_function('memory_fts_eligible', 11, eligible)
    rows = db.execute("""
        SELECT m.id FROM memory_fts JOIN memories m ON m.rowid=memory_fts.rowid
        WHERE memory_fts MATCH ? AND memory_fts_eligible(
            m.id,m.text,m.type,m.tags,m.importance,m.created_at,m.last_accessed,
            m.access_count,m.source,m.metadata,m.deleted_at)
        ORDER BY bm25(memory_fts), m.id LIMIT ?
    """, (match, limit)).fetchall()
    return [row[0] for row in rows]
