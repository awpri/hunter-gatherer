"""Exact original-file identities; reads yield metadata, never binary content."""
from __future__ import annotations

import re
import sqlite3

from proharness.memory.document_visibility import document_reference_states
from proharness.memory.library_health import _read_snapshot
from proharness.memory.original_page_reference import (
    original_page_citation,
    original_page_reference_row,
    resolve_original_page_reference,
    valid_original_page_reference,
)

_FIELDS = {'document_id', 'version', 'original_content_hash'}
_CITATION = re.compile(r'\[file:([^\s@:\[\]\x00]{1,100})@([1-9][0-9]*):([0-9a-f]{64})\]')
_LABELS = {'title': 200, 'author': 200, 'document_date': 100, 'category': 128, 'media_type': 120}


def valid_original_file_reference(ref):
    return (type(ref) is dict and set(ref) == _FIELDS
            and valid_original_page_reference({**ref, 'page': 1}))


def original_file_citation(ref):
    if not valid_original_file_reference(ref):
        raise ValueError('Invalid exact original-file reference.')
    return f"[file:{ref['document_id']}@{ref['version']}:{ref['original_content_hash']}]"


def parse_original_file_citation(label):
    if type(label) is not str or len(label) > 240:
        return None
    match = _CITATION.fullmatch(label)
    if not match:
        return None
    document, version, digest = match.groups()
    ref = {'document_id': document, 'version': int(version), 'original_content_hash': digest}
    return ref if valid_original_file_reference(ref) else None


def original_file_reference_row(db, ref, *, document_states=None):
    if not valid_original_file_reference(ref):
        return None
    projections = [f"CASE WHEN typeof({field})='text' AND length(CAST({field} AS BLOB))<={limit} "
                   f"THEN {field} ELSE NULL END AS {field}" for field, limit in _LABELS.items()]
    try:
        row = db.execute('SELECT id,version,content_hash,' + ','.join(projections)
                         + ', (SELECT MAX(newer.version) FROM archive_documents newer '
                         'WHERE newer.source_identity=d.source_identity) AS latest_version '
                         'FROM archive_documents d WHERE id=? AND version=? AND content_hash=? LIMIT 1',
                         (ref['document_id'], ref['version'], ref['original_content_hash'])).fetchone()
        if row is None or type(row['latest_version']) is not int or row['latest_version'] < ref['version']:
            return None
        states = document_reference_states(db, document_ids=[ref['document_id']]) if document_states is None else document_states
        state = states.get(ref['document_id'])
        return {**dict(row), 'sensitive': state['sensitive']} if state and state['available'] else None
    except (sqlite3.DatabaseError, KeyError, TypeError):
        return None


def resolve_original_file_reference(store, ref, include_sensitive=False):
    if type(include_sensitive) is not bool:
        return None
    with _read_snapshot(store) as db:
        row = original_file_reference_row(db, ref)
    if row is None or (row['sensitive'] and not include_sensitive):
        return None
    arguments = {'document_id': ref['document_id'], 'max_chars': 2000}
    if include_sensitive:
        arguments['include_sensitive'] = True
    return {'kind': 'original_file', 'citation': original_file_citation(ref), 'source_reference': dict(ref),
            **{field: row[field] for field in _LABELS}, 'version': ref['version'],
            'is_latest': row['latest_version'] == ref['version'], 'latest_version': row['latest_version'],
            'source_uri': f"proharness://documents/{ref['document_id']}",
            'followup': {'tool': 'proharness_get_document', 'arguments': arguments},
            'expected_source_reference': dict(ref), 'availability': 'unchecked',
            'content_policy': 'Metadata-only original-file reference, not a quotation or verified claim. '
                              'This read does not verify original bytes or preview availability. '
                              'Inspect the exact original; compare its document/version/hash with this reference.'}


def valid_original_reference(ref):
    return valid_original_file_reference(ref) or valid_original_page_reference(ref)


def original_reference_citation(ref):
    return original_file_citation(ref) if valid_original_file_reference(ref) else original_page_citation(ref)


def original_reference_row(db, ref, *, document_states=None):
    resolver = original_file_reference_row if valid_original_file_reference(ref) else original_page_reference_row
    return resolver(db, ref, document_states=document_states)


def resolve_original_reference(store, ref, include_sensitive=False):
    resolver = resolve_original_file_reference if valid_original_file_reference(ref) else resolve_original_page_reference
    return resolver(store, ref, include_sensitive)
