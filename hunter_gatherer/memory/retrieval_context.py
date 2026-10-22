"""Request-owned reuse of query vectors; no embedding, index writes or public state."""
from __future__ import annotations

import asyncio
from contextlib import contextmanager
from contextvars import ContextVar
import json
import math
import threading

_context = ContextVar('retrieval_query_vector', default=None)


def _owner():
    try:
        task = asyncio.current_task()
    except RuntimeError:
        task = None
    return threading.get_ident(), task


@contextmanager
def retrieval_context(query, *, preserve_semantic_query=False):
    """Capture this query and explicitly registered ordinary retrieval variants."""
    token = _context.set({'query': query, 'owner': _owner(), 'value': None, 'variants': set(), 'exact': False, 'preserve_semantic_query': preserve_semantic_query})
    try:
        yield
    finally:
        _context.reset(token)


def embedding_query(query):
    """Resolve only an explicitly registered ordinary source-search variant.

    Lexical query terms remain untouched. Unrelated requirement queries retain
    their own semantic input, including when a context is inherited by a task.
    """
    state = _context.get()
    if (state is not None and state['owner'] == _owner() and state['preserve_semantic_query']
            and (query == state['query'] or query in state['variants'])):
        return state['query']
    return query


def register_query_variant(original_query, search_query):
    """Authorize an actual source-search derivation, bounded to eight variants.

    Requirement subqueries cannot register themselves against a different root.
    This records query text only in private, short-lived request state.
    """
    state = _context.get()
    if (state is None or state['owner'] != _owner() or original_query != state['query']
            or not isinstance(search_query, str) or not search_query or len(search_query) > 16000):
        return
    if len(state['variants']) < 8:
        state['variants'].add(search_query)


def _norm(value):
    if not isinstance(value, (list, tuple)) or not value:
        return None
    if any(type(x) not in (int, float) for x in value):
        return None
    try:
        norm = math.hypot(*value)
    except (OverflowError, TypeError, ValueError):
        return None
    # hypot propagates non-finite components. Checking its result avoids a
    # separate Python finite-check loop over every stored vector component.
    if not math.isfinite(norm) or not norm:
        return None
    return norm


def _vector(value):
    norm = _norm(value)
    if norm is None:
        return None
    return tuple(x / norm for x in value)


def capture_query_vector(query, vector, fingerprint):
    """Copy a valid vector already computed by ordinary retrieval, if owned."""
    state = _context.get()
    if state is None or state['owner'] != _owner() or (query != state['query'] and query not in state['variants']):
        return
    if not isinstance(fingerprint, str) or not fingerprint or fingerprint.startswith('hash'):
        return
    normalized = _vector(vector)
    if normalized is not None and (query == state['query'] or not state['exact']):
        state['value'] = (fingerprint, normalized)
        state['exact'] = query == state['query']


def score_existing_chunks(store, query, chunk_ids, *, include_sensitive=False):
    """Return chunk-ID -> cosine or None, for at most 32 exact visible chunks.

    Similarity uses an existing ordinary query representation (possibly expanded),
    solely as a retrieval ordering signal, never original-query support or truth. Missing,
    stale and hidden sources remain unavailable; vectors never leave this helper.
    """
    if not isinstance(chunk_ids, (list, tuple)) or len(chunk_ids) > 32 or any(not isinstance(x, str) for x in chunk_ids):
        raise ValueError('chunk_ids must contain at most 32 exact chunk IDs')
    if type(include_sensitive) is not bool:
        raise ValueError('include_sensitive must be a boolean')
    result = dict.fromkeys(chunk_ids)
    state = _context.get()
    if not result or state is None or state['owner'] != _owner() or state['query'] != query or state['value'] is None:
        return result
    fingerprint, query_vector = state['value']
    try:
        if store.embedder.fingerprint() != fingerprint:
            return result
    except (AttributeError, TypeError):
        return result
    from hunter_gatherer.memory.documents import DocumentArchive
    # Reuse exact-source validation and privacy rules, without constructing an
    # archive (whose initialization can install derived search indexes).
    clause = DocumentArchive._visible(include_sensitive, latest=False, exact=True)
    with store._lock:
        rows = store._sqlite.execute(
            'SELECT c.id,c.vector,c.embedder_fingerprint FROM archive_chunks c '
            'JOIN archive_documents d ON d.id=c.document_id JOIN archive_sections s '
            'ON s.id=c.section_id AND s.document_id=c.document_id '
            'AND c.start>=0 AND c.end>c.start AND c.end<=length(s.text) '
            'AND substr(s.text,c.start+1,c.end-c.start)=c.text '
            f'WHERE {clause} AND c.id IN ({",".join("?" for _ in result)})', tuple(result)).fetchall()
    for row in rows:
        if row['embedder_fingerprint'] != fingerprint:
            continue
        try:
            stored = json.loads(row['vector'])
            norm = _norm(stored)
        except (TypeError, ValueError):
            continue
        if norm is not None and len(stored) == len(query_vector):
            # Python 3.12+ provides a C dot product. Keep the supported 3.11
            # fallback and avoid allocating a normalized tuple for every row.
            dot = getattr(math, 'sumprod', None)
            try:
                score = (dot(stored, query_vector) if dot else
                         math.fsum(x*y for x, y in zip(stored, query_vector, strict=True))) / norm
            except (OverflowError, ValueError):
                continue
            if math.isfinite(score):
                result[row['id']] = max(-1., min(1., score))
    return result
