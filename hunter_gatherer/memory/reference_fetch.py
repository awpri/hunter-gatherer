"""Read-only bounded resolution of known citations; source text is untrusted."""
import re
import sqlite3

from hunter_gatherer.memory.archive_context import _ExistingArchiveReader
from hunter_gatherer.memory.document_handoff import (
    document_handoff_descriptor,
    without_original_page_hint,
)
from hunter_gatherer.memory.original_file_reference import (
    parse_original_file_citation,
    resolve_original_reference,
)
from hunter_gatherer.memory.original_page_reference import parse_original_page_citation
from hunter_gatherer.memory.output_budget import budget_report, fits, validate_budget
from hunter_gatherer.memory.retrieval_feedback import current_memory
from hunter_gatherer.memory.source_metadata import memory_source_descriptor
from hunter_gatherer.memory.workspace_scope import scope_visible

_ID = r'(?:[0-9a-f]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})'
_EXACT = re.compile(_ID)
_MEM = re.compile(r'\[mem:([0-9a-f]{6}[0-9a-f-]{0,30})\]')
_DOC = re.compile(r'\[doc:(' + _ID + r')#(' + _ID + r')\]')


def fetch_references(store, refs, *, max_tokens=4096, include_sensitive=False, store_identity=None):
    """Statuses correspond to input positions; duplicate bodies appear once."""
    validate_budget(max_tokens)
    if type(include_sensitive) is not bool:
        raise ValueError('include_sensitive must be boolean')
    if not isinstance(refs, list) or not 1 <= len(refs) <= 32:
        raise ValueError('refs must be a list of 1 to 32 references')
    result = {'statuses': ['invalid'] * len(refs), 'sources': []}
    if store_identity is not None:
        result['store'] = store_identity
    from hunter_gatherer.memory.library_health import _read_snapshot

    with _read_snapshot(store):
        tables = {r[0] for r in store._sqlite.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        archive = _ExistingArchiveReader(store)
        groups = {}
        for i, ref in enumerate(refs):
            if not isinstance(ref, str) or len(ref) > 240:
                continue
            page = parse_original_page_citation(ref) or parse_original_file_citation(ref)
            doc, mem = _DOC.fullmatch(ref), _MEM.fullmatch(ref)
            if mem and '-' in mem[1] and any(
                (char == '-') != (position in {8, 13, 18, 23})
                for position, char in enumerate(mem[1])
            ):
                continue
            if mem and '-' not in mem[1] and len(mem[1]) > 32:
                continue
            source = None
            if page:
                result['statuses'][i] = 'unavailable'
                source = resolve_original_reference(store, page, include_sensitive)
            elif doc:
                result['statuses'][i] = 'unavailable'
                if {'archive_documents', 'archive_chunks', 'archive_sections'} <= tables:
                    try:
                        hit = archive.get_chunk(doc[2], include_sensitive=include_sensitive)
                        if not hit.get('error') and hit.get('document_id') == doc[1]:
                            source = document_handoff_descriptor(store, hit, include_sensitive=include_sensitive)
                    except (ValueError, TypeError, KeyError, sqlite3.DatabaseError):
                        source = None
            elif mem or _EXACT.fullmatch(ref):
                result['statuses'][i] = 'unavailable'
                key = mem[1] if mem else ref
                if _EXACT.fullmatch(key):
                    ids = [key]
                else:
                    # Hidden rows count toward ambiguity, but IDs never escape.
                    # Hex/UUID prefixes have an ASCII successor. Use the
                    # primary-key range rather than scanning substr(id) for
                    # every reference in a batch; retain hidden/tombstone rows.
                    upper = key[:-1] + chr(ord(key[-1]) + 1)
                    ids = [r[0] for r in store._sqlite.execute(
                        'SELECT id FROM memories WHERE id >= ? AND id < ? LIMIT 2', (key, upper))]
                if len(ids) == 1:
                    item = current_memory(store, ids[0], include_sensitive)
                    if item is not None:
                        scope = item.metadata.get('scope')
                        if scope is None or (isinstance(scope, str) and scope_visible(store, scope, include_sensitive)):
                            source = {'kind': 'memory', 'id': item.id, 'text': item.text,
                                      'citation': f'[mem:{item.id}]',
                                      **memory_source_descriptor(item.metadata, memory_type=item.type,
                                                                 source=item.source, recorded_at=item.created_at)}
            if source is not None:
                key = source['citation']
                if key not in groups:
                    groups[key] = {'indices': [], **source}
                groups[key]['indices'].append(i)
                result['statuses'][i] = 'omitted'
        # Try the complete response first. At tighter budgets select whole
        # quotations/warnings before spending space on optional page hints.
        result['sources'] = list(groups.values())
        for source in result['sources']:
            for i in source['indices']:
                result['statuses'][i] = 'available'
        if not fits(result, max_tokens):
            result['sources'] = []
            for source in groups.values():
                for i in source['indices']:
                    result['statuses'][i] = 'omitted'
            for source in groups.values():
                result['sources'].append(without_original_page_hint(source))
                for i in source['indices']:
                    result['statuses'][i] = 'available'
                if not fits(result, max_tokens):
                    result['sources'].pop()
                    for i in source['indices']:
                        result['statuses'][i] = 'omitted'
            for position, source in enumerate(result['sources']):
                enriched = groups[source['citation']]
                if 'original_page' in enriched:
                    result['sources'][position] = enriched
                    if not fits(result, max_tokens):
                        result['sources'][position] = source
    if not fits(result, max_tokens, reserve=0):
        raise ValueError('Budget too small for reference framing')
    return budget_report(result, max_tokens)
