"""Persistent lexical candidates and local SQLite vector distances for archives.

FTS is derived data; exact quotations remain in archive_sections/archive_chunks.
Filtering and span validation happen before candidate limits on both channels.
"""
import json
import math
import sqlite3

_JOIN = """FROM archive_chunks c JOIN archive_documents d ON d.id=c.document_id
JOIN archive_sections s ON s.id=c.section_id AND s.document_id=c.document_id
AND c.start>=0 AND c.end>c.start AND c.end<=length(s.text)
AND substr(s.text,c.start+1,c.end-c.start)=c.text"""
_SELECT = """c.*,s.page,s.section,d.title,d.author,d.document_date,d.category,
d.sensitive,d.version,d.warnings,
(SELECT MAX(v.version) FROM archive_documents v WHERE v.source_identity=d.source_identity) AS latest_version"""


# Shared with backup validation: exact known SQL, restricted to title updates.
ARCHIVE_FTS_TITLE_TRIGGER_SQL = 'CREATE TRIGGER archive_fts_title AFTER UPDATE OF title ON archive_documents BEGIN\n        UPDATE archive_chunks_fts SET title=new.title WHERE chunk_id IN (SELECT id FROM archive_chunks WHERE document_id=new.id); END'

def ensure_index(db):
    """Create/backfill atomically without committing a caller-owned transaction."""
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='archive_chunks_fts'").fetchone():
        return True
    owns = not db.in_transaction
    db.execute('SAVEPOINT archive_search_init')
    try:
        db.execute('UPDATE archive_documents SET version=version WHERE 0')
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='archive_chunks_fts'").fetchone():
            db.execute('RELEASE archive_search_init')
            return True
        db.execute('CREATE VIRTUAL TABLE archive_chunks_fts USING fts5(chunk_id UNINDEXED, title, section, text, tokenize="unicode61")')
        db.execute('INSERT INTO archive_chunks_fts(rowid,chunk_id,title,section,text) SELECT c.rowid,c.id,d.title,s.section,c.text FROM archive_chunks c JOIN archive_documents d ON d.id=c.document_id JOIN archive_sections s ON s.id=c.section_id')
        db.execute('''CREATE TRIGGER archive_fts_insert AFTER INSERT ON archive_chunks BEGIN
        INSERT INTO archive_chunks_fts(rowid,chunk_id,title,section,text)
        SELECT new.rowid,new.id,d.title,s.section,new.text FROM archive_documents d,archive_sections s
        WHERE d.id=new.document_id AND s.id=new.section_id; END''')
        db.execute('''CREATE TRIGGER archive_fts_delete AFTER DELETE ON archive_chunks BEGIN
        DELETE FROM archive_chunks_fts WHERE rowid=old.rowid; END''')
        db.execute('''CREATE TRIGGER archive_fts_update AFTER UPDATE OF id,document_id,section_id,text ON archive_chunks BEGIN
        DELETE FROM archive_chunks_fts WHERE rowid=old.rowid;
        INSERT INTO archive_chunks_fts(rowid,chunk_id,title,section,text)
        SELECT new.rowid,new.id,d.title,s.section,new.text FROM archive_documents d,archive_sections s
        WHERE d.id=new.document_id AND s.id=new.section_id; END''')
        db.execute(ARCHIVE_FTS_TITLE_TRIGGER_SQL)
        db.execute('''CREATE TRIGGER archive_fts_section AFTER UPDATE OF section ON archive_sections BEGIN
        UPDATE archive_chunks_fts SET section=new.section WHERE chunk_id IN (SELECT id FROM archive_chunks WHERE section_id=new.id); END''')
        db.execute('RELEASE archive_search_init')
    except sqlite3.OperationalError as exc:
        db.execute('ROLLBACK TO archive_search_init')
        db.execute('RELEASE archive_search_init')
        if 'no such module: fts5' in str(exc).lower():
            return False
        raise
    except Exception:
        db.execute('ROLLBACK TO archive_search_init')
        db.execute('RELEASE archive_search_init')
        raise
    if owns:
        db.commit()
    return True


def ensure_vectors(db):
    """Persist native float32 blobs; B-tree narrows fingerprint/dimension joins.

    Exact native distance scans preserve pre-limit metadata filters. This is
    deliberately not an ANN index: vec0 KNN cannot safely prefilter arbitrary
    archive version/access/span conditions without candidate widening.
    """
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='archive_chunk_vectors'").fetchone():
        return
    owns = not db.in_transaction
    db.execute('SAVEPOINT archive_vectors_init')
    try:
        db.execute('UPDATE archive_documents SET version=version WHERE 0')
        if db.execute("SELECT 1 FROM sqlite_master WHERE name='archive_chunk_vectors'").fetchone():
            db.execute('RELEASE archive_vectors_init')
            return
        db.execute('CREATE TABLE archive_chunk_vectors(chunk_id TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, dimension INTEGER NOT NULL, embedding BLOB NOT NULL)')
        db.execute('CREATE INDEX archive_vectors_model ON archive_chunk_vectors(fingerprint,dimension,chunk_id)')
        db.execute('CREATE TABLE archive_vector_dirty(chunk_id TEXT PRIMARY KEY)')
        db.execute('INSERT INTO archive_vector_dirty SELECT id FROM archive_chunks')
        for event, name in [('INSERT', 'insert'), ('UPDATE OF vector,embedder_fingerprint', 'update')]:
            db.execute(f"""CREATE TRIGGER archive_vec_{name} AFTER {event} ON archive_chunks BEGIN
            INSERT OR IGNORE INTO archive_vector_dirty VALUES(new.id); END""")
        db.execute('CREATE TRIGGER archive_vec_delete AFTER DELETE ON archive_chunks BEGIN DELETE FROM archive_chunk_vectors WHERE chunk_id=old.id; DELETE FROM archive_vector_dirty WHERE chunk_id=old.id; END')
        db.execute('RELEASE archive_vectors_init')
    except Exception:
        db.execute('ROLLBACK TO archive_vectors_init')
        db.execute('RELEASE archive_vectors_init')
        raise
    if owns:
        db.commit()


def sync_vectors(db):
    """Refresh only changed chunks. Writers do not need extension functions."""
    if not db.execute('SELECT 1 FROM archive_vector_dirty LIMIT 1').fetchone():
        return
    owns = not db.in_transaction
    db.execute('SAVEPOINT archive_vectors_sync')
    try:
        db.execute('DELETE FROM archive_chunk_vectors WHERE chunk_id IN (SELECT chunk_id FROM archive_vector_dirty)')
        # Bound conversion to dirty rows; malformed data cannot hide other
        # eligible vectors or make normal archive writes depend on sqlite_vec.
        cursor = db.execute('SELECT c.id,c.embedder_fingerprint,c.vector FROM archive_chunks c JOIN archive_vector_dirty p ON p.chunk_id=c.id WHERE c.vector IS NOT NULL')
        while rows := cursor.fetchmany(128):
            for row in rows:
                try:
                    vector = json.loads(row['vector'])
                    if not isinstance(vector, list) or not vector or not all(isinstance(x, (int,float)) and math.isfinite(x) for x in vector):
                        continue
                    db.execute('INSERT INTO archive_chunk_vectors VALUES(?,?,?,vec_f32(?))', (row['id'], row['embedder_fingerprint'], len(vector), row['vector']))
                except (ValueError, TypeError):
                    continue
        db.execute('DELETE FROM archive_vector_dirty')
        db.execute('RELEASE archive_vectors_sync')
    except Exception:
        db.execute('ROLLBACK TO archive_vectors_sync')
        db.execute('RELEASE archive_vectors_sync')
        raise
    if owns:
        db.commit()


def load_vec(db):
    try:
        db.execute("SELECT vec_distance_cosine('[1,0]','[1,0]')").fetchone()
        return True
    except sqlite3.OperationalError:
        try:
            import sqlite_vec
            db.enable_load_extension(True)
            try:
                sqlite_vec.load(db)
            finally:
                db.enable_load_extension(False)
            return True
        except (ImportError, AttributeError, sqlite3.Error):
            return False


def candidates(archive, terms, vector, fingerprint, include_sensitive, document_ids, limit):
    db = archive.store._sqlite
    clause = archive._visible(include_sensitive)
    args = []
    if document_ids is not None:
        if not document_ids:
            return [], [], 'empty_scope'
        clause += ' AND c.document_id IN (' + ','.join('?' for _ in document_ids) + ')'
        args.extend(document_ids)
    lexical = []
    if terms and archive._fts_available:
        match = ' OR '.join('"' + term.replace('"', '""') + '"' for term in sorted(terms))
        lexical = db.execute(f'''SELECT {_SELECT}, -bm25(archive_chunks_fts,0,2,1.5,1) AS lexical
        {_JOIN} JOIN archive_chunks_fts ON archive_chunks_fts.rowid=c.rowid
        WHERE archive_chunks_fts MATCH ? AND {clause}
        ORDER BY bm25(archive_chunks_fts,0,2,1.5,1),d.id,c.ordinal LIMIT ?''',
        [match, *args, limit]).fetchall()
    semantic, backend = [], 'unavailable'
    if vector is not None and archive._native_vectors:
        sync_vectors(db)
        try:
            rows = db.execute(f'''SELECT {_SELECT},
            1-vec_distance_cosine(v.embedding,vec_f32(?)) AS similarity
            {_JOIN} JOIN archive_chunk_vectors v ON v.chunk_id=c.id
            WHERE {clause} AND v.fingerprint=? AND v.dimension=?
            AND c.embedder_fingerprint=v.fingerprint AND c.vector IS NOT NULL
            ORDER BY similarity DESC,d.id,c.ordinal LIMIT ?''',
            [json.dumps(vector), *args, fingerprint, len(vector), limit]).fetchall()
            semantic = [row for row in rows if row['similarity'] is not None and math.isfinite(row['similarity']) and row['similarity'] >= .35]
            backend = 'sqlite_vec_exact_filtered_scan'
        except (sqlite3.Error, ValueError):
            backend = 'invalid_vectors_lexical_fallback'
    return lexical, semantic, backend


def native_scores(archive, query_vector, fingerprint, include_sensitive, document_ids):
    """All eligible exact similarities for the established weighted scorer."""
    db = archive.store._sqlite
    sync_vectors(db)
    clause, args = archive._visible(include_sensitive), []
    if document_ids is not None:
        if not document_ids:
            return {}
        clause += ' AND c.document_id IN (' + ','.join('?' for _ in document_ids) + ')'
        args.extend(document_ids)
    rows = db.execute(f'''SELECT c.id,v.dimension,
        CASE WHEN v.dimension=? THEN 1-vec_distance_cosine(v.embedding,vec_f32(?)) END AS similarity
        {_JOIN} LEFT JOIN archive_chunk_vectors v ON v.chunk_id=c.id
        AND v.fingerprint=? AND v.fingerprint=c.embedder_fingerprint
        WHERE {clause}''', [len(query_vector), json.dumps(query_vector), fingerprint, *args]).fetchall()
    return {row['id']: (row['dimension'], row['similarity']) for row in rows}
