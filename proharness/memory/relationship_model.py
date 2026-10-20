"""Bounded projections of authored relationships; never identity resolution or truth.

Projection rows are hints only. Records are validated against sources and
dependencies; reuse is confined to one pinned read snapshot. Legacy backups
need no migration for read access.
"""
from __future__ import annotations

import asyncio
import json
import math
import threading
import unicodedata
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy
from datetime import datetime

PREDICATES = frozenset({'related_to', 'part_of', 'created_by', 'describes', 'cites',
    'same_as', 'uses', 'original_of', 'depicts', 'supersedes', 'depends_on',
    'constrained_by', 'supports', 'qualifies', 'contradicts', 'compatible_with'})
PROJECTIONS = {'document_entity_names', 'document_relationship_edges'}

_VALIDATED_RECORD_MEMO = ContextVar('relationship_validated_records', default=None)
_MEMO_LIMIT = 512
_NOT_ENTITY = object()


def _owner():
    # Context variables can be inherited by a child asyncio task. A shared
    # mutable memo must never follow that inheritance into concurrent work.
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    return threading.get_ident(), id(task) if task is not None else None


def _database_stamp(db):
    # total_changes alone misses DDL and commit/BEGIN followed by another
    # connection's writes. These cheap guards do not replace source validation.
    return (db.total_changes,
            db.execute('PRAGMA data_version').fetchone()[0],
            db.execute('PRAGMA schema_version').fetchone()[0])


@contextmanager
def validated_record_snapshot(store, *, diagnostics=None):
    """Memoize validated records only inside this one pinned read snapshot.

    The store lock and existing transaction helper preserve ordinary visibility
    semantics. Keys distinguish connection, record and privacy. Caller mutation,
    lost transaction or changed DB/schema disables reuse for the remaining scope.
    Nested scopes receive separate memos; finally clears even inherited contexts.
    """
    if diagnostics is not None and type(diagnostics) is not dict:
        raise ValueError('diagnostics must be a dictionary')
    stats = {'hits': 0, 'misses': 0, 'invalidations': 0, 'guard_queries': 0,
             'recursion_blocks': 0, 'uncached_at_capacity': 0, 'capacity': _MEMO_LIMIT}
    with _e()._read_snapshot(store) as db:
        memo = {'db': db, 'owner': _owner(), 'stamp': _database_stamp(db),
                'values': {}, 'active': set(), 'disabled': False, 'closed': False, 'stats': stats}
        stats['guard_queries'] += 2
        token = _VALIDATED_RECORD_MEMO.set(memo)
        try:
            yield db
        finally:
            memo['closed'] = True
            memo['values'].clear()
            memo['active'].clear()
            _VALIDATED_RECORD_MEMO.reset(token)
            if diagnostics is not None:
                diagnostics.update(stats)


def _request_memo(db):
    memo = _VALIDATED_RECORD_MEMO.get()
    if (memo is None or memo['db'] is not db or memo['owner'] != _owner()
            or memo['closed'] or memo['disabled']):
        return None
    if not db.in_transaction or db.total_changes != memo['stamp'][0]:
        memo['values'].clear()
        memo['disabled'] = True
        memo['stats']['invalidations'] += 1
        return None
    return memo


def _e():
    from proharness.memory import document_enrichment
    return document_enrichment


def _name(value):
    return ' '.join(unicodedata.normalize('NFKC', value).casefold().split())


def validate_relation(relation):
    e = _e()
    e._object('relation', relation, {'predicate'}, {'subject_key', 'subject_ref', 'object_key', 'object_ref'})
    if type(relation['predicate']) is not str or relation['predicate'] not in PREDICATES:
        e._fail('unsupported relationship predicate')
    for side in ('subject', 'object'):
        selectors = [side + suffix for suffix in ('_key', '_ref') if side + suffix in relation]
        if len(selectors) != 1:
            e._fail('Each relationship endpoint requires exactly one key or record reference')
        field = selectors[0]
        e._string(field, relation[field], 80 if field.endswith('_key') else 128)


def validate_v2_fields(record):
    e = _e()
    for field, bound in (('label', 200), ('entity_type', 80)):
        if field in record:
            if record['kind'] != 'entity':
                e._fail(field + ' is only valid on entities')
            e._string(field, record[field], bound)
    if 'attributes' in record:
        attrs = record['attributes']
        if record['kind'] != 'entity' or type(attrs) is not dict or len(attrs) > 16:
            e._fail('attributes must contain at most 16 primitive entity fields')
        for key, value in attrs.items():
            e._string('attribute key', key, 80)
            if type(value) is str:
                e._string('attribute value', value, 500)
            elif value is not None and type(value) not in (bool, int, float):
                e._fail('attribute values must be primitives')
            elif type(value) in (int, float) and (abs(value) > 1e15 or not math.isfinite(value)):
                e._fail('attribute numeric value out of bounds')
    for field, allowed in (('assertion_status', {'asserted', 'inferred', 'uncertain'}),
                           ('review_status', {'unreviewed', 'reviewed', 'disputed', 'rejected'})):
        if field in record and (type(record[field]) is not str or record[field] not in allowed):
            e._fail('Unsupported authored ' + field)
    dates = {}
    for field in ('valid_from', 'valid_to'):
        if field in record:
            e._string(field, record[field], 40)
            try:
                dates[field] = datetime.fromisoformat(record[field].replace('Z', '+00:00'))
            except ValueError:
                e._fail('Validity dates must use ISO-8601')
    if len(dates) == 2:
        try:
            if dates['valid_to'] < dates['valid_from']:
                e._fail('valid_to precedes valid_from')
        except TypeError:
            e._fail('Validity dates must use compatible timezone forms')
    refs = record.get('source_refs', [])
    if type(refs) is not list or len(refs) > 8:
        e._fail('source_refs must contain at most 8 exact references')
    for ref in refs:
        if type(ref) is dict and 'original_content_hash' in ref:
            from proharness.memory.original_file_reference import valid_original_reference
            if not valid_original_reference(ref):
                e._fail('Invalid exact original file/page reference')
            continue
        e._object('source_ref', ref, {'document_id', 'version', 'content_hash'}, {'chunk_id', 'page'})
        e._string('source_ref.document_id', ref['document_id'], 128)
        e._integer('source_ref.version', ref['version'], 1)
        for field in ('content_hash',):
            if field in ref and (type(ref[field]) is not str or not e.re.fullmatch('[0-9a-f]{64}', ref[field])):
                e._fail('source_ref hashes require lowercase SHA-256')
        if 'page' in ref:
            e._integer('source_ref.page', ref['page'], 1, 1000000)
        if 'chunk_id' in ref:
            e._string('source_ref.chunk_id', ref['chunk_id'], 128)
    validate_field_sources(record)


def validate_field_sources(record):
    """Bind authored entity fields only to its existing exact references.

    This checks reference membership, never whether a source entails a field.
    It deliberately performs no source lookup: normal anchors/dependencies must
    also validate before the enclosing record can be returned.
    """
    if 'field_sources' not in record:
        return
    e = _e()
    bindings = record['field_sources']
    if record['kind'] != 'entity' or type(bindings) is not list or not 1 <= len(bindings) <= 24:
        e._fail('field_sources requires 1 to 24 entity field bindings')
    # Local anchor document identity is supplied at dependency validation. Here
    # leave local citations to that phase; shape/field checks remain independent.
    seen = set()
    for binding in bindings:
        e._object('field_source', binding, {'field', 'citations'}, {'key'})
        field = binding['field']
        if type(field) is not str or field not in {'label', 'entity_type', 'alias', 'attribute'}:
            e._fail('Unsupported field_source field')
        key = binding.get('key')
        if field in {'label', 'entity_type'}:
            if 'key' in binding or field not in record:
                e._fail('label/entity_type binding requires that field and forbids key')
        else:
            e._string('field_source.key', key, 80)
            if field == 'alias' and key not in record.get('aliases', []):
                e._fail('field_source alias key is not an exact existing alias')
            if field == 'attribute' and key not in record.get('attributes', {}):
                e._fail('field_source attribute key is not an exact existing attribute')
        identity = (field, key)
        if identity in seen:
            e._fail('Duplicate field_source binding')
        seen.add(identity)
        citations = binding['citations']
        if type(citations) is not list or not 1 <= len(citations) <= 8:
            e._fail('field_source citations requires 1 to 8 exact citations')
        for citation in citations:
            e._string('field_source citation', citation, 300)
        if len(set(citations)) != len(citations):
            e._fail('Duplicate field_source citation')


def _validate_field_source_membership(record, document_id, source_media_types):
    """Pure membership check after ordinary exact sources have validated."""
    if 'field_sources' not in record:
        return
    from proharness.memory.original_file_reference import original_reference_citation
    from proharness.memory.original_page_reference import original_page_citation
    allowed = {f"[doc:{document_id}#{anchor['chunk_id']}]" for anchor in record['anchors']}
    for ref in record.get('source_refs', []):
        if 'original_content_hash' in ref:
            allowed.add(original_reference_citation(ref))
        else:
            if 'chunk_id' in ref:
                allowed.add(f"[doc:{ref['document_id']}#{ref['chunk_id']}]")
            if 'page' in ref and source_media_types.get(ref['document_id'], '').lower() == 'application/pdf':
                allowed.add(original_page_citation({'document_id': ref['document_id'],
                    'version': ref['version'], 'original_content_hash': ref['content_hash'], 'page': ref['page']}))
    if any(citation not in allowed for binding in record['field_sources'] for citation in binding['citations']):
        _e()._fail('field_source citation is not bound to an existing exact record reference')


def validate_dependencies(db, record, include_sensitive, *, document_id=None):
    e = _e()
    source_media_types = {}
    for ref in record.get('source_refs', []):
        if 'original_content_hash' in ref:
            from proharness.memory.original_file_reference import original_reference_row
            row = original_reference_row(db, ref)
            if row is None or (row['sensitive'] and not include_sensitive):
                e._fail('Referenced original not found or access excluded', 'source_unavailable', 404)
            continue
        src = e._source(db, ref['document_id'], include_sensitive, latest=False)
        source_media_types[src['id']] = src['media_type']
        if (src['version'], src['content_hash']) != (ref['version'], ref['content_hash']):
            e._fail('Referenced source version/hash changed', 'source_unavailable', 404)
        if 'chunk_id' in ref:
            chunk = db.execute('SELECT c.document_id,s.page FROM archive_chunks c JOIN archive_sections s ON s.id=c.section_id WHERE c.id=? AND s.document_id=c.document_id', (ref['chunk_id'],)).fetchone()
            if chunk is None or chunk['document_id'] != src['id'] or ('page' in ref and chunk['page'] != ref['page']):
                e._fail('Referenced chunk/page is unavailable', 'source_unavailable', 404)
        elif 'page' in ref and not db.execute('SELECT 1 FROM archive_sections WHERE document_id=? AND page=?', (src['id'], ref['page'])).fetchone():
            e._fail('Referenced page is unavailable', 'source_unavailable', 404)
    if 'field_sources' in record:
        if document_id is None:
            e._fail('Exact field source validation requires owning document identity')
        _validate_field_source_membership(record, document_id, source_media_types)
    for field in ('subject_ref', 'object_ref'):
        if field in record.get('relation', {}) and _get(db, record['relation'][field], include_sensitive, entity_only=True) is None:
            e._fail('Relationship endpoint not found or access excluded', 'source_unavailable', 404)


def _get_uncached(db, record_id, include_sensitive, entity_only=False):
    e = _e()
    if not e._available(db, e._TABLES):
        return None
    row = db.execute(e._SELECT + 'WHERE r.id=? AND b.revoked_at IS NULL', (record_id,)).fetchone()
    if row is None:
        return None
    if entity_only and row['kind'] != 'entity':
        return _NOT_ENTITY
    try:
        source = e._source(db, row['document_id'], include_sensitive, latest=False)
        return e._saved_record(db, row, source, include_sensitive=include_sensitive)
    except e.EnrichmentError:
        return None


def _get(db, record_id, include_sensitive, entity_only=False):
    memo = _request_memo(db)
    key = (id(db), record_id, include_sensitive)
    if memo is not None:
        if key in memo['active'] or len(memo['active']) >= 64:
            memo['stats']['recursion_blocks'] += 1
            return None
        if key in memo['values']:
            memo['stats']['guard_queries'] += 2
            if _database_stamp(db) == memo['stamp']:
                memo['stats']['hits'] += 1
                result = deepcopy(memo['values'][key])
                return None if result and entity_only and result['kind'] != 'entity' else result
            memo['values'].clear()
            memo['disabled'] = True
            memo['stats']['invalidations'] += 1
            memo = None
    if memo is None:
        result = _get_uncached(db, record_id, include_sensitive, entity_only=entity_only)
    else:
        memo['stats']['misses'] += 1
        memo['active'].add(key)
        try:
            result = _get_uncached(db, record_id, include_sensitive, entity_only=entity_only)
            if result is not _NOT_ENTITY:
                if len(memo['values']) < _MEMO_LIMIT:
                    memo['values'][key] = deepcopy(result)
                else:
                    memo['stats']['uncached_at_capacity'] += 1
        finally:
            memo['active'].discard(key)
    if result is _NOT_ENTITY:
        return None
    return None if result and entity_only and result['kind'] != 'entity' else result


def project_batch(db, batch_id):
    """Only called inside explicit enrichment writes; projections are rebuildable."""
    db.execute('CREATE TABLE IF NOT EXISTS document_entity_names (record_id TEXT NOT NULL,name TEXT NOT NULL,PRIMARY KEY(record_id,name))')
    db.execute('CREATE INDEX IF NOT EXISTS idx_document_entity_name ON document_entity_names(name,record_id)')
    db.execute('CREATE TABLE IF NOT EXISTS document_relationship_edges (record_id TEXT PRIMARY KEY,subject_id TEXT NOT NULL,predicate TEXT NOT NULL,object_id TEXT NOT NULL)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_document_relationship_subject ON document_relationship_edges(subject_id,predicate,record_id)')
    db.execute('CREATE INDEX IF NOT EXISTS idx_document_relationship_object ON document_relationship_edges(object_id,predicate,record_id)')
    rows = db.execute('SELECT id,record_key,kind,record_json FROM document_enrichment_records WHERE batch_id=?', (batch_id,)).fetchall()
    ids = {r['record_key']: r['id'] for r in rows if r['kind'] == 'entity'}
    for row in rows:
        record = json.loads(row['record_json'])
        if row['kind'] == 'entity':
            for value in [record.get('label', record['text']), *record.get('aliases', [])]:
                db.execute('INSERT OR IGNORE INTO document_entity_names VALUES (?,?)', (row['id'], _name(value)))
        elif row['kind'] == 'relationship':
            rel = record['relation']
            subject = rel.get('subject_ref') or ids.get(rel.get('subject_key'))
            obj = rel.get('object_ref') or ids.get(rel.get('object_key'))
            db.execute('INSERT OR REPLACE INTO document_relationship_edges VALUES (?,?,?,?)', (row['id'], subject, rel['predicate'], obj))


def _bounds(limit, include_sensitive):
    e = _e()
    e._integer('limit', limit, 1, 64)
    if type(include_sensitive) is not bool:
        e._fail('include_sensitive must be a boolean')


def _diagnostics(diagnostics, limit):
    if diagnostics is not None:
        if type(diagnostics) is not dict:
            _e()._fail('diagnostics must be a dictionary')
        diagnostics.update(scan_limit=1024, candidate_rows=0, truncated=False,
                           result_limit=limit, result_limit_reached=False)


def _scan(db, sql, params, diagnostics):
    rows = db.execute(sql + ' LIMIT 1025', params).fetchall()
    if diagnostics is not None:
        diagnostics.update(candidate_rows=min(len(rows), 1024), truncated=len(rows) > 1024)
    return rows[:1024]


def get_entity_record(store, record_id, *, include_sensitive=False):
    """Return one validated entity dict (record_id, batch_id, source refs), or None."""
    _bounds(1, include_sensitive)
    _e()._string('record_id', record_id, 128)
    with _e()._read_snapshot(store) as db:
        return _get(db, record_id, include_sensitive, entity_only=True)


def named_entity_seeds(store, names, *, limit=8, include_sensitive=False, diagnostics=None):
    _bounds(limit, include_sensitive)
    _diagnostics(diagnostics, limit)
    e = _e()
    if type(names) is not list or not 1 <= len(names) <= 8:
        e._fail('names must contain 1 to 8 names')
    for value in names:
        e._string('name', value, 200)
    with e._read_snapshot(store) as db:
        if not e._available(db, PROJECTIONS):
            return []
        params = sorted({_name(n) for n in names})
        rows = _scan(db, 'SELECT DISTINCT record_id FROM document_entity_names WHERE name IN (' + ','.join('?' for _ in params) + ') ORDER BY record_id', params, diagnostics)
        results = []
        for row in rows:
            record = _get(db, row[0], include_sensitive, entity_only=True)
            if record and any(_name(n) in params for n in [record.get('label', record['derived_text']), *record['aliases']]):
                results.append(record)
                if len(results) == limit:
                    if diagnostics is not None:
                        diagnostics["result_limit_reached"] = True
                    break
        return results


def entities_for_source(store, *, document_id=None, chunk_id=None, limit=16, include_sensitive=False, diagnostics=None):
    _bounds(limit, include_sensitive)
    _diagnostics(diagnostics, limit)
    e = _e()
    if document_id is None and chunk_id is None:
        e._fail('document_id or chunk_id is required')
    for value in (document_id, chunk_id):
        if value is not None:
            e._string('source id', value, 128)
    with e._read_snapshot(store) as db:
        if not e._available(db, e._TABLES):
            return []
        sql = 'SELECT DISTINCT r.id FROM document_enrichment_records r JOIN document_enrichment_batches b ON b.id=r.batch_id JOIN document_enrichment_anchors a ON a.record_id=r.id WHERE r.kind=\'entity\' AND b.revoked_at IS NULL'
        params = []
        if document_id is not None:
            sql += ' AND b.document_id=?'; params.append(document_id)
        if chunk_id is not None:
            sql += ' AND a.chunk_id=?'; params.append(chunk_id)
        results = []
        for row in _scan(db, sql + ' ORDER BY r.id', params, diagnostics):
            record = _get(db, row[0], include_sensitive, entity_only=True)
            if record:
                results.append(record)
                if len(results) == limit:
                    if diagnostics is not None:
                        diagnostics["result_limit_reached"] = True
                    break
        return results


def adjacent_edges(store, record_ids, *, predicates=None, direction='both', limit=32, include_sensitive=False, diagnostics=None):
    """Return validated relationship dicts plus subject_id/object_id, never merged names.

    Optional diagnostics exposes candidate scan truncation independently from the
    result limit. Empty results do not establish that a relationship is absent.
    """
    _bounds(limit, include_sensitive)
    _diagnostics(diagnostics, limit)
    e = _e()
    if type(record_ids) is not list or not 1 <= len(record_ids) <= 16:
        e._fail('record_ids must contain 1 to 16 entity ids')
    for value in record_ids:
        e._string('record_id', value, 128)
    if direction not in ('both', 'outgoing', 'incoming'):
        e._fail('Unsupported adjacency direction')
    if predicates is not None and (type(predicates) is not list or not 1 <= len(predicates) <= len(PREDICATES) or any(type(p) is not str or p not in PREDICATES for p in predicates)):
        e._fail('Unsupported adjacency predicates')
    with e._read_snapshot(store) as db:
        if not e._available(db, PROJECTIONS):
            return []
        ids = sorted({r for r in record_ids if _get(db, r, include_sensitive, entity_only=True)})
        if not ids:
            return []
        slots = ','.join('?' for _ in ids)
        fields = ['subject_id', 'object_id'] if direction == 'both' else ['subject_id' if direction == 'outgoing' else 'object_id']
        params = ids * len(fields)
        where = '(' + ' OR '.join(f'{field} IN ({slots})' for field in fields) + ')'
        if predicates:
            where += ' AND predicate IN (' + ','.join('?' for _ in predicates) + ')'; params += predicates
        results = []
        for row in _scan(db, 'SELECT * FROM document_relationship_edges WHERE ' + where + ' ORDER BY record_id', params, diagnostics):
            record = _get(db, row['record_id'], include_sensitive)
            if not record or record['kind'] != 'relationship':
                continue
            rel = record['relation']
            endpoints = {}
            for side in ('subject', 'object'):
                target = rel.get(side + '_ref')
                if target is None:
                    endpoint = db.execute('SELECT id FROM document_enrichment_records WHERE batch_id=? AND record_key=? AND kind=\'entity\'', (record['batch_id'], rel[side + '_key'])).fetchone()
                    target = endpoint[0] if endpoint else None
                endpoints[side + '_id'] = target
            # A corrupt or stale projection cannot invent a different edge.
            if (row['subject_id'], row['predicate'], row['object_id']) != (endpoints['subject_id'], rel['predicate'], endpoints['object_id']):
                continue
            results.append({**record, **endpoints})
            if len(results) == limit:
                if diagnostics is not None:
                    diagnostics["result_limit_reached"] = True
                break
        return results
